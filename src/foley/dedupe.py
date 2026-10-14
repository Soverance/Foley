"""Duplicate detection and reporting. Read-only in Phase 1a.

`plan`, `apply`, `restore` and `purge` are Phase 2 and go through foley.nas_write.

CONTRACT:

    @dataclass DuplicateGroup: content_hash, size_bytes, members: list[str] (rel_paths),
                               survivor: str, reclaimable_bytes
    find_exact_groups(cfg, conn) -> list[DuplicateGroup]
        Active assets sharing content_hash, survivor chosen by choose_survivor.
    find_audio_identical_groups(conn) -> list[list[str]]
        Active assets sharing audio_hash but not content_hash (report only).
    choose_survivor(cfg, conn, rel_paths: list[str], content_hash: str) -> str
        Order: has ledger rows (decisions/usages by content_hash... all copies share the
        hash, so prefer the rel_path recorded in ledger rows); then first matching
        cfg.dedupe_keep_priority prefix; then oldest first_seen; then shortest path;
        then lexicographic (deterministic).
    report(cfg, conn) -> dict
        {exact_groups, files_removable, reclaimable_bytes, groups: [...],
         audio_identical: [...]} for the CLI and catalog_status.

Notes (not contract): nothing here touches the library; it only reads catalog.db and
ledger.db. keep_priority entries match as case-insensitive prefixes of the relative
path (backslashes normalized). Groups are ordered by reclaimable bytes, largest first.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from foley.config import Config


@dataclass
class DuplicateGroup:
    content_hash: str
    size_bytes: int
    members: list[str]
    survivor: str

    @property
    def reclaimable_bytes(self) -> int:
        return self.size_bytes * (len(self.members) - 1)


def _has_ledger(conn: sqlite3.Connection) -> bool:
    return any(row[1] == "ledger" for row in conn.execute("PRAGMA database_list"))


def find_exact_groups(cfg: Config, conn: sqlite3.Connection) -> list[DuplicateGroup]:
    rows = conn.execute(
        """
        SELECT content_hash, size_bytes, rel_path FROM assets
        WHERE status = 'active' AND content_hash IN (
            SELECT content_hash FROM assets
            WHERE status = 'active' AND content_hash IS NOT NULL
            GROUP BY content_hash HAVING count(*) > 1)
        ORDER BY content_hash, rel_path
        """
    ).fetchall()
    by_hash: dict[str, tuple[int, list[str]]] = {}
    for content_hash, size, rel in rows:
        by_hash.setdefault(content_hash, (size, []))[1].append(rel)
    groups = [
        DuplicateGroup(h, size, members, choose_survivor(cfg, conn, members, h))
        for h, (size, members) in by_hash.items()
    ]
    groups.sort(key=lambda g: (-g.reclaimable_bytes, g.content_hash))
    return groups


def find_audio_identical_groups(conn: sqlite3.Connection) -> list[list[str]]:
    rows = conn.execute(
        """
        SELECT audio_hash, rel_path FROM assets
        WHERE status = 'active' AND audio_hash IN (
            SELECT audio_hash FROM assets
            WHERE status = 'active' AND audio_hash IS NOT NULL
            GROUP BY audio_hash HAVING count(DISTINCT content_hash) > 1)
        ORDER BY audio_hash, rel_path
        """
    ).fetchall()
    groups: dict[str, list[str]] = {}
    for audio_hash, rel in rows:
        groups.setdefault(audio_hash, []).append(rel)
    return list(groups.values())


def _prefix_matches(prefix: str, rel_path: str) -> bool:
    p = prefix.replace("\\", "/").lstrip("/").casefold()
    return bool(p) and rel_path.casefold().startswith(p)


def choose_survivor(
    cfg: Config, conn: sqlite3.Connection, rel_paths: list[str], content_hash: str
) -> str:
    if not rel_paths:
        raise ValueError("choose_survivor needs at least one path")
    candidates = sorted(set(rel_paths))

    if _has_ledger(conn):
        recorded = {
            r[0] for r in conn.execute(
                "SELECT rel_path FROM ledger.decisions WHERE content_hash = ? "
                "UNION SELECT rel_path FROM ledger.usages WHERE content_hash = ?",
                (content_hash, content_hash),
            )
        }
        with_ledger = [p for p in candidates if p in recorded]
        if with_ledger:
            candidates = with_ledger

    for prefix in cfg.dedupe_keep_priority:
        preferred = [p for p in candidates if _prefix_matches(prefix, p)]
        if preferred:
            candidates = preferred
            break

    if len(candidates) > 1:
        marks = ",".join("?" * len(candidates))
        first_seen = dict(
            conn.execute(
                f"SELECT rel_path, first_seen FROM assets WHERE rel_path IN ({marks})",
                candidates,
            ).fetchall()
        )
        known = [first_seen[p] for p in candidates if first_seen.get(p)]
        if known:
            oldest = min(known)
            candidates = [p for p in candidates if first_seen.get(p) == oldest]

    return min(candidates, key=lambda p: (len(p), p))


def report(cfg: Config, conn: sqlite3.Connection) -> dict:
    groups = find_exact_groups(cfg, conn)
    audio_identical = find_audio_identical_groups(conn)
    return {
        "exact_groups": len(groups),
        "files_removable": sum(len(g.members) - 1 for g in groups),
        "reclaimable_bytes": sum(g.reclaimable_bytes for g in groups),
        "groups": [
            {
                "content_hash": g.content_hash,
                "size_bytes": g.size_bytes,
                "members": g.members,
                "survivor": g.survivor,
                "reclaimable_bytes": g.reclaimable_bytes,
            }
            for g in groups
        ],
        "audio_identical": audio_identical,
    }
