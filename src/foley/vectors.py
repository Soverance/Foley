"""In-memory CLAP vectors for hybrid search and find-similar.

CONTRACT:

    clap_api() -> module | None
        The `foley.models.clap` interface (CLAP_CHECKPOINTS, DEFAULT_CLAP, model_key,
        text_encoder), imported lazily once per process (the result, module or None, is
        kept); None when it cannot be imported (models not installed). Callers check for
        embeddings first so a catalog without any never imports it (it may pull in torch).
        Tests replace this function with a fake implementing the interface.
    model_key(short_name) -> str | None      clap_api().model_key, None if unavailable
    checkpoints() -> list[str]               short names of CLAP_CHECKPOINTS ([] if none)
    default_clap() -> str | None             DEFAULT_CLAP

    embed_text(short_name, text) -> np.ndarray (dim,) float32, L2-normalized
        The CLAP text embedding of `text`, through clap_api().text_encoder(short_name)
        (process-wide, CPU, loaded on first use: may take seconds). Recent queries are
        cached (QUERY_CACHE entries). Raises EncoderUnavailable when the encoder cannot be
        imported or loaded; a failed load is not retried for RETRY_S seconds.
    encoder_state(short_name) -> "unloaded" | "loading" | "ready" | "failed"
    encoder_error(short_name) -> str | None

    get_index(conn, model: str) -> VectorIndex | None
        Every embeddings row of `model` (a model key) as one float32 matrix, cached per
        catalog file and model (the MAX_INDEXES most recently used), None when there are
        none. Rows are re-normalized; rows
        whose blob length disagrees with `dim` are skipped. The cache is invalidated when
        the catalog changes: a cheap fingerprint (catalog file identity, per-model row
        count and max rowid, latest index run) is checked on every call.
        Thread-safe: concurrent callers share one load.
    embedding_models(conn) -> dict[str, int]   model key -> row count (fingerprint query)

    VectorIndex (immutable once built):
        model, dim, ids (int64, ascending), matrix (n, dim) float32
        __len__, row(asset_id) -> int | None, vector(asset_id) -> np.ndarray | None
        mask(asset_ids) -> bool array over rows (True where the id is in `asset_ids`)
        similarities(query) -> (n,) float32 cosine of every row with `query`
        topk(query, k, restrict=None, exclude=None) -> list[(asset_id, cosine)]
            best first; `restrict` (iterable of ids or a bool row mask) limits the rows,
            `exclude` drops ids. Brute force (one matmul + argpartition): about 5 ms at
            40k x 512 on a desktop CPU.

DESIGN NOTES:

    No sqlite-vec (see the spec's data model): 40k 512-d vectors are ~80 MB and a
    matmul over them takes milliseconds. The matrix holds every embedding of the model,
    whatever the asset's status; callers restrict to the assets their filters allow.
    PRAGMA data_version is per connection and blind to the connection's own commits, so
    the fingerprint is a small aggregate query instead (count(*) walks the embeddings
    primary-key index, ~2 ms at 40k rows).
"""

from __future__ import annotations

import importlib
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable
from pathlib import Path

import numpy as np

QUERY_CACHE = 256
MAX_INDEXES = 6  # cached matrices (catalogs x models), least recently used dropped
RETRY_S = 300.0


class EncoderUnavailable(RuntimeError):
    pass


# --------------------------------------------------------------------------- clap interface


_api: list = []  # [module | None] once imported (or failed): imported once per process


def clap_api():
    if not _api:
        try:
            module = importlib.import_module("foley.models.clap")
        except Exception:  # noqa: BLE001 - not installed / broken install: keyword-only
            module = None
        _api[:] = [module]
    return _api[0]


def model_key(short_name: str) -> str | None:
    api = clap_api()
    if api is None or not short_name:
        return None
    try:
        return api.model_key(short_name)
    except Exception:  # noqa: BLE001 - unknown checkpoint name
        return None


def checkpoints() -> list[str]:
    api = clap_api()
    return list(getattr(api, "CLAP_CHECKPOINTS", {}) or {}) if api is not None else []


def default_clap() -> str | None:
    api = clap_api()
    return getattr(api, "DEFAULT_CLAP", None) if api is not None else None


# --------------------------------------------------------------------------- text encoder

_enc_lock = threading.Lock()
_enc_state: dict[str, str] = {}
_enc_error: dict[str, tuple[float, str]] = {}
_query_cache: OrderedDict = OrderedDict()


def encoder_state(short_name: str) -> str:
    with _enc_lock:
        return _enc_state.get(short_name, "unloaded")


def encoder_error(short_name: str) -> str | None:
    with _enc_lock:
        got = _enc_error.get(short_name)
        return got[1] if got else None


def _reset_encoders() -> None:
    """Forget encoder states, failures and cached queries (tests)."""
    with _enc_lock:
        _enc_state.clear()
        _enc_error.clear()
        _query_cache.clear()


def embed_text(short_name: str, text: str) -> np.ndarray:
    key = (short_name, text)
    with _enc_lock:
        if key in _query_cache:
            _query_cache.move_to_end(key)
            return _query_cache[key]
        failed = _enc_error.get(short_name)
        if failed and time.monotonic() - failed[0] < RETRY_S:
            raise EncoderUnavailable(failed[1])
        if _enc_state.get(short_name) != "ready":
            _enc_state[short_name] = "loading"
    api = clap_api()
    try:
        if api is None:
            raise EncoderUnavailable("the CLAP models are not installed (foley.models)")
        encoder = api.text_encoder(short_name)
        vec = np.asarray(encoder.embed_text([text]), dtype=np.float32).reshape(-1)
    except Exception as e:  # noqa: BLE001
        msg = str(e) if isinstance(e, EncoderUnavailable) else (
            f"cannot load the CLAP text encoder {short_name!r}: {e.__class__.__name__}: {e}")
        with _enc_lock:
            _enc_state[short_name] = "failed"
            _enc_error[short_name] = (time.monotonic(), msg)
        raise EncoderUnavailable(msg) from e
    norm = float(np.linalg.norm(vec))
    if norm > 0:
        vec = vec / norm
    vec.setflags(write=False)
    with _enc_lock:
        _enc_state[short_name] = "ready"
        _enc_error.pop(short_name, None)
        _query_cache[key] = vec
        while len(_query_cache) > QUERY_CACHE:
            _query_cache.popitem(last=False)
    return vec


# --------------------------------------------------------------------------- index


class VectorIndex:
    def __init__(self, model: str, ids: np.ndarray, matrix: np.ndarray):
        self.model = model
        self.ids = ids
        self.matrix = matrix
        self.dim = int(matrix.shape[1]) if matrix.ndim == 2 else 0
        ids.setflags(write=False)
        matrix.setflags(write=False)

    def __len__(self) -> int:
        return int(self.ids.shape[0])

    def row(self, asset_id: int) -> int | None:
        i = int(np.searchsorted(self.ids, asset_id))
        if i < len(self.ids) and int(self.ids[i]) == int(asset_id):
            return i
        return None

    def vector(self, asset_id: int) -> np.ndarray | None:
        i = self.row(asset_id)
        return None if i is None else self.matrix[i]

    def mask(self, asset_ids) -> np.ndarray:
        wanted = np.fromiter((int(a) for a in asset_ids), dtype=np.int64)
        if not len(wanted):
            return np.zeros(len(self), dtype=bool)
        return np.isin(self.ids, wanted, assume_unique=False)

    def similarities(self, query: np.ndarray) -> np.ndarray:
        q = np.asarray(query, dtype=np.float32).reshape(-1)
        if q.shape[0] != self.dim:
            raise ValueError(f"query has {q.shape[0]} dims, {self.model} has {self.dim}")
        return self.matrix @ q

    def topk(self, query: np.ndarray, k: int, restrict=None,
             exclude: Iterable[int] | None = None,
             sims: np.ndarray | None = None) -> list[tuple[int, float]]:
        if k <= 0 or not len(self):
            return []
        s = self.similarities(query) if sims is None else sims
        valid = None
        if restrict is not None:
            valid = restrict if isinstance(restrict, np.ndarray) and restrict.dtype == bool \
                else self.mask(restrict)
        if exclude:
            ex = self.mask(exclude)
            valid = ~ex if valid is None else valid & ~ex
        if valid is not None:
            idx = np.flatnonzero(valid)
            if not len(idx):
                return []
            sub = s[idx]
        else:
            idx = None
            sub = s
        k = min(k, len(sub))
        part = np.argpartition(-sub, k - 1)[:k] if k < len(sub) else np.arange(len(sub))
        order = part[np.lexsort((part, -sub[part]))]  # by similarity, then row (= id) order
        rows = order if idx is None else idx[order]
        return [(int(self.ids[r]), float(s[r])) for r in rows]


def embedding_models(conn: sqlite3.Connection) -> dict[str, int]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT model, count(*) FROM embeddings GROUP BY model")}


def _fingerprint(conn: sqlite3.Connection, catalog: Path | None) -> tuple:
    ident = None
    if catalog is not None:
        try:
            st = catalog.stat()
            ident = (st.st_dev, st.st_ino)
        except OSError:
            ident = None
    models = tuple(conn.execute(
        "SELECT model, count(*), max(rowid) FROM embeddings GROUP BY model ORDER BY model"))
    run = conn.execute(
        "SELECT id || ':' || status || ':' || coalesce(finished_at, '') FROM index_runs "
        "ORDER BY id DESC LIMIT 1").fetchone()
    return (ident, tuple(tuple(m) for m in models), run[0] if run else None)


def _catalog_path(conn: sqlite3.Connection) -> Path | None:
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main" and row[2]:
            return Path(row[2])
    return None


def _load(conn: sqlite3.Connection, model: str) -> VectorIndex | None:
    rows = conn.execute(
        "SELECT asset_id, dim, vector FROM embeddings WHERE model = ? ORDER BY asset_id",
        (model,),
    ).fetchall()
    if not rows:
        return None
    dims = {}
    for r in rows:
        dims[r[1]] = dims.get(r[1], 0) + 1
    dim = max(dims, key=dims.get)  # the model's dimension; odd rows are skipped
    good = [r for r in rows if r[1] == dim and r[2] is not None and len(r[2]) == dim * 4]
    if not good or dim <= 0:
        return None
    ids = np.fromiter((r[0] for r in good), dtype=np.int64, count=len(good))
    matrix = np.frombuffer(b"".join(r[2] for r in good), dtype="<f4").reshape(len(good), dim)
    matrix = matrix.astype(np.float32, copy=True)
    np.nan_to_num(matrix, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    np.divide(matrix, norms, out=matrix, where=norms > 0)
    return VectorIndex(model, ids, matrix)


class _Store:
    def __init__(self):
        self._lock = threading.Lock()
        # (catalog, model) -> (fingerprint, index), least recently used first
        self._entries: OrderedDict = OrderedDict()

    def get(self, conn: sqlite3.Connection, model: str) -> VectorIndex | None:
        catalog = _catalog_path(conn)
        where = os.path.normcase(str(catalog)) if catalog else id(conn)
        fp = _fingerprint(conn, catalog)
        key = (where, model)
        with self._lock:
            got = self._entries.get(key)
            if got is not None and got[0] == fp:
                self._entries.move_to_end(key)
                return got[1]
            self._entries.pop(key, None)  # free the stale matrix before loading anew
            index = _load(conn, model)
            self._entries[key] = (fp, index)
            while len(self._entries) > MAX_INDEXES:
                self._entries.popitem(last=False)
            return index

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_store = _Store()


def get_index(conn: sqlite3.Connection, model: str) -> VectorIndex | None:
    return _store.get(conn, model)


def clear_cache() -> None:
    _store.clear()
