"""`foley status` / MCP `catalog_status`.

CONTRACT:

    catalog_status(cfg, conn) -> dict   (compact, JSON-serializable)
        {
          library_root, schema_versions: {catalog, ledger},
          assets: {active, pending, missing, error, total},
          last_scan: {run_id, started_at, finished_at, status} | None,
          stale_hours: float | None,          # since last completed scan
          pending_files, settling_files,      # from the last run
          archives_not_extracted: [rel_path, ...] (first 20) + count,
          duplicates: {exact_groups, reclaimable_bytes, audio_identical_groups},
          used_but_missing: [{rel_path, project}],   # ledger usages whose asset is missing
          models: {
            installed: bool,                       # the optional `models` extra (torch,
            note: str | None,                      # transformers); note says how to
                                                   # install it when it is missing
            tags: {model, done, pending},          # active assets tagged by that model
            embeddings: {<model key>: {name, done, pending}},   # [models] clap, plus any
                                                   # other model with embeddings rows
            provenance: {...} from meta `models` (checkpoints, licences),
          },
          errors: [{rel_path, error}] (first 20) + count,
        }

    staleness_note(conn) -> str | None
        One line for search headers when the last completed scan is older than
        24 h or files are pending, e.g. "catalog last scanned 3 days ago; 41 files pending".

Shapes chosen for "(first 20) + count": `archives_not_extracted` is a list of up to 20
rel_paths with the total in `archives_not_extracted_count`; `errors` is a list of up to
20 {rel_path, error} with the total in `error_count`. `pending_files` is the live count
of pending assets; `settling_files` comes from the last run. An archive counts as
extracted when a folder named after it (same parent, archive stem) holds indexed assets.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from foley import dedupe
from foley.catalog import db
from foley.config import Config, clap_checkpoints

_LIST_LIMIT = 20


def _hours_since(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return max((datetime.now(UTC) - then).total_seconds() / 3600.0, 0.0)


def _last_completed(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT id, started_at, finished_at FROM index_runs WHERE status = 'completed' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()


def _has_ledger(conn: sqlite3.Connection) -> bool:
    return any(row[1] == "ledger" for row in conn.execute("PRAGMA database_list"))


def _archives_not_extracted(conn: sqlite3.Connection) -> list[str]:
    out = []
    for (rel,) in conn.execute("SELECT rel_path FROM archives ORDER BY rel_path"):
        folder = rel.rsplit(".", 1)[0] + "/"
        # range scan on the rel_path index: everything that starts with `folder`
        hit = conn.execute(
            "SELECT 1 FROM assets WHERE rel_path >= ? AND rel_path < ? LIMIT 1",
            (folder, folder[:-1] + "0"),
        ).fetchone()
        if not hit:
            out.append(rel)
    return out


def _model_progress(cfg: Config, conn: sqlite3.Connection, active: int) -> dict:
    """How far the model stages (enrichment, not gating `active`) have got."""
    from foley import models, sync
    from foley.models import clap, panns

    versions = sync.stage_versions(cfg)
    missing = models.factory().unavailable()
    tagged = conn.execute(
        "SELECT count(*) FROM assets a JOIN asset_stages s ON s.asset_id = a.id "
        "AND s.stage = 'tags' AND s.version = ? WHERE a.status = 'active'",
        (versions["tags"],),
    ).fetchone()[0]
    have = dict(conn.execute(
        "SELECT e.model, count(*) FROM embeddings e JOIN assets a ON a.id = e.asset_id "
        "WHERE a.status = 'active' GROUP BY e.model"
    ).fetchall())
    keys = [clap.model_key(n) for n in clap_checkpoints(cfg)]
    keys += sorted(k for k in have if k not in keys)
    return {
        "installed": missing is None,
        "note": f"model stages are skipped: {missing}" if missing else None,
        "tags": {"model": panns.MODEL_KEY, "done": tagged, "pending": max(active - tagged, 0)},
        "embeddings": {
            k: {"name": clap.short_name_for_key(k), "done": have.get(k, 0),
                "pending": max(active - have.get(k, 0), 0)}
            for k in keys
        },
    }


def catalog_status(cfg: Config, conn: sqlite3.Connection) -> dict:
    counts = {s: 0 for s in ("active", "pending", "missing", "error")}
    for status, n in conn.execute("SELECT status, count(*) FROM assets GROUP BY status"):
        counts[status] = n
    counts["total"] = sum(counts.values())

    ledger = _has_ledger(conn)
    ledger_version = None
    if ledger:
        row = conn.execute(
            "SELECT value FROM ledger.meta WHERE key = 'schema_version'"
        ).fetchone()
        ledger_version = int(row[0]) if row else 0

    last = conn.execute(
        "SELECT id, started_at, finished_at, status, settling FROM index_runs "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    completed = _last_completed(conn)

    archives = _archives_not_extracted(conn)
    dup = dedupe.report(cfg, conn)

    used_but_missing: list[dict] = []
    if ledger:
        used_but_missing = [
            {"rel_path": r[0], "project": r[1]}
            for r in conn.execute(
                """
                SELECT DISTINCT u.rel_path, u.project FROM ledger.usages u
                WHERE NOT EXISTS (SELECT 1 FROM assets a WHERE a.content_hash = u.content_hash
                                  AND a.status != 'missing')
                ORDER BY u.project, u.rel_path
                """
            )
        ]

    models_raw = db.get_meta(conn, "models")
    try:
        provenance = json.loads(models_raw) if models_raw else {}
    except ValueError:
        provenance = {"raw": models_raw}
    models = _model_progress(cfg, conn, counts["active"])
    models["provenance"] = provenance

    errors = [
        {"rel_path": r[0], "error": r[1]}
        for r in conn.execute(
            "SELECT rel_path, error FROM assets WHERE status = 'error' ORDER BY rel_path LIMIT ?",
            (_LIST_LIMIT,),
        )
    ]

    return {
        "library_root": db.get_meta(conn, "library_root") or str(cfg.library_root),
        "schema_versions": {"catalog": db.schema_version(conn), "ledger": ledger_version},
        "assets": counts,
        "last_scan": (
            {"run_id": last["id"], "started_at": last["started_at"],
             "finished_at": last["finished_at"], "status": last["status"]}
            if last else None
        ),
        "stale_hours": (
            round(h, 2) if (h := _hours_since(completed["finished_at"] if completed else None))
            is not None else None
        ),
        "pending_files": counts["pending"],
        "settling_files": last["settling"] if last else 0,
        "archives_not_extracted": archives[:_LIST_LIMIT],
        "archives_not_extracted_count": len(archives),
        "duplicates": {
            "exact_groups": dup["exact_groups"],
            "reclaimable_bytes": dup["reclaimable_bytes"],
            "audio_identical_groups": len(dup["audio_identical"]),
        },
        "used_but_missing": used_but_missing,
        "models": models,
        "errors": errors,
        "error_count": counts["error"],
    }


def _ago(hours: float) -> str:
    if hours < 1:
        return "less than an hour ago"
    if hours < 48:
        n = int(hours)
        return f"{n} hour{'s' if n != 1 else ''} ago"
    return f"{int(hours // 24)} days ago"


def staleness_note(conn: sqlite3.Connection) -> str | None:
    completed = _last_completed(conn)
    pending = conn.execute("SELECT count(*) FROM assets WHERE status = 'pending'").fetchone()[0]
    parts = []
    if completed is None:
        parts.append("catalog has no completed scan yet (run `foley index`)")
    else:
        hours = _hours_since(completed["finished_at"])
        if hours is not None and hours > 24:
            parts.append(f"catalog last scanned {_ago(hours)}")
    if pending:
        parts.append(f"{pending} file{'s' if pending != 1 else ''} pending")
    return "; ".join(parts) or None
