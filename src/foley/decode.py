"""Read each library file once, hash it, and decode it to float32 PCM.

CONTRACT (other modules depend on these names and shapes):

    read_bytes(path) -> bytes
        The single read of a library file per index run. Opens read-only.
    content_hash(data: bytes) -> str
        xxh3-128 hex digest of the file bytes.
    decode_bytes(data: bytes, ext: str) -> DecodedAudio
        soundfile first, ffmpeg (subprocess, stdin->f32le stdout) as fallback.
        Raises DecodeError with a human-readable reason on failure.
    decode_path(path) -> DecodedAudio
        read_bytes + decode_bytes; used by on-demand image rendering and import.
    audio_hash(audio: DecodedAudio) -> str
        xxh3-128 hex digest of the decoded PCM, quantized to 16-bit so the hash is
        stable across decoders; identical audio with different metadata matches.

    DecodedAudio
        samples: np.ndarray       float32, shape (frames, channels), C-contiguous
        sample_rate: int          rate of `samples`
        native_sample_rate: int   rate of the source file (may differ for very long files)
        channels / frames / duration_s properties (duration from the source)
        mono() -> np.ndarray      float32 (frames,), channel mean, cached
        resampled(sr) -> np.ndarray   mono at `sr`, cached per rate (soxr/librosa)

Memory policy (very long or high-rate files)
--------------------------------------------
Decoding happens from the bytes already in memory; nothing is re-read from the share.
If the full-rate decode (frames * channels * 4 bytes) would exceed MAX_DECODED_BYTES
(1 GiB), the file is decoded in blocks and streamed through a soxr resampler to the
highest rate in REDUCED_RATES (48 kHz first) that fits the budget, so peak RAM stays
around 2 GB (budget + the encoded bytes + one block). `sample_rate` is then the reduced
rate while `native_sample_rate` and `duration_s` still describe the source. BS.1770
loudness, true peak (4x oversampled) and all DSP features remain valid at 48 kHz; at
lower fallback rates they are approximations of the full-band values. A file that does
not fit even at the lowest rate (roughly > 4.6 h of stereo) raises DecodeError.
Consumers that need bit-exact native audio (e.g. writing a normalized copy) should check
`audio.sample_rate != audio.native_sample_rate`.

Non-finite samples (NaN/inf in float WAVs) are replaced with 0 so no NaN reaches the
analysis stages. Files with no audio frames raise DecodeError.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf
import xxhash

MAX_DECODED_BYTES = 1 << 30  # 1 GiB of float32 samples
REDUCED_RATES = (48000, 32000, 24000, 16000, 12000, 8000)
_BLOCK_FRAMES = 1 << 18
_HASH_BLOCK_FRAMES = 1 << 18


class DecodeError(Exception):
    pass


@dataclass
class DecodedAudio:
    samples: np.ndarray
    sample_rate: int
    native_sample_rate: int
    _cache: dict = field(default_factory=dict, repr=False, compare=False)
    native_frames: int | None = field(default=None, repr=False, compare=False)

    @property
    def channels(self) -> int:
        return int(self.samples.shape[1])

    @property
    def frames(self) -> int:
        return int(self.samples.shape[0])

    @property
    def duration_s(self) -> float:
        if self.native_frames is not None and self.native_sample_rate:
            return self.native_frames / float(self.native_sample_rate)
        return self.frames / float(self.sample_rate)

    def mono(self) -> np.ndarray:
        cached = self._cache.get("mono")
        if cached is None:
            if self.channels == 1:
                cached = np.ascontiguousarray(self.samples[:, 0], dtype=np.float32)
            else:
                cached = np.empty(self.frames, dtype=np.float32)
                for start in range(0, self.frames, _BLOCK_FRAMES):
                    block = self.samples[start : start + _BLOCK_FRAMES]
                    cached[start : start + len(block)] = block.mean(axis=1, dtype=np.float32)
            self._cache["mono"] = cached
        return cached

    def resampled(self, sr: int) -> np.ndarray:
        sr = int(sr)
        if sr == self.sample_rate:
            return self.mono()
        key = ("resampled", sr)
        cached = self._cache.get(key)
        if cached is None:
            import soxr

            if "mono" in self._cache or self.channels == 1:
                y = soxr.resample(self.mono(), self.sample_rate, sr, quality="HQ")
            else:
                # Stream the channel mean so a full-rate mono copy of a long
                # high-rate file is never materialized just to be resampled.
                stream = soxr.ResampleStream(self.sample_rate, sr, 1, dtype="float32")
                parts = []
                for start in range(0, self.frames, _BLOCK_FRAMES):
                    block = self.samples[start : start + _BLOCK_FRAMES].mean(
                        axis=1, dtype=np.float32
                    )
                    last = start + _BLOCK_FRAMES >= self.frames
                    parts.append(stream.resample_chunk(block, last=last))
                y = np.concatenate(parts) if parts else np.zeros(0, np.float32)
            cached = np.ascontiguousarray(y, dtype=np.float32)
            self._cache[key] = cached
        return cached


def read_bytes(path: Path) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def content_hash(data: bytes) -> str:
    return xxhash.xxh3_128_hexdigest(data)


def decode_path(path: Path) -> DecodedAudio:
    """Decode straight from the file with soundfile, so a large file is never held in
    memory as encoded bytes and samples at once; falls back to decode_bytes (ffmpeg)."""
    path = Path(path)
    try:
        audio = _decode_soundfile(path)
    except DecodeError:
        raise
    except Exception:
        return decode_bytes(read_bytes(path), path.suffix)
    if audio.frames == 0:
        raise DecodeError("file contains no audio frames")
    _sanitize(audio.samples)
    return audio


def audio_hash(audio: DecodedAudio) -> str:
    h = xxhash.xxh3_128()
    h.update(f"pcm16:{audio.channels}:{audio.native_sample_rate}:{audio.sample_rate}:".encode())
    x = audio.samples
    for start in range(0, x.shape[0], _HASH_BLOCK_FRAMES):
        block = x[start : start + _HASH_BLOCK_FRAMES]
        q = np.clip(np.rint(block * 32768.0), -32768, 32767).astype("<i2")
        h.update(np.ascontiguousarray(q).tobytes())
    return h.hexdigest()


# ---------------------------------------------------------------------------- decoding


def decode_bytes(data: bytes, ext: str) -> DecodedAudio:
    ext = (ext or "").lower()
    if not data:
        raise DecodeError("empty file (0 bytes)")
    try:
        audio = _decode_soundfile(data)
    except DecodeError:
        raise
    except Exception as sf_exc:  # libsndfile cannot read it: try ffmpeg
        sf_reason = _reason(sf_exc)
        try:
            audio = _decode_ffmpeg(data)
        except DecodeError as ff_exc:
            raise DecodeError(
                f"cannot decode {ext or 'file'}: soundfile: {sf_reason}; ffmpeg: {ff_exc}"
            ) from None
    if audio.frames == 0:
        raise DecodeError("file contains no audio frames")
    _sanitize(audio.samples)
    return audio


def _reason(exc: Exception) -> str:
    msg = str(exc).strip() or type(exc).__name__
    return msg.splitlines()[0][:300]


def _choose_rate(frames: int, channels: int, rate: int) -> int:
    """Native rate if the decode fits the budget, else the best reduced rate."""
    if frames * channels * 4 <= MAX_DECODED_BYTES:
        return rate
    for r in REDUCED_RATES:
        if r < rate and int(frames * r / rate + 1) * channels * 4 <= MAX_DECODED_BYTES:
            return r
    hours = frames / rate / 3600 if rate else 0.0
    raise DecodeError(
        f"audio too long to analyze within the memory budget "
        f"({hours:.1f} h, {channels} channels at {rate} Hz)"
    )


def _decode_soundfile(source: bytes | Path) -> DecodedAudio:
    handle = io.BytesIO(source) if isinstance(source, bytes) else str(source)
    with sf.SoundFile(handle) as f:
        rate, channels, frames = int(f.samplerate), int(f.channels), int(f.frames)
        if channels < 1 or rate < 1 or frames <= 0:
            # Let ffmpeg have a go; if it also finds nothing the caller gets a DecodeError.
            raise RuntimeError(f"no audio frames ({channels} channels at {rate} Hz)")
        target = _choose_rate(frames, channels, rate)
        if target == rate:
            x = f.read(dtype="float32", always_2d=True)
            x = np.ascontiguousarray(x)
            return DecodedAudio(x, rate, rate, native_frames=int(x.shape[0]))
        return _decode_reduced(f, rate, channels, frames, target)


def _decode_reduced(f: sf.SoundFile, rate: int, channels: int, frames: int, target: int):
    import soxr

    stream = soxr.ResampleStream(rate, target, channels, dtype="float32", quality="HQ")
    capacity = int(frames * target / rate) + 4096
    out = np.empty((capacity, channels), dtype=np.float32)
    n_out = 0
    n_in = 0

    def put(y: np.ndarray) -> None:
        nonlocal out, n_out
        if n_out + len(y) > out.shape[0]:
            grown = np.empty((n_out + len(y) + 65536, channels), dtype=np.float32)
            grown[:n_out] = out[:n_out]
            out = grown
        out[n_out : n_out + len(y)] = y
        n_out += len(y)

    while True:
        block = f.read(_BLOCK_FRAMES, dtype="float32", always_2d=True)
        n_in += len(block)
        last = len(block) < _BLOCK_FRAMES
        if len(block):
            _sanitize(block)
        put(stream.resample_chunk(np.ascontiguousarray(block), last=last))
        if last:
            break
    # A leading row slice of a C-contiguous array is still C-contiguous: no copy needed
    # (the unused tail of the buffer is at most a few thousand frames).
    return DecodedAudio(out[:n_out], target, rate, native_frames=n_in)


def _sanitize(x: np.ndarray) -> None:
    """Replace NaN/inf in place, block-wise to avoid a full-size temporary."""
    flat = x.reshape(-1)
    step = 1 << 22
    for start in range(0, flat.size, step):
        seg = flat[start : start + step]
        if not np.isfinite(seg).all():
            np.nan_to_num(seg, copy=False, nan=0.0, posinf=0.0, neginf=0.0)


# ---------------------------------------------------------------------------- ffmpeg


def _ffprobe(ffprobe: str, data: bytes) -> tuple[int, int, float | None]:
    proc = subprocess.run(
        [
            ffprobe, "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=channels,sample_rate,duration:format=duration",
            "-of", "json", "-i", "pipe:0",
        ],
        input=data, capture_output=True, timeout=120,
    )
    if proc.returncode != 0:
        raise DecodeError(_reason(Exception(proc.stderr.decode("utf-8", "replace"))))
    info = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    streams = info.get("streams") or []
    if not streams:
        raise DecodeError("no audio stream")
    st = streams[0]
    channels, rate = int(st.get("channels") or 0), int(st.get("sample_rate") or 0)
    if channels < 1 or rate < 1:
        raise DecodeError(f"invalid audio stream ({channels} channels at {rate} Hz)")
    duration = None
    for src in (st.get("duration"), (info.get("format") or {}).get("duration")):
        try:
            duration = float(src)
            break
        except (TypeError, ValueError):
            continue
    return channels, rate, duration


def _decode_ffmpeg(data: bytes) -> DecodedAudio:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise DecodeError("ffmpeg not found on PATH (needed for formats libsndfile cannot read)")
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        channels, rate, duration = _ffprobe(ffprobe, data)
        native_rate = rate
        if duration is not None:
            rate = _choose_rate(int(duration * rate) + 1, channels, rate)
    else:  # no probe available: ask ffmpeg for a fixed layout
        channels, rate, native_rate, duration = 2, 48000, 48000, None

    cmd = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
        "-map", "0:a:0", "-vn", "-ac", str(channels), "-ar", str(rate),
        "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1",
    ]
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    err_chunks: list[bytes] = []

    def feed() -> None:
        try:
            proc.stdin.write(data)
        except OSError:
            pass  # ffmpeg exited early; its stderr says why
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    def drain_err() -> None:
        err_chunks.append(proc.stderr.read())

    threads = [threading.Thread(target=fn, daemon=True) for fn in (feed, drain_err)]
    for t in threads:
        t.start()
    buf = bytearray()
    limit = MAX_DECODED_BYTES + channels * 4 * 65536
    too_big = False
    while True:
        chunk = proc.stdout.read(1 << 20)
        if not chunk:
            break
        buf += chunk
        if len(buf) > limit:
            too_big = True
            proc.kill()
            break
    proc.wait()
    for t in threads:
        t.join(timeout=5)
    if too_big:
        raise DecodeError("audio too long to analyze within the memory budget (unknown duration)")
    if proc.returncode != 0:
        msg = b"".join(err_chunks).decode("utf-8", "replace").strip()
        raise DecodeError(_reason(Exception(msg or f"ffmpeg exited with {proc.returncode}")))
    frame_bytes = channels * 4
    usable = len(buf) - len(buf) % frame_bytes
    if usable == 0:
        raise DecodeError("file contains no audio frames")
    del buf[usable:]
    x = np.frombuffer(buf, dtype="<f4").reshape(-1, channels)
    native_frames = int(round(x.shape[0] * native_rate / rate))
    return DecodedAudio(x, rate, native_rate, native_frames=native_frames)
