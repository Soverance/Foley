"""Templated one-line captions (Phase 1). A captioning model is evaluated in Phase 3.

CONTRACT:

    CAPTION_VERSION: str
    CAPTION_MODEL = "template-v1"

    templated_caption(asset: Mapping, features: Mapping, tags: list[tuple[str, float]],
                      metadata: Mapping | None) -> str
        One sentence from filename/UCS/metadata, features and tags, e.g.
        "0.42 s bright metallic impact, sharp attack, short tail, -14 LUFS".
        Brightness from centroid / band energy, attack/tail descriptors from
        attack_s and tail/decay, loudness from lufs_integrated (or true peak for
        short clips). Robust to any value being None.

DESIGN (thresholds are module constants so they are easy to tune):

    Shape: "<duration> s [descriptors] <noun phrase>, <attack>, <tail>, <loudness>".
    Every clause is omitted when its measurement is missing; nothing is inferred
    beyond the numbers.

    duration      "0.42 s" (two decimals under 10 s, one decimal under 100 s, else m:ss).
    brightness    "bright" when centroid_hz_mean > 3000 Hz or (presence+air)/total band
                  energy > 0.35; "dark" when centroid < 800 Hz and that share < 0.05.
                  Neither otherwise (mid-range sounds get no brightness word).
    texture       "noisy" when flatness_mean > 0.3. No "tonal": on real libraries mean
                  spectral flatness is near 0 for almost everything (median ~0.0025,
                  incl. machinery and underwater ambiences), so it cannot support that
                  claim. Revisit with AudioSet tags in Phase 1b.
    attack        "sharp attack" when attack_s < 0.010; "slow attack" when > 0.25.
    tail          only for clips under 10 s: "short tail" when decay_s < 0.3;
                  "long tail" when decay_s > 1.5.
    loudness      "-14 LUFS" from lufs_integrated; clips without it (under 0.4 s)
                  report "peak -6 dBTP" from true_peak_db.
    noun phrase   1) UCS subcategory / fx_name words (lowercased, deduplicated; the
                  UCS category when neither exists), 2) words of the file name minus
                  numbers, extension and filler like "copy", 3) the best tag with
                  score >= 0.3, 4) "music" / "sound" from is_music.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from foley.search import split_words

CAPTION_VERSION = "2"
CAPTION_MODEL = "template-v1"

BRIGHT_CENTROID_HZ = 3000.0
BRIGHT_HF_SHARE = 0.35
DARK_CENTROID_HZ = 800.0
DARK_HF_SHARE = 0.05
NOISY_FLATNESS = 0.3
SHARP_ATTACK_S = 0.010
SLOW_ATTACK_S = 0.25
SHORT_TAIL_S = 0.3
LONG_TAIL_S = 1.5
TAIL_MAX_CLIP_S = 10.0
MIN_TAG_SCORE = 0.3
_FILLER_WORDS = frozenset({"copy", "final", "new", "wav", "mp3", "ogg", "flac", "aif", "aiff", "v"})
_MAX_NAME_WORDS = 4


def _num(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


def _band_energy(features: Mapping) -> Mapping | None:
    be = features.get("band_energy")
    if isinstance(be, str):
        try:
            be = json.loads(be)
        except ValueError:
            return None
    return be if isinstance(be, Mapping) else None


def _hf_share(features: Mapping) -> float | None:
    be = _band_energy(features)
    if not be:
        return None
    values = {k: _num(v) for k, v in be.items()}
    total = sum(v for v in values.values() if v is not None and v > 0)
    if total <= 0:
        return None
    hf = sum(max(values.get(k) or 0.0, 0.0) for k in ("presence", "air"))
    return hf / total


def _duration_text(seconds: float) -> str:
    if seconds < 10:
        return f"{seconds:.2f} s"
    if seconds < 100:
        return f"{seconds:.1f} s"
    minutes, rest = divmod(int(round(seconds)), 60)
    return f"{minutes}:{rest:02d} min"


def _noun_phrase(asset: Mapping, tags, metadata: Mapping | None, features: Mapping) -> str:
    ucs = (metadata or {}).get("ucs") if isinstance(metadata, Mapping) else None
    if isinstance(ucs, Mapping):
        words: list[str] = []
        for key in ("subcategory", "fx_name"):
            words += split_words(str(ucs.get(key) or ""))
        if not words:
            words = split_words(str(ucs.get("category") or ""))
        words = [w for w in dict.fromkeys(words) if not w.isdigit()]
        if words:
            return " ".join(words[:_MAX_NAME_WORDS])
    rel = str(asset.get("rel_path") or "")
    if rel:
        words = [
            w
            for w in split_words(Path(rel).stem)
            if not w.isdigit() and w not in _FILLER_WORDS and len(w) > 1
        ]
        words = list(dict.fromkeys(words))
        if words:
            return " ".join(words[:_MAX_NAME_WORDS])
    best = [(label, s) for label, s in (tags or []) if (_num(s) or 0.0) >= MIN_TAG_SCORE]
    if best:
        return str(max(best, key=lambda t: _num(t[1]) or 0.0)[0]).lower()
    return "music" if features.get("is_music") else "sound"


def templated_caption(
    asset: Mapping,
    features: Mapping,
    tags: list[tuple[str, float]],
    metadata: Mapping | None,
) -> str:
    asset = asset or {}
    features = features or {}
    duration = _num(features.get("duration_s"))
    centroid = _num(features.get("centroid_hz_mean"))
    flatness = _num(features.get("flatness_mean"))
    attack = _num(features.get("attack_s"))
    decay = _num(features.get("decay_s"))
    lufs = _num(features.get("lufs_integrated"))
    peak = _num(features.get("true_peak_db"))
    hf = _hf_share(features)

    adjectives: list[str] = []
    if (centroid is not None and centroid > BRIGHT_CENTROID_HZ) or (
        hf is not None and hf > BRIGHT_HF_SHARE
    ):
        adjectives.append("bright")
    elif (
        centroid is not None and centroid < DARK_CENTROID_HZ and (hf is None or hf < DARK_HF_SHARE)
    ):
        adjectives.append("dark")
    if flatness is not None and flatness > NOISY_FLATNESS:
        adjectives.append("noisy")

    head = " ".join(
        part
        for part in (
            _duration_text(duration) if duration is not None else "",
            " ".join(adjectives),
            _noun_phrase(asset, tags, metadata, features),
        )
        if part
    )
    clauses = [head]
    if attack is not None:
        if attack < SHARP_ATTACK_S:
            clauses.append("sharp attack")
        elif attack > SLOW_ATTACK_S:
            clauses.append("slow attack")
    if decay is not None and (duration is None or duration < TAIL_MAX_CLIP_S):
        if decay < SHORT_TAIL_S:
            clauses.append("short tail")
        elif decay > LONG_TAIL_S:
            clauses.append("long tail")
    if lufs is not None:
        clauses.append(f"{lufs:.0f} LUFS")
    elif peak is not None:
        clauses.append(f"peak {peak:.0f} dBTP")
    return ", ".join(clauses)
