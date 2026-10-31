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
