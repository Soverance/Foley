"""`foley browse`: hybrid listing, /api/similar, semantic status and test-set mode."""

from __future__ import annotations

import http.client
import json
import sys
import threading
from urllib.parse import quote, urlencode

import _fake_clap as fc
import pytest
from _catalog_builder import add_asset

from foley import browse, scans, status, testset
from foley.catalog import db
from foley.config import Config

SOUNDS = {
    "sfx/dog/Dog_Bark_01.wav": ("dog bark animal", 1.0),
    "sfx/misc/xq_17.wav": ("dog bark animal growl", 0.8),
    "sfx/misc/zz_04.wav": ("glass shatter break", 2.0),
    "sfx/impacts/Metal_Hit_03.wav": ("metal impact clang", 0.5),
    "sfx/impacts/copies/Metal_Hit_03.wav": ("metal impact clang", 0.5),
    "sfx/impacts/Metal_Hit_04.wav": ("metal impact clang ring", 0.7),
    "music/Battle Loop.mp3": ("orchestral drums battle", 30.0),
}

HEADER = "# Game test set — private.\n# Hand-edited header, kept verbatim.\n\n"


@pytest.fixture(autouse=True)
def _quiet_status(monkeypatch):
    monkeypatch.setattr(status, "staleness_note", lambda conn: None)


@pytest.fixture
def env(tmp_path, monkeypatch):
    fc.install(monkeypatch)
    lib = tmp_path / "library"
    lib.mkdir()
    ts_path = tmp_path / "private" / "testset.toml"
    ts_path.parent.mkdir()
    ts_path.write_text(
        HEADER + '[[query]]\ntext = "metal clang"\ncue = "sfx:hit"\ngood = []\n\n'
        '[[query]]\ntext = "pickup chime"\ngood = []\nfilters = { max_duration_s = 2.0 }\n',
        encoding="utf-8")
    cfg = Config(library_root=lib, data_dir=tmp_path / "data", testset=ts_path)
    conn = db.open_catalog(cfg)
    ids = {}
    for rel, (words, dur) in SOUNDS.items():
        h = "hash-metal" if rel.endswith("Metal_Hit_03.wav") else "auto"
        ids[rel] = add_asset(conn, rel, features={"duration_s": dur,
                                                  "is_music": int(rel.startswith("music"))},
                             content_hash=h)
        fc.add_embedding(conn, ids[rel], words)
    conn.close()
    sch = scans.ScanScheduler(cfg, command=lambda: [sys.executable, "-c", "print(1)"])
    server = browse.make_server(cfg, "127.0.0.1", 0, scans=sch)
    server.app.check_interval_s = 0.0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield cfg, server, ids
    finally:
        server.shutdown()
        server.server_close()


def call(server, path, method="GET", body=None, expect=200):
    c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=60)
    try:
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=headers)
        r = c.getresponse()
        payload = r.read()
        assert r.status == expect, (r.status, payload[:300])
        return json.loads(payload)
    finally:
        c.close()


def test_status_reports_semantic_state_and_testset(env):
    cfg, server, _ = env
    st = call(server, "/api/status")
    assert st["semantic"] == {"available": True, "model": "fake-a", "weight": 0.5,
                              "embeddings": 7, "encoder": "unloaded", "error": None}
    assert st["has_testset"] is True and st["testset_path"] == str(cfg.testset)
    call(server, "/api/list?" + urlencode({"q": "dog"}))
    assert call(server, "/api/status")["semantic"]["encoder"] == "ready"


def test_hybrid_listing(env):
    _, server, ids = env
    out = call(server, "/api/list?" + urlencode({"q": "growling animal"}))
    assert out["mode"] == "hybrid" and out["model"] == "fake-a"
    paths = [r["path"] for r in out["results"]]
    assert "sfx/misc/xq_17.wav" in paths  # semantic-only: no keyword in the name
    row = out["results"][0]
    assert 0 < row["score"] <= 1 and row["similarity"] is not None
    out = call(server, "/api/list?" + urlencode({"q": "growling animal", "max_duration": 0.9}))
    assert [r["path"] for r in out["results"]] == ["sfx/misc/xq_17.wav"]
    out = call(server, "/api/list?" + urlencode({"q": "dog", "folder": "sfx/dog"}))
    assert [r["path"] for r in out["results"]] == ["sfx/dog/Dog_Bark_01.wav"]
    plain = call(server, "/api/list")
    assert plain["mode"] is None and plain["results"][0]["similarity"] is None


def test_listing_without_embeddings_says_keyword_only(env):
    cfg, server, _ = env
    conn = db.open_catalog(cfg)
    conn.execute("DELETE FROM embeddings")
    conn.close()
    out = call(server, "/api/list?" + urlencode({"q": "dog"}))
    assert out["mode"] == "keyword" and "no CLAP embeddings" in out["note"]
    assert call(server, "/api/status")["semantic"]["available"] is False


def test_similar_endpoint(env):
    _, server, ids = env
    src = ids["sfx/impacts/Metal_Hit_03.wav"]
    out = call(server, f"/api/similar/{src}")
    assert out["mode"] == "similar" and out["source"] == {
        "id": src, "path": "sfx/impacts/Metal_Hit_03.wav", "name": "Metal_Hit_03.wav"}
    paths = [r["path"] for r in out["results"]]
    assert paths[0] == "sfx/impacts/Metal_Hit_04.wav"
    assert "sfx/impacts/copies/Metal_Hit_03.wav" not in paths  # its own copy is left out
    first = out["results"][0]
    assert set(first) >= {"id", "path", "name", "folder", "duration_s", "tags", "starred",
                          "similarity", "score"}
    assert first["similarity"] == first["score"] and out["total"] == len(SOUNDS) - 2
    sims = [r["similarity"] for r in out["results"]]
    assert sims == sorted(sims, reverse=True)
    page = call(server, f"/api/similar/{src}?limit=2&offset=1")
    assert [r["path"] for r in page["results"]] == paths[1:3]
    music = call(server, f"/api/similar/{src}?is_music=1")
    assert [r["path"] for r in music["results"]] == ["music/Battle Loop.mp3"]
    by_name = call(server, f"/api/similar/{src}?sort=name")
    names = [r["name"].casefold() for r in by_name["results"]]
    assert names == sorted(names)
    call(server, "/api/similar/999999", expect=404)


# ----------------------------------------------------------------------------- test set


def test_testset_view_mark_unmark_and_round_trip(env):
    cfg, server, ids = env
    view = call(server, "/api/testset")
    assert view["path"] == str(cfg.testset) and view["max_answers"] == testset.MAX_ANSWERS
    q0, q1 = view["queries"]
    assert (q0["text"], q0["cue"], q0["answers"]) == ("metal clang", "sfx:hit", 0)
    assert q1["filters"] == {"max_duration_s": 2.0}

    a = ids["sfx/impacts/Metal_Hit_03.wav"]
    out = call(server, "/api/testset/mark", "POST",
               {"query_index": 0, "id": a, "good": True, "text": "metal clang"})
    assert out["changed"] is True and out["warning"] is None
    good = out["query"]["good"]
    assert [g["path"] for g in good] == ["sfx/impacts/Metal_Hit_03.wav"]
    assert set(good[0]["ids"]) == {a, ids["sfx/impacts/copies/Metal_Hit_03.wav"]}
    # marking an exact copy is a no-op
    out = call(server, "/api/testset/mark", "POST",
               {"query_index": 0, "id": ids["sfx/impacts/copies/Metal_Hit_03.wav"],
                "good": True})
    assert out["changed"] is False and out["query"]["answers"] == 1

    text = cfg.testset.read_text(encoding="utf-8")
    assert text.startswith(HEADER) and 'cue = "sfx:hit"' in text
    assert 'good = [\n    "sfx/impacts/Metal_Hit_03.wav",\n]' in text
    assert "filters = { max_duration_s = 2.0 }" in text
    assert [f.name for f in cfg.testset.parent.iterdir()] == ["testset.toml"]

    # unmarking through the copy removes the listed twin
    out = call(server, "/api/testset/mark", "POST",
               {"query_index": 0, "id": ids["sfx/impacts/copies/Metal_Hit_03.wav"],
                "good": False})
    assert out["changed"] is True and out["query"]["answers"] == 0
    assert testset.load(cfg.testset).queries[0].good == []


def test_testset_cap_warning_and_errors(env):
    cfg, server, ids = env
    for rel in list(SOUNDS)[:4]:
        out = call(server, "/api/testset/mark", "POST",
                   {"query_index": 1, "id": ids[rel], "good": True})
    assert out["query"]["answers"] == 4 and "4 answers" in out["warning"]
    call(server, "/api/testset/mark", "POST", {"query_index": 9, "id": 1, "good": True},
         expect=404)
    call(server, "/api/testset/mark", "POST",
         {"query_index": 0, "id": 1, "good": True, "text": "edited elsewhere"}, expect=409)
    call(server, "/api/testset/mark", "POST", {"query_index": 0, "id": 1}, expect=400)
    call(server, "/api/testset/mark", "POST", {"query_index": 0, "id": 99999, "good": True},
         expect=404)
    # a hand edit between requests is respected (the file is re-read)
    ts = testset.load(cfg.testset)
    ts.queries[0].good.append("sfx/hand/edited.wav")
    testset.save(ts)
    view = call(server, "/api/testset")
    hand = view["queries"][0]["good"][-1]
    assert hand == {"path": "sfx/hand/edited.wav", "id": None, "status": None, "ids": []}


def test_testset_add_and_delete_queries(env):
    cfg, server, _ = env
    out = call(server, "/api/testset/query", "POST",
               {"text": "  ÿ unicode   query ", "cue": "ui:ok",
                "filters": {"max_duration_s": 1}})
    assert out["index"] == 2 and out["query"]["text"] == "ÿ unicode query"
    assert testset.load(cfg.testset).queries[2].filters == {"max_duration_s": 1.0}
    call(server, "/api/testset/query", "POST", {"text": ""}, expect=400)
    call(server, "/api/testset/query", "POST", {"text": "x", "filters": {"loud": 1}},
         expect=400)
    call(server, "/api/testset/query/2?text=" + quote("nope"), "DELETE", expect=409)
    out = call(server, "/api/testset/query/2?text=" + quote("ÿ unicode query"), "DELETE")
    assert out == {"deleted": 2, "count": 2}
    call(server, "/api/testset/query/7", "DELETE", expect=404)
    assert cfg.testset.read_text(encoding="utf-8").startswith(HEADER)


def test_testset_hidden_without_config_and_bad_file(env, tmp_path):
    cfg, server, _ = env
    cfg.testset.write_text("[[query]\n", encoding="utf-8")
    assert "cannot read" in call(server, "/api/testset", expect=422)["error"]
    cfg.testset = None
    assert call(server, "/api/status")["has_testset"] is False
    call(server, "/api/testset", expect=404)


def test_delete_needs_same_origin(env):
    _, server, _ = env
    c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
    try:
        c.request("DELETE", "/api/testset/query/0", headers={"Origin": "http://evil.example"})
        assert c.getresponse().status == 403
    finally:
        c.close()
