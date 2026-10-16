"""Only foley/nas_write.py may modify the library, enforced at runtime.

A sys.addaudithook hook watches file opens, filesystem-mutating calls, sqlite3
connections and subprocess launches in the process; soundfile (which writes through
libsndfile, invisible to audit hooks) is wrapped. While armed, any of these that
targets a path under the fixture library root with write intent raises PermissionError
and is recorded, unless the call stack includes foley/nas_write.py. Audit hooks cannot
be removed, so the hook is installed once and armed/disarmed through _STATE.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import audiogen as ag
import pytest
import soundfile as sf

from foley import dedupe, status, sync
from foley.catalog import db

_STATE: dict = {"armed": False, "root": None, "root_text": None, "violations": [],
                 "fold": False}


def _fold(text: str) -> str:
    """normcase, plus case folding where the library's filesystem ignores case (Windows;
    usually macOS too, where normcase does not fold)."""
    text = os.path.normcase(text)
    return text.lower() if _STATE["fold"] else text


def _case_insensitive(path: Path) -> bool:
    other = Path(str(path).swapcase())
    try:
        return str(other) != str(path) and other.exists() and os.path.samefile(other, path)
    except OSError:
        return False
_INSTALLED = False
_LOCAL = threading.local()
_REAL_SOUNDFILE = sf.SoundFile

_WRITE_FLAGS = (
    os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
    | getattr(os, "O_TEMPORARY", 0)  # Windows: delete on close
    | getattr(os, "O_SHORT_LIVED", 0)
    | getattr(os, "O_TMPFILE", 0)
)
# audit event -> indexes of the path arguments that must not be under the library
_PATH_EVENTS = {
    "os.remove": (0,),  # also os.unlink
    "os.rename": (0, 1),  # also os.replace
    "os.rmdir": (0,),
    "os.mkdir": (0,),
    "os.utime": (0,),
    "os.chmod": (0,),
    "os.chown": (0,),
    "os.chflags": (0,),
    "os.truncate": (0,),
    "os.link": (0, 1),
    "os.symlink": (0, 1),
    "os.setxattr": (0,),
    "os.removexattr": (0,),
    "shutil.move": (0, 1),
    "shutil.rmtree": (0,),
    "shutil.copyfile": (1,),
    "shutil.copytree": (1,),
    "shutil.chown": (0,),
    "shutil.make_archive": (0,),
    "shutil.unpack_archive": (1,),
    "sqlite3.connect": (0,),  # a database in the library would be written
}


def _normalize(path) -> str | None:
    if path is None or isinstance(path, int):
        return None
    try:
        text = os.fsdecode(path)
    except (TypeError, ValueError):
        return None
    if not text or text == ":memory:" or text.startswith("file:"):
        if text.startswith("file:"):
            text = text[5:].split("?", 1)[0].lstrip("/") if os.name == "nt" else text[5:]
        else:
            return None
    for prefix, repl in (("\\\\?\\UNC\\", "\\\\"), ("//?/UNC/", "//"), ("\\\\?\\", ""),
                         ("//?/", ""), ("\\\\.\\", ""), ("//./", "")):
        if text[: len(prefix)].upper() == prefix.upper():
            text = repl + text[len(prefix):]
            break
    return _fold(os.path.realpath(text))


def _under_root(path) -> bool:
    root = _STATE["root"]
    p = _normalize(path)
    return root is not None and p is not None and (p == root or p.startswith(root + os.sep))


def _mentions_root(text) -> bool:
    try:
        s = _fold(os.fsdecode(text))
    except (TypeError, ValueError):
        return False
    return _STATE["root_text"] is not None and _STATE["root_text"] in s or _under_root(text)


def _called_from_nas_write() -> bool:
    frame = sys._getframe(2)
    while frame is not None:
        name = frame.f_code.co_filename.replace("\\", "/").lower()
        if name.endswith("foley/nas_write.py"):
            return True
        frame = frame.f_back
    return False


def _violation(event: str, target) -> None:
    if _called_from_nas_write():
        return
    _STATE["violations"].append((event, os.fsdecode(target) if target is not None else None))
    log = os.environ.get("FOLEY_TEST_GUARD_LOG")
    if log:  # in an analysis worker: tell the test process
        _STATE["armed"] = False
        try:
            with open(Path(log) / f"{os.getpid()}.log", "a", encoding="utf-8") as f:
                f.write(f"{event} {target!r}\n")
        finally:
            _STATE["armed"] = True
    raise PermissionError(f"library write outside nas_write.py: {event} {target!r}")


def _subprocess_targets(args: tuple) -> list:
    """Library paths a process launch could write: anything not passed as an input."""
    _exe, argv, cwd = (tuple(args) + (None, None, None))[:3]
    hits = []
    if cwd is not None and _under_root(cwd):
        hits.append(cwd)
    if isinstance(argv, bytes):
        argv = os.fsdecode(argv)
    if isinstance(argv, str):  # Windows audits the joined command line
        try:
            argv = [t.strip('"') for t in shlex.split(argv, posix=os.name != "nt")]
        except ValueError:
            return [argv] if _mentions_root(argv) else hits
    if argv:
        argv = list(argv)
        for i, arg in enumerate(argv):
            if isinstance(arg, (str, bytes, os.PathLike)) and _mentions_root(arg):
                if i > 0 and os.fsdecode(argv[i - 1]) == "-i":
                    continue  # ffmpeg-style input: read only
                hits.append(arg)
    return hits


def _hook(event: str, args: tuple) -> None:
    if not _STATE["armed"] or getattr(_LOCAL, "busy", False):
        return
    _LOCAL.busy = True  # realpath etc. below must not re-enter the hook
    try:
        if event == "open":
            path, mode, flags = (tuple(args) + (None, None, None))[:3]
            writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & _WRITE_FLAGS
            )
            targets = [path] if writes else []
        elif event in _PATH_EVENTS:
            targets = [args[i] for i in _PATH_EVENTS[event] if i < len(args)]
        elif event == "subprocess.Popen":
            hits = _subprocess_targets(args)
            if hits:
                _violation(event, hits[0])
            return
        elif event == "os.system":
            if args and _mentions_root(args[0]):
                _violation(event, args[0])
            return
        else:
            return
        for target in targets:
            if _under_root(target):
                _violation(event, target)
    finally:
        _LOCAL.busy = False


class _GuardedSoundFile(_REAL_SOUNDFILE):
    """soundfile writes through libsndfile, which audit hooks cannot see."""

    def __init__(self, file, mode="r", *args, **kwargs):
        if _STATE["armed"] and any(c in (mode or "r") for c in "wx+") and (
            isinstance(file, (str, bytes, os.PathLike)) and _under_root(file)
        ):
            _violation("soundfile.SoundFile", file)
        super().__init__(file, mode, *args, **kwargs)


def _arm(library: Path) -> None:
    global _INSTALLED
    if not _INSTALLED:
        sys.addaudithook(_hook)
        _INSTALLED = True
    _STATE["fold"] = _case_insensitive(Path(library))
    root = _fold(os.path.realpath(library))
    _STATE.update(root=root, root_text=_fold(str(library)), violations=[], armed=True)


def _worker_guard() -> None:
    """sync.WORKER_INIT_HOOK target: the same guard inside each analysis worker."""
    sf.SoundFile = _GuardedSoundFile
    _arm(Path(os.environ["FOLEY_TEST_GUARD_ROOT"]))


def _try_append(path: str) -> str:
    try:
        with open(path, "ab"):
            pass
    except PermissionError as e:
        return f"blocked: {e}"
    return "written"


def _worker_logs(log_dir: Path) -> list[str]:
    return [line for p in log_dir.glob("*.log") for line in p.read_text().splitlines()]


@pytest.fixture
def worker_guard(library, monkeypatch, tmp_path) -> Path:
    log_dir = tmp_path / "guard-logs"
    log_dir.mkdir()
    monkeypatch.setenv("FOLEY_TEST_GUARD_ROOT", str(library))
    monkeypatch.setenv("FOLEY_TEST_GUARD_LOG", str(log_dir))
    monkeypatch.setattr(sync, "WORKER_INIT_HOOK", "test_readonly:_worker_guard")
    return log_dir


def test_guard_works_inside_spawned_workers(worker_guard, library):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    target = str(library / "sfx" / "click.wav")
    with ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("spawn"),
                             initializer=_worker_guard) as pool:
        assert pool.submit(_try_append, target).result().startswith("blocked")
    assert len(_worker_logs(worker_guard)) == 1


@pytest.fixture
def guard(library, monkeypatch):
    monkeypatch.setattr(sf, "SoundFile", _GuardedSoundFile)
    _arm(library)
    try:
        yield _STATE
    finally:
        _STATE["armed"] = False


def _snapshot(root: Path) -> dict:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            out[os.path.join(dirpath, d)] = "dir"
        for f in filenames:
            p = os.path.join(dirpath, f)
            st = os.stat(p)
            with open(p, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            out[p] = (st.st_size, st.st_mtime_ns, digest)
    return out


def test_hook_catches_library_writes(guard, library, tmp_path):
    target = library / "sfx/click.wav"
    with open(target, "rb") as f:  # reading is fine
        f.read(4)
    sf.read(target)
    (tmp_path / "elsewhere.txt").write_text("ok")  # writing outside the library is fine
    sf.write(tmp_path / "elsewhere.wav", ag.click(), ag.SR)
    sqlite3.connect(tmp_path / "elsewhere.db").close()
    subprocess.run([sys.executable, "-c", "pass", "-i", str(target)], check=True)

    attempts = [
        lambda: open(target, "ab"),
        lambda: open(target, "r+b"),
        lambda: open(library / "new.txt", "w"),
        lambda: open("\\\\?\\" + str(target), "ab"),
        lambda: os.open(target, os.O_WRONLY),
        lambda: os.remove(target),
        lambda: os.rename(target, library / "moved.wav"),
        lambda: os.replace(target, tmp_path / "out.wav"),
        lambda: os.utime(target, None),
        lambda: os.mkdir(library / "newdir"),
        lambda: os.chmod(target, 0o444),
        lambda: (library / "sfx/click.wav").unlink(),
        lambda: sqlite3.connect(library / "catalog.db"),
        lambda: sqlite3.connect("\\\\?\\" + str(library / "x.db")),
        lambda: sf.write(library / "sfx/new.wav", ag.click(), ag.SR),
        lambda: sf.SoundFile(target, "r+"),
        lambda: subprocess.run([sys.executable, "-c", "pass", "-i", str(target),
                                str(library / "out.wav")]),
        lambda: subprocess.run([sys.executable, "-c", "pass"], cwd=library),
    ]
    if guard["fold"]:  # the same file under another case (Windows, usually macOS)
        attempts.append(lambda: open(str(target).upper(), "ab"))
    if hasattr(os, "O_TEMPORARY"):
        attempts.append(lambda: os.open(target, os.O_RDONLY | os.O_TEMPORARY))
    for attempt in attempts:
        with pytest.raises(PermissionError, match="outside nas_write.py"):
            attempt()
    assert len(guard["violations"]) == len(attempts)
    guard["armed"] = False
    assert target.exists() and not (library / "catalog.db").exists()

    # The same write is allowed when nas_write.py is on the stack.
    guard["armed"] = True
    import foley.nas_write as nas_write

    code = compile(
        "def append(p):\n    with open(p, 'ab') as f:\n        f.write(b'')\n",
        nas_write.__file__, "exec",
    )
    scope: dict = {}
    exec(code, scope)
    scope["append"](target)
    assert len(guard["violations"]) == len(attempts)


def _exercise_browse(cfg, ids: list[int], mp3: int) -> None:
    """Every `foley browse` endpoint that touches the library or writes anything."""
    import http.client
    import json

    from foley import browse

    server = browse.make_server(cfg, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def call(path, method="GET", body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=120)
        try:
            c.request(method, path, body=json.dumps(body) if body is not None else None,
                      headers=headers or {})
            r = c.getresponse()
            data = r.read()
            assert r.status in (200, 206), (path, r.status, data[:200])
            return data
        finally:
            c.close()

    try:
        for path in ("/", "/api/status", "/api/tree", "/api/list", "/api/list?q=metal",
                     f"/api/asset/{ids[0]}", f"/api/audio/{ids[0]}",
                     f"/api/audio/{mp3}?transcode=1", f"/api/image/{ids[1]}",
                     f"/api/peaks/{ids[0]}", f"/api/peaks/{mp3}?n=200", "/api/scans"):
            call(path)
        call(f"/api/audio/{ids[0]}", headers={"Range": "bytes=10-99"})
        call("/api/shortlist", "POST", {"id": ids[0], "starred": True, "note": "x"})
        call("/api/shortlist")
        call("/api/shortlist", "POST", {"id": ids[0], "starred": False})
    finally:
        server.shutdown()
        server.server_close()


def _use_real_or_fake(monkeypatch, tmp_path) -> bool:
    from test_sync import install_fakes, siblings_ready

    if siblings_ready(tmp_path) is None:
        return True
    install_fakes(monkeypatch)
    return False


@pytest.mark.parametrize("workers", [1, 2])
def test_every_entry_point_leaves_the_library_alone(
    guard, worker_guard, cfg, library, monkeypatch, tmp_path, workers
):
    guard["armed"] = False
    real = _use_real_or_fake(monkeypatch, tmp_path)
    from _fake_models import FakeFactory

    from foley import models

    fake_models = FakeFactory()
    models.set_factory(fake_models)  # the model stages read the library too
    if workers > 1 and not real:
        pytest.skip("parallel analysis needs the real pipeline modules (fakes stay in-process)")
    project = tmp_path / "game"
    project.mkdir()
    (project / "project.godot").write_text("")
    before = _snapshot(library)
    guard["armed"] = True

    summary = sync.run_index(cfg, workers=workers)
    assert summary.analyzed > 0 and not summary.interrupted
    assert any(c[0] == "tag" for c in fake_models.calls)
    assert any(c[0] == "embed" for c in fake_models.calls)
    if real:
        assert summary.errors == 0
    sync.run_index(cfg, force=True, workers=workers)  # every stage again
    conn = db.open_catalog(cfg)
    try:
        rep = dedupe.report(cfg, conn)
        assert rep["exact_groups"] == 1
        st = status.catalog_status(cfg, conn)
        assert st["assets"]["active"] == summary.analyzed
        status.staleness_note(conn)
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM assets WHERE ext IN ('.wav', '.flac') ORDER BY id LIMIT 3")]
        mp3 = conn.execute("SELECT id FROM assets WHERE ext='.mp3'").fetchone()[0]
        if real:
            from foley import images, importer, mcp_server, search

            assert Path(images.render_overview(cfg, conn, ids[0], force=True)).exists()
            assert Path(images.render_comparison(cfg, conn, ids[:3])).exists()
            importer.import_to_project(cfg, conn, ids[0], project, "res://sfx/a.wav",
                                       target_lufs=-20.0)
            importer.import_to_project(cfg, conn, mp3, project, "res://music/b.mp3",
                                       target_lufs=-16.0)
            search.search(conn, "metal")
            search.describe(conn, ids[0])
            mcp_server.set_config(cfg)
            try:
                mcp_server.tool_search_audio("scratch")
                mcp_server.tool_describe(ids[1])
                mcp_server.tool_render_comparison(ids[:2])
                mcp_server.tool_import_to_project(ids[1], str(project), "res://sfx/c.wav",
                                                  target_lufs=-18.0)
                mcp_server.tool_catalog_status()
            finally:
                mcp_server.close()
                mcp_server.set_config(None)
            assert (project / "sfx" / "c.wav").exists()
            _exercise_browse(cfg, ids, mp3)
        conn.execute(
            "UPDATE assets SET status='missing', missing_since='2000-01-01T00:00:00' "
            "WHERE rel_path='sfx/click.wav'"
        )
    finally:
        conn.close()
    assert sync.prune(cfg) == ["sfx/click.wav"]
    assert sync.prune(cfg, dry_run=False) == ["sfx/click.wav"]

    guard["armed"] = False
    assert guard["violations"] == []
    assert _worker_logs(worker_guard) == []
    assert _snapshot(library) == before
