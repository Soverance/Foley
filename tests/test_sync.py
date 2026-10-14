"""Library sync: every row of the "Library sync" rules table, resume, locking, prune.

Logic tests replace the DSP / caption / FTS functions with fast fakes (install_fakes)
so they test sync itself; test_end_to_end_real_pipeline runs the real modules.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import audiogen as ag
import numpy as np
import pytest
import xxhash
from conftest import LIBRARY_FILES, OLD_MTIME

from foley import captions, decode, features, loudness, scan, search, sync
from foley.catalog import db

N_AUDIO = len(LIBRARY_FILES)
GORE = (
    "packs/Test Bundle 2026/Sinister Textures/"
    "GOREMisc_Cladding_NailScratch19_InMotionAudio_SinisterTextures5.wav"
)
LASER = "sfx/lightsabers/lasrhit3.WAV"
CLICK = "sfx/click.wav"

_AUDIO_MAGIC = (b"RIFF", b"RF64", b"fLaC", b"OggS", b"FORM")


def _looks_like_audio(data: bytes) -> bool:
    if data[:4] in _AUDIO_MAGIC or data[:3] == b"ID3":
        return True
    return len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0


def install_fakes(monkeypatch) -> Counter:
    """Replace every sibling pipeline function with a fast fake; returns call counts."""
    calls: Counter = Counter()

    def read_bytes(path):
        calls["read"] += 1
        with open(path, "rb") as f:
            return f.read()

    def content_hash(data):
        return xxhash.xxh3_128_hexdigest(data)

    def decode_bytes(data, ext):
        calls["decode"] += 1
        if not _looks_like_audio(data):
            raise decode.DecodeError(f"not a decodable {ext} file")
        digest = np.frombuffer(xxhash.xxh3_128(data).digest(), dtype=np.uint8)
        samples = (digest.astype(np.float32) / 255.0).reshape(-1, 1)
        return decode.DecodedAudio(samples, 48000, 48000)

    def audio_hash(audio):
        return xxhash.xxh3_128_hexdigest(audio.samples.tobytes())

    def compute_features(audio, kind_hint):
        calls["features"] += 1
        calls[f"kind:{kind_hint}"] += 1
        return {
            "duration_s": 1.0, "sample_rate": 48000, "channels": 1,
            "lead_silence_s": 0.0, "tail_silence_s": 0.1, "attack_s": 0.01, "decay_s": 0.2,
            "onset_count": 1, "centroid_hz_mean": 2000.0, "rolloff_hz_mean": 6000.0,
            "flatness_mean": 0.1, "zcr_mean": 0.05,
            "band_energy": {"sub": 0.1, "low": 0.1, "low_mid": 0.1, "mid": 0.3,
                            "upper_mid": 0.2, "presence": 0.1, "air": 0.1},
            "tempo_bpm": None, "loop_seam_score": None,
        }

    def measure(audio):
        calls["loudness"] += 1
        return {"lufs_integrated": -20.0, "lufs_short_max": -18.0, "true_peak_db": -3.0}

    def templated_caption(asset, feats, tags, metadata):
        calls["caption"] += 1
        assert tags == []
        return f"caption for {asset['rel_path']} ({feats.get('lufs_integrated')} LUFS)"

    def index_asset(conn, asset_id):
        calls["fts"] += 1
        rel = conn.execute("SELECT rel_path FROM assets WHERE id=?", (asset_id,)).fetchone()[0]
        conn.execute("DELETE FROM search_fts WHERE rowid=?", (asset_id,))
        conn.execute("INSERT INTO search_fts(rowid, name) VALUES (?, ?)", (asset_id, rel))

    monkeypatch.setattr(decode, "read_bytes", read_bytes)
    monkeypatch.setattr(decode, "content_hash", content_hash)
    monkeypatch.setattr(decode, "decode_bytes", decode_bytes)
    monkeypatch.setattr(decode, "audio_hash", audio_hash)
    monkeypatch.setattr(features, "compute_features", compute_features)
    monkeypatch.setattr(loudness, "measure", measure)
    monkeypatch.setattr(captions, "templated_caption", templated_caption)
    monkeypatch.setattr(search, "index_asset", index_asset)
    return calls


@pytest.fixture
def fakes(monkeypatch) -> Counter:
    return install_fakes(monkeypatch)


def _conn(cfg):
    return db.open_catalog(cfg)


def _asset(cfg, rel):
    conn = _conn(cfg)
    try:
        row = conn.execute("SELECT * FROM assets WHERE rel_path=?", (rel,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _statuses(cfg) -> Counter:
    conn = _conn(cfg)
    try:
        return Counter(r[0] for r in conn.execute("SELECT status FROM assets"))
    finally:
        conn.close()


def _one(conn, sql: str, *args):
    return conn.execute(sql, args).fetchone()[0]


def _lock_json(pid: int) -> str:
    return json.dumps({"pid": pid, "started_at": "x", "started_ts": time.time()})


def _old(path: Path, t: int = OLD_MTIME) -> None:
    os.utime(path, (t, t))


def _write_old(path: Path, x, t: int = OLD_MTIME) -> None:
    ag.write(path, x)
    _old(path, t)


# --------------------------------------------------------------------------- basics


def test_first_run_indexes_everything(cfg, fakes):
    lines: list[str] = []
    s = sync.run_index(cfg, progress=lines.append)
    assert (s.scanned, s.added, s.analyzed, s.errors, s.archives) == (
        N_AUDIO, N_AUDIO, N_AUDIO, 0, 1
    )
    assert (s.changed, s.moved, s.missing, s.settling, s.interrupted) == (0, 0, 0, 0, False)
    assert fakes["read"] == N_AUDIO and fakes["decode"] == N_AUDIO
    assert _statuses(cfg) == Counter(active=N_AUDIO)

    conn = _conn(cfg)
    versions = sync.stage_versions()
    stages = conn.execute("SELECT stage, version FROM asset_stages").fetchall()
    assert Counter(r[0] for r in stages) == Counter({st: N_AUDIO for st in sync.STAGES})
    assert all(r[1] == versions[r[0]] for r in stages)
    assert conn.execute("SELECT count(*) FROM features").fetchone()[0] == N_AUDIO
    band = conn.execute("SELECT band_energy, lufs_integrated FROM features LIMIT 1").fetchone()
    assert json.loads(band[0])["mid"] == 0.3 and band[1] == -20.0
    assert {r[0] for r in conn.execute("SELECT model FROM captions")} == {captions.CAPTION_MODEL}
    assert conn.execute("SELECT count(*) FROM search_fts").fetchone()[0] == N_AUDIO
    assert [r[0] for r in conn.execute("SELECT rel_path FROM archives")] == [
        "packs/Test Bundle 2026/Extras.zip"
    ]
    run = conn.execute("SELECT * FROM index_runs").fetchone()
    assert run["status"] == "completed" and run["finished_at"] and run["analyzed"] == N_AUDIO
    assert json.loads(run["stages"]) == list(sync.STAGES)

    gore = dict(conn.execute("SELECT * FROM assets WHERE rel_path=?", (GORE,)).fetchone())
    assert gore["pack"] == "Test Bundle 2026" and gore["category_path"] == "Sinister Textures"
    assert gore["content_hash"] and gore["audio_hash"] and gore["indexed_at"]
    meta = json.loads(gore["source_metadata"])
    assert meta["ucs"]["cat_id"] == "GOREMisc"
    laser = dict(conn.execute("SELECT * FROM assets WHERE rel_path=?", (LASER,)).fetchone())
    assert laser["ext"] == ".wav" and laser["pack"] is None
    assert laser["category_path"] == "sfx/lightsabers"
    lic = conn.execute(
        "SELECT license FROM assets WHERE rel_path LIKE 'packs/Test Bundle 2015/%'"
    ).fetchall()
    assert {r[0] for r in lic} == {"packs/Test Bundle 2015/LICENSE.txt"}
    paths = {r[0] for r in conn.execute("SELECT rel_path FROM assets")}
    assert not any(p.endswith((".gpk", ".mdd", ".txt", ".md", ".zip")) for p in paths)
    conn.close()

    # kind hints reach the feature stage
    assert fakes["kind:music"] == 1 and fakes["kind:voice"] == 1
    assert fakes["kind:ambience"] >= 1
    assert any(line.startswith("scanned") for line in lines)
    assert any(line.startswith("analyzed") for line in lines)


def test_second_run_is_a_no_op(cfg, fakes):
    sync.run_index(cfg)
    fakes.clear()
    s = sync.run_index(cfg)
    assert (s.scanned, s.added, s.changed, s.moved, s.missing, s.analyzed, s.errors) == (
        N_AUDIO, 0, 0, 0, 0, 0, 0
    )
    assert sum(fakes.values()) == 0  # not a single library read
    assert _statuses(cfg) == Counter(active=N_AUDIO)


def test_unknown_stage_rejected(cfg, fakes):
    with pytest.raises(ValueError, match="unknown stage"):
        sync.run_index(cfg, stages=["tags"])


# --------------------------------------------------------------------------- rules table


def test_new_file_is_analyzed(cfg, library, fakes):
    sync.run_index(cfg)
    _write_old(library / "sfx/new_boom.wav", ag.burst(0.4, seed=9))
    fakes.clear()
    s = sync.run_index(cfg)
    assert (s.added, s.analyzed) == (1, 1)
    assert fakes["read"] == 1
    assert _asset(cfg, "sfx/new_boom.wav")["status"] == "active"


def test_changed_file_reanalyzed_with_same_id(cfg, library, fakes):
    sync.run_index(cfg)
    before = _asset(cfg, CLICK)
    _write_old(library / CLICK, ag.tone(300, 0.5), t=OLD_MTIME + 100)
    fakes.clear()
    s = sync.run_index(cfg)
    assert (s.changed, s.added, s.analyzed) == (1, 0, 1)
    assert fakes["decode"] == 1 and fakes["features"] == 1 and fakes["fts"] == 1
    after = _asset(cfg, CLICK)
    assert after["id"] == before["id"]
    assert after["status"] == "active"
    assert after["content_hash"] != before["content_hash"]
    assert after["size_bytes"] != before["size_bytes"]
    assert after["first_seen"] == before["first_seen"]


def test_touched_file_with_same_bytes_counts_as_changed(cfg, library, fakes):
    sync.run_index(cfg)
    before = _asset(cfg, CLICK)
    _old(library / CLICK, OLD_MTIME + 5)
    s = sync.run_index(cfg)
    assert s.changed == 1
    after = _asset(cfg, CLICK)
    assert after["id"] == before["id"] and after["content_hash"] == before["content_hash"]
    assert after["mtime_ns"] == (OLD_MTIME + 5) * 10**9


def test_missing_file_keeps_row_and_reappears(cfg, library, fakes, tmp_path):
    sync.run_index(cfg)
    before = _asset(cfg, LASER)
    stash = tmp_path / "stash.wav"
    shutil.move(library / LASER, stash)
    s = sync.run_index(cfg)
    assert s.missing == 1 and s.analyzed == 0
    row = _asset(cfg, LASER)
    assert row["status"] == "missing" and row["missing_since"]
    assert row["id"] == before["id"] and row["content_hash"] == before["content_hash"]
    conn = _conn(cfg)
    assert _one(conn, "SELECT count(*) FROM features WHERE asset_id=?", row["id"]) == 1
    conn.close()

    # A third run keeps it missing without recounting it.
    assert sync.run_index(cfg).missing == 0

    shutil.move(stash, library / LASER)
    fakes.clear()
    s = sync.run_index(cfg)
    assert (s.added, s.missing, s.analyzed) == (0, 0, 0)
    assert fakes["read"] == 0
    row = _asset(cfg, LASER)
    assert row["status"] == "active" and row["missing_since"] is None and row["id"] == before["id"]


def test_moved_file_keeps_id_and_analysis(cfg, library, fakes):
    sync.run_index(cfg)
    before = _asset(cfg, GORE)
    conn = _conn(cfg)
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO ledger.decisions(content_hash, rel_path, project, usage_key, verdict, "
            "decided_by, decided_at) VALUES (?, ?, 'game', 'sfx.gore', 'approved', 'scott', ?)",
            (before["content_hash"], GORE, db.utcnow()),
        )
    feats_sql = "SELECT * FROM features WHERE asset_id=?"
    feats_before = dict(conn.execute(feats_sql, (before["id"],)).fetchone())
    conn.close()

    new_rel = "sfx/gore/GOREMisc_Cladding_NailScratch19_InMotionAudio_SinisterTextures5.wav"
    (library / "sfx/gore").mkdir()
    shutil.move(library / GORE, library / new_rel)
    fakes.clear()
    s = sync.run_index(cfg)
    assert (s.moved, s.added, s.missing, s.analyzed, s.errors) == (1, 0, 0, 1, 0)
    assert fakes["read"] == 1  # hashed once to detect the move
    assert fakes["decode"] == 0 and fakes["features"] == 0 and fakes["loudness"] == 0
    assert fakes["caption"] == 1 and fakes["fts"] == 1  # path-derived stages re-run

    assert _asset(cfg, GORE) is None
    after = _asset(cfg, new_rel)
    assert after["id"] == before["id"]
    assert after["status"] == "active" and after["missing_since"] is None
    assert after["first_seen"] == before["first_seen"]
    assert after["content_hash"] == before["content_hash"]
    assert after["pack"] is None and after["category_path"] == "sfx/gore"
    assert after["license"] is None
    conn = _conn(cfg)
    assert conn.execute("SELECT count(*) FROM assets").fetchone()[0] == N_AUDIO
    feats_sql = "SELECT * FROM features WHERE asset_id=?"
    feats_after = dict(conn.execute(feats_sql, (after["id"],)).fetchone())
    assert feats_after == feats_before
    assert _one(conn, "SELECT name FROM search_fts WHERE rowid=?", after["id"]) == new_rel
    # the ledger decision still joins to the asset by content_hash
    hit = conn.execute(
        "SELECT a.rel_path FROM ledger.decisions d "
        "JOIN assets a ON a.content_hash = d.content_hash "
        "WHERE a.status='active' AND a.id=?", (after["id"],)
    ).fetchone()
    assert hit[0] == new_rel
    conn.close()

    # Idempotent afterwards.
    fakes.clear()
    assert sync.run_index(cfg).analyzed == 0 and fakes["read"] == 0


def test_move_detected_across_runs(cfg, library, fakes, tmp_path):
    sync.run_index(cfg)
    before = _asset(cfg, CLICK)
    shutil.move(library / CLICK, tmp_path / "click.wav")
    assert sync.run_index(cfg).missing == 1
    shutil.move(tmp_path / "click.wav", library / "sfx/ui_click_renamed.wav")
    s = sync.run_index(cfg)
    assert (s.moved, s.added, s.missing) == (1, 0, 0)
    assert _asset(cfg, "sfx/ui_click_renamed.wav")["id"] == before["id"]


def test_copy_of_existing_file_is_new_not_moved(cfg, library, fakes):
    sync.run_index(cfg)
    shutil.copy2(library / CLICK, library / "sfx/click_copy.wav")
    s = sync.run_index(cfg)
    assert (s.added, s.moved) == (1, 0)
    assert _asset(cfg, CLICK)["status"] == "active"


def test_settling_files_are_skipped_until_settled(cfg, library, fakes):
    sync.run_index(cfg)
    fresh = library / "sfx/fresh.wav"
    ag.write(fresh, ag.tone(500, 0.3))  # mtime = now
    _write_old(library / CLICK, ag.tone(800, 0.2))
    os.utime(library / CLICK, None)  # an existing file being rewritten right now
    before = _asset(cfg, CLICK)
    s = sync.run_index(cfg)
    assert (s.settling, s.added, s.changed, s.analyzed, s.missing) == (2, 0, 0, 0, 0)
    assert _asset(cfg, "sfx/fresh.wav") is None
    assert _asset(cfg, CLICK)["size_bytes"] == before["size_bytes"]

    _old(fresh, OLD_MTIME + 10)
    _old(library / CLICK, OLD_MTIME + 10)
    s = sync.run_index(cfg)
    assert (s.settling, s.added, s.changed) == (0, 1, 1)


def test_settle_window_is_configurable(cfg, library, fakes):
    cfg.settle_minutes = 0
    ag.write(library / "sfx/fresh.wav", ag.tone(500, 0.3))
    os.utime(library / "sfx/fresh.wav", (time.time() - 5, time.time() - 5))
    s = sync.run_index(cfg)
    assert s.settling == 0 and s.added == N_AUDIO + 1


def test_archives_table_tracks_archives(cfg, library, fakes):
    sync.run_index(cfg)
    (library / "packs/Big.RAR").write_bytes(b"Rar!")
    (library / "sfx/more.7z").write_bytes(b"7z")
    (library / "packs/Test Bundle 2026/Extras.zip").unlink()
    s = sync.run_index(cfg)
    assert s.archives == 2 and s.added == 0
    conn = _conn(cfg)
    assert [r[0] for r in conn.execute("SELECT rel_path FROM archives ORDER BY rel_path")] == [
        "packs/Big.RAR", "sfx/more.7z"
    ]
    conn.close()


def test_decode_failure_sets_error_and_retries(cfg, library, fakes):
    bad = library / "sfx/broken.wav"
    bad.write_bytes(b"BAD!" + b"\x00" * 100)
    _old(bad)
    s = sync.run_index(cfg)
    assert s.errors == 1 and s.analyzed == N_AUDIO
    row = _asset(cfg, "sfx/broken.wav")
    assert row["status"] == "error" and "not a decodable" in row["error"]
    assert row["error_count"] == 1
    conn = _conn(cfg)
    assert _one(conn, "SELECT count(*) FROM asset_stages WHERE asset_id=?", row["id"]) == 0
    run = conn.execute("SELECT errors FROM index_runs ORDER BY id DESC").fetchone()
    assert run[0] == 1
    conn.close()

    fakes.clear()
    s = sync.run_index(cfg)  # retried, still broken
    assert s.errors == 1 and fakes["read"] == 1
    assert _asset(cfg, "sfx/broken.wav")["error_count"] == 2

    _write_old(bad, ag.tone(400, 0.5), t=OLD_MTIME + 1)  # fixed
    s = sync.run_index(cfg)
    assert s.errors == 0 and s.changed == 1
    row = _asset(cfg, "sfx/broken.wav")
    assert row["status"] == "active" and row["error"] is None and row["error_count"] == 0


def test_any_stage_exception_is_recorded_not_fatal(cfg, monkeypatch, fakes):
    real = features.compute_features

    def flaky(audio, kind):
        if kind == "music":
            raise ValueError("librosa exploded")
        return real(audio, kind)

    monkeypatch.setattr(features, "compute_features", flaky)
    s = sync.run_index(cfg)
    assert s.errors == 1 and s.analyzed == N_AUDIO - 1
    row = _asset(cfg, "music/Arteria3D/Battle Loop.mp3")
    assert row["status"] == "error" and row["error"] == "ValueError: librosa exploded"


# --------------------------------------------------------------------------- stages


def test_stage_version_bump_reruns_only_that_stage(cfg, fakes, monkeypatch):
    sync.run_index(cfg)
    monkeypatch.setattr(loudness, "LOUDNESS_VERSION", "999")
    fakes.clear()
    s = sync.run_index(cfg)
    assert s.analyzed == N_AUDIO
    assert fakes["loudness"] == N_AUDIO and fakes["decode"] == N_AUDIO
    assert fakes["features"] == 0 and fakes["caption"] == 0 and fakes["fts"] == 0
    conn = _conn(cfg)
    sql = "SELECT version FROM asset_stages WHERE stage='loudness'"
    assert {r[0] for r in conn.execute(sql)} == {"999"}
    # features columns survived the loudness-only rewrite
    assert _one(conn, "SELECT count(*) FROM features WHERE centroid_hz_mean IS NULL") == 0
    conn.close()
    assert _statuses(cfg) == Counter(active=N_AUDIO)


def test_caption_bump_needs_no_library_reads(cfg, fakes, monkeypatch):
    sync.run_index(cfg)
    monkeypatch.setattr(captions, "CAPTION_VERSION", "2")
    fakes.clear()
    sync.run_index(cfg)
    assert fakes["caption"] == N_AUDIO and fakes["read"] == 0 and fakes["decode"] == 0


def test_metadata_only_rerun_does_not_decode(cfg, fakes, monkeypatch):
    sync.run_index(cfg)
    monkeypatch.setattr(scan, "METADATA_VERSION", "2")
    fakes.clear()
    sync.run_index(cfg)
    assert fakes["read"] == N_AUDIO and fakes["decode"] == 0 and fakes["features"] == 0


def test_stages_filter_and_force(cfg, fakes):
    sync.run_index(cfg)
    fakes.clear()
    s = sync.run_index(cfg, stages=["caption"])
    assert s.analyzed == 0 and sum(fakes.values()) == 0
    s = sync.run_index(cfg, stages=["caption", "fts"], force=True)
    assert s.analyzed == N_AUDIO
    assert fakes["caption"] == N_AUDIO and fakes["fts"] == N_AUDIO and fakes["read"] == 0


def test_stage_subset_on_new_files_leaves_them_pending(cfg, fakes):
    s = sync.run_index(cfg, stages=["metadata"])
    assert s.analyzed == N_AUDIO and fakes["decode"] == 0
    assert _statuses(cfg) == Counter(pending=N_AUDIO)
    fakes.clear()
    sync.run_index(cfg)
    assert fakes["read"] == N_AUDIO and fakes["decode"] == N_AUDIO
    assert _statuses(cfg) == Counter(active=N_AUDIO)


def test_limit_caps_files_analyzed(cfg, fakes):
    s = sync.run_index(cfg, limit=3)
    assert s.added == N_AUDIO and s.analyzed == 3
    assert _statuses(cfg) == Counter(active=3, pending=N_AUDIO - 3)
    s = sync.run_index(cfg)
    assert s.analyzed == N_AUDIO - 3 and s.added == 0


# --------------------------------------------------------------------------- resume


def test_interrupt_and_resume(cfg, fakes, monkeypatch):
    real = features.compute_features
    seen = Counter()

    def interrupting(audio, kind):
        seen["n"] += 1
        if seen["n"] == 3:
            raise KeyboardInterrupt
        return real(audio, kind)

    monkeypatch.setattr(features, "compute_features", interrupting)
    s = sync.run_index(cfg)
    assert s.interrupted and s.analyzed == 2
    assert _lock_free(cfg)
    assert _statuses(cfg) == Counter(active=2, pending=N_AUDIO - 2)
    conn = _conn(cfg)
    run = conn.execute("SELECT status, finished_at, analyzed FROM index_runs").fetchone()
    assert run["status"] == "interrupted" and run["finished_at"] and run["analyzed"] == 2
    conn.close()

    monkeypatch.setattr(features, "compute_features", real)
    fakes.clear()
    s = sync.run_index(cfg)
    assert not s.interrupted and s.analyzed == N_AUDIO - 2 and s.added == 0
    assert fakes["decode"] == N_AUDIO - 2  # finished files are not redone
    assert _statuses(cfg) == Counter(active=N_AUDIO)


def test_interrupt_inside_the_commit_leaves_asset_pending(cfg, fakes, monkeypatch):
    real = search.index_asset

    def interrupting(conn, asset_id):
        real(conn, asset_id)
        raise KeyboardInterrupt  # after other writes, inside the per-file transaction

    monkeypatch.setattr(search, "index_asset", interrupting)
    s = sync.run_index(cfg)
    assert s.interrupted and s.analyzed == 0
    conn = _conn(cfg)
    assert conn.execute("SELECT count(*) FROM features").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM captions").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM asset_stages").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM search_fts").fetchone()[0] == 0
    assert _one(conn, "SELECT count(*) FROM assets WHERE content_hash IS NOT NULL") == 0
    conn.close()
    assert _statuses(cfg) == Counter(pending=N_AUDIO)

    monkeypatch.setattr(search, "index_asset", real)
    assert sync.run_index(cfg).analyzed == N_AUDIO


def test_crashed_run_is_marked_and_resumed(cfg, fakes):
    conn = _conn(cfg)
    conn.execute("INSERT INTO index_runs(started_at, status) VALUES (?, 'running')", (db.utcnow(),))
    conn.close()
    sync.run_index(cfg)
    conn = _conn(cfg)
    statuses = [r[0] for r in conn.execute("SELECT status FROM index_runs ORDER BY id")]
    conn.close()
    assert statuses == ["interrupted", "completed"]


# --------------------------------------------------------------------------- lock


def _dead_pid() -> int:
    out = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                         capture_output=True, text=True, check=True)
    return int(out.stdout)


def _lock_free(cfg) -> bool:
    try:
        info = sync._acquire_lock(cfg)
    except sync.IndexLocked:
        return False
    sync._release_lock(cfg, info)
    return True


def test_lock_is_free_after_runs(cfg, fakes):
    sync.run_index(cfg)
    assert _lock_free(cfg)
    assert cfg.lock_path.read_text() == ""  # empty file = not held


def test_live_lock_blocks(cfg, fakes):
    holder = sync._acquire_lock(cfg)
    try:
        held = json.loads(cfg.lock_path.read_text())
        assert held["pid"] == os.getpid() and held["started_at"] and held["host"]
        with pytest.raises(sync.IndexLocked, match=f"pid {os.getpid()}"):
            sync.run_index(cfg)
        assert json.loads(cfg.lock_path.read_text()) == held  # untouched
    finally:
        sync._release_lock(cfg, holder)
    assert sync.run_index(cfg).analyzed == N_AUDIO


def test_lock_held_by_another_process(cfg, fakes):
    cfg.ensure_dirs()
    script = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "from foley import sync\n"
        "from foley.config import Config\n"
        "cfg = Config(library_root=Path(sys.argv[1]), data_dir=Path(sys.argv[2]))\n"
        "sync._acquire_lock(cfg)\n"
        "print(os.getpid(), flush=True)\n"
        "sys.stdin.read()\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(cfg.library_root), str(cfg.data_dir)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    holder_pid = None
    try:
        # (a venv launcher may sit between us and the interpreter, so ask it its pid)
        holder_pid = int(proc.stdout.readline())
        with pytest.raises(sync.IndexLocked, match=rf"is running \(pid {holder_pid} "):
            sync.run_index(cfg)
        with pytest.raises(sync.IndexLocked):
            sync.prune(cfg, dry_run=False)
    finally:
        if holder_pid:
            os.kill(holder_pid, signal.SIGTERM)  # dies holding the lock: the OS frees it
        proc.kill()
        proc.wait()
    for _ in range(50):  # the OS releases the lock once the process is fully gone
        if _lock_free(cfg):
            break
        time.sleep(0.1)
    assert sync.run_index(cfg).analyzed == N_AUDIO


@pytest.mark.parametrize("content", ["dead", "own-old", "corrupt", "empty"])
def test_stale_lock_is_recovered(cfg, fakes, content):
    cfg.ensure_dirs()
    text = {
        "dead": _lock_json(_dead_pid()),
        "own-old": json.dumps({"pid": os.getpid(), "started_at": "x", "started_ts": 1.0}),
        "corrupt": "{not json",
        "empty": "",
    }[content]
    cfg.lock_path.write_text(text)
    s = sync.run_index(cfg)
    assert s.analyzed == N_AUDIO
    assert _lock_free(cfg)


def test_pid_file_lock_of_older_build_is_respected(cfg, fakes):
    """A live process named in the file but holding no OS lock (an older Foley that
    used a pid-file lock) still blocks a run."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import os, sys; print(os.getpid(), flush=True); sys.stdin.read()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    pid = None
    try:
        pid = int(proc.stdout.readline())
        cfg.ensure_dirs()
        cfg.lock_path.write_text(_lock_json(pid))
        with pytest.raises(sync.IndexLocked, match=rf"pid {pid} "):
            sync.run_index(cfg)
    finally:
        if pid:
            os.kill(pid, signal.SIGTERM)
        proc.kill()
        proc.wait()
    for _ in range(50):
        if not sync._pid_alive(pid, None):
            break
        time.sleep(0.1)
    assert sync.run_index(cfg).analyzed == N_AUDIO


def test_stale_takeover_is_exclusive(cfg, fakes):
    cfg.ensure_dirs()
    cfg.lock_path.write_text(_lock_json(_dead_pid()))
    first = sync._acquire_lock(cfg)
    try:
        with pytest.raises(sync.IndexLocked):
            sync._acquire_lock(cfg)  # a second taker of the same stale lock loses
        held = json.loads(cfg.lock_path.read_text())
        assert held["started_ts"] == first["started_ts"]
    finally:
        sync._release_lock(cfg, first)


def test_own_pid_with_old_start_time_is_not_alive():
    assert sync._pid_alive(os.getpid(), time.time())
    assert sync._pid_alive(os.getpid(), None)
    assert not sync._pid_alive(os.getpid(), 1.0)  # written before this process existed
    assert not sync._pid_alive(_dead_pid(), time.time())


def test_fallback_without_os_locking(cfg, fakes, monkeypatch):
    def unsupported(fd):
        raise sync._LockUnsupported("no locking on this filesystem")

    monkeypatch.setattr(sync, "_os_lock", unsupported)
    cfg.ensure_dirs()
    cfg.lock_path.write_text(_lock_json(os.getpid()))  # live: this process, just now
    with pytest.raises(sync.IndexLocked):
        sync.run_index(cfg)
    cfg.lock_path.write_text(_lock_json(_dead_pid()))
    assert sync.run_index(cfg).analyzed == N_AUDIO


def test_lock_held_during_run_and_released_on_failure(cfg, fakes, monkeypatch):
    seen = {}

    def boom(c):
        seen["lock"] = json.loads(c.lock_path.read_text())
        seen["free"] = _lock_free(c)
        raise RuntimeError("scan blew up")

    monkeypatch.setattr(scan, "scan_library", boom)
    with pytest.raises(RuntimeError, match="scan blew up"):
        sync.run_index(cfg)
    assert seen["lock"]["pid"] == os.getpid() and seen["lock"]["started_at"]
    assert seen["free"] is False
    assert _lock_free(cfg)
    conn = _conn(cfg)
    run = conn.execute("SELECT status, note FROM index_runs").fetchone()
    conn.close()
    assert run["status"] == "failed" and "scan blew up" in run["note"]


def test_data_dir_inside_library_is_rejected(tmp_path, library):
    from foley.config import Config, ConfigError, from_dict

    bad = Config(library_root=library, data_dir=library / "sfx" / ".foley")
    with pytest.raises(ConfigError, match="inside library.root"):
        sync.run_index(bad)
    with pytest.raises(ConfigError):
        sync.prune(bad)
    with pytest.raises(ConfigError):
        from_dict({"library": {"root": str(library)}, "storage": {"data_dir": str(library)}})
    assert not (library / "sfx" / ".foley").exists()
    ok = from_dict({"library": {"root": str(library)},
                    "storage": {"data_dir": str(tmp_path / "library-data")}})
    assert ok.data_dir == tmp_path / "library-data"


# --------------------------------------------------------------------------- share trouble


def test_read_error_keeps_analyzed_assets_searchable(cfg, fakes, monkeypatch):
    sync.run_index(cfg)
    real = decode.read_bytes

    def flaky(path):
        if Path(path).name == "click.wav":
            raise OSError(64, "The specified network name is no longer available")
        return real(path)

    monkeypatch.setattr(decode, "read_bytes", flaky)
    monkeypatch.setattr(loudness, "LOUDNESS_VERSION", "77")
    s = sync.run_index(cfg)
    assert s.errors == 1 and s.analyzed == N_AUDIO - 1 and not s.interrupted
    row = _asset(cfg, CLICK)
    assert row["status"] == "active"  # still searchable
    assert "read failed" in row["error"] and row["error_count"] == 1
    conn = _conn(cfg)
    version = _one(conn, "SELECT version FROM asset_stages WHERE asset_id=? AND stage='loudness'",
                   row["id"])
    conn.close()
    assert version == "1"  # will be retried next run

    # a never-analyzed file that cannot be read is an ordinary error
    _write_old(cfg.library_root / "sfx/new/click.wav", ag.click())
    s = sync.run_index(cfg)
    assert _asset(cfg, "sfx/new/click.wav")["status"] == "error"

    monkeypatch.setattr(decode, "read_bytes", real)
    s = sync.run_index(cfg)
    row = _asset(cfg, CLICK)
    assert s.errors == 0 and row["status"] == "active" and row["error"] is None


def test_root_vanishing_mid_run_aborts(cfg, library, fakes, monkeypatch):
    sync.run_index(cfg)
    real = decode.read_bytes
    calls = Counter()

    def dropping(path):
        calls["n"] += 1
        if calls["n"] == 3:
            monkeypatch.setattr(sync, "_root_reachable", lambda c: False)
        if calls["n"] >= 3:
            raise OSError(53, "The network path was not found")
        return real(path)

    monkeypatch.setattr(decode, "read_bytes", dropping)
    monkeypatch.setattr(scan, "METADATA_VERSION", "2")
    with pytest.raises(sync.LibraryUnavailable, match="became unreachable"):
        sync.run_index(cfg, force=True)
    assert _statuses(cfg) == Counter(active=N_AUDIO)
    conn = _conn(cfg)
    run = conn.execute("SELECT status, analyzed, note FROM index_runs ORDER BY id DESC").fetchone()
    assert _one(conn, "SELECT count(*) FROM assets WHERE error IS NOT NULL") == 0
    conn.close()
    assert run["status"] == "failed" and run["analyzed"] == 2
    assert "unreachable" in run["note"]
    assert _lock_free(cfg)


def test_consecutive_read_errors_abort(cfg, fakes, monkeypatch):
    sync.run_index(cfg)
    monkeypatch.setattr(sync, "MAX_CONSECUTIVE_READ_ERRORS", 3)

    def failing(path):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(decode, "read_bytes", failing)
    with pytest.raises(sync.LibraryUnavailable, match="3 library reads failed in a row"):
        sync.run_index(cfg, force=True)
    assert _statuses(cfg) == Counter(active=N_AUDIO)
    conn = _conn(cfg)
    assert _one(conn, "SELECT count(*) FROM assets WHERE error_count > 0") == 3
    conn.close()


def test_hash_change_without_stat_change_invalidates_all_stages(cfg, library, fakes):
    sync.run_index(cfg)
    path = library / CLICK
    data = bytearray(path.read_bytes())
    data[-1] ^= 0x01  # same size, same mtime, different bytes
    st = path.stat()
    path.write_bytes(bytes(data))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    before = _asset(cfg, CLICK)
    s = sync.run_index(cfg, stages=["metadata"], force=True)
    assert s.analyzed == N_AUDIO
    after = _asset(cfg, CLICK)
    assert after["content_hash"] != before["content_hash"] and after["audio_hash"] is None
    assert after["status"] == "pending"
    conn = _conn(cfg)
    stages = {r[0] for r in conn.execute(
        "SELECT stage FROM asset_stages WHERE asset_id=?", (after["id"],))}
    conn.close()
    assert stages == {"metadata"}
    fakes.clear()
    sync.run_index(cfg)
    assert fakes["decode"] == 1 and _asset(cfg, CLICK)["status"] == "active"


def test_mass_missing_guard(cfg, library, fakes):
    for i in range(60):
        _write_old(library / f"sfx/bulk/b{i:03d}.wav", ag.tone(200 + i, 0.01))
    sync.run_index(cfg)
    hidden = library.parent / "elsewhere"
    shutil.move(library / "sfx/bulk", hidden)  # 60 of 72 active files vanish
    with pytest.raises(sync.LibraryUnavailable, match="allow-mass-missing"):
        sync.run_index(cfg)
    assert _statuses(cfg) == Counter(active=N_AUDIO + 60)
    s = sync.run_index(cfg, allow_mass_missing=True)
    assert s.missing == 60
    # below the 50-file floor nothing trips
    for rel in list(LIBRARY_FILES)[:5]:
        (library / rel).unlink()
    assert sync.run_index(cfg).missing == 5


def test_unreachable_or_empty_root_marks_nothing_missing(cfg, library, fakes):
    sync.run_index(cfg)
    shutil.rmtree(library)
    library.mkdir()
    with pytest.raises(sync.LibraryUnavailable):
        sync.run_index(cfg)
    library.rmdir()
    with pytest.raises(sync.LibraryUnavailable):
        sync.run_index(cfg)
    assert _statuses(cfg) == Counter(active=N_AUDIO)
    assert _lock_free(cfg)
    library.mkdir()
    assert sync.run_index(cfg, allow_mass_missing=True).missing == N_AUDIO


# --------------------------------------------------------------------------- scan safety


def test_unreadable_directory_is_an_error_and_not_missing(cfg, library, fakes, monkeypatch):
    sync.run_index(cfg)
    real_scandir = os.scandir
    blocked = os.path.normcase(str(library / "sfx" / "lightsabers"))

    def scandir(path="."):
        if os.path.normcase(os.fspath(path)) == blocked:
            raise PermissionError(13, "Access is denied", os.fspath(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    lines: list[str] = []
    s = sync.run_index(cfg, progress=lines.append)
    assert s.errors == 1 and s.missing == 0
    assert _asset(cfg, LASER)["status"] == "active"
    assert any("sfx/lightsabers" in line and "unreadable" in line for line in lines)



def test_quarantine_under_root_is_not_indexed(cfg, library, fakes):
    q = library / "_quarantine"
    _write_old(q / "sfx/click.wav", ag.click())
    cfg.quarantine_root = q
    s = sync.run_index(cfg)
    assert s.scanned == N_AUDIO


def test_progress_lines_every_50_files(cfg, library, fakes):
    for i in range(60):
        _write_old(library / f"sfx/bulk/b{i:03d}.wav", ag.tone(200 + i, 0.01))
    lines: list[str] = []
    sync.run_index(cfg, progress=lines.append)
    ticks = [line for line in lines if line.strip().startswith("[")]
    assert len(ticks) == (N_AUDIO + 60) // 50
    assert "files/s" in ticks[0] and "ETA" in ticks[0]


# --------------------------------------------------------------------------- prune


def _age_missing(cfg, rel, days):
    conn = _conn(cfg)
    when = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    conn.execute("UPDATE assets SET missing_since=? WHERE rel_path=?", (when, rel))
    conn.close()


def test_prune(cfg, library, fakes):
    sync.run_index(cfg)
    used = _asset(cfg, GORE)
    # a cached image for the CLICK asset, inside data_dir
    click = _asset(cfg, CLICK)
    img = cfg.image_dir / f"{click['id']}.png"
    img.write_bytes(b"png")
    conn = _conn(cfg)
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO images(asset_id, kind, local_path, version, rendered_at) "
            "VALUES (?, 'overview', ?, '1', ?)",
            (click["id"], f"images/{click['id']}.png", db.utcnow()),
        )
        conn.execute(
            "INSERT INTO ledger.usages(content_hash, rel_path, project, project_path, imported_at) "
            "VALUES (?, ?, 'game', 'res://a.wav', ?)",
            (used["content_hash"], GORE, db.utcnow()),
        )
    conn.close()

    for rel in (CLICK, GORE, LASER):
        (library / rel).unlink()
    sync.run_index(cfg)
    _age_missing(cfg, CLICK, 40)
    _age_missing(cfg, GORE, 40)  # has a ledger usage: kept
    _age_missing(cfg, LASER, 5)  # too recent: kept
    library_files = sorted(p for p in library.rglob("*"))

    assert sync.prune(cfg) == [CLICK]
    assert _asset(cfg, CLICK) is not None and img.exists()  # dry run changed nothing

    assert sync.prune(cfg, dry_run=False) == [CLICK]
    assert _asset(cfg, CLICK) is None and not img.exists()
    assert _asset(cfg, GORE)["status"] == "missing"
    assert _asset(cfg, LASER)["status"] == "missing"
    conn = _conn(cfg)
    assert _one(conn, "SELECT count(*) FROM features WHERE asset_id=?", click["id"]) == 0
    assert _one(conn, "SELECT count(*) FROM search_fts WHERE rowid=?", click["id"]) == 0
    conn.close()
    assert sorted(p for p in library.rglob("*")) == library_files
    assert sync.prune(cfg, dry_run=False) == []


def _wav_with_trailing_ixml(path: Path, note: str, odd_junk: bool = False) -> None:
    import io
    import struct

    import soundfile as sf

    bio = io.BytesIO()
    sf.write(bio, ag.tone(330, 0.2), ag.SR, format="WAV", subtype="PCM_16")
    wav = bio.getvalue()
    extra = b""
    if odd_junk:
        extra += b"junk" + struct.pack("<I", 3) + b"abc"  # odd size, pad byte omitted
    xml = f"<BWFXML><NOTE>{note}</NOTE></BWFXML>".encode()
    extra += b"iXML" + struct.pack("<I", len(xml)) + xml + (b"\x00" if len(xml) % 2 else b"")
    body = wav[12:] + extra
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body)
    _old(path)


def test_large_files_stream_hash_and_decode_by_path(cfg, library, fakes, monkeypatch):
    big = library / "sfx/huge_ambience.wav"
    _wav_with_trailing_ixml(big, "after the audio", odd_junk=True)
    decoded_paths = []
    real_decode_path = decode.decode_path

    def decode_path(path):
        decoded_paths.append(Path(path))
        return real_decode_path(path)

    monkeypatch.setattr(decode, "decode_path", decode_path)
    monkeypatch.setattr(sync, "LARGE_FILE_BYTES", 0)  # every file takes the large path
    s = sync.run_index(cfg)
    assert s.errors == 0 and s.analyzed == N_AUDIO + 1
    assert len(decoded_paths) == N_AUDIO + 1
    row = _asset(cfg, "sfx/huge_ambience.wav")
    assert row["content_hash"] == xxhash.xxh3_128_hexdigest(big.read_bytes())
    assert json.loads(row["source_metadata"])["ixml"] == {"note": "after the audio"}


def test_metadata_bytes_matches_full_parse(tmp_path):
    path = tmp_path / "a.wav"
    _wav_with_trailing_ixml(path, "x" * 5, odd_junk=True)
    head = sync._metadata_bytes(path)
    assert len(head) < path.stat().st_size
    full = scan.read_metadata(path.read_bytes(), ".wav", "a.wav")
    assert scan.read_metadata(head, ".wav", "a.wav") == full
    assert full["ixml"] == {"note": "xxxxx"}


def test_prune_respects_the_index_lock(cfg, fakes):
    holder = sync._acquire_lock(cfg)
    try:
        assert sync.prune(cfg) == []  # dry run needs no lock
        with pytest.raises(sync.IndexLocked):
            sync.prune(cfg, dry_run=False)
    finally:
        sync._release_lock(cfg, holder)
    assert sync.prune(cfg, dry_run=False) == []


# --------------------------------------------------------------------------- end to end


def siblings_ready(tmp: Path) -> str | None:
    """None if the real decode/features/loudness/captions/search modules work."""
    import soundfile as sf

    try:
        d = tmp / "probe"
        d.mkdir()
        if True:
            p = Path(d) / "t.wav"
            sf.write(p, ag.tone(440, 0.5), ag.SR)
            data = decode.read_bytes(p)
            decode.content_hash(data)
            audio = decode.decode_bytes(data, ".wav")
            decode.audio_hash(audio)
            feats = features.compute_features(audio, "sfx")
            feats.update(loudness.measure(audio))
            captions.templated_caption({"rel_path": "t.wav"}, feats, [], {})
            from foley.config import Config

            cfg = Config(library_root=Path(d), data_dir=Path(d) / "data")
            conn = db.open_catalog(cfg)
            try:
                conn.execute(
                    "INSERT INTO assets(rel_path, ext, size_bytes, mtime_ns, first_seen, "
                    "last_seen) "
                    "VALUES ('t.wav', '.wav', 1, 1, 'x', 'x')"
                )
                search.index_asset(conn, 1)
            finally:
                conn.close()
    except NotImplementedError as e:
        return f"sibling module not implemented yet: {e!r}"
    return None


def test_end_to_end_real_pipeline(cfg, library, tmp_path):
    reason = siblings_ready(tmp_path)
    if reason:
        pytest.skip(reason)
    s = sync.run_index(cfg)
    assert (s.added, s.errors, s.interrupted) == (N_AUDIO, 0, False), s
    assert s.analyzed == N_AUDIO
    conn = _conn(cfg)
    assert _one(conn, "SELECT count(*) FROM assets WHERE status='active'") == N_AUDIO
    row = conn.execute(
        "SELECT f.duration_s, f.sample_rate, f.band_energy, c.text FROM assets a "
        "JOIN features f ON f.asset_id=a.id JOIN captions c ON c.asset_id=a.id "
        "WHERE a.rel_path=?", (GORE,)
    ).fetchone()
    assert abs(row["duration_s"] - 1.2) < 0.05 and row["sample_rate"] == ag.SR
    assert abs(sum(json.loads(row["band_energy"]).values()) - 1) < 1e-3
    assert row["text"]
    dup = conn.execute(
        "SELECT count(DISTINCT content_hash), count(DISTINCT audio_hash) FROM assets "
        "WHERE rel_path LIKE '%NailScratch19%'"
    ).fetchone()
    assert tuple(dup) == (1, 1)
    hits = conn.execute(
        "SELECT rowid FROM search_fts WHERE search_fts MATCH 'cladding'"
    ).fetchall()
    assert len(hits) == 2
    conn.close()
    s = sync.run_index(cfg)
    assert s.analyzed == 0 and s.errors == 0
