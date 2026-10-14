from __future__ import annotations

import pytest
from _catalog_builder import add_asset, add_decision

from foley import search, status
from foley.catalog import db
from foley.search import SearchFilters


@pytest.fixture
def conn(cfg):
    c = db.open_catalog(cfg)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _no_status(monkeypatch):
    def nope(conn):
        raise NotImplementedError

    monkeypatch.setattr(status, "staleness_note", nope)


def feats(**kw):
    base = {"duration_s": 1.0, "lufs_integrated": -20.0, "attack_s": 0.01, "is_music": 0}
    base.update(kw)
    return base


def build(conn):
    ids = {}
    ids["gore"] = add_asset(
        conn,
        "packs/Test Bundle 2026/Sinister Textures/GOREMisc_Cladding_NailScratch19_InMotionAudio.wav",  # noqa: E501
        pack="Test Bundle 2026",
        category_path="Sinister Textures",
        metadata={"ucs": {"category": "GORE", "subcategory": "MISC", "fx_name": "Cladding"}},
        features=feats(duration_s=1.2),
        caption="1.20 s misc cladding",
        tags=[("scratch", 0.9), ("metal", 0.5), ("a", 0.4), ("b", 0.3)],
        license="packs/Test Bundle 2026/LICENSE.txt",
    )
    ids["shelf"] = add_asset(
        conn,
        "packs/Test Bundle 2015/Metal Shelf/Metal Shelf Hit 10.wav",
        pack="Test Bundle 2015",
        category_path="Metal Shelf",
        features=feats(duration_s=0.6, attack_s=0.002, lufs_integrated=-12.0),
    )
    ids["scratchy"] = add_asset(
        conn,
        "sfx/scratch_scratch_scratch.wav",
        category_path="sfx",
        features=feats(duration_s=2.0),
    )
    ids["music"] = add_asset(
        conn,
        "music/Arteria3D/Battle Loop.mp3",
        category_path="music/Arteria3D",
        features=feats(duration_s=8.0, is_music=1, lufs_integrated=-14.0),
        metadata={"tags": {"title": "Epic Battle", "comment": "orchestral drums"}},
    )
    ids["ui"] = add_asset(
        conn,
        "sfx/ui_scifi_confirm.wav",
        category_path="sfx",
        features=feats(duration_s=0.12, lufs_integrated=None),
    )
    return ids


def test_split_words_and_name_tokens():
    assert search.split_words("NailScratch19") == ["nail", "scratch", "19"]
    assert search.split_words("GOREMisc_Cladding-x.y z") == [
        "gore",
        "misc",
        "cladding",
        "x",
        "y",
        "z",
    ]
    assert search.split_words("") == []
    toks = search.name_tokens("GOREMisc_Cladding_NailScratch19")
    assert {
        "goremisc",
        "gore",
        "misc",
        "cladding",
        "nailscratch19",
        "nail",
        "scratch",
        "19",
    } <= set(toks)


def test_index_asset_is_idempotent_and_has_metadata(conn):
    ids = build(conn)
    search.index_asset(conn, ids["music"])
    search.index_asset(conn, ids["music"])
    rows = conn.execute("SELECT * FROM search_fts WHERE rowid = ?", (ids["music"],)).fetchall()
    assert len(rows) == 1
    assert "epic battle" in rows[0]["metadata"].lower() and "orchestral" in rows[0]["metadata"]
    gore = conn.execute("SELECT * FROM search_fts WHERE rowid = ?", (ids["gore"],)).fetchone()
    assert "nail scratch 19" in gore["name"] or "nail" in gore["name"].split()
    assert "gore" in gore["category"].split()
    assert "scratch" in gore["tags"]
    assert "cladding" in gore["caption"]


@pytest.mark.parametrize("q", ["gore", "cladding", "nail scratch", "NailScratch19", "nail"])
def test_findable_by_filename_words(conn, q):
    ids = build(conn)
    res = search.search(conn, q)["results"]
    assert res and res[0]["id"] == ids["gore"]


def test_nail_scratch_ranks_gore_first(conn):
    ids = build(conn)
    res = search.search(conn, "nail scratch")["results"]
    assert res[0]["id"] == ids["gore"]
    assert ids["scratchy"] in [r["id"] for r in res]
    assert res[0]["score"] > res[1]["score"]


def test_prefix_and_metadata_match(conn):
    ids = build(conn)
    assert search.search(conn, "orch")["results"][0]["id"] == ids["music"]
    assert search.search(conn, "epic")["results"][0]["id"] == ids["music"]


def test_compact_result_shape(conn):
    ids = build(conn)
    r = search.search(conn, "nail scratch")["results"][0]
    assert set(r) == {
        "id",
        "path",
        "score",
        "caption",
        "tags",
        "duration_s",
        "lufs",
        "attack_s",
        "license",
        "duplicates",
    }
    assert r["tags"] == ["scratch", "metal", "a"]
    assert r["duration_s"] == 1.2 and r["license"].endswith("LICENSE.txt")
    assert r["id"] == ids["gore"]


@pytest.mark.parametrize(
    "q",
    [
        '"',
        "AND",
        "*",
        "-",
        "nail AND",
        "OR OR",
        '"nail',
        "NEAR(a b)",
        "a:b",
        "col:x",
        "(",
        ")",
        "^x",
        "' --",
        "NOT",
        "nail*scratch",
        "\\",
    ],
)
def test_hostile_queries_do_not_crash(conn, q):
    build(conn)
    out = search.search(conn, q)
    assert isinstance(out["results"], list)


def test_operator_words_are_plain_terms(conn):
    ids = build(conn)
    res = search.search(conn, "nail AND scratch NOT zzz")["results"]
    assert res[0]["id"] == ids["gore"]


def test_filters(conn):
    ids = build(conn)
    got = lambda **kw: {r["id"] for r in search.search(conn, "", SearchFilters(**kw))["results"]}  # noqa: E731
    assert got(is_music=True) == {ids["music"]}
    assert ids["music"] not in got(is_music=False)
    assert got(min_duration_s=5) == {ids["music"]}
    assert got(max_duration_s=0.7) == {ids["shelf"], ids["ui"]}
    assert got(max_attack_s=0.005) == {ids["shelf"]}
    assert got(min_lufs=-13) == {ids["shelf"]}
    assert got(max_lufs=-15) == {ids["gore"], ids["scratchy"], ids["ui"]} - {ids["ui"]}
    assert got(pack="2015") == {ids["shelf"]}
    assert got(category="MUSIC") == {ids["music"]}
    assert got(category="gore") == {ids["gore"]}  # UCS category, case-insensitive prefix
    assert got(category="Metal") == {ids["shelf"]}  # path segment prefix
    assert got(category="sinister") == {ids["gore"]}


def test_filter_with_query_and_empty_query_orders_by_path(conn):
    ids = build(conn)
    res = search.search(conn, "scratch", SearchFilters(max_duration_s=1.5))["results"]
    assert [r["id"] for r in res] == [ids["gore"]]
    listing = search.search(conn, "", None, limit=50)["results"]
    paths = [r["path"] for r in listing]
    assert paths == sorted(paths) and len(paths) == 5


def test_missing_assets_hidden_unless_included(conn):
    gone = add_asset(conn, "sfx/ghost_scratch.wav", status="missing", features=feats())
    assert gone not in [r["id"] for r in search.search(conn, "ghost")["results"]]
    res = search.search(conn, "ghost", SearchFilters(include_missing=True))["results"]
    assert [r["id"] for r in res] == [gone]


def test_duplicates_collapse(conn):
    a = add_asset(conn, "a/NailScratch19.wav", content_hash="same", features=feats())
    b = add_asset(conn, "b/NailScratch19_copy.wav", content_hash="same", features=feats())
    c = add_asset(conn, "c/NailScratch19_other.wav", content_hash="other", features=feats())
    res = search.search(conn, "nail scratch")["results"]
    ids = [r["id"] for r in res]
    assert len(ids) == 2 and c in ids
    winner = next(r for r in res if r["id"] in (a, b))
    assert winner["duplicates"] == 1
    assert next(r for r in res if r["id"] == c)["duplicates"] == 0
    listing = search.search(conn, "")["results"]
    assert len(listing) == 2


def test_rejected_hidden_for_project_only(conn):
    ids = build(conn)
    h = f"hash-{'sfx/ui_scifi_confirm.wav'}"
    add_decision(conn, h, "game-a", "rejected")
    add_decision(conn, "hash-sfx/scratch_scratch_scratch.wav", "game-a", "approved")

    def seen(proj):
        return {r["id"] for r in search.search(conn, "", project=proj, limit=50)["results"]}

    assert ids["ui"] not in seen("game-a")
    assert ids["scratchy"] in seen("game-a")
    assert ids["ui"] in seen("game-b")
    assert ids["ui"] in seen(None)


def test_note_from_status(conn, monkeypatch):
    build(conn)
    monkeypatch.setattr(status, "staleness_note", lambda c: "catalog last scanned 3 days ago")
    assert search.search(conn, "scratch")["note"] == "catalog last scanned 3 days ago"


def test_limit(conn):
    build(conn)
    assert len(search.search(conn, "", limit=2)["results"]) == 2


def test_blend_scores_seam():
    assert search.blend_scores(3.0) == 3.0
    assert search.blend_scores(3.0, 0.5, 0.0) == 3.0
    assert search.blend_scores(2.0, 1.0, 0.5) == 1.5


def test_describe(conn):
    ids = build(conn)
    h = "hash-sfx/ui_scifi_confirm.wav"
    add_decision(conn, h, "game-a", "rejected", rel_path="sfx/ui_scifi_confirm.wav")
    conn.execute(
        "INSERT INTO ledger.usages(content_hash, rel_path, project, project_path, imported_at) "
        "VALUES (?, 'x', 'game-a', 'res://a.wav', ?)",
        (h, db.utcnow()),
    )
    add_asset(conn, "dup/ui_scifi_confirm.wav", content_hash=h, features=feats())
    conn.execute(
        "INSERT INTO images(asset_id, kind, local_path, version, rendered_at) "
        "VALUES (?, 'overview', 'images/1.png', '1', ?)",
        (ids["ui"], db.utcnow()),
    )
    by_id = search.describe(conn, ids["ui"])
    by_path = search.describe(conn, "sfx/ui_scifi_confirm.wav")
    by_str = search.describe(conn, str(ids["ui"]))
    assert by_id == by_path == by_str
    assert by_id["features"]["duration_s"] == 0.12
    assert [d["path"] for d in by_id["duplicates"]] == ["dup/ui_scifi_confirm.wav"]
    assert by_id["decisions"][0]["verdict"] == "rejected"
    assert by_id["usages"][0]["project_path"] == "res://a.wav"
    assert by_id["image"]["local_path"] == "images/1.png"
    import json

    json.dumps(by_id)

    gore = search.describe(conn, ids["gore"])
    assert gore["metadata"]["ucs"]["fx_name"] == "Cladding"
    assert gore["caption"] == "1.20 s misc cladding"
    assert gore["tags"][0]["label"] == "scratch"
    music = search.describe(conn, ids["music"])
    assert music["features"]["is_music"] is True


def test_describe_unknown(conn):
    with pytest.raises(LookupError):
        search.describe(conn, 9999)
    with pytest.raises(LookupError):
        search.describe(conn, "nope.wav")
