from __future__ import annotations

from foley import dedupe, sync
from foley.catalog import db


def _add(conn, rel, content_hash, size=1000, status="active", first_seen="2026-01-01T00:00:00",
         audio_hash=None):
    conn.execute(
        "INSERT INTO assets(rel_path, ext, size_bytes, mtime_ns, content_hash, audio_hash, "
        "first_seen, last_seen, status) VALUES (?, '.wav', ?, 1, ?, ?, ?, ?, ?)",
        (rel, size, content_hash, audio_hash, first_seen, first_seen, status),
    )


def _decision(conn, content_hash, rel):
    conn.execute(
        "INSERT INTO ledger.decisions(content_hash, rel_path, project, usage_key, verdict, "
        "decided_by, decided_at) VALUES (?, ?, 'game', 'sfx.x', 'approved', 'scott', 'now')",
        (content_hash, rel),
    )


def test_exact_groups_and_report(cfg):
    conn = db.open_catalog(cfg)
    _add(conn, "a/one.wav", "h1", 100)
    _add(conn, "b/one.wav", "h1", 100)
    _add(conn, "c/one.wav", "h1", 100)
    _add(conn, "a/two.wav", "h2", 5000)
    _add(conn, "b/two_long_name.wav", "h2", 5000)
    _add(conn, "gone/two.wav", "h2", 5000, status="missing")
    _add(conn, "a/solo.wav", "h3")
    _add(conn, "p/pending.wav", "h3", status="pending")
    groups = dedupe.find_exact_groups(cfg, conn)
    assert [(g.content_hash, g.members, g.survivor, g.reclaimable_bytes) for g in groups] == [
        ("h2", ["a/two.wav", "b/two_long_name.wav"], "a/two.wav", 5000),
        ("h1", ["a/one.wav", "b/one.wav", "c/one.wav"], "a/one.wav", 200),
    ]
    rep = dedupe.report(cfg, conn)
    assert rep["exact_groups"] == 2 and rep["files_removable"] == 3
    assert rep["reclaimable_bytes"] == 5200
    assert rep["groups"][0] == {
        "content_hash": "h2", "size_bytes": 5000, "members": ["a/two.wav", "b/two_long_name.wav"],
        "survivor": "a/two.wav", "reclaimable_bytes": 5000,
    }
    assert rep["audio_identical"] == []


def test_survivor_rules_in_order(cfg):
    conn = db.open_catalog(cfg)
    paths = ["packs/Zeta/x/very/deep/a.wav", "packs/Alpha/a.wav", "sfx/a.wav", "packs/Beta/b.wav"]
    _add(conn, paths[0], "h", first_seen="2026-03-01T00:00:00")
    _add(conn, paths[1], "h", first_seen="2026-02-01T00:00:00")
    _add(conn, paths[2], "h", first_seen="2026-02-01T00:00:00")
    _add(conn, paths[3], "h", first_seen="2026-01-01T00:00:00")

    # oldest first_seen wins when nothing else applies
    assert dedupe.choose_survivor(cfg, conn, paths, "h") == "packs/Beta/b.wav"
    # keep_priority beats age; first matching prefix in the list wins
    cfg.dedupe_keep_priority = ["packs/Nope", "PACKS\\Alpha", "sfx/"]
    assert dedupe.choose_survivor(cfg, conn, paths, "h") == "packs/Alpha/a.wav"
    # ledger rows beat keep_priority
    _decision(conn, "h", "packs/Zeta/x/very/deep/a.wav")
    assert dedupe.choose_survivor(cfg, conn, paths, "h") == "packs/Zeta/x/very/deep/a.wav"


def test_survivor_ties_shortest_then_lexicographic(cfg):
    conn = db.open_catalog(cfg)
    for p in ["b/xx.wav", "a/xx.wav", "a/longer.wav"]:
        _add(conn, p, "h")
    assert dedupe.choose_survivor(cfg, conn, ["b/xx.wav", "a/longer.wav", "a/xx.wav"], "h") == (
        "a/xx.wav"
    )
    # ledger rows on several copies: tie-break continues among them
    _decision(conn, "h", "b/xx.wav")
    _decision(conn, "h", "a/longer.wav")
    assert dedupe.choose_survivor(cfg, conn, ["b/xx.wav", "a/longer.wav", "a/xx.wav"], "h") == (
        "b/xx.wav"
    )
    # a ledger path no longer among the copies is ignored
    assert dedupe.choose_survivor(cfg, conn, ["a/xx.wav", "c/xx.wav"], "h") == "a/xx.wav"


def test_usages_count_as_ledger_rows(cfg):
    conn = db.open_catalog(cfg)
    _add(conn, "a.wav", "h")
    _add(conn, "zz/b.wav", "h")
    conn.execute(
        "INSERT INTO ledger.usages(content_hash, rel_path, project, project_path, imported_at) "
        "VALUES ('h', 'zz/b.wav', 'game', 'res://b.wav', 'now')"
    )
    assert dedupe.find_exact_groups(cfg, conn)[0].survivor == "zz/b.wav"


def test_audio_identical_groups(cfg):
    conn = db.open_catalog(cfg)
    _add(conn, "a.wav", "c1", audio_hash="A")
    _add(conn, "b.wav", "c2", audio_hash="A")  # same audio, different bytes
    _add(conn, "c.wav", "c2", audio_hash="A")
    _add(conn, "d.wav", "c3", audio_hash="B")
    _add(conn, "e.wav", "c3", audio_hash="B")  # exact duplicates only: not audio-identical
    _add(conn, "f.wav", "c4", audio_hash="A", status="missing")
    assert dedupe.find_audio_identical_groups(conn) == [["a.wav", "b.wav", "c.wav"]]


def test_fixture_library_duplicate(cfg, monkeypatch):
    from test_sync import install_fakes

    install_fakes(monkeypatch)
    sync.run_index(cfg)
    conn = db.open_catalog(cfg)
    groups = dedupe.find_exact_groups(cfg, conn)
    assert len(groups) == 1
    g = groups[0]
    assert all("NailScratch19" in m for m in g.members) and len(g.members) == 2
    assert g.survivor.endswith("GOREMisc_Cladding_NailScratch19_copy.wav")  # shortest path
    cfg.dedupe_keep_priority = [
        "packs/Test Bundle 2026/Sinister Textures/GOREMisc_Cladding_NailScratch19_In"
    ]
    assert dedupe.find_exact_groups(cfg, conn)[0].survivor.endswith("SinisterTextures5.wav")
    assert g.reclaimable_bytes == g.size_bytes > 0
