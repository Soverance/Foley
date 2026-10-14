"""SQLite access for catalog.db and ledger.db.

Both files are public contracts (the Godot Sound Board reads them from C#), so
every schema change is a numbered migration in catalog/migrations/<db>/ and the
applied version is recorded in that database's meta table.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from foley.config import Config

CATALOG = "catalog"
LEDGER = "ledger"


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)  # autocommit; use transaction()
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


class transaction:
    """`with transaction(conn):` runs the block in BEGIN IMMEDIATE ... COMMIT/ROLLBACK."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")


def _migrations(kind: str) -> list[tuple[int, str]]:
    folder = resources.files("foley.catalog") / "migrations" / kind
    found = []
    for entry in folder.iterdir():
        name = entry.name
        if name.endswith(".sql") and name[:4].isdigit():
            found.append((int(name[:4]), entry.read_text(encoding="utf-8")))
    return sorted(found)


def latest_version(kind: str) -> int:
    migrations = _migrations(kind)
    return migrations[-1][0] if migrations else 0


def schema_version(conn: sqlite3.Connection) -> int:
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
    ).fetchone()
    if not has_meta:
        return 0
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return int(row[0]) if row else 0


def migrate(conn: sqlite3.Connection, kind: str) -> int:
    """Apply pending migrations in order. Returns the resulting schema version."""
    current = schema_version(conn)
    target = latest_version(kind)
    if current > target:
        raise RuntimeError(
            f"{kind} schema version {current} is newer than this Foley supports ({target}); "
            "upgrade Foley"
        )
    for version, sql in _migrations(kind):
        if version <= current:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            # executescript would COMMIT our transaction, so run statements one by one.
            for statement in _split_sql(sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(version),),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        current = version
    return current


def _split_sql(script: str) -> list[str]:
    statements, buffer = [], ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                statements.append(buffer)
            buffer = ""
    if buffer.strip() and not all(
        ln.strip().startswith("--") or not ln.strip() for ln in buffer.splitlines()
    ):
        raise ValueError(f"incomplete SQL statement in migration: {buffer!r}")
    return statements


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def open_catalog(cfg: Config) -> sqlite3.Connection:
    """Open catalog.db (migrated), with ledger.db attached as schema `ledger`."""
    cfg.ensure_dirs()
    open_ledger(cfg).close()  # make sure the ledger exists and is migrated before attaching
    conn = connect(cfg.catalog_path)
    migrate(conn, CATALOG)
    if get_meta(conn, "library_root") is None:
        set_meta(conn, "library_root", str(cfg.library_root))
    conn.execute("ATTACH DATABASE ? AS ledger", (str(cfg.ledger_path),))
    return conn


def open_ledger(cfg: Config) -> sqlite3.Connection:
    cfg.ensure_dirs()
    conn = connect(cfg.ledger_path)
    migrate(conn, LEDGER)
    return conn
