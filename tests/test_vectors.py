from __future__ import annotations

import threading
import time

import _fake_clap as fc
import numpy as np
import pytest
from _catalog_builder import add_asset

from foley import vectors
from foley.catalog import db


@pytest.fixture
def api(monkeypatch):
    return fc.install(monkeypatch)


@pytest.fixture
def conn(cfg, api):
    c = db.open_catalog(cfg)
    yield c
    c.close()


def _index(n=50, dim=16, seed=0) -> vectors.VectorIndex:
    rng = np.random.default_rng(seed)
    m = rng.standard_normal((n, dim)).astype(np.float32)
    m /= np.linalg.norm(m, axis=1, keepdims=True)
    return vectors.VectorIndex("m", np.arange(10, 10 + n, dtype=np.int64), m)


def test_topk_matches_full_sort_and_honours_restrict_and_exclude():
    idx = _index()
    q = idx.matrix[3] + 0.1 * idx.matrix[7]
    sims = idx.matrix @ q
    want = [int(idx.ids[i]) for i in np.argsort(-sims)]
    assert [a for a, _ in idx.topk(q, 10)] == want[:10]
    assert idx.topk(q, 1)[0][0] == 13 and idx.topk(q, 1)[0][1] == pytest.approx(sims[3])
    assert len(idx.topk(q, 500)) == 50 and idx.topk(q, 0) == []
    allowed = {20, 30, 40, 13}
    got = [a for a, _ in idx.topk(q, 3, restrict=allowed)]
    assert got == [a for a in want if a in allowed][:3]
    mask = idx.mask(allowed)
    assert [a for a, _ in idx.topk(q, 3, restrict=mask)] == got
    assert 13 not in [a for a, _ in idx.topk(q, 5, exclude=[13])]
    assert idx.topk(q, 5, restrict=[]) == []
    assert idx.row(13) == 3 and idx.row(9) is None and idx.vector(999) is None


def test_topk_is_fast_at_library_scale():
    rng = np.random.default_rng(1)
    n, dim = 40_000, 512
    m = rng.standard_normal((n, dim)).astype(np.float32)
    m /= np.linalg.norm(m, axis=1, keepdims=True)
    idx = vectors.VectorIndex("m", np.arange(n, dtype=np.int64), m)
    q = m[124]
    idx.topk(q, 200)  # warm up BLAS
    t = time.perf_counter()
    for _ in range(5):
        top = idx.topk(q, 200, restrict=idx.mask(range(0, n, 2)))
    per = (time.perf_counter() - t) / 5
    assert top[0][0] == 124
    assert per < 0.25, f"topk took {per * 1000:.0f} ms"


def test_index_loads_normalizes_skips_bad_rows_and_invalidates(conn):
    a = add_asset(conn, "a.wav")
    b = add_asset(conn, "b.wav")
    fc.add_embedding(conn, a, np.array([3.0] + [0.0] * (fc.DIM - 1)))  # not normalized
    conn.execute("INSERT INTO embeddings(asset_id, model, dim, vector) VALUES (?, ?, ?, ?)",
                 (b, fc.model_key("fake-a"), fc.DIM, b"\0" * 12))  # truncated blob
    key = fc.model_key("fake-a")
    idx = vectors.get_index(conn, key)
    assert len(idx) == 1 and idx.vector(a)[0] == pytest.approx(1.0)
    assert vectors.get_index(conn, key) is idx  # cached
    assert vectors.embedding_models(conn) == {key: 2}
    fc.add_embedding(conn, b, "dog bark")
    idx2 = vectors.get_index(conn, key)
    assert idx2 is not idx and len(idx2) == 2
    assert vectors.get_index(conn, "clap:nothing") is None


def test_index_is_shared_across_threads(cfg, conn):
    for i in range(20):
        fc.add_embedding(conn, add_asset(conn, f"s{i}.wav"), f"sound {i}")
    got, errors = [], []

    def worker():
        try:
            c = db.open_catalog(cfg)
            try:
                got.append(vectors.get_index(c, fc.model_key("fake-a")))
            finally:
                c.close()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(got) == 6 and all(g is got[0] for g in got)


def test_embed_text_caches_and_reports_state(api):
    assert vectors.encoder_state("fake-a") == "unloaded"
    v = vectors.embed_text("fake-a", "metal hit")
    assert v.shape == (fc.DIM,) and np.linalg.norm(v) == pytest.approx(1.0)
    assert vectors.encoder_state("fake-a") == "ready"
    vectors.embed_text("fake-a", "metal hit")
    assert api.encoders["fake-a"].calls == 1  # second call came from the query cache


def test_embed_text_failure_is_remembered(monkeypatch):
    fc.install(monkeypatch, fail="no torch here")
    with pytest.raises(vectors.EncoderUnavailable, match="no torch here"):
        vectors.embed_text("fake-a", "x")
    assert vectors.encoder_state("fake-a") == "failed"
    assert "no torch here" in vectors.encoder_error("fake-a")
    calls = []
    monkeypatch.setattr(vectors, "clap_api", lambda: calls.append(1))
    with pytest.raises(vectors.EncoderUnavailable):
        vectors.embed_text("fake-a", "y")  # not retried within RETRY_S
    assert calls == []


def test_without_models_package(monkeypatch):
    monkeypatch.setattr(vectors, "clap_api", lambda: None)
    vectors._reset_encoders()
    assert vectors.model_key("fake-a") is None and vectors.checkpoints() == []
    assert vectors.default_clap() is None
    with pytest.raises(vectors.EncoderUnavailable, match="not installed"):
        vectors.embed_text("fake-a", "x")
