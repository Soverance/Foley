from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime, timedelta

import audiogen as ag
import pytest
from conftest import LIBRARY_FILES, OLD_MTIME

from foley import status, sync
from foley.catalog import db

N_AUDIO = len(LIBRARY_FILES)


@pytest.fixture
def indexed(cfg, monkeypatch):
    from test_sync import install_fakes

    install_fakes(monkeypatch)
    sync.run_index(cfg)
    return cfg


def test_status_on_empty_catalog(cfg):
    conn = db.open_catalog(cfg)
    st = status.catalog_status(cfg, conn)
    assert st["assets"] == {"active": 0, "pending": 0, "missing": 0, "error": 0, "total": 0}
    assert st["last_scan"] is None and st["stale_hours"] is None
    assert st["schema_versions"]["catalog"] >= 1 and st["schema_versions"]["ledger"] >= 1
    assert status.staleness_note(conn).startswith("catalog has no completed scan")
    json.dumps(st)


def test_status_after_index(indexed, library):
    cfg = indexed
    conn = db.open_catalog(cfg)
    usage_hash = conn.execute(
        "SELECT content_hash FROM assets WHERE rel_path='sfx/click.wav'"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO ledger.usages(content_hash, rel_path, project, project_path, imported_at) "
        "VALUES (?, 'sfx/click.wav', 'endless', 'res://click.wav', 'now')", (usage_hash,)
    )
    conn.execute(
        "INSERT INTO ledger.usages(content_hash, rel_path, project, project_path, imported_at) "
        "VALUES ('nohash', 'sfx/gone_long_ago.wav', 'endless', 'res://x.wav', 'now')"
    )
    db.set_meta(conn, "models", json.dumps({"clap": "laion/clap-htsat-unfused"}))
    conn.close()

    (library / "sfx/click.wav").unlink()
    bad = library / "sfx/broken.wav"
    bad.write_bytes(b"BAD!")
    os.utime(bad, (OLD_MTIME, OLD_MTIME))
    ag.write(library / "sfx/fresh.wav", ag.tone(300, 0.1))  # settling
    sync.run_index(cfg)

    conn = db.open_catalog(cfg)
    st = status.catalog_status(cfg, conn)
    json.dumps(st)
    assert st["library_root"] == str(cfg.library_root)
    assert st["assets"] == {
        "active": N_AUDIO - 1, "pending": 0, "missing": 1, "error": 1, "total": N_AUDIO + 1
    }
    assert st["last_scan"]["status"] == "completed" and st["last_scan"]["run_id"] == 2
    assert 0 <= st["stale_hours"] < 0.1
    assert st["pending_files"] == 0 and st["settling_files"] == 1
    assert st["archives_not_extracted"] == ["packs/Test Bundle 2026/Extras.zip"]
    assert st["archives_not_extracted_count"] == 1
    assert st["duplicates"]["exact_groups"] == 1 and st["duplicates"]["reclaimable_bytes"] > 0
    assert st["duplicates"]["audio_identical_groups"] == 0
    assert st["used_but_missing"] == [
        {"rel_path": "sfx/click.wav", "project": "endless"},
        {"rel_path": "sfx/gone_long_ago.wav", "project": "endless"},
    ]
    assert st["models"] == {"clap": "laion/clap-htsat-unfused"}
    assert st["errors"] == [{"rel_path": "sfx/broken.wav", "error": st["errors"][0]["error"]}]
    assert st["error_count"] == 1
    assert status.staleness_note(conn) is None


def test_extracted_archive_is_not_reported(indexed, library):
    folder = library / "packs/Test Bundle 2026/Extras"
    ag.write(folder / "inside.wav", ag.click())
    os.utime(folder / "inside.wav", (OLD_MTIME, OLD_MTIME))
    sync.run_index(indexed)
    conn = db.open_catalog(indexed)
    st = status.catalog_status(indexed, conn)
    assert st["archives_not_extracted"] == [] and st["archives_not_extracted_count"] == 0


def test_staleness_note(indexed):
    conn = db.open_catalog(indexed)
    assert status.staleness_note(conn) is None
    old = (datetime.now(UTC) - timedelta(days=3, hours=1)).isoformat(timespec="seconds")
    conn.execute("UPDATE index_runs SET finished_at=?", (old,))
    assert status.staleness_note(conn) == "catalog last scanned 3 days ago"
    conn.execute("UPDATE assets SET status='pending' WHERE id IN (SELECT id FROM assets LIMIT 41)")
    assert status.staleness_note(conn) == (
        f"catalog last scanned 3 days ago; {N_AUDIO} files pending"
    )
    recent = (datetime.now(UTC) - timedelta(hours=30)).isoformat(timespec="seconds")
    conn.execute("UPDATE index_runs SET finished_at=?", (recent,))
    assert status.staleness_note(conn).startswith("catalog last scanned 30 hours ago")
    st = status.catalog_status(indexed, conn)
    assert 29.9 < st["stale_hours"] < 30.1


def test_interrupted_run_does_not_count_as_completed(indexed, library, monkeypatch):
    from foley import features

    def interrupt(audio, kind):
        raise KeyboardInterrupt

    shutil.copy2(library / "sfx/click.wav", library / "sfx/click2.wav")
    data = (library / "sfx/click2.wav").read_bytes()
    (library / "sfx/click2.wav").write_bytes(data + bytes(2))
    os.utime(library / "sfx/click2.wav", (OLD_MTIME, OLD_MTIME))
    monkeypatch.setattr(features, "compute_features", interrupt)
    assert sync.run_index(indexed).interrupted
    conn = db.open_catalog(indexed)
    st = status.catalog_status(indexed, conn)
    assert st["last_scan"]["status"] == "interrupted"
    assert st["pending_files"] == 1
    assert status.staleness_note(conn) == "1 file pending"
