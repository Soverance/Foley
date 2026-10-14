"""Objective DSP measurements (pipeline stage 3).

CONTRACT:

    FEATURES_VERSION: str      bump when any definition below changes

    compute_features(audio: DecodedAudio, kind_hint: str | None) -> dict
        Returns exactly these keys (matching catalog `features` columns):
          duration_s, sample_rate (native), channels,
          lead_silence_s, tail_silence_s, attack_s, decay_s, onset_count,
          centroid_hz_mean, rolloff_hz_mean, flatness_mean, zcr_mean,
          band_energy (dict: sub, low, low_mid, mid, upper_mid, presence, air; shares sum to 1),
          tempo_bpm (music only, else None),
          loop_seam_score (music or ambience only, else None)
        kind_hint is 'music', 'ambience', 'sfx', 'voice' or None (from foley.scan.kind_hint).
        librosa with n_fft=2048, hop=512. Definitions (the brief's, implement exactly):
          lead/tail silence: time before/after the signal first/last exceeds -60 dBFS
          attack: envelope reaching 10% of peak -> peak
          decay: peak -> 40 dB below peak
          band edges: sub <60, low 60-250, low_mid 250-500, mid 500-2k,
                      upper_mid 2k-4k, presence 4k-6k, air >6k Hz
          loop seam: sample discontinuity + spectral distance between last and first 50 ms
                     (lower = cleaner loop)
          tempo: librosa beat tracking
        Silent or near-silent input must not crash: return None for undefined values.

Precise definitions used (version "1")
--------------------------------------
* Time-domain measures use all channels at the decoded rate; spectral measures use the
  channel-mean (mono) signal at min(rate, 48 kHz).
* Silence threshold: -60 dBFS = 0.001 linear. A file whose sample peak never exceeds it
  is "near-silent": lead/tail silence, attack, decay, the spectral means, band_energy,
  tempo and loop_seam_score are None and onset_count is 0.
* lead_silence_s / tail_silence_s: time before the first / after the last sample whose
  absolute value (any channel) exceeds 0.001.
* Envelope: RMS over channels in a 5 ms moving window, 1 ms resolution.
  "peak" is the first time the envelope comes within 1 dB of its maximum (so a
  sustained tone peaks at its onset, not at an arbitrary later cycle).
  attack_s = peak time - first time the envelope reaches 10% (-20 dB) of the maximum.
  decay_s = time from the peak to the last point where the envelope is at or above
  1% (-40 dB) of the maximum (a sound cut off before it decays 40 dB reports the time
  to its end). An instantaneous onset measures about 4 ms because of the 5 ms window.
* Spectral frames: Hann, n_fft=2048, hop=512, centered with zero padding (librosa's
  stft layout), processed in chunks so long files stay in bounded memory.
  centroid/rolloff (85%)/flatness come from librosa on the magnitude spectrogram;
  zcr is the fraction of sign changes per 2048-sample frame. The means are taken over
  frames whose RMS exceeds -70 dBFS, so padding silence does not dilute them.
* band_energy: share of total power-spectrum energy (all frames) per band; bins at
  or above a band's lower edge and below its upper edge. Sums to 1.
* onset_count: librosa onset_detect on the mel onset-strength envelope (dB, 128 mels)
  of the same spectrogram (no extra resampling), unnormalized with delta = 4 dB so
  steady tones/noise do not produce spurious onsets; a silent frame is assumed before
  the file, so sound present at the start counts as one onset. Only onsets in frames
  above -70 dBFS count.
* tempo_bpm (kind_hint 'music' only): librosa.beat.beat_track on mono at 22.05 kHz,
  at most the central 120 s; None when no beat is found.
* loop_seam_score (kind_hint 'music' or 'ambience' only), in [0, 1], lower is cleaner:
    0.5 * discontinuity + 0.5 * spectral distance, where
    discontinuity = min(1, r / 10), r = largest per-channel jump |x[0] - x[-1]| divided
      by the RMS of sample-to-sample differences in the last and first 50 ms (r ~ 1 for
      a seamless wrap, large for a click);
    spectral distance = min(1, mean |dB difference| / 40) between 40-band mel power
      spectra (Hann, mono) of the last and first 50 ms, floored at -100 dB.
"""

from __future__ import annotations

import math

import numpy as np

from foley.decode import DecodedAudio

FEATURES_VERSION = "1"

N_FFT = 2048
HOP = 512
MAX_ANALYSIS_SR = 48000
SILENCE_LIN = 10 ** (-60 / 20)
FRAME_GATE_LIN = 10 ** (-70 / 20)
ENV_BLOCK_S = 0.001
ENV_WINDOW_BLOCKS = 5
TEMPO_SR = 22050
TEMPO_MAX_S = 120.0
SEAM_S = 0.05
ONSET_DELTA_DB = 4.0
BANDS = (
    ("sub", 0.0, 60.0),
    ("low", 60.0, 250.0),
    ("low_mid", 250.0, 500.0),
    ("mid", 500.0, 2000.0),
    ("upper_mid", 2000.0, 4000.0),
    ("presence", 4000.0, 6000.0),
    ("air", 6000.0, math.inf),
)
_CHUNK_FRAMES = 2048  # STFT frames per chunk (~22 s at 48 kHz)
_TD_CHUNK = 1 << 18  # time-domain samples per chunk


def _f(value, digits: int = 6):
    """Python float or None; never NaN/inf."""
    if value is None:
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    return round(value, digits)


# ---------------------------------------------------------------------- time domain


def _peak_and_silence(x: np.ndarray, sr: int):
    """(sample peak, lead_silence_s, tail_silence_s) over all channels."""
    n = x.shape[0]
    peak = 0.0
    first = last = None
    for start in range(0, n, _TD_CHUNK):
        a = np.max(np.abs(x[start : start + _TD_CHUNK]), axis=1)
        peak = max(peak, float(a.max()))
        idx = np.flatnonzero(a > SILENCE_LIN)
        if idx.size:
            if first is None:
                first = start + int(idx[0])
            last = start + int(idx[-1])
    if first is None:
        return peak, None, None
    return peak, first / sr, (n - 1 - last) / sr


def _envelope(x: np.ndarray, sr: int) -> tuple[np.ndarray, float]:
    """RMS envelope (5 ms window) at 1 ms resolution; returns (env, block_seconds)."""
    block = max(1, int(round(sr * ENV_BLOCK_S)))
    n = x.shape[0]
    n_blocks = -(-n // block)
    power = np.empty(n_blocks, dtype=np.float64)
    chunk = block * 4096
    for start in range(0, n, chunk):
        seg = np.asarray(x[start : start + chunk], dtype=np.float64)
        p = np.mean(seg * seg, axis=1)
        nb = -(-len(p) // block)
        pad = nb * block - len(p)
        if pad:
            p = np.concatenate([p, np.zeros(pad)])
        b0 = start // block
        power[b0 : b0 + nb] = p.reshape(nb, block).sum(axis=1)
    kernel = np.ones(ENV_WINDOW_BLOCKS) / (ENV_WINDOW_BLOCKS * block)
    env = np.sqrt(np.maximum(np.convolve(power, kernel, mode="same"), 0.0))
    return env, block / sr


def _attack_decay(x: np.ndarray, sr: int):
    env, dt = _envelope(x, sr)
    top = float(env.max()) if env.size else 0.0
    if not top > 0.0:
        return None, None
    peak_i = int(np.argmax(env >= top * 10 ** (-1 / 20)))
    start_i = int(np.argmax(env >= top * 0.1))
    above = np.flatnonzero(env[peak_i:] >= top * 0.01)
    end_i = peak_i + int(above[-1]) if above.size else peak_i
    attack = max(0, peak_i - start_i) * dt
    decay = (end_i - peak_i + 1) * dt
    return attack, decay


# ---------------------------------------------------------------------- spectral


def _spectral(y: np.ndarray, sr: int):
    """Chunked centered STFT; returns per-frame aggregates and mel power for onsets."""
    import librosa

    n = len(y)
    n_frames = 1 + n // HOP
    window = librosa.filters.get_window("hann", N_FFT, fftbins=True).astype(np.float32)
    mel_fb = librosa.filters.mel(sr=sr, n_fft=N_FFT, n_mels=128).astype(np.float32)
    freqs = librosa.fft_frequencies(sr=sr, n_fft=N_FFT)
    band_idx = [
        (name, (freqs >= lo) & (freqs < hi)) for name, lo, hi in BANDS
    ]
    band_sum = dict.fromkeys((b[0] for b in BANDS), 0.0)
    centroid, rolloff, flatness, zcr, rms, mel = [], [], [], [], [], []
    half = N_FFT // 2
    for f0 in range(0, n_frames, _CHUNK_FRAMES):
        f1 = min(n_frames, f0 + _CHUNK_FRAMES)
        s0 = f0 * HOP - half
        s1 = (f1 - 1) * HOP + half
        seg = np.zeros(s1 - s0, dtype=np.float32)
        a, b = max(0, s0), min(n, s1)
        if b > a:
            seg[a - s0 : b - s0] = y[a:b]
        frames = librosa.util.frame(seg, frame_length=N_FFT, hop_length=HOP)
        spec = np.abs(np.fft.rfft(frames * window[:, None], axis=0)).astype(np.float32)
        power = spec * spec
        centroid.append(librosa.feature.spectral_centroid(S=spec, sr=sr, n_fft=N_FFT)[0])
        rolloff.append(
            librosa.feature.spectral_rolloff(S=spec, sr=sr, n_fft=N_FFT, roll_percent=0.85)[0]
        )
        flatness.append(librosa.feature.spectral_flatness(S=spec)[0])
        signs = np.signbit(frames)
        zcr.append(np.mean(signs[1:] != signs[:-1], axis=0))
        rms.append(np.sqrt(np.mean(np.square(frames, dtype=np.float64), axis=0)))
        mel.append(mel_fb @ power)
        total = power.sum(axis=1, dtype=np.float64)
        for name, mask in band_idx:
            band_sum[name] += float(total[mask].sum())
    return {
        "centroid": np.concatenate(centroid),
        "rolloff": np.concatenate(rolloff),
        "flatness": np.concatenate(flatness),
        "zcr": np.concatenate(zcr),
        "rms": np.concatenate(rms),
        "mel": np.concatenate(mel, axis=1),
        "bands": band_sum,
    }


def _onset_count(mel: np.ndarray, voiced: np.ndarray, sr: int) -> int:
    import librosa

    if not voiced.any():
        return 0
    # A silent frame in front makes sound present at the very start count as an onset.
    mel = np.concatenate([np.zeros((mel.shape[0], 1), mel.dtype), mel], axis=1)
    env = librosa.onset.onset_strength(S=librosa.power_to_db(mel, ref=np.max), sr=sr)
    if not np.isfinite(env).all() or float(env.max()) <= 0.0:
        return 0
    onsets = librosa.onset.onset_detect(
        onset_envelope=env, sr=sr, hop_length=HOP, units="frames",
        normalize=False, delta=ONSET_DELTA_DB,
    )
    onsets = onsets - 1
    onsets = onsets[(onsets >= 0) & (onsets < len(voiced))]
    return int(np.count_nonzero(voiced[onsets]))


def _tempo(audio: DecodedAudio) -> float | None:
    import librosa

    y = audio.resampled(TEMPO_SR)
    max_n = int(TEMPO_MAX_S * TEMPO_SR)
    if len(y) > max_n:
        start = (len(y) - max_n) // 2
        y = y[start : start + max_n]
    if len(y) < 2 * N_FFT:
        return None
    tempo, beats = librosa.beat.beat_track(y=y, sr=TEMPO_SR, hop_length=HOP)
    bpm = float(np.atleast_1d(tempo)[0]) if np.size(tempo) else 0.0
    if len(beats) < 2 or not math.isfinite(bpm) or bpm <= 0:
        return None
    return round(bpm, 1)


def _loop_seam(x: np.ndarray, y: np.ndarray, sr: int, sr_y: int) -> float | None:
    import librosa

    m = max(2, int(round(SEAM_S * sr)))
    if x.shape[0] < 2 * m:
        return None
    head, tail = np.asarray(x[:m], np.float64), np.asarray(x[-m:], np.float64)
    jump = float(np.max(np.abs(x[0].astype(np.float64) - x[-1].astype(np.float64))))
    diffs = np.concatenate([np.diff(tail, axis=0).ravel(), np.diff(head, axis=0).ravel()])
    typical = float(np.sqrt(np.mean(diffs * diffs)))
    r = jump / typical if typical > 1e-12 else (0.0 if jump <= 1e-9 else 10.0)
    disc = min(1.0, r / 10.0)

    my = max(2, int(round(SEAM_S * sr_y)))
    n_fft = 1 << max(8, int(math.ceil(math.log2(my))))
    win = np.hanning(my)
    fb = librosa.filters.mel(sr=sr_y, n_fft=n_fft, n_mels=40)

    def mel_db(seg: np.ndarray) -> np.ndarray:
        p = np.abs(np.fft.rfft(seg.astype(np.float64) * win, n=n_fft)) ** 2
        return 10.0 * np.log10(np.maximum(fb @ p, 1e-10))

    spec = float(np.mean(np.abs(mel_db(y[-my:]) - mel_db(y[:my]))))
    spec = min(1.0, spec / 40.0)
    return 0.5 * disc + 0.5 * spec


# ---------------------------------------------------------------------- entry point


def compute_features(audio: DecodedAudio, kind_hint: str | None) -> dict:
    x = audio.samples
    sr = int(audio.sample_rate)
    out = {
        "duration_s": _f(audio.duration_s),
        "sample_rate": int(audio.native_sample_rate),
        "channels": int(audio.channels),
        "lead_silence_s": None,
        "tail_silence_s": None,
        "attack_s": None,
        "decay_s": None,
        "onset_count": 0,
        "centroid_hz_mean": None,
        "rolloff_hz_mean": None,
        "flatness_mean": None,
        "zcr_mean": None,
        "band_energy": None,
        "tempo_bpm": None,
        "loop_seam_score": None,
    }
    if x.shape[0] == 0:
        return out
    peak, lead, tail = _peak_and_silence(x, sr)
    if lead is None or peak <= SILENCE_LIN:
        return out  # near-silent: everything undefined
    out["lead_silence_s"] = _f(lead)
    out["tail_silence_s"] = _f(tail)
    attack, decay = _attack_decay(x, sr)
    out["attack_s"] = _f(attack)
    out["decay_s"] = _f(decay)

    sr_a = min(sr, MAX_ANALYSIS_SR)
    y = audio.mono() if sr_a == sr else audio.resampled(sr_a)
    s = _spectral(y, sr_a)
    voiced = s["rms"] > FRAME_GATE_LIN
    if voiced.any():
        out["centroid_hz_mean"] = _f(np.mean(s["centroid"][voiced]), 2)
        out["rolloff_hz_mean"] = _f(np.mean(s["rolloff"][voiced]), 2)
        out["flatness_mean"] = _f(np.mean(s["flatness"][voiced]))
        out["zcr_mean"] = _f(np.mean(s["zcr"][voiced]))
    total = sum(s["bands"].values())
    if total > 0.0 and math.isfinite(total):
        out["band_energy"] = {k: float(v / total) for k, v in s["bands"].items()}
    out["onset_count"] = _onset_count(s["mel"], voiced, sr_a)

    if kind_hint == "music":
        out["tempo_bpm"] = _f(_tempo(audio), 1)
    if kind_hint in ("music", "ambience"):
        out["loop_seam_score"] = _f(_loop_seam(x, y, sr, sr_a), 4)
    return out
