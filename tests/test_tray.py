"""`foley tray`: the child-process supervisor, icon state and the browse side of the
clean stop (closing stdin). None of this needs pystray or a desktop."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from foley import tray
from foley.config import Config

# ---------------------------------------------------------------------------- helpers


def test_browse_command_asks_for_a_clean_stop(tmp_path):
    cfg = Config(library_root=tmp_path, data_dir=tmp_path / "data",
                 source=tmp_path / "foley.toml")
    cmd = tray.browse_command(cfg, tmp_path / "browse.log")
    assert cmd[1:3] == ["-m", "foley.cli"]
    assert cmd[3:5] == ["--config", str(tmp_path / "foley.toml")]
    assert cmd[5:] == ["browse", "--no-open", "--stop-on-stdin-eof",
                       "--log-file", str(tmp_path / "browse.log")]
    assert not Path(cmd[0]).name.lower().startswith("pythonw")


def test_browse_command_without_a_config_file(tmp_path):
    cfg = Config(library_root=tmp_path, data_dir=tmp_path / "data")
    assert "--config" not in tray.browse_command(cfg, "x.log")


def test_console_python_replaces_pythonw(tmp_path, monkeypatch):
    (tmp_path / "pythonw.exe").touch()
    (tmp_path / "python.exe").touch()
    monkeypatch.setattr(sys, "executable", str(tmp_path / "pythonw.exe"))
    assert tray._console_python() == str(tmp_path / "python.exe")


@pytest.mark.parametrize("host,url", [
    ("127.0.0.1", "http://127.0.0.1:8765/"),
    ("0.0.0.0", "http://127.0.0.1:8765/"),
    ("::", "http://127.0.0.1:8765/"),
    ("::1", "http://[::1]:8765/"),
    ("192.168.1.5", "http://192.168.1.5:8765/"),
])
def test_local_url(tmp_path, host, url):
    cfg = Config(library_root=tmp_path, data_dir=tmp_path, browse_host=host)
    assert tray.local_url(cfg) == url


def test_tray_log_sits_beside_the_server_log(tmp_path):
    assert tray.tray_log_path(tmp_path / "logs" / "browse.log") == tmp_path / "logs" / "tray.log"


# ------------------------------------------------------------------------- supervisor


class FakeProc:
    def __init__(self):
        self.pid = 4242
        self.returncode = None
        self.stdin = self
        self.closed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.closed:
            self.returncode = 0
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode

    def close(self):  # stdin.close(): a well-behaved browse exits
        self.closed = True


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def fake():
    procs: list[FakeProc] = []

    def popen(cmd, **kwargs):
        assert kwargs["stdin"] == subprocess.PIPE
        procs.append(FakeProc())
        return procs[-1]

    clock = Clock()
    sup = tray.Supervisor(["browse"], restart_delay=5, max_restarts=2, window=600,
                          clock=clock, popen=popen)
    return sup, procs, clock


def test_supervisor_restarts_a_crashed_child_after_a_delay(fake):
    sup, procs, clock = fake
    sup.start()
    assert sup.poll() == "running" and sup.pid == 4242
    procs[0].returncode = 1
    assert sup.poll() == "restarting"
    assert sup.last_exit == 1
    clock.now = 4
    assert sup.poll() == "restarting" and len(procs) == 1
    clock.now = 5
    assert sup.poll() == "running" and len(procs) == 2


def test_supervisor_gives_up_after_too_many_crashes(fake):
    sup, procs, clock = fake
    sup.start()
    for _ in range(2):
        procs[-1].returncode = 2
        assert sup.poll() == "restarting"
        clock.now += 5
        assert sup.poll() == "running"
    procs[-1].returncode = 2
    assert sup.poll() == "failed"
    clock.now += 1000
    assert sup.poll() == "failed" and len(procs) == 3
    sup.start()  # the menu's Start server clears it
    assert sup.poll() == "running" and len(procs) == 4


def test_old_crashes_age_out_of_the_window(fake):
    sup, procs, clock = fake
    sup.start()
    for _ in range(5):
        procs[-1].returncode = 1
        assert sup.poll() == "restarting"
        clock.now += 700  # each crash is outside the previous one's window
        assert sup.poll() == "running"


def test_stop_closes_stdin_and_does_not_restart(fake):
    sup, procs, clock = fake
    sup.start()
    sup.stop()
    assert procs[0].closed and sup.last_exit == 0
    clock.now += 100
    assert sup.poll() == "stopped" and len(procs) == 1


def test_restart_starts_a_new_child(fake):
    sup, procs, _ = fake
    sup.start()
    sup.restart()
    assert procs[0].closed and len(procs) == 2 and sup.poll() == "running"


def test_a_child_that_cannot_start_counts_as_a_crash(tmp_path):
    clock = Clock()
    sup = tray.Supervisor([str(tmp_path / "missing.exe")], max_restarts=1, clock=clock)
    sup.start()
    assert sup.poll() == "restarting"
    clock.now += 5
    assert sup.poll() == "restarting"  # the retry failed too
    assert sup.poll() == "failed"


def test_supervisor_stops_a_real_child_cleanly():
    sup = tray.Supervisor([sys.executable, "-c", "import sys; sys.stdin.read()"],
                          stop_timeout=30)
    sup.start()
    assert sup.poll() == "running"
    sup.stop()
    assert sup.last_exit == 0
    assert sup.poll() == "stopped"


def test_supervisor_kills_a_child_that_ignores_stdin():
    sup = tray.Supervisor([sys.executable, "-c", "import time; time.sleep(120)"],
                          stop_timeout=1)
    sup.start()
    began = time.monotonic()
    sup.stop()
    assert time.monotonic() - began < 60
    assert sup.last_exit not in (None, 0)
    assert sup.poll() == "stopped"


# ------------------------------------------------------------------------- icon state


def test_display_state():
    scanning = {"scan": {"running": True}}
    idle = {"scan": {"running": False}}
    assert tray.display_state("running", idle) == "running"
    assert tray.display_state("running", scanning) == "scanning"
    assert tray.display_state("running", None) == "starting"  # not answering yet
    assert tray.display_state("restarting", None) == "starting"
    assert tray.display_state("stopped", None) == "stopped"
    assert tray.display_state("failed", None) == "failed"
    assert tray.display_state("running", idle, busy="stopping") == "stopping"


def test_tooltip_fits_windows_limit():
    status = {"scan": {"running": True, "progress": "x" * 500}}
    text = tray.tooltip("scanning", "http://127.0.0.1:8765/", status)
    assert len(text) <= 127 and text.startswith("Foley: scanning")
    assert tray.tooltip("running", "http://h/") == "Foley: running at http://h/"
    for state in tray.STATES:
        assert tray.tooltip(state, "http://h/").startswith("Foley:")


def test_render_icon_with_and_without_a_logo(tmp_path):
    from PIL import Image

    for state in tray.STATES:
        img = tray.render_icon(state)
        assert img.mode == "RGBA" and img.size == (64, 64)
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (200, 100), (255, 0, 0, 255)).save(logo)
    img = tray.render_icon("running", logo)
    assert img.getpixel((32, 32))[:3] == (255, 0, 0)
    assert img.getpixel((51, 51))[:3] == tray._COLORS["running"]  # the status dot
    assert tray.render_icon("running", tmp_path / "missing.png").size == (64, 64)


def test_one_tray_per_data_dir(tmp_path):
    first = tray._InstanceLock(tmp_path / "tray.lock")
    second = tray._InstanceLock(tmp_path / "tray.lock")
    assert first.acquire()
    try:
        assert not second.acquire()
    finally:
        first.release()
    assert second.acquire()
    second.release()


# ----------------------------------------------------------------- browse side


def test_browse_stops_cleanly_when_stdin_closes(cfg, tmp_path):
    toml = tmp_path / "foley.toml"
    toml.write_text(
        f"[library]\nroot = '{cfg.library_root.as_posix()}'\n"
        f"[storage]\ndata_dir = '{cfg.data_dir.as_posix()}'\n",
        encoding="utf-8")
    log_file = tmp_path / "logs" / "browse.log"
    proc = subprocess.Popen(
        [sys.executable, "-m", "foley.cli", "--config", str(toml), "browse", "--no-open",
         "--port", "0", "--stop-on-stdin-eof", "--log-file", str(log_file)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and proc.poll() is None:
            if log_file.exists() and "listening on" in log_file.read_text(encoding="utf-8"):
                break
            time.sleep(0.2)
        assert proc.poll() is None, proc.stderr.read().decode()
        proc.stdin.close()
        assert proc.wait(30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert "stopping (standard input closed)" in log_file.read_text(encoding="utf-8")


# --------------------------------------------------- one process per program (Windows)


@pytest.fixture
def venv(tmp_path, monkeypatch):
    """A fake Windows venv: launchers in venv/Scripts, the real interpreters in base/."""
    scripts, base = tmp_path / "venv" / "Scripts", tmp_path / "base"
    for d in (scripts, base):
        d.mkdir(parents=True)
        (d / "python.exe").touch()
        (d / "pythonw.exe").touch()
    monkeypatch.setattr(tray, "_is_windows", lambda: True)
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(sys, "base_prefix", str(base))
    monkeypatch.setattr(sys, "_base_executable", str(base / "python.exe"), raising=False)
    monkeypatch.setattr(sys, "executable", str(scripts / "pythonw.exe"))
    monkeypatch.delenv(tray.RELAUNCHED_ENV, raising=False)
    return scripts, base


def test_direct_interpreter_skips_the_venv_launcher(venv):
    scripts, base = venv
    assert tray.direct_interpreter(scripts / "python.exe") == (
        str(base / "python.exe"), str(scripts / "python.exe"))
    assert tray.direct_interpreter(scripts / "pythonw.exe") == (
        str(base / "pythonw.exe"), str(scripts / "pythonw.exe"))


def test_direct_interpreter_falls_back(venv, monkeypatch, tmp_path):
    scripts, base = venv
    exe = str(scripts / "python.exe")
    (base / "pythonw.exe").unlink()
    assert tray.direct_interpreter(scripts / "pythonw.exe") == (
        str(scripts / "pythonw.exe"), None)  # no base interpreter of that name
    monkeypatch.setattr(sys, "base_prefix", sys.prefix)  # not in a venv
    assert tray.direct_interpreter(exe) == (exe, None)
    monkeypatch.setattr(sys, "base_prefix", str(base))
    monkeypatch.setattr(tray, "_is_windows", lambda: False)
    assert tray.direct_interpreter(exe) == (exe, None)


def test_browse_runs_on_the_base_console_interpreter(venv, tmp_path, monkeypatch):
    scripts, base = venv
    monkeypatch.setenv(tray.RELAUNCHED_ENV, "1")
    cfg = Config(library_root=tmp_path, data_dir=tmp_path / "data")
    assert tray.browse_command(cfg, "b.log")[0] == str(base / "python.exe")
    env = tray.browse_env()
    assert env[tray.LAUNCHER_ENV] == str(scripts / "python.exe")
    assert tray.RELAUNCHED_ENV not in env


def test_browse_env_without_a_venv(tmp_path, monkeypatch):
    monkeypatch.setattr(tray, "_is_windows", lambda: False)
    monkeypatch.setenv(tray.LAUNCHER_ENV, "stale")
    assert tray.LAUNCHER_ENV not in tray.browse_env()
    cfg = Config(library_root=tmp_path, data_dir=tmp_path / "data")
    assert tray.browse_command(cfg, "b.log")[0] == tray._console_python()


def test_supervisor_passes_the_environment():
    seen = []

    def popen(cmd, **kwargs):
        seen.append(kwargs["env"])
        return FakeProc()

    tray.Supervisor(["x"], popen=popen, env=lambda: {"A": "1"}).start()
    tray.Supervisor(["x"], popen=popen).start()
    assert seen == [{"A": "1"}, None]


def test_relaunch_decision(venv, monkeypatch):
    assert tray.should_relaunch()
    monkeypatch.setenv(tray.RELAUNCHED_ENV, "1")  # the copy itself
    assert not tray.should_relaunch()
    monkeypatch.delenv(tray.RELAUNCHED_ENV)
    monkeypatch.setattr(sys, "base_prefix", sys.prefix)  # no venv, no launcher
    assert not tray.should_relaunch()
    monkeypatch.setattr(sys, "base_prefix", str(venv[1]))
    monkeypatch.setattr(tray, "_is_windows", lambda: False)
    assert not tray.should_relaunch()


def test_relaunch_starts_a_detached_copy_on_base_pythonw(venv, tmp_path):
    scripts, base = venv
    calls = []

    def popen(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return FakeProc()

    cfg = Config(library_root=tmp_path, data_dir=tmp_path / "data",
                 source=tmp_path / "foley.toml")
    assert tray.relaunch(cfg, tmp_path / "browse.log", popen=popen) == 4242
    (cmd, kwargs), = calls
    assert cmd == [str(base / "pythonw.exe"), "-m", "foley.cli", "--config",
                   str(tmp_path / "foley.toml"), "tray", "--log-file",
                   str(tmp_path / "browse.log")]
    assert kwargs["env"][tray.LAUNCHER_ENV] == str(scripts / "pythonw.exe")
    assert kwargs["env"][tray.RELAUNCHED_ENV] == "1"
    assert kwargs["close_fds"] and kwargs["stdin"] == subprocess.DEVNULL


def test_relaunch_retries_when_the_job_forbids_breakaway(venv, tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000, raising=False)
    flags = []

    def popen(cmd, **kwargs):
        flags.append(kwargs["creationflags"])
        if len(flags) == 1:
            raise PermissionError("access denied")
        return FakeProc()

    cfg = Config(library_root=tmp_path, data_dir=tmp_path / "data")
    tray.relaunch(cfg, "b.log", popen=popen)
    assert flags[0] & 0x01000000 and not flags[1] & 0x01000000


def _no_pystray(monkeypatch):
    monkeypatch.setitem(sys.modules, "pystray", None)  # import fails: run() returns 2


def test_run_with_the_lock_held_exits_without_relaunching(tmp_path, monkeypatch):
    cfg = Config(library_root=tmp_path, data_dir=tmp_path / "data")
    monkeypatch.setattr(tray, "configure_logging", lambda path: None)
    monkeypatch.setattr(tray, "should_relaunch", lambda: True)
    monkeypatch.setattr(tray, "relaunch", lambda *a, **k: pytest.fail("relaunched"))
    holder = tray._InstanceLock(cfg.data_dir / tray.LOCK_FILE)
    assert holder.acquire()
    try:
        assert tray.run(cfg, tmp_path / "b.log") == 0
    finally:
        holder.release()


def test_run_relaunches_and_leaves_the_lock_to_the_copy(tmp_path, monkeypatch):
    cfg = Config(library_root=tmp_path, data_dir=tmp_path / "data")
    monkeypatch.setattr(tray, "configure_logging", lambda path: None)
    monkeypatch.setattr(tray, "should_relaunch", lambda: True)
    lock_path = cfg.data_dir / tray.LOCK_FILE

    def relaunch(cfg_, log_file):
        copy = tray._InstanceLock(lock_path)  # the copy can take the lock now
        assert copy.acquire()
        copy.release()
        return 99

    monkeypatch.setattr(tray, "relaunch", relaunch)
    assert tray.run(cfg, tmp_path / "b.log") == 0


def test_run_carries_on_itself_when_the_copy_cannot_start(tmp_path, monkeypatch):
    cfg = Config(library_root=tmp_path, data_dir=tmp_path / "data")
    monkeypatch.setattr(tray, "configure_logging", lambda path: None)
    monkeypatch.setattr(tray, "should_relaunch", lambda: True)

    def relaunch(*_):
        raise OSError("nope")

    monkeypatch.setattr(tray, "relaunch", relaunch)
    _no_pystray(monkeypatch)
    assert tray.run(cfg, tmp_path / "b.log") == 2  # got as far as loading pystray
