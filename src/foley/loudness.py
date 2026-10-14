"""Loudness (pipeline stage 4) and loudness normalization helpers.

CONTRACT:

    LOUDNESS_VERSION: str

    measure(audio: DecodedAudio) -> dict
        {lufs_integrated, lufs_short_max, true_peak_db}
        ITU-R BS.1770 via pyloudnorm on all channels. lufs_integrated is None for
        clips shorter than 0.4 s. lufs_short_max is the max 3 s short-term loudness
        (for clips shorter than 3 s, the loudness of the whole clip, or None under
        0.4 s). true_peak_db is dBTP from 4x oversampling. Silence gives None/-inf
        handled as None. Must agree with ffmpeg's ebur128 filter within 0.5 LU.

    gain_for_target(measured: dict, duration_s: float, target_lufs: float | None,
                    oneshot_peak_db: float | None) -> float | None
        Gain in dB to reach the target. Clips >= 0.4 s use integrated LUFS ->
        target_lufs; shorter clips use true peak -> oneshot_peak_db. Returns None
        when the needed measurement or target is missing.

    apply_gain(samples: np.ndarray, gain_db: float) -> np.ndarray
        Returns float32 samples scaled by gain_db (no limiting; caller checks peaks).

Implementation notes
--------------------
The K-weighting filters are pyloudnorm's own (`Meter(rate)._filters`, BS.1770-4
high shelf + high pass), but they are run once per channel as a streaming
second-order-section filter in blocks, and the K-weighted energy is accumulated into
100 ms sub-blocks. From those:
  * integrated: pyloudnorm's algorithm exactly (400 ms blocks, 75% overlap, the same
    block count and bounds, absolute -70 LUFS and relative -10 LU gates); it matches
    `pyloudnorm.Meter.integrated_loudness` to ~0.01 LU but needs only O(blocks) memory
    and no per-block Python loop, so hour-long files are cheap;
  * short-term max: max over 3 s windows stepped by 100 ms (ungated); clips under
    3 s use the whole clip;
  * true peak: per channel 4x polyphase oversampling (scipy resample_poly) in
    overlapping chunks, never below the sample peak.
Channel weights follow pyloudnorm for <= 5 channels ([L, R, C, Ls, Rs] -> 1, 1, 1,
1.41, 1.41); 6 channels are treated as 5.1 (L R C LFE Ls Rs, LFE excluded) and 8 as
7.1 (L R C LFE + 4 surrounds at 1.41), as ffmpeg's ebur128 does; any other count
weights every channel 1.0. Values below -70 LUFS (the BS.1770 absolute gate) and
digital silence are reported as None. Loudness values are rounded to 0.01.
"""

from __future__ import annotations

import math

import numpy as np
import pyloudnorm
from scipy import signal

from foley.decode import DecodedAudio

LOUDNESS_VERSION = "1"

ABS_GATE_LUFS = -70.0
REL_GATE_LU = -10.0
MIN_INTEGRATED_S = 0.4
SHORT_TERM_S = 3.0
_CHUNK = 1 << 18
_TP_PAD = 64
_FILTER_CACHE: dict[int, np.ndarray] = {}


def _k_weighting_sos(rate: int) -> np.ndarray:
    sos = _FILTER_CACHE.get(rate)
    if sos is None:
        meter = pyloudnorm.Meter(rate)
        rows = []
        for stage in meter._filters.values():
            b = np.asarray(stage.b, dtype=np.float64) * stage.passband_gain
            a = np.asarray(stage.a, dtype=np.float64)
            rows.append(np.concatenate([b / a[0], a / a[0]]))
        sos = np.vstack(rows)
        _FILTER_CACHE[rate] = sos
    return sos


def _channel_weights(channels: int) -> np.ndarray:
    if channels <= 5:
        g = [1.0, 1.0, 1.0, 1.41, 1.41][:channels]
    elif channels == 6:
        g = [1.0, 1.0, 1.0, 0.0, 1.41, 1.41]
    elif channels == 8:
        g = [1.0, 1.0, 1.0, 0.0, 1.41, 1.41, 1.41, 1.41]
    else:
        g = [1.0] * channels
    return np.asarray(g, dtype=np.float64)


def _subblock_edges(n: int, rate: int) -> np.ndarray:
    """Sample index where each 100 ms sub-block starts (pyloudnorm's int() bounds)."""
    k = np.arange(int(n / (0.1 * rate)) + 2)
    edges = (k * 0.1 * rate).astype(np.int64)
    edges = edges[edges < n]
    return edges


def _weighted_subblock_energy(x: np.ndarray, rate: int, weights: np.ndarray):
    """Channel-weighted sum of squared K-weighted samples per 100 ms sub-block."""
    n = x.shape[0]
    sos = _k_weighting_sos(rate)
    edges = _subblock_edges(n, rate)
    energy = np.zeros(len(edges), dtype=np.float64)
    for ch in range(x.shape[1]):
        if weights[ch] == 0.0:
            continue
        zi = np.zeros((sos.shape[0], 2))
        acc = np.zeros(len(edges), dtype=np.float64)
        for start in range(0, n, _CHUNK):
            seg = np.asarray(x[start : start + _CHUNK, ch], dtype=np.float64)
            y, zi = signal.sosfilt(sos, seg, zi=zi)
            sq = y * y
            stop = start + len(seg)
            first = np.searchsorted(edges, start, side="right") - 1
            last = np.searchsorted(edges, stop, side="left")
            local = np.clip(edges[first:last] - start, 0, None)
            sums = np.add.reduceat(sq, local)
            acc[first : first + len(sums)] += sums
        energy += weights[ch] * acc
    return energy, edges


def _to_lufs(mean_square: float) -> float | None:
    if not (mean_square > 0.0) or not math.isfinite(mean_square):
        return None
    value = -0.691 + 10.0 * math.log10(mean_square)
    if value < ABS_GATE_LUFS:
        return None
    return round(value, 2)


def _integrated(energy: np.ndarray, n: int, rate: int) -> float | None:
    block_len = 0.4 * rate
    num_blocks = int(np.round((n / rate - 0.4) / 0.1)) + 1
    if num_blocks < 1:
        return None
    csum = np.concatenate([[0.0], np.cumsum(energy)])
    j = np.arange(num_blocks)
    hi = np.minimum(j + 4, len(energy))
    z = (csum[hi] - csum[np.minimum(j, len(energy))]) / block_len
    with np.errstate(divide="ignore"):
        lk = -0.691 + 10.0 * np.log10(z)
    gated = z[lk >= ABS_GATE_LUFS]
    if gated.size == 0:
        return None
    rel = -0.691 + 10.0 * math.log10(float(gated.mean())) + REL_GATE_LU
    final = z[(lk > rel) & (lk > ABS_GATE_LUFS)]
    if final.size == 0:
        return None
    return _to_lufs(float(final.mean()))


def _short_term_max(energy: np.ndarray, edges: np.ndarray, n: int, rate: int):
    if n < SHORT_TERM_S * rate:
        return _to_lufs(float(energy.sum()) / n)
    window = int(round(SHORT_TERM_S / 0.1))
    bounds = np.append(edges, n)
    csum = np.concatenate([[0.0], np.cumsum(energy)])
    starts = np.arange(0, len(energy) - window + 1)
    starts = starts[bounds[starts + window] <= n]
    if starts.size == 0:
        return _to_lufs(float(energy.sum()) / n)
    sums = csum[starts + window] - csum[starts]
    lengths = bounds[starts + window] - bounds[starts]
    return _to_lufs(float(np.max(sums / lengths)))


def _true_peak(x: np.ndarray) -> float | None:
    n = x.shape[0]
    peak = 0.0
    for start in range(0, n, _CHUNK):
        lo = max(0, start - _TP_PAD)
        hi = min(n, start + _CHUNK + _TP_PAD)
        seg = np.asarray(x[lo:hi], dtype=np.float64)
        up = signal.resample_poly(seg, 4, 1, axis=0)
        a = (start - lo) * 4
        b = a + (min(n, start + _CHUNK) - start) * 4
        core = up[a:b]
        if core.size:
            peak = max(peak, float(np.max(np.abs(core))))
        sample_peak = float(np.max(np.abs(x[start : start + _CHUNK])))
        peak = max(peak, sample_peak)
    if not (peak > 1e-10) or not math.isfinite(peak):
        return None
    return round(20.0 * math.log10(peak), 2)


def measure(audio: DecodedAudio) -> dict:
    x = audio.samples
    rate = int(audio.sample_rate)
    n = int(x.shape[0])
    result = {"lufs_integrated": None, "lufs_short_max": None, "true_peak_db": None}
    if n == 0:
        return result
    result["true_peak_db"] = _true_peak(x)
    if result["true_peak_db"] is None or n < MIN_INTEGRATED_S * rate:
        return result
    energy, edges = _weighted_subblock_energy(x, rate, _channel_weights(x.shape[1]))
    result["lufs_integrated"] = _integrated(energy, n, rate)
    result["lufs_short_max"] = _short_term_max(energy, edges, n, rate)
    return result


def gain_for_target(
    measured: dict,
    duration_s: float,
    target_lufs: float | None,
    oneshot_peak_db: float | None,
) -> float | None:
    if duration_s is None:
        return None
    if duration_s >= MIN_INTEGRATED_S:
        lufs = measured.get("lufs_integrated")
        if lufs is None or target_lufs is None:
            return None
        return float(target_lufs) - float(lufs)
    peak = measured.get("true_peak_db")
    if peak is None or oneshot_peak_db is None:
        return None
    return float(oneshot_peak_db) - float(peak)


def apply_gain(samples: np.ndarray, gain_db: float) -> np.ndarray:
    factor = np.float32(10.0 ** (float(gain_db) / 20.0))
    return np.multiply(np.asarray(samples, dtype=np.float32), factor, dtype=np.float32)
