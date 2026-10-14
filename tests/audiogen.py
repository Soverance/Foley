"""Synthetic test audio. The repo ships no real audio; tests build what they need."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

SR = 48000


def tone(freq: float = 440.0, dur: float = 1.0, amp_db: float = -12.0, sr: int = SR) -> np.ndarray:
    t = np.arange(int(dur * sr)) / sr
    return (10 ** (amp_db / 20) * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def noise(dur: float = 1.0, amp_db: float = -20.0, sr: int = SR, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(int(dur * sr))
    x /= np.max(np.abs(x))
    return (10 ** (amp_db / 20) * x).astype(np.float32)


def burst(
    dur: float = 0.5, attack: float = 0.005, decay_tau: float = 0.08, sr: int = SR, seed: int = 1
) -> np.ndarray:
    """Noise burst with a linear attack and exponential decay: a stand-in impact."""
    n = int(dur * sr)
    t = np.arange(n) / sr
    env = np.where(t < attack, t / attack, np.exp(-(t - attack) / decay_tau))
    return (noise(dur, 0.0, sr, seed) * env * 0.8).astype(np.float32)


def click(sr: int = SR) -> np.ndarray:
    x = np.zeros(int(0.05 * sr), dtype=np.float32)
    x[10] = 0.9
    return x


def pad(x: np.ndarray, lead: float = 0.0, tail: float = 0.0, sr: int = SR) -> np.ndarray:
    return np.concatenate(
        [np.zeros(int(lead * sr), np.float32), x, np.zeros(int(tail * sr), np.float32)]
    )


def stereo(left: np.ndarray, right: np.ndarray | None = None) -> np.ndarray:
    return np.stack([left, left if right is None else right], axis=1)


def write(path: Path, x: np.ndarray, sr: int = SR, subtype: str | None = None) -> Path:
    """Write by extension. MP3 falls back to ffmpeg when libsndfile lacks MPEG support."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ext = path.suffix.lower()
    if ext == ".mp3":
        try:
            sf.write(path, x, sr, format="MP3")
            return path
        except Exception:
            tmp = path.with_suffix(".tmp.wav")
            sf.write(tmp, x, sr)
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise
            subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", str(tmp), str(path)], check=True
            )
            tmp.unlink()
            return path
    fmt = {".wav": "WAV", ".flac": "FLAC", ".ogg": "OGG", ".aif": "AIFF", ".aiff": "AIFF"}[ext]
    if fmt == "OGG":
        subtype = subtype or "VORBIS"
    sf.write(path, x, sr, format=fmt, subtype=subtype)
    return path
