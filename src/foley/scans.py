"""Daily library scans run by the always-on `foley browse` server.

CONTRACT:

    Settings: run `foley index` daily at a local wall-clock time on the server machine.
    @dataclass ScanSettings: enabled: bool, time: "HH:MM" (24 h), catch_up: bool
        Defaults come from foley.toml [schedule] (enabled = false, time = "03:00",
        catch_up = true, startup_delay_minutes = 5). Once the UI saves, all three values
        live in data_dir/schedule.json and override foley.toml (delete the file to fall
        back to foley.toml). startup_delay_minutes is foley.toml only.
    SCHEDULE_FILE = "schedule.json"
    load_settings(cfg) -> ScanSettings        an unreadable schedule.json is ignored (logged)
    merge_settings(base, update: dict) -> ScanSettings
        `update` may hold enabled (bool), time ("H:MM"/"HH:MM"), catch_up (bool); anything
        else, or a bad value, raises ValueError with a message fit for the user.
    save_settings(cfg, settings) -> None      atomic write of schedule.json

    Slot arithmetic (pure; `tz` is a tzinfo, None = this machine's local zone; datetimes
    are timezone-aware and are compared as instants):
    next_run(after, hhmm, tz=None) -> datetime     first slot strictly after `after`
    previous_slot(at, hhmm, tz=None) -> datetime   latest slot at or before `at`
    catch_up_due(last_completed, now, hhmm, tz=None) -> bool
        True when no scan ever completed or the last one finished before the latest slot.
        A slot is the wall-clock time on a calendar day. On a DST spring-forward day a
        slot inside the skipped hour runs at the same wall-clock offset after it (02:30
        -> 03:30); on a fall-back day a repeated time runs once, at its first occurrence.

    index_command(cfg) -> list[str]
        [sys.executable, "-m", "foley.cli", ("--config", cfg.source if known), "index"]
    classify(exit_code, output, stopped) -> (outcome, note)
        "stopped" if a stop was requested; "ok" for exit 0; "skipped" for exit 2 with the
        indexer-lock message (sync.IndexLocked, printed by `foley index`); else "failed".

    class ScanScheduler(cfg, command=None, clock=None, tz=None, last_completed=None)
        `command` (list or zero-arg callable returning one) replaces index_command for
        tests; `clock` returns an aware "now"; `last_completed` returns the finish time of
        the last completed index run (default: read from catalog.db index_runs).
        plan_startup(now=None) -> datetime | None
                                   first slot after now; with enabled + catch_up and
                                   catch_up_due(...), a catch-up at now +
                                   startup_delay_minutes (returned)
        start()                    plan_startup() + background thread calling tick()
        shutdown(stop_running=True)
        tick(now=None)             one scheduling decision (the thread calls it every
                                   TICK_S; tests call it with an injected time)
        run_now(trigger) -> bool   start a scan ("schedule" | "catch-up" | "manual");
                                   False when one is already running
        stop() -> bool             terminate the running scan's whole process tree
                                   (taskkill /T /F on Windows, the process group on POSIX)
        wait(timeout=None) -> bool True once no scan is running
        settings -> ScanSettings; update_settings(dict) -> ScanSettings (validated, saved,
                                   next slot recomputed)
        running() -> dict | None   {trigger, started_at, log, tail: [last TAIL_LINES lines]}
        history() -> list[dict]    outcomes, newest first
        snapshot(conn) -> dict     the GET /api/scans payload (see browse's contract)
        status(conn) -> dict       {running, enabled, next_run_at, last_completed_at,
                                    trigger, started_at, progress}  (/api/status `scan`)

    Files, all in cfg.data_dir (never the library):
        logs/scan-YYYYmmdd-HHMMSS[-N].log   stdout+stderr of each scan; newest KEEP_LOGS kept
        logs/scans.json                     outcomes, last KEEP_HISTORY: {trigger, outcome
                                            (ok|failed|skipped|stopped), exit_code,
                                            started_at, finished_at (server-local ISO with
                                            offset), log, note, run_id}
    LOG_NAME = regex of valid log names (the only files GET /api/scans/log serves)
    log_path(cfg, name) -> Path | None     None unless `name` matches LOG_NAME and exists

DESIGN NOTES:

    A scan is a subprocess, so analysis workers, crashes and Ctrl+C semantics are exactly
    those of `foley index`; the server only watches it. Its output goes straight to the
    log file (the scan survives the server going away) and a tail thread follows the file
    into memory. `foley index` commits per file and resumes, so stopping is always safe.
    Only one scan runs at a time from here; a CLI run holding the indexer lock makes ours
    exit 2 and is recorded as skipped (the next slot tries again). A slot that arrives
    while a scan is running is recorded as skipped. Outcomes are joined with index_runs
    (by the run id the CLI prints, else by start time) when the API is read.
    Logging goes through the "foley.scans" logger only (never print: under pythonw.exe
    sys.stdout and sys.stderr are None).
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from datetime import time as dtime
from pathlib import Path

from foley.config import Config, parse_hhmm

log = logging.getLogger("foley.scans")

SCHEDULE_FILE = "schedule.json"
LOGS_DIR = "logs"
HISTORY_FILE = "scans.json"
KEEP_LOGS = 30
KEEP_HISTORY = 50
TAIL_LINES = 200
TICK_S = 1.0
TAIL_POLL_S = 0.5
STOP_WAIT_S = 10.0
LOG_NAME = re.compile(r"scan-\d{8}-\d{6}(?:-\d{1,3})?\.log")
TRIGGERS = ("schedule", "catch-up", "manual")
_LOCKED = re.compile(r"another `foley index`|index\.lock")
_RUN_LINE = re.compile(r"^run (\d+): scanned", re.MULTILINE)


# =============================================================================== settings


@dataclass(frozen=True)
class ScanSettings:
    enabled: bool = False
    time: str = "03:00"
    catch_up: bool = True


def schedule_path(cfg: Config) -> Path:
    return cfg.data_dir / SCHEDULE_FILE


def _defaults(cfg: Config) -> ScanSettings:
    return ScanSettings(
        enabled=bool(cfg.schedule_enabled),
        time=parse_hhmm(cfg.schedule_time),
        catch_up=bool(cfg.schedule_catch_up),
    )


def merge_settings(base: ScanSettings, update: dict) -> ScanSettings:
    if not isinstance(update, dict):
        raise ValueError("schedule settings must be a JSON object")
    unknown = sorted(set(update) - {"enabled", "time", "catch_up"})
    if unknown:
        raise ValueError(f"unknown schedule setting(s): {', '.join(unknown)}")
    values = asdict(base)
    for key in ("enabled", "catch_up"):
        if key in update:
            if not isinstance(update[key], bool):
                raise ValueError(f"`{key}` must be true or false")
            values[key] = update[key]
    if "time" in update:
        values["time"] = parse_hhmm(update["time"])
    return ScanSettings(**values)


def load_settings(cfg: Config) -> ScanSettings:
    base = _defaults(cfg)
    path = schedule_path(cfg)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return base
    except OSError as e:
        log.warning("cannot read %s (%s); using foley.toml [schedule]", path, e)
        return base
    try:
        return merge_settings(base, json.loads(text) if text.strip() else {})
    except ValueError as e:
        log.warning("ignoring %s: %s; using foley.toml [schedule]", path, e)
        return base


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def save_settings(cfg: Config, settings: ScanSettings) -> None:
    _write_json(schedule_path(cfg), asdict(settings))


# =============================================================================== slots


def _hm(hhmm: str) -> tuple[int, int]:
    h, m = parse_hhmm(hhmm).split(":")
    return int(h), int(m)


def _slot(day: date, hhmm: str, tz: tzinfo | None) -> datetime:
    """The instant `hhmm` happens on `day` in `tz`, normalized through a timestamp so a
    time inside a DST gap lands after it."""
    h, m = _hm(hhmm)
    naive = datetime.combine(day, dtime(h, m))
    if tz is None:
        return datetime.fromtimestamp(naive.timestamp()).astimezone()
    return datetime.fromtimestamp(naive.replace(tzinfo=tz).timestamp(), tz)


def _local(dt: datetime, tz: tzinfo | None) -> datetime:
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive = this machine's local time
    return dt.astimezone(tz) if tz is not None else dt.astimezone()


def next_run(after: datetime, hhmm: str, tz: tzinfo | None = None) -> datetime:
    local = _local(after, tz)
    for days in range(0, 4):
        cand = _slot(local.date() + timedelta(days=days), hhmm, tz)
        if cand.timestamp() > local.timestamp():
            return cand
    raise AssertionError("unreachable: a daily slot exists within four days")


def previous_slot(at: datetime, hhmm: str, tz: tzinfo | None = None) -> datetime:
    local = _local(at, tz)
    for days in range(0, 4):
        cand = _slot(local.date() - timedelta(days=days), hhmm, tz)
        if cand.timestamp() <= local.timestamp():
            return cand
    raise AssertionError("unreachable: a daily slot exists within four days")


def catch_up_due(last_completed: datetime | None, now: datetime, hhmm: str,
                 tz: tzinfo | None = None) -> bool:
    if last_completed is None:
        return True
    return _local(last_completed, tz).timestamp() < previous_slot(now, hhmm, tz).timestamp()


# =============================================================================== running


def index_command(cfg: Config) -> list[str]:
    cmd = [sys.executable, "-m", "foley.cli"]
    if cfg.source:
        cmd += ["--config", str(cfg.source)]
    cmd.append("index")
    return cmd


def classify(exit_code: int | None, output: str, stopped: bool) -> tuple[str, str | None]:
    if stopped:
        return "stopped", "stopped from the browser; the next scan resumes where it left off"
    if exit_code == 0:
        return "ok", None
    if exit_code == 2 and _LOCKED.search(output or ""):
        return "skipped", "another `foley index` was running (indexer lock held)"
    last = next(
        (ln.strip() for ln in reversed((output or "").splitlines())
         if ln.strip() and not ln.startswith("# ")),
        None,
    )
    return "failed", (last[:500] if last else None)


def log_path(cfg: Config, name: str) -> Path | None:
    if not isinstance(name, str) or not LOG_NAME.fullmatch(name):
        return None
    path = cfg.data_dir / LOGS_DIR / name
    return path if path.is_file() else None


def _popen_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {"start_new_session": True}


def kill_tree(proc: subprocess.Popen) -> None:
    """Terminate `proc` and everything it started (index's analysis worker pool)."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        try:
            subprocess.run(
                [str(taskkill) if taskkill.exists() else "taskkill", "/T", "/F",
                 "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=30, **_popen_kwargs(),
            )
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("taskkill failed (%s); killing the scan process only", e)
        try:
            proc.wait(STOP_WAIT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(STOP_WAIT_S)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait(STOP_WAIT_S)


class _Run:
    def __init__(self, trigger: str, started: datetime, path: Path, proc: subprocess.Popen):
        self.trigger = trigger
        self.started = started
        self.path = path
        self.proc = proc
        self.tail: deque[str] = deque(maxlen=TAIL_LINES)
        self.stop_requested = False


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt is not None else None


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _utc_text(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")  # index_runs' format


def _catalog_last_completed(cfg: Config) -> datetime | None:
    from foley.catalog import db

    conn = db.open_catalog(cfg)
    try:
        return _last_completed_from(conn)
    finally:
        conn.close()


def _last_completed_from(conn) -> datetime | None:
    row = conn.execute(
        "SELECT max(finished_at) FROM index_runs WHERE status = 'completed'"
    ).fetchone()
    return _parse(row[0]) if row else None


_RUN_COLUMNS = ("id", "status", "scanned", "added", "changed", "moved", "missing", "errors",
                "analyzed", "started_at", "finished_at")


class ScanScheduler:
    def __init__(
        self,
        cfg: Config,
        command: list[str] | Callable[[], list[str]] | None = None,
        clock: Callable[[], datetime] | None = None,
        tz: tzinfo | None = None,
        last_completed: Callable[[], datetime | None] | None = None,
    ):
        self.cfg = cfg
        self.tz = tz
        self._command = command
        self._clock = clock
        self._last_completed = last_completed or (lambda: _catalog_last_completed(cfg))
        self.logs_dir = cfg.data_dir / LOGS_DIR
        self.history_path = self.logs_dir / HISTORY_FILE
        self.poll_s = TAIL_POLL_S
        self.startup_delay_s = max(0.0, float(cfg.schedule_startup_delay_minutes)) * 60.0
        self._lock = threading.RLock()
        self._idle = threading.Event()
        self._idle.set()
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None
        self._settings = load_settings(cfg)
        self._next_slot: datetime | None = None
        self._catch_up_at: datetime | None = None
        self._current: _Run | None = None
        self._history = self._load_history()

    # ----------------------------------------------------------------- time

    def now(self) -> datetime:
        if self._clock is not None:
            return self._clock()
        return datetime.now(self.tz) if self.tz is not None else datetime.now().astimezone()

    def _local(self, dt: datetime | None) -> datetime | None:
        return _local(dt, self.tz) if dt is not None else None

    # ----------------------------------------------------------------- settings

    @property
    def settings(self) -> ScanSettings:
        with self._lock:
            return self._settings

    def update_settings(self, update: dict) -> ScanSettings:
        with self._lock:
            new = merge_settings(self._settings, update)
            save_settings(self.cfg, new)
            changed = new != self._settings
            self._settings = new
            self._next_slot = next_run(self.now(), new.time, self.tz)
            if not new.enabled or not new.catch_up:
                self._catch_up_at = None
        if changed:
            log.info("scan schedule: %s", self._describe(new))
        return new

    def _describe(self, s: ScanSettings) -> str:
        if not s.enabled:
            return "off"
        nxt = self._next_slot or next_run(self.now(), s.time, self.tz)
        return (f"daily at {s.time} (catch-up {'on' if s.catch_up else 'off'}); "
                f"next {_iso(nxt)}")

    # ----------------------------------------------------------------- loop

    def plan_startup(self, now: datetime | None = None) -> datetime | None:
        """First slot after `now`, plus a catch-up `startup_delay_minutes` later when
        enabled, catch_up is on and the last completed scan predates the latest slot.
        Returns the catch-up time (None if none). start() calls it."""
        now = now or self.now()
        with self._lock:
            s = self._settings
            self._next_slot = next_run(now, s.time, self.tz)
            self._catch_up_at = None
            due = False
            if s.enabled and s.catch_up:
                try:
                    due = catch_up_due(self._last_completed(), now, s.time, self.tz)
                except Exception as e:  # noqa: BLE001 - a broken catalog must not stop the server
                    log.warning("cannot read the last scan time (%s); no catch-up", e)
            if due:
                self._catch_up_at = now + timedelta(seconds=self.startup_delay_s)
            catch_up_at = self._catch_up_at
        log.info("scan schedule: %s", self._describe(s))
        if catch_up_at is not None:
            log.info("catch-up scan due (no completed scan since the last %s slot); "
                     "starting at %s", s.time, _iso(catch_up_at))
        return catch_up_at

    def start(self) -> None:
        if self._thread is not None:
            return
        self.plan_startup()
        self._halt.clear()
        self._thread = threading.Thread(target=self._loop, name="foley-scans", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._halt.wait(TICK_S):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - keep scheduling whatever happens
                log.exception("scan scheduler tick failed")

    def shutdown(self, stop_running: bool = True) -> None:
        self._halt.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if stop_running and self.stop():
            self.wait(STOP_WAIT_S + 5)

    def tick(self, now: datetime | None = None) -> str | None:
        """One scheduling decision; returns the trigger it acted on, if any."""
        now = now or self.now()
        fire = None
        with self._lock:
            s = self._settings
            if not s.enabled:
                self._catch_up_at = None
                return None
            if self._next_slot is None:
                self._next_slot = next_run(now, s.time, self.tz)
            if self._catch_up_at is not None and now.timestamp() >= self._catch_up_at.timestamp():
                self._catch_up_at = None
                if s.catch_up and self._current is None:
                    try:
                        due = catch_up_due(self._last_completed(), now, s.time, self.tz)
                    except Exception as e:  # noqa: BLE001
                        log.warning("cannot read the last scan time (%s); no catch-up", e)
                        due = False
                    if due:
                        fire = "catch-up"
            if now.timestamp() >= self._next_slot.timestamp():
                self._next_slot = next_run(now, s.time, self.tz)
                self._catch_up_at = None  # this scan is the catch-up too
                fire = "schedule"
            busy = self._current is not None
        if fire is None:
            return None
        if busy:
            self._record({
                "trigger": fire, "outcome": "skipped", "exit_code": None,
                "started_at": _iso(self._local(now)), "finished_at": _iso(self._local(now)),
                "log": None, "note": "a scan was already running", "run_id": None,
            })
            log.info("%s scan skipped: a scan is already running", fire)
            return fire
        self.run_now(fire)
        return fire

    # ----------------------------------------------------------------- one scan

    def _cmd(self) -> list[str]:
        if self._command is None:
            return index_command(self.cfg)
        return list(self._command() if callable(self._command) else self._command)

    def _new_log(self, started: datetime) -> Path:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        stem = f"scan-{started.strftime('%Y%m%d-%H%M%S')}"
        path = self.logs_dir / f"{stem}.log"
        n = 2
        while path.exists():
            path = self.logs_dir / f"{stem}-{n}.log"
            n += 1
        return path

    def run_now(self, trigger: str = "manual") -> bool:
        if trigger not in TRIGGERS:
            raise ValueError(f"unknown trigger {trigger!r}")
        with self._lock:
            if self._current is not None:
                return False
            started = self._local(self.now())
            path = self._new_log(started)
            cmd = self._cmd()
            env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
            with open(path, "wb") as fh:
                fh.write(f"# foley scan ({trigger}) started {_iso(started)}\n# "
                         f"{subprocess.list2cmdline(cmd)}\n".encode())
                fh.flush()
                try:
                    proc = subprocess.Popen(
                        cmd, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                        cwd=str(self.cfg.data_dir), env=env, **_popen_kwargs(),
                    )
                except OSError as e:
                    fh.write(f"# could not start: {e}\n".encode())
                    proc = None
            if proc is None:
                self._record({
                    "trigger": trigger, "outcome": "failed", "exit_code": None,
                    "started_at": _iso(started), "finished_at": _iso(started),
                    "log": path.name, "note": "could not start `foley index`", "run_id": None,
                })
                log.error("%s scan could not start; see %s", trigger, path)
                self._prune_logs(keep=path)
                return True
            run = _Run(trigger, started, path, proc)
            self._current = run
            self._idle.clear()
        log.info("%s scan started (pid %s), log %s", trigger, proc.pid, path)
        self._prune_logs(keep=path)
        threading.Thread(target=self._watch, args=(run,), name="foley-scan-watch",
                         daemon=True).start()
        return True

    def _watch(self, run: _Run) -> None:
        pos, partial = 0, b""
        try:
            while True:
                done = run.proc.poll() is not None
                pos, partial = self._follow(run, pos, partial)
                if done:
                    break
                time.sleep(self.poll_s)
            if partial:
                run.tail.append(partial.decode("utf-8", "replace").rstrip("\r"))
            code = run.proc.returncode
            output = "\n".join(run.tail)
            outcome, note = classify(code, output, run.stop_requested)
            found = _RUN_LINE.findall(output)
            finished = self._local(self.now())
            try:
                with open(run.path, "ab") as fh:
                    fh.write(f"# {outcome} (exit code {code}) at {_iso(finished)}\n".encode())
            except OSError:
                pass
            self._record({
                "trigger": run.trigger, "outcome": outcome, "exit_code": code,
                "started_at": _iso(run.started), "finished_at": _iso(finished),
                "log": run.path.name, "note": note,
                "run_id": int(found[-1]) if found else None,
            })
            level = logging.INFO if outcome in ("ok", "skipped", "stopped") else logging.ERROR
            log.log(level, "%s scan %s (exit code %s) after %.0f s%s", run.trigger, outcome,
                    code, (finished - run.started).total_seconds(),
                    f": {note}" if note else "")
        except Exception:  # noqa: BLE001
            log.exception("scan watcher failed")
        finally:
            with self._lock:
                if self._current is run:
                    self._current = None
                self._idle.set()

    def _follow(self, run: _Run, pos: int, partial: bytes) -> tuple[int, bytes]:
        try:
            with open(run.path, "rb") as fh:
                fh.seek(pos)
                data = fh.read()
        except OSError:
            return pos, partial
        if not data:
            return pos, partial
        pos += len(data)
        chunks = (partial + data).split(b"\n")
        partial = chunks.pop()
        for raw in chunks:
            line = raw.decode("utf-8", "replace").rstrip("\r")
            if "\r" in line:  # progress redrawn in place: keep the latest state
                line = line.rsplit("\r", 1)[-1]
            run.tail.append(line)
        return pos, partial

    def stop(self) -> bool:
        with self._lock:
            run = self._current
            if run is None:
                return False
            run.stop_requested = True
        log.info("stopping the %s scan (pid %s)", run.trigger, run.proc.pid)
        kill_tree(run.proc)
        return True

    def wait(self, timeout: float | None = None) -> bool:
        return self._idle.wait(timeout)

    def running(self) -> dict | None:
        with self._lock:
            run = self._current
            if run is None:
                return None
            return {"trigger": run.trigger, "started_at": _iso(run.started),
                    "log": run.path.name, "tail": list(run.tail)}

    # ----------------------------------------------------------------- history / files

    def _load_history(self) -> list[dict]:
        try:
            data = json.loads(self.history_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            log.warning("ignoring unreadable %s: %s", self.history_path, e)
            return []
        return [d for d in data if isinstance(d, dict)][-KEEP_HISTORY:] if isinstance(
            data, list) else []

    def _record(self, outcome: dict) -> None:
        with self._lock:
            self._history.append(outcome)
            self._history = self._history[-KEEP_HISTORY:]
            try:
                _write_json(self.history_path, self._history)
            except OSError as e:
                log.error("cannot write %s: %s", self.history_path, e)

    def history(self) -> list[dict]:
        with self._lock:
            return [dict(h) for h in reversed(self._history)]

    def _prune_logs(self, keep: Path | None = None) -> None:
        try:
            logs = [p for p in self.logs_dir.iterdir() if LOG_NAME.fullmatch(p.name)]
        except OSError:
            return
        current = {keep} if keep else set()
        with self._lock:
            if self._current is not None:
                current.add(self._current.path)

        def key(p: Path):
            try:
                return (p.stat().st_mtime_ns, p.name)
            except OSError:
                return (0, p.name)

        logs.sort(key=key, reverse=True)
        for old in logs[KEEP_LOGS:]:
            if old in current:
                continue
            try:
                old.unlink()
            except OSError as e:
                log.warning("cannot remove old scan log %s: %s", old, e)

    # ----------------------------------------------------------------- API views

    def _next_run_at(self) -> datetime | None:
        with self._lock:
            s = self._settings
            if not s.enabled:
                return None
            slot = self._next_slot or next_run(self.now(), s.time, self.tz)
            if self._catch_up_at is not None and self._catch_up_at.timestamp() < slot.timestamp():
                return self._catch_up_at
            return slot

    def _join(self, conn, item: dict) -> dict | None:
        cols = ", ".join(_RUN_COLUMNS)
        row = None
        if item.get("run_id"):
            row = conn.execute(f"SELECT {cols} FROM index_runs WHERE id = ?",
                               (item["run_id"],)).fetchone()
        elif item.get("outcome") != "skipped" and item.get("log"):
            start, end = _parse(item.get("started_at")), _parse(item.get("finished_at"))
            if start is not None:
                lo = _utc_text(start - timedelta(seconds=2))
                hi = _utc_text(end) if end is not None else "9999"
                row = conn.execute(
                    f"SELECT {cols} FROM index_runs WHERE started_at >= ? AND started_at <= ? "
                    "ORDER BY id LIMIT 1", (lo, hi),
                ).fetchone()
        if row is None:
            return None
        return dict(zip(_RUN_COLUMNS, tuple(row), strict=True))

    def _last_completed_at(self, conn) -> str | None:
        try:
            return _iso(self._local(_last_completed_from(conn)))
        except Exception:  # noqa: BLE001 - e.g. a catalog without index_runs
            return None

    def snapshot(self, conn) -> dict:
        now = self._local(self.now())
        s = self.settings
        history = []
        for item in self.history():
            try:
                item["run"] = self._join(conn, item)
            except Exception:  # noqa: BLE001
                item["run"] = None
            start, end = _parse(item.get("started_at")), _parse(item.get("finished_at"))
            item["duration_s"] = (round((end - start).total_seconds(), 1)
                                  if start is not None and end is not None else None)
            history.append(item)
        with self._lock:
            catch_up_at = self._catch_up_at
        return {
            "schedule": asdict(s),
            "startup_delay_minutes": self.startup_delay_s / 60.0,
            "server_time": _iso(now),
            "timezone": now.tzname() or "",
            "utc_offset": now.strftime("%z"),
            "hostname": socket.gethostname(),
            "next_run_at": _iso(self._next_run_at()),
            "catch_up_at": _iso(catch_up_at),
            "running": self.running(),
            "history": history,
            "last_completed_at": self._last_completed_at(conn),
        }

    def status(self, conn) -> dict:
        run = self.running()
        progress = None
        if run:
            progress = next((ln for ln in reversed(run["tail"])
                             if ln.strip() and not ln.startswith("# ")), None)
        return {
            "running": run is not None,
            "enabled": self.settings.enabled,
            "next_run_at": _iso(self._next_run_at()),
            "last_completed_at": self._last_completed_at(conn),
            "trigger": run["trigger"] if run else None,
            "started_at": run["started_at"] if run else None,
            "progress": progress.strip() if progress else None,
        }
