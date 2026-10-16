"""Real weights (deselected by default; run with `uv run pytest -m models`). Downloads
the PANNs checkpoint (~330 MB, Zenodo) and the CLAP checkpoints (~0.6-0.8 GB each, Hugging
Face) on first use. Library-file checks against the reference code: eval/verify_models.py."""

from __future__ import annotations

import os
import shutil

import audiogen as ag
import numpy as np
import pytest

from foley import models
from foley.decode import DecodedAudio
from foley.models import clap, panns, prep

pytestmark = [
    pytest.mark.models,
    pytest.mark.skipif(bool(models.missing_packages()),
                       reason="needs the optional `models` extra (torch, transformers)"),
]


@pytest.fixture(scope="module")
def device() -> str:
    return models.resolve_device("auto")


@pytest.fixture(scope="module")
def tagger(device):
    from foley.config import default_data_dir

    # the user's model cache, so the checkpoint is downloaded once, not per test run
    return panns.PannsTagger(default_data_dir() / "models" / "panns", device)


def _audio(x, sr=ag.SR) -> DecodedAudio:
    x = np.asarray(x, dtype=np.float32)
    return DecodedAudio(x.reshape(len(x), 1), sr, sr)


def test_checkpoint_front_end_is_the_reference_one(tagger):
    fresh = panns.Cnn14().state_dict()
    loaded = tagger.model.state_dict()
    for k in ("spectrogram_extractor.stft.conv_real.weight",
              "spectrogram_extractor.stft.conv_imag.weight", "logmel_extractor.melW"):
        assert float((fresh[k] - loaded[k].cpu()).abs().max()) < 1e-4, k
    assert len(tagger.labels) == panns.N_LABELS and tagger.labels[0] == "Speech"


def test_white_noise_is_tagged_as_noise(tagger):
    (scores,) = tagger.tag([prep.panns_windows(_audio(ag.noise(3.0, -12)))])
    labels = [lab for lab, _ in panns.scores_to_tags(scores, tagger.labels)]
    assert {"White noise", "Noise", "Static"} <= set(labels[:4]), labels
    assert panns.is_music(panns.scores_to_tags(scores, tagger.labels)) == 0


def test_windows_of_a_short_clip_equal_the_whole_clip(tagger, device):
    import torch

    y = ag.noise(4.0, -20, sr=32000, seed=3).astype(np.float32)
    with torch.inference_mode():
        ref = tagger.model(torch.from_numpy(y[None]).to(device))["clipwise_output"][0]
    (ours,) = tagger.tag([[y]])
    np.testing.assert_allclose(ours, ref.float().cpu().numpy(), atol=1e-5)


@pytest.mark.parametrize("name", list(clap.CLAP_CHECKPOINTS))
def test_clap_towers_match_clap_model(name, device):
    import torch
    import transformers as tf

    repo, rev = clap.CLAP_CHECKPOINTS[name], clap.REVISIONS[name]
    full = tf.ClapModel.from_pretrained(repo, revision=rev).to(device).eval()
    fe = tf.ClapFeatureExtractor.from_pretrained(repo, revision=rev)
    tok = tf.AutoTokenizer.from_pretrained(repo, revision=rev)
    emb = clap.ClapAudioEmbedder(name, device)
    txt = clap.ClapTextEncoder(name, device)
    texts = ["a dog barking", "rain on a tin roof"]
    with torch.inference_mode():
        ref_t = full.get_text_features(**tok(texts, padding=True, return_tensors="pt").to(device))
    ref_t = ref_t.pooler_output if hasattr(ref_t, "pooler_output") else ref_t
    np.testing.assert_allclose(txt.embed_text(texts), ref_t.float().cpu().numpy(), atol=1e-4)
    y = ag.noise(6.0, -20, seed=2).astype(np.float32)
    inputs = fe(y, sampling_rate=48000, return_tensors="pt")
    if emb.fusion:  # the reference flags a lone clip as "longer" (see foley.models.clap)
        inputs["is_longer"] = torch.ones_like(inputs["is_longer"])
    with torch.inference_mode():
        ref_a = full.get_audio_features(**{k: v.to(device) for k, v in inputs.items()})
    ref_a = ref_a.pooler_output if hasattr(ref_a, "pooler_output") else ref_a
    (ours,) = emb.embed([prep.clap_windows(_audio(y))])
    assert float(ours @ ref_a[0].float().cpu().numpy()) > 0.9999
    assert emb.dim == txt.dim == ours.shape[0] and emb.key == txt.key == clap.model_key(name)


@pytest.mark.parametrize("workers", [1, 2])
def test_indexer_vectors_equal_direct_embedding(cfg, library, device, workers):
    """Worker-side feature extraction (workers=2) and in-process (1) give the vectors a
    direct ClapAudioEmbedder.embed / PannsTagger.tag call gives."""
    from foley import decode, sync
    from foley.catalog import db
    from foley.config import default_data_dir

    cfg.models_clap = ["htsat-unfused", "htsat-fused"]
    # link the user's PANNs download into this temp data_dir instead of fetching it again
    cache = cfg.data_dir / "models" / "panns"
    cache.mkdir(parents=True)
    for f in (default_data_dir() / "models" / "panns").glob("*"):
        if f.is_file():
            try:
                os.link(f, cache / f.name)
            except OSError:
                shutil.copyfile(f, cache / f.name)
    s = sync.run_index(cfg, workers=workers)
    assert s.errors == 0 and not s.notes, s
    rel = "packs/Test Bundle 2015/Metal Shelf/Metal Shelf Tonal Texture 07.wav"
    conn = db.open_catalog(cfg)
    try:
        aid = conn.execute("SELECT id FROM assets WHERE rel_path=?", (rel,)).fetchone()[0]
        rows = dict(conn.execute(
            "SELECT model, vector FROM embeddings WHERE asset_id=?", (aid,)).fetchall())
    finally:
        conn.close()
    audio = decode.decode_path(cfg.abs_path(rel))
    for name in cfg.models_clap:
        emb = clap.ClapAudioEmbedder(name, device)
        (direct,) = emb.embed([prep.clap_windows(audio)])
        stored = np.frombuffer(rows[clap.model_key(name)], dtype="<f4")
        assert float(stored @ direct) > 0.9999, name
