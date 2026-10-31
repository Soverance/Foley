"""`foley tray`: a notification-area (system tray) icon that runs and controls `foley browse`.

CONTRACT:

    run(cfg, log_file=None) -> int
        Takes the single-instance lock (data_dir/tray.lock; returns 3 if another tray
        holds it), starts `foley browse` as a child process (Supervisor) and shows the
        icon until Quit, which stops the child cleanly and returns 0. Returns 2 when
        pystray cannot load. The menu: Open Foley (the default item, on click), Scan now
        or Stop scan, Restart server, Stop server or Start server, Open log, Quit.
        Restart starts a fresh Python process, so it picks up code changes in the
        checkout. Changes to dependencies or to this module need the tray itself
        restarted. pystray is imported only here (a dependency on Windows only; elsewhere
        install it and have a desktop tray). `log_file` is the server's log (default
        default_log_file(cfg)); the tray logs to TRAY_LOG beside it.
    browse_command(cfg, log_file) -> list[str]
        [python, "-m", "foley.cli", ("--config", cfg.source), "browse", "--no-open",
         "--stop-on-stdin-eof", "--log-file", log_file]. python is the console
        interpreter beside sys.executable (python.exe for pythonw.exe) so the child has a
        real stdin; it runs with no window.
    default_log_file(cfg) -> Path        data_dir/logs/browse.log
    tray_log_path(log_file=None) -> Path TRAY_LOG beside `log_file`, else in the default
                                         data dir's logs/
    configure_logging(path) -> None      the "foley" logger to `path` (size-rotated) and
                                         the console when there is one; replaces itself
    local_url(cfg) -> str                the server as this machine reaches it (a wildcard
                                         bind -> 127.0.0.1)

    class Supervisor(command, restart_delay=5.0, max_restarts=5, window=600.0,
                     stop_timeout=20.0, clock=time.monotonic, popen=subprocess.Popen)
        `command` is a list or a zero-arg callable returning one. Thread-safe.
        start()      start the child if it is not running; clears the crash count
        stop()       close the child's stdin (browse then stops its scan and exits), wait
                     stop_timeout seconds, then kill its process tree; no restart follows
        restart()    stop() then start()
        poll() -> "running" | "stopped" | "restarting" | "failed"
                     Call it periodically. An exit nobody asked for restarts the child
                     after restart_delay, at most max_restarts times in `window` seconds,
                     then stays "failed" until start().
        pid, last_exit

    Icon state (pure, tested directly):
    STATES = starting, running, scanning, stopping, stopped, failed
    display_state(proc_state, status: dict | None, busy=None) -> state
        `status` is GET /api/status (None while the server does not answer); `busy` is
        the action in progress ("stopping" / "starting").
    tooltip(state, url, status=None) -> str      at most 127 characters (Windows' limit)
    render_icon(state, logo=None, size=64) -> PIL.Image (RGBA)
        logo.png from [browse] brand_dir when present, else a drawn mark, with a status
        dot in the corner.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections import deque
from collections.abc import Callable
from pathlib import Path

from foley.config import Config, default_data_dir
from foley.scans import _popen_kwargs, kill_tree

log = logging.getLogger("foley.tray")

TRAY_LOG = "tray.log"
LOCK_FILE = "tray.lock"
POLL_S = 2.0
STATUS_TIMEOUT_S = 3.0
LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUPS = 3
TOOLTIP_MAX = 127

STATES = ("starting", "running", "scanning", "stopping", "stopped", "failed")
_COLORS = {
    "starting": (210, 153, 34),
    "stopping": (210, 153, 34),
    "running": (63, 185, 80),
    "scanning": (47, 129, 247),
    "stopped": (139, 148, 158),
    "failed": (248, 81, 73),
}


# =============================================================================== paths


def default_log_file(cfg: Config) -> Path:
    return cfg.data_dir / "logs" / "browse.log"


def tray_log_path(log_file: str | os.PathLike | None = None) -> Path:
    if log_file:
        return Path(log_file).with_name(TRAY_LOG)
    return default_data_dir() / "logs" / TRAY_LOG


def configure_logging(path: str | os.PathLike) -> None:
    logger = logging.getLogger("foley")
    for handler in list(logger.handlers):
        if getattr(handler, "_foley_tray", False):
            logger.removeHandler(handler)
            handler.close()
    added: list[logging.Handler] = []
    if sys.stdout is not None:  # None under pythonw.exe
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter("%(message)s"))
        added.append(console)
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
        rotating.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        added.append(rotating)
    except OSError:
        pass
    if not added:
        added.append(logging.NullHandler())
    for handler in added:
        handler._foley_tray = True
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _console_python() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        console = exe.with_name("python.exe")
        if console.exists():
            return str(console)
    return str(exe)


def browse_command(cfg: Config, log_file: str | os.PathLike) -> list[str]:
    cmd = [_console_python(), "-m", "foley.cli"]
    if cfg.source is not None:
        cmd += ["--config", str(cfg.source)]
    return cmd + ["browse", "--no-open", "--stop-on-stdin-eof", "--log-file", str(log_file)]


def local_url(cfg: Config) -> str:
    host = cfg.browse_host.strip().strip("[]")
    if host in ("", "0.0.0.0", "::"):
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{cfg.browse_port}/"


# ========================================================================== supervisor


class Supervisor:
    def __init__(self, command: list[str] | Callable[[], list[str]],
                 restart_delay: float = 5.0, max_restarts: int = 5, window: float = 600.0,
                 stop_timeout: float = 20.0, clock: Callable[[], float] = time.monotonic,
                 popen: Callable[..., subprocess.Popen] = subprocess.Popen):
        self._command = command
        self.restart_delay = restart_delay
        self.max_restarts = max_restarts
        self.window = window
        self.stop_timeout = stop_timeout
        self._clock = clock
        self._popen = popen
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._wanted = False
        self._failed = False
        self._restart_at: float | None = None
        self._crashes: deque[float] = deque()
        self.last_exit: int | None = None

    @property
    def pid(self) -> int | None:
        proc = self._proc
        return proc.pid if proc is not None and proc.poll() is None else None

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _spawn(self) -> None:
        cmd = self._command() if callable(self._command) else list(self._command)
        try:
            self._proc = self._popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, **_popen_kwargs())
        except OSError as e:
            log.error("foley tray: cannot start %s: %s", cmd[0], e)
            self._proc = None
            return
        log.info("foley tray: started foley browse (pid %s)", self._proc.pid)

    def start(self) -> None:
        with self._lock:
            self._wanted = True
            self._failed = False
            self._restart_at = None
            self._crashes.clear()
            if not self._alive():
                self._spawn()

    def stop(self) -> None:
        with self._lock:
            self._wanted = False
            self._restart_at = None
            proc = self._proc
            if proc is None or proc.poll() is not None:
                return
            log.info("foley tray: stopping foley browse (pid %s)", proc.pid)
            try:
                proc.stdin.close()  # browse sees end of file and shuts down cleanly
            except (OSError, AttributeError):
                pass
            try:
                proc.wait(self.stop_timeout)
            except subprocess.TimeoutExpired:
                log.warning("foley tray: foley browse did not stop within %.0f s; killing it",
                            self.stop_timeout)
                kill_tree(proc)
            self.last_exit = proc.poll()
            log.info("foley tray: foley browse stopped (exit code %s)", self.last_exit)

    def restart(self) -> None:
        with self._lock:
            self.stop()
            self.start()

    def poll(self) -> str:
        with self._lock:
            if self._alive():
                return "running"
            if not self._wanted:
                return "stopped"
            if self._failed:
                return "failed"
            now = self._clock()
            if self._restart_at is None:  # an exit nobody asked for, seen just now
                self.last_exit = self._proc.poll() if self._proc is not None else None
                log.warning("foley tray: foley browse exited unexpectedly (exit code %s)",
                            self.last_exit)
                while self._crashes and now - self._crashes[0] > self.window:
                    self._crashes.popleft()
                self._crashes.append(now)
                if len(self._crashes) > self.max_restarts:
                    self._failed = True
                    log.error("foley tray: foley browse stopped %d times in %.0f minutes; "
                              "not restarting it again (see the browse log)",
                              len(self._crashes), self.window / 60)
                    return "failed"
                self._restart_at = now + self.restart_delay
                return "restarting"
            if now >= self._restart_at:
                self._restart_at = None
                self._spawn()
                return "running" if self._alive() else "restarting"
            return "restarting"


# ========================================================================= icon state


def display_state(proc_state: str, status: dict | None, busy: str | None = None) -> str:
    if busy in ("stopping", "starting"):
        return busy
    if proc_state in ("stopped", "failed"):
        return proc_state
    if proc_state == "restarting" or status is None:
        return "starting"
    scan = status.get("scan") or {}
    return "scanning" if scan.get("running") else "running"


def tooltip(state: str, url: str, status: dict | None = None) -> str:
    if state == "running":
        text = f"Foley: running at {url}"
    elif state == "scanning":
        progress = ((status or {}).get("scan") or {}).get("progress") or ""
        text = "Foley: scanning the library" + (f" ({progress})" if progress else "")
    elif state == "starting":
        text = "Foley: starting..."
    elif state == "stopping":
        text = "Foley: stopping..."
    elif state == "failed":
        text = "Foley: the server keeps stopping; see the log"
    else:
        text = "Foley: stopped"
    if len(text) > TOOLTIP_MAX:
        text = text[: TOOLTIP_MAX - 3] + "..."
    return text


def render_icon(state: str, logo: str | os.PathLike | None = None, size: int = 64):
    from PIL import Image, ImageDraw

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    base = None
    if logo is not None:
        try:
            with Image.open(logo) as img:
                base = img.convert("RGBA")
        except (OSError, ValueError):
            base = None
    draw = ImageDraw.Draw(canvas)
    if base is not None:
        base.thumbnail((size, size), Image.LANCZOS)
        canvas.alpha_composite(base, ((size - base.width) // 2, (size - base.height) // 2))
    else:  # a rounded tile with waveform bars
        draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=size // 5,
                               fill=(31, 36, 48, 255))
        heights = (0.30, 0.55, 0.80, 0.50, 0.65, 0.35)
        bar = size / (2 * len(heights) + 1)
        for i, h in enumerate(heights):
            x0 = bar * (2 * i + 1)
            half = h * size * 0.38
            draw.rounded_rectangle((x0, size / 2 - half, x0 + bar, size / 2 + half),
                                   radius=bar / 2, fill=(230, 237, 243, 255))
    r = size * 0.2
    cx = cy = size - r - 1
    draw.ellipse((cx - r - 2, cy - r - 2, cx + r + 2, cy + r + 2), fill=(13, 17, 23, 255))
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*_COLORS.get(state, (139, 148, 158)),
                                                         255))
    return canvas


# ============================================================================== locking


class _InstanceLock:
    """An OS lock on data_dir/tray.lock, held for the tray's lifetime."""

    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)  # closing drops the lock on every platform
            self._fd = None


# ================================================================================ app


def _open_path(path: Path) -> None:
    if hasattr(os, "startfile"):
        os.startfile(path)  # noqa: S606 - Windows: open with the associated app
    else:
        webbrowser.open(path.as_uri())


class _TrayApp:
    def __init__(self, cfg: Config, log_file: Path, pystray):
        self.cfg = cfg
        self.log_file = log_file
        self.url = local_url(cfg)
        self.supervisor = Supervisor(lambda: browse_command(cfg, log_file))
        self.pystray = pystray
        self.state = "starting"
        self.status: dict | None = None
        self.busy: str | None = None
        self._quit = threading.Event()
        self._wake = threading.Event()
        brand = cfg.browse_brand_dir
        logo = brand / "logo.png" if brand is not None else None
        self.logo = logo if logo is not None and logo.is_file() else None
        self._images: dict[str, object] = {}
        self.icon = pystray.Icon("foley", self._image(self.state),
                                 tooltip(self.state, self.url), menu=self._menu())

    # ------------------------------------------------------------------ display

    def _image(self, state: str):
        if state not in self._images:
            self._images[state] = render_icon(state, self.logo)
        return self._images[state]

    def _menu(self):
        item, menu = self.pystray.MenuItem, self.pystray.Menu
        up = lambda _: self.state in ("running", "scanning")  # noqa: E731
        idle = lambda _: self.busy is None  # noqa: E731
        return menu(
            item("Open Foley", self._on_open, default=True, enabled=up),
            item("Scan now", self._on_scan, enabled=lambda _: self.state == "running",
                 visible=lambda _: self.state != "scanning"),
            item("Stop scan", self._on_stop_scan, visible=lambda _: self.state == "scanning"),
            menu.SEPARATOR,
            item("Restart server", self._on_restart, enabled=idle),
            item("Stop server", self._on_stop, enabled=idle,
                 visible=lambda _: self.state not in ("stopped", "failed")),
            item("Start server", self._on_start, enabled=idle,
                 visible=lambda _: self.state in ("stopped", "failed")),
            item("Open log", self._on_log),
            menu.SEPARATOR,
            item("Quit", self._on_quit),
        )

    def _fetch_status(self) -> dict | None:
        try:
            with urllib.request.urlopen(self.url + "api/status",
                                        timeout=STATUS_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (OSError, ValueError):
            return None

    def refresh(self) -> None:
        if self.busy:  # the supervisor's lock is held by the action: show it, don't wait
            proc_state, self.status = "running", None
        else:
            proc_state = self.supervisor.poll()
            self.status = self._fetch_status() if proc_state == "running" else None
        state = display_state(proc_state, self.status, self.busy)
        previous, self.state = self.state, state
        self.icon.title = tooltip(state, self.url, self.status)
        if state != previous:
            self.icon.icon = self._image(state)
            self.icon.update_menu()
            if state == "failed":
                self._notify("The Foley server keeps stopping. Open the log to see why.")

    def _notify(self, message: str) -> None:
        try:
            self.icon.notify(message, "Foley")
        except Exception:  # noqa: BLE001 - notifications are a nicety
            log.debug("notification failed", exc_info=True)

    def _monitor(self) -> None:
        while not self._quit.is_set():
            try:
                self.refresh()
            except Exception:  # noqa: BLE001 - the icon must keep running
                log.exception("foley tray: status refresh failed")
            self._wake.wait(POLL_S)
            self._wake.clear()

    # ------------------------------------------------------------------ actions

    def _in_background(self, busy: str | None, work: Callable[[], None]) -> None:
        def run() -> None:
            self.busy = busy
            self._wake.set()
            try:
                work()
            except Exception:  # noqa: BLE001
                log.exception("foley tray: action failed")
            finally:
                self.busy = None
                self._wake.set()

        threading.Thread(target=run, daemon=True).start()

    def _post(self, path: str) -> tuple[int, dict]:
        req = urllib.request.Request(self.url + path, data=b"", method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8") or "{}")
            except ValueError:
                body = {}
            return e.code, body
        except OSError as e:
            return 0, {"error": str(e)}

    def _on_open(self, *_) -> None:
        webbrowser.open(self.url)

    def _on_scan(self, *_) -> None:
        def work() -> None:
            code, body = self._post("api/scans/run")
            if code == 200:
                log.info("foley tray: manual scan started")
                self._notify("Scan started.")
            elif code == 409:
                self._notify("A scan is already running.")
            else:
                self._notify(f"Could not start a scan: {body.get('error', code)}")

        self._in_background(None, work)

    def _on_stop_scan(self, *_) -> None:
        def work() -> None:
            code, body = self._post("api/scans/stop")
            if code != 200:
                self._notify(f"Could not stop the scan: {body.get('error', code)}")

        self._in_background(None, work)

    def _on_restart(self, *_) -> None:
        self._in_background("stopping", self.supervisor.restart)

    def _on_stop(self, *_) -> None:
        self._in_background("stopping", self.supervisor.stop)

    def _on_start(self, *_) -> None:
        self._in_background("starting", self.supervisor.start)

    def _on_log(self, *_) -> None:
        if self.log_file.exists():
            _open_path(self.log_file)
        else:
            self._notify(f"No log yet at {self.log_file}")

    def _on_quit(self, *_) -> None:
        def work() -> None:
            self._quit.set()
            self.supervisor.stop()
            self.icon.stop()

        self._in_background("stopping", work)

    # ------------------------------------------------------------------ main loop

    def run(self) -> None:
        def setup(icon) -> None:
            icon.visible = True
            self.supervisor.start()
            threading.Thread(target=self._monitor, name="foley-tray-monitor",
                             daemon=True).start()

        try:
            self.icon.run(setup=setup)
        finally:
            self._quit.set()
            self.supervisor.stop()  # also when the icon loop ends some other way


def run(cfg: Config, log_file: str | os.PathLike | None = None) -> int:
    log_path = Path(log_file) if log_file else default_log_file(cfg)
    configure_logging(tray_log_path(log_path))
    lock = _InstanceLock(cfg.data_dir / LOCK_FILE)
    if not lock.acquire():
        log.error("foley tray: another foley tray is already running for %s", cfg.data_dir)
        return 3
    try:
        try:
            import pystray
        except Exception as e:  # noqa: BLE001 - ImportError, or no display backend
            log.error("foley tray: cannot load pystray (%s). It is installed with Foley on "
                      "Windows; elsewhere install it, or run `foley browse` instead.", e)
            return 2
        from foley import __version__

        log.info("foley tray %s starting (pid %s, config %s, server log %s)",
                 __version__, os.getpid(), cfg.source, log_path)
        _TrayApp(cfg, log_path, pystray).run()
        log.info("foley tray: quit")
        return 0
    finally:
        lock.release()
