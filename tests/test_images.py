from __future__ import annotations

from dataclasses import dataclass

import audiogen as ag
import numpy as np
import pytest
import soundfile as sf
from PIL import Image

from foley import decode, images
from foley.catalog import db


@dataclass
class FakeDecoded:
    """DecodedAudio-shaped stand-in so these tests don't depend on foley.decode."""

    samples: np.ndarray
    sample_rate: int
    native_sample_rate: int

    @property
    def duration_s(self) -> float:
        return self.samples.shape[0] / self.sample_rate

    def mono(self) -> np.ndarray:
        return self.samples.mean(axis=1).astype(np.float32)

    def resampled(self, sr: int) -> np.ndarray:
        import librosa

        y = self.mono()
        if sr != self.sample_rate:
            y = librosa.resample(y, orig_sr=self.sample_rate, target_sr=sr)
        return y.astype(np.float32)


def fake_decode_path(path):
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    return FakeDecoded(data, sr, sr)


@pytest.fixture(autouse=True)
def _fake_decode(monkeypatch):
    monkeypatch.setattr(decode, "decode_path", fake_decode_path)


@pytest.fixture
def conn(cfg):
    c = db.open_catalog(cfg)
    yield c
    c.close()


def add_asset(cfg, conn, name, x, sr=ag.SR, lufs=-18.0, tp=-3.0, features=True) -> int:
    path = ag.write(cfg.library_root / "img" / name, x, sr)
    now = db.utcnow()
    cur = conn.execute(
        "INSERT INTO assets(rel_path, ext, size_bytes, mtime_ns, first_seen, last_seen, status) "
        "VALUES (?, '.wav', ?, 0, ?, ?, 'active')",
        (f"img/{name}", path.stat().st_size, now, now),
    )
    aid = cur.lastrowid
    if features:
        conn.execute(
            "INSERT INTO features(asset_id, duration_s, lufs_integrated, true_peak_db) "
            "VALUES (?, ?, ?, ?)",
            (aid, len(x) / sr, lufs, tp),
        )
    return aid


def test_overview_size_cache_and_row(cfg, conn):
    aid = add_asset(cfg, conn, "burst.wav", ag.burst(1.0))
    p = images.render_overview(cfg, conn, aid)
    assert p == cfg.image_dir / str(aid // 1000) / f"{aid}.png"
    assert Image.open(p).size == (1024, 512)
    row = conn.execute("SELECT * FROM images WHERE asset_id = ?", (aid,)).fetchone()
    assert row["kind"] == "overview" and row["version"] == images.IMAGE_VERSION
    assert (cfg.data_dir / row["local_path"]) == p
    m = p.stat().st_mtime_ns
    assert images.render_overview(cfg, conn, aid) == p
    assert p.stat().st_mtime_ns == m  # cached


def test_invalidation(cfg, conn, monkeypatch):
    aid = add_asset(cfg, conn, "tone.wav", ag.tone(440, 0.5))
    p = images.render_overview(cfg, conn, aid)
    m = p.stat().st_mtime_ns
    images.render_overview(cfg, conn, aid, force=True)
    assert p.stat().st_mtime_ns > m
    m = p.stat().st_mtime_ns
    p.unlink()
    images.render_overview(cfg, conn, aid)
    assert p.is_file()
    m = p.stat().st_mtime_ns
    monkeypatch.setattr(images, "IMAGE_VERSION", "999")
    images.render_overview(cfg, conn, aid)
    assert p.stat().st_mtime_ns > m
    row = conn.execute("SELECT version FROM images WHERE asset_id = ?", (aid,)).fetchone()
    assert row["version"] == "999"


def test_missing_features_and_short_clip(cfg, conn):
    a = add_asset(cfg, conn, "nofeat.wav", ag.click(), features=False)
    b = add_asset(cfg, conn, "short.wav", ag.click(), lufs=None)
    assert Image.open(images.render_overview(cfg, conn, a)).size == (1024, 512)
    assert Image.open(images.render_overview(cfg, conn, b)).size == (1024, 512)


def test_resamples_and_long_stereo(cfg, conn):
    x = ag.stereo(ag.noise(1.0, -20, sr=22050), ag.tone(300, 1.0, sr=22050))
    aid = add_asset(cfg, conn, "st.wav", x, sr=22050)
    assert images.render_overview(cfg, conn, aid).is_file()
    long = add_asset(cfg, conn, "long.wav", ag.noise(120, -30, sr=8000), sr=8000)
    assert images.render_overview(cfg, conn, long).is_file()


def test_comparison_grid(cfg, conn):
    ids = [add_asset(cfg, conn, f"c{i}.wav", ag.burst(0.3 * (i + 1))) for i in range(6)]
    p = images.render_comparison(cfg, conn, ids)
    assert p.parent == cfg.image_dir / "comparisons"
    w, h = Image.open(p).size
    assert w >= 1024 and h > 512
    p2 = images.render_comparison(cfg, conn, ids[:2])
    assert p2 != p and p2.is_file()


def test_errors(cfg, conn):
    aid = add_asset(cfg, conn, "e.wav", ag.click())
    with pytest.raises(ValueError, match="unknown"):
        images.render_overview(cfg, conn, 9999)
    with pytest.raises(ValueError, match="unknown"):
        images.render_comparison(cfg, conn, [aid, 9999])
    with pytest.raises(ValueError):
        images.render_comparison(cfg, conn, [aid])
    with pytest.raises(ValueError):
        images.render_comparison(cfg, conn, [aid] * 7)
    (cfg.library_root / "img" / "e.wav").unlink()
    with pytest.raises(ValueError, match="missing"):
        images.render_overview(cfg, conn, aid, force=True)
