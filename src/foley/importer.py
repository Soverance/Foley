"""import_to_project: copy a library file into a Godot project, optionally normalized.

CONTRACT:

    load_project_config(project_root: Path) -> dict
        Reads <project_root>/foley.project.toml if present:
          [project] name = "endless-reach"
          [loudness] oneshot_peak_db = -3.0
          [loudness.targets]  "sfx.ui" = -20.0, "sfx.weapon" = -14.0, "music" = -16.0
          [review] hero_slots = ["music.title", "sfx.boss.*"]
        Target for a usage_key = longest dotted-prefix match in loudness.targets.
        Missing file -> {} (name defaults to the project folder name).

    import_to_project(cfg, conn, asset_id: int, project_root: Path, dest: str,
                      usage_key: str | None = None, target_lufs: float | None = None,
                      overwrite: bool = False, decided_by: str = "claude-code") -> dict
        dest is a res:// path (or a path relative to project_root). Refuses to
        overwrite unless overwrite=True. Never writes outside project_root.
        Normalization rules (docs/handoffs/AudioCatalog.md, MCP tool surface):
          target = target_lufs or the project config target for usage_key or None;
          clips >= 0.4 s use integrated LUFS, shorter use true peak -> oneshot_peak_db;
          lossless sources (wav, flac, aif) are written normalized in their own format
          and bit depth (float output only if the source was float);
          lossy sources (mp3, ogg) are copied unmodified and the gain is recorded for
          volume_db (gain_applied = 0).
          If the gain would push true peak above -1 dBTP, cap it and report that.
        Records a ledger `usages` row. Returns {project_path, gain_db, gain_applied,
        normalized_to_lufs, capped, license, warnings}.

DESIGN NOTES:

    The measurement comes from the catalog `features` row when it has the needed
    values, else the file is decoded and measured. The library file is only ever read.
    Output goes to a temp file in the destination folder, then os.replace. A source
    whose decoded rate differs from its native rate (very long files) is copied
    unmodified with the gain recorded, like a lossy file. Raises ValueError for bad
    arguments (path escape, extension mismatch, unknown asset), FileExistsError when
    the destination exists and overwrite is False.
"""

from __future__ import annotations

import math
import os
import shutil
import sqlite3
import tempfile
import tomllib
from pathlib import Path, PurePosixPath

import numpy as np

from foley import decode, loudness
from foley.catalog import db
from foley.config import Config

PEAK_CEILING_DB = -1.0
LOSSLESS_FORMATS = {".wav": "WAV", ".flac": "FLAC", ".aif": "AIFF", ".aiff": "AIFF"}
LOSSY_EXTENSIONS = frozenset({".mp3", ".ogg"})
_FLOAT_SUBTYPES = frozenset({"FLOAT", "DOUBLE"})


def load_project_config(project_root: Path) -> dict:
    path = Path(project_root) / "foley.project.toml"
    if not path.is_file():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def target_for_usage(config: dict, usage_key: str | None) -> float | None:
    """Longest dotted-prefix match of usage_key in [loudness.targets]."""
    if not usage_key:
        return None
    targets = (config.get("loudness") or {}).get("targets") or {}
    best, best_len = None, -1
    for key, value in targets.items():
        if (usage_key == key or usage_key.startswith(key + ".")) and len(key) > best_len:
            best, best_len = value, len(key)
    return float(best) if best is not None else None


def resolve_destination(project_root: Path, dest: str) -> tuple[Path, str]:
    """Map a res:// (or relative) path under project_root. Returns (absolute, res:// path)."""
    root = Path(project_root).resolve()
    rel = dest.strip().replace("\\", "/")
    if rel.startswith("res://"):
        rel = rel[len("res://") :]
    rel = rel.lstrip("/")
    pure = PurePosixPath(rel)
    if not rel or ":" in rel or ".." in pure.parts:
        raise ValueError(f"destination escapes the project: {dest!r}")
    target = (root / Path(*pure.parts)).resolve()
    if not target.is_relative_to(root) or target == root:
        raise ValueError(f"destination escapes the project: {dest!r}")
    return target, "res://" + target.relative_to(root).as_posix()


def _read_measurement(features: sqlite3.Row | None) -> dict | None:
    if features is None:
        return None
    measured = {
        "lufs_integrated": features["lufs_integrated"],
        "lufs_short_max": features["lufs_short_max"],
        "true_peak_db": features["true_peak_db"],
    }
    return measured if measured["true_peak_db"] is not None else None


def import_to_project(
    cfg: Config,
    conn: sqlite3.Connection,
    asset_id: int,
    project_root: Path,
    dest: str,
    usage_key: str | None = None,
    target_lufs: float | None = None,
    overwrite: bool = False,
    decided_by: str = "claude-code",
) -> dict:
    project_root = Path(project_root)
    if not project_root.is_dir():
        raise ValueError(f"project_root is not a directory: {project_root}")
    row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if row is None:
        raise ValueError(f"no such asset: {asset_id}")
    if not row["content_hash"]:
        raise ValueError(f"asset {asset_id} has not been hashed yet; run `foley index`")
    ext = row["ext"].lower()
    src = cfg.abs_path(row["rel_path"])
    if not src.is_file():
        raise FileNotFoundError(f"source file is missing from the library: {row['rel_path']}")

    st = src.stat()
    if st.st_size != row["size_bytes"] or st.st_mtime_ns != row["mtime_ns"]:
        raise ValueError(
            f"{row['rel_path']} changed on disk since it was indexed; "
            "run `foley index` and try again"
        )

    st = src.stat()
    if st.st_size != row["size_bytes"] or st.st_mtime_ns != row["mtime_ns"]:
        raise ValueError(
            f"{row['rel_path']} changed on disk since it was indexed; "
            "run `foley index` and try again"
        )

    out_path, project_path = resolve_destination(project_root, dest)
    if out_path.suffix.lower() != ext:
        raise ValueError(
            f"destination extension {out_path.suffix or '(none)'!r} must match the source ({ext})"
        )
    library = Path(cfg.library_root).resolve()
    if out_path.is_relative_to(library):
        raise ValueError("refusing to write inside the audio library")
    if out_path.exists() and not overwrite:
        raise FileExistsError(f"{project_path} already exists; pass overwrite=true to replace it")

    config = load_project_config(project_root)
    project_name = (config.get("project") or {}).get("name") or project_root.resolve().name
    oneshot_peak = (config.get("loudness") or {}).get("oneshot_peak_db")
    target = target_lufs if target_lufs is not None else target_for_usage(config, usage_key)

    warnings: list[str] = []
    feat = conn.execute("SELECT * FROM features WHERE asset_id = ?", (asset_id,)).fetchone()
    duration = feat["duration_s"] if feat is not None else None
    measured = _read_measurement(feat)
    lossless = ext in LOSSLESS_FORMATS
    audio = None
    if lossless or measured is None or duration is None:
        audio = decode.decode_path(src)
        if lossless or measured is None:
            # Lossless files are rewritten, so the cap must come from the real samples.
            measured = loudness.measure(audio)
        if lossless or duration is None:
            duration = audio.duration_s

    gain = loudness.gain_for_target(measured, duration, target, oneshot_peak)
    capped = False
    if gain is not None:
        peak = measured.get("true_peak_db")
        if peak is not None and peak + gain > PEAK_CEILING_DB:
            gain = PEAK_CEILING_DB - peak
            capped = True
            warnings.append(
                f"gain capped to {gain:+.2f} dB to keep true peak at {PEAK_CEILING_DB:g} dBTP"
            )
        gain = round(gain, 2)
        if lossless and audio is not None and audio.samples.size:
            sample_peak = float(np.abs(audio.samples).max())
            if sample_peak > 0:
                ceiling = math.floor(-20 * math.log10(sample_peak) * 100) / 100
                if gain > ceiling:
                    gain = ceiling
                    capped = True
                    warnings.append("gain reduced to avoid clipping at full scale")
    else:
        if target is None and not (
            duration is not None and duration < 0.4 and oneshot_peak is not None
        ):
            warnings.append("no loudness target for this usage; file copied unmodified")
        else:
            warnings.append("not enough measurement to compute a gain; file copied unmodified")

    bake = lossless and gain is not None
    if bake and audio is not None and audio.sample_rate != audio.native_sample_rate:
        bake = False
        warnings.append(
            "decoded rate differs from the source; copied unmodified, apply gain via volume_db"
        )
    if not lossless and gain is not None:
        warnings.append(f"lossy source copied unmodified; apply {gain:+.2f} dB via volume_db")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".foley-", suffix=ext, dir=out_path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        if bake:
            _write_normalized(src, tmp, audio, gain, ext, warnings)
        else:
            shutil.copy2(src, tmp)
        os.replace(tmp, out_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    gain_applied = 1 if bake else 0
    short_clip = duration is not None and duration < 0.4
    normalized_to_lufs = None
    if gain is not None and not short_clip:
        lufs = measured.get("lufs_integrated")
        normalized_to_lufs = round(lufs + gain, 2) if capped and lufs is not None else target

    with db.transaction(conn):
        conn.execute(
            "INSERT INTO ledger.usages(content_hash, rel_path, project, project_path, usage_key, "
            "imported_at, normalized_to_lufs, gain_db, gain_applied, license) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["content_hash"],
                row["rel_path"],
                project_name,
                project_path,
                usage_key,
                db.utcnow(),
                normalized_to_lufs,
                gain,
                gain_applied,
                row["license"],
            ),
        )
    return {
        "project_path": project_path,
        "gain_db": gain,
        "gain_applied": bool(gain_applied),
        "normalized_to_lufs": normalized_to_lufs,
        "capped": capped,
        "license": row["license"],
        "warnings": warnings,
    }


def _write_normalized(
    src: Path, tmp: Path, audio, gain: float, ext: str, warnings: list[str]
) -> None:
    import soundfile as sf

    subtype = None
    try:
        info = sf.info(str(src))
        subtype = info.subtype
    except Exception:  # noqa: BLE001 - fall back below
        pass
    if not subtype:
        subtype = "PCM_24"
        warnings.append("source bit depth unknown; wrote PCM_24")
    samples = loudness.apply_gain(audio.samples, gain)
    try:
        sf.write(
            str(tmp), samples, audio.sample_rate, subtype=subtype, format=LOSSLESS_FORMATS[ext]
        )
    except Exception:
        warnings.append(f"could not keep subtype {subtype}; wrote PCM_24")
        sf.write(
            str(tmp), samples, audio.sample_rate, subtype="PCM_24", format=LOSSLESS_FORMATS[ext]
        )
