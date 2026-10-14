from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from _catalog_builder import add_asset

from foley import decode, importer, loudness
from foley.catalog import db

SHORT = "sfx/click.wav"
MAYBE = "sfx/CancelMaybe.wav"
FLAC = "packs/Test Bundle 2015/Water 01/Water_Fountain_Fienup_002.flac"
MP3 = "music/Arteria3D/Battle Loop.mp3"


@pytest.fixture
def conn(cfg):
    c = db.open_catalog(cfg)
    yield c
    c.close()


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "game"
    root.mkdir()
    return root


def asset(conn, rel, **kw):
    aid = add_asset(conn, rel, license="packs/LICENSE.txt", **kw)
    st = Path(db.get_meta(conn, "library_root")).joinpath(*rel.split("/")).stat()
    conn.execute(
        "UPDATE assets SET size_bytes = ?, mtime_ns = ? WHERE id = ?",
        (st.st_size, st.st_mtime_ns, aid),
    )
    return aid


def usage_rows(conn):
    return conn.execute("SELECT * FROM ledger.usages ORDER BY id").fetchall()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def measure_file(path):
    return loudness.measure(decode.decode_path(path))


def test_load_project_config(project):
    assert importer.load_project_config(project) == {}
    (project / "foley.project.toml").write_text(
        '[project]\nname = "endless-reach"\n[loudness]\noneshot_peak_db = -3.0\n'
        '[loudness.targets]\n"sfx.ui" = -20.0\n"music" = -16.0\n[review]\nhero_slots = ["music.title"]\n'  # noqa: E501
    )
    cfg = importer.load_project_config(project)
    assert cfg["project"]["name"] == "endless-reach"
    assert importer.target_for_usage(cfg, "sfx.ui.click") == -20.0
    assert importer.target_for_usage(cfg, "music") == -16.0
    assert importer.target_for_usage(cfg, "sfx.weapon") is None
    assert importer.target_for_usage(cfg, "musical") is None
    assert importer.target_for_usage(cfg, None) is None


def test_longest_prefix_wins():
    config = {"loudness": {"targets": {"sfx": -20.0, "sfx.ui": -10.0}}}
    assert importer.target_for_usage(config, "sfx.ui.click") == -10.0
    assert importer.target_for_usage(config, "sfx.weapon.x") == -20.0


def test_lossless_normalized_in_place_format(cfg, conn, project):
    aid = asset(conn, FLAC)
    src = cfg.abs_path(FLAC)
    before = sha(src)
    out = importer.import_to_project(
        cfg,
        conn,
        aid,
        project,
        "res://audio/sfx/water.flac",
        usage_key="sfx.water",
        target_lufs=-16.0,
    )
    dest = project / "audio" / "sfx" / "water.flac"
    assert dest.is_file()
    assert out["project_path"] == "res://audio/sfx/water.flac"
    assert out["gain_applied"] is True and out["capped"] is False
    assert out["normalized_to_lufs"] == -16.0
    assert out["license"] == "packs/LICENSE.txt"
    assert measure_file(dest)["lufs_integrated"] == pytest.approx(-16.0, abs=0.15)
    assert sf.info(str(dest)).subtype == sf.info(str(src)).subtype
    assert sha(src) == before  # the library is never written
    assert [p.name for p in dest.parent.iterdir()] == ["water.flac"]  # no temp leftovers
    (row,) = usage_rows(conn)
    assert row["project"] == "game" and row["project_path"] == "res://audio/sfx/water.flac"
    assert row["usage_key"] == "sfx.water" and row["gain_applied"] == 1
    assert row["gain_db"] == out["gain_db"] and row["normalized_to_lufs"] == -16.0
    assert row["content_hash"] == f"hash-{FLAC}" and row["rel_path"] == FLAC


def test_bit_depth_preserved(cfg, conn, project):
    # The fixture WAVs are written by soundfile with its default subtype; check it survives.
    aid = asset(conn, "sfx/lightsabers/lasrhit3.WAV", ext=".wav")
    src = cfg.abs_path("sfx/lightsabers/lasrhit3.WAV")
    importer.import_to_project(cfg, conn, aid, project, "a/b.wav", target_lufs=-20.0)
    assert sf.info(str(project / "a" / "b.wav")).subtype == sf.info(str(src)).subtype


def test_cap_at_minus_one_dbtp(cfg, conn, project):
    aid = asset(conn, FLAC)
    out = importer.import_to_project(cfg, conn, aid, project, "n.flac", target_lufs=-3.0)
    assert out["capped"] is True
    assert any("capped" in w for w in out["warnings"])
    peak = measure_file(project / "n.flac")["true_peak_db"]
    assert peak == pytest.approx(-1.0, abs=0.1) and peak <= -0.9
    assert out["normalized_to_lufs"] < -3.0  # reports what was actually achieved


def test_short_clip_uses_peak_rule(cfg, conn, project):
    (project / "foley.project.toml").write_text(
        '[project]\nname = "endless-reach"\n[loudness]\noneshot_peak_db = -6.0\n'
    )
    aid = asset(conn, SHORT)
    out = importer.import_to_project(cfg, conn, aid, project, "res://click.wav", target_lufs=-20.0)
    assert out["gain_applied"] is True
    assert out["normalized_to_lufs"] is None
    assert measure_file(project / "click.wav")["true_peak_db"] == pytest.approx(-6.0, abs=0.1)
    assert usage_rows(conn)[0]["project"] == "endless-reach"


def test_lossy_copied_unmodified_with_gain_recorded(cfg, conn, project):
    aid = asset(
        conn,
        MP3,
        features={"duration_s": 8.0, "lufs_integrated": -14.0, "true_peak_db": -10.0},
    )
    out = importer.import_to_project(
        cfg, conn, aid, project, "res://music/battle.mp3", target_lufs=-16.0
    )
    assert out["gain_db"] == -2.0 and out["gain_applied"] is False
    assert out["normalized_to_lufs"] == -16.0
    assert sha(project / "music" / "battle.mp3") == sha(cfg.abs_path(MP3))
    (row,) = usage_rows(conn)
    assert row["gain_db"] == -2.0 and row["gain_applied"] == 0


def test_lossy_gain_capped_by_peak(cfg, conn, project):
    aid = asset(
        conn, MP3, features={"duration_s": 8.0, "lufs_integrated": -14.0, "true_peak_db": -2.0}
    )
    out = importer.import_to_project(cfg, conn, aid, project, "b.mp3", target_lufs=-6.0)
    assert out["capped"] is True and out["gain_db"] == 1.0


def test_usage_key_target_from_project_config(cfg, conn, project):
    (project / "foley.project.toml").write_text('[loudness.targets]\n"sfx.water" = -18.0\n')
    aid = asset(conn, FLAC)
    out = importer.import_to_project(cfg, conn, aid, project, "w.flac", usage_key="sfx.water.loop")
    assert out["normalized_to_lufs"] == -18.0
    assert measure_file(project / "w.flac")["lufs_integrated"] == pytest.approx(-18.0, abs=0.15)
    assert usage_rows(conn)[0]["project"] == "game"  # folder name fallback


def test_no_target_copies_unmodified(cfg, conn, project):
    aid = asset(conn, FLAC)
    out = importer.import_to_project(cfg, conn, aid, project, "w.flac")
    assert out["gain_db"] is None and out["gain_applied"] is False
    assert out["warnings"]
    assert sha(project / "w.flac") == sha(cfg.abs_path(FLAC))


def test_overwrite_refused_then_allowed(cfg, conn, project):
    aid = asset(conn, FLAC)
    importer.import_to_project(cfg, conn, aid, project, "w.flac", target_lufs=-30.0)
    first = sha(project / "w.flac")
    with pytest.raises(FileExistsError):
        importer.import_to_project(cfg, conn, aid, project, "w.flac", target_lufs=-20.0)
    assert sha(project / "w.flac") == first and len(usage_rows(conn)) == 1
    importer.import_to_project(cfg, conn, aid, project, "w.flac", target_lufs=-20.0, overwrite=True)
    assert sha(project / "w.flac") != first and len(usage_rows(conn)) == 2


@pytest.mark.parametrize(
    "dest",
    [
        "res://../escape.flac",
        "../escape.flac",
        "res://a/../../escape.flac",
        "a\\..\\..\\escape.flac",
        "C:/Windows/escape.flac",
        "res://",
        "",
    ],
)
def test_path_escape_refused(cfg, conn, project, dest):
    aid = asset(conn, FLAC)
    with pytest.raises(ValueError):
        importer.import_to_project(cfg, conn, aid, project, dest, target_lufs=-20.0)
    assert not usage_rows(conn)
    assert not (project.parent / "escape.flac").exists()


def test_extension_must_match(cfg, conn, project):
    aid = asset(conn, FLAC)
    with pytest.raises(ValueError, match="extension"):
        importer.import_to_project(cfg, conn, aid, project, "w.wav")
    assert not any(project.iterdir())


def test_refuses_to_write_into_library(cfg, conn, tmp_path):
    aid = asset(conn, FLAC)
    with pytest.raises(ValueError, match="library"):
        importer.import_to_project(
            cfg, conn, aid, cfg.library_root, "inside.flac", target_lufs=-20.0
        )


def test_unknown_asset_and_missing_root(cfg, conn, project, tmp_path):
    with pytest.raises(ValueError):
        importer.import_to_project(cfg, conn, 999, project, "w.flac")
    aid = asset(conn, FLAC)
    with pytest.raises(ValueError):
        importer.import_to_project(cfg, conn, aid, tmp_path / "nope", "w.flac")


def test_stale_catalog_row_refused(cfg, conn, project):
    aid = asset(conn, FLAC)
    with cfg.abs_path(FLAC).open("ab") as f:  # edited after the last index
        f.write(b"\0" * 16)
    with pytest.raises(ValueError, match="foley index"):
        importer.import_to_project(cfg, conn, aid, project, "w.flac", target_lufs=-20.0)
    assert not any(project.iterdir()) and not usage_rows(conn)


def test_lossless_uses_real_samples_not_stale_features(cfg, conn, project):
    # Catalog claims a quiet peak; the real file is hot. The cap must follow the samples.
    aid = asset(conn, SHORT, features={"duration_s": 0.05, "true_peak_db": -40.0})
    importer.import_to_project(cfg, conn, aid, project, "c.wav", target_lufs=-20.0)
    data, _ = sf.read(str(project / "c.wav"))
    assert np.abs(data).max() <= 1.0
