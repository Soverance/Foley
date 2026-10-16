"""A stand-in for `foley.models.clap` (the contract interface), CPU-only, no weights.

Text and "audio" embeddings are signed hashed bags of words, so a sound embedded from
the words "dog barking" is close to the query "barking dog" whatever its file name.
Checkpoint "fake-a" embeds words; "fake-b" is a worse model (hashes letters pairs).
"""

from __future__ import annotations

import hashlib
import re
import threading
from types import SimpleNamespace

import numpy as np

from foley import vectors

DIM = 1024
CHECKPOINTS = {"fake-a": "test/fake-a", "fake-b": "test/fake-b"}


def _bow(text: str, model: str = "fake-a") -> np.ndarray:
    v = np.zeros(DIM, dtype=np.float32)
    words = re.findall(r"[a-z0-9]+", text.lower())
    if model == "fake-b":
        words = [w[i : i + 2] for w in words for i in range(0, max(1, len(w) - 1), 2)]
    for w in words:
        h = int.from_bytes(hashlib.blake2b(f"{model}:{w}".encode(), digest_size=8).digest(),
                           "little")
        v[h % DIM] += 1.0 if (h >> 32) & 1 else -1.0
    n = float(np.linalg.norm(v))
    return v / n if n else v


def embed(text: str, model: str = "fake-a") -> np.ndarray:
    return _bow(text, model)


def model_key(short_name: str) -> str:
    if short_name not in CHECKPOINTS:
        raise KeyError(short_name)
    return f"clap:{CHECKPOINTS[short_name]}@rev1"


class FakeEncoder:
    def __init__(self, short_name: str):
        self.key = model_key(short_name)
        self.short_name = short_name
        self.dim = DIM
        self.calls = 0

    def embed_text(self, texts):
        self.calls += 1
        return np.stack([_bow(t, self.short_name) for t in texts]).astype(np.float32)


class FakeClap(SimpleNamespace):
    pass


def make_api(fail: str | None = None):
    lock = threading.Lock()
    cache: dict[str, FakeEncoder] = {}

    def text_encoder(short_name=None):
        name = short_name or "fake-a"
        if fail:
            raise RuntimeError(fail)
        with lock:
            if name not in cache:
                cache[name] = FakeEncoder(name)
            return cache[name]

    return FakeClap(CLAP_CHECKPOINTS=dict(CHECKPOINTS), DEFAULT_CLAP="fake-a",
                    model_key=model_key, text_encoder=text_encoder, encoders=cache)


def install(monkeypatch, fail: str | None = None):
    api = make_api(fail)
    monkeypatch.setattr(vectors, "clap_api", lambda: api)
    vectors._reset_encoders()
    vectors.clear_cache()
    return api


def add_embedding(conn, asset_id: int, text_or_vec, model: str = "fake-a") -> None:
    vec = embed(text_or_vec, model) if isinstance(text_or_vec, str) else \
        np.asarray(text_or_vec, dtype=np.float32)
    conn.execute(
        "INSERT OR REPLACE INTO embeddings(asset_id, model, dim, vector) VALUES (?, ?, ?, ?)",
        (asset_id, model_key(model), int(vec.shape[0]), vec.astype("<f4").tobytes()),
    )
