"""Loading foley.toml.

Every studio-specific value (library root, quarantine, pack priorities) comes from
config, never from code, so the repo stays publishable.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

AUDIO_EXTENSIONS = frozenset({".wav", ".flac", ".ogg", ".mp3", ".aif", ".aiff"})
ARCHIVE_EXTENSIONS = frozenset({".zip", ".rar", ".7z"})


class ConfigError(Exception):
    pass


def default_data_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "Foley"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "foley"


@dataclass
class Config:
    library_root: Path
    data_dir: Path = field(default_factory=default_data_dir)
    pack_roots: list[str] = field(default_factory=lambda: ["packs"])
    settle_minutes: float = 10.0
    prune_missing_days: float = 30.0
    quarantine_root: Path | None = None
    dedupe_retention_days: float = 30.0
    dedupe_keep_priority: list[str] = field(default_factory=list)
    testset: Path | None = None
    browse_loudness_target_lufs: float = -20.0  # [browse] loudness_target_lufs
    source: Path | None = None  # the file this config was loaded from

    @property
    def catalog_path(self) -> Path:
        return self.data_dir / "catalog.db"

    @property
    def ledger_path(self) -> Path:
        return self.data_dir / "ledger.db"

    @property
    def image_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "index.lock"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.image_dir.mkdir(parents=True, exist_ok=True)

    def abs_path(self, rel_path: str) -> Path:
        """Library-relative path (forward slashes) to an absolute path."""
        return self.library_root.joinpath(*rel_path.split("/"))

    def rel_path(self, path: Path) -> str:
        """Absolute path under the library root to its catalog form (forward slashes)."""
        return path.relative_to(self.library_root).as_posix()


def _path(value: str | None) -> Path | None:
    return Path(value) if value else None


def from_dict(data: dict, source: Path | None = None) -> Config:
    library = data.get("library", {})
    storage = data.get("storage", {})
    prune = data.get("prune", {})
    dedupe = data.get("dedupe", {})
    evaluation = data.get("eval", {})
    browse = data.get("browse", {})
    root = library.get("root")
    if not root:
        raise ConfigError("library.root is not set")
    cfg = Config(library_root=Path(root))
    if storage.get("data_dir"):
        cfg.data_dir = Path(storage["data_dir"])
    cfg.pack_roots = list(library.get("pack_roots", cfg.pack_roots))
    cfg.settle_minutes = float(library.get("settle_minutes", cfg.settle_minutes))
    cfg.prune_missing_days = float(prune.get("missing_days", cfg.prune_missing_days))
    cfg.quarantine_root = _path(dedupe.get("quarantine_root"))
    cfg.dedupe_retention_days = float(dedupe.get("retention_days", cfg.dedupe_retention_days))
    cfg.dedupe_keep_priority = list(dedupe.get("keep_priority", []))
    cfg.testset = _path(evaluation.get("testset"))
    cfg.browse_loudness_target_lufs = float(
        browse.get("loudness_target_lufs", cfg.browse_loudness_target_lufs)
    )
    cfg.source = source
    check_data_dir(cfg)
    return cfg


def _inside(child: Path, parent: Path) -> bool:
    c = os.path.normcase(os.path.realpath(child))
    p = os.path.normcase(os.path.realpath(parent)).rstrip("\\/")
    return c == p or c.startswith(p + os.sep)


def check_data_dir(cfg: Config) -> None:
    """Refuse a data_dir (catalog, ledger, image cache) inside the library root: Foley
    never writes to the library."""
    if _inside(cfg.data_dir, cfg.library_root):
        raise ConfigError(
            f"storage.data_dir ({cfg.data_dir}) is inside library.root ({cfg.library_root}); "
            "put the catalog on local disk outside the library"
        )


def find_config_file(explicit: str | Path | None = None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    elif os.environ.get("FOLEY_CONFIG"):
        candidates.append(Path(os.environ["FOLEY_CONFIG"]))
    else:
        candidates += [Path.cwd() / "foley.toml", default_data_dir() / "foley.toml"]
    for path in candidates:
        if path.is_file():
            return path
    tried = ", ".join(str(p) for p in candidates)
    raise ConfigError(f"no foley.toml found (tried {tried}); copy foley.toml.example")


def load(explicit: str | Path | None = None) -> Config:
    path = find_config_file(explicit)
    with path.open("rb") as f:
        return from_dict(tomllib.load(f), source=path)
