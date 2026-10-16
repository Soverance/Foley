"""Loading foley.toml.

Every studio-specific value (library root, quarantine, pack priorities) comes from
config, never from code, so the repo stays publishable.

Helpers shared with callers: parse_hhmm(value) -> "HH:MM" (ValueError if not a 24-hour
time) and normalize_host(name) -> the lowercase host without port, brackets or trailing dot.
"""

from __future__ import annotations

import os
import re
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
    browse_host: str = "127.0.0.1"  # [browse] host
    browse_port: int = 8765  # [browse] port
    browse_allowed_hosts: list[str] = field(default_factory=list)  # [browse] allowed_hosts
    schedule_enabled: bool = False  # [schedule] enabled
    schedule_time: str = "03:00"  # [schedule] time: "HH:MM", local time on the server
    schedule_catch_up: bool = True  # [schedule] catch_up
    schedule_startup_delay_minutes: float = 5.0  # [schedule] startup_delay_minutes
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


_HHMM = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")


def parse_hhmm(value) -> str:
    """A 24-hour "H:MM" / "HH:MM" time of day, normalized to "HH:MM"; ValueError if bad."""
    m = _HHMM.fullmatch(value.strip()) if isinstance(value, str) else None
    if not m:
        raise ValueError(f'expected a 24-hour time like "03:00", got {value!r}')
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def normalize_host(value: str) -> str:
    """A host name or IP as compared with a Host header: lowercase, without port,
    brackets or trailing dot."""
    text = str(value).strip().lower()
    if text.startswith("["):
        text = text[1 : text.find("]")] if "]" in text else text[1:]
    elif text.count(":") == 1:
        text = text.rsplit(":", 1)[0]
    return text.rstrip(".")


def _bool(value, key: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be true or false, got {value!r}")
    return value


def from_dict(data: dict, source: Path | None = None) -> Config:
    library = data.get("library", {})
    storage = data.get("storage", {})
    prune = data.get("prune", {})
    dedupe = data.get("dedupe", {})
    evaluation = data.get("eval", {})
    browse = data.get("browse", {})
    schedule = data.get("schedule", {})
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
    cfg.browse_host = str(browse.get("host", cfg.browse_host)).strip() or cfg.browse_host
    port = browse.get("port", cfg.browse_port)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ConfigError(f"browse.port must be a port number (0-65535), got {port!r}")
    cfg.browse_port = port
    hosts = browse.get("allowed_hosts", [])
    if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
        raise ConfigError("browse.allowed_hosts must be a list of host names")
    cfg.browse_allowed_hosts = [n for n in (normalize_host(h) for h in hosts) if n]
    cfg.schedule_enabled = _bool(schedule.get("enabled", cfg.schedule_enabled),
                                 "schedule.enabled")
    try:
        cfg.schedule_time = parse_hhmm(schedule.get("time", cfg.schedule_time))
    except ValueError as e:
        raise ConfigError(f"schedule.time: {e}") from None
    cfg.schedule_catch_up = _bool(schedule.get("catch_up", cfg.schedule_catch_up),
                                  "schedule.catch_up")
    delay = schedule.get("startup_delay_minutes", cfg.schedule_startup_delay_minutes)
    if isinstance(delay, bool) or not isinstance(delay, (int, float)) or delay < 0:
        raise ConfigError(f"schedule.startup_delay_minutes must be >= 0, got {delay!r}")
    cfg.schedule_startup_delay_minutes = float(delay)
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
