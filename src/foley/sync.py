"""`foley index`: incremental, resumable, idempotent library sync and analysis.

CONTRACT:

    CORE_STAGES = ("metadata", "features", "loudness", "caption", "fts")   # Phase 1a
    MODEL_STAGES = ("tags", "embeddings")                                 # Phase 1b
    STAGES = CORE_STAGES + MODEL_STAGES
        Each has a version (scan.METADATA_VERSION, features.FEATURES_VERSION,
        loudness.LOUDNESS_VERSION, captions.CAPTION_VERSION, search.FTS_VERSION;
        "tags": TAGS_VERSION + panns.MODEL_KEY; "embeddings": EMBED_VERSION + the sorted
        model keys of the configured [models] clap checkpoints).
        A stage runs for an asset when asset_stages lacks it or its version differs;
        "embeddings" is current when its recorded EMBED_VERSION matches and its recorded
        keys include every configured checkpoint, and only the missing checkpoints are
        computed (so dropping a checkpoint from [models] clap re-runs nothing).
        All stages needing audio share one read_bytes + decode per file.
        (Files over LARGE_FILE_BYTES: streamed hash + decode.decode_path instead.)

        `active` is gated by CORE_STAGES only: an asset is active once they are
        current. The model stages are enrichment that fills in afterwards (resumable,
        per file); their progress is in status.catalog_status()["models"]. A plain
        `foley index` runs STAGES when [models] enabled (default), else CORE_STAGES;
        `--stage tags` / `--stage embeddings` always work. If a model cannot be loaded
        (no weights, download failure, broken torch), its stage is skipped for the run
        with a note, before any file is read for it; the core stages proceed.
        "tags" writes the tags rows (every AudioSet label >= panns.TAG_MIN_SCORE,
        replacing the asset's previous tags) and features.is_music, and rewrites the
        asset's caption and search_fts row in the same transaction (their stage rows are
        left as they were). The caption stage reads the asset's stored tags.
        "embeddings" writes one embeddings row per computed checkpoint (other
        checkpoints' rows are kept). A content change drops the asset's tags, embeddings
        and is_music along with its stage records.
        Model inference runs in this (the indexer) process on [models] device (auto =
        CUDA when available); workers only decode and cut the windows
        (foley.models.prep). In the parallel path, files are inferred in batches of up
        to MODEL_BATCH_FILES (each file still commits in its own transaction afterwards;
        a Ctrl+C loses at most the uncommitted batch, which stays pending). A model
        failure on one file is recorded as that file's error (counted in `errors`) but
        its core results still commit and its status is unaffected.

    @dataclass RunSummary: run_id, scanned, added, changed, moved, missing, analyzed,
                           errors, settling, archives, interrupted: bool,
                           notes: list[str] (e.g. a model that could not be loaded; also
                           stored in index_runs.note)

    run_index(cfg, stages: list[str] | None = None, force: bool = False,
              limit: int | None = None, progress: Callable[[str], None] | None = None,
              allow_mass_missing: bool = False, workers: int | None = None) -> RunSummary
        Implements "Library sync" in docs/handoffs/AudioCatalog.md exactly:
        lock file (IndexLocked if held by a live process; stale locks recovered),
        index_runs row, scan, diff by (rel_path, size, mtime_ns), settle window,
        move detection by content hash against `missing` assets, missing marking,
        archives table, per-file commit, retry of `error` assets, Ctrl+C marks the
        run interrupted and the next run resumes. `stages` limits which stages run
        (re-run those even if current when force=True). `limit` caps files analyzed
        (for trial runs).
        Raises LibraryUnavailable (run recorded as 'failed', nothing marked missing)
        when the root cannot be listed, when the scan would mark more than
        MASS_MISSING_FRACTION (10%) of active assets and at least MASS_MISSING_MIN (50)
        files missing (or every file) unless allow_mass_missing=True, and when the root
        becomes unreachable or MAX_CONSECUTIVE_READ_ERRORS reads fail in a row during
        analysis. A failed read of a file that was analyzed before records the error
        text/count but keeps the asset's status (it stays searchable).
        `workers`: analysis processes. None = auto (default_workers():
        min(cpu_count - 2, 8), at least 1; small runs stay in-process); 1 = in-process
        (the reference path); N > 1 = N spawned workers that read, hash, parse metadata,
        decode and measure, while this process alone writes SQLite (move resolution,
        captions, FTS, stages, status) in the same per-file transaction. Results are
        identical to workers=1; files over LARGE_FILE_BYTES and files needing no read
        run in this process. A crashed worker costs only the file it was on (recorded
        as an error after an isolated retry); Ctrl+C stops the workers and leaves
        unfinished files pending.

    default_workers() -> int

    prune(cfg, dry_run: bool = True) -> list[str]
        rel_paths of `missing` assets older than prune_missing_days with no ledger
        rows (by content_hash); deletes their catalog rows and cached images unless
        dry_run. Never touches the library.

    class IndexLocked(Exception)
    class LibraryUnavailable(RuntimeError)

Implementation notes (not contract):
  * The diff is done in memory against one query of `assets` and one of
    `asset_stages`; only files that need work cost a NAS read.
  * New paths get a provisional `pending` row during the diff. Their first hashing
    (part of analysis, so each file is read once) checks for a `missing` asset with
    the same content_hash; on a match the provisional row is deleted and the missing
    asset is re-pointed to the new path in the same transaction that stores the
    analysis, keeping its id, stages and analysis. Path-derived stages (metadata,
    caption, fts) re-run for it; features and loudness do not.
  * Each file's results, stage records and status change commit in one transaction,
    so a crash or Ctrl+C mid-file leaves that asset exactly as it was (pending).
  * An asset becomes `active` once every stage in CORE_STAGES is current.
  * Paths under a directory that could not be listed are never marked missing.
  * prune(dry_run=False) takes the index lock and raises IndexLocked if an index runs.
  * The lock is an OS-level lock on data_dir/index.lock held for the whole run (see
    the lock section); it frees itself if the process dies.
  * If a file's bytes changed without a size/mtime change, all its stage records are
    dropped so every stage re-runs (now or on the next full run).
  * Scan-time move matching: before anything is marked missing, a vanished (or
    earlier-missing) asset and a new path with the same casefolded basename, size and
    mtime form a move when the key is unique on both sides. The asset is re-pointed in
    the scan transaction with no file read; only caption and fts re-run (plus metadata
    if the basename's case changed). Ambiguous keys use hash-based detection. The
    mass-missing guard counts only what is still missing after matching.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import struct
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import xxhash

from foley import captions, decode, features, loudness, scan, search
from foley.catalog import db
from foley.config import Config, check_data_dir, clap_checkpoints
from foley.models import clap as clap_models
from foley.models import panns, prep

CORE_STAGES = ("metadata", "features", "loudness", "caption", "fts")
MODEL_STAGES = ("tags", "embeddings")
STAGES = CORE_STAGES + MODEL_STAGES
TAGS_VERSION = "2"  # windowing / padding / aggregation (2: pad only below 0.31 s)
EMBED_VERSION = "2"  # windowing / pooling (2: fused checkpoints flag every window)
MODEL_BATCH_FILES = 16

# Stages whose output depends on the path (re-run when an asset is moved).
_PATH_STAGES = ("metadata", "caption", "fts")
_AUDIO_STAGES = ("features", "loudness")
_PROGRESS_EVERY = 50

# Files above this size are hashed by streaming and decoded with decode.decode_path, so
# their raw bytes and decoded audio are never in memory together (a second read is fine
# for these rare files). Metadata then comes from the header/chunks only.
LARGE_FILE_BYTES = 512 * 1024 * 1024
_HASH_BLOCK = 8 * 1024 * 1024
_MAX_META_CHUNK = 16 * 1024 * 1024
_NON_RIFF_HEAD = 4 * 1024 * 1024

# Safety valves against a share that drops or half-mounts mid-run.
MASS_MISSING_FRACTION = 0.10
MASS_MISSING_MIN = 50
MAX_CONSECUTIVE_READ_ERRORS = 20

FEATURE_COLUMNS = (
    "duration_s", "sample_rate", "channels", "lufs_integrated", "lufs_short_max",
    "true_peak_db", "lead_silence_s", "tail_silence_s", "attack_s", "decay_s",
    "onset_count", "centroid_hz_mean", "rolloff_hz_mean", "flatness_mean", "zcr_mean",
    "band_energy", "tempo_bpm", "loop_seam_score", "is_music",
)


class IndexLocked(Exception):
    pass


class LibraryUnavailable(RuntimeError):
    """The library (or too much of it) is not visible, so the run stops before it
    marks files missing or records read failures that are not the files' fault."""


class LibraryReadError(Exception):
    """Reading a library file failed with an OSError (share/IO trouble, not decoding)."""


def _read(fn, *args):
    try:
        return fn(*args)
    except OSError as e:
        raise LibraryReadError(f"read failed: {type(e).__name__}: {e}") from e


@dataclass
class RunSummary:
    run_id: int
    scanned: int = 0
    added: int = 0
    changed: int = 0
    moved: int = 0
    missing: int = 0
    analyzed: int = 0
    errors: int = 0
    settling: int = 0
    archives: int = 0
    interrupted: bool = False
    notes: list[str] = field(default_factory=list)


def _clap_keys(cfg: Config | None) -> list[str]:
    names = clap_checkpoints(cfg) if cfg is not None else list(clap_models.CLAP_CHECKPOINTS)
    return [clap_models.model_key(n) for n in names]


def embed_version(keys) -> str:
    return f"{EMBED_VERSION}|{','.join(sorted(set(keys)))}"


def _embed_keys(version: str | None) -> set[str] | None:
    """The model keys recorded in an embeddings stage version (None: other EMBED_VERSION)."""
    if not version or "|" not in version:
        return None
    head, _, keys = version.partition("|")
    if head != EMBED_VERSION:
        return None
    return {k for k in keys.split(",") if k}


def stage_versions(cfg: Config | None = None) -> dict[str, str]:
    """Current version of every stage (read at call time so upgrades are picked up).
    The embeddings version lists cfg's [models] clap checkpoints (all known ones when
    cfg is None)."""
    return {
        "metadata": scan.METADATA_VERSION,
        "features": features.FEATURES_VERSION,
        "loudness": loudness.LOUDNESS_VERSION,
        "caption": captions.CAPTION_VERSION,
        "fts": search.FTS_VERSION,
        "tags": f"{TAGS_VERSION}|{panns.MODEL_KEY}",
        "embeddings": embed_version(_clap_keys(cfg)),
    }


def stage_current(stage: str, stored: str | None, versions: dict[str, str]) -> bool:
    """Whether a recorded stage version satisfies the current one (see STAGES)."""
    if stage == "embeddings":
        want = _embed_keys(versions[stage]) or set()
        if not want:
            return True  # no checkpoint configured: nothing to compute
        have = _embed_keys(stored)
        return have is not None and want <= have
    return stored == versions[stage]


def clap_keys_needed(stored: str | None, versions: dict[str, str], force: bool) -> list[str]:
    """Configured CLAP model keys an asset still needs (all of them when forced)."""
    want = sorted(_embed_keys(versions["embeddings"]) or ())
    have = None if force else _embed_keys(stored)
    return [k for k in want if have is None or k not in have]


# --------------------------------------------------------------------------- lock
#
# The lock is an OS-level exclusive lock (msvcrt.locking on Windows, fcntl.flock
# elsewhere) held on data_dir/index.lock for the whole run. The OS drops it when the
# holder exits or crashes, so a stale lock needs no detection and two runs can never
# both take it over. The file's JSON (pid, start time, host) is informational, for the
# "already running" message. The file is never deleted (deleting it would let a new
# run lock a fresh inode while an old one still holds the unlinked one); an empty
# file means nobody holds it. If the platform/filesystem cannot lock files, a
# pid-liveness check is the fallback.

_PROCESS_SEEN_AT = time.time()  # no lock written by this process predates this
_LOCK_OFFSET = 1 << 30  # Windows byte-range locks are mandatory: lock far past the JSON


def _pid_alive(pid: int, since: float | None = None) -> bool:
    """True if `pid` is a running process that started no later than `since` (when known),
    i.e. plausibly the process that wrote a lock at time `since`."""
    if pid <= 0:
        return False
    if pid == os.getpid():
        # Our own pid: the lock is ours only if written after this process started.
        return since is None or since >= _PROCESS_SEEN_AT - 1
    if sys.platform == "win32":
        return _pid_alive_windows(pid, since)
    try:
        os.kill(pid, 0)  # signal 0: existence check only (POSIX)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _pid_alive_windows(pid: int, since: float | None) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (
        ctypes.POINTER(wintypes.FILETIME),
    ) * 4
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5  # access denied: it exists
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
            return False  # 259 = STILL_ACTIVE
        if since is not None:
            created, t1, t2, t3 = (wintypes.FILETIME() for _ in range(4))
            if kernel32.GetProcessTimes(
                handle, ctypes.byref(created), ctypes.byref(t1), ctypes.byref(t2),
                ctypes.byref(t3)
            ):
                ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
                created_unix = ticks / 1e7 - 11644473600
                if created_unix > since + 5:
                    return False  # the pid was reused by a newer process
        return True
    finally:
        kernel32.CloseHandle(handle)


def _read_lock(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class _LockUnsupported(Exception):
    pass


def _os_lock(fd: int) -> bool:
    """Try to take the OS lock without blocking. False if another holder has it."""
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    except OSError as e:
        if e.errno in (errno.EACCES, errno.EAGAIN, getattr(errno, "EDEADLOCK", -1),
                       errno.EDEADLK):
            return False
        raise _LockUnsupported(str(e)) from e


def _os_unlock(fd: int) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass  # closing the descriptor releases it anyway


def _locked_message(path: Path, held: dict | None) -> str:
    if not held:
        return f"another `foley index` holds {path}; wait for it to finish"
    return (
        f"another `foley index` is running (pid {held.get('pid')} on "
        f"{held.get('host', 'this machine')}, started {held.get('started_at')}). "
        "Wait for it to finish; the lock frees itself when that process exits."
    )


def _write_info(fd: int, info: dict) -> None:
    payload = json.dumps(info).encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, payload)
    os.ftruncate(fd, len(payload))
    os.fsync(fd)


def _acquire_lock(cfg: Config) -> dict:
    cfg.ensure_dirs()
    path = cfg.lock_path
    info = {
        "pid": os.getpid(),
        "started_at": db.utcnow(),
        "started_ts": time.time(),
        "host": socket.gethostname(),
    }
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
    try:
        try:
            got = _os_lock(fd)
        except _LockUnsupported:
            # No OS locking here: fall back to the pid recorded in the file.
            held = _read_lock(path)
            if held and held.get("host") in (None, info["host"]) and _pid_alive(
                int(held.get("pid") or 0), held.get("started_ts")
            ):
                raise IndexLocked(_locked_message(path, held)) from None
            got = True
            info["os_lock"] = False
        if not got:
            raise IndexLocked(_locked_message(path, _read_lock(path)))
        held = _read_lock(path)
        if (
            held and held.get("pid") != info["pid"]
            and held.get("host") in (None, info["host"])
            and _pid_alive(int(held.get("pid") or 0), held.get("started_ts"))
        ):
            # A live process wrote this lock without an OS lock: an older Foley build
            # (pid-file lock only) is still running.
            raise IndexLocked(_locked_message(path, held))
        _write_info(fd, {k: v for k, v in info.items() if k != "os_lock"})
    except BaseException:
        os.close(fd)
        raise
    info["_fd"] = fd
    return info


def _release_lock(cfg: Config, info: dict) -> None:
    fd = info.pop("_fd", None)
    if fd is None:
        return
    try:
        os.ftruncate(fd, 0)  # empty file = not held
    except OSError:
        pass
    if info.get("os_lock", True):
        _os_unlock(fd)
    os.close(fd)


# --------------------------------------------------------------------------- run


@dataclass
class _Work:
    asset_id: int
    rel_path: str
    needed: set[str]
    candidate: bool  # never analyzed: may turn out to be a moved file
    new_this_run: bool = False


@dataclass
class _Progress:
    callback: Callable[[str], None] | None
    total: int = 0
    done: int = 0
    started: float = field(default_factory=time.monotonic)

    def say(self, line: str) -> None:
        if self.callback:
            self.callback(line)

    def tick(self, summary: RunSummary, rel_path: str) -> None:
        self.done += 1
        if not self.callback or self.done % _PROGRESS_EVERY:
            return
        elapsed = max(time.monotonic() - self.started, 1e-6)
        rate = self.done / elapsed
        remaining = self.total - self.done
        eta = _fmt_duration(remaining / rate) if rate > 0 else "?"
        self.say(
            f"  [{self.done}/{self.total}] {rate:.1f} files/s, ETA {eta}, "
            f"errors {summary.errors}  ({rel_path})"
        )


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _under(rel_path: str, dirs: list[str]) -> bool:
    return any(rel_path == d or rel_path.startswith(d + "/") for d in dirs)


def run_index(
    cfg: Config,
    stages: list[str] | None = None,
    force: bool = False,
    limit: int | None = None,
    progress: Callable[[str], None] | None = None,
    allow_mass_missing: bool = False,
    workers: int | None = None,
) -> RunSummary:
    if stages:
        requested = list(dict.fromkeys(stages))
    else:
        requested = list(STAGES if getattr(cfg, "models_enabled", True) else CORE_STAGES)
    unknown = [s for s in requested if s not in STAGES]
    if unknown:
        raise ValueError(f"unknown stage(s) {unknown}; valid stages: {', '.join(STAGES)}")

    check_data_dir(cfg)
    lock = _acquire_lock(cfg)
    try:
        conn = db.open_catalog(cfg)
        try:
            return _run(
                cfg, conn, requested, force, limit, _Progress(progress), allow_mass_missing,
                workers,
            )
        finally:
            conn.close()
    finally:
        _release_lock(cfg, lock)


def _run(cfg, conn, requested, force, limit, prog: _Progress, allow_mass_missing=False,
         workers: int | None = 1):
    with db.transaction(conn):
        # We hold the lock, so any run still marked 'running' died without finishing.
        conn.execute(
            "UPDATE index_runs SET status = 'interrupted', "
            "note = coalesce(note, 'process ended without finishing') WHERE status = 'running'"
        )
        cur = conn.execute(
            "INSERT INTO index_runs(started_at, status, stages) VALUES (?, 'running', ?)",
            (db.utcnow(), json.dumps(requested)),
        )
    summary = RunSummary(run_id=cur.lastrowid)
    status, note = "failed", None
    try:
        work, scan_errors, gone_ids = _sync(
            cfg, conn, summary, requested, force, prog, allow_mass_missing
        )
        if scan_errors:
            note = json.dumps({"scan_errors": scan_errors[:50]})
        if limit is not None:
            work = work[: max(limit, 0)]
        _analyze_all(cfg, conn, work, requested, force, summary, prog, gone_ids, workers)
        status = "completed"
    except KeyboardInterrupt:
        summary.interrupted = True
        status = "interrupted"
        prog.say("interrupted; run `foley index` again to resume")
    except BaseException as e:
        note = f"{type(e).__name__}: {e}"[:1000]
        raise
    finally:
        if summary.notes:
            extra = "; ".join(summary.notes)
            note = f"{note}; {extra}" if note else extra
        _save_run(conn, summary, status, note)
    return summary


def _save_run(conn, summary: RunSummary, status: str | None, note: str | None) -> None:
    conn.execute(
        "UPDATE index_runs SET scanned=?, added=?, changed=?, moved=?, missing=?, "
        "analyzed=?, errors=?, settling=?, archives=?, status=coalesce(?, status), "
        "finished_at=CASE WHEN ? IS NULL THEN finished_at ELSE ? END, "
        "note=coalesce(?, note) WHERE id=?",
        (summary.scanned, summary.added, summary.changed, summary.moved, summary.missing,
         summary.analyzed, summary.errors, summary.settling, summary.archives,
         status, status, db.utcnow(), note, summary.run_id),
    )


def _sync(cfg, conn, summary: RunSummary, requested, force, prog: _Progress,
          allow_mass_missing: bool = False):
    """Scan, diff and record path-level changes. Returns (work list, scan errors)."""
    t0 = time.monotonic()
    prog.say(f"scanning {cfg.library_root} ...")
    result = scan.scan_library(cfg)
    summary.scanned = len(result.audio)
    summary.archives = len(result.archives)
    error_dirs = [p for p, _ in result.errors]
    for rel, reason in result.errors[:20]:
        prog.say(f"  error: {rel}: {reason}")
    if "." in error_dirs:
        raise LibraryUnavailable(f"cannot list the library root {cfg.library_root}")
    summary.errors += len(result.errors)

    rows = conn.execute(
        "SELECT id, rel_path, size_bytes, mtime_ns, content_hash, status, indexed_at "
        "FROM assets"
    ).fetchall()
    by_path = {r["rel_path"]: r for r in rows}
    stage_map: dict[int, dict[str, str]] = {}
    for r in conn.execute("SELECT asset_id, stage, version FROM asset_stages"):
        stage_map.setdefault(r[0], {})[r[1]] = r[2]
    if not result.audio and not allow_mass_missing and any(
        r["status"] != "missing" for r in rows
    ):
        raise LibraryUnavailable(
            f"{cfg.library_root} lists no audio files but the catalog has "
            f"{sum(r['status'] != 'missing' for r in rows)}; refusing to mark everything "
            "missing (is the share mounted?). If the files really are gone, run "
            "`foley index --allow-mass-missing`."
        )

    versions = stage_versions(cfg)
    now = db.utcnow()
    now_ns = time.time_ns()
    settle_ns = int(cfg.settle_minutes * 60 * 1e9)
    day_ns = 86_400 * 10**9

    def settling(f: scan.ScannedFile) -> bool:
        age = now_ns - f.mtime_ns
        return (0 <= age < settle_ns) or (-day_ns < age < 0)

    present = {f.rel_path for f in result.audio}
    new_files, changed, reappeared, seen_ids = [], [], [], []
    for f in result.audio:
        row = by_path.get(f.rel_path)
        if settling(f):
            summary.settling += 1
            if row is not None:
                seen_ids.append(row["id"])
            continue
        if row is None:
            new_files.append(f)
            continue
        seen_ids.append(row["id"])
        if row["size_bytes"] != f.size_bytes or row["mtime_ns"] != f.mtime_ns:
            changed.append((row, f))
        elif row["status"] == "missing":
            reappeared.append(row)
    gone = [
        r for p, r in by_path.items()
        if p not in present and r["status"] != "missing" and not _under(p, error_dirs)
    ]
    # Scan-time move matching: a rename/move on the same share keeps basename, size and
    # mtime, so a unique 1:1 match on that key re-points the asset with no file read
    # (the same trust as an unchanged size+mtime at an unchanged path). Assets missing
    # since earlier runs are eligible too. Ambiguous keys go to hash-based detection.
    earlier_missing = [
        r for p, r in by_path.items()
        if p not in present and r["status"] == "missing" and not _under(p, error_dirs)
    ]
    renamed = _match_renames(gone + earlier_missing, new_files)
    if renamed:
        renamed_ids = {r["id"] for r, _ in renamed}
        renamed_paths = {f.rel_path for _, f in renamed}
        gone = [r for r in gone if r["id"] not in renamed_ids]
        new_files = [f for f in new_files if f.rel_path not in renamed_paths]
    active = sum(r["status"] == "active" for r in rows)
    if (not allow_mass_missing and len(gone) >= MASS_MISSING_MIN
            and len(gone) > MASS_MISSING_FRACTION * active):
        raise LibraryUnavailable(
            f"this scan would mark {len(gone)} of {active} active files missing "
            f"(more than {MASS_MISSING_FRACTION:.0%}); refusing in case the share is only "
            "partly visible. Nothing was changed. If the files really were removed or "
            "moved, run `foley index --allow-mass-missing`."
        )

    def all_current(asset_id: int) -> bool:
        done = stage_map.get(asset_id, {})
        return all(stage_current(s, done.get(s), versions) for s in CORE_STAGES)

    work: list[_Work] = []
    with db.transaction(conn):
        for row, f in renamed:
            pack, category = scan.classify_path(cfg, f.rel_path)
            # path-derived stages re-run (cheap, no reads); metadata too if only the
            # basename's case changed (the UCS parse is case-sensitive)
            stale = ["caption", "fts"]
            if row["rel_path"].rsplit("/", 1)[-1] != f.rel_path.rsplit("/", 1)[-1]:
                stale.append("metadata")
            conn.executemany(
                "DELETE FROM asset_stages WHERE asset_id=? AND stage=?",
                [(row["id"], st) for st in stale],
            )
            for st in stale:
                stage_map.get(row["id"], {}).pop(st, None)
            conn.execute(
                "UPDATE assets SET rel_path=?, pack=?, category_path=?, ext=?, license=?, "
                "last_seen=?, missing_since=NULL, status=? WHERE id=?",
                (f.rel_path, pack, category, f.ext, scan.find_license(cfg, f.rel_path), now,
                 "active" if all_current(row["id"]) else "pending", row["id"]),
            )
            needed = {
                st for st in requested
                if force or not stage_current(st, stage_map.get(row["id"], {}).get(st), versions)
            }
            if needed:
                work.append(_Work(row["id"], f.rel_path, needed, False))
        for f in new_files:
            pack, category = scan.classify_path(cfg, f.rel_path)
            cur = conn.execute(
                "INSERT INTO assets(rel_path, pack, category_path, ext, size_bytes, mtime_ns, "
                "first_seen, last_seen, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                (f.rel_path, pack, category, f.ext, f.size_bytes, f.mtime_ns, now, now),
            )
            work.append(_Work(cur.lastrowid, f.rel_path, set(requested), True, True))
        for row, f in changed:
            conn.execute(
                "UPDATE assets SET size_bytes=?, mtime_ns=?, ext=?, content_hash=NULL, "
                "audio_hash=NULL, status='pending', missing_since=NULL, error=NULL, "
                "error_count=0 WHERE id=?",
                (f.size_bytes, f.mtime_ns, f.ext, row["id"]),
            )
            conn.execute("DELETE FROM asset_stages WHERE asset_id=?", (row["id"],))
            _drop_model_outputs(conn, row["id"])
            stage_map.pop(row["id"], None)
        for row in reappeared:
            conn.execute(
                "UPDATE assets SET status=?, missing_since=NULL WHERE id=?",
                ("active" if all_current(row["id"]) else "pending", row["id"]),
            )
        conn.executemany(
            "UPDATE assets SET status='missing', missing_since=? WHERE id=?",
            [(now, r["id"]) for r in gone],
        )
        conn.executemany("UPDATE assets SET last_seen=? WHERE id=?", [(now, i) for i in seen_ids])
        # archives: mirror what the scan saw (keep rows under unreadable folders)
        archive_paths = {a.rel_path for a in result.archives}
        for (old,) in conn.execute("SELECT rel_path FROM archives").fetchall():
            if old not in archive_paths and not _under(old, error_dirs):
                conn.execute("DELETE FROM archives WHERE rel_path=?", (old,))
        conn.executemany(
            "INSERT INTO archives(rel_path, size_bytes, last_seen) VALUES (?, ?, ?) "
            "ON CONFLICT(rel_path) DO UPDATE SET size_bytes=excluded.size_bytes, "
            "last_seen=excluded.last_seen",
            [(a.rel_path, a.size_bytes, now) for a in result.archives],
        )
    summary.added = len(new_files)
    summary.changed = len(changed)
    summary.missing = len(gone)
    summary.moved = len(renamed)

    changed_ids = {row["id"] for row, _ in changed}
    reappeared_ids = {row["id"] for row in reappeared}
    gone_ids = {r["id"] for r in gone}
    for f in result.audio:
        row = by_path.get(f.rel_path)
        if row is None or settling(f) or row["id"] in gone_ids:
            continue
        aid = row["id"]
        if aid in changed_ids:
            work.append(_Work(aid, f.rel_path, set(requested), False))
            continue
        done = stage_map.get(aid, {})
        needed = {s for s in requested if force or not stage_current(s, done.get(s), versions)}
        status = "pending" if aid in reappeared_ids else row["status"]
        if needed:
            candidate = row["content_hash"] is None and row["indexed_at"] is None and not done
            work.append(_Work(aid, f.rel_path, needed, candidate))
        elif status in ("pending", "error") and all_current(aid):
            conn.execute(
                "UPDATE assets SET status='active', error=NULL, "
                "indexed_at=coalesce(indexed_at, ?) WHERE id=?", (now, aid)
            )
    work.sort(key=lambda w: w.rel_path)
    prog.say(
        f"scanned {summary.scanned} audio files in {time.monotonic() - t0:.1f}s: "
        f"{summary.added} new, {summary.changed} changed, "
        + (f"{len(renamed)} moved (renamed, no reads), " if renamed else "")
        + f"{summary.missing} missing, {summary.settling} settling, "
        f"{summary.archives} archives; {len(work)} to analyze"
    )
    return work, result.errors, gone_ids


def _match_renames(old_rows, new_files) -> list:
    """Unique 1:1 pairs (old asset row, new ScannedFile) with the same casefolded
    basename, size and mtime."""
    def key(rel: str, size: int, mtime: int) -> tuple:
        return rel.rsplit("/", 1)[-1].casefold(), size, mtime

    olds: dict[tuple, list] = {}
    for r in old_rows:
        olds.setdefault(key(r["rel_path"], r["size_bytes"], r["mtime_ns"]), []).append(r)
    news: dict[tuple, list] = {}
    for f in new_files:
        news.setdefault(key(f.rel_path, f.size_bytes, f.mtime_ns), []).append(f)
    return [
        (olds[k][0], fs[0]) for k, fs in news.items()
        if len(fs) == 1 and len(olds.get(k, ())) == 1
    ]


def default_workers() -> int:
    """Auto worker count: leave two CPUs for the OS and the main (SQLite) process."""
    return max(1, min((os.cpu_count() or 1) - 2, 8))


class _Models:
    """The loaded tagger / CLAP embedders for one run, in this process.

    prefetch() runs one batched inference over several files' windows; outputs() hands
    a file its (prefetched or freshly computed) results at commit time."""

    def __init__(self, cfg, versions, force, prog: _Progress):
        self.cfg, self.versions, self.force, self.prog = cfg, versions, force, prog
        self.device: str | None = None
        self.tagger = None
        self.clap: dict = {}  # model key -> embedder
        self.notes: list[str] = []
        self._errors: list[str] = []
        self._pre: dict[int, dict] = {}
        self.tagged = 0
        self.embedded = 0
        self.infer_s = 0.0
        self.extractor_configs: dict[str, dict] = {}  # config id -> config, for workers

    def _note(self, text: str) -> None:
        self.notes.append(text)
        self.prog.say(f"warning: {text}")

    def load(self, wanted: set[str]) -> set[str]:
        """Load the models for `wanted` stages; returns the stages that cannot run."""
        from foley import models as m

        try:
            self.device = m.resolve_device(self.cfg.models_device)
        except Exception as e:  # noqa: BLE001
            self._note(f"model stages skipped this run: {e}")
            return set(wanted)
        f = m.factory()
        disabled: set[str] = set()
        if "tags" in wanted:
            t0 = time.monotonic()
            try:
                self.tagger = f.tagger(self.cfg, self.device)
                self.prog.say(f"loaded {self.tagger.key} on {self.device} "
                              f"in {time.monotonic() - t0:.1f}s")
            except Exception as e:  # noqa: BLE001 - any load failure skips the stage
                disabled.add("tags")
                self._note(f"tags skipped this run: {type(e).__name__}: {e}"[:500])
        if "embeddings" in wanted:
            for name in clap_checkpoints(self.cfg):
                t0 = time.monotonic()
                try:
                    emb = f.clap_audio(name, self.device)
                    self.clap[emb.key] = emb
                    self.prog.say(f"loaded {emb.key} on {self.device} "
                                  f"in {time.monotonic() - t0:.1f}s")
                except Exception as e:  # noqa: BLE001
                    self._note(f"CLAP {name} skipped this run: {type(e).__name__}: {e}"[:500])
            if not self.clap:
                disabled.add("embeddings")
        for emb in self.clap.values():
            cid = getattr(emb, "config_id", None)
            if cid is not None and cid not in _CLAP_EXTRACTORS:
                _CLAP_EXTRACTORS[cid] = emb.extractor  # this process's own extractions
                self.extractor_configs[cid] = clap_models.extractor_config(emb.extractor)
        self._reset_peak()
        return disabled

    def close(self) -> None:
        _CLAP_EXTRACTORS.clear()

    def _embed(self, emb, results: list[dict]):
        """Embed several files' CLAP inputs (worker features when present, else windows)."""
        cid = getattr(emb, "config_id", None)
        if all("clap_feats" in r and cid in r["clap_feats"] for r in results):
            return self._timed(emb.embed_features, [r["clap_feats"][cid] for r in results])
        if all("clap" in r for r in results):
            return self._timed(emb.embed, [r["clap"] for r in results])
        raise RuntimeError(f"no CLAP input for {emb.key} (features were computed for "
                           "another extractor)")

    def active(self) -> bool:
        return self.tagger is not None or bool(self.clap)

    def _reset_peak(self) -> None:
        if self.device and self.device.startswith("cuda"):
            try:
                import torch

                torch.cuda.reset_peak_memory_stats()
            except Exception:  # noqa: BLE001
                pass

    def _peak_mb(self) -> float | None:
        if not (self.device and self.device.startswith("cuda")):
            return None
        try:
            import torch

            return torch.cuda.max_memory_allocated() / 2**20
        except Exception:  # noqa: BLE001
            return None

    def _timed(self, fn, *args):
        t0 = time.monotonic()
        try:
            return fn(*args)
        finally:
            self.infer_s += time.monotonic() - t0

    def _keys_for(self, stored: str | None) -> list[str]:
        return [k for k in clap_keys_needed(stored, self.versions, self.force) if k in self.clap]

    def prefetch(self, conn, entries: list[tuple[int, dict]]) -> None:
        """Batched inference for several files; failures fall back to per-file later."""
        if self.tagger is not None:
            idx = [i for i, (_, r) in enumerate(entries) if "panns" in r]
            if idx:
                try:
                    scores = self._timed(self.tagger.tag, [entries[i][1]["panns"] for i in idx])
                    for i, s in zip(idx, scores, strict=True):
                        self._pre.setdefault(entries[i][0], {})["tags"] = s
                except Exception:  # noqa: BLE001 - retried file by file in outputs()
                    pass
        if self.clap:
            need = {
                aid: self._keys_for(_stage_version(conn, aid, "embeddings"))
                for aid, r in entries if _has_clap_input(r)
            }
            for key, emb in self.clap.items():
                idx = [i for i, (aid, _) in enumerate(entries) if key in need.get(aid, ())]
                if not idx:
                    continue
                try:
                    vecs = self._embed(emb, [entries[i][1] for i in idx])
                    for i, v in zip(idx, vecs, strict=True):
                        self._pre.setdefault(entries[i][0], {}).setdefault("emb", {})[key] = v
                except Exception:  # noqa: BLE001
                    pass

    def outputs(self, asset_id: int, result: dict, needed, embed_stored: str | None) -> dict:
        pre = self._pre.pop(asset_id, {})
        out: dict = {}
        errors: list[str] = []
        if "tags" in needed and self.tagger is not None and "panns" in result:
            scores = pre.get("tags")
            if scores is None:
                try:
                    scores = self._timed(self.tagger.tag, [result["panns"]])[0]
                except Exception as e:  # noqa: BLE001
                    errors.append(f"tags: {type(e).__name__}: {e}")
            if scores is not None:
                out["tags"] = panns.scores_to_tags(scores, self.tagger.labels)
                out["tags_model"] = self.tagger.key
                self.tagged += 1
        if "embeddings" in needed and self.clap and _has_clap_input(result):
            done: dict = {}
            for key in self._keys_for(embed_stored):
                vec = pre.get("emb", {}).get(key)
                if vec is None:
                    try:
                        vec = self._embed(self.clap[key], [result])[0]
                    except Exception as e:  # noqa: BLE001
                        errors.append(f"embeddings {key}: {type(e).__name__}: {e}")
                        continue
                done[key] = np.asarray(vec, dtype=np.float32)
            if done:
                out["embeddings"] = done
                self.embedded += 1
        if errors:
            out["errors"] = errors
            self._errors.extend(errors)
        return out

    def take_errors(self) -> list[str]:
        errors, self._errors = self._errors, []
        return errors

    def report(self) -> str:
        peak = self._peak_mb()
        return (
            f"models on {self.device}: tagged {self.tagged} files, embedded {self.embedded} "
            f"files with {len(self.clap)} CLAP checkpoint(s), {self.infer_s:.1f}s inference"
            + (f", peak GPU memory {peak:.0f} MiB" if peak is not None else "")
        )

    def record_provenance(self, conn) -> None:
        """meta `models`: which checkpoints produced the tags and embeddings rows."""
        try:
            current = json.loads(db.get_meta(conn, "models") or "{}")
            if not isinstance(current, dict):
                current = {}
        except ValueError:
            current = {}
        if self.tagger is not None:
            current["tags"] = {
                "model": self.tagger.key, "checkpoint": panns.CHECKPOINT_NAME,
                "source": f"https://zenodo.org/records/{panns.ZENODO_RECORD}",
                "license": "CC-BY-4.0", "min_score": panns.TAG_MIN_SCORE,
                "music_threshold": panns.MUSIC_THRESHOLD,
            }
        emb = current.get("embeddings") if isinstance(current.get("embeddings"), dict) else {}
        for key in self.clap:
            name = clap_models.short_name_for_key(key)
            emb[key] = {
                "name": name,
                "repo": clap_models.CLAP_CHECKPOINTS.get(name),
                "revision": clap_models.REVISIONS.get(name),
                "license": clap_models.LICENSES.get(name),
                "windows": f"<= {prep.MAX_WINDOWS} x {prep.WINDOW_S:g} s, mean-pooled",
            } if name else {"name": None}
        current["embeddings"] = emb
        with db.transaction(conn):
            db.set_meta(conn, "models", json.dumps(current, sort_keys=True))


def _handle(cfg, conn, item: _Work, summary: RunSummary, prog: _Progress, gone_ids,
            state: dict, step: Callable[[], int | None]) -> None:
    """Run one file's step (analysis + commit) with the shared error semantics."""
    if state.get("models") is not None:
        state["models"].take_errors()  # only this file's model errors are reported below
    try:
        moved_from = step()
    except KeyboardInterrupt:
        raise
    except LibraryReadError as e:
        if not _root_reachable(cfg):
            raise LibraryUnavailable(
                f"the library root {cfg.library_root} became unreachable during the run "
                f"({e}); stopped. Run `foley index` again once the share is back."
            ) from e
        state["read_errors"] += 1
        summary.errors += 1
        _record_error(conn, item.asset_id, str(e), keep_analyzed=True)
        prog.say(f"  error: {item.rel_path}: {e}"[:300])
        if state["read_errors"] >= MAX_CONSECUTIVE_READ_ERRORS:
            raise LibraryUnavailable(
                f"{state['read_errors']} library reads failed in a row (last: {item.rel_path}: "
                f"{e}); stopped. Check the share and run `foley index` again."
            ) from e
    except Exception as e:  # noqa: BLE001 - any failure is recorded on the asset
        state["read_errors"] = 0
        summary.errors += 1
        reason = (
            str(e) if isinstance(e, (decode.DecodeError, _WorkerFailure))
            else f"{type(e).__name__}: {e}"
        )
        _record_error(conn, item.asset_id, reason)
        prog.say(f"  error: {item.rel_path}: {reason}"[:300])
    else:
        state["read_errors"] = 0
        summary.analyzed += 1
        models = state.get("models")
        for reason in models.take_errors() if models is not None else ():
            summary.errors += 1
            prog.say(f"  error: {item.rel_path}: {reason}"[:300])
        if moved_from is not None:
            summary.moved += 1
            if item.new_this_run:
                summary.added -= 1
            if moved_from in gone_ids:  # it was marked missing earlier in this run
                summary.missing -= 1
    prog.tick(summary, item.rel_path)
    if prog.done % _PROGRESS_EVERY == 0:
        _save_run(conn, summary, None, None)


def _analyze_all(
    cfg, conn, work, requested, force, summary: RunSummary, prog: _Progress, gone_ids: set[int],
    workers: int | None = None,
):
    versions = stage_versions(cfg)
    models = None
    wanted = {s for item in work for s in item.needed} & set(MODEL_STAGES)
    if wanted:
        models = _Models(cfg, versions, force, prog)
        disabled = models.load(wanted)
        if disabled:
            summary.notes.extend(models.notes)
            requested = [s for s in requested if s not in disabled]
            for item in work:
                item.needed -= disabled
            work = [item for item in work if item.needed]
        if not models.active():
            models = None
        else:
            models.record_provenance(conn)
    prog.total = len(work)
    prog.started = time.monotonic()
    state = {"read_errors": 0, "models": models}  # consecutive read failures, models
    n_workers = default_workers() if workers is None else max(1, int(workers))

    def serial(item: _Work) -> None:
        _handle(cfg, conn, item, summary, prog, gone_ids, state,
                lambda: _analyze(cfg, conn, item, requested, force, versions, models=models))

    pooled: list[_Work] = []
    if n_workers > 1:
        rows = {
            r["id"]: r for r in conn.execute(
                "SELECT id, content_hash, size_bytes FROM assets WHERE status != 'missing'"
            )
        }
        for item in work:
            row = rows.get(item.asset_id)
            if row is not None and row["size_bytes"] <= LARGE_FILE_BYTES and (
                _needs_read(item.needed, row["content_hash"], item.candidate)
            ):
                pooled.append(item)
        if workers is None and len(pooled) <= n_workers:
            pooled = []  # not worth starting processes for a handful of files
    try:
        if not pooled:
            for item in work:
                serial(item)
        else:
            pooled_ids = {w.asset_id for w in pooled}
            rest = [w for w in work if w.asset_id not in pooled_ids]
            with _single_threaded_children():
                _run_pool(cfg, conn, pooled, rest, requested, force, versions, n_workers,
                          summary, prog, gone_ids, state, serial, models)
    finally:
        if models is not None:
            models.close()
    elapsed = time.monotonic() - prog.started
    if work:
        prog.say(
            f"analyzed {summary.analyzed} files in {_fmt_duration(elapsed)} "
            f"({len(work) / max(elapsed, 1e-6):.1f} files/s), {summary.errors} errors"
            + (f", {n_workers} workers" if pooled else "")
        )
    if models is not None:
        prog.say(models.report())


# ---- parallel analysis --------------------------------------------------------------
#
# Workers (spawned processes) only read, hash and compute: they get a picklable _Job and
# return a small dict (content hash, metadata dict, audio hash, feature/loudness
# numbers). The main process alone touches SQLite: move resolution, captions, FTS,
# asset_stages and status, in the same per-file transaction as the serial path
# (_analyze with a precomputed result). Files over LARGE_FILE_BYTES and files that need
# no read (caption/fts-only bumps) run in the main process.

# "module:function" called in each worker after start-up. Tests only (fakes, audit hook).
WORKER_INIT_HOOK: str | None = None
_WORKER_MOVES: dict = {}
# CLAP feature extractors (config id -> extractor) for _compute: set in the workers from
# the indexer's loaded checkpoints, and in the indexer itself for files it reads.
_CLAP_EXTRACTORS: dict = {}
_MAX_TASKS_PER_CHILD = 500
_WORKER_START_TIMEOUT = 300  # s; first imports can be slow


class _WorkerFailure(Exception):
    """A worker's non-read failure, already formatted like the serial path's message."""


def _worker_init(moves: dict, hook: str | None, extractors: dict | None = None) -> None:
    import signal

    signal.signal(signal.SIGINT, signal.SIG_IGN)  # the main process handles Ctrl+C
    _WORKER_MOVES.clear()
    _WORKER_MOVES.update(moves)
    _CLAP_EXTRACTORS.clear()
    for cid, config in (extractors or {}).items():
        _CLAP_EXTRACTORS[cid] = clap_models.make_extractor(config)
    if hook:
        import importlib

        module, _, func = hook.partition(":")
        getattr(importlib.import_module(module), func)()


def _worker_compute(job: _Job) -> tuple:
    try:
        return ("ok", _compute(job, _WORKER_MOVES.get))
    except LibraryReadError as e:
        return ("read", str(e))
    except decode.DecodeError as e:
        return ("other", str(e))
    except Exception as e:  # noqa: BLE001 - reported per file, like the serial path
        return ("other", f"{type(e).__name__}: {e}")


def _move_map(conn, requested, force, versions) -> dict:
    """content_hash of every missing asset -> (stages a move onto it needs, its metadata).
    Several missing assets with one hash: the union, so a worker computes enough."""
    done: dict[int, dict[str, str]] = {}
    for r in conn.execute(
        "SELECT s.asset_id, s.stage, s.version FROM asset_stages s "
        "JOIN assets a ON a.id = s.asset_id WHERE a.status = 'missing'"
    ):
        done.setdefault(r[0], {})[r[1]] = r[2]
    out: dict[str, tuple[frozenset, str | None]] = {}
    for r in conn.execute(
        "SELECT id, content_hash, source_metadata FROM assets "
        "WHERE status = 'missing' AND content_hash IS NOT NULL"
    ):
        needs = _needs_after_move(done.get(r[0], {}), requested, force, versions)
        prev = out.get(r[1])
        out[r[1]] = (needs | prev[0], prev[1]) if prev else (needs, r[2])
    return out


# One math thread per worker: N workers each starting a BLAS/OpenMP/numba thread pool
# per CPU oversubscribe the machine (measured: 5.5 -> 14.5 files/s with 8 workers).
# Children read these at start-up, so they are set while the pool exists (replacement
# workers start later). Values the user set explicitly are kept.
_THREAD_ENV = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
               "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "NUMBA_NUM_THREADS")


class _single_threaded_children:
    def __enter__(self):
        self.added = [k for k in _THREAD_ENV if k not in os.environ]
        for k in self.added:
            os.environ[k] = "1"
        return self

    def __exit__(self, *exc) -> None:
        for k in self.added:
            os.environ.pop(k, None)


class _PoolUnavailable(Exception):
    """Worker processes cannot be started here (or die on start-up)."""


def _worker_ping() -> int:
    return os.getpid()


def _run_pool(cfg, conn, pooled, rest, requested, force, versions, n_workers,
              summary, prog, gone_ids, state, serial, models=None) -> None:
    import multiprocessing
    from collections import deque
    from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
    from concurrent.futures.process import BrokenProcessPool

    moves = _move_map(conn, requested, force, versions)

    def new_pool(size: int) -> ProcessPoolExecutor:
        """A pool whose workers have proven they start (one ping), else _PoolUnavailable."""
        last: BaseException | None = None
        for _ in range(2):
            pool = ProcessPoolExecutor(
                max_workers=size, mp_context=multiprocessing.get_context("spawn"),
                initializer=_worker_init,
                initargs=(moves, WORKER_INIT_HOOK,
                          models.extractor_configs if models is not None else {}),
                max_tasks_per_child=_MAX_TASKS_PER_CHILD,
            )
            try:
                pool.submit(_worker_ping).result(timeout=_WORKER_START_TIMEOUT)
                return pool
            except KeyboardInterrupt:
                _kill_pool(pool)
                raise
            except Exception as e:  # noqa: BLE001 - broken pool, timeout, pickling...
                last = e
                _kill_pool(pool)
        raise _PoolUnavailable(f"{type(last).__name__}: {last}")

    def job_for(item: _Work) -> _Job | None:
        row = conn.execute("SELECT * FROM assets WHERE id=?", (item.asset_id,)).fetchone()
        return None if row is None else _job(cfg, row, item, requested)

    queue = deque(pooled)
    suspects: deque = deque()  # in flight when a worker died: retried one at a time
    inflight: dict = {}
    main_items = deque(rest)  # no-read and large files: done here between results
    deferred: list = []  # (item, outcome) waiting for batched model inference
    cap = 2 * n_workers
    pool = None

    def commit(item, outcome) -> None:
        _handle(cfg, conn, item, summary, prog, gone_ids, state,
                _pooled_step(cfg, conn, item, outcome, requested, force, versions, models))

    def flush() -> None:
        batch = list(deferred)
        deferred.clear()
        models.prefetch(conn, [(item.asset_id, outcome[1]) for item, outcome in batch])
        for item, outcome in batch:
            commit(item, outcome)

    try:
        pool = new_pool(n_workers)
        prog.say(f"analyzing with {n_workers} worker processes")
        while queue or suspects or inflight or main_items or deferred:
            if deferred and (len(deferred) >= MODEL_BATCH_FILES
                             or not (queue or suspects or inflight)):
                flush()
                continue
            if suspects:
                if not inflight:
                    item = suspects.popleft()
                    job = job_for(item)
                    if job is None:
                        serial(item)
                        continue
                    inflight[pool.submit(_worker_compute, job)] = (item, True)
            else:
                while queue and len(inflight) < cap:
                    item = queue.popleft()
                    job = job_for(item)
                    if job is None:
                        serial(item)  # records the "row vanished" error
                        continue
                    inflight[pool.submit(_worker_compute, job)] = (item, False)
            if main_items and not any(f.done() for f in inflight):
                serial(main_items.popleft())
                continue
            if not inflight:
                continue
            done, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
            broken: list = []
            for fut in done:
                item, isolated = inflight.pop(fut)
                try:
                    outcome = fut.result()
                except BrokenProcessPool:
                    broken.append((item, isolated))
                    continue
                if models is not None and outcome[0] == "ok" and _has_model_input(outcome[1]):
                    deferred.append((item, outcome))
                else:
                    commit(item, outcome)
            if broken:
                broken += list(inflight.values())
                inflight.clear()
                _kill_pool(pool)
                pool = None
                for item, isolated in broken:
                    if isolated:  # it crashed alone: it is the culprit
                        _handle(cfg, conn, item, summary, prog, gone_ids, state,
                                _raiser(_WorkerFailure(
                                    "analysis worker process crashed on this file "
                                    "(out of memory or a decoder crash?)")))
                    else:
                        suspects.append(item)
                pool = new_pool(n_workers)
    except _PoolUnavailable as e:
        prog.say(f"worker processes unavailable ({e}); continuing in this process")
        if pool is not None:
            _kill_pool(pool)
        if deferred:
            flush()
        for item in [*(i for i, _ in inflight.values()), *suspects, *queue, *main_items]:
            serial(item)
    except BaseException:
        if pool is not None:
            _kill_pool(pool)
        raise
    else:
        pool.shutdown(wait=True)


def _raiser(exc: Exception) -> Callable[[], int | None]:
    def step() -> int | None:
        raise exc

    return step


def _pooled_step(cfg, conn, item, outcome, requested, force, versions, models=None):
    kind = outcome[0]
    if kind == "read":
        return _raiser(LibraryReadError(outcome[1]))
    if kind == "other":
        return _raiser(_WorkerFailure(outcome[1]))
    return lambda: _analyze(cfg, conn, item, requested, force, versions, result=outcome[1],
                            models=models)


def _has_clap_input(result: dict) -> bool:
    return "clap" in result or "clap_feats" in result


def _has_model_input(result: dict) -> bool:
    return "panns" in result or _has_clap_input(result)


def _kill_pool(pool) -> None:
    """Stop now (Ctrl+C or an abort): drop queued jobs and end the worker processes."""
    try:
        procs = list((getattr(pool, "_processes", None) or {}).values())
        pool.shutdown(wait=False, cancel_futures=True)
        for p in procs:
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass
        for p in procs:
            try:
                p.join(timeout=5)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass


def _root_reachable(cfg: Config) -> bool:
    try:
        with os.scandir(cfg.library_root) as it:
            next(it, None)
        return True
    except OSError:
        return False


def _record_error(conn, asset_id: int, reason: str, keep_analyzed: bool = False) -> None:
    """Record a failure. With keep_analyzed (I/O errors), an asset analyzed before keeps
    its status so a flaky share does not pull good files out of search."""
    status_sql = (
        "CASE WHEN indexed_at IS NOT NULL THEN status ELSE 'error' END"
        if keep_analyzed else "'error'"
    )
    with db.transaction(conn):
        conn.execute(
            f"UPDATE assets SET status={status_sql}, error=?, error_count=error_count+1 "
            "WHERE id=?",
            (reason[:1000], asset_id),
        )


def _find_move_target(conn, content_hash: str, rel_path: str):
    rows = conn.execute(
        "SELECT * FROM assets WHERE content_hash=? AND status='missing'", (content_hash,)
    ).fetchall()
    if not rows:
        return None
    name = rel_path.rsplit("/", 1)[-1].casefold()

    def key(r):
        same_name = r["rel_path"].rsplit("/", 1)[-1].casefold() == name
        return (not same_name, "" if r["missing_since"] is None else _neg(r["missing_since"]),
                r["id"])

    return min(rows, key=key)


def _neg(text: str) -> str:
    # sort most recently missing first
    return "".join(chr(0x10FFFF - ord(c)) for c in text)


def _load_features(conn, asset_id: int) -> dict:
    row = conn.execute("SELECT * FROM features WHERE asset_id=?", (asset_id,)).fetchone()
    if row is None:
        return {}
    out = {k: row[k] for k in FEATURE_COLUMNS}
    if isinstance(out.get("band_energy"), str):
        try:
            out["band_energy"] = json.loads(out["band_energy"])
        except ValueError:
            out["band_energy"] = None
    return out


def _stream_hash(path: Path) -> str:
    """Same digest as decode.content_hash(read_bytes(path)), without holding the file."""
    h = xxhash.xxh3_128()
    with open(path, "rb") as f:
        while block := f.read(_HASH_BLOCK):
            h.update(block)
    return h.hexdigest()


def _metadata_bytes(path: Path) -> bytes:
    """A small stand-in for a large file that read_metadata parses the same way: for
    RIFF/RF64 every chunk except the audio (`data` emitted empty), else the file head."""
    with open(path, "rb") as f:
        head = f.read(12)
        if head[:4] not in (b"RIFF", b"RF64", b"BW64") or len(head) < 12:
            return head + f.read(_NON_RIFF_HEAD)
        out = bytearray(b"RIFF" + bytes(4) + head[8:12])
        pos, ds64_data, count = 12, None, 0
        total = os.fstat(f.fileno()).st_size

        def read8(at: int) -> bytes:
            f.seek(at)
            return f.read(8)

        while count < 10000:
            count += 1
            f.seek(pos)
            hdr = f.read(8)
            if len(hdr) < 8 or not all(0x20 <= b <= 0x7E for b in hdr[:4]):
                break
            cid = hdr[:4]
            (size,) = struct.unpack("<I", hdr[4:])
            if cid == b"data":
                if size == 0xFFFFFFFF and ds64_data is not None:
                    size = ds64_data
                out += b"data" + struct.pack("<I", 0)
            elif cid == b"ds64":
                body = f.read(min(size, 64))
                if len(body) >= 16:
                    ds64_data = struct.unpack_from("<Q", body, 8)[0]
            else:
                body = f.read(min(size, _MAX_META_CHUNK))
                pad = b"\x00" if len(body) % 2 else b""
                out += cid + struct.pack("<I", len(body)) + body + pad
            pos = scan.riff_next_offset(read8, pos, size, total)
        struct.pack_into("<I", out, 4, len(out) - 8)
        return bytes(out)


_CONTENT_STAGES = frozenset({"metadata", *_AUDIO_STAGES, *MODEL_STAGES})  # need the bytes


@dataclass(frozen=True)
class _Job:
    """Everything needed to analyze one file away from the database (picklable)."""

    asset_id: int
    rel_path: str
    path: str
    ext: str
    large: bool
    needed: frozenset
    requested: tuple
    candidate: bool
    stored_hash: str | None
    stored_metadata: str | None  # source_metadata JSON, for kind_hint when not re-read


def _needs_read(needed, content_hash, candidate: bool) -> bool:
    return bool(set(needed) & _CONTENT_STAGES) or content_hash is None or candidate


def _needs_after_move(done: dict, requested, force, versions) -> frozenset:
    needed = {s for s in requested if force or not stage_current(s, done.get(s), versions)}
    return frozenset(needed | (set(_PATH_STAGES) & set(requested)))


def _job(cfg, row, item: _Work, requested) -> _Job:
    return _Job(
        asset_id=row["id"], rel_path=row["rel_path"], path=str(cfg.abs_path(row["rel_path"])),
        ext=row["ext"], large=row["size_bytes"] > LARGE_FILE_BYTES,
        needed=frozenset(item.needed), requested=tuple(requested), candidate=item.candidate,
        stored_hash=row["content_hash"], stored_metadata=row["source_metadata"],
    )


def _compute(job: _Job, resolve_move: Callable[[str], tuple | None]) -> dict:
    """Read the file once, hash it, and compute the content stages it needs.

    Runs in a worker process or in the main process; touches no database. Which stages
    it computes follows the same rules as the commit: a never-analyzed file whose hash
    matches a missing asset (resolve_move) needs only what that move needs; bytes that
    changed under an unchanged size/mtime need every requested stage.
    """
    path = Path(job.path)
    needed = set(job.needed)
    stored_metadata = job.stored_metadata
    data = None
    if job.large:  # never hold a huge file in memory next to its decoded audio
        chash = _read(_stream_hash, path)
    else:
        data = _read(decode.read_bytes, path)
        chash = decode.content_hash(data)
    move = resolve_move(chash) if job.candidate else None
    if move is not None:
        needed, stored_metadata = set(move[0]), move[1]
    elif job.stored_hash is not None and job.stored_hash != chash:
        needed = set(job.requested)
    out: dict = {"content_hash": chash, "computed": []}
    if "metadata" in needed:
        head = _read(_metadata_bytes, path) if job.large else data
        out["metadata"] = scan.read_metadata(head, job.ext, job.rel_path)
        out["computed"].append("metadata")
        del head
        metadata = out["metadata"]
    else:
        try:
            metadata = json.loads(stored_metadata) if stored_metadata else None
        except ValueError:
            metadata = None
    if needed & {*_AUDIO_STAGES, *MODEL_STAGES}:
        if job.large:
            audio = _read(decode.decode_path, path)
        else:
            audio = decode.decode_bytes(data, job.ext)
        data = None
        if needed & set(_AUDIO_STAGES):
            out["audio_hash"] = decode.audio_hash(audio)
        if "tags" in needed:
            out["panns"] = prep.panns_windows(audio)
            out["computed"].append("tags")
        if "embeddings" in needed:
            windows = prep.clap_windows(audio)
            if _CLAP_EXTRACTORS:  # the log-mel features, once per distinct extractor
                out["clap_feats"] = {
                    cid: clap_models.extract(ex, windows) for cid, ex in _CLAP_EXTRACTORS.items()
                }
            else:
                out["clap"] = windows
            del windows
            out["computed"].append("embeddings")
        if "features" in needed:
            out["features"] = features.compute_features(
                audio, scan.kind_hint(job.rel_path, metadata)
            )
            out["computed"].append("features")
        if "loudness" in needed:
            out["loudness"] = loudness.measure(audio)
            out["computed"].append("loudness")
        del audio
    return out


def _stage_version(conn, asset_id: int, stage: str) -> str | None:
    row = conn.execute(
        "SELECT version FROM asset_stages WHERE asset_id=? AND stage=?", (asset_id, stage)
    ).fetchone()
    return row[0] if row else None


def _stored_tags(conn, asset_id: int) -> list[tuple[str, float]]:
    return [
        (r[0], r[1]) for r in conn.execute(
            "SELECT label, score FROM tags WHERE asset_id=? ORDER BY score DESC, label",
            (asset_id,),
        )
    ]


def _drop_model_outputs(conn, asset_id: int) -> None:
    """The asset's bytes changed: its tags, embeddings and is_music describe old audio."""
    conn.execute("DELETE FROM tags WHERE asset_id=?", (asset_id,))
    conn.execute("DELETE FROM embeddings WHERE asset_id=?", (asset_id,))
    conn.execute("UPDATE features SET is_music=NULL WHERE asset_id=?", (asset_id,))


def _live_move_resolver(conn, rel, requested, force, versions):
    def resolve(chash: str) -> tuple | None:
        target = _find_move_target(conn, chash, rel)
        if target is None:
            return None
        done = {
            r[0]: r[1] for r in conn.execute(
                "SELECT stage, version FROM asset_stages WHERE asset_id=?", (target["id"],)
            )
        }
        return _needs_after_move(done, requested, force, versions), target["source_metadata"]

    return resolve


def _analyze(cfg, conn, item: _Work, requested, force, versions,
             result: dict | None = None, models: _Models | None = None) -> int | None:
    """Analyze one file (or take a worker's `result`) and commit it in one transaction.
    Returns the re-pointed asset id for a move."""
    row = conn.execute("SELECT * FROM assets WHERE id=?", (item.asset_id,)).fetchone()
    if row is None:
        raise RuntimeError("asset row vanished during the run")
    rel = row["rel_path"]
    ext = row["ext"]
    job = _job(cfg, row, item, requested)
    resolver = _live_move_resolver(conn, rel, requested, force, versions)
    if result is None and _needs_read(item.needed, row["content_hash"], item.candidate):
        result = _compute(job, resolver)

    for _attempt in range(2):
        needed = set(item.needed)
        target = None
        content_changed = False
        chash = row["content_hash"]
        if result is not None:
            chash = result["content_hash"]
            if item.candidate:
                target = _find_move_target(conn, chash, rel)
            if target is not None:
                done = {
                    r[0]: r[1] for r in conn.execute(
                        "SELECT stage, version FROM asset_stages WHERE asset_id=?",
                        (target["id"],),
                    )
                }
                needed = set(_needs_after_move(done, requested, force, versions))
            elif row["content_hash"] is not None and row["content_hash"] != chash:
                needed = set(requested)  # bytes changed without a size/mtime change
                content_changed = True
            if (needed & _CONTENT_STAGES) - set(result["computed"]):
                # a worker planned for a move that an earlier file already took: redo here
                result = _compute(job, resolver)
                continue
        break
    asset_id = target["id"] if target is not None else row["id"]
    base = target if target is not None else row

    asset_view = dict(base)
    asset_view.update(rel_path=rel, ext=ext, size_bytes=row["size_bytes"],
                      mtime_ns=row["mtime_ns"], content_hash=chash)
    if "metadata" in needed:
        metadata = result["metadata"]
        pack, category = scan.classify_path(cfg, rel)
        asset_view.update(
            pack=pack, category_path=category, license=scan.find_license(cfg, rel),
            source_metadata=json.dumps(metadata, ensure_ascii=False),
        )
    else:
        try:
            metadata = json.loads(base["source_metadata"]) if base["source_metadata"] else None
        except ValueError:
            metadata = None
        if target is not None:  # path-derived columns follow the new path regardless
            pack, category = scan.classify_path(cfg, rel)
            asset_view.update(pack=pack, category_path=category,
                              license=scan.find_license(cfg, rel))

    feats = _load_features(conn, asset_id)
    new_feats: dict = {}
    ahash = None
    if needed & set(_AUDIO_STAGES):
        ahash = result["audio_hash"]
        if "features" in needed:
            new_feats.update(result["features"])
        if "loudness" in needed:
            new_feats.update(result["loudness"])
    # Model stages: inference (or a batch's prefetched output) for what is still needed.
    model_out: dict = {}
    embed_stored = None
    if needed & set(MODEL_STAGES):
        if not content_changed:
            embed_stored = _stage_version(conn, asset_id, "embeddings")
        if models is not None and result is not None:
            model_out = models.outputs(item.asset_id, result, needed, embed_stored)
    result = None
    feats.update({k: v for k, v in new_feats.items() if k in FEATURE_COLUMNS})
    tags_now = model_out.get("tags")
    if tags_now is not None:
        feats["is_music"] = panns.is_music(tags_now)
    elif content_changed:
        feats["is_music"] = None

    caption = None
    if "caption" in needed or tags_now is not None:
        if tags_now is not None:
            tag_list = tags_now
        elif content_changed:
            tag_list = []
        else:
            tag_list = _stored_tags(conn, asset_id)
        caption = captions.templated_caption(asset_view, feats, tag_list, metadata)

    now = db.utcnow()
    with db.transaction(conn):
        if target is not None:
            conn.execute("DELETE FROM assets WHERE id=?", (row["id"],))
            conn.execute("DELETE FROM search_fts WHERE rowid=?", (row["id"],))
            # path-derived stages not re-run now (a --stage subset) become stale
            conn.executemany(
                "DELETE FROM asset_stages WHERE asset_id=? AND stage=?",
                [(asset_id, s) for s in _PATH_STAGES if s not in needed],
            )
        if content_changed:
            # Every stage's output describes the old bytes: drop them all, so stages not
            # requested in this run are redone later instead of looking current.
            conn.execute("DELETE FROM asset_stages WHERE asset_id=?", (asset_id,))
            _drop_model_outputs(conn, asset_id)
            if ahash is None:
                conn.execute("UPDATE assets SET audio_hash=NULL WHERE id=?", (asset_id,))
        conn.execute(
            "UPDATE assets SET rel_path=?, pack=?, category_path=?, ext=?, size_bytes=?, "
            "mtime_ns=?, content_hash=?, audio_hash=coalesce(?, audio_hash), license=?, "
            "source_metadata=?, last_seen=?, missing_since=NULL WHERE id=?",
            (rel, asset_view.get("pack"), asset_view.get("category_path"), ext,
             row["size_bytes"], row["mtime_ns"], chash, ahash, asset_view.get("license"),
             asset_view.get("source_metadata"), now, asset_id),
        )
        if new_feats:
            values = dict(feats)
            if isinstance(values.get("band_energy"), dict):
                values["band_energy"] = json.dumps(values["band_energy"])
            cols = ("asset_id",) + FEATURE_COLUMNS
            conn.execute(
                f"INSERT OR REPLACE INTO features({', '.join(cols)}) "
                f"VALUES ({', '.join('?' * len(cols))})",
                (asset_id, *(values.get(c) for c in FEATURE_COLUMNS)),
            )
        elif tags_now is not None:
            conn.execute(
                "INSERT INTO features(asset_id, is_music) VALUES (?, ?) "
                "ON CONFLICT(asset_id) DO UPDATE SET is_music=excluded.is_music",
                (asset_id, feats["is_music"]),
            )
        done_now = [(s, versions[s]) for s in CORE_STAGES if s in needed and s != "fts"]
        if tags_now is not None:
            conn.execute("DELETE FROM tags WHERE asset_id=?", (asset_id,))
            conn.executemany(
                "INSERT INTO tags(asset_id, label, score, model) VALUES (?, ?, ?, ?)",
                [(asset_id, label, score, model_out["tags_model"]) for label, score in tags_now],
            )
            done_now.append(("tags", versions["tags"]))
        if "embeddings" in model_out:
            conn.executemany(
                "INSERT OR REPLACE INTO embeddings(asset_id, model, dim, vector) "
                "VALUES (?, ?, ?, ?)",
                [(asset_id, key, int(vec.shape[0]), vec.astype("<f4").tobytes())
                 for key, vec in model_out["embeddings"].items()],
            )
            have = _embed_keys(embed_stored) or set()
            done_now.append(
                ("embeddings", embed_version(have | set(model_out["embeddings"])))
            )
        if caption is not None:
            conn.execute(
                "INSERT OR REPLACE INTO captions(asset_id, model, text) VALUES (?, ?, ?)",
                (asset_id, captions.CAPTION_MODEL, caption),
            )
        if "fts" in needed or tags_now is not None:
            search.index_asset(conn, asset_id)
        if "fts" in needed:
            done_now.append(("fts", versions["fts"]))
        conn.executemany(
            "INSERT OR REPLACE INTO asset_stages(asset_id, stage, version, done_at) "
            "VALUES (?, ?, ?, ?)",
            [(asset_id, s, v, now) for s, v in done_now],
        )
        current = {
            r[0]: r[1] for r in conn.execute(
                "SELECT stage, version FROM asset_stages WHERE asset_id=?", (asset_id,)
            )
        }
        complete = all(stage_current(s, current.get(s), versions) for s in CORE_STAGES)
        conn.execute(
            "UPDATE assets SET status=?, error=NULL, error_count=0, "
            "indexed_at=CASE WHEN ? THEN ? ELSE indexed_at END WHERE id=?",
            ("active" if complete else "pending", complete, now, asset_id),
        )
        if model_out.get("errors"):
            reason = "; ".join(model_out["errors"])
            conn.execute(
                "UPDATE assets SET error=?, error_count=error_count+1 WHERE id=?",
                (reason[:1000], asset_id),
            )
    return None if target is None else asset_id


# --------------------------------------------------------------------------- prune


def prune(cfg: Config, dry_run: bool = True) -> list[str]:
    check_data_dir(cfg)
    lock = None if dry_run else _acquire_lock(cfg)
    try:
        conn = db.open_catalog(cfg)
        try:
            return _prune(cfg, conn, dry_run)
        finally:
            conn.close()
    finally:
        if lock is not None:
            _release_lock(cfg, lock)


_PRUNE_SQL = """
    SELECT a.id, a.rel_path FROM assets a
    WHERE a.status = 'missing' AND a.missing_since IS NOT NULL AND a.missing_since < ?
      AND (a.content_hash IS NULL OR (
            NOT EXISTS (SELECT 1 FROM ledger.decisions d WHERE d.content_hash = a.content_hash)
        AND NOT EXISTS (SELECT 1 FROM ledger.usages u WHERE u.content_hash = a.content_hash)
        AND NOT EXISTS (SELECT 1 FROM ledger.dedupe_actions x
                        WHERE x.content_hash = a.content_hash)))
    ORDER BY a.rel_path
"""


def _prune(cfg: Config, conn, dry_run: bool) -> list[str]:
    cutoff = (datetime.now(UTC) - timedelta(days=cfg.prune_missing_days)).isoformat(
        timespec="seconds"
    )
    rows = conn.execute(_PRUNE_SQL, (cutoff,)).fetchall()
    if dry_run or not rows:
        return [r["rel_path"] for r in rows]
    data_dir = cfg.data_dir.resolve()
    removed: list[str] = []
    with db.transaction(conn):
        for r in conn.execute(_PRUNE_SQL, (cutoff,)).fetchall():  # re-check inside the txn
            for (local,) in conn.execute(
                "SELECT local_path FROM images WHERE asset_id=?", (r["id"],)
            ).fetchall():
                path = (cfg.data_dir / local).resolve()
                if path.is_relative_to(data_dir):  # only ever delete our own cache files
                    path.unlink(missing_ok=True)
            conn.execute("DELETE FROM search_fts WHERE rowid=?", (r["id"],))
            conn.execute("DELETE FROM assets WHERE id=?", (r["id"],))
            removed.append(r["rel_path"])
    return removed
