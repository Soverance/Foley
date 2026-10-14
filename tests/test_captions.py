from __future__ import annotations

from foley import captions


def cap(asset=None, features=None, tags=(), metadata=None):
    return captions.templated_caption(asset or {}, features or {}, list(tags), metadata)


def test_full_caption_from_numbers():
    text = cap(
        {"rel_path": "sfx/Metal Shelf Hit 10.wav"},
        {
            "duration_s": 0.42,
            "centroid_hz_mean": 4200.0,
            "attack_s": 0.004,
            "decay_s": 0.1,
            "lufs_integrated": -14.2,
            "flatness_mean": 0.1,
        },
    )
    assert text == "0.42 s bright metal shelf hit, sharp attack, short tail, -14 LUFS"


def test_ucs_noun_phrase_preferred():
    text = cap(
        {"rel_path": "x/GOREMisc_Cladding_NailScratch19.wav"},
        {"duration_s": 1.2},
        metadata={"ucs": {"category": "GORE", "subcategory": "MISC", "fx_name": "Cladding"}},
    )
    assert text == "1.20 s misc cladding"


def test_short_clip_uses_peak_and_skips_unsupported_claims():
    text = cap({"rel_path": "sfx/click.wav"}, {"duration_s": 0.05, "true_peak_db": -0.9})
    assert text == "0.05 s click, peak -1 dBTP"
    assert "bright" not in text and "attack" not in text and "tail" not in text


def test_dark_band_energy_and_long_tail():
    text = cap(
        {"rel_path": "a/rumble.wav"},
        {
            "duration_s": 5.0,
            "centroid_hz_mean": 300.0,
            "band_energy": '{"sub": 0.5, "low": 0.4, "presence": 0.01, "air": 0.0}',
            "decay_s": 2.0,
            "attack_s": 0.4,
            "lufs_integrated": -30.0,
        },
    )
    assert text == "5.00 s dark rumble, slow attack, long tail, -30 LUFS"


def test_bright_from_band_share_only():
    text = cap(
        {"rel_path": "a/hiss.wav"},
        {"duration_s": 2.0, "band_energy": {"mid": 0.4, "presence": 0.4, "air": 0.2}},
    )
    assert "bright" in text


def test_long_clip_has_no_tail_clause():
    text = cap({"rel_path": "m/Battle Loop.mp3"}, {"duration_s": 60.0, "decay_s": 0.1})
    assert "tail" not in text
    assert text.startswith("60.0 s battle loop")


def test_everything_missing_is_robust():
    assert cap() == "sound"
    assert cap(features={"is_music": 1}) == "music"
    assert cap(tags=[("Gunshot", 0.8), ("Boom", 0.1)]) == "gunshot"
    assert cap({"rel_path": "a/bb.wav"}, {"duration_s": None, "lufs_integrated": None}) == "bb"


def test_noisy_but_never_tonal():
    assert "noisy" in cap({"rel_path": "w.wav"}, {"flatness_mean": 0.5})
    assert "tonal" not in cap({"rel_path": "t.wav"}, {"flatness_mean": 0.001})
