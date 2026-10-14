"""Search and describe. Keyword (FTS5) in Phase 1a; CLAP hybrid joins in Phase 1b.

CONTRACT:

    FTS_VERSION: str

    index_asset(conn, asset_id: int) -> None
        (Re)write the search_fts row (rowid = asset_id) from assets, source_metadata,
        tags and caption. Filename words are split on _ - . space and camelCase, and
        UCS category/subcategory names are added, so "GOREMisc_Cladding_NailScratch19"
        is findable by "gore", "cladding", "nail scratch".

    @dataclass SearchFilters: category, pack, is_music, min/max duration_s,
        min/max lufs, min/max attack_s, include_missing=False

    search(conn, query: str, filters: SearchFilters | None = None,
           project: str | None = None, limit: int = 20) -> dict
        {"note": staleness note or None, "results": [compact result, ...]}
        Compact result: {id, path, score, caption, tags (top 3 labels), duration_s,
                         lufs, attack_s, license, duplicates (count of other copies)}
        Ranking: bm25 with column weights (name and metadata highest). Query words
        are OR-ed with prefix matching so partial matches still rank. Exact-duplicate
        groups (same content_hash) collapse to one result. `project` hides files with
        a `rejected` decision for that project. Only active assets unless
        include_missing.

    describe(conn, asset: int | str) -> dict
        By id or rel_path. Full record: asset fields, parsed metadata, features,
        tags, caption, image path (if cached), duplicate copies, and ledger
        decisions/usages for its content_hash.

DESIGN NOTES:

    Ranking = -bm25(weights name 10, metadata 8, category 4, tags 4, caption 3, pack 2)
    plus DISTINCT_TERM_BONUS per distinct query term the file matches (prefix match
    on any indexed token), so a file matching "nail" and "scratch" beats one matching
    "scratch" twice. Query text is reduced to alphanumeric words and every term is
    emitted as a quoted FTS5 string with a prefix star, so no user input can form FTS5
    syntax. `blend_scores` is the Phase 1b seam for CLAP hybrid scoring.

    `split_words` / `name_tokens` are shared with captions.py.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

FTS_VERSION = "1"

# bm25 column weights, in search_fts column order:
# name, pack, category, metadata, tags, caption
BM25_WEIGHTS = (10.0, 2.0, 4.0, 8.0, 4.0, 3.0)
DISTINCT_TERM_BONUS = 1.5  # added to the score per distinct query term the file matches
MAX_TERMS = 24
_STOPWORDS = frozenset({"a", "an", "the", "of", "and", "or", "not", "to", "for", "with", "in"})
_CANDIDATE_FLOOR = 200


@dataclass
class SearchFilters:
    category: str | None = None
    pack: str | None = None
    is_music: bool | None = None
    min_duration_s: float | None = None
    max_duration_s: float | None = None
    min_lufs: float | None = None
    max_lufs: float | None = None
    min_attack_s: float | None = None
    max_attack_s: float | None = None
    include_missing: bool = False


# --------------------------------------------------------------------------- tokenizing

_RAW_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+|[^\W\d_]+", re.UNICODE)


def split_words(text: str) -> list[str]:
    """Lowercase words of `text`: split on separators, camelCase and letter/digit edges.

    "GOREMisc_Cladding_NailScratch19" -> gore misc cladding nail scratch 19
    """
    words: list[str] = []
    for token in _RAW_TOKEN.findall(text or ""):
        words.extend(w.lower() for w in _CAMEL.findall(token))
    return words


def name_tokens(text: str) -> list[str]:
    """Search tokens for a name: the original (separator-split) tokens plus split words."""
    out: list[str] = []
    seen: set[str] = set()
    for token in _RAW_TOKEN.findall(text or ""):
        pieces = [token.lower()]
        parts = [w.lower() for w in _CAMEL.findall(token)]
        if len(parts) > 1:
            pieces.extend(parts)
        for piece in pieces:
            if piece not in seen:
                seen.add(piece)
                out.append(piece)
    return out


def _flatten(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [t for v in value.values() for t in _flatten(v)]
    if isinstance(value, (list, tuple)):
        return [t for v in value for t in _flatten(v)]
    return []


def _pick(d, *keys) -> list[str]:
    if not isinstance(d, dict):
        return []
    lowered = {str(k).lower(): v for k, v in d.items()}
    out: list[str] = []
    for key in keys:
        out.extend(_flatten(lowered.get(key)))
    return out


def _metadata_text(meta: dict) -> str:
    parts: list[str] = []
    parts += _pick(meta.get("bext"), "description")
    parts += _pick(
        meta.get("ixml"), "note", "keywords", "category", "subcategory", "user_description"
    )
    parts += _pick(meta.get("info"), "title", "comment", "comments", "description", "subject")
    parts += _pick(meta.get("tags"), "title", "comment", "comments", "description")
    parts += _pick(meta.get("ucs"), "fx_name", "category", "subcategory")
    return " ".join(p for p in parts if p).strip()


def _parse_json(text) -> dict:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _caption_text(conn: sqlite3.Connection, asset_id: int) -> str | None:
    # Prefer a model caption over the template when both exist.
    row = conn.execute(
        "SELECT text FROM captions WHERE asset_id = ? ORDER BY (model = 'template-v1'), model LIMIT 1",  # noqa: E501
        (asset_id,),
    ).fetchone()
    return row[0] if row else None


# --------------------------------------------------------------------------- indexing


def index_asset(conn: sqlite3.Connection, asset_id: int) -> None:
    """(Re)write the search_fts row for one asset. Delete-then-insert, rowid = asset id."""
    row = conn.execute(
        "SELECT rel_path, pack, category_path, source_metadata FROM assets WHERE id = ?",
        (asset_id,),
    ).fetchone()
    conn.execute("DELETE FROM search_fts WHERE rowid = ?", (asset_id,))
    if row is None:
        return
    meta = _parse_json(row["source_metadata"])
    ucs = meta.get("ucs") if isinstance(meta.get("ucs"), dict) else {}

    name = " ".join(name_tokens(Path(row["rel_path"]).stem))
    pack = " ".join(name_tokens(row["pack"] or ""))
    category_bits = [
        row["category_path"] or "",
        ucs.get("category") or "",
        ucs.get("subcategory") or "",
    ]
    category = " ".join(" ".join(name_tokens(b)) for b in category_bits if b)
    metadata = _metadata_text(meta)
    tag_rows = conn.execute(
        "SELECT label FROM tags WHERE asset_id = ? ORDER BY score DESC", (asset_id,)
    ).fetchall()
    tags = " ".join(dict.fromkeys(r[0] for r in tag_rows))
    caption = _caption_text(conn, asset_id) or ""

    conn.execute(
        "INSERT INTO search_fts(rowid, name, pack, category, metadata, tags, caption) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (asset_id, name, pack, category, metadata, tags, caption),
    )


# --------------------------------------------------------------------------- search


def query_terms(query: str) -> list[str]:
    """Safe, deduplicated search terms from free text (no FTS5 syntax survives)."""
    terms: list[str] = []
    for word in split_words(query or ""):
        if word in _STOPWORDS or (len(word) == 1 and not word.isdigit()):
            continue
        if word not in terms:
            terms.append(word)
    return terms[:MAX_TERMS]


def _match_expression(terms: list[str]) -> str:
    # Every term is a quoted string with a prefix star, so operators are inert.
    return " OR ".join(f'"{t}"*' for t in terms)


def blend_scores(keyword: float, vector: float | None = None, weight: float = 0.0) -> float:
    """PHASE 1B SEAM: blend a keyword score with an optional CLAP cosine score.

    Phase 1a has no vectors, so this returns the keyword score. Phase 1b passes
    `vector_scores` (asset id -> cosine similarity) into `_rank_rows` and sets the
    blend weight tuned on the test set; both scores should be normalized to 0..1
    there before mixing.
    """
    if vector is None or weight <= 0:
        return keyword
    return (1.0 - weight) * keyword + weight * vector


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _filter_sql(f: SearchFilters, project: str | None) -> tuple[list[str], list]:
    where: list[str] = []
    args: list = []
    if f.include_missing:
        where.append("a.status IN ('active', 'missing')")
    else:
        where.append("a.status = 'active'")
    if f.category:
        like = _like_escape(f.category.strip().lower().strip("/")) + "%"
        where.append(
            "(lower(a.category_path) LIKE ? ESCAPE '\\' "
            "OR lower(a.category_path) LIKE ? ESCAPE '\\' "
            "OR lower(json_extract(a.source_metadata, '$.ucs.category')) LIKE ? ESCAPE '\\' "
            "OR lower(json_extract(a.source_metadata, '$.ucs.subcategory')) LIKE ? ESCAPE '\\')"
        )
        args += [like, "%/" + like, like, like]
    if f.pack:
        where.append("lower(a.pack) LIKE ? ESCAPE '\\'")
        args.append("%" + _like_escape(f.pack.strip().lower()) + "%")
    if f.is_music is not None:
        where.append("f.is_music = ?")
        args.append(1 if f.is_music else 0)
    for column, op, value in (
        ("f.duration_s", ">=", f.min_duration_s),
        ("f.duration_s", "<=", f.max_duration_s),
        ("f.lufs_integrated", ">=", f.min_lufs),
        ("f.lufs_integrated", "<=", f.max_lufs),
        ("f.attack_s", ">=", f.min_attack_s),
        ("f.attack_s", "<=", f.max_attack_s),
    ):
        if value is not None:
            where.append(f"{column} {op} ?")
            args.append(value)
    if project:
        where.append(
            "(a.content_hash IS NULL OR a.content_hash NOT IN "
            "(SELECT content_hash FROM ledger.decisions WHERE project = ? AND verdict = 'rejected'))"  # noqa: E501
        )
        args.append(project)
    return where, args


_SELECT = "SELECT a.id, a.rel_path, a.content_hash, a.license, f.duration_s, f.lufs_integrated, f.attack_s"  # noqa: E501


def _rank_rows(
    rows: list[sqlite3.Row],
    terms: list[str],
    texts: dict[int, list[str]],
    vector_scores: dict[int, float] | None = None,
) -> list[tuple[float, sqlite3.Row]]:
    """Score candidates: -bm25 plus a bonus per distinct query term matched."""
    ranked = []
    for row in rows:
        tokens: set[str] = set()
        for text in texts.get(row["id"], []):
            tokens.update(re.findall(r"\w+", (text or "").lower()))
        matched = sum(1 for t in terms if any(tok.startswith(t) for tok in tokens))
        keyword = -float(row["rank"]) + DISTINCT_TERM_BONUS * matched
        vector = vector_scores.get(row["id"]) if vector_scores else None
        ranked.append((blend_scores(keyword, vector), row))
    ranked.sort(key=lambda sr: (-sr[0], sr[1]["rel_path"]))
    return ranked


def search(
    conn: sqlite3.Connection,
    query: str,
    filters: SearchFilters | None = None,
    project: str | None = None,
    limit: int = 20,
) -> dict:
    filters = filters or SearchFilters()
    limit = max(1, int(limit))
    terms = query_terms(query)
    where, args = _filter_sql(filters, project)
    fetch = max(limit * 10, _CANDIDATE_FLOOR)

    if terms:
        sql = (
            _SELECT + ", bm25(search_fts, ?, ?, ?, ?, ?, ?) AS rank "
            "FROM search_fts JOIN assets a ON a.id = search_fts.rowid "
            "LEFT JOIN features f ON f.asset_id = a.id "
            "WHERE search_fts MATCH ? AND " + " AND ".join(where) + " ORDER BY rank LIMIT ?"
        )
        rows = conn.execute(sql, [*BM25_WEIGHTS, _match_expression(terms), *args, fetch]).fetchall()
        texts: dict[int, list[str]] = {}
        if rows:
            ids = [r["id"] for r in rows]
            marks = ",".join("?" * len(ids))
            for t in conn.execute(
                "SELECT rowid, name, pack, category, metadata, tags, caption FROM search_fts "
                f"WHERE rowid IN ({marks})",
                ids,
            ):
                texts[t[0]] = list(t[1:])
        ranked = _rank_rows(rows, terms, texts)
    else:
        sql = (
            _SELECT + ", 0.0 AS rank FROM assets a "
            "LEFT JOIN features f ON f.asset_id = a.id "
            "WHERE " + " AND ".join(where) + " ORDER BY a.rel_path LIMIT ?"
        )
        rows = conn.execute(sql, [*args, fetch]).fetchall()
        ranked = [(0.0, r) for r in rows]

    # Collapse exact duplicates (same content_hash): the best-ranked copy wins.
    chosen: list[tuple[float, sqlite3.Row]] = []
    seen_hashes: set[str] = set()
    for score, row in ranked:
        h = row["content_hash"]
        if h:
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
        chosen.append((score, row))
        if len(chosen) >= limit:
            break

    results = [_compact(conn, score, row, filters.include_missing) for score, row in chosen]
    return {"note": _staleness_note(conn), "results": results}


def _compact(
    conn: sqlite3.Connection, score: float, row: sqlite3.Row, include_missing: bool
) -> dict:
    aid = row["id"]
    tag_rows = conn.execute(
        "SELECT label FROM tags WHERE asset_id = ? ORDER BY score DESC", (aid,)
    ).fetchall()
    labels = list(dict.fromkeys(r[0] for r in tag_rows))[:3]
    dupes = 0
    if row["content_hash"]:
        statuses = "('active', 'missing')" if include_missing else "('active')"
        dupes = conn.execute(
            f"SELECT COUNT(*) FROM assets WHERE content_hash = ? AND id != ? AND status IN {statuses}",  # noqa: E501
            (row["content_hash"], aid),
        ).fetchone()[0]
    return {
        "id": aid,
        "path": row["rel_path"],
        "score": round(score, 3),
        "caption": _caption_text(conn, aid),
        "tags": labels,
        "duration_s": row["duration_s"],
        "lufs": row["lufs_integrated"],
        "attack_s": row["attack_s"],
        "license": row["license"],
        "duplicates": dupes,
    }


def _staleness_note(conn: sqlite3.Connection) -> str | None:
    from foley import status

    try:
        return status.staleness_note(conn)
    except NotImplementedError:
        return None


# --------------------------------------------------------------------------- describe


def _data_dir(conn: sqlite3.Connection) -> Path | None:
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main" and row[2]:
            return Path(row[2]).parent
    return None


def describe(conn: sqlite3.Connection, asset: int | str) -> dict:
    if isinstance(asset, str) and asset.strip().isdigit():
        asset = int(asset)
    if isinstance(asset, int):
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset,)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM assets WHERE rel_path = ?", (str(asset).replace("\\", "/"),)
        ).fetchone()
    if row is None:
        raise LookupError(f"no such asset: {asset!r}")
    aid = row["id"]
    out = {
        "id": aid,
        "path": row["rel_path"],
        "pack": row["pack"],
        "category_path": row["category_path"],
        "ext": row["ext"],
        "size_bytes": row["size_bytes"],
        "content_hash": row["content_hash"],
        "audio_hash": row["audio_hash"],
        "license": row["license"],
        "status": row["status"],
        "error": row["error"],
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
        "missing_since": row["missing_since"],
        "indexed_at": row["indexed_at"],
        "metadata": _parse_json(row["source_metadata"]),
    }
    feat = conn.execute("SELECT * FROM features WHERE asset_id = ?", (aid,)).fetchone()
    features = None
    if feat is not None:
        features = {k: feat[k] for k in feat.keys() if k != "asset_id"}
        if features.get("band_energy"):
            features["band_energy"] = (
                _parse_json(features["band_energy"]) or features["band_energy"]
            )
        if features.get("is_music") is not None:
            features["is_music"] = bool(features["is_music"])
    out["features"] = features
    out["tags"] = [
        {"label": r["label"], "score": r["score"], "model": r["model"]}
        for r in conn.execute(
            "SELECT label, score, model FROM tags WHERE asset_id = ? ORDER BY score DESC", (aid,)
        )
    ]
    out["captions"] = [
        {"model": r["model"], "text": r["text"]}
        for r in conn.execute("SELECT model, text FROM captions WHERE asset_id = ?", (aid,))
    ]
    out["caption"] = _caption_text(conn, aid)

    img = conn.execute(
        "SELECT kind, local_path, version FROM images WHERE asset_id = ? AND kind = 'overview'",
        (aid,),
    ).fetchone()
    image = None
    if img is not None:
        image = {"kind": img["kind"], "local_path": img["local_path"], "version": img["version"]}
        data_dir = _data_dir(conn)
        if data_dir is not None:
            candidate = data_dir / img["local_path"]
            image["path"] = str(candidate)
            image["exists"] = candidate.exists()
    out["image"] = image

    h = row["content_hash"]
    if h:
        out["duplicates"] = [
            {"id": r["id"], "path": r["rel_path"], "status": r["status"]}
            for r in conn.execute(
                "SELECT id, rel_path, status FROM assets WHERE content_hash = ? AND id != ? "
                "ORDER BY rel_path",
                (h, aid),
            )
        ]
        out["decisions"] = [
            dict(r)
            for r in conn.execute(
                "SELECT project, usage_key, verdict, gain_db, note, decided_by, decided_at "
                "FROM ledger.decisions WHERE content_hash = ? ORDER BY decided_at, id",
                (h,),
            )
        ]
        out["usages"] = [
            dict(r)
            for r in conn.execute(
                "SELECT project, project_path, usage_key, imported_at, normalized_to_lufs, "
                "gain_db, gain_applied, license FROM ledger.usages WHERE content_hash = ? "
                "ORDER BY imported_at, id",
                (h,),
            )
        ]
    else:
        out["duplicates"], out["decisions"], out["usages"] = [], [], []
    return out
