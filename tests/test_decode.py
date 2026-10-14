from __future__ import annotations

import io
import shutil

import audiogen as ag
import numpy as np
import pytest
import soundfile as sf

from foley import decode, loudness
from foley.decode import DecodedAudio, DecodeError

FFMPEG = shutil.which("ffmpeg")


def _wav_bytes(x: np.ndarray, sr: int = ag.SR, subtype: str = "PCM_16", fmt: str = "WAV") -> bytes:
    buf = io.BytesIO()
    sf.write(buf, x, sr, format=fmt, subtype=subtype)
    return buf.getvalue()


def test_content_hash():
    assert decode.content_hash(b"abc") == decode.content_hash(b"abc")
    assert decode.content_hash(b"abc") != decode.content_hash(b"abd")
    assert len(decode.content_hash(b"")) == 32


def test_read_bytes_and_decode_path(tmp_path):
    path = ag.write(tmp_path / "Thing.WAV", ag.tone(440, 0.5))
    assert decode.read_bytes(path) == path.read_bytes()
    audio = decode.decode_path(path)
    assert audio.sample_rate == audio.native_sample_rate == ag.SR
    assert audio.frames == int(0.5 * ag.SR)


@pytest.mark.parametrize(
    "subtype, tol",
    [("PCM_U8", 1 / 64), ("PCM_16", 1e-4), ("PCM_24", 1e-6), ("PCM_32", 1e-6),
     ("FLOAT", 0.0), ("DOUBLE", 1e-7)],
)
def test_wav_bit_depths(subtype, tol):
    x = ag.stereo(ag.tone(440, 0.25, -6), ag.noise(0.25, -12))
    audio = decode.decode_bytes(_wav_bytes(x, subtype=subtype), ".WAV")
    assert audio.samples.dtype == np.float32
    assert audio.samples.flags["C_CONTIGUOUS"]
    assert audio.samples.shape == x.shape
    assert np.max(np.abs(audio.samples - x)) <= tol + 1e-9


@pytest.mark.parametrize("ext", [".wav", ".flac", ".ogg", ".aiff", ".mp3", ".WAV", ".FLAC"])
def test_formats(tmp_path, ext):
    if ext == ".mp3" and FFMPEG is None and "MP3" not in sf.available_formats():
        pytest.skip("no MP3 writer available")
    x = ag.stereo(ag.tone(440, 1.0, -12), ag.tone(660, 1.0, -12))
    path = ag.write(tmp_path / f"f{ext}", x)
    audio = decode.decode_path(path)
    assert audio.channels == 2
    assert audio.native_sample_rate == ag.SR
    assert audio.duration_s == pytest.approx(1.0, abs=0.06)  # mp3 padding
    assert np.isfinite(audio.samples).all()
    if ext.lower() in (".wav", ".flac", ".aiff"):
        assert np.max(np.abs(audio.samples - x)) < 1e-3


@pytest.mark.parametrize("channels", [1, 2, 6, 8])
def test_channel_counts(channels):
    x = np.tile(ag.noise(0.2, -10)[:, None], (1, channels))
    audio = decode.decode_bytes(_wav_bytes(x), ".wav")
    assert audio.channels == channels
    assert audio.mono().shape == (audio.frames,)
    assert audio.mono().dtype == np.float32


def test_empty_and_corrupt_raise_decode_error():
    with pytest.raises(DecodeError, match="empty"):
        decode.decode_bytes(b"", ".wav")
    with pytest.raises(DecodeError):
        decode.decode_bytes(b"RIFF\x00\x00\x00\x00WAVEjunkjunkjunk" * 10, ".wav")
    with pytest.raises(DecodeError):
        decode.decode_bytes(np.random.default_rng(0).bytes(5000), ".mp3")
    with pytest.raises(DecodeError):
        decode.decode_bytes(_wav_bytes(np.zeros((0, 2), np.float32)), ".wav")


def test_truncated_file_does_not_crash():
    data = _wav_bytes(ag.tone(440, 1.0))
    try:
        audio = decode.decode_bytes(data[: len(data) // 2], ".wav")
    except DecodeError:
        return
    assert 0 < audio.frames < ag.SR


def test_missing_ffmpeg_gives_clear_error(monkeypatch):
    monkeypatch.setattr(decode.shutil, "which", lambda name: None)
    with pytest.raises(DecodeError, match="ffmpeg not found"):
        decode.decode_bytes(b"definitely not audio" * 100, ".m4a")


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not on PATH")
def test_ffmpeg_fallback(monkeypatch):
    x = ag.stereo(ag.tone(440, 0.5, -6, sr=44100), ag.noise(0.5, -12, sr=44100))
    data = _wav_bytes(x, sr=44100, subtype="PCM_24")

    def refuse(_data):
        raise RuntimeError("pretend libsndfile cannot read this")

    monkeypatch.setattr(decode, "_decode_soundfile", refuse)
    audio = decode.decode_bytes(data, ".wav")
    assert audio.sample_rate == audio.native_sample_rate == 44100
    assert audio.samples.shape == x.shape
    assert audio.samples.dtype == np.float32 and audio.samples.flags["C_CONTIGUOUS"]
    assert np.max(np.abs(audio.samples - x)) < 1e-5
    with pytest.raises(DecodeError, match="soundfile: pretend"):
        decode.decode_bytes(b"\x00garbage" * 500, ".wav")


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not on PATH")
def test_ffmpeg_only_format(tmp_path):
    """AAC in ADTS: libsndfile cannot read it, ffmpeg can."""
    import subprocess

    wav = ag.write(tmp_path / "a.wav", ag.tone(440, 1.0, -12))
    aac = tmp_path / "a.aac"
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(wav), str(aac)], check=True)
    audio = decode.decode_path(aac)
    assert audio.channels == 1
    assert audio.duration_s == pytest.approx(1.0, abs=0.1)


def test_non_finite_samples_are_zeroed():
    x = ag.tone(440, 0.1)
    x[5] = np.nan
    x[6] = np.inf
    audio = decode.decode_bytes(_wav_bytes(x, subtype="FLOAT"), ".wav")
    assert np.isfinite(audio.samples).all()


def test_audio_hash_ignores_container_and_metadata(tmp_path):
    # int16 input so every container stores exactly the same PCM (libsndfile's
    # float->int rounding differs between WAV and FLAC writers)
    x = np.rint(ag.stereo(ag.noise(0.5, -10), ag.tone(300, 0.5, -10)) * 32767).astype(np.int16)
    plain = decode.decode_bytes(_wav_bytes(x), ".wav")
    flac = decode.decode_bytes(_wav_bytes(x, subtype="PCM_16", fmt="FLAC"), ".flac")
    tagged_path = tmp_path / "tagged.wav"
    with sf.SoundFile(tagged_path, "w", ag.SR, 2, subtype="PCM_16", format="WAV") as f:
        f.title = "A different title"
        f.comment = "metadata changes the bytes, not the audio"
        f.write(x)
    tagged = decode.decode_path(tagged_path)
    assert decode.content_hash(tagged_path.read_bytes()) != decode.content_hash(_wav_bytes(x))
    assert decode.audio_hash(plain) == decode.audio_hash(flac) == decode.audio_hash(tagged)
    # 24-bit source of the same 16-bit-representable audio also matches after quantizing
    assert decode.audio_hash(plain) == decode.audio_hash(
        decode.decode_bytes(_wav_bytes(plain.samples, subtype="PCM_24"), ".wav")
    )


def test_audio_hash_includes_layout_and_rate():
    x = ag.noise(0.5, -10)
    mono = decode.decode_bytes(_wav_bytes(x), ".wav")
    other_rate = decode.decode_bytes(_wav_bytes(x, sr=44100), ".wav")
    interleaved = decode.decode_bytes(_wav_bytes(x.reshape(-1, 2)), ".wav")
    hashes = {decode.audio_hash(a) for a in (mono, other_rate, interleaved)}
    assert len(hashes) == 3
    louder = decode.decode_bytes(_wav_bytes(x * 0.5), ".wav")
    assert decode.audio_hash(louder) != decode.audio_hash(mono)


def test_mono_and_resampled_are_cached():
    x = ag.stereo(ag.tone(440, 1.0, -6), np.zeros(ag.SR, np.float32))
    audio = DecodedAudio(np.ascontiguousarray(x), ag.SR, ag.SR)
    m = audio.mono()
    assert m is audio.mono()
    assert np.allclose(m, x.mean(axis=1))
    r = audio.resampled(16000)
    assert r is audio.resampled(16000)
    assert r.dtype == np.float32 and len(r) == pytest.approx(16000, abs=2)
    assert audio.resampled(ag.SR) is m


def test_long_file_is_reduced_within_budget(monkeypatch):
    """Files over the decode budget come back at 48 kHz with native rate/duration kept."""
    sr = 96000
    # no content above 24 kHz, which the 48 kHz reduction (by design) drops
    x = ag.stereo(ag.tone(100, 6.0, -12, sr=sr) + ag.tone(9000, 6.0, -20, sr=sr),
                  ag.tone(1000, 6.0, -18, sr=sr))
    data = _wav_bytes(x, sr=sr, subtype="PCM_24")
    full = decode.decode_bytes(data, ".wav")
    monkeypatch.setattr(decode, "MAX_DECODED_BYTES", 3_000_000)
    small = decode.decode_bytes(data, ".wav")
    assert small.sample_rate == 48000
    assert small.native_sample_rate == sr
    assert small.duration_s == pytest.approx(6.0, abs=1e-6)
    assert small.frames == pytest.approx(6.0 * 48000, abs=64)
    assert small.samples.nbytes <= 3_000_000
    a, b = loudness.measure(full), loudness.measure(small)
    assert b["lufs_integrated"] == pytest.approx(a["lufs_integrated"], abs=0.1)
    assert b["true_peak_db"] == pytest.approx(a["true_peak_db"], abs=0.3)
    # budget too small even at the lowest rate -> clear error
    monkeypatch.setattr(decode, "MAX_DECODED_BYTES", 1000)
    with pytest.raises(DecodeError, match="too long"):
        decode.decode_bytes(data, ".wav")


def test_fixture_library_decodes(library):
    paths = [p for p in library.rglob("*") if p.suffix.lower() in {".wav", ".flac", ".ogg", ".mp3"}]
    assert len(paths) >= 10
    for p in paths:
        audio = decode.decode_path(p)
        assert audio.frames > 0 and np.isfinite(audio.samples).all(), p
