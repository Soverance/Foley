from __future__ import annotations

import json
import math
import os
import time

import audiogen as ag
import numpy as np
import pytest

from foley import decode, features, loudness
from foley.decode import DecodedAudio

KEYS = {
    "duration_s", "sample_rate", "channels", "lead_silence_s", "tail_silence_s", "attack_s",
    "decay_s", "onset_count", "centroid_hz_mean", "rolloff_hz_mean", "flatness_mean",
    "zcr_mean", "band_energy", "tempo_bpm", "loop_seam_score",
}
BAND_KEYS = {"sub", "low", "low_mid", "mid", "upper_mid", "presence", "air"}


def _audio(x: np.ndarray, sr: int = ag.SR) -> DecodedAudio:
    x = np.ascontiguousarray(x.reshape(len(x), -1), dtype=np.float32)
    return DecodedAudio(x, sr, sr)


def _check_clean(f: dict) -> None:
    assert set(f) == KEYS
    json.dumps(f)  # plain Python types only (sqlite/JSON friendly)
    for k, v in f.items():
        if k == "band_energy" and v is not None:
            assert set(v) == BAND_KEYS
            assert sum(v.values()) == pytest.approx(1.0, abs=1e-9)
            assert all(isinstance(s, float) and math.isfinite(s) and s >= 0 for s in v.values())
        elif v is not None and k != "band_energy":
            assert isinstance(v, (int, float)) and not isinstance(v, bool), k
            assert math.isfinite(v), k


def _env_tone(attack: float, tau: float, dur: float, freq: float = 1000.0) -> np.ndarray:
    t = np.arange(int(dur * ag.SR)) / ag.SR
    env = np.where(t < attack, t / attack, np.exp(-(t - attack) / tau))
    return (0.5 * env * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def test_keys_and_basic_values():
    f = features.compute_features(_audio(ag.stereo(ag.tone(440, 2.0, -12))), "sfx")
    _check_clean(f)
    assert f["duration_s"] == pytest.approx(2.0)
    assert f["sample_rate"] == ag.SR and f["channels"] == 2
    assert f["tempo_bpm"] is None and f["loop_seam_score"] is None
    assert f["centroid_hz_mean"] == pytest.approx(440, rel=0.1)
    assert f["onset_count"] == 1


def test_lead_and_tail_silence():
    x = ag.pad(ag.tone(1000, 0.5, -6), lead=0.25, tail=0.4)
    f = features.compute_features(_audio(x), None)
    assert f["lead_silence_s"] == pytest.approx(0.25, abs=0.002)
    assert f["tail_silence_s"] == pytest.approx(0.4, abs=0.002)


def test_silence_threshold_is_minus_60_dbfs():
    quiet = ag.pad(ag.tone(1000, 0.5, -70), lead=0.1)
    f = features.compute_features(_audio(quiet), "ambience")
    _check_clean(f)
    assert f["lead_silence_s"] is None and f["band_energy"] is None


def test_attack_and_decay():
    x = ag.pad(_env_tone(attack=0.05, tau=0.1, dur=1.5), lead=0.1)
    f = features.compute_features(_audio(x), "sfx")
    # linear 50 ms ramp: 10% at 5 ms, "peak" (within 1 dB of max) at 44.6 ms
    assert f["attack_s"] == pytest.approx(0.0396, abs=0.004)
    # 40 dB down on exp(-t/0.1) takes 0.1 * ln(100) = 0.46 s
    assert f["decay_s"] == pytest.approx(0.1 * math.log(100), abs=0.02)


def test_sharp_vs_slow_attack():
    sharp = features.compute_features(_audio(ag.burst(0.5, 0.001, 0.05)), "sfx")
    slow = features.compute_features(_audio(_env_tone(0.4, 0.2, 2.0)), "sfx")
    assert sharp["attack_s"] < 0.01
    assert slow["attack_s"] > 0.3


@pytest.mark.parametrize(
    "freq, band", [(30, "sub"), (120, "low"), (1000, "mid"), (3000, "upper_mid"), (9000, "air")]
)
def test_band_energy(freq, band):
    f = features.compute_features(_audio(ag.tone(freq, 1.0, -12)), None)
    _check_clean(f)
    assert max(f["band_energy"], key=f["band_energy"].get) == band
    assert f["band_energy"][band] > 0.95


@pytest.mark.parametrize(
    "name, x",
    [
        ("click", ag.click()),
        ("silence", np.zeros(ag.SR, np.float32)),
        ("dc", np.full(ag.SR, 0.5, np.float32)),
        ("tiny", np.full(10, 0.5, np.float32)),
        ("one_sample", np.array([0.5], np.float32)),
        ("eight_channels", np.tile(ag.noise(1.0, -20)[:, None], (1, 8))),
        ("antiphase", np.stack([ag.tone(440, 1.0), -ag.tone(440, 1.0)], axis=1)),
    ],
)
@pytest.mark.parametrize("kind", ["music", "ambience", "sfx", None])
def test_edge_cases_do_not_crash(name, x, kind):
    f = features.compute_features(_audio(x), kind)
    _check_clean(f)
    if name == "silence":
        assert all(f[k] is None for k in KEYS - {"duration_s", "sample_rate", "channels",
                                                 "onset_count"})
        assert f["onset_count"] == 0
    if name == "click":
        assert f["attack_s"] is not None and f["attack_s"] < 0.005
    if name == "eight_channels":
        assert f["channels"] == 8


def test_onset_count():
    x = ag.noise(4.0, -45)
    for t in (0.5, 1.3, 2.0, 2.5, 3.1):
        i = int(t * ag.SR)
        b = ag.burst(0.3, 0.002, 0.03, seed=int(t * 10))
        x[i : i + len(b)] += 0.5 * b
    f = features.compute_features(_audio(x), "sfx")
    assert f["onset_count"] == 6  # the noise floor starting at t=0, plus five hits
    steady = features.compute_features(_audio(ag.noise(3.0, -20)), "ambience")
    assert steady["onset_count"] == 1


def test_tempo_only_for_music():
    x = np.zeros(ag.SR * 8, np.float32)
    for k in range(16):  # 120 BPM
        b = ag.burst(0.15, 0.001, 0.03, seed=k)
        i = k * ag.SR // 2
        x[i : i + len(b)] += b
    x += ag.tone(110, 8.0, -24)
    music = features.compute_features(_audio(x), "music")
    assert music["tempo_bpm"] == pytest.approx(120, abs=4)
    assert features.compute_features(_audio(x), "sfx")["tempo_bpm"] is None
    assert features.compute_features(_audio(x), "ambience")["tempo_bpm"] is None


def test_loop_seam_score():
    sr = ag.SR
    seamless = ag.tone(100, 2.0, -6)  # 200 whole cycles: wraps cleanly
    cut = ag.tone(100, 2.0025, -6)  # ends a quarter cycle early: a click at the seam
    steady = ag.noise(4.0, -20)
    faded = steady * np.linspace(1.0, 0.0, len(steady), dtype=np.float32) ** 3
    s = {
        k: features.compute_features(_audio(v, sr), "ambience")["loop_seam_score"]
        for k, v in {"seamless": seamless, "cut": cut, "steady": steady, "faded": faded}.items()
    }
    assert all(0.0 <= v <= 1.0 for v in s.values())
    assert s["seamless"] < 0.1
    assert s["cut"] > s["seamless"] + 0.2
    assert s["faded"] > s["steady"] + 0.2
    assert features.compute_features(_audio(seamless), "sfx")["loop_seam_score"] is None
    assert features.compute_features(_audio(seamless), "voice")["loop_seam_score"] is None
    assert features.compute_features(_audio(seamless), "music")["loop_seam_score"] is not None


def test_high_rate_analysis_is_capped_at_48k():
    sr = 96000
    x = ag.stereo(ag.tone(1000, 1.0, -12, sr=sr) + ag.tone(30000, 1.0, -12, sr=sr))
    audio = _audio(x, sr)
    f = features.compute_features(audio, "sfx")
    _check_clean(f)
    assert f["sample_rate"] == sr
    # the 30 kHz partial is above the 24 kHz analysis Nyquist: centroid stays near 1 kHz
    assert f["centroid_hz_mean"] == pytest.approx(1000, rel=0.15)
    assert f["band_energy"]["mid"] > 0.95


def test_long_file_chunks_consistently():
    """Spectral features of a > 1 chunk file match the same content analysed short."""
    short = ag.noise(5.0, -20)
    long = np.tile(short, 6)  # 30 s, more than one STFT chunk
    a = features.compute_features(_audio(short), "ambience")
    b = features.compute_features(_audio(long), "ambience")
    for k in ("centroid_hz_mean", "rolloff_hz_mean", "flatness_mean", "zcr_mean"):
        assert b[k] == pytest.approx(a[k], rel=0.02), k
    assert b["onset_count"] <= 3


def test_fixture_library_end_to_end(library):
    for p in library.rglob("*"):
        if p.suffix.lower() not in {".wav", ".flac", ".ogg", ".mp3"}:
            continue
        audio = decode.decode_path(p)
        kind = "music" if "music" in p.parts else "ambience" if "_lp" in p.name else "sfx"
        _check_clean(features.compute_features(audio, kind))
        m = loudness.measure(audio)
        assert all(v is None or math.isfinite(v) for v in m.values())


def test_typical_file_is_fast(capsys):
    """3 s 48 kHz stereo: decode + features + loudness, warm, well under 0.3 s.

    Shared CI runners are several times slower and noisy, so under CI (the `CI`
    environment variable, set by GitHub Actions) the budget only catches
    pathological regressions."""
    import io

    import soundfile as sf

    buf = io.BytesIO()
    x = ag.stereo(ag.noise(3.0, -20), ag.tone(300, 3.0, -20))
    sf.write(buf, x, ag.SR, format="WAV", subtype="PCM_24")
    data = buf.getvalue()

    def run(kind):
        t = time.perf_counter()
        audio = decode.decode_bytes(data, ".wav")
        features.compute_features(audio, kind)
        loudness.measure(audio)
        decode.audio_hash(audio)
        return time.perf_counter() - t

    budget = 2.0 if os.environ.get("CI") else 0.3
    for kind in ("sfx", "music"):
        run(kind)  # warm-up: librosa lazy imports and numba compilation
        best = min(run(kind) for _ in range(3))
        with capsys.disabled():
            print(f"\n3 s stereo 48 kHz, {kind}: {best * 1000:.1f} ms")
        assert best < budget
