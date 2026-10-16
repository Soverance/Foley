"""`foley browse`: a local web UI for exploring and auditioning the whole library.

CONTRACT:

    make_server(cfg, host="127.0.0.1", port=8765, verbose=False) -> BrowseServer
        A bound (not yet serving) stdlib ThreadingHTTPServer; port 0 picks a free port.
        Opens catalog.db once up front (creating/migrating it) so a broken catalog fails
        here, not on the first request. `server.url` is "http://<host>:<port>/" and
        `server.app` the BrowseApp holding the caches and the shortlist.
    serve(cfg, host="127.0.0.1", port=8765, open_browser=True, out=None,
          verbose=False) -> None
        make_server + serve_forever until Ctrl+C. Prints the URL through `out`
        (default: flushed print), with a loud warning when `host` is not loopback;
        opens the default browser unless told not to; `verbose` logs every request.
    is_loopback(host: str) -> bool

    Helpers (pure, tested directly):
    compute_peaks(samples (frames, channels) float32, n) -> (n, mins, maxs, peak)
    stream_peaks(path, n) -> (duration_s, compute_peaks result) | None
    wav_bytes(samples, sample_rate) -> bytes        16-bit PCM WAV, >2 channels -> mono
    parse_range(header, size) -> (start, end) | None   raises _Unsatisfiable (-> 416)

    Shortlist (Scott's stars; also read by the MCP tool `get_shortlist`):
    SHORTLIST_FILE = "shortlist.json", stored in cfg.data_dir as
        {content_hash: {"rel_path", "note", "added_at"}}. Keyed by content_hash like the
        ledger, so a star survives moves and renames. Not in ledger.db (no schema change).
    shortlist_path(cfg) -> Path
    load_shortlist(cfg) -> dict          {} when the file does not exist; ValueError if corrupt
    shortlist_items(conn, data: dict) -> list[dict]
        Newest first: {id, path, content_hash, note, added_at, caption, duration_s, lufs,
        status}. Each hash resolves to its active copy (the starred rel_path if it is still
        there, else the first other active copy, else a missing row); id/status are None
        when no catalog row has that hash any more (path is then the starred rel_path).

    HTTP API (JSON unless noted; errors are {"error": "..."} with a 4xx/5xx status):
        GET  /                    index.html from browse_ui/ (placeholder page if missing)
        GET  /static/<name>       whitelisted files of browse_ui/ (index.html, app.js, app.css)
        GET  /api/status          {library_root, assets: {active, missing, error, pending},
                                   note, has_tags, has_embeddings, shortlist_count,
                                   loudness_target_lufs}
        GET  /api/tree?path=      {path, files, folders: [{name, path, count}]}: immediate
                                  children of a folder ("" = root); counts are active assets,
                                  recursive; `files` = active assets directly in the folder;
                                  folders sorted case-insensitively. 404 for an unknown folder.
        GET  /api/list            {total, offset, limit, note, results: [row]}; see LISTING
        GET  /api/asset/<id>      search.describe(conn, id) plus {starred, star_note,
                                  audio_url, image_url, peaks_url, image_plot_x}
        GET  /api/audio/<id>      the file's bytes (read-only), Accept-Ranges/Range -> 206,
                                  416 for an unsatisfiable range. ?transcode=1 (automatic for
                                  .aif/.aiff) serves 16-bit PCM WAV decoded with
                                  decode.decode_path, built in memory (Range works too).
        GET  /api/image/<id>      image/png from images.render_overview (renders serialized
                                  behind one lock); 404 unknown/missing file, 422 undecodable
        GET  /api/peaks/<id>?n=1600
                                  {duration_s, channels, n, peak, min: [[n]...], max: [[n]...]}
                                  per channel (1 or 2; >2 channels are downmixed to mono),
                                  -1..1 rounded to 4 dp; n is 16..8000 and is lowered to the
                                  frame count for tiny files. Read block-wise with soundfile
                                  (decode.decode_path when libsndfile cannot read the file).
                                  Cached in data_dir/peaks/ keyed by content_hash + n
                                  (+ PEAKS_VERSION).
        GET  /api/shortlist       {"items": shortlist_items(...)}
        POST /api/shortlist       body {"id", "starred", "note"?} -> {"starred", "note"};
                                  unstar removes the entry; starring again with a note
                                  updates the note, without "note" keeps the old one.

    LISTING (/api/list): params folder, q, min_duration, max_duration, min_lufs, max_lufs,
        max_attack, is_music (1 = music, 0 = not known to be music: 0 or untagged), ext
        ("wav" or ".wav", comma-separated for several), starred=1, sort (relevance | name |
        -name | duration | -duration | lufs | -lufs | path; relevance needs q, default
        relevance with q else name), offset (>= 0), limit (1..1000, default 200).
        Bad values -> 400. Row: {id, path, name, folder, ext, caption, duration_s, lufs,
        true_peak_db, attack_s, sample_rate, channels, tags (top 3 labels), duplicates
        (other active copies), starred, score (None without q)}. Sorting puts unknown
        values last. Without q every file is listed (duplicates not collapsed). With q,
        candidates come from search.rank (the search.search ranking) restricted to the
        folder via SearchFilters.folder, at most SEARCH_CANDIDATES of them (the note says
        so when the cap is hit), then filtered, and exact duplicates collapse to the
        best-ranked copy.

    Security: requests whose Host header is not localhost / a loopback IP (or the
    explicitly bound host) get 403 (DNS rebinding); a POST whose Origin is present and is
    not this server gets 403. The client never sends filesystem paths: audio is located by
    asset id, and `folder` is only compared as a path prefix (in memory, or as an escaped
    SQL LIKE prefix through SearchFilters.folder). Nothing here writes to the library;
    writes go to cfg.data_dir only (image cache + `images` rows via images.render_overview,
    peaks cache, shortlist.json).

DESIGN NOTES:

    One sqlite connection per thread: each handler opens db.open_catalog lazily and
    closes it when its (keep-alive) connection ends; nothing shares a connection.
    Listing and tree read an in-memory snapshot of every active asset (id, path and the
    sortable features), rebuilt when a cheap catalog fingerprint changes (row counts,
    latest index run, catalog file identity; checked at most every CHECK_INTERVAL_S).
    Per-sort orders, the folder tree, ranked search candidates and filtered results are
    cached per snapshot, so paging is a slice. Captions and tags are fetched per page.
    Decodes (peaks, transcode) are limited to DECODE_SLOTS at a time; transcoded WAVs
    are kept in a small in-memory LRU so Range requests do not decode again. Peaks
    stream the file in blocks, so a 30-minute 96 kHz recording costs I/O, not a
    gigabyte of RAM.
    Importing this module has no side effects.
"""

from __future__ import annotations

import io
import ipaddress
import json
import math
import os
import socket
import sqlite3
import sys
import threading
import time
import traceback
import wave
from collections import Counter, OrderedDict, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import numpy as np

from foley.catalog import db
from foley.config import Config

UI_DIR = Path(__file__).with_name("browse_ui")
STATIC_FILES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
}
AUDIO_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
}
TRANSCODE_EXTS = frozenset({".aif", ".aiff"})
SHORTLIST_FILE = "shortlist.json"
PEAKS_VERSION = "1"
PEAKS_DEFAULT_N = 1600
PEAKS_MIN_N, PEAKS_MAX_N = 16, 8000
SEARCH_CANDIDATES = 2000
LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT = 200, 1000
CHECK_INTERVAL_S = 1.0
DECODE_SLOTS = 2
TRANSCODE_CACHE_BYTES = 256 * 1024 * 1024
SORTS = ("relevance", "name", "-name", "duration", "-duration", "lufs", "-lufs", "path")
_STREAM_CHUNK = 256 * 1024
_PEAK_BLOCK = 1 << 18  # frames per soundfile read when streaming peaks
_MAX_BODY = 64 * 1024
_LOOPBACK_NAMES = frozenset({"localhost"})


def is_loopback(host: str) -> bool:
    name = (host or "").strip().strip("[]").lower().rstrip(".")
    if name in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


# =============================================================================== shortlist


def shortlist_path(cfg: Config) -> Path:
    return cfg.data_dir / SHORTLIST_FILE


def _read_shortlist_file(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError as e:
        raise ValueError(f"{path} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{path} must hold a JSON object keyed by content_hash")
    return {
        str(h): {
            "rel_path": v.get("rel_path"),
            "note": v.get("note"),
            "added_at": v.get("added_at"),
        }
        for h, v in data.items()
        if isinstance(v, dict)
    }


def load_shortlist(cfg: Config) -> dict:
    return _read_shortlist_file(shortlist_path(cfg))


def shortlist_items(conn: sqlite3.Connection, data: dict) -> list[dict]:
    items = []
    for h, entry in data.items():
        rows = conn.execute(
            "SELECT a.id, a.rel_path, a.status, f.duration_s, f.lufs_integrated "
            "FROM assets a LEFT JOIN features f ON f.asset_id = a.id "
            "WHERE a.content_hash = ? ORDER BY (a.status != 'active'), "
            "(a.rel_path != ?), a.rel_path",
            (h, entry.get("rel_path") or ""),
        ).fetchall()
        row = rows[0] if rows else None
        items.append(
            {
                "id": row["id"] if row else None,
                "path": row["rel_path"] if row else entry.get("rel_path"),
                "content_hash": h,
                "note": entry.get("note"),
                "added_at": entry.get("added_at"),
                "caption": _captions(conn, [row["id"]]).get(row["id"]) if row else None,
                "duration_s": row["duration_s"] if row else None,
                "lufs": row["lufs_integrated"] if row else None,
                "status": row["status"] if row else None,
            }
        )
    items.sort(key=lambda it: it["added_at"] or "", reverse=True)
    return items


class Shortlist:
    """Thread-safe view of shortlist.json; reloads when the file changes on disk."""

    def __init__(self, path: Path):
        self.path = path
        self.version = 0
        self._lock = threading.Lock()
        self._data: dict = {}
        self._stamp: tuple | None = None

    def _stat(self) -> tuple | None:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _refresh(self) -> None:
        stamp = self._stat()
        if stamp == self._stamp:
            return
        try:
            self._data = _read_shortlist_file(self.path)
        except ValueError as e:
            print(f"foley browse: ignoring unreadable shortlist: {e}", file=sys.stderr)
            self._data = {}
        self._stamp = stamp
        self.version += 1

    def data(self) -> dict:
        with self._lock:
            self._refresh()
            return dict(self._data)

    def current(self) -> tuple[int, frozenset]:
        """(version, starred content hashes)."""
        with self._lock:
            self._refresh()
            return self.version, frozenset(self._data)

    def get(self, content_hash: str | None) -> dict | None:
        if not content_hash:
            return None
        with self._lock:
            self._refresh()
            entry = self._data.get(content_hash)
            return dict(entry) if entry else None

    def set(self, content_hash: str, rel_path: str, starred: bool, note=...) -> dict | None:
        """Star (or re-note) / unstar. `note=...` keeps an existing note."""
        with self._lock:
            self._refresh()
            data = dict(self._data)
            if starred:
                old = data.get(content_hash) or {}
                data[content_hash] = {
                    "rel_path": rel_path,
                    "note": old.get("note") if note is ... else note,
                    "added_at": old.get("added_at") or db.utcnow(),
                }
            else:
                data.pop(content_hash, None)
            self._write(data)
            self._data = data
            self._stamp = self._stat()
            self.version += 1
            entry = data.get(content_hash)
            return dict(entry) if entry else None

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)


# =============================================================================== snapshot


class _Entry:
    __slots__ = (
        "id", "path", "name", "folder", "ext", "content_hash", "duration_s", "lufs",
        "true_peak_db", "attack_s", "sample_rate", "channels", "is_music", "name_key",
    )

    def __init__(self, row):
        self.id = row[0]
        self.path = row[1]
        folder, _, name = self.path.rpartition("/")
        self.folder = folder
        self.name = name
        self.name_key = name.casefold()
        self.ext = (row[2] or "").lower()
        self.content_hash = row[3]
        self.duration_s = row[4]
        self.lufs = row[5]
        self.true_peak_db = row[6]
        self.attack_s = row[7]
        self.sample_rate = row[8]
        self.channels = row[9]
        self.is_music = row[10]


def _num_key(value, reverse: bool):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return (1, 0.0)
    return (0, -value if reverse else value)


_SORT_KEYS = {
    "name": lambda e: (e.name_key, e.path),
    "path": lambda e: e.path,
    "duration": lambda e: (_num_key(e.duration_s, False), e.name_key, e.path),
    "-duration": lambda e: (_num_key(e.duration_s, True), e.name_key, e.path),
    "lufs": lambda e: (_num_key(e.lufs, False), e.name_key, e.path),
    "-lufs": lambda e: (_num_key(e.lufs, True), e.name_key, e.path),
}


class _Snapshot:
    """Every active asset with its sortable features; immutable once built."""

    def __init__(self, conn: sqlite3.Connection, version: int):
        self.version = version
        rows = conn.execute(
            "SELECT a.id, a.rel_path, a.ext, a.content_hash, f.duration_s, f.lufs_integrated, "
            "f.true_peak_db, f.attack_s, f.sample_rate, f.channels, f.is_music "
            "FROM assets a LEFT JOIN features f ON f.asset_id = a.id "
            "WHERE a.status = 'active'"
        ).fetchall()
        self.entries = [_Entry(r) for r in rows]
        self.by_id = {e.id: e for e in self.entries}
        self.copies = Counter(e.content_hash for e in self.entries if e.content_hash)
        self._orders: dict[str, list[_Entry]] = {}
        self._tree: tuple | None = None
        self._lock = threading.Lock()

    def duplicates(self, e: _Entry) -> int:
        return self.copies[e.content_hash] - 1 if e.content_hash else 0

    def order(self, sort: str) -> list[_Entry]:
        """Entries in `sort` order (any SORTS value but relevance), cached."""
        with self._lock:
            got = self._orders.get(sort)
            if got is None:
                if sort == "-name":
                    got = sorted(self.entries, key=_SORT_KEYS["name"], reverse=True)
                else:
                    got = sorted(self.entries, key=_SORT_KEYS[sort])
                self._orders[sort] = got
            return got

    def tree(self) -> tuple[dict, Counter, dict]:
        """(recursive counts per folder, direct file counts, children names per folder)."""
        with self._lock:
            if self._tree is None:
                direct = Counter(e.folder for e in self.entries)
                counts: Counter = Counter()
                children: dict[str, set] = defaultdict(set)
                for folder, n in direct.items():
                    counts[""] += n
                    if not folder:
                        continue
                    parent = ""
                    for part in folder.split("/"):
                        path = f"{parent}/{part}" if parent else part
                        counts[path] += n
                        children[parent].add(part)
                        parent = path
                self._tree = (counts, direct, children)
            return self._tree


def _fingerprint(conn: sqlite3.Connection, catalog_path: Path) -> tuple:
    try:
        st = catalog_path.stat()
        ident = (st.st_dev, st.st_ino)
    except OSError:
        ident = None
    row = conn.execute(
        "SELECT (SELECT count(*) FROM assets), (SELECT coalesce(max(id), 0) FROM assets), "
        "(SELECT count(*) FROM assets WHERE status = 'active'), "
        "(SELECT count(*) FROM captions), (SELECT count(*) FROM tags), "
        "(SELECT id || ':' || status || ':' || coalesce(finished_at, '') FROM index_runs "
        " ORDER BY id DESC LIMIT 1)"
    ).fetchone()
    return (ident, *tuple(row))


class _LRU:
    def __init__(self, max_items: int = 32, max_bytes: int | None = None):
        self.max_items = max_items
        self.max_bytes = max_bytes
        self._d: OrderedDict = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
                return self._d[key]
            return None

    def put(self, key, value) -> None:
        with self._lock:
            if key in self._d:
                self._bytes -= _size(self._d.pop(key))
            self._d[key] = value
            self._bytes += _size(value)
            while len(self._d) > self.max_items or (
                self.max_bytes is not None and self._bytes > self.max_bytes and len(self._d) > 1
            ):
                _, old = self._d.popitem(last=False)
                self._bytes -= _size(old)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()
            self._bytes = 0


def _size(value) -> int:
    return len(value) if isinstance(value, (bytes, bytearray)) else 0


# =============================================================================== helpers


def _captions(conn: sqlite3.Connection, ids: list[int]) -> dict[int, str]:
    """Preferred caption per asset (a model caption over the template, as search does)."""
    out: dict[int, str] = {}
    if not ids:
        return out
    for aid, _model, text in conn.execute(
        "SELECT asset_id, model, text FROM captions WHERE asset_id IN "
        "(SELECT value FROM json_each(?)) ORDER BY asset_id, (model = 'template-v1'), model",
        (json.dumps(ids),),
    ):
        out.setdefault(aid, text)
    return out


def _tags(conn: sqlite3.Connection, ids: list[int], top: int = 3) -> dict[int, list[str]]:
    out: dict[int, list[str]] = defaultdict(list)
    if not ids:
        return out
    for aid, label in conn.execute(
        "SELECT asset_id, label FROM tags WHERE asset_id IN (SELECT value FROM json_each(?)) "
        "ORDER BY asset_id, score DESC",
        (json.dumps(ids),),
    ):
        labels = out[aid]
        if len(labels) < top and label not in labels:
            labels.append(label)
    return out


def _clean(value):
    """Non-finite floats -> None so browsers' JSON.parse accepts the payload."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _dumps(obj) -> bytes:
    try:
        text = json.dumps(obj, ensure_ascii=False, default=str, allow_nan=False)
    except ValueError:
        text = json.dumps(_clean(obj), ensure_ascii=False, default=str)
    return text.encode("utf-8")


def _staleness_note(conn: sqlite3.Connection) -> str | None:
    from foley import status

    try:
        return status.staleness_note(conn)
    except NotImplementedError:
        return None


def _norm_folder(value: str | None) -> str:
    return (value or "").replace("\\", "/").strip().strip("/")


class _Peaks:
    """Min/max per bin over `frames` frames, fed block by block (bin = frame * n // frames)."""

    def __init__(self, frames: int, channels: int, n: int):
        self.frames = max(1, int(frames))
        self.channels = 1 if channels > 2 else max(1, int(channels))
        self.n = max(1, min(int(n), self.frames))
        self.lo = np.full((self.n, self.channels), np.inf, dtype=np.float32)
        self.hi = np.full((self.n, self.channels), -np.inf, dtype=np.float32)
        self.pos = 0

    def add(self, block: np.ndarray) -> None:
        x = block if block.ndim == 2 else block[:, None]
        if x.shape[1] > 2:
            x = x.mean(axis=1, dtype=np.float32)[:, None]
        x = x[: max(0, self.frames - self.pos)]
        if not len(x):
            return
        if not np.isfinite(x).all():
            x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
        idx = (np.arange(self.pos, self.pos + len(x), dtype=np.int64) * self.n) // self.frames
        starts = np.concatenate(([0], np.flatnonzero(np.diff(idx)) + 1))
        bins = idx[starts]
        self.lo[bins] = np.minimum(self.lo[bins], np.minimum.reduceat(x, starts, axis=0))
        self.hi[bins] = np.maximum(self.hi[bins], np.maximum.reduceat(x, starts, axis=0))
        self.pos += len(x)

    def result(self) -> tuple[int, list, list, float]:
        lo = np.where(np.isfinite(self.lo), self.lo, 0.0).astype(np.float64)
        hi = np.where(np.isfinite(self.hi), self.hi, 0.0).astype(np.float64)
        peak = min(float(max(np.max(np.abs(lo)), np.max(np.abs(hi)))), 1.0)
        lo = np.clip(np.round(lo, 4), -1.0, 1.0)
        hi = np.clip(np.round(hi, 4), -1.0, 1.0)
        return self.n, lo.T.tolist(), hi.T.tolist(), round(peak, 4)


def compute_peaks(samples: np.ndarray, n: int) -> tuple[int, list, list, float]:
    """(n, mins, maxs, peak) for float32 (frames, channels) audio; >2 channels -> mono."""
    x = samples if samples.ndim == 2 else samples[:, None]
    acc = _Peaks(x.shape[0], x.shape[1], n)
    acc.add(x)
    return acc.result()


def stream_peaks(path: Path, n: int) -> tuple[float, tuple] | None:
    """(duration_s, compute_peaks-style result) read block-wise with soundfile, so a long
    file never sits in memory; None when libsndfile cannot read it (caller decodes)."""
    import soundfile as sf

    try:
        f = sf.SoundFile(str(path))  # mode "r": read-only
    except Exception:  # noqa: BLE001 - libsndfile cannot read it: let decode_path try
        return None
    with f:
        frames, rate = int(f.frames), int(f.samplerate)
        if frames <= 0 or rate <= 0 or f.channels < 1:
            return None
        acc = _Peaks(frames, int(f.channels), n)
        while True:
            block = f.read(_PEAK_BLOCK, dtype="float32", always_2d=True)
            if not len(block):
                break
            acc.add(block)
    if acc.pos == 0:
        return None
    return frames / rate, acc.result()


def wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
    """16-bit PCM WAV of float32 (frames, channels) audio; >2 channels -> mono."""
    x = samples if samples.ndim == 2 else samples[:, None]
    if x.shape[1] > 2:
        x = x.mean(axis=1, dtype=np.float32)[:, None]
    pcm = np.clip(np.rint(x * 32767.0), -32768, 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(int(x.shape[1]))
        w.setsampwidth(2)
        w.setframerate(int(sample_rate))
        w.writeframes(np.ascontiguousarray(pcm).tobytes())
    return buf.getvalue()


class _Unsatisfiable(Exception):
    pass


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """(start, end inclusive) for a single `bytes=` range, None to serve the whole body.
    Raises _Unsatisfiable when the range lies beyond the end."""
    if not header:
        return None
    unit, _, spec = header.strip().partition("=")
    if unit.strip().lower() != "bytes" or "," in spec:
        return None
    first, dash, last = spec.strip().partition("-")
    if not dash:
        return None
    first, last = first.strip(), last.strip()
    if (first and not first.isdigit()) or (last and not last.isdigit()):
        return None
    if not first:
        if not last:
            return None
        n = int(last)
        if n == 0 or size == 0:
            raise _Unsatisfiable
        return max(0, size - n), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if last and end < start:
        return None
    if start >= size:
        raise _Unsatisfiable
    return start, min(end, size - 1)


class _HTTPError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# =============================================================================== app


class BrowseApp:
    """State shared by every request thread: config, caches, shortlist, locks."""

    def __init__(self, cfg: Config, host: str = "127.0.0.1"):
        self.cfg = cfg
        bound = (host or "").strip().strip("[]").lower()
        self.allowed_hosts = {bound} if bound and bound not in ("0.0.0.0", "::", "") else set()
        self.shortlist = Shortlist(shortlist_path(cfg))
        self.peaks_dir = cfg.data_dir / "peaks"
        self.check_interval_s = CHECK_INTERVAL_S
        self.render_lock = threading.Lock()
        self.decode_slots = threading.BoundedSemaphore(DECODE_SLOTS)
        self.transcodes = _LRU(max_items=16, max_bytes=TRANSCODE_CACHE_BYTES)
        self._ranked = _LRU(max_items=32)
        self._filtered = _LRU(max_items=32)
        self._snap: _Snapshot | None = None
        self._snap_print = None
        self._snap_checked = 0.0
        self._snap_version = 0
        self._snap_lock = threading.Lock()

    # ----------------------------------------------------------------- connections

    def connect(self) -> sqlite3.Connection:
        return db.open_catalog(self.cfg)

    def invalidate(self) -> None:
        """Forget the snapshot and derived caches (the next request rebuilds them)."""
        with self._snap_lock:
            self._snap = None
            self._snap_print = None
        self._ranked.clear()
        self._filtered.clear()

    def snapshot(self, conn: sqlite3.Connection) -> _Snapshot:
        with self._snap_lock:
            now = time.monotonic()
            if self._snap is not None and now - self._snap_checked < self.check_interval_s:
                return self._snap
            fp = _fingerprint(conn, self.cfg.catalog_path)
            self._snap_checked = now
            if self._snap is None or fp != self._snap_print:
                self._snap_version += 1
                self._snap = _Snapshot(conn, self._snap_version)
                self._snap_print = fp
                self._ranked.clear()
                self._filtered.clear()
            return self._snap

    # ----------------------------------------------------------------- status / tree

    def status(self, conn: sqlite3.Connection) -> dict:
        counts = {s: 0 for s in ("active", "missing", "error", "pending")}
        for st, n in conn.execute("SELECT status, count(*) FROM assets GROUP BY status"):
            counts[st] = n

        def has(table: str) -> bool:
            return bool(conn.execute(f"SELECT EXISTS (SELECT 1 FROM {table})").fetchone()[0])

        return {
            "library_root": db.get_meta(conn, "library_root") or str(self.cfg.library_root),
            "assets": counts,
            "note": _staleness_note(conn),
            "has_tags": has("tags"),
            "has_embeddings": has("embeddings"),
            "shortlist_count": len(self.shortlist.current()[1]),
            "loudness_target_lufs": float(self.cfg.browse_loudness_target_lufs),
        }

    def tree(self, conn: sqlite3.Connection, path: str) -> dict:
        path = _norm_folder(path)
        counts, direct, children = self.snapshot(conn).tree()
        if path and path not in counts:
            raise _HTTPError(404, f"no such folder: {path}")
        names = sorted(children.get(path, ()), key=lambda n: (n.casefold(), n))
        folders = []
        for name in names:
            child = f"{path}/{name}" if path else name
            folders.append({"name": name, "path": child, "count": counts[child]})
        return {"path": path, "files": direct.get(path, 0), "folders": folders}

    # ----------------------------------------------------------------- listing

    def listing(self, conn: sqlite3.Connection, qs: dict) -> dict:
        from foley import search

        p = _ListParams(qs)
        snap = self.snapshot(conn)
        star_version, starred = self.shortlist.current()
        terms = tuple(search.query_terms(p.q)) if p.q else ()
        sort = p.sort or ("relevance" if terms else "name")
        if sort == "relevance" and not terms:
            sort = "name"
        key = (snap.version, star_version if p.starred else None, terms, sort, *p.filter_key())
        cached = self._filtered.get(key)
        if cached is None:
            pred = p.predicate(starred)
            if terms:
                cached = self._search_matches(conn, snap, terms, p.folder, pred, sort)
            else:
                cached = ([(e, None) for e in snap.order(sort) if pred(e)], False)
            self._filtered.put(key, cached)
        rows, capped = cached
        notes = [n for n in (_staleness_note(conn),) if n]
        if capped:
            notes.append(
                f"showing the best {SEARCH_CANDIDATES} keyword matches; add words or "
                "pick a folder to narrow the search"
            )
        page = rows[p.offset : p.offset + p.limit]
        ids = [e.id for e, _ in page]
        caps = _captions(conn, ids)
        tags = _tags(conn, ids)
        results = [
            {
                "id": e.id,
                "path": e.path,
                "name": e.name,
                "folder": e.folder,
                "ext": e.ext,
                "caption": caps.get(e.id),
                "duration_s": e.duration_s,
                "lufs": e.lufs,
                "true_peak_db": e.true_peak_db,
                "attack_s": e.attack_s,
                "sample_rate": e.sample_rate,
                "channels": e.channels,
                "tags": tags.get(e.id, []),
                "duplicates": snap.duplicates(e),
                "starred": bool(e.content_hash and e.content_hash in starred),
                "score": score,
            }
            for e, score in page
        ]
        return {
            "total": len(rows),
            "offset": p.offset,
            "limit": p.limit,
            "note": "; ".join(notes) or None,
            "results": results,
        }

    def _search_matches(self, conn, snap: _Snapshot, terms: tuple, folder: str, pred,
                        sort: str) -> tuple[list, bool]:
        """Ranked candidates in the folder that pass the filters, duplicates collapsed."""
        from foley import search

        key = (snap.version, terms, folder)
        got = self._ranked.get(key)
        if got is None:
            ranked = search.rank(
                conn, " ".join(terms), search.SearchFilters(folder=folder or None),
                limit=SEARCH_CANDIDATES,
            )
            got = (ranked, len(ranked) >= SEARCH_CANDIDATES)
            self._ranked.put(key, got)
        ranked, capped = got
        seen: set[str] = set()
        matches = []
        for aid, score in ranked:
            e = snap.by_id.get(aid)
            if e is None or not pred(e):
                continue
            if e.content_hash:
                if e.content_hash in seen:
                    continue
                seen.add(e.content_hash)
            matches.append((e, score))
        if sort != "relevance":
            keyfn = _SORT_KEYS["name" if sort == "-name" else sort]
            matches.sort(key=lambda es: keyfn(es[0]), reverse=sort == "-name")
        return matches, capped

    # ----------------------------------------------------------------- asset

    def asset(self, conn: sqlite3.Connection, aid: int) -> dict:
        from foley import images, search

        try:
            out = search.describe(conn, aid)
        except LookupError as e:
            raise _HTTPError(404, f"no such asset: {aid}") from e
        star = self.shortlist.get(out.get("content_hash"))
        x0, x1 = images.OVERVIEW_PLOT_X
        duration = (out.get("features") or {}).get("duration_s")
        if duration and duration > 0:
            x1 = x0 + (x1 - x0) * min(1.0, duration / max(duration, images.MIN_SPAN_S))
        folder, _, name = out["path"].rpartition("/")
        out.update(
            {
                "name": name,
                "folder": folder,
                "starred": star is not None,
                "star_note": star.get("note") if star else None,
                "audio_url": f"/api/audio/{aid}",
                "image_url": f"/api/image/{aid}",
                "peaks_url": f"/api/peaks/{aid}",
                "image_plot_x": [round(x0, 6), round(x1, 6)],
            }
        )
        return out

    def asset_row(self, conn: sqlite3.Connection, aid: int):
        row = conn.execute(
            "SELECT id, rel_path, ext, content_hash, status FROM assets WHERE id = ?", (aid,)
        ).fetchone()
        if row is None:
            raise _HTTPError(404, f"no such asset: {aid}")
        return row

    def decode(self, row):
        from foley import decode

        path = self.cfg.abs_path(row["rel_path"])
        if not path.is_file():
            raise _HTTPError(404, f"asset {row['id']}: file is missing: {row['rel_path']}")
        with self.decode_slots:
            try:
                return decode.decode_path(path)
            except decode.DecodeError as e:
                raise _HTTPError(422, f"asset {row['id']}: cannot decode: {e}") from e
            except OSError as e:
                raise _HTTPError(404, f"asset {row['id']}: cannot read file: {e}") from e

    def transcoded(self, row) -> bytes:
        key = (row["id"], row["content_hash"])
        body = self.transcodes.get(key)
        if body is None:
            audio = self.decode(row)
            body = wav_bytes(audio.samples, audio.sample_rate)
            self.transcodes.put(key, body)
        return body

    # ----------------------------------------------------------------- peaks / image

    def peaks(self, conn: sqlite3.Connection, aid: int, n: int) -> bytes:
        row = self.asset_row(conn, aid)
        h = row["content_hash"]
        cache = None
        if h:
            cache = self.peaks_dir / h[:2] / f"{h}-{n}-v{PEAKS_VERSION}.json"
            try:
                return cache.read_bytes()
            except OSError:
                pass
        path = self.cfg.abs_path(row["rel_path"])
        streamed = None
        if path.is_file():
            with self.decode_slots:
                try:
                    streamed = stream_peaks(path, n)
                except Exception:  # noqa: BLE001 - truncated/odd file: full decode decides
                    streamed = None
        if streamed is not None:
            duration, (n_out, lo, hi, peak) = streamed
        else:
            audio = self.decode(row)
            duration = audio.duration_s
            n_out, lo, hi, peak = compute_peaks(audio.samples, n)
        body = _dumps(
            {
                "duration_s": round(float(duration), 6),
                "channels": len(lo),
                "n": n_out,
                "peak": peak,
                "min": lo,
                "max": hi,
            }
        )
        if cache is not None:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_name(f"{cache.name}.{threading.get_ident()}.tmp")
            tmp.write_bytes(body)
            os.replace(tmp, cache)
        return body

    def image(self, conn: sqlite3.Connection, aid: int) -> bytes:
        from foley import images

        self.asset_row(conn, aid)
        cached = conn.execute(
            "SELECT local_path, version FROM images WHERE asset_id = ? AND kind = 'overview'",
            (aid,),
        ).fetchone()
        if cached is not None and cached["version"] == images.IMAGE_VERSION:
            try:
                return (self.cfg.data_dir / cached["local_path"]).read_bytes()
            except OSError:
                pass
        with self.render_lock:  # matplotlib Figure/Agg rendering is serialized
            try:
                path = images.render_overview(self.cfg, conn, aid)
            except ValueError as e:
                msg = str(e)
                status = 422 if "cannot decode" in msg else 404
                raise _HTTPError(status, msg) from e
        return Path(path).read_bytes()

    # ----------------------------------------------------------------- shortlist

    def set_star(self, conn: sqlite3.Connection, body: dict) -> dict:
        try:
            aid = int(body.get("id"))
        except (TypeError, ValueError) as e:
            raise _HTTPError(400, "body needs a numeric `id`") from e
        starred = body.get("starred")
        if not isinstance(starred, bool):
            raise _HTTPError(400, "body needs `starred`: true or false")
        note = body.get("note", ...)
        if note is not ... and note is not None and not isinstance(note, str):
            raise _HTTPError(400, "`note` must be a string")
        row = self.asset_row(conn, aid)
        if not row["content_hash"]:
            raise _HTTPError(422, f"asset {aid} has no content hash yet (not indexed)")
        entry = self.shortlist.set(row["content_hash"], row["rel_path"], starred, note)
        return {"starred": entry is not None, "note": entry.get("note") if entry else None}

    def shortlist_items(self, conn: sqlite3.Connection) -> dict:
        return {"items": shortlist_items(conn, self.shortlist.data())}


def _bound_check(attr: str, op: str, value: float):
    if op == ">=":
        def at_least(e) -> bool:
            x = getattr(e, attr)
            return x is not None and x >= value

        return at_least

    def at_most(e) -> bool:
        x = getattr(e, attr)
        return x is not None and x <= value

    return at_most


class _ListParams:
    _FLOATS = {
        "min_duration": ("duration_s", ">="),
        "max_duration": ("duration_s", "<="),
        "min_lufs": ("lufs", ">="),
        "max_lufs": ("lufs", "<="),
        "max_attack": ("attack_s", "<="),
    }

    def __init__(self, qs: dict):
        def one(name: str) -> str:
            values = qs.get(name)
            return values[-1].strip() if values else ""

        self.folder = _norm_folder(one("folder"))
        self.q = one("q")
        self.bounds: list[tuple[str, str, float]] = []
        for name, (attr, op) in self._FLOATS.items():
            raw = one(name)
            if raw:
                try:
                    value = float(raw)
                except ValueError as e:
                    raise _HTTPError(400, f"bad value for {name}: {raw!r}") from e
                if not math.isfinite(value):
                    raise _HTTPError(400, f"bad value for {name}: {raw!r}")
                self.bounds.append((attr, op, value))
        music = one("is_music").lower()
        if music not in ("", "0", "1", "true", "false"):
            raise _HTTPError(400, f"bad value for is_music: {music!r} (use 0 or 1)")
        self.is_music = None if not music else music in ("1", "true")
        exts = [x.strip().lower() for x in one("ext").split(",") if x.strip()]
        self.exts = frozenset(x if x.startswith(".") else "." + x for x in exts)
        starred = one("starred").lower()
        if starred not in ("", "0", "1", "true", "false"):
            raise _HTTPError(400, f"bad value for starred: {starred!r} (use 0 or 1)")
        self.starred = starred in ("1", "true")
        self.sort = one("sort") or None
        if self.sort is not None and self.sort not in SORTS:
            raise _HTTPError(400, f"bad sort {self.sort!r}; use one of {', '.join(SORTS)}")
        self.offset = self._int(one("offset"), "offset", 0)
        if self.offset < 0:
            raise _HTTPError(400, "offset must be >= 0")
        limit = self._int(one("limit"), "limit", LIST_DEFAULT_LIMIT)
        if limit < 1:
            raise _HTTPError(400, "limit must be >= 1")
        self.limit = min(limit, LIST_MAX_LIMIT)

    @staticmethod
    def _int(raw: str, name: str, default: int) -> int:
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError as e:
            raise _HTTPError(400, f"bad value for {name}: {raw!r}") from e

    def filter_key(self) -> tuple:
        return (self.folder, tuple(self.bounds), self.is_music, self.exts, self.starred)

    def predicate(self, starred: frozenset):
        checks = []
        if self.folder:
            prefix = self.folder + "/"
            checks.append(lambda e: e.path.startswith(prefix))
        checks += [_bound_check(attr, op, value) for attr, op, value in self.bounds]
        if self.is_music is True:
            checks.append(lambda e: e.is_music == 1)
        elif self.is_music is False:
            checks.append(lambda e: e.is_music != 1)
        if self.exts:
            exts = self.exts
            checks.append(lambda e: e.ext in exts)
        if self.starred:
            checks.append(lambda e: e.content_hash is not None and e.content_hash in starred)
        if not checks:
            return lambda e: True
        if len(checks) == 1:
            return checks[0]
        return lambda e: all(c(e) for c in checks)


# =============================================================================== HTTP


_PLACEHOLDER = b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Foley Browse</title>
<style>body{background:#14161a;color:#e8e8e8;font:15px/1.5 system-ui,sans-serif;
padding:2rem}code{color:#6cb6ff}</style></head>
<body><h1>Foley Browse</h1><p>The web UI files are not installed
(<code>foley/browse_ui/index.html</code> is missing). The API is running: try
<a style="color:#6cb6ff" href="/api/status">/api/status</a>.</p></body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "FoleyBrowse"
    protocol_version = "HTTP/1.1"
    timeout = 60  # idle keep-alive connections close after this
    _conn: sqlite3.Connection | None = None

    # ----------------------------------------------------------------- plumbing

    @property
    def app(self) -> BrowseApp:
        return self.server.app

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self.app.connect()
        return self._conn

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None

    def handle(self) -> None:
        try:
            super().handle()
        except (ConnectionError, TimeoutError):
            self.close_connection = True

    def log_message(self, format, *args) -> None:  # noqa: A002 - stdlib signature
        if getattr(self.server, "verbose", False):
            super().log_message(format, *args)

    def _headers(self, status: int, ctype: str, length: int, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self._headers(status, ctype, len(body), extra)
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, _dumps(obj), "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def _error(self, status: int, message: str, extra: dict | None = None) -> None:
        headers = {"Cache-Control": "no-store", **(extra or {})}
        self._send(status, _dumps({"error": message}), "application/json; charset=utf-8",
                   headers)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return False
        if host.startswith("["):
            name = host[1 : host.find("]")] if "]" in host else ""
        else:
            name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        name = name.rstrip(".")
        return is_loopback(name) or name in self.app.allowed_hosts

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        parts = urlsplit(origin.strip())
        host = (self.headers.get("Host") or "").strip().lower()
        return parts.scheme == "http" and parts.netloc.lower() == host

    # ----------------------------------------------------------------- verbs

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            if not self._host_ok():
                self.close_connection = True
                self._error(403, "forbidden host: foley browse only answers on localhost")
                return
            parts = urlsplit(self.path)
            path = unquote(parts.path)
            qs = parse_qs(parts.query, keep_blank_values=True)
            if method == "POST":
                self.close_connection = True  # never reuse a connection after a body
                if not self._origin_ok():
                    self._error(403, "cross-origin request refused")
                    return
                self._route_post(path)
            else:
                self._route_get(path, qs)
        except _HTTPError as e:
            self._safe_error(e.status, e.message)
        except (ConnectionError, TimeoutError):
            self.close_connection = True
        except Exception as e:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            self._safe_error(500, f"internal error: {e.__class__.__name__}: {e}")

    def _safe_error(self, status: int, message: str) -> None:
        try:
            self._error(status, message)
        except (ConnectionError, TimeoutError):
            self.close_connection = True

    @staticmethod
    def _id(text: str) -> int:
        if not text.isdigit() or len(text) > 18:
            raise _HTTPError(404, f"no such asset: {text}")
        return int(text)

    def _route_get(self, path: str, qs: dict) -> None:
        app = self.app
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/") :])
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if path == "/api/status":
            return self._json(app.status(self.conn))
        if path == "/api/tree":
            return self._json(app.tree(self.conn, (qs.get("path") or [""])[-1]))
        if path == "/api/list":
            return self._json(app.listing(self.conn, qs))
        if path == "/api/shortlist":
            return self._json(app.shortlist_items(self.conn))
        head, _, rest = path.strip("/").partition("/")
        kind, _, ident = rest.partition("/")
        if head == "api" and ident and "/" not in ident:
            if kind == "asset":
                return self._json(app.asset(self.conn, self._id(ident)))
            if kind == "audio":
                flag = (qs.get("transcode") or ["0"])[-1].lower()
                return self._audio(self._id(ident), flag in ("1", "true", "yes"))
            if kind == "image":
                body = app.image(self.conn, self._id(ident))
                return self._send(200, body, "image/png", {"Cache-Control": "no-cache"})
            if kind == "peaks":
                return self._peaks(self._id(ident), qs)
        raise _HTTPError(404, f"not found: {path}")

    def _route_post(self, path: str) -> None:
        if path != "/api/shortlist":
            raise _HTTPError(404, f"not found: {path}")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as e:
            raise _HTTPError(400, "bad Content-Length") from e
        if length <= 0 or length > _MAX_BODY:
            raise _HTTPError(400, "expected a small JSON body")
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise _HTTPError(400, "body is not valid JSON") from e
        if not isinstance(body, dict):
            raise _HTTPError(400, "body must be a JSON object")
        self._json(self.app.set_star(self.conn, body))

    # ----------------------------------------------------------------- endpoints

    def _static(self, name: str) -> None:
        if name not in STATIC_FILES:
            raise _HTTPError(404, f"not found: /static/{name}")
        try:
            body = (UI_DIR / name).read_bytes()
        except OSError:
            if name != "index.html":
                raise _HTTPError(404, f"not found: /static/{name}") from None
            body = _PLACEHOLDER
        self._send(200, body, STATIC_FILES[name], {"Cache-Control": "no-cache"})

    def _peaks(self, aid: int, qs: dict) -> None:
        raw = (qs.get("n") or [""])[-1].strip()
        try:
            n = int(raw) if raw else PEAKS_DEFAULT_N
        except ValueError as e:
            raise _HTTPError(400, f"bad value for n: {raw!r}") from e
        n = max(PEAKS_MIN_N, min(PEAKS_MAX_N, n))
        body = self.app.peaks(self.conn, aid, n)
        self._send(200, body, "application/json; charset=utf-8",
                   {"Cache-Control": "private, max-age=3600"})

    def _audio(self, aid: int, transcode: bool) -> None:
        row = self.app.asset_row(self.conn, aid)
        ext = (row["ext"] or "").lower()
        if transcode or ext in TRANSCODE_EXTS:
            body = self.app.transcoded(row)
            return self._ranged_bytes(body, "audio/wav")
        path = self.app.cfg.abs_path(row["rel_path"])
        try:
            fh = open(path, "rb")  # read-only: the library is never written
        except OSError as e:
            raise _HTTPError(404, f"asset {aid}: file is missing: {row['rel_path']}") from e
        with fh:
            size = os.fstat(fh.fileno()).st_size
            ctype = AUDIO_TYPES.get(ext, "application/octet-stream")
            start, end, status, extra = self._range_plan(size)
            if status == 416:
                return
            self._headers(status, ctype, end - start + 1 if size else 0, extra)
            if self.command == "HEAD" or not size:
                return
            fh.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = fh.read(min(_STREAM_CHUNK, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _range_plan(self, size: int) -> tuple[int, int, int, dict]:
        extra = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}
        try:
            rng = parse_range(self.headers.get("Range"), size)
        except _Unsatisfiable:
            self._error(416, "requested range not satisfiable",
                        {"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"})
            return 0, -1, 416, extra
        if rng is None:
            return 0, size - 1, 200, extra
        start, end = rng
        extra["Content-Range"] = f"bytes {start}-{end}/{size}"
        return start, end, 206, extra

    def _ranged_bytes(self, body: bytes, ctype: str) -> None:
        start, end, status, extra = self._range_plan(len(body))
        if status == 416:
            return
        self._headers(status, ctype, end - start + 1, extra)
        if self.command != "HEAD":
            view = memoryview(body)[start : end + 1]
            for i in range(0, len(view), _STREAM_CHUNK):
                self.wfile.write(view[i : i + _STREAM_CHUNK])


class BrowseServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server bind the same port: keep it off there.
    allow_reuse_address = os.name != "nt"

    def __init__(self, cfg: Config, host: str, port: int, verbose: bool = False):
        self.app = BrowseApp(cfg, host)
        self.verbose = verbose
        super().__init__((host, port), _Handler)

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        if ":" in str(host):
            host = f"[{host}]"
        return f"http://{host}:{port}/"


class _BrowseServerV6(BrowseServer):
    address_family = socket.AF_INET6


def make_server(cfg: Config, host: str = "127.0.0.1", port: int = 8765,
                verbose: bool = False) -> BrowseServer:
    db.open_catalog(cfg).close()  # create/migrate up front; fail fast on a broken catalog
    host = host.strip().strip("[]")
    if ":" in host:  # IPv6 literal
        return _BrowseServerV6(cfg, host, port, verbose=verbose)
    return BrowseServer(cfg, host, port, verbose=verbose)


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True,
          out=None, verbose: bool = False) -> None:
    out = out or (lambda line: print(line, flush=True))
    server = make_server(cfg, host, port, verbose=verbose)
    url = server.url
    if not is_loopback(host):
        bar = "!" * 72
        out(bar)
        out(f"WARNING: foley browse is listening on {host}, not just this machine.")
        out("The library's sample licences forbid hosting it for others; only requests")
        out("addressed to localhost or the bound address are answered. Use 127.0.0.1.")
        out(bar)
    out(f"foley browse: {url}  (catalog {cfg.catalog_path}; Ctrl+C to stop)")
    if open_browser:
        import webbrowser

        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        out("foley browse: stopped")
    finally:
        server.server_close()
