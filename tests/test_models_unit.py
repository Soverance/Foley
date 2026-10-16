"""foley.models without weights: windowing, the vendored CNN14 front end against
librosa, the clap interface (keys, cached text encoders), config and downloads."""

from __future__ import annotations

import threading

import audiogen as ag
import numpy as np
import pytest

from foley import config, models
from foley.decode import DecodedAudio
from foley.models import ModelUnavailable, clap, panns, prep


def _audio(x: np.ndarray, sr: int) -> DecodedAudio:
    x = np.asarray(x, dtype=np.float32)
    return DecodedAudio(x.reshape(len(x), -1), sr, sr)


# --------------------------------------------------------------------------- windows


def test_window_starts():
    assert prep.window_starts(5, 10) == [0]
    assert prep.window_starts(10, 10) == [0]
    assert prep.window_starts(11, 10) == [0, 1]
    assert prep.window_starts(250, 100) == [0, 75, 150]  # 25 s -> 3 windows, end at n
    starts = prep.window_starts(1300, 100)
    assert len(starts) == 12 and starts[0] == 0 and starts[-1] == 1200
    assert all(b > a for a, b in zip(starts, starts[1:], strict=False))
    assert len(prep.window_starts(10**7, 100)) == prep.MAX_WINDOWS


def test_short_clip_windows():
    a = _audio(ag.tone(440, 0.3), ag.SR)
    (w32,) = prep.panns_windows(a)
    (w48,) = prep.clap_windows(a)
    assert len(w32) == prep.PANNS_MIN_SAMPLES  # padded to CNN14's minimum
    assert np.all(w32[int(0.3 * prep.PANNS_SR):] == 0)
    (w32,) = prep.panns_windows(_audio(ag.tone(440, 0.45), ag.SR))
    assert len(w32) == int(0.45 * prep.PANNS_SR)  # long enough: never padded
    assert len(w48) == int(0.3 * prep.CLAP_SR) and w48.dtype == np.float32


def test_stereo_is_channel_mean():
    left = ag.tone(440, 2.0)
    stereo = np.stack([left, -left * 0.5], axis=1)
    (w,) = prep.clap_windows(DecodedAudio(stereo.astype(np.float32), ag.SR, ag.SR))
    np.testing.assert_allclose(w, left * 0.25, atol=1e-6)


def test_long_file_cut_windows_match_whole_resample(monkeypatch):
    sr = 44100
    x = ag.noise(140.0, -20, sr=sr, seed=7) + ag.tone(1000, 140.0, -20, sr=sr)
    whole = prep.windows(_audio(x, sr), prep.CLAP_SR)
    monkeypatch.setattr(prep, "WHOLE_RESAMPLE_BYTES", 1024)  # force the cut path
    cut = prep.windows(_audio(x, sr), prep.CLAP_SR)
    assert len(whole) == len(cut) == 12
    for a, b in zip(whole, cut, strict=True):
        assert len(a) == len(b) == 10 * prep.CLAP_SR
        inner = slice(1000, -1000)  # away from the resampler's run-in
        assert np.max(np.abs(a[inner] - b[inner])) < 2e-3


# --------------------------------------------------------------------------- CNN14


@pytest.fixture(scope="module")
def cnn14():
    torch = pytest.importorskip("torch")
    torch.manual_seed(0)
    return panns.Cnn14().eval()


def test_cnn14_front_end_matches_librosa(cnn14):
    import librosa
    import torch

    y = (ag.noise(2.0, -20, sr=32000, seed=1) + ag.tone(523, 2.0, -12, sr=32000)).astype(
        np.float32)
    with torch.inference_mode():
        ours = cnn14.logmel_extractor(cnn14.spectrogram_extractor(torch.from_numpy(y[None])))
    ours = ours[0, 0].numpy()  # (frames, mel)
    ref = librosa.feature.melspectrogram(
        y=y, sr=32000, n_fft=1024, hop_length=320, win_length=1024, window="hann",
        center=True, pad_mode="reflect", power=2.0, n_mels=64, fmin=50, fmax=14000,
    )
    ref = librosa.power_to_db(ref, ref=1.0, amin=1e-10, top_db=None).T
    assert ours.shape == ref.shape
    assert np.max(np.abs(ours - ref)) < 0.05  # dB


def test_cnn14_shapes(cnn14):
    import torch

    with torch.inference_mode():
        out = cnn14(torch.zeros(2, prep.PANNS_MIN_SAMPLES))  # the shortest it accepts
    assert tuple(out["clipwise_output"].shape) == (2, panns.N_LABELS)
    assert tuple(out["embedding"].shape) == (2, 2048)
    sd = cnn14.state_dict()
    assert "spectrogram_extractor.stft.conv_real.weight" in sd and "logmel_extractor.melW" in sd


def test_scores_to_tags_and_is_music():
    labels = ["Music", "Speech", "Dog"]
    tags = panns.scores_to_tags(np.array([0.6, 0.05, 0.2]), labels)
    assert tags == [("Music", pytest.approx(0.6)), ("Dog", pytest.approx(0.2))]
    assert panns.is_music(tags) == 1
    assert panns.is_music([("Music", 0.49)]) == 0 and panns.is_music([]) == 0


def test_labels_are_validated():
    rows = "index,mid,display_name\n" + "".join(f"{i},/m/{i},L{i}\n" for i in range(527))
    assert panns.parse_labels(rows)[526] == "L526"
    with pytest.raises(ModelUnavailable):
        panns.parse_labels(rows.splitlines()[0] + "\n0,/m/0,x\n")


def test_checkpoint_download_verifies_md5(tmp_path, monkeypatch):
    src = tmp_path / "src.pth"
    src.write_bytes(b"not the checkpoint")
    monkeypatch.setattr(panns, "CHECKPOINT_URL", src.as_uri())
    cache = tmp_path / "cache"
    with pytest.raises(ModelUnavailable, match="md5"):
        panns.ensure_checkpoint(cache)
    assert not any(cache.iterdir())  # no partial or bogus file left behind
    monkeypatch.setattr(panns, "CHECKPOINT_MD5", __import__("hashlib").md5(
        src.read_bytes()).hexdigest())
    monkeypatch.setattr(panns, "CHECKPOINT_BYTES", src.stat().st_size)
    path = panns.ensure_checkpoint(cache)
    assert path.read_bytes() == src.read_bytes()
    monkeypatch.setattr(panns, "CHECKPOINT_URL", "file:///nowhere/at/all.pth")
    assert panns.ensure_checkpoint(cache) == path  # verified once, not fetched again


# --------------------------------------------------------------------------- clap


def test_model_keys():
    assert set(clap.CLAP_CHECKPOINTS) == set(clap.REVISIONS) == set(clap.LICENSES)
    assert clap.DEFAULT_CLAP in clap.CLAP_CHECKPOINTS
    key = clap.model_key("htsat-unfused")
    assert key == "clap:laion/clap-htsat-unfused@8fa0f1c6d043"
    assert clap.short_name_for_key(key) == "htsat-unfused"
    assert clap.short_name_for_key("clap:other@x") is None
    with pytest.raises(KeyError):
        clap.model_key("nope")


def test_text_encoder_is_cached_lazy_and_thread_safe(fake_models):
    clap.configure(["htsat-fused"])
    try:
        got = []
        threads = [threading.Thread(target=lambda: got.append(clap.text_encoder()))
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len({id(e) for e in got}) == 1
        enc = got[0]
        assert enc.key == clap.model_key("htsat-fused")
        assert clap.text_encoder("htsat-fused") is enc
        assert clap.text_encoder("htsat-unfused") is not enc
        v = enc.embed_text(["dog bark", "rain"])
        assert v.shape == (2, enc.dim) and v.dtype == np.float32
        np.testing.assert_allclose(np.linalg.norm(v, axis=1), 1, atol=1e-5)
    finally:
        clap.configure(None)
    assert clap.text_encoder().key == clap.model_key(clap.DEFAULT_CLAP)
    with pytest.raises(KeyError):
        clap.text_encoder("nope")


def test_set_factory_clears_cached_encoders(fake_models):
    enc = clap.text_encoder()
    models.set_factory(fake_models)
    assert clap.text_encoder() is not enc


def test_resolve_device():
    assert models.resolve_device("cpu") == "cpu"
    if "torch" in models.missing_packages():
        with pytest.raises(ModelUnavailable, match="torch"):
            models.resolve_device("auto")
    else:
        assert models.resolve_device("auto") in ("cpu", "cuda")


# --------------------------------------------------------------------------- config


def _cfg(models_table=None):
    data = {"library": {"root": "X:/lib"}, "storage": {"data_dir": "C:/foley-test-data"}}
    if models_table is not None:
        data["models"] = models_table
    return config.from_dict(data)


def test_models_config():
    c = _cfg()
    assert (c.models_enabled, c.models_device) == (True, "auto")
    assert c.models_clap == [] and config.clap_checkpoints(c) == list(clap.CLAP_CHECKPOINTS)
    c = _cfg({"enabled": False, "device": "CPU", "clap": ["htsat-unfused"]})
    assert (c.models_enabled, c.models_device, c.models_clap) == (False, "cpu", ["htsat-unfused"])
    with pytest.raises(config.ConfigError, match="unknown checkpoint"):
        _cfg({"clap": ["laion/clap-htsat-unfused"]})
    with pytest.raises(config.ConfigError):
        _cfg({"enabled": "yes"})
