"""foley.scans: slot arithmetic, catch-up, scan subprocesses, logs and settings."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta, tzinfo

import pytest

from foley import config, scans, sync
from foley.catalog import db
from foley.config import Config

# ----------------------------------------------------------------------------- a DST zone

ZERO, HOUR = timedelta(0), timedelta(hours=1)


def _first_sunday_on_or_after(dt: datetime) -> datetime:
    return dt + timedelta(days=(6 - dt.weekday()) % 7)


class Eastern(tzinfo):
    """US Eastern rules (2007+), PEP 495 fold-aware; Windows has no tz database for
    zoneinfo, so the tests bring their own (after the stdlib docs' USTimeZone)."""

    std = timedelta(hours=-5)

    def _range(self, year: int):
        start = _first_sunday_on_or_after(datetime(year, 3, 8, 2))
        end = _first_sunday_on_or_after(datetime(year, 11, 1, 2))
        return start, end

    def tzname(self, dt):
        return "EDT" if self.dst(dt) else "EST"

    def utcoffset(self, dt):
        return self.std + self.dst(dt)

    def dst(self, dt):
        if dt is None or dt.tzinfo is None:
            return ZERO
        start, end = self._range(dt.year)
        naive = dt.replace(tzinfo=None)
        if start + HOUR <= naive < end - HOUR:
            return HOUR
        if end - HOUR <= naive < end:
            return ZERO if dt.fold else HOUR
        if start <= naive < start + HOUR:
            return HOUR if dt.fold else ZERO
        return ZERO

    def fromutc(self, dt):
        start, end = self._range(dt.year)
        start, end = start.replace(tzinfo=self), end.replace(tzinfo=self)
        std_time = dt + self.std
        dst_time = std_time + HOUR
        if end <= dst_time < end + HOUR:
            return std_time.replace(fold=1)
        if std_time < start or dst_time >= end:
            return std_time
        return dst_time


ET = Eastern()


def et(*args, fold=0) -> datetime:
    return datetime(*args, tzinfo=ET, fold=fold)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def same_instant(a: datetime, b: datetime) -> bool:
    return a.timestamp() == b.timestamp()


# ----------------------------------------------------------------------------- slots


def test_next_run_before_at_and_after_the_slot():
    assert same_instant(scans.next_run(et(2026, 10, 15, 2, 0), "03:00", ET), et(2026, 10, 15, 3))
    # strictly after: at the slot itself, the next one is tomorrow
    assert same_instant(scans.next_run(et(2026, 10, 15, 3, 0), "03:00", ET), et(2026, 10, 16, 3))
    assert same_instant(scans.next_run(et(2026, 10, 15, 3, 0, 1), "3:00", ET),
                        et(2026, 10, 16, 3))
    assert same_instant(scans.previous_slot(et(2026, 10, 15, 3, 0), "03:00", ET),
                        et(2026, 10, 15, 3))
    assert same_instant(scans.previous_slot(et(2026, 10, 15, 2, 59), "03:00", ET),
                        et(2026, 10, 14, 3))
    # an instant given in another zone is read as the same moment
    assert same_instant(scans.next_run(utc(2026, 10, 15, 6, 0), "03:00", ET),
                        et(2026, 10, 15, 3))  # 06:00Z = 02:00 EDT
    nxt = scans.next_run(et(2026, 10, 15, 2, 0), "03:00", ET)
    assert (nxt.hour, nxt.minute) == (3, 0) and nxt.tzinfo is ET


def test_next_run_across_midnight_and_year_end():
    assert same_instant(scans.next_run(et(2026, 10, 15, 23, 59), "00:00", ET),
                        et(2026, 10, 16, 0, 0))
    assert same_instant(scans.next_run(et(2026, 10, 15, 0, 10), "23:30", ET),
                        et(2026, 10, 15, 23, 30))
    assert same_instant(scans.next_run(et(2026, 12, 31, 23, 45), "00:15", ET),
                        et(2027, 1, 1, 0, 15))
    assert same_instant(scans.previous_slot(et(2027, 1, 1, 0, 5), "23:50", ET),
                        et(2026, 12, 31, 23, 50))


def test_next_run_across_dst_keeps_wall_clock_time():
    # 2026-03-08: clocks jump 02:00 EST -> 03:00 EDT
    before = scans.next_run(et(2026, 3, 6, 12), "03:00", ET)
    after = scans.next_run(before, "03:00", ET)
    assert same_instant(before, utc(2026, 3, 7, 8)) and same_instant(after, utc(2026, 3, 8, 7))
    assert after.timestamp() - before.timestamp() == 23 * 3600
    assert (after.hour, after.minute, after.tzname()) == (3, 0, "EDT")
    # a slot inside the skipped hour runs just after it
    gap = scans.next_run(et(2026, 3, 8, 1, 0), "02:30", ET)
    assert same_instant(gap, utc(2026, 3, 8, 7, 30)) and (gap.hour, gap.minute) == (3, 30)
    assert same_instant(scans.next_run(gap, "02:30", ET), et(2026, 3, 9, 2, 30))

    # 2026-11-01: clocks fall back 02:00 EDT -> 01:00 EST
    a = scans.next_run(et(2026, 10, 30, 12), "03:00", ET)
    b = scans.next_run(a, "03:00", ET)
    assert b.timestamp() - a.timestamp() == 25 * 3600 and same_instant(b, utc(2026, 11, 1, 8))
    # a repeated wall-clock time runs once, at its first occurrence
    first = scans.next_run(et(2026, 11, 1, 0, 0), "01:30", ET)
    assert same_instant(first, utc(2026, 11, 1, 5, 30))
    again = scans.next_run(first, "01:30", ET)
    assert same_instant(again, et(2026, 11, 2, 1, 30))
    assert same_instant(again, utc(2026, 11, 2, 6, 30))
    # even when asked from inside the repeated hour (01:45 EST, second pass)
    assert same_instant(scans.next_run(et(2026, 11, 1, 1, 45, fold=1), "01:30", ET), again)


def test_local_zone_default():
    now = datetime.now().astimezone()
    nxt = scans.next_run(now, "03:00")
    assert now < nxt <= now + timedelta(hours=26)
    prev = scans.previous_slot(now, "03:00")
    assert prev <= now and now - prev <= timedelta(hours=26)
    assert nxt.tzinfo is not None
    naive = scans.next_run(datetime.now(), "03:00")  # naive = local time
    assert naive.tzinfo is not None


def test_catch_up_due():
    now = et(2026, 10, 15, 10)
    assert scans.catch_up_due(None, now, "03:00", ET)
    assert scans.catch_up_due(et(2026, 10, 15, 2, 59), now, "03:00", ET)
    assert not scans.catch_up_due(et(2026, 10, 15, 3, 20), now, "03:00", ET)
    assert not scans.catch_up_due(utc(2026, 10, 15, 7, 1), now, "03:00", ET)  # 03:01 EDT
    # before today's slot, yesterday's slot is the one that counts
    early = et(2026, 10, 15, 2)
    assert not scans.catch_up_due(et(2026, 10, 14, 3, 30), early, "03:00", ET)
    assert scans.catch_up_due(et(2026, 10, 14, 2, 30), early, "03:00", ET)


# ----------------------------------------------------------------------------- settings


def test_merge_settings_validation():
    base = scans.ScanSettings()
    assert scans.merge_settings(base, {}) == base
    s = scans.merge_settings(base, {"enabled": True, "time": "4:05", "catch_up": False})
    assert s == scans.ScanSettings(enabled=True, time="04:05", catch_up=False)
    for bad in ({"time": "24:00"}, {"time": "3pm"}, {"time": "03:60"}, {"time": 300},
                {"enabled": "yes"}, {"catch_up": 1}, {"nightly": True}):
        with pytest.raises(ValueError):
            scans.merge_settings(base, bad)
    with pytest.raises(ValueError):
        scans.merge_settings(base, ["enabled"])


def test_settings_defaults_persistence_and_corrupt_file(tmp_path):
    cfg = config.from_dict({
        "library": {"root": str(tmp_path / "lib")},
        "storage": {"data_dir": str(tmp_path / "data")},
        "schedule": {"enabled": True, "time": "2:15", "catch_up": False,
                     "startup_delay_minutes": 1.5},
    })
    got = (cfg.schedule_enabled, cfg.schedule_time, cfg.schedule_catch_up)
    assert got == (True, "02:15", False)
    assert cfg.schedule_startup_delay_minutes == 1.5
    assert scans.load_settings(cfg) == scans.ScanSettings(True, "02:15", False)

    sch = scans.ScanScheduler(cfg, tz=ET, clock=lambda: et(2026, 10, 15, 12),
                              last_completed=lambda: None)
    assert sch.startup_delay_s == 90
    new = sch.update_settings({"time": "05:45", "catch_up": True})
    assert new == scans.ScanSettings(True, "05:45", True)
    stored = json.loads((cfg.data_dir / scans.SCHEDULE_FILE).read_text(encoding="utf-8"))
    assert stored == {"enabled": True, "time": "05:45", "catch_up": True}
    assert scans.load_settings(cfg) == new  # schedule.json overrides foley.toml
    assert scans.ScanScheduler(cfg).settings == new
    with pytest.raises(ValueError):
        sch.update_settings({"time": "nope"})
    assert sch.settings == new

    (cfg.data_dir / scans.SCHEDULE_FILE).write_text("{broken", encoding="utf-8")
    assert scans.load_settings(cfg) == scans.ScanSettings(True, "02:15", False)
    (cfg.data_dir / scans.SCHEDULE_FILE).write_text('{"time": "99:99"}', encoding="utf-8")
    assert scans.load_settings(cfg) == scans.ScanSettings(True, "02:15", False)


def test_config_schedule_defaults_and_errors(tmp_path):
    base = {"library": {"root": str(tmp_path / "lib")},
            "storage": {"data_dir": str(tmp_path / "data")}}
    cfg = config.from_dict(base)
    got = (cfg.schedule_enabled, cfg.schedule_time, cfg.schedule_catch_up)
    assert got == (False, "03:00", True)
    assert cfg.schedule_startup_delay_minutes == 5.0
    for bad in ({"time": "25:00"}, {"enabled": "true"}, {"catch_up": 0},
                {"startup_delay_minutes": -1}):
        with pytest.raises(config.ConfigError):
            config.from_dict({**base, "schedule": bad})


def test_index_command(tmp_path):
    cfg = Config(library_root=tmp_path / "lib", data_dir=tmp_path / "data")
    assert scans.index_command(cfg) == [sys.executable, "-m", "foley.cli", "index"]
    cfg.source = tmp_path / "foley.toml"
    assert scans.index_command(cfg) == [sys.executable, "-m", "foley.cli", "--config",
                                        str(tmp_path / "foley.toml"), "index"]


def test_classify():
    assert scans.classify(0, "run 3: scanned 1", False) == ("ok", None)
    assert scans.classify(0, "", True)[0] == "stopped"
    locked = ("foley: another `foley index` is running (pid 12 on HOST, started x). "
              "Wait for it to finish; the lock frees itself when that process exits.")
    assert scans.classify(2, locked, False)[0] == "skipped"
    assert scans.classify(2, "foley: library root not reachable: //x", False) == (
        "failed", "foley: library root not reachable: //x")
    assert scans.classify(1, locked, False)[0] == "failed"
    assert scans.classify(3, "# header\n", False) == ("failed", None)


# ----------------------------------------------------------------------------- running


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def make(tmp_path):
    made = []

    def factory(command, now=None, last_completed=None, **cfg_kw):
        cfg = Config(library_root=tmp_path / "lib", data_dir=tmp_path / "data", **cfg_kw)
        clock = Clock(now or et(2026, 10, 15, 12))
        sch = scans.ScanScheduler(cfg, command=command, clock=clock, tz=ET,
                                  last_completed=last_completed or (lambda: None))
        sch.poll_s = 0.05
        sch.clock = clock
        made.append(sch)
        return sch

    yield factory
    for sch in made:
        sch.shutdown(stop_running=True)


def _finish(sch, timeout=60):
    assert sch.wait(timeout), "scan did not finish"
    return sch.history()[0]


def test_ok_run_writes_log_tail_and_history(make):
    sch = make(_py("import sys\nprint('scanning //lib ...')\nprint('héllo ✓')\n"
                   "print('run 7: scanned 3, added 1')\nprint('warn', file=sys.stderr)"))
    assert sch.running() is None
    assert sch.run_now("manual")
    first = _finish(sch)
    assert first["trigger"] == "manual" and first["outcome"] == "ok"
    assert first["exit_code"] == 0 and first["run_id"] == 7 and first["note"] is None
    assert first["started_at"].startswith("2026-10-15T12:00:00") and first["finished_at"]
    assert scans.LOG_NAME.fullmatch(first["log"])
    path = scans.log_path(sch.cfg, first["log"])
    text = path.read_text(encoding="utf-8")
    assert "héllo ✓" in text and "warn" in text and text.startswith("# foley scan (manual)")
    assert "# ok (exit code 0)" in text
    stored = json.loads((sch.cfg.data_dir / "logs" / "scans.json").read_text(encoding="utf-8"))
    assert stored[-1]["log"] == first["log"]
    # a second scan in the same second gets its own log
    assert sch.run_now("manual")
    second = _finish(sch)
    assert second["log"] != first["log"] and scans.log_path(sch.cfg, second["log"])
    assert [h["log"] for h in sch.history()] == [second["log"], first["log"]]


def test_tail_is_live_and_one_scan_at_a_time(make):
    sch = make(_py("import time\nfor i in range(3):\n    print(f'line {i}')\n"
                   "time.sleep(1.5)\nprint('done')"))
    assert sch.run_now("manual")
    assert not sch.run_now("manual")  # already running
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        run = sch.running()
        if run and "line 2" in run["tail"]:
            break
        time.sleep(0.05)
    else:
        pytest.fail("tail never showed the output")
    assert run["trigger"] == "manual" and run["log"].startswith("scan-")
    assert run["tail"][0].startswith("# foley scan")
    assert _finish(sch)["outcome"] == "ok"
    assert sch.running() is None


def test_failed_and_locked_runs(make):
    sch = make(_py("import sys\nprint('foley: library root not reachable: //nas/x', "
                   "file=sys.stderr)\nsys.exit(2)"))
    sch.run_now("manual")
    failed = _finish(sch)
    assert failed["outcome"] == "failed" and failed["exit_code"] == 2
    assert "not reachable" in failed["note"]

    sch._command = _py("import sys\nprint('foley: another `foley index` is running (pid 1 on "
                       "X, started y). Wait for it to finish.', file=sys.stderr)\nsys.exit(2)")
    sch.run_now("schedule")
    skipped = _finish(sch)
    assert skipped["outcome"] == "skipped" and skipped["trigger"] == "schedule"
    assert "lock" in skipped["note"]

    sch._command = _py("raise SystemExit(5)")
    sch.run_now("manual")
    assert _finish(sch)["exit_code"] == 5

    sch._command = [str(sch.cfg.data_dir / "no-such-program.exe")]
    assert sch.run_now("manual")
    nostart = _finish(sch)
    assert nostart["outcome"] == "failed" and nostart["exit_code"] is None
    assert "could not start" in nostart["note"]


def test_stop_kills_the_whole_process_tree(make):
    child = ("import subprocess, sys, time\n"
             "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
             "print(f'grandchild {p.pid}', flush=True)\n"
             "time.sleep(120)\n")
    sch = make(_py(child))
    assert not sch.stop()  # nothing running
    sch.run_now("manual")
    deadline = time.monotonic() + 30
    pid = None
    while time.monotonic() < deadline and pid is None:
        run = sch.running()
        for line in (run or {}).get("tail", []):
            if line.startswith("grandchild "):
                pid = int(line.split()[1])
        time.sleep(0.05)
    assert pid and sync._pid_alive(pid)
    t0 = time.monotonic()
    assert sch.stop()
    out = _finish(sch, timeout=30)
    assert time.monotonic() - t0 < 25
    assert out["outcome"] == "stopped" and "resumes" in out["note"]
    deadline = time.monotonic() + 10
    while sync._pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not sync._pid_alive(pid), "the analysis-worker stand-in survived the stop"


def test_log_retention_keeps_newest(make):
    sch = make(_py("print('x')"))
    logs = sch.cfg.data_dir / "logs"
    logs.mkdir(parents=True)
    old = []
    for i in range(35):
        p = logs / f"scan-20250101-{i:06d}.log"
        p.write_text("old", encoding="utf-8")
        os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))
        old.append(p)
    (logs / "notes.txt").write_text("not a scan log", encoding="utf-8")
    sch.run_now("manual")
    newest = _finish(sch)["log"]
    kept = sorted(p.name for p in logs.iterdir() if scans.LOG_NAME.fullmatch(p.name))
    assert len(kept) == scans.KEEP_LOGS and newest in kept
    assert {p.name for p in old[-29:]} <= set(kept)  # the 29 newest old ones
    assert not old[5].exists() and (logs / "notes.txt").exists()


def test_history_keeps_last_50(make):
    sch = make(_py("pass"))
    for i in range(55):
        sch._record({"trigger": "manual", "outcome": "ok", "exit_code": 0, "started_at": None,
                     "finished_at": None, "log": None, "note": str(i), "run_id": None})
    hist = sch.history()
    assert len(hist) == scans.KEEP_HISTORY and hist[0]["note"] == "54" and hist[-1]["note"] == "5"
    again = scans.ScanScheduler(sch.cfg)
    assert [h["note"] for h in again.history()] == [h["note"] for h in hist]


def test_log_path_whitelist(make, tmp_path):
    sch = make(_py("pass"))
    logs = sch.cfg.data_dir / "logs"
    logs.mkdir(parents=True)
    (logs / "scan-20261015-030000.log").write_text("ok", encoding="utf-8")
    (logs / "scans.json").write_text("[]", encoding="utf-8")
    assert scans.log_path(sch.cfg, "scan-20261015-030000.log") is not None
    for bad in ("scans.json", "../scans.json", "scan-20261015-030000.log/..",
                "..\\scan-20261015-030000.log", "scan-1.log", "scan-20261015-040000.log", None):
        assert scans.log_path(sch.cfg, bad) is None


# ----------------------------------------------------------------------------- scheduling


def test_tick_fires_at_the_slot_once(make):
    sch = make(_py("print('scheduled')"), now=et(2026, 10, 15, 2, 59), schedule_enabled=True)
    sch.plan_startup(et(2026, 10, 15, 2, 59))
    assert sch.tick(et(2026, 10, 15, 2, 59, 59)) is None
    sch.clock.now = et(2026, 10, 15, 3, 0)
    assert sch.tick(et(2026, 10, 15, 3, 0)) == "schedule"
    assert _finish(sch)["trigger"] == "schedule"
    assert sch.tick(et(2026, 10, 15, 3, 0, 30)) is None  # next slot is tomorrow
    assert same_instant(sch._next_run_at(), et(2026, 10, 16, 3))
    # disabled: nothing fires, and next_run_at is null
    sch.update_settings({"enabled": False})
    assert sch.tick(et(2026, 10, 16, 3, 0)) is None and sch._next_run_at() is None


def test_slot_while_a_scan_runs_is_skipped(make):
    sch = make(_py("import time; time.sleep(30)"), now=et(2026, 10, 15, 2, 0),
               schedule_enabled=True)
    sch.plan_startup()
    sch.run_now("manual")
    assert sch.tick(et(2026, 10, 15, 3, 0)) == "schedule"
    skipped = sch.history()[0]
    assert skipped["outcome"] == "skipped" and skipped["note"] == "a scan was already running"
    assert sch.running()["trigger"] == "manual"
    sch.stop()
    assert _finish(sch)["outcome"] == "stopped"


def test_catch_up_after_startup_delay(make):
    last = {"t": et(2026, 10, 14, 3, 20)}  # yesterday's run; today's 03:00 was missed
    sch = make(_py("print('catching up')"), now=et(2026, 10, 15, 10), schedule_enabled=True,
               last_completed=lambda: last["t"], schedule_startup_delay_minutes=5)
    at = sch.plan_startup(et(2026, 10, 15, 10))
    assert same_instant(at, et(2026, 10, 15, 10, 5))
    assert same_instant(sch._next_run_at(), at)  # the catch-up is the next run
    assert sch.tick(et(2026, 10, 15, 10, 4, 59)) is None
    assert sch.tick(et(2026, 10, 15, 10, 5)) == "catch-up"
    assert _finish(sch)["trigger"] == "catch-up"
    assert sch.tick(et(2026, 10, 15, 10, 6)) is None  # once only
    assert same_instant(sch._next_run_at(), et(2026, 10, 16, 3))

    # a scan that completed (say from the CLI) during the delay cancels the catch-up
    sch.plan_startup(et(2026, 10, 15, 10))
    last["t"] = et(2026, 10, 15, 10, 2)
    assert sch.tick(et(2026, 10, 15, 10, 5)) is None


@pytest.mark.parametrize("settings, last", [
    ({"enabled": True, "catch_up": False}, None),
    ({"enabled": False, "catch_up": True}, None),
    ({"enabled": True, "catch_up": True}, et(2026, 10, 15, 3, 30)),
])
def test_no_catch_up(make, settings, last):
    sch = make(_py("pass"), now=et(2026, 10, 15, 10), last_completed=lambda: last)
    sch.update_settings(settings)
    assert sch.plan_startup(et(2026, 10, 15, 10)) is None
    assert sch.tick(et(2026, 10, 15, 10, 30)) is None


def test_catch_up_never_scanned_and_unreadable_catalog(make):
    sch = make(_py("pass"), schedule_enabled=True)
    assert sch.plan_startup(et(2026, 10, 15, 10)) is not None

    def boom():
        raise OSError("catalog locked")

    sch2 = make(_py("pass"), schedule_enabled=True, last_completed=boom)
    assert sch2.plan_startup(et(2026, 10, 15, 10)) is None


def test_snapshot_joins_index_runs(make):
    sch = make(_py("pass"), schedule_enabled=True)
    cfg = sch.cfg
    conn = db.open_catalog(cfg)
    try:
        with db.transaction(conn):
            rid = conn.execute(
                "INSERT INTO index_runs(started_at, finished_at, status, scanned, added, "
                "changed, moved, missing, analyzed, errors) VALUES "
                "('2026-10-15T07:00:01+00:00', '2026-10-15T07:01:00+00:00', 'completed', "
                "120, 4, 1, 2, 3, 5, 0)"
            ).lastrowid
        sch._command = _py(f"print('run {rid}: scanned 120, added 4')")
        sch.run_now("manual")
        _finish(sch)
        sch._record({"trigger": "schedule", "outcome": "failed", "exit_code": 2,
                     "started_at": "2026-10-15T03:00:00-04:00",
                     "finished_at": "2026-10-15T03:02:00-04:00",
                     "log": "scan-20261015-030000.log", "note": "x", "run_id": None})
        snap = sch.snapshot(conn)
    finally:
        conn.close()
    assert snap["schedule"] == {"enabled": True, "time": "03:00", "catch_up": True}
    assert snap["server_time"].startswith("2026-10-15T12:00:00-04:00")
    assert snap["timezone"] == "EDT" and snap["utc_offset"] == "-0400" and snap["hostname"]
    assert snap["running"] is None and snap["startup_delay_minutes"] == 5.0
    assert snap["last_completed_at"] == "2026-10-15T03:01:00-04:00"  # local, not UTC
    by_window, by_id = snap["history"][0], snap["history"][1]
    assert by_id["run"]["id"] == rid and by_id["run"]["moved"] == 2
    assert by_window["run"]["id"] == rid and by_window["duration_s"] == 120.0  # window match
    assert set(by_id["run"]) >= {"scanned", "added", "changed", "moved", "missing", "errors",
                                 "analyzed", "status"}
    conn = db.open_catalog(cfg)
    try:
        st = sch.status(conn)
    finally:
        conn.close()
    assert st["running"] is False and st["enabled"] is True and st["progress"] is None
    assert st["next_run_at"].startswith("2026-10-16T03:00:00")
