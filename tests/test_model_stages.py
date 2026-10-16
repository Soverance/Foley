"""Model stages (tags, embeddings) in `foley index`, with fake models (no torch, no
downloads): stage wiring, `active` gating, resume, caption/FTS refresh, per-checkpoint
embeddings, long-file windowing, batching and failure handling."""

from __future__ import annotations

import functools
import json
from collections import Counter

import audiogen as ag
import numpy as np
import pytest
from _fake_models import DIM, LABELS, FakeFactory, FakeTagger
from conftest import LIBRARY_FILES
from test_sync import _make_fakes

from foley import features, loudness, models, status, sync
from foley.catalog import db
from foley.models import clap, panns, prep

N_AUDIO = len(LIBRARY_FILES)
ALL_KEYS = sorted(clap.model_key(n) for n in clap.CLAP_CHECKPOINTS)
_REAL_RUN_INDEX = sync.run_index


@pytest.fixture
def workers() -> int:
    return 1


@pytest.fixture
def fast(monkeypatch, workers):
    """Real decode, captions and FTS; fake features/loudness (librosa is slow).
    Fakes live in this process, so these tests analyze in-process unless parametrized."""
    fakes = _make_fakes(lambda name: None)
    for module, name in ((features, "compute_features"), (loudness, "measure")):
        monkeypatch.setattr(module, name, fakes[(module, name)])
    monkeypatch.setattr(sync, "run_index", functools.partial(_REAL_RUN_INDEX, workers=workers))


@pytest.fixture
def factory(fake_models) -> FakeFactory:
    return fake_models


def _conn(cfg):
    return db.open_catalog(cfg)


def _q(cfg, sql, *args):
    conn = _conn(cfg)
    try:
        return [tuple(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def _statuses(cfg) -> Counter:
    return Counter(r[0] for r in _q(cfg, "SELECT status FROM assets"))


def _id(cfg, rel) -> int:
    return _q(cfg, "SELECT id FROM assets WHERE rel_path=?", rel)[0][0]


MUSIC = "music/Arteria3D/Battle Loop.mp3"


def test_first_run_tags_and_embeds_everything(cfg, fast, factory):
    s = sync.run_index(cfg)
    assert (s.analyzed, s.errors, s.notes) == (N_AUDIO, 0, [])
    assert _statuses(cfg) == Counter(active=N_AUDIO)
    stages = Counter(r[0] for r in _q(cfg, "SELECT stage FROM asset_stages"))
    assert stages == Counter({st: N_AUDIO for st in sync.STAGES})
    assert {r[0] for r in _q(cfg, "SELECT model FROM tags")} == {panns.MODEL_KEY}
    assert min(r[0] for r in _q(cfg, "SELECT score FROM tags")) >= panns.TAG_MIN_SCORE
    emb = _q(cfg, "SELECT model, dim, vector FROM embeddings")
    assert Counter(r[0] for r in emb) == Counter({k: N_AUDIO for k in ALL_KEYS})
    for _, dim, blob in emb:
        v = np.frombuffer(blob, dtype="<f4")
        assert dim == DIM == len(v) and abs(np.linalg.norm(v) - 1) < 1e-5
    music = dict(_q(cfg, "SELECT a.rel_path, f.is_music FROM assets a "
                         "JOIN features f ON f.asset_id = a.id"))
    assert music.pop(MUSIC) == 1 and set(music.values()) == {0}
    # tags reach FTS (every file has the fake's confident "Tick" tag)
    assert len(_q(cfg, "SELECT rowid FROM search_fts WHERE search_fts MATCH 'tick'")) == N_AUDIO
    st = _status(cfg)["models"]
    assert st["tags"] == {"model": panns.MODEL_KEY, "done": N_AUDIO, "pending": 0}
    assert {k: v["done"] for k, v in st["embeddings"].items()} == {k: N_AUDIO for k in ALL_KEYS}
    prov = st["provenance"]
    assert prov["tags"]["license"] == "CC-BY-4.0"
    assert {v["license"] for v in prov["embeddings"].values()} == {"Apache-2.0"}
    # a second run has nothing to do
    factory.calls.clear()
    s = sync.run_index(cfg)
    assert s.analyzed == 0 and factory.calls == []


def _status(cfg) -> dict:
    conn = _conn(cfg)
    try:
        return status.catalog_status(cfg, conn)
    finally:
        conn.close()


def test_models_disabled_runs_core_only_and_assets_are_active(cfg, fast, factory,
                                                              monkeypatch):
    cfg.models_enabled = False
    s = sync.run_index(cfg)
    assert s.analyzed == N_AUDIO and factory.loads == []
    assert _statuses(cfg) == Counter(active=N_AUDIO)
    assert _q(cfg, "SELECT count(*) FROM tags")[0][0] == 0
    st = _status(cfg)["models"]
    assert st["tags"]["pending"] == N_AUDIO
    assert all(v["pending"] == N_AUDIO for v in st["embeddings"].values())
    # enabling the models later backfills only the model stages; assets stay active
    cfg.models_enabled = True
    calls = Counter()
    real = features.compute_features
    monkeypatch.setattr(features, "compute_features",
                        lambda *a: calls.update(["features"]) or real(*a))
    s = sync.run_index(cfg)
    assert s.analyzed == N_AUDIO and s.errors == 0
    assert calls["features"] == 0  # only the model stages ran
    assert _statuses(cfg) == Counter(active=N_AUDIO)
    assert _status(cfg)["models"]["tags"]["pending"] == 0


def test_backfill_interrupted_keeps_assets_active_and_resumes(cfg, fast, factory, monkeypatch):
    cfg.models_enabled = False
    sync.run_index(cfg)
    cfg.models_enabled = True
    seen = {"n": 0}
    orig = FakeTagger.tag

    def interrupting(self, clips):
        seen["n"] += 1
        if seen["n"] == 5:
            raise KeyboardInterrupt
        return orig(self, clips)

    monkeypatch.setattr(FakeTagger, "tag", interrupting)
    s = sync.run_index(cfg, stages=["tags"])
    assert s.interrupted and s.analyzed == 4
    assert _statuses(cfg) == Counter(active=N_AUDIO)  # search keeps working meanwhile
    assert _status(cfg)["models"]["tags"]["done"] == 4
    s = sync.run_index(cfg, stages=["tags"])
    assert s.analyzed == N_AUDIO - 4 and not s.interrupted
    assert _status(cfg)["models"]["tags"]["done"] == N_AUDIO


def test_unloadable_models_are_skipped_before_any_read(cfg, fast, monkeypatch):
    f = FakeFactory(fail={"tags", *clap.CLAP_CHECKPOINTS})
    models.set_factory(f)
    lines: list[str] = []
    s = sync.run_index(cfg, progress=lines.append)
    assert s.analyzed == N_AUDIO and s.errors == 0
    assert _statuses(cfg) == Counter(active=N_AUDIO)
    assert any("tags skipped" in n for n in s.notes) and any("CLAP" in n for n in s.notes)
    note = _q(cfg, "SELECT note FROM index_runs ORDER BY id DESC LIMIT 1")[0][0]
    assert "tags skipped" in note
    assert any(line.startswith("warning: tags skipped") for line in lines)
    # nothing to do but the model stages: files are not read at all
    reads = Counter()
    from foley import decode

    real = decode.read_bytes
    monkeypatch.setattr(decode, "read_bytes", lambda p: reads.update(["r"]) or real(p))
    s = sync.run_index(cfg)
    assert s.analyzed == 0 and reads["r"] == 0
    # one checkpoint failing does not stop the others
    models.set_factory(FakeFactory(fail={"htsat-fused"}))
    s = sync.run_index(cfg)
    assert s.analyzed == N_AUDIO and reads["r"] == N_AUDIO
    keys = Counter(r[0] for r in _q(cfg, "SELECT model FROM embeddings"))
    assert clap.model_key("htsat-fused") not in keys and len(keys) == 2
    assert _status(cfg)["models"]["embeddings"][clap.model_key("htsat-fused")]["pending"] == N_AUDIO
    models.set_factory(f2 := FakeFactory())  # healthy: only the missing checkpoint runs
    s = sync.run_index(cfg)
    assert s.analyzed == N_AUDIO
    embedded = {c[1] for c in f2.calls if c[0] == "embed"}
    assert embedded == {clap.model_key("htsat-fused")}
    assert not [c for c in f2.calls if c[0] == "tag"]


def test_checkpoint_list_changes(cfg, fast, factory):
    cfg.models_clap = ["htsat-unfused"]
    sync.run_index(cfg)
    assert {r[0] for r in _q(cfg, "SELECT model FROM embeddings")} == {
        clap.model_key("htsat-unfused")}
    factory.calls.clear()
    cfg.models_clap = ["larger-general", "htsat-unfused"]
    s = sync.run_index(cfg)
    assert s.analyzed == N_AUDIO
    assert {c[1] for c in factory.calls if c[0] == "embed"} == {clap.model_key("larger-general")}
    assert not [c for c in factory.calls if c[0] == "tag"]
    # dropping a checkpoint re-runs nothing and keeps its rows
    factory.calls.clear()
    cfg.models_clap = ["larger-general"]
    assert sync.run_index(cfg).analyzed == 0 and factory.calls == []
    assert len({r[0] for r in _q(cfg, "SELECT model FROM embeddings")}) == 2
    # --force recomputes the configured checkpoints
    s = sync.run_index(cfg, stages=["embeddings"], force=True)
    assert s.analyzed == N_AUDIO
    assert {c[1] for c in factory.calls if c[0] == "embed"} == {clap.model_key("larger-general")}


def test_tags_refresh_caption_and_fts(cfg, fast, factory, library, monkeypatch):
    ag.write(library / "sfx" / "001.wav", ag.tone(880, 0.5))  # no name words
    import os

    os.utime(library / "sfx" / "001.wav", (1_700_000_000, 1_700_000_000))
    sync.run_index(cfg)
    aid = _id(cfg, "sfx/001.wav")
    cap = _q(cfg, "SELECT text FROM captions WHERE asset_id=?", aid)[0][0]
    assert "tick" in cap  # best tag >= 0.3 is the noun phrase
    caption_version = _q(cfg, "SELECT version, done_at FROM asset_stages "
                              "WHERE asset_id=? AND stage='caption'", aid)[0]

    orig = FakeTagger.tag

    def rainy(self, clips):
        out = orig(self, clips)
        out[:, LABELS.index("Tick")] = 0.0
        out[:, LABELS.index("Rain")] = 0.8
        return out

    monkeypatch.setattr(FakeTagger, "tag", rainy)
    s = sync.run_index(cfg, stages=["tags"], force=True)
    assert s.analyzed == N_AUDIO + 1
    cap = _q(cfg, "SELECT text FROM captions WHERE asset_id=?", aid)[0][0]
    assert "rain" in cap and "tick" not in cap
    labels = {r[0] for r in _q(cfg, "SELECT label FROM tags WHERE asset_id=?", aid)}
    assert "Rain" in labels and "Tick" not in labels
    hits = {r[0] for r in _q(cfg, "SELECT rowid FROM search_fts WHERE search_fts MATCH 'rain'")}
    assert aid in hits
    assert not _q(cfg, "SELECT rowid FROM search_fts WHERE search_fts MATCH 'tick'")
    # the caption stage record is untouched (its version is still current)
    assert _q(cfg, "SELECT version, done_at FROM asset_stages "
                   "WHERE asset_id=? AND stage='caption'", aid)[0] == caption_version
    # a caption-only re-run reads the stored tags
    sync.run_index(cfg, stages=["caption"], force=True)
    assert "rain" in _q(cfg, "SELECT text FROM captions WHERE asset_id=?", aid)[0][0]


def test_long_and_short_files_are_windowed(cfg, fast, factory, library):
    import os

    long_path = library / "ambiance" / "long_wind.wav"
    ag.write(long_path, ag.noise(130.0, -30, sr=16000), sr=16000)
    mid_path = library / "ambiance" / "mid_wind.wav"
    ag.write(mid_path, ag.noise(25.0, -30, sr=16000), sr=16000)
    for p in (long_path, mid_path):
        os.utime(p, (1_700_000_000, 1_700_000_000))
    sync.run_index(cfg)
    tag_clips = [c for call in factory.calls if call[0] == "tag" for c in call[1]]
    emb_clips = [c for call in factory.calls if call[0] == "embed" for c in call[2]]
    win32, win48 = 10 * prep.PANNS_SR, 10 * prep.CLAP_SR
    assert [win32] * 12 in tag_clips and [win48] * 12 in emb_clips  # 130 s: capped at 12
    assert [win32] * 3 in tag_clips and [win48] * 3 in emb_clips  # 25 s: 3 windows
    # very short clips: padded to CNN14's minimum, left short for CLAP (it repeat-pads)
    assert [prep.PANNS_MIN_SAMPLES] in tag_clips
    assert any(len(c) == 1 and c[0] < prep.CLAP_SR for c in emb_clips)


@pytest.mark.parametrize("workers", [2])
def test_parallel_path_batches_inference(cfg, fast, factory, workers):
    s = sync.run_index(cfg)
    assert s.analyzed == N_AUDIO and s.errors == 0
    tag_batches = [len(call[1]) for call in factory.calls if call[0] == "tag"]
    assert sum(tag_batches) == N_AUDIO and max(tag_batches) > 1
    assert Counter(r[0] for r in _q(cfg, "SELECT model FROM embeddings")) == Counter(
        {k: N_AUDIO for k in ALL_KEYS})


def test_model_failure_on_one_file_is_an_error_not_a_status(cfg, fast, factory, monkeypatch):
    orig = FakeTagger.tag

    def picky(self, clips):
        if any(len(ws) == 1 and len(ws[0]) == prep.PANNS_MIN_SAMPLES for ws in clips):
            raise RuntimeError("CUDA error: device-side assert")
        return orig(self, clips)

    monkeypatch.setattr(FakeTagger, "tag", picky)
    s = sync.run_index(cfg)
    assert s.errors >= 1 and s.analyzed == N_AUDIO
    assert _statuses(cfg) == Counter(active=N_AUDIO)
    bad = _q(cfg, "SELECT rel_path, error, error_count FROM assets WHERE error IS NOT NULL")
    assert bad and all("CUDA error" in e and n == 1 for _, e, n in bad)
    # their tags stay pending; embeddings were stored
    st = _status(cfg)["models"]
    assert st["tags"]["pending"] == len(bad)
    assert all(v["pending"] == 0 for v in st["embeddings"].values())


def test_content_change_drops_model_outputs(cfg, fast, factory, library):
    sync.run_index(cfg)
    aid = _id(cfg, MUSIC)
    assert _q(cfg, "SELECT count(*) FROM tags WHERE asset_id=?", aid)[0][0] > 0
    import os

    path = library / MUSIC
    path.write_bytes(path.read_bytes() + b"\x00" * 128)  # size changes
    os.utime(path, (1_700_000_100, 1_700_000_100))
    cfg.models_enabled = False
    sync.run_index(cfg)
    assert _q(cfg, "SELECT count(*) FROM tags WHERE asset_id=?", aid)[0][0] == 0
    assert _q(cfg, "SELECT count(*) FROM embeddings WHERE asset_id=?", aid)[0][0] == 0
    assert _q(cfg, "SELECT is_music FROM features WHERE asset_id=?", aid)[0][0] is None
    assert _q(cfg, "SELECT status FROM assets WHERE id=?", aid)[0][0] == "active"


def test_stage_versions_and_currency():
    v = sync.stage_versions()
    assert v["tags"].endswith(panns.MODEL_KEY)
    assert sync.stage_current("embeddings", sync.embed_version(ALL_KEYS), v)
    assert sync.stage_current("embeddings", sync.embed_version(ALL_KEYS + ["clap:x@y"]), v)
    assert not sync.stage_current("embeddings", sync.embed_version(ALL_KEYS[:2]), v)
    assert not sync.stage_current("embeddings", None, v)
    assert not sync.stage_current("embeddings", "0|" + ",".join(ALL_KEYS), v)
    assert sync.clap_keys_needed(sync.embed_version(ALL_KEYS[:2]), v, False) == ALL_KEYS[2:]
    assert sync.clap_keys_needed(sync.embed_version(ALL_KEYS), v, True) == ALL_KEYS


def test_unknown_stage_lists_model_stages(cfg, fast):
    with pytest.raises(ValueError, match="tags, embeddings"):
        sync.run_index(cfg, stages=["nope"])


def test_meta_provenance_is_json(cfg, fast, factory):
    sync.run_index(cfg)
    conn = _conn(cfg)
    try:
        meta = json.loads(db.get_meta(conn, "models"))
    finally:
        conn.close()
    assert meta["tags"]["model"] == panns.MODEL_KEY
    assert set(meta["embeddings"]) == set(ALL_KEYS)
    assert meta["embeddings"][clap.model_key("larger-general")]["repo"] == (
        "laion/larger_clap_general")
