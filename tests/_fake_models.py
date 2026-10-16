"""Fake PANNs / CLAP models for tests: CPU-only, no torch, no downloads.

conftest.py installs FakeFactory for every test not marked `models`, so any
`foley index` in a test runs the model stages with these. Outputs are deterministic
functions of the windows the indexer passes in, and every call is recorded so tests can
check batching and windowing.
"""

from __future__ import annotations

import hashlib

import numpy as np

from foley.models import ModelFactory, ModelUnavailable, panns, prep
from foley.models import clap as clap_models

LABELS = ["Music", "Speech", "Dog", "Rain", "Silence", "Wind", "Tick"]
DIM = 8


def _seed(windows) -> int:
    h = hashlib.blake2b(digest_size=8)
    for w in windows:
        h.update(np.asarray(w, dtype=np.float32).tobytes())
    return int.from_bytes(h.digest(), "little")


class FakeTagger:
    key = panns.MODEL_KEY
    labels = LABELS

    def __init__(self, calls: list):
        self.calls = calls

    def tag(self, clips):
        self.calls.append(("tag", [[len(w) for w in ws] for ws in clips]))
        out = np.zeros((len(clips), len(LABELS)), dtype=np.float32)
        for i, ws in enumerate(clips):
            rng = np.random.default_rng(_seed(ws))
            out[i] = rng.uniform(0.0, 0.25, len(LABELS))
            seconds = sum(len(w) for w in ws) / prep.PANNS_SR
            out[i, 0] = 0.9 if seconds > 7.5 else 0.05  # "Music": only long clips
            out[i, LABELS.index("Tick")] = 0.6  # always a confident tag (>= 0.3)
        return out


class FakeAudioEmbedder:
    def __init__(self, short_name: str, calls: list):
        self.key = clap_models.model_key(short_name)
        self.dim = DIM
        self.calls = calls

    def embed(self, clips):
        self.calls.append(("embed", self.key, [[len(w) for w in ws] for ws in clips]))
        out = np.zeros((len(clips), DIM), dtype=np.float32)
        for i, ws in enumerate(clips):
            v = np.random.default_rng(_seed(ws)).normal(size=DIM)
            out[i] = v / np.linalg.norm(v)
        return out


class FakeTextEncoder:
    def __init__(self, short_name: str):
        self.key = clap_models.model_key(short_name)
        self.dim = DIM

    def embed_text(self, texts):
        out = np.zeros((len(texts), DIM), dtype=np.float32)
        for i, t in enumerate(texts):
            seed = int.from_bytes(hashlib.blake2b(t.encode(), digest_size=8).digest(), "little")
            v = np.random.default_rng(seed).normal(size=DIM)
            out[i] = v / np.linalg.norm(v)
        return out


class FakeFactory(ModelFactory):
    def __init__(self, fail: set[str] | None = None):
        self.calls: list = []
        self.loads: list = []
        self.fail = set(fail or ())  # "tags", "<clap short name>", "text"

    def tagger(self, cfg, device):
        self.loads.append(("tagger", device))
        if "tags" in self.fail:
            raise ModelUnavailable("fake tagger unavailable")
        return FakeTagger(self.calls)

    def clap_audio(self, short_name, device):
        self.loads.append(("clap", short_name, device))
        if short_name in self.fail:
            raise ModelUnavailable(f"fake {short_name} unavailable")
        return FakeAudioEmbedder(short_name, self.calls)

    def clap_text(self, short_name, device):
        if "text" in self.fail:
            raise ModelUnavailable("fake text encoder unavailable")
        return FakeTextEncoder(short_name)


class UnavailableFactory(ModelFactory):
    """The default in tests: like a machine whose weights cannot be fetched, so the model
    stages are skipped (with a note) unless a test installs FakeFactory."""

    def tagger(self, cfg, device):
        raise ModelUnavailable("model loading is disabled in tests (use FakeFactory)")

    def clap_audio(self, short_name, device):
        raise ModelUnavailable("model loading is disabled in tests (use FakeFactory)")

    def clap_text(self, short_name, device):
        raise ModelUnavailable("model loading is disabled in tests (use FakeFactory)")
