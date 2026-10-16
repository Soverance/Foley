"""Spectrogram + waveform images (pipeline stage 5), rendered on demand and cached.

CONTRACT:

    IMAGE_VERSION: str     bump when render settings change (invalidates the cache)

    render_overview(cfg, conn, asset_id: int, force: bool = False) -> Path
        Absolute path of the cached PNG for the asset, rendering it first if missing,
        stale (version differs) or force. Decodes from the library via
        decode.decode_path(cfg.abs_path(rel_path)). Records the `images` row
        (kind='overview', local_path relative to cfg.data_dir).
        Layout (fixed scales are mandatory so images compare side by side):
          1024x512 PNG. Header: filename, duration, integrated LUFS (or "short"),
          true peak. Top: waveform (mono, linear -1..1) with RMS envelope.
          Bottom: log-mel spectrogram, 128 mel bands, 20 Hz to Nyquist (of a fixed
          analysis rate, 48 kHz -> 24 kHz, so all images share a y axis),
          fixed -80..0 dB range, one fixed colormap (CMAP_STOPS). Time axis labelled
          in seconds. Dark navy theme matching the `foley browse` UI.

    OVERVIEW_PLOT_X: tuple[float, float]
        Horizontal fractions of the overview PNG (0 = left edge, 1 = right edge) where
        the plot's time axis starts and ends: PANEL_LEFT_PX / OVERVIEW_W and
        1 - PANEL_RIGHT_PX / OVERVIEW_W (62 px each side at 1024 px, about
        0.0605..0.9395). t = 0 sits at the first value. The axis spans
        max(duration, MIN_SPAN_S), so t = duration sits at the second value for any
        clip of at least MIN_SPAN_S and proportionally before it for shorter ones.
        Lets a UI draw a playhead over the image. Changing the panel margins changes
        this constant and must bump IMAGE_VERSION.

    render_comparison(cfg, conn, asset_ids: list[int]) -> Path
        2-6 assets. One grid PNG of their overview images at identical scales,
        each labelled with asset id and filename, saved under
        cfg.image_dir / "comparisons". For grids, use a shared time axis length
        (the longest clip) so durations compare visually.

Notes on the scales (identical in overviews and grids):
    * Waveform y is fixed at -1..1 (linear), drawn in cyan; dashed lines mark -6 dBFS
      (0.5) and -20 dBFS (0.1). The orange line is the RMS envelope (mirrored).
    * Spectrogram level is dBFS, 0 dB = a full-scale sine, clipped to -80..0, with a
      custom colormap (CMAP_STOPS): near-black navy = silence (-80), through deep blue
      and blue to cyan, white = loud (0). Rows are 128 mel bands from 20 Hz to 24 kHz,
      labelled in Hz.
    * In grids the time axis spans the longest clip; the region past the end of a
      shorter clip is a lighter navy with faint diagonal hatching and an "end" line
      (not the colormap's navy-black, which means digital silence).
    * Grids are always re-rendered (cheap) and named from the ordered ids.
    * Errors (unknown id, missing file, wrong id count) raise ValueError.
"""

from __future__ import annotations

import functools
import sqlite3
import warnings
from pathlib import Path

import numpy as np

from foley import decode
from foley.catalog import db
from foley.config import Config

IMAGE_VERSION = "2"

ANALYSIS_SR = 48000
N_MELS = 128
FMIN = 20.0
FMAX = 24000.0
N_FFT = 2048
DB_MIN, DB_MAX = -80.0, 0.0
# Spectrogram colormap: (level 0..1 over DB_MIN..DB_MAX, colour). Changing it must bump
# IMAGE_VERSION and the colour descriptions in the module docstring and mcp_server.
CMAP_STOPS = ((0.0, "#03060f"), (0.30, "#0e165c"), (0.55, "#2a4be0"), (0.80, "#3fd0ff"),
              (1.0, "#ecfcff"))

_BG = "#03061a"
_FG = "#a9bedb"
_PAST_END = "#0a1534"
_WAVE = "#5fd4ff"
_RMS = "#ff7a3d"
_GRID = "#5fd4ff"  # always drawn at low alpha
_GRID_SOFT = "#5fd4ff59"  # spines and outlines (alpha .35)

OVERVIEW_W, OVERVIEW_H = 1024, 512
MIN_SPAN_S = 0.05
# Panel margins at scale 1 (multiplied by the panel scale in grids).
PANEL_LEFT_PX = 62
PANEL_RIGHT_PX = 62  # with the dBFS colorbar (overviews)
PANEL_RIGHT_PX_NO_COLORBAR = 16  # grids
OVERVIEW_PLOT_X = (PANEL_LEFT_PX / OVERVIEW_W, 1.0 - PANEL_RIGHT_PX / OVERVIEW_W)
_HZ_TICKS = [100, 500, 1000, 2000, 5000, 10000, 20000]


# ---------------------------------------------------------------- analysis


@functools.lru_cache(maxsize=1)
def _mel_basis() -> np.ndarray:
    import librosa

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return librosa.filters.mel(
            sr=ANALYSIS_SR, n_fft=N_FFT, n_mels=N_MELS, fmin=FMIN, fmax=FMAX, norm=None
        ).astype(np.float32)


@functools.lru_cache(maxsize=1)
def _cmap():
    from matplotlib.colors import LinearSegmentedColormap

    return LinearSegmentedColormap.from_list("foley_ion", list(CMAP_STOPS), N=256)


@functools.lru_cache(maxsize=1)
def _mel_freqs() -> np.ndarray:
    import librosa

    return librosa.mel_frequencies(n_mels=N_MELS, fmin=FMIN, fmax=FMAX)


def _mel_db(y: np.ndarray, columns: int) -> np.ndarray:
    """Log-mel in dBFS (0 dB = full-scale sine), shape (N_MELS, frames), clipped -80..0."""
    import librosa

    hop = max(256, int(np.ceil(len(y) / max(columns, 1))))
    if len(y) < N_FFT:
        y = np.pad(y, (0, N_FFT - len(y)))
    spec = librosa.stft(y, n_fft=N_FFT, hop_length=hop, center=True)
    power = (np.abs(spec) ** 2) / (N_FFT / 4.0) ** 2
    mel = _mel_basis() @ power
    level = 10.0 * np.log10(np.maximum(mel, 1e-10))  # power_to_db with ref=1.0
    return np.clip(level, DB_MIN, DB_MAX).astype(np.float32)


def _wave_columns(y: np.ndarray, columns: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Min, max and RMS per drawing column (decimation for any length)."""
    n = len(y)
    if n == 0:
        z = np.zeros(1, np.float32)
        return z, z, z
    columns = max(1, min(columns, n))
    edges = np.linspace(0, n, columns + 1).astype(np.int64)
    starts = edges[:-1]
    lo = np.minimum.reduceat(y, starts)
    hi = np.maximum.reduceat(y, starts)
    sq = np.add.reduceat(y.astype(np.float64) ** 2, starts)
    rms = np.sqrt(sq / np.maximum(np.diff(edges), 1))
    return lo, hi, rms.astype(np.float32)


class _Clip:
    def __init__(self, asset_id: int, name: str, audio, feats):
        self.asset_id = asset_id
        self.name = name
        self.feats = feats
        self.y = np.asarray(audio.resampled(ANALYSIS_SR), dtype=np.float32)
        self.duration = float(audio.duration_s) if audio.duration_s else len(self.y) / ANALYSIS_SR


# ---------------------------------------------------------------- drawing


def _fmt_header(clip: _Clip) -> str:
    f = clip.feats
    d = clip.duration
    parts = [f"{d:.2f} s" if d < 100 else f"{d:.1f} s"]
    if f is None:
        parts += ["LUFS n/a", "TP n/a"]
    else:
        lufs = f["lufs_integrated"]
        parts.append("short clip" if lufs is None else f"{lufs:.1f} LUFS")
        tp = f["true_peak_db"]
        parts.append("TP n/a" if tp is None else f"TP {tp:.1f} dBTP")
    return "   |   ".join(parts)


def _amp_label(v: float, _pos) -> str:
    for val, text in ((1, "1.0"), (-1, "-1.0"), (0.5, "-6 dB"), (0.1, "-20 dB")):
        if abs(v - val) < 1e-9:
            return text
    return ""


def _draw_panel(fig, clip: _Clip, box, span: float, label: str, scale: float,
                colorbar: bool, fig_w: int, fig_h: int) -> None:
    """Draw one waveform + spectrogram panel into pixel box (x, y_top, w, h)."""
    from matplotlib.ticker import AutoLocator, FixedLocator, FuncFormatter

    x0, y0, w, h = box

    def rect(px, py_top, pw_, ph_):
        return [px / fig_w, 1 - (py_top + ph_) / fig_h, pw_ / fig_w, ph_ / fig_h]

    fs = 10.5 * scale
    small = 8.5 * scale
    left = int(PANEL_LEFT_PX * scale)
    right = int((PANEL_RIGHT_PX if colorbar else PANEL_RIGHT_PX_NO_COLORBAR) * scale)
    head = int(46 * scale)
    gap = int(8 * scale)
    bottom = int(44 * scale)
    plot_h = h - head - bottom - gap
    wave_h = int(plot_h * 0.36)
    spec_h = plot_h - wave_h
    pw = w - left - right
    px = x0 + left

    fig.text((x0 + 6) / fig_w, 1 - (y0 + 4) / fig_h, label, color=_FG,
             fontsize=fs + 1.5, fontweight="bold", va="top", ha="left")
    fig.text((x0 + 6) / fig_w, 1 - (y0 + 25 * scale) / fig_h, _fmt_header(clip),
             color=_FG, fontsize=fs, va="top", ha="left")

    n = len(clip.y)
    dur = n / ANALYSIS_SR if n else clip.duration
    frac = min(1.0, dur / span)
    ncols = max(8, int(round(pw * frac)))

    # waveform ---------------------------------------------------------
    ax = fig.add_axes(rect(px, y0 + head, pw, wave_h), facecolor=_PAST_END)
    lo, hi, rms = _wave_columns(clip.y, ncols)
    edges = np.linspace(0, dur, len(lo) + 1)
    tc = (edges[:-1] + edges[1:]) / 2
    ax.axvspan(0, dur, color=_BG, lw=0)
    for lvl in (0.5, 0.1):
        for s in (1, -1):
            ax.axhline(s * lvl, color=_GRID, lw=0.8, ls=(0, (4, 3)), alpha=0.4)
    ax.axhline(0, color=_GRID, lw=0.5, alpha=0.3)
    ax.fill_between(tc, lo, hi, color=_WAVE, lw=0.6)
    ax.plot(tc, rms, color=_RMS, lw=1.2 * scale)
    ax.plot(tc, -rms, color=_RMS, lw=1.2 * scale)
    ax.set_xlim(0, span)
    ax.set_ylim(-1, 1)
    ax.yaxis.set_major_locator(FixedLocator([-1, -0.5, -0.1, 0.1, 0.5, 1]))
    ax.yaxis.set_major_formatter(FuncFormatter(_amp_label))
    ax.xaxis.set_major_locator(AutoLocator())
    ax.tick_params(colors=_FG, labelsize=small, length=3)
    ax.tick_params(axis="x", labelbottom=False)
    ax.grid(axis="x", color=_GRID, alpha=0.14, lw=0.6)
    ax.set_ylabel("amplitude", color=_FG, fontsize=small, labelpad=2)
    for sp in ax.spines.values():
        sp.set_color(_GRID_SOFT)
    ax.text(0.995, 0.93, "RMS", color=_RMS, fontsize=small, transform=ax.transAxes,
            ha="right", va="top", fontweight="bold")

    # spectrogram ------------------------------------------------------
    ax2 = fig.add_axes(rect(px, y0 + head + wave_h + gap, pw, spec_h), facecolor=_PAST_END)
    mel = _mel_db(clip.y, ncols)
    im = ax2.imshow(mel, origin="lower", aspect="auto", cmap=_cmap(), vmin=DB_MIN, vmax=DB_MAX,
                    extent=(0, dur, -0.5, N_MELS - 0.5), interpolation="nearest")
    ax2.set_xlim(0, span)
    ax2.set_ylim(-0.5, N_MELS - 0.5)
    pos = [float(np.interp(f, _mel_freqs(), np.arange(N_MELS))) for f in _HZ_TICKS]
    ax2.yaxis.set_major_locator(FixedLocator(pos))
    lab = {
        round(p, 6): (f"{f / 1000:g}k" if f >= 1000 else str(f))
        for f, p in zip(_HZ_TICKS, pos, strict=True)
    }
    ax2.yaxis.set_major_formatter(FuncFormatter(lambda v, _: lab.get(round(v, 6), "")))
    ax2.xaxis.set_major_locator(AutoLocator())
    ax2.tick_params(colors=_FG, labelsize=small, length=3)
    ax2.set_ylabel("Hz (mel scale)", color=_FG, fontsize=small, labelpad=2)
    ax2.set_xlabel("time (s)", color=_FG, fontsize=small, labelpad=2)
    ax2.grid(axis="x", color=_GRID, alpha=0.14, lw=0.6)
    for sp in ax2.spines.values():
        sp.set_color(_GRID_SOFT)
    if dur < span * 0.999:
        for a in (ax, ax2):
            a.axvspan(dur, span, facecolor="none", edgecolor=_GRID, alpha=0.18, hatch="////",
                      lw=0)
            a.axvline(dur, color=_FG, lw=1.0, alpha=0.9)
        ax2.text(dur, N_MELS * 0.97, " end", color=_FG, fontsize=small, va="top", ha="left")

    if colorbar:
        cax = fig.add_axes(rect(px + pw + int(12 * scale), y0 + head + wave_h + gap,
                                int(12 * scale), spec_h))
        cb = fig.colorbar(im, cax=cax)
        cb.set_ticks([-80, -60, -40, -20, 0])
        cb.ax.tick_params(colors=_FG, labelsize=small, length=2)
        cb.outline.set_edgecolor(_GRID_SOFT)
        cax.set_title("dBFS", color=_FG, fontsize=small, pad=3, loc="left")


def _new_figure(w: int, h: int):
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    fig = Figure(figsize=(w / 100, h / 100), dpi=100, facecolor=_BG)
    FigureCanvasAgg(fig)
    return fig


def _save(fig, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fig.canvas.print_png(fh)
    tmp.replace(path)


# ---------------------------------------------------------------- catalog access


def _load_clip(cfg: Config, conn: sqlite3.Connection, asset_id: int) -> _Clip:
    row = conn.execute("SELECT id, rel_path FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown asset id {asset_id}")
    path = cfg.abs_path(row["rel_path"])
    if not path.is_file():
        raise ValueError(f"asset {asset_id}: file is missing: {path}")
    try:
        audio = decode.decode_path(path)
    except decode.DecodeError as e:
        raise ValueError(f"asset {asset_id}: cannot decode {path}: {e}") from e
    feats = conn.execute(
        "SELECT lufs_integrated, true_peak_db FROM features WHERE asset_id = ?", (asset_id,)
    ).fetchone()
    name = row["rel_path"].replace("\\", "/").rsplit("/", 1)[-1]
    return _Clip(asset_id, name, audio, feats)


def render_overview(
    cfg: Config, conn: sqlite3.Connection, asset_id: int, force: bool = False
) -> Path:
    out = cfg.image_dir / str(asset_id // 1000) / f"{asset_id}.png"
    row = conn.execute(
        "SELECT version FROM images WHERE asset_id = ? AND kind = 'overview'", (asset_id,)
    ).fetchone()
    if not force and row is not None and row["version"] == IMAGE_VERSION and out.is_file():
        return out
    clip = _load_clip(cfg, conn, asset_id)
    fig = _new_figure(OVERVIEW_W, OVERVIEW_H)
    span = max(clip.duration, MIN_SPAN_S)
    _draw_panel(fig, clip, (0, 0, OVERVIEW_W, OVERVIEW_H), span, clip.name, 1.0, True,
                OVERVIEW_W, OVERVIEW_H)
    _save(fig, out)
    rel = out.relative_to(cfg.data_dir).as_posix()
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO images(asset_id, kind, local_path, version, rendered_at) "
            "VALUES (?, 'overview', ?, ?, ?) "
            "ON CONFLICT(asset_id, kind) DO UPDATE SET local_path = excluded.local_path, "
            "version = excluded.version, rendered_at = excluded.rendered_at",
            (asset_id, rel, IMAGE_VERSION, db.utcnow()),
        )
    return out


def render_comparison(cfg: Config, conn: sqlite3.Connection, asset_ids: list[int]) -> Path:
    ids = [int(i) for i in asset_ids]
    if not 2 <= len(ids) <= 6:
        raise ValueError(f"render_comparison needs 2-6 asset ids, got {len(ids)}")
    clips = [_load_clip(cfg, conn, i) for i in ids]
    span = max(max(c.duration for c in clips), MIN_SPAN_S)
    n = len(clips)
    cols = 1 if n <= 3 else 2
    rows = -(-n // cols)
    pw, ph = (1024, 360) if cols == 1 else (800, 380)
    top = 30
    fig_w, fig_h = pw * cols, ph * rows + top
    fig = _new_figure(fig_w, fig_h)
    fig.text(8 / fig_w, 1 - 7 / fig_h,
             f"Comparison, shared time axis 0 to {span:.2f} s  |  spectrogram dBFS: "
             "navy = -80 (silence), cyan to white = 0 (loud)  |  hatched = past end of clip",
             color=_FG, fontsize=9, va="top", ha="left")
    for k, clip in enumerate(clips):
        r, c = divmod(k, cols)
        _draw_panel(fig, clip, (c * pw, top + r * ph, pw, ph), span,
                    f"[{clip.asset_id}] {clip.name}", 0.85 if cols == 2 else 0.9, False,
                    fig_w, fig_h)
    out = cfg.image_dir / "comparisons" / ("cmp_" + "-".join(map(str, ids)) + ".png")
    _save(fig, out)
    return out
