"""Model inputs from one decoded file: the mono, resampled windows each model sees.

Runs in the analysis workers (numpy + soxr only, no torch), so each library file is
read and decoded once per run and only these small arrays travel to the indexer
process, where the models run.

CONTRACT:

    PANNS_SR = 32000, CLAP_SR = 48000          rates the models were trained at
    WINDOW_S = 10.0, MAX_WINDOWS = 12          both models see at most 12 windows of 10 s
    PANNS_MIN_SAMPLES = 9920                   (0.31 s at 32 kHz) the shortest input CNN14
                                               accepts (32 frames survive its five 2x poolings);
                                               shorter clips are zero-padded to it. Longer clips
                                               are never padded: padding a 0.45 s dog bark to 1 s
                                               took its "Dog" score from 0.62 to under 0.1.

    window_starts(n: int, win: int, max_windows: int = MAX_WINDOWS) -> list[int]
        n <= win: [0] (the whole clip, one window). Otherwise k = min(max_windows,
        ceil(n / win)) windows of exactly `win` samples, evenly spaced from the start to
        the end of the file (first at 0, last ending at n). Up to max_windows * WINDOW_S
        (120 s) every sample is covered; longer files are sampled at k evenly spaced spots.

    windows(audio: DecodedAudio, sr: int) -> list[np.ndarray]
        float32 mono (channel mean) windows at `sr`, placed by window_starts. A clip of at
        most WINDOW_S is returned whole (its own length). Memory stays bounded for very long
        files: when the whole file at `sr` would exceed WHOLE_RESAMPLE_BYTES, only the
        windows (plus a little context) are cut from the decoded audio and resampled.

    panns_windows(audio) -> list[np.ndarray]   windows(audio, PANNS_SR), each >= PANNS_MIN_SAMPLES
    clap_windows(audio) -> list[np.ndarray]    windows(audio, CLAP_SR); short clips are left
                                               short (the CLAP feature extractor repeat-pads)
"""

from __future__ import annotations

import math

import numpy as np

PANNS_SR = 32000
CLAP_SR = 48000
WINDOW_S = 10.0
MAX_WINDOWS = 12
PANNS_MIN_SAMPLES = 31 * 320  # 32 STFT frames (hop 320, centred) -> 1 frame after 5 pools
WHOLE_RESAMPLE_BYTES = 64 * 1024 * 1024  # ~5.8 min of mono float32 at 48 kHz
_CONTEXT_S = 0.05  # resampler run-in either side of a window cut from a long file


def window_starts(n: int, win: int, max_windows: int = MAX_WINDOWS) -> list[int]:
    if n <= win:
        return [0]
    k = max(2, min(max_windows, math.ceil(n / win)))
    span = n - win
    return [int(round(i * span / (k - 1))) for i in range(k)]


def _cut(audio, start_native: int, end_native: int) -> np.ndarray:
    start_native = max(start_native, 0)
    end_native = min(end_native, audio.frames)
    seg = audio.samples[start_native:end_native]
    if audio.channels == 1:
        return np.ascontiguousarray(seg[:, 0], dtype=np.float32)
    return seg.mean(axis=1, dtype=np.float32)


def windows(audio, sr: int) -> list[np.ndarray]:
    src = int(audio.sample_rate)
    n = int(round(audio.frames * sr / src))
    win = int(round(WINDOW_S * sr))
    starts = window_starts(n, win)
    if n <= win or n * 4 <= WHOLE_RESAMPLE_BYTES or src == sr:
        y = audio.resampled(sr) if src != sr else audio.mono()
        if n <= win:
            return [np.ascontiguousarray(y, dtype=np.float32)]
        return [np.ascontiguousarray(y[s : s + win], dtype=np.float32) for s in starts]
    import soxr

    ctx = int(round(_CONTEXT_S * src))
    # Cut on the grid where source and target samples coincide, so a resampled cut lines
    # up sample-exactly with the same span of a whole-file resample.
    step = src // math.gcd(src, sr)
    out = []
    for s in starts:
        a = max((int(math.floor(s * src / sr)) - ctx) // step * step, 0)
        b = int(math.ceil((s + win) * src / sr)) + ctx
        seg = _cut(audio, a, b)
        y = soxr.resample(seg, src, sr, quality="HQ")
        off = s - a * sr // src
        w = y[off : off + win]
        if len(w) < win:
            w = np.pad(w, (0, win - len(w)))
        out.append(np.ascontiguousarray(w, dtype=np.float32))
    return out


def panns_windows(audio) -> list[np.ndarray]:
    minimum = PANNS_MIN_SAMPLES
    return [
        w if len(w) >= minimum else np.pad(w, (0, minimum - len(w)))
        for w in windows(audio, PANNS_SR)
    ]


def clap_windows(audio) -> list[np.ndarray]:
    return windows(audio, CLAP_SR)
