"""Helpers for tests: insert assets/features/tags/captions rows directly."""

from __future__ import annotations

import json

from foley import search
from foley.catalog import db


def add_asset(
    conn,
    rel_path: str,
    *,
    pack=None,
    category_path=None,
    content_hash="auto",
    status="active",
    metadata=None,
    features=None,
    tags=(),
    caption=None,
    license=None,
    ext=None,
    index=True,
) -> int:
    ext = ext or "." + rel_path.rsplit(".", 1)[-1].lower()
    if content_hash == "auto":
        content_hash = f"hash-{rel_path}"
    now = db.utcnow()
    cur = conn.execute(
        "INSERT INTO assets(rel_path, pack, category_path, ext, size_bytes, mtime_ns, content_hash, "  # noqa: E501
        "license, source_metadata, first_seen, last_seen, status) "
        "VALUES (?, ?, ?, ?, 1, 1, ?, ?, ?, ?, ?, ?)",
        (
            rel_path,
            pack,
            category_path,
            ext,
            content_hash,
            license,
            json.dumps(metadata) if metadata is not None else None,
            now,
            now,
            status,
        ),
    )
    aid = cur.lastrowid
    if features is not None:
        feats = dict(features)
        if isinstance(feats.get("band_energy"), dict):
            feats["band_energy"] = json.dumps(feats["band_energy"])
        cols = ["asset_id", *feats]
        conn.execute(
            f"INSERT INTO features({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [aid, *feats.values()],
        )
    for label, score in tags:
        conn.execute(
            "INSERT INTO tags(asset_id, label, score, model) VALUES (?, ?, ?, 'test')",
            (aid, label, score),
        )
    if caption:
        conn.execute(
            "INSERT INTO captions(asset_id, model, text) VALUES (?, 'template-v1', ?)",
            (aid, caption),
        )
    if index:
        search.index_asset(conn, aid)
    return aid


def add_decision(conn, content_hash, project, verdict, usage_key="sfx.x", rel_path="x"):
    conn.execute(
        "INSERT INTO ledger.decisions(content_hash, rel_path, project, usage_key, verdict, "
        "decided_by, decided_at) VALUES (?, ?, ?, ?, ?, 'test', ?)",
        (content_hash, rel_path, project, usage_key, verdict, db.utcnow()),
    )
