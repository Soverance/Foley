"""`foley browse`: real HTTP calls against a server on an ephemeral port."""

from __future__ import annotations

import http.client
import io
import json
import logging
import logging.handlers
import shutil
import socket
import sys
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, urlencode

import audiogen as ag
import numpy as np
import pytest
from _catalog_builder import add_asset

from foley import browse, images, scans, status, sync
from foley.catalog import db
from foley.config import Config

STEREO_AIFF = "sfx/stereo_sweep.aiff"
ODD = {  # catalog-only rows (not on disk) for folder escaping, sorting and filters
    "odd/100%_done/a.wav": {"duration_s": 1.0, "lufs_integrated": -30.0},
    "odd/100x_done/b.wav": {"duration_s": 2.0, "lufs_integrated": -10.0},
    "odd/100%Xdone/c.wav": {"duration_s": 3.0, "lufs_integrated": None},
    "odd/a_b/x.wav": {"duration_s": None},
    "odd/axb/y.wav": {"duration_s": 0.5},
    "odd/music/Theme Song.mp3": {"duration_s": 30.0, "is_music": 1, "lufs_integrated": -16.0},
}
# What the test servers' scan scheduler runs instead of a real `foley index`.
SCAN_CMD = {"cmd": [sys.executable, "-c", "print('fake scan')"]}


def fake_scans(cfg) -> scans.ScanScheduler:
    sch = scans.ScanScheduler(cfg, command=lambda: SCAN_CMD["cmd"])
    sch.poll_s = 0.05
    return sch


@pytest.fixture(scope="module")
def env(tmp_path_factory, _library_template):
    base = tmp_path_factory.mktemp("browse")
    library = base / "library"
    shutil.copytree(_library_template, library, copy_function=shutil.copy2)
    ag.write(library / STEREO_AIFF, ag.stereo(ag.tone(440, 0.5), ag.tone(880, 0.5)))
    cfg = Config(library_root=library, data_dir=base / "data", settle_minutes=0)
    summary = sync.run_index(cfg, workers=1)
    assert summary.errors == 0
    conn = db.open_catalog(cfg)
    for rel, feats in ODD.items():
        add_asset(conn, rel, features=feats, caption=f"caption for {rel}")
    conn.close()

    server = browse.make_server(cfg, "127.0.0.1", 0, scans=fake_scans(cfg))
    server.app.check_interval_s = 0.0  # see catalog changes immediately
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield cfg, server
    finally:
        server.app.scans.shutdown(stop_running=True)
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def _quiet_status(monkeypatch):
    monkeypatch.setattr(status, "staleness_note", lambda conn: None)


def request(server, path, method="GET", body=None, headers=None):
    port = server.server_address[1]
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    try:
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode()
            headers = {"Content-Type": "application/json", **(headers or {})}
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        return r, r.read()
    finally:
        c.close()


def get_json(server, path, expect=200, **kw):
    r, data = request(server, path, **kw)
    assert r.status == expect, (r.status, data[:300])
    assert r.getheader("Content-Type").startswith("application/json")
    return json.loads(data)


def ids_by_path(cfg) -> dict[str, int]:
    conn = db.open_catalog(cfg)
    try:
        return {r[1]: r[0] for r in conn.execute("SELECT id, rel_path FROM assets")}
    finally:
        conn.close()


def listing(server, **params):
    return get_json(server, "/api/list?" + urlencode(params))


def all_paths(server, **params):
    return [r["path"] for r in listing(server, limit=1000, **params)["results"]]


# ----------------------------------------------------------------------------- status/tree


def test_status(env):
    cfg, server = env
    st = get_json(server, "/api/status")
    assert st["assets"]["active"] == len(ids_by_path(cfg))
    assert set(st["assets"]) == {"active", "missing", "error", "pending"}
    assert st["has_tags"] is False and st["has_embeddings"] is False
    assert st["loudness_target_lufs"] == -20.0
    assert isinstance(st["shortlist_count"], int) and "note" in st
    assert st["library_root"]
    assert set(st["scan"]) == {"running", "enabled", "next_run_at", "last_completed_at",
                               "trigger", "started_at", "progress"}
    assert st["scan"]["running"] is False and st["scan"]["last_completed_at"]


def test_tree_counts(env):
    _, server = env
    root = get_json(server, "/api/tree")
    assert root["path"] == "" and root["files"] == 0
    names = [f["name"] for f in root["folders"]]
    assert names == sorted(names, key=str.casefold)
    counts = {f["name"]: f["count"] for f in root["folders"]}
    assert counts == {"music": 1, "odd": 6, "packs": 7, "sfx": 4, "voice": 1}
    assert root["folders"][0]["path"] == root["folders"][0]["name"]

    sfx = get_json(server, "/api/tree?path=sfx")
    assert sfx["files"] == 3  # CancelMaybe, click, the stereo AIFF
    assert sfx["folders"] == [{"name": "lightsabers", "path": "sfx/lightsabers", "count": 1}]
    bundle = get_json(server, "/api/tree?" + urlencode({"path": "packs/Test Bundle 2015/"}))
    assert bundle["path"] == "packs/Test Bundle 2015" and bundle["files"] == 0
    assert [(f["name"], f["count"]) for f in bundle["folders"]] == [
        ("Metal Shelf", 2), ("Water 01", 1)
    ]
    get_json(server, "/api/tree?path=nope", expect=404)


# ----------------------------------------------------------------------------- listing


def test_listing_sorted_by_name_with_paging(env):
    cfg, server = env
    first = listing(server)
    assert first["total"] == len(ids_by_path(cfg)) and first["offset"] == 0
    assert first["limit"] == 200 and first["note"] is None
    names = [r["name"] for r in first["results"]]
    assert names == sorted(names, key=lambda n: n.casefold())
    row = next(r for r in first["results"] if r["path"] == "sfx/click.wav")
    assert row["folder"] == "sfx" and row["ext"] == ".wav" and row["score"] is None
    assert row["caption"] and row["duration_s"] == pytest.approx(0.05, abs=0.01)
    assert row["sample_rate"] == 48000 and row["channels"] == 1
    assert row["tags"] == [] and row["starred"] is False and row["duplicates"] == 0
    assert {"lufs", "true_peak_db", "attack_s"} <= set(row)

    a = listing(server, limit=4)["results"]
    b = listing(server, limit=4, offset=4)
    assert b["total"] == first["total"] and b["offset"] == 4
    assert [r["id"] for r in a + b["results"]] == [r["id"] for r in first["results"][:8]]
    assert listing(server, offset=10_000)["results"] == []
    assert listing(server, limit=5000)["limit"] == 1000


def test_listing_shows_duplicates_uncollapsed(env):
    _, server = env
    rows = listing(server, folder="packs/Test Bundle 2026/Sinister Textures")["results"]
    assert len(rows) == 2 and all(r["duplicates"] == 1 for r in rows)


def test_folder_is_a_literal_prefix(env):
    _, server = env
    assert all_paths(server, folder="odd/100%_done") == ["odd/100%_done/a.wav"]
    assert all_paths(server, folder="odd/a_b") == ["odd/a_b/x.wav"]
    assert all_paths(server, folder="odd/a") == []  # a folder, not a string prefix
    assert all_paths(server, folder="ODD") == []  # case-sensitive
    assert len(all_paths(server, folder="/odd/")) == 6
    assert all_paths(server, folder="packs/Test Bundle 2015/Metal Shelf") == [
        "packs/Test Bundle 2015/Metal Shelf/Metal Shelf Hit 10.wav",
        "packs/Test Bundle 2015/Metal Shelf/Metal Shelf Tonal Texture 07.wav",
    ]


def test_search_mode_ranks_collapses_and_respects_folder(env):
    _, server = env
    res = listing(server, q="nail scratch")
    assert res["total"] >= 1
    top = res["results"][0]
    assert "NailScratch19" in top["path"] and top["score"] is not None
    assert top["duplicates"] == 1
    assert sum("NailScratch19" in r["path"] for r in res["results"]) == 1  # collapsed

    metal = all_paths(server, q="metal", folder="packs/Test Bundle 2015")
    assert metal and all(p.startswith("packs/Test Bundle 2015/") for p in metal)
    assert all_paths(server, q="metal", folder="sfx") == []
    assert all_paths(server, q="done", folder="odd/100%_done") == ["odd/100%_done/a.wav"]

    by_name = listing(server, q="metal shelf", sort="name")["results"]
    names = [r["name"] for r in by_name]
    assert names == sorted(names, key=str.casefold) and all(r["score"] for r in by_name)
    # no real search terms -> plain listing
    assert listing(server, q="the")["total"] == listing(server)["total"]


def test_filters(env):
    _, server = env
    long = listing(server, min_duration=2, limit=1000)["results"]
    assert long and all(r["duration_s"] >= 2 for r in long)
    short = listing(server, max_duration=0.5, limit=1000)["results"]
    assert short and all(r["duration_s"] <= 0.5 for r in short)
    assert all(r["path"] != "odd/a_b/x.wav" for r in long + short)  # unknown never matches

    loud = listing(server, min_lufs=-20, max_lufs=-5, limit=1000)["results"]
    assert loud and all(-20 <= r["lufs"] <= -5 for r in loud)
    sharp = listing(server, max_attack=0.01, limit=1000)["results"]
    assert sharp and all(r["attack_s"] <= 0.01 for r in sharp)

    assert all_paths(server, is_music=1) == ["odd/music/Theme Song.mp3"]
    assert "odd/music/Theme Song.mp3" not in all_paths(server, is_music=0)
    assert len(all_paths(server, is_music=0)) == listing(server)["total"] - 1

    assert {Path(p).suffix for p in all_paths(server, ext="ogg")} == {".ogg"}
    wavs = all_paths(server, ext=".wav")
    assert "sfx/lightsabers/lasrhit3.WAV" in wavs
    assert {Path(p).suffix.lower() for p in all_paths(server, ext="ogg,mp3")} == {".ogg", ".mp3"}
    combo = listing(server, folder="odd", min_duration=1, max_lufs=-20, limit=1000)
    assert [r["path"] for r in combo["results"]] == ["odd/100%_done/a.wav"]

    for bad in ({"min_lufs": "loud"}, {"limit": "x"}, {"offset": -1}, {"sort": "random"},
                {"is_music": "maybe"}, {"limit": 0}, {"max_duration": "nan"}):
        get_json(server, "/api/list?" + urlencode(bad), expect=400)


def test_sorting(env):
    _, server = env

    def column(sort, key):
        return [r[key] for r in listing(server, sort=sort, limit=1000)["results"]]

    for sort, key in (("duration", "duration_s"), ("lufs", "lufs")):
        asc, desc = column(sort, key), column("-" + sort, key)
        known = [v for v in asc if v is not None]
        assert known == sorted(known) and asc[len(known):] == [None] * (len(asc) - len(known))
        known_d = [v for v in desc if v is not None]
        assert known_d == sorted(known_d, reverse=True) and desc[-1] is None
    names = column("-name", "name")
    assert names == sorted(names, key=str.casefold, reverse=True)
    paths = column("path", "path")
    assert paths == sorted(paths)


def test_listing_sees_catalog_changes(env):
    cfg, server = env
    before = listing(server)["total"]
    conn = db.open_catalog(cfg)
    aid = add_asset(conn, "odd/late/new_file.wav", features={"duration_s": 1.0})
    conn.close()
    try:
        assert listing(server)["total"] == before + 1
        assert get_json(server, "/api/tree?path=odd/late")["files"] == 1
    finally:
        conn = db.open_catalog(cfg)
        conn.execute("DELETE FROM search_fts WHERE rowid = ?", (aid,))
        conn.execute("DELETE FROM assets WHERE id = ?", (aid,))
        conn.close()
    assert listing(server)["total"] == before


# ----------------------------------------------------------------------------- asset/audio


def test_asset_endpoint(env):
    cfg, server = env
    ids = ids_by_path(cfg)
    aid = ids["packs/Test Bundle 2015/Metal Shelf/Metal Shelf Hit 10.wav"]
    a = get_json(server, f"/api/asset/{aid}")
    assert a["id"] == aid and a["path"].endswith("Metal Shelf Hit 10.wav")
    assert a["features"]["duration_s"] > 0.5 and a["caption"]
    assert {"metadata", "tags", "duplicates", "decisions", "usages", "license"} <= set(a)
    assert a["audio_url"] == f"/api/audio/{aid}"
    assert a["image_url"] == f"/api/image/{aid}" and a["peaks_url"] == f"/api/peaks/{aid}"
    assert a["image_plot_x"] == [round(62 / 1024, 6), round(1 - 62 / 1024, 6)]
    assert a["image_plot_x"] == [round(x, 6) for x in images.OVERVIEW_PLOT_X]
    assert a["starred"] is False and a["star_note"] is None
    assert a["name"] == "Metal Shelf Hit 10.wav"

    for path in ("/api/asset/999999", "/api/asset/abc", "/api/audio/999999",
                 "/api/image/999999", "/api/peaks/999999", "/api/asset/1/extra", "/api/nope"):
        err = get_json(server, path, expect=404)
        assert err["error"]


def _file_bytes(cfg, rel):
    return cfg.abs_path(rel).read_bytes()


def test_audio_full_and_ranges(env):
    cfg, server = env
    ids = ids_by_path(cfg)
    rel = "packs/Test Bundle 2015/Metal Shelf/Metal Shelf Hit 10.wav"
    data = _file_bytes(cfg, rel)
    r, body = request(server, f"/api/audio/{ids[rel]}")
    assert r.status == 200 and body == data
    assert r.getheader("Content-Type") == "audio/wav"
    assert r.getheader("Accept-Ranges") == "bytes"
    assert int(r.getheader("Content-Length")) == len(data)

    r, body = request(server, f"/api/audio/{ids[rel]}", headers={"Range": "bytes=100-199"})
    assert r.status == 206 and body == data[100:200]
    assert r.getheader("Content-Range") == f"bytes 100-199/{len(data)}"
    r, body = request(server, f"/api/audio/{ids[rel]}", headers={"Range": "bytes=-10"})
    assert r.status == 206 and body == data[-10:]
    r, body = request(server, f"/api/audio/{ids[rel]}", headers={"Range": "bytes=50-"})
    assert r.status == 206 and body == data[50:]
    r, body = request(server, f"/api/audio/{ids[rel]}",
                      headers={"Range": f"bytes=0-{len(data) * 2}"})
    assert r.status == 206 and body == data
    r, body = request(server, f"/api/audio/{ids[rel]}",
                      headers={"Range": f"bytes={len(data)}-"})
    assert r.status == 416 and r.getheader("Content-Range") == f"bytes */{len(data)}"
    assert json.loads(body)["error"]
    r, body = request(server, f"/api/audio/{ids[rel]}", headers={"Range": "lines=1-2"})
    assert r.status == 200 and body == data  # unsupported unit: whole file

    for rel, ctype in (("music/Arteria3D/Battle Loop.mp3", "audio/mpeg"),
                       ("packs/Oculus Like Pack/03_weather/ambient_wind_through_trees_01_lp.ogg",
                        "audio/ogg"),
                       ("packs/Test Bundle 2015/Water 01/Water_Fountain_Fienup_002.flac",
                        "audio/flac")):
        r, body = request(server, f"/api/audio/{ids[rel]}")
        assert r.status == 200 and r.getheader("Content-Type") == ctype
        assert body == _file_bytes(cfg, rel)

    r, body = request(server, f"/api/audio/{ids['odd/axb/y.wav']}")  # row without a file
    assert r.status == 404


def _parse_wav(body: bytes):
    with wave.open(io.BytesIO(body)) as w:
        return w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()


def test_transcode_serves_valid_wav(env):
    cfg, server = env
    ids = ids_by_path(cfg)
    r, body = request(server, f"/api/audio/{ids['music/Arteria3D/Battle Loop.mp3']}?transcode=1")
    assert r.status == 200 and r.getheader("Content-Type") == "audio/wav"
    channels, width, rate, frames = _parse_wav(body)
    assert (channels, width) == (1, 2) and rate > 0
    assert frames / rate == pytest.approx(8.0, abs=0.2)

    # AIFF is transcoded automatically, stereo kept; Range works on the WAV too.
    r, body = request(server, f"/api/audio/{ids[STEREO_AIFF]}")
    assert r.status == 200 and r.getheader("Content-Type") == "audio/wav"
    channels, width, rate, frames = _parse_wav(body)
    assert (channels, width, rate) == (2, 2, 48000) and frames == pytest.approx(24000, abs=10)
    r, part = request(server, f"/api/audio/{ids[STEREO_AIFF]}", headers={"Range": "bytes=0-43"})
    assert r.status == 206 and part == body[:44] and part.startswith(b"RIFF")


# ----------------------------------------------------------------------------- image/peaks


def test_image_png(env):
    cfg, server = env
    ids = ids_by_path(cfg)
    aid = ids["sfx/CancelMaybe.wav"]
    r, body = request(server, f"/api/image/{aid}")
    assert r.status == 200 and r.getheader("Content-Type") == "image/png"
    assert body.startswith(b"\x89PNG\r\n\x1a\n")
    t = time.perf_counter()
    r2, body2 = request(server, f"/api/image/{aid}")
    assert r2.status == 200 and body2 == body and time.perf_counter() - t < 1.0  # cached
    r, body = request(server, f"/api/image/{ids['odd/axb/y.wav']}")
    assert r.status == 404 and "missing" in json.loads(body)["error"]


def test_peaks_shape_and_cache(env, monkeypatch):
    cfg, server = env
    ids = ids_by_path(cfg)
    aid = ids["packs/Test Bundle 2015/Metal Shelf/Metal Shelf Tonal Texture 07.wav"]
    p = get_json(server, f"/api/peaks/{aid}?n=100")
    assert p["n"] == 100 and p["channels"] == 1
    assert p["duration_s"] == pytest.approx(3.0, abs=0.01)
    assert len(p["min"]) == len(p["max"]) == 1 and len(p["min"][0]) == 100
    lo, hi = np.array(p["min"]), np.array(p["max"])
    assert (lo <= hi).all() and lo.min() >= -1 and hi.max() <= 1
    assert p["peak"] == pytest.approx(10 ** (-12 / 20), abs=0.01)  # the -12 dBFS tone
    assert all(round(v, 4) == v for v in p["max"][0])

    cached = list((cfg.data_dir / "peaks").rglob("*-100-*.json"))
    assert len(cached) == 1

    def boom(row):
        raise AssertionError("peaks should come from the cache")

    monkeypatch.setattr(server.app, "decode", boom)
    assert get_json(server, f"/api/peaks/{aid}?n=100") == p
    monkeypatch.undo()

    # The decode fallback (formats libsndfile cannot stream) gives the same bins.
    streamed = get_json(server, f"/api/peaks/{aid}?n=333")
    monkeypatch.setattr(browse, "stream_peaks", lambda path, n: None)
    decoded = get_json(server, f"/api/peaks/{ids['sfx/CancelMaybe.wav']}?n=333")
    monkeypatch.undo()
    again = get_json(server, f"/api/peaks/{ids['sfx/CancelMaybe.wav']}?n=334")
    assert streamed["n"] == 333 and decoded["n"] == 333
    expected = browse.compute_peaks(_decode(cfg, "sfx/CancelMaybe.wav").samples, 333)
    assert decoded["max"] == expected[2] and decoded["min"] == expected[1]
    assert len(again["max"][0]) == 334

    stereo = get_json(server, f"/api/peaks/{ids[STEREO_AIFF]}")
    assert stereo["channels"] == 2 and stereo["n"] == 1600
    assert len(stereo["min"]) == 2 and len(stereo["max"][1]) == 1600
    assert get_json(server, f"/api/peaks/{aid}?n=1")["n"] == 16  # clamped
    get_json(server, f"/api/peaks/{aid}?n=lots", expect=400)


def _decode(cfg, rel):
    from foley import decode

    return decode.decode_path(cfg.abs_path(rel))


@pytest.mark.parametrize("block", [777, 1 << 18])
def test_stream_peaks_matches_in_memory(env, monkeypatch, block):
    cfg, _ = env
    monkeypatch.setattr(browse, "_PEAK_BLOCK", block)  # several blocks per bin and vice versa
    for rel in ("packs/Test Bundle 2015/Water 01/Water_Fountain_Fienup_002.flac", STEREO_AIFF):
        audio = _decode(cfg, rel)
        duration, streamed = browse.stream_peaks(cfg.abs_path(rel), 500)
        assert duration == pytest.approx(audio.duration_s)
        assert streamed == browse.compute_peaks(audio.samples, 500)
    assert browse.stream_peaks(cfg.abs_path("packs/Test Bundle 2015/LICENSE.txt"), 10) is None


def test_compute_peaks_downmix_and_tiny_files():
    x = np.zeros((10, 6), np.float32)
    x[3] = 0.5
    n, lo, hi, peak = browse.compute_peaks(x, 1600)
    assert n == 10 and len(lo) == 1 and hi[0][3] == 0.5 and peak == 0.5
    st = np.stack([np.linspace(-1, 1, 1000), np.zeros(1000)], axis=1).astype(np.float32)
    n, lo, hi, peak = browse.compute_peaks(st, 4)
    assert n == 4 and len(lo) == 2 and lo[0][0] == -1.0 and hi[0][-1] == 1.0 and peak == 1.0


def test_parse_range():
    pr = browse.parse_range
    assert pr(None, 100) is None and pr("bytes=0-9", 100) == (0, 9)
    assert pr("bytes=90-", 100) == (90, 99) and pr("bytes=-5", 100) == (95, 99)
    assert pr("bytes=0-999", 100) == (0, 99) and pr("bytes=-500", 100) == (0, 99)
    assert pr("bytes=5-1", 100) is None and pr("bytes=a-b", 100) is None
    assert pr("bytes=0-1,5-6", 100) is None and pr("items=0-1", 100) is None
    for bad in ("bytes=100-", "bytes=-0", "bytes=200-300"):
        with pytest.raises(browse._Unsatisfiable):
            pr(bad, 100)


# ----------------------------------------------------------------------------- shortlist


def test_shortlist_round_trip_keyed_by_content_hash(env):
    cfg, server = env
    ids = ids_by_path(cfg)
    folder = "packs/Test Bundle 2026/Sinister Textures"
    original = f"{folder}/GOREMisc_Cladding_NailScratch19_InMotionAudio_SinisterTextures5.wav"
    copy = f"{folder}/GOREMisc_Cladding_NailScratch19_copy.wav"

    out = get_json(server, "/api/shortlist", method="POST",
                   body={"id": ids[original], "starred": True, "note": "for boss hit?"})
    assert out == {"starred": True, "note": "for boss hit?"}
    try:
        stored = json.loads(browse.shortlist_path(cfg).read_text(encoding="utf-8"))
        conn = db.open_catalog(cfg)
        h = conn.execute("SELECT content_hash FROM assets WHERE id = ?",
                         (ids[original],)).fetchone()[0]
        conn.close()
        assert list(stored) == [h] and stored[h]["rel_path"] == original
        assert stored[h]["note"] == "for boss hit?" and stored[h]["added_at"]

        items = get_json(server, "/api/shortlist")["items"]
        assert len(items) == 1
        item = items[0]
        assert item["id"] == ids[original] and item["path"] == original
        assert item["content_hash"] == h and item["note"] == "for boss hit?"
        assert item["caption"] and item["duration_s"] == pytest.approx(1.2, abs=0.01)

        # the exact copy shares the content hash, so it shows as starred too
        assert sorted(all_paths(server, starred=1)) == sorted([original, copy])
        rows = listing(server, folder=folder)["results"]
        assert all(r["starred"] for r in rows)
        a = get_json(server, f"/api/asset/{ids[copy]}")
        assert a["starred"] is True and a["star_note"] == "for boss hit?"
        assert get_json(server, "/api/status")["shortlist_count"] == 1

        # starring again without a note keeps it; with a note replaces it
        out = get_json(server, "/api/shortlist", method="POST",
                       body={"id": ids[copy], "starred": True})
        assert out == {"starred": True, "note": "for boss hit?"}
        out = get_json(server, "/api/shortlist", method="POST",
                       body={"id": ids[original], "starred": True, "note": "nope, intro"})
        assert out["note"] == "nope, intro"
        assert get_json(server, "/api/shortlist")["items"][0]["note"] == "nope, intro"
    finally:
        out = get_json(server, "/api/shortlist", method="POST",
                       body={"id": ids[original], "starred": False})
    assert out == {"starred": False, "note": None}
    assert get_json(server, "/api/shortlist") == {"items": []}
    assert all_paths(server, starred=1) == []

    for bad in ({"starred": True}, {"id": ids[original]}, {"id": "x", "starred": True},
                {"id": ids[original], "starred": True, "note": 5}):
        get_json(server, "/api/shortlist", method="POST", body=bad, expect=400)
    get_json(server, "/api/shortlist", method="POST", body=b"[1]", expect=400,
             headers={"Content-Type": "application/json"})
    get_json(server, "/api/shortlist", method="POST", body={"id": 999999, "starred": True},
             expect=404)


# ----------------------------------------------------------------------------- security


def test_host_header_and_origin_checks(env):
    cfg, server = env
    port = server.server_address[1]
    for host in ("evil.example.com", f"evil.example.com:{port}", f"192.168.1.20:{port}",
                 "localhost.evil.com"):
        err = get_json(server, "/api/status", expect=403, headers={"Host": host})
        assert "host" in err["error"]
    r, _ = request(server, "/", headers={"Host": "evil.example.com"})
    assert r.status == 403
    for host in (f"localhost:{port}", f"127.0.0.1:{port}", "localhost", f"[::1]:{port}"):
        get_json(server, "/api/status", headers={"Host": host})

    aid = ids_by_path(cfg)["sfx/click.wav"]
    get_json(server, "/api/shortlist", method="POST", body={"id": aid, "starred": True},
             headers={"Origin": "http://evil.example.com"}, expect=403)
    get_json(server, "/api/shortlist", method="POST", body={"id": aid, "starred": True},
             headers={"Origin": f"http://localhost:{port + 1}"}, expect=403)
    assert get_json(server, "/api/shortlist") == {"items": []}
    out = get_json(server, "/api/shortlist", method="POST",
                   body={"id": aid, "starred": True},
                   headers={"Origin": f"http://127.0.0.1:{port}"})
    assert out["starred"] is True
    get_json(server, "/api/shortlist", method="POST", body={"id": aid, "starred": False})


def test_static_whitelist(env):
    _, server = env
    r, body = request(server, "/")
    assert r.status == 200 and r.getheader("Content-Type").startswith("text/html")
    assert b"<html" in body.lower() or b"<!doctype" in body.lower()
    for path in ("/static/../../pyproject.toml", "/static/browse.py", "/static/" +
                 quote("..\\browse.py"), "/static/", "/static/APP.JS"):
        r, _ = request(server, path)
        assert r.status == 404


def test_placeholder_when_ui_missing(env, monkeypatch, tmp_path):
    _, server = env
    monkeypatch.setattr(browse, "UI_DIR", tmp_path / "no-ui")
    r, body = request(server, "/")
    assert r.status == 200 and b"Foley Browse" in body
    r, _ = request(server, "/static/app.js")
    assert r.status == 404


def test_is_loopback():
    assert browse.is_loopback("127.0.0.1") and browse.is_loopback("localhost")
    assert browse.is_loopback("::1") and browse.is_loopback("[::1]")
    assert browse.is_loopback("127.0.0.2")
    assert not browse.is_loopback("0.0.0.0") and not browse.is_loopback("192.168.1.2")
    assert not browse.is_loopback("example.com")


def test_cli_parses_browse_options():
    from foley import cli

    args = cli.build_parser().parse_args(["browse"])
    # None = take [browse] host / port from foley.toml (defaults 127.0.0.1:8765)
    assert (args.host, args.port, args.no_open, args.log_file) == (None, None, False, None)
    args = cli.build_parser().parse_args(["browse", "--port", "9000", "--no-open",
                                          "--host", "0.0.0.0", "--log-file", "x.log"])
    assert (args.host, args.port, args.no_open, args.log_file) == ("0.0.0.0", 9000, True, "x.log")


def test_serve_warns_when_not_loopback(cfg, monkeypatch):
    lines: list[str] = []

    class FakeScans:
        def start(self):
            lines.append("scheduler started")

        def shutdown(self, stop_running=True):
            lines.append(f"scheduler stopped (stop_running={stop_running})")

    class FakeServer:
        url = "http://0.0.0.0:1/"
        server_address = ("0.0.0.0", 80)
        app = SimpleNamespace(scans=FakeScans())

        def serve_forever(self, poll_interval=0.5):
            raise KeyboardInterrupt

        def server_close(self):
            lines.append("closed")

    monkeypatch.setattr(browse, "make_server", lambda *a, **k: FakeServer())
    browse.serve(cfg, host="0.0.0.0", port=1, open_browser=False, out=lines.append)
    assert any("WARNING" in line for line in lines) and lines[-1] == "closed"
    assert "scheduler started" in lines and "scheduler stopped (stop_running=True)" in lines
    lines.clear()
    browse.serve(cfg, host="127.0.0.1", port=1, open_browser=False, out=lines.append,
                 schedule=False)
    assert not any("WARNING" in line for line in lines) and "scheduler started" not in lines
    # with allowed_hosts configured, a non-loopback bind gets a one-line notice
    lines.clear()
    cfg.browse_allowed_hosts = ["foley.example.lan"]
    browse.serve(cfg, host="0.0.0.0", port=80, open_browser=False, out=lines.append)
    assert not any("WARNING" in line for line in lines)
    notices = [line for line in lines if "local network" in line]
    assert len(notices) == 1 and "http://foley.example.lan/" in notices[0]


def test_config_reads_browse_table(tmp_path):
    from foley import config

    cfg = config.from_dict({"library": {"root": str(tmp_path / "lib")},
                            "storage": {"data_dir": str(tmp_path / "data")},
                            "browse": {"loudness_target_lufs": -18}})
    assert cfg.browse_loudness_target_lufs == -18.0
    base = {"library": {"root": str(tmp_path / "lib")},
            "storage": {"data_dir": str(tmp_path / "data")}}
    cfg = config.from_dict(base)
    assert cfg.browse_loudness_target_lufs == -20.0
    assert (cfg.browse_host, cfg.browse_port, cfg.browse_allowed_hosts) == ("127.0.0.1", 8765, [])
    cfg = config.from_dict({**base, "browse": {
        "host": "0.0.0.0", "port": 80, "allowed_hosts": ["Foley.Example.LAN", "[fe80::1]:80"]}})
    assert (cfg.browse_host, cfg.browse_port) == ("0.0.0.0", 80)
    assert cfg.browse_allowed_hosts == ["foley.example.lan", "fe80::1"]
    for bad in ({"port": "80"}, {"port": 70000}, {"port": True}, {"allowed_hosts": "x.lan"},
                {"allowed_hosts": [1]}):
        with pytest.raises(config.ConfigError):
            config.from_dict({**base, "browse": bad})


# ----------------------------------------------------------------------------- scans


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def _wait_idle(server, timeout=60) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = get_json(server, "/api/scans")
        if snap["running"] is None:
            return snap
        time.sleep(0.1)
    raise AssertionError("scan did not finish")


def test_scans_api(env):
    cfg, server = env
    snap = get_json(server, "/api/scans")
    assert set(snap) >= {"schedule", "server_time", "timezone", "utc_offset", "hostname",
                         "next_run_at", "catch_up_at", "running", "history",
                         "last_completed_at", "startup_delay_minutes"}
    assert snap["schedule"] == {"enabled": False, "time": "03:00", "catch_up": True}
    assert snap["next_run_at"] is None and snap["running"] is None
    assert snap["last_completed_at"] and snap["server_time"] and snap["timezone"]

    for bad in ({"time": "25:00"}, {"enabled": "yes"}, {"bogus": 1}, {"catch_up": None}):
        err = get_json(server, "/api/scans/schedule", method="POST", body=bad, expect=400)
        assert err["error"]
    get_json(server, "/api/scans/schedule", method="POST", body=b"", expect=400)
    try:
        out = get_json(server, "/api/scans/schedule", method="POST",
                       body={"enabled": True, "time": "4:30"})
        assert out["schedule"] == {"enabled": True, "time": "04:30", "catch_up": True}
        assert "T04:30:00" in out["next_run_at"]
        assert (cfg.data_dir / "schedule.json").exists()
        st = get_json(server, "/api/status")["scan"]
        assert st["enabled"] is True and st["next_run_at"] == out["next_run_at"]
    finally:
        get_json(server, "/api/scans/schedule", method="POST", body={"enabled": False})
    assert get_json(server, "/api/scans")["next_run_at"] is None

    # a manual scan: 409 while it runs, live progress in /api/status, then history + log
    get_json(server, "/api/scans/stop", method="POST", expect=409)
    SCAN_CMD["cmd"] = _py("import time\nprint('scanning the fixture ...', flush=True)\n"
                          "time.sleep(1.5)\nprint('run 999: scanned 0, added 0')")
    started = get_json(server, "/api/scans/run", method="POST")
    assert started["running"]["trigger"] == "manual"
    get_json(server, "/api/scans/run", method="POST", body={}, expect=409)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        st = get_json(server, "/api/status")["scan"]
        if st["progress"] == "scanning the fixture ...":
            break
        time.sleep(0.05)
    assert st["running"] is True and st["trigger"] == "manual" and st["started_at"]
    snap = _wait_idle(server)
    last = snap["history"][0]
    assert last["outcome"] == "ok" and last["trigger"] == "manual" and last["run_id"] == 999
    assert last["run"] is None and last["duration_s"] >= 1.0  # no index_runs row 999
    r, body = request(server, f"/api/scans/log/{last['log']}")
    assert r.status == 200 and r.getheader("Content-Type").startswith("text/plain")
    assert b"scanning the fixture" in body
    for bad in ("scans.json", quote("../schedule.json", safe=""), "", "scan-1.log",
                quote("..\\scans.json", safe="")):
        r, _ = request(server, f"/api/scans/log/{bad}")
        assert r.status == 404, bad

    # stop
    SCAN_CMD["cmd"] = _py("import time\nprint('long scan', flush=True)\ntime.sleep(120)")
    get_json(server, "/api/scans/run", method="POST")
    get_json(server, "/api/scans/stop", method="POST")
    stopped = _wait_idle(server)["history"][0]
    assert stopped["outcome"] == "stopped"


def test_allowed_hosts_and_origin(env, tmp_path):
    cfg, _ = env
    cfg2 = Config(library_root=cfg.library_root, data_dir=tmp_path / "data",
                  browse_allowed_hosts=["foley.example.lan"])
    server = browse.make_server(cfg2, "127.0.0.1", 0, scans=fake_scans(cfg2))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        for host in ("foley.example.lan", "FOLEY.Example.LAN:80", "foley.example.lan.",
                     f"foley.example.lan:{port}", f"localhost:{port}"):
            get_json(server, "/api/scans", headers={"Host": host})
        for host in ("other.example.lan", "foley.example.lan.evil.com", "example.lan",
                     "[foley.example.lan"):
            r, _ = request(server, "/api/status", headers={"Host": host})
            assert r.status == 403, host

        body = {"catch_up": False}
        lan = {"Host": "foley.example.lan"}
        get_json(server, "/api/scans/schedule", method="POST", body=body, expect=403,
                 headers={**lan, "Origin": "http://evil.example.com"})
        get_json(server, "/api/scans/schedule", method="POST", body=body, expect=403,
                 headers={**lan, "Origin": f"http://localhost:{port}"})
        get_json(server, "/api/scans/schedule", method="POST", body=body, expect=403,
                 headers={**lan, "Origin": "https://foley.example.lan"})
        out = get_json(server, "/api/scans/schedule", method="POST", body=body,
                       headers={**lan, "Origin": "http://foley.example.lan"})
        assert out["schedule"]["catch_up"] is False
    finally:
        server.shutdown()
        server.server_close()


def test_machine_names_only_for_non_loopback_binds(tmp_path):
    cfg = Config(library_root=tmp_path / "lib", data_dir=tmp_path / "data",
                 browse_allowed_hosts=["foley.example.lan"])
    me = socket.gethostname().lower()
    wide = browse.BrowseApp(cfg, "0.0.0.0")
    assert me in wide.allowed_hosts and "foley.example.lan" in wide.allowed_hosts
    assert "0.0.0.0" not in wide.allowed_hosts
    local = browse.BrowseApp(cfg, "127.0.0.1")
    assert local.allowed_hosts == {"foley.example.lan", "127.0.0.1"}
    assert browse.BrowseApp(cfg, "192.0.2.10").allowed_hosts >= {"192.0.2.10", me}


@pytest.fixture
def foley_logging():
    logger = logging.getLogger("foley")
    yield logger
    for h in list(logger.handlers):
        if getattr(h, "_foley_browse", False):
            logger.removeHandler(h)
            h.close()
    logger.propagate = True
    logger.setLevel(logging.NOTSET)


def _ours(logger):
    return [h for h in logger.handlers if getattr(h, "_foley_browse", False)]


def test_configure_logging_without_console(foley_logging, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)  # as under pythonw.exe
    monkeypatch.setattr(sys, "stderr", None)
    path = tmp_path / "logs" / "browse.log"
    browse.configure_logging(path)
    handlers = _ours(foley_logging)
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.handlers.RotatingFileHandler)
    logging.getLogger("foley.scans").info("scan started")
    logging.getLogger("foley.browse").error("boom")
    browse.configure_logging(None)  # replaces, never stacks
    assert [type(h) for h in _ours(foley_logging)] == [logging.NullHandler]
    logging.getLogger("foley.browse").warning("dropped quietly")
    text = path.read_text(encoding="utf-8")
    assert "INFO    foley.scans: scan started" in text and "ERROR   foley.browse: boom" in text


def test_browse_port_in_use_is_logged_and_exits_nonzero(foley_logging, tmp_path, monkeypatch):
    from foley import cli

    toml = tmp_path / "foley.toml"
    toml.write_text(
        f"[library]\nroot = '{(tmp_path / 'lib').as_posix()}'\n"
        f"[storage]\ndata_dir = '{(tmp_path / 'data').as_posix()}'\n",
        encoding="utf-8",
    )
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    port = blocker.getsockname()[1]
    log_file = tmp_path / "browse.log"
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    try:
        rc = cli.main(["--config", str(toml), "browse", "--no-open", "--port", str(port),
                       "--log-file", str(log_file)])
    finally:
        blocker.close()
    assert rc == 2
    text = log_file.read_text(encoding="utf-8")
    assert "starting" in text and f"cannot serve on 127.0.0.1:{port}" in text

    rc = cli.main(["--config", str(tmp_path / "nope.toml"), "browse", "--log-file",
                   str(log_file)])
    assert rc == 2 and "no foley.toml found" in log_file.read_text(encoding="utf-8")
