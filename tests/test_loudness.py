from __future__ import annotations

import math
import re
import shutil
import subprocess

import audiogen as ag
import numpy as np
import pyloudnorm
import pytest

from foley import loudness
from foley.decode import DecodedAudio, decode_path


def _audio(x: np.ndarray, sr: int = ag.SR) -> DecodedAudio:
    x = np.ascontiguousarray(x.reshape(len(x), -1), dtype=np.float32)
    return DecodedAudio(x, sr, sr)


def _pyln(x: np.ndarray, sr: int) -> float:
    return pyloudnorm.Meter(sr).integrated_loudness(x.astype(np.float64))


@pytest.mark.parametrize(
    "x",
    [
        ag.tone(440, 3.0, -14),
        ag.noise(2.0, -20),
        ag.pad(ag.burst(0.6), 0.02, 1.0),
        ag.stereo(ag.noise(4.0, -18), ag.tone(1000, 4.0, -24)),
        np.concatenate([ag.noise(7.0, -30), ag.tone(200, 5.0, -12)]),  # > one chunk
    ],
    ids=["tone", "noise", "burst", "stereo", "long"],
)
def test_integrated_matches_pyloudnorm(x):
    got = loudness.measure(_audio(x))["lufs_integrated"]
    assert got == pytest.approx(_pyln(x, ag.SR), abs=0.02)


def test_matches_pyloudnorm_at_other_rates():
    for sr in (44100, 96000, 22050):
        x = ag.stereo(ag.noise(2.5, -20, sr=sr), ag.noise(2.5, -26, sr=sr, seed=9))
        got = loudness.measure(_audio(x, sr))["lufs_integrated"]
        assert got == pytest.approx(_pyln(x, sr), abs=0.02), sr


def test_short_clip_has_no_integrated_but_has_peak():
    m = loudness.measure(_audio(ag.tone(1000, 0.3, -6)))
    assert m["lufs_integrated"] is None
    assert m["lufs_short_max"] is None
    assert m["true_peak_db"] == pytest.approx(-6.0, abs=0.1)


def test_silence_is_none_not_inf():
    m = loudness.measure(_audio(np.zeros(ag.SR * 4, np.float32)))
    assert m == {"lufs_integrated": None, "lufs_short_max": None, "true_peak_db": None}


def test_short_term_max_finds_loud_section():
    x = np.concatenate([ag.noise(10.0, -40), ag.noise(3.0, -10, seed=2), ag.noise(10.0, -40)])
    m = loudness.measure(_audio(x))
    loud_only = _pyln(ag.noise(3.0, -10, seed=2), ag.SR)
    assert m["lufs_short_max"] == pytest.approx(loud_only, abs=0.3)
    assert m["lufs_short_max"] > m["lufs_integrated"]


def test_short_term_for_clip_under_3s_is_whole_clip():
    x = ag.tone(997, 2.0, -20)
    m = loudness.measure(_audio(x))
    assert m["lufs_short_max"] == pytest.approx(m["lufs_integrated"], abs=0.05)


def test_true_peak_catches_intersample_peak():
    sr = ag.SR
    n = np.arange(sr)
    x = (0.5 * np.sin(2 * np.pi * (sr / 4) * n / sr + np.pi / 4)).astype(np.float32)
    sample_peak_db = 20 * math.log10(float(np.max(np.abs(x))))
    tp = loudness.measure(_audio(x))["true_peak_db"]
    assert sample_peak_db == pytest.approx(-9.03, abs=0.05)
    assert tp == pytest.approx(-6.02, abs=0.2)


def test_multichannel_weights():
    x = np.tile(ag.noise(2.0, -20)[:, None], (1, 6))
    m6 = loudness.measure(_audio(x))
    assert m6["lufs_integrated"] is not None
    m8 = loudness.measure(_audio(np.tile(ag.noise(2.0, -20)[:, None], (1, 8))))
    assert m8["lufs_integrated"] > m6["lufs_integrated"]


def test_gain_for_target_rules():
    measured = {"lufs_integrated": -20.0, "lufs_short_max": -18.0, "true_peak_db": -6.0}
    assert loudness.gain_for_target(measured, 2.0, -14.0, -1.0) == pytest.approx(6.0)
    assert loudness.gain_for_target(measured, 0.2, -14.0, -1.0) == pytest.approx(5.0)
    assert loudness.gain_for_target(measured, 2.0, None, -1.0) is None
    assert loudness.gain_for_target(measured, 0.2, -14.0, None) is None
    assert loudness.gain_for_target({"lufs_integrated": None}, 2.0, -14.0, -1.0) is None


def test_apply_gain():
    x = ag.stereo(ag.tone(440, 0.1, -12))
    y = loudness.apply_gain(x, 6.0)
    assert y.dtype == np.float32 and y.shape == x.shape
    assert np.max(np.abs(y)) == pytest.approx(np.max(np.abs(x)) * 10 ** (6 / 20), rel=1e-5)
    assert x is not y


# ------------------------------------------------------------------ ffmpeg agreement

FFMPEG = shutil.which("ffmpeg")


def _ebur128(path) -> dict:
    proc = subprocess.run(
        [FFMPEG, "-nostats", "-hide_banner", "-i", str(path),
         "-af", "ebur128=peak=true", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    )
    log = proc.stderr
    summary = log[log.rindex("Summary:"):]
    integrated = float(re.search(r"I:\s+(-?[\d.]+|-inf) LUFS", summary).group(1))
    peak = float(re.search(r"Peak:\s+(-?[\d.]+|-inf) dBFS", summary).group(1))
    shorts = [float(v) for v in re.findall(r" S:\s*(-?[\d.]+|-inf)", log[: log.rindex("Summary:")])]
    return {"I": integrated, "TP": peak, "S_max": max(shorts) if shorts else None}


def _faded(x: np.ndarray, fade_s: float = 0.1) -> np.ndarray:
    n = int(fade_s * ag.SR)
    env = np.ones_like(x)
    env[:n] = np.linspace(0.0, 1.0, n)
    env[-n:] = np.linspace(1.0, 0.0, n)
    return x * env


AGREEMENT_CASES = {
    "tone_48k": (lambda: ag.tone(440, 5.0, -14), 48000),
    "noise_48k": (lambda: ag.noise(6.0, -20), 48000),
    "burst_48k": (lambda: ag.pad(ag.burst(0.6), 0.5, 1.0), 48000),
    "stereo_44k1": (
        lambda: ag.stereo(ag.noise(5.0, -18, sr=44100), ag.tone(1000, 5.0, -24, sr=44100)),
        44100,
    ),
    "stereo_96k": (
        lambda: ag.stereo(ag.tone(3000, 4.0, -10, sr=96000), ag.noise(4.0, -22, sr=96000)),
        96000,
    ),
    "dynamic_48k": (
        lambda: np.concatenate([ag.noise(4.0, -40), ag.tone(220, 4.0, -12), ag.noise(4.0, -30)]),
        48000,
    ),
    "low_tone_44k1": (lambda: ag.tone(50, 4.0, -6, sr=44100), 44100),
    # Faded: on an abrupt HF start the edge overshoot depends on the interpolation
    # filter (ffmpeg resamples to 192 kHz with swresample); unfaded it differs ~0.5 dB.
    "hf_tone_48k": (lambda: _faded(ag.tone(11000, 4.0, -6)), 48000),
}


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not on PATH")
@pytest.mark.parametrize("name", list(AGREEMENT_CASES))
def test_agrees_with_ffmpeg_ebur128(tmp_path, name):
    make, sr = AGREEMENT_CASES[name]
    path = ag.write(tmp_path / f"{name}.wav", make(), sr, subtype="FLOAT")
    ours = loudness.measure(decode_path(path))
    ref = _ebur128(path)
    d_i = ours["lufs_integrated"] - ref["I"]
    d_tp = ours["true_peak_db"] - ref["TP"]
    # ffmpeg's S only covers a full 3 s window once 3 s have elapsed; for shorter clips
    # our contract reports the whole-clip loudness instead, so S is compared on >= 3 s.
    long_enough = decode_path(path).duration_s >= 3.0
    d_s = ours["lufs_short_max"] - ref["S_max"] if long_enough else None
    print(
        f"\n{name}: I ours {ours['lufs_integrated']:.2f} ffmpeg {ref['I']:.2f} (d {d_i:+.2f}); "
        f"TP ours {ours['true_peak_db']:.2f} ffmpeg {ref['TP']:.2f} (d {d_tp:+.2f}); "
        f"S_max ours {ours['lufs_short_max']:.2f} ffmpeg {ref['S_max']} "
        f"(d {'n/a' if d_s is None else f'{d_s:+.2f}'})"
    )
    assert abs(d_i) <= 0.5
    assert abs(d_tp) <= 0.5
    assert d_s is None or abs(d_s) <= 0.5
