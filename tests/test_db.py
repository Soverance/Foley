from foley.catalog import db


def test_catalog_and_ledger_migrate_and_attach(cfg):
    conn = db.open_catalog(cfg)
    assert db.schema_version(conn) == db.latest_version(db.CATALOG) >= 1
    assert db.get_meta(conn, "library_root") == str(cfg.library_root)
    conn.execute("SELECT count(*) FROM ledger.decisions").fetchone()
    conn.execute("SELECT count(*) FROM search_fts").fetchone()
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_migrate_is_idempotent(cfg):
    db.open_catalog(cfg).close()
    conn = db.open_catalog(cfg)
    assert db.schema_version(conn) == db.latest_version(db.CATALOG)
    ledger = db.open_ledger(cfg)
    assert db.schema_version(ledger) == db.latest_version(db.LEDGER)


def test_fixture_library_shape(library):
    assert (library / "sfx/lightsabers/lasrhit3.WAV").stat().st_size > 0
    assert (library / "music/Arteria3D/Battle Loop.mp3").stat().st_size > 0
