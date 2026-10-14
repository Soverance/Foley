from __future__ import annotations

import os
import shutil
from pathlib import Path

import audiogen as ag
import pytest

from foley.config import Config

# A small library shaped like the real one: packs/<pack>/..., top-level category folders,
# UCS and terse names, an exact duplicate, a license file, an archive and editor sidecars.
LIBRARY_FILES = {
    "packs/Test Bundle 2015/Metal Shelf/Metal Shelf Hit 10.wav": lambda: ag.pad(ag.burst(0.6), 0.02),
    "packs/Test Bundle 2015/Metal Shelf/Metal Shelf Tonal Texture 07.wav": lambda: ag.tone(220, 3.0),
    "packs/Test Bundle 2015/Water 01/Water_Fountain_Fienup_002.flac": lambda: ag.noise(4.0, -24),
    "packs/Test Bundle 2026/Sinister Textures/GOREMisc_Cladding_NailScratch19_InMotionAudio_SinisterTextures5.wav":
        lambda: ag.burst(1.2, 0.05, 0.4, seed=3),
    "packs/Test Bundle 2026/Sinister Textures/GOREMisc_Cladding_NailScratch19_copy.wav": None,  # dup, below
    "packs/Oculus Like Pack/05_user_interface/ui_scifi_traditional_confirm.wav":
        lambda: ag.pad(ag.tone(1760, 0.12, -6), 0.0, 0.1),
    "packs/Oculus Like Pack/03_weather/ambient_wind_through_trees_01_lp.ogg": lambda: ag.noise(6.0, -30, seed=4),
    "sfx/lightsabers/lasrhit3.WAV": lambda: ag.burst(0.3, 0.002, 0.03, seed=5),
    "sfx/CancelMaybe.wav": lambda: ag.pad(ag.tone(660, 0.2, -10), 0.05, 0.05),
    "sfx/click.wav": ag.click,
    "music/Arteria3D/Battle Loop.mp3": lambda: ag.tone(110, 8.0, -14),
    "voice/male-npc/greetings adventurer.wav": lambda: ag.noise(1.5, -18, seed=6),
}

OTHER_FILES = {
    "packs/Test Bundle 2015/LICENSE.txt": b"Test license: free for commercial use.\n",
    "packs/Test Bundle 2026/Readme.txt": b"Bundle readme.\n",
    "packs/Test Bundle 2026/Extras.zip": b"PK\x03\x04 not a real zip",
    "voice/male-npc/goodbye.gpk": b"",
    "voice/male-npc/greetings adventurer.wav.mdd": b"",
    "README.md": b"Fixture library.\n",
}

DUPLICATE_OF = {
    "packs/Test Bundle 2026/Sinister Textures/GOREMisc_Cladding_NailScratch19_copy.wav":
        "packs/Test Bundle 2026/Sinister Textures/GOREMisc_Cladding_NailScratch19_InMotionAudio_SinisterTextures5.wav",
}

OLD_MTIME = 1_700_000_000  # well outside any settle window


@pytest.fixture(scope="session")
def _library_template(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("library_template")
    for rel, make in LIBRARY_FILES.items():
        if make is not None:
            ag.write(root / rel, make())
    for rel, source in DUPLICATE_OF.items():
        shutil.copyfile(root / source, root / rel)
    for rel, data in OTHER_FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    for path in root.rglob("*"):
        if path.is_file():
            os.utime(path, (OLD_MTIME, OLD_MTIME))
    return root


@pytest.fixture
def library(tmp_path, _library_template) -> Path:
    """A fresh, writable copy of the fixture library for each test."""
    root = tmp_path / "library"
    shutil.copytree(_library_template, root, copy_function=shutil.copy2)
    return root


@pytest.fixture
def cfg(tmp_path, library) -> Config:
    return Config(library_root=library, data_dir=tmp_path / "data", settle_minutes=10)
