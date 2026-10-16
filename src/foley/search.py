"""Search, find-similar and describe: FTS5 keyword ranking blended with CLAP vectors.

CONTRACT:

    FTS_VERSION: str

    index_asset(conn, asset_id: int) -> None
        (Re)write the search_fts row (rowid = asset_id) from assets, source_metadata,
        tags and caption. Filename words are split on _ - . space and camelCase, and
        UCS category/subcategory names are added, so "GOREMisc_Cladding_NailScratch19"
        is findable by "gore", "cladding", "nail scratch".

    @dataclass SearchFilters: category, pack, folder, is_music, min/max duration_s,
        min/max lufs, min/max attack_s, include_missing=False
        `folder` is a library-relative folder: only files under it (recursively) match.
        It is applied as an escaped SQL LIKE prefix plus an exact rel_path range, so
        `%` and `_` in folder names are literal and the match is case-sensitive.

    @dataclass SearchSettings(clap_model=None, blend_weight=None, models_clap=(),
                              semantic=True)
        SearchSettings.from_config(cfg): [search] clap_model / blend_weight and
        [models] clap. Every function below takes `settings=` (keyword-only, default
        SearchSettings()); semantic=False forces keyword-only.
    resolve(conn, settings=None) -> Resolved(clap_model, model_key, blend_weight,
                                             weight_source, model_source)
    resolve_weight(conn, settings=None) -> (blend_weight, source)   no foley.models import
        Active CLAP checkpoint: [search] clap_model, else meta `search_clap_model`
        (written by `foley eval --adopt`), else the first of [models] clap, else
        foley.models.clap.DEFAULT_CLAP. Blend weight: [search] blend_weight, else meta
        `search_blend_weight`, else DEFAULT_BLEND_WEIGHT; clamped to 0..1.

    search(conn, query: str, filters: SearchFilters | None = None,
           project: str | None = None, limit: int = 20, *, settings=None) -> dict
        {"note": str | None, "mode": "hybrid" | "keyword" | "filter",
         "model": CLAP short name (hybrid only), "results": [compact result, ...]}
        Compact result: {id, path, score, similarity, caption, tags (top 3 labels),
                         duration_s, lufs, attack_s, license, duplicates (count of other
                         copies)}
        Hybrid (see RANKING) when the active model has embeddings and the blend weight
        is > 0: `score` is the blended 0..1 score and `similarity` the raw CLAP cosine
        of the query text and the file (None for a file without an embedding).
        Otherwise keyword-only: `score` is the raw keyword score (as in Phase 1a),
        `similarity` None, and `note` says why search is keyword-only (no embeddings
        for the model, models not installed, text encoder failed to load) unless the
        weight is 0 by choice. `note` also carries catalog staleness and partial
        embedding coverage. An empty query (no search terms) lists files matching the
        filters by path (mode "filter"). Exact-duplicate groups (same content_hash)
        collapse to their best-ranked copy. `project` hides files with a `rejected`
        decision for that project. Only active assets unless include_missing.

    rank(conn, query, filters=None, project=None, limit=200, *, settings=None,
         restrict=None) -> list[tuple[int, float]]
        The ranking behind `search`, without collapsing or compacting: (asset id,
        score) for up to `limit` candidates, best first, exact duplicates kept. []
        when the query has no search terms (see query_terms). `restrict` (asset ids)
        limits candidates further. Used by `foley browse` to page through every match.
    ranking(...same arguments...) -> Ranking(mode, model, weight, notes, hits: [Hit])
        rank() with detail: Hit(score, row, similarity) and the notes search() reports.
    candidates(conn, query, filters=None, project=None, fetch=200, settings=None,
               restrict=None, semantic=None) -> Candidates
        The scored, unblended union behind rank/search; Candidates.order(weight=None)
        -> [Hit] blends it at any weight (None: the resolved one; <= 0: keyword-only
        ranking with raw scores). semantic=True computes vectors even when the
        resolved weight is 0 (foley eval sweeps weights over one Candidates).
    collapse(hits, limit=None) -> [Hit]   best-ranked copy per content_hash

    find_similar(conn, asset_id: int, filters: SearchFilters | None = None,
                 limit: int = 20, project: str | None = None, *, settings=None) -> dict
        {"note", "model", "source": {id, path}, "results": [compact result]}: nearest
        neighbours of the asset by CLAP audio-embedding cosine (active model), best
        first; `score` = `similarity` = cosine. The asset itself and its exact copies
        are excluded and duplicate groups collapse. No embedding for the asset (or no
        models) -> empty results and a `note` saying why. LookupError for an unknown id.
    similar_ranking(conn, asset_id, filters=None, project=None, limit=200, *,
                    settings=None, restrict=None) -> Similar(source, model, notes, hits)
        find_similar's ranking: hits = [(asset id, cosine)], duplicates kept.

    describe(conn, asset: int | str) -> dict
        By id or rel_path. Full record: asset fields, parsed metadata, features,
        tags, caption, image path (if cached), duplicate copies, and ledger
        decisions/usages for its content_hash.

RANKING:

    Keyword: -bm25(weights name 10, metadata 8, category 4, tags 4, caption 3, pack 2)
    plus DISTINCT_TERM_BONUS per distinct query term the file matches (prefix match
    on any indexed token), so a file matching "nail" and "scratch" beats one matching
    "scratch" twice. Query text is reduced to alphanumeric words and every term is
    emitted as a quoted FTS5 string with a prefix star, so no user input can form FTS5
    syntax.

    Hybrid: the query text (as typed) is embedded by the CLAP text encoder
    (foley.vectors, CPU, cached) and compared with every embedding of the active model.
    Candidates are the union of the top `fetch` keyword matches and the top
    max(fetch, VECTOR_CANDIDATES_MIN) vector matches, both restricted to the assets
    passing the filters. Scores are normalized to 0..1 per query before blending:
    keyword / the best keyword score among the candidates; vector = (cos - median) /
    (best - median) over every filter-passing asset with an embedding, clipped to 0..1,
    so the closest sound is 1 and a typical one 0. score = blend_scores(keyword, vector,
    weight). A file without an embedding blends its keyword score with itself (keeps
    its keyword rank). A semantic-only candidate (no keyword match) needs a normalized
    vector score >= SEMANTIC_MIN to be listed, so a query does not drag in half the
    library at a zero keyword score.

    `split_words` / `name_tokens` are shared with captions.py.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from foley import vectors

FTS_VERSION = "1"

# bm25 column weights, in search_fts column order:
# name, pack, category, metadata, tags, caption
BM25_WEIGHTS = (10.0, 2.0, 4.0, 8.0, 4.0, 3.0)
DISTINCT_TERM_BONUS = 1.5  # added to the score per distinct query term the file matches
MAX_TERMS = 24
_STOPWORDS = frozenset({"a", "an", "the", "of", "and", "or", "not", "to", "for", "with", "in"})
_CANDIDATE_FLOOR = 200

DEFAULT_BLEND_WEIGHT = 0.5  # until `foley eval --adopt` records a tuned one
VECTOR_CANDIDATES_MIN = 200  # vector top-N joined with the keyword candidates
SEMANTIC_MIN = 0.4  # normalized vector score a semantic-only candidate needs
META_CLAP_MODEL = "search_clap_model"
META_BLEND_WEIGHT = "search_blend_weight"


@dataclass
class SearchFilters:
    category: str | None = None
    pack: str | None = None
    folder: str | None = None
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
    """Blend a normalized keyword score with a normalized CLAP score (both 0..1).

    `vector` None (no embedding for this asset) or `weight` <= 0 returns the keyword
    score, so assets without an embedding keep their keyword rank (in effect their
    vector score is imputed as equal to their keyword score).
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
    folder = (f.folder or "").replace("\\", "/").strip("/")
    if folder:
        # Exact, index-friendly range ('0' sorts right after '/') plus the escaped LIKE.
        where.append("a.rel_path >= ? AND a.rel_path < ? AND a.rel_path LIKE ? ESCAPE '\\'")
        args += [folder + "/", folder + "0", _like_escape(folder) + "/%"]
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
) -> list[tuple[float, sqlite3.Row]]:
    """Keyword score per candidate: -bm25 plus a bonus per distinct query term matched."""
    ranked = []
    for row in rows:
        tokens: set[str] = set()
        for text in texts.get(row["id"], []):
            tokens.update(re.findall(r"\w+", (text or "").lower()))
        matched = sum(1 for t in terms if any(tok.startswith(t) for tok in tokens))
        ranked.append((-float(row["rank"]) + DISTINCT_TERM_BONUS * matched, row))
    ranked.sort(key=lambda sr: (-sr[0], sr[1]["rel_path"]))
    return ranked


def _keyword_candidates(
    conn: sqlite3.Connection,
    terms: list[str],
    filters: SearchFilters,
    project: str | None,
    fetch: int,
) -> list[tuple[float, sqlite3.Row]]:
    where, args = _filter_sql(filters, project)
    sql = (
        _SELECT + ", bm25(search_fts, ?, ?, ?, ?, ?, ?) AS rank "
        "FROM search_fts JOIN assets a ON a.id = search_fts.rowid "
        "LEFT JOIN features f ON f.asset_id = a.id "
        "WHERE search_fts MATCH ? AND " + " AND ".join(where) + " ORDER BY rank LIMIT ?"
    )
    rows = conn.execute(sql, [*BM25_WEIGHTS, _match_expression(terms), *args, fetch]).fetchall()
    texts: dict[int, list[str]] = {}
    if rows:
        # json_each keeps this one bound parameter however many candidates there are.
        for t in conn.execute(
            "SELECT rowid, name, pack, category, metadata, tags, caption FROM search_fts "
            "WHERE rowid IN (SELECT value FROM json_each(?))",
            (json.dumps([r["id"] for r in rows]),),
        ):
            texts[t[0]] = list(t[1:])
    return _rank_rows(rows, terms, texts)


def _allowed_ids(conn: sqlite3.Connection, filters: SearchFilters,
                 project: str | None) -> np.ndarray:
    """Ids of every asset passing the filters (vector candidates are restricted to them)."""
    where, args = _filter_sql(filters, project)
    needs_features = any(w.startswith("f.") for w in where)
    sql = ("SELECT a.id FROM assets a "
           + ("LEFT JOIN features f ON f.asset_id = a.id " if needs_features else "")
           + "WHERE " + " AND ".join(where))
    return np.fromiter((r[0] for r in conn.execute(sql, args)), dtype=np.int64)


def _rows_by_id(conn: sqlite3.Connection, ids: list[int]) -> dict[int, sqlite3.Row]:
    if not ids:
        return {}
    return {
        r["id"]: r
        for r in conn.execute(
            _SELECT + ", 0.0 AS rank FROM assets a LEFT JOIN features f ON f.asset_id = a.id "
            "WHERE a.id IN (SELECT value FROM json_each(?))",
            (json.dumps(ids),),
        )
    }


# --------------------------------------------------------------------------- hybrid


@dataclass
class SearchSettings:
    """Which CLAP model and blend weight search uses; None fields resolve (see resolve)."""

    clap_model: str | None = None  # [search] clap_model
    blend_weight: float | None = None  # [search] blend_weight
    models_clap: tuple[str, ...] = ()  # [models] clap: checkpoints the indexer computes
    semantic: bool = True  # False: keyword only, whatever the weight

    @classmethod
    def from_config(cls, cfg) -> SearchSettings:
        if cfg is None:
            return cls()
        models = getattr(cfg, "models_clap", None) or getattr(cfg, "clap_models", None) or ()
        if isinstance(models, str):
            models = (models,)
        return cls(
            clap_model=getattr(cfg, "search_clap_model", None),
            blend_weight=getattr(cfg, "search_blend_weight", None),
            models_clap=tuple(models),
        )


@dataclass
class Resolved:
    clap_model: str | None  # short checkpoint name
    model_key: str | None  # embeddings.model value, None when foley.models is unavailable
    blend_weight: float
    weight_source: str  # "config" | "meta" | "default"
    model_source: str  # "config" | "meta" | "models" | "default" | "none"


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def resolve(conn: sqlite3.Connection, settings: SearchSettings | None = None) -> Resolved:
    s = settings or SearchSettings()
    name, source = s.clap_model, "config"
    if not name:
        name, source = _meta(conn, META_CLAP_MODEL), "meta"
    if not name and s.models_clap:
        name, source = s.models_clap[0], "models"
    if not name:
        name, source = vectors.default_clap(), "default"
    if not name:
        source = "none"
    weight, wsource = resolve_weight(conn, s)
    return Resolved(name, vectors.model_key(name) if name else None, weight, wsource, source)


def resolve_weight(conn: sqlite3.Connection,
                   settings: SearchSettings | None = None) -> tuple[float, str]:
    """(blend weight, source) without touching foley.models (cheap; see resolve)."""
    weight, source = (settings or SearchSettings()).blend_weight, "config"
    if weight is None:
        source = "meta"
        try:
            weight = float(_meta(conn, META_BLEND_WEIGHT))
        except (TypeError, ValueError):
            weight = None
    if weight is None or not math.isfinite(weight):
        weight, source = DEFAULT_BLEND_WEIGHT, "default"
    return min(1.0, max(0.0, float(weight))), source


@dataclass
class Hit:
    score: float
    row: sqlite3.Row
    similarity: float | None  # raw CLAP cosine, None without an embedding / in keyword mode

    @property
    def id(self) -> int:
        return self.row["id"]


@dataclass
class _Cand:
    row: sqlite3.Row
    keyword: float | None = None  # raw keyword score, None for semantic-only candidates
    kw_norm: float = 0.0
    cosine: float | None = None
    vec_norm: float | None = None


@dataclass
class Candidates:
    """Keyword and vector candidates of one query, scored but not yet blended."""

    mode: str  # "hybrid" | "keyword"
    resolved: Resolved | None
    notes: list[str]
    cands: list[_Cand]

    def order(self, weight: float | None = None) -> list[Hit]:
        """Best first. weight None: the resolved weight; <= 0 (or keyword mode): keyword
        ranking with raw keyword scores, exactly as Phase 1a."""
        if weight is None:
            weight = self.resolved.blend_weight if self.resolved else 0.0
        hits: list[Hit] = []
        if self.mode != "hybrid" or weight <= 0:
            for c in self.cands:
                if c.keyword is not None:
                    hits.append(Hit(c.keyword, c.row, c.cosine))
        else:
            for c in self.cands:
                if c.keyword is None:
                    if c.vec_norm is None or c.vec_norm < SEMANTIC_MIN:
                        continue
                    score = blend_scores(0.0, c.vec_norm, weight)
                else:
                    score = blend_scores(c.kw_norm, c.vec_norm, weight)
                hits.append(Hit(score, c.row, c.cosine))
        hits.sort(key=lambda hit: (-hit.score, hit.row["rel_path"]))
        return hits


def _vector_reference(sims: np.ndarray) -> tuple[float, float]:
    """(floor, top) for normalizing cosines: the median and the best of the allowed set."""
    top = float(sims.max())
    floor = float(np.partition(sims, len(sims) // 2)[len(sims) // 2])
    return floor, top


def _vec_norm(cos: float, floor: float, top: float) -> float:
    span = top - floor
    if span <= 1e-6:
        return 0.0  # no spread (all equally close, or one candidate): no vector signal
    return min(1.0, max(0.0, (cos - floor) / span))


def candidates(
    conn: sqlite3.Connection,
    query: str,
    filters: SearchFilters | None = None,
    project: str | None = None,
    fetch: int = _CANDIDATE_FLOOR,
    settings: SearchSettings | None = None,
    restrict: Iterable[int] | None = None,
    semantic: bool | None = None,
) -> Candidates:
    filters = filters or SearchFilters()
    settings = settings or SearchSettings()
    terms = query_terms(query)
    restrict_set = None if restrict is None else {int(i) for i in restrict}
    kw = _keyword_candidates(conn, terms, filters, project, fetch) if terms else []
    by_id: dict[int, _Cand] = {}
    kw_max = max((s for s, _ in kw), default=0.0)
    for score, row in kw:
        if restrict_set is not None and row["id"] not in restrict_set:
            continue
        by_id[row["id"]] = _Cand(row, score, max(score, 0.0) / kw_max if kw_max > 0 else 0.0)

    notes: list[str] = []
    mode, resolved = "keyword", None
    use = settings.semantic if semantic is None else semantic
    text = (query or "").strip()
    # Checked in this order so a catalog without embeddings never imports foley.models.
    if use and terms and text and (resolve_weight(conn, settings)[0] > 0 or semantic is True):
        models = vectors.embedding_models(conn)
        if not models:
            notes.append("keyword-only search: the catalog has no CLAP embeddings yet "
                         "(`foley index` computes them)")
        else:
            resolved = resolve(conn, settings)
            mode = _add_vectors(conn, text, filters, project, fetch, resolved, models,
                                restrict_set, by_id, notes)
    ordered = sorted(by_id.values(), key=lambda c: c.row["rel_path"])
    return Candidates(mode, resolved, notes, ordered)


def _add_vectors(conn, text, filters, project, fetch, resolved, models, restrict_set, by_id,
                 notes) -> str:
    name = resolved.clap_model
    if resolved.model_key is None:
        why = f"CLAP model {name!r} is unknown" if name and vectors.clap_api() else \
            "the CLAP models are not installed"
        notes.append(f"keyword-only search: {why}")
        return "keyword"
    if resolved.model_key not in models:
        notes.append(f"keyword-only search: no CLAP embeddings for {name} yet "
                     "(`foley index` computes them)")
        return "keyword"
    index = vectors.get_index(conn, resolved.model_key)
    if index is None or not len(index):
        notes.append(f"keyword-only search: no usable CLAP embeddings for {name}")
        return "keyword"
    try:
        q = vectors.embed_text(name, text)
    except vectors.EncoderUnavailable as e:
        notes.append(f"keyword-only search: {e}")
        return "keyword"
    allowed = _allowed_ids(conn, filters, project)
    if restrict_set is not None:
        allowed = allowed[np.isin(allowed, np.fromiter(restrict_set, dtype=np.int64))]
    mask = index.mask(allowed)
    covered = int(mask.sum())
    if covered < len(allowed):
        notes.append(f"CLAP embeddings cover {covered:,} of {len(allowed):,} matching sounds "
                     "so far; the others rank by keyword only")
    if not covered:
        return "keyword"
    sims = index.similarities(q)
    floor, top = _vector_reference(sims[mask])
    for c in by_id.values():
        r = index.row(c.row["id"])
        if r is not None and mask[r]:
            c.cosine = float(sims[r])
            c.vec_norm = _vec_norm(c.cosine, floor, top)
    extra = []
    for aid, cos in index.topk(q, max(fetch, VECTOR_CANDIDATES_MIN), restrict=mask, sims=sims):
        if aid in by_id:
            continue
        vn = _vec_norm(cos, floor, top)
        if vn < SEMANTIC_MIN:
            break  # best first: the rest are weaker still
        extra.append((aid, cos, vn))
    rows = _rows_by_id(conn, [aid for aid, _, _ in extra])
    for aid, cos, vn in extra:
        if aid in rows:
            by_id[aid] = _Cand(rows[aid], None, 0.0, cos, vn)
    return "hybrid"


def collapse(hits: Iterable[Hit], limit: int | None = None) -> list[Hit]:
    """Exact duplicates (same content_hash) collapse to their best-ranked copy."""
    chosen: list[Hit] = []
    seen: set[str] = set()
    for hit in hits:
        h = hit.row["content_hash"]
        if h:
            if h in seen:
                continue
            seen.add(h)
        chosen.append(hit)
        if limit is not None and len(chosen) >= limit:
            break
    return chosen


def _join_notes(*notes) -> str | None:
    parts = [n for n in notes if n]
    return "; ".join(parts) or None


def rank(
    conn: sqlite3.Connection,
    query: str,
    filters: SearchFilters | None = None,
    project: str | None = None,
    limit: int = 200,
    *,
    settings: SearchSettings | None = None,
    restrict: Iterable[int] | None = None,
) -> list[tuple[int, float]]:
    return [(h.id, round(h.score, 3)) for h in ranking(
        conn, query, filters, project, limit, settings=settings, restrict=restrict).hits]


@dataclass
class Ranking:
    mode: str  # "hybrid" | "keyword"
    model: str | None  # CLAP short name when hybrid
    weight: float
    notes: list[str]
    hits: list[Hit]


def ranking(
    conn: sqlite3.Connection,
    query: str,
    filters: SearchFilters | None = None,
    project: str | None = None,
    limit: int = 200,
    *,
    settings: SearchSettings | None = None,
    restrict: Iterable[int] | None = None,
) -> Ranking:
    limit = max(1, int(limit))
    if not query_terms(query):
        return Ranking("keyword", None, 0.0, [], [])
    c = candidates(conn, query, filters, project, limit, settings, restrict)
    weight = c.resolved.blend_weight if c.resolved and c.mode == "hybrid" else 0.0
    return Ranking(c.mode, c.resolved.clap_model if c.mode == "hybrid" else None, weight,
                   c.notes, c.order()[:limit])


def search(
    conn: sqlite3.Connection,
    query: str,
    filters: SearchFilters | None = None,
    project: str | None = None,
    limit: int = 20,
    *,
    settings: SearchSettings | None = None,
) -> dict:
    filters = filters or SearchFilters()
    limit = max(1, int(limit))
    terms = query_terms(query)
    fetch = max(limit * 10, _CANDIDATE_FLOOR)

    notes: list[str] = []
    mode, model = "filter", None
    if terms:
        c = candidates(conn, query, filters, project, fetch, settings)
        hits = c.order()
        notes = c.notes
        mode = c.mode
        model = c.resolved.clap_model if c.mode == "hybrid" and c.resolved else None
    else:
        where, args = _filter_sql(filters, project)
        sql = (
            _SELECT + ", 0.0 AS rank FROM assets a "
            "LEFT JOIN features f ON f.asset_id = a.id "
            "WHERE " + " AND ".join(where) + " ORDER BY a.rel_path LIMIT ?"
        )
        hits = [Hit(0.0, r, None) for r in conn.execute(sql, [*args, fetch]).fetchall()]

    results = [_compact(conn, h.score, h.row, filters.include_missing, h.similarity)
               for h in collapse(hits, limit)]
    out = {"note": _join_notes(_staleness_note(conn), *notes), "mode": mode, "results": results}
    if model:
        out["model"] = model
    return out


# --------------------------------------------------------------------------- similar


@dataclass
class Similar:
    source: sqlite3.Row  # the asset's id, rel_path, content_hash
    model: str | None
    notes: list[str]
    hits: list[tuple[int, float]]  # (asset id, cosine), best first, duplicates kept


def similar_ranking(
    conn: sqlite3.Connection,
    asset_id: int,
    filters: SearchFilters | None = None,
    project: str | None = None,
    limit: int = 200,
    *,
    settings: SearchSettings | None = None,
    restrict: Iterable[int] | None = None,
) -> Similar:
    src = conn.execute(
        "SELECT id, rel_path, content_hash FROM assets WHERE id = ?", (int(asset_id),)
    ).fetchone()
    if src is None:
        raise LookupError(f"no such asset: {asset_id!r}")
    resolved = resolve(conn, settings)
    name = resolved.clap_model
    if resolved.model_key is None:
        return Similar(src, None, ["find_similar needs CLAP embeddings: the CLAP models are "
                                   "not available"], [])
    index = vectors.get_index(conn, resolved.model_key)
    vec = index.vector(src["id"]) if index is not None else None
    if vec is None:
        why = ("this sound has no CLAP embedding yet" if index is not None
               else f"the catalog has no CLAP embeddings for {name} yet")
        return Similar(src, name, [f"no similar sounds: {why} (`foley index` computes them)"],
                       [])
    allowed = _allowed_ids(conn, filters or SearchFilters(), project)
    if restrict is not None:
        allowed = allowed[np.isin(allowed, np.fromiter((int(i) for i in restrict),
                                                       dtype=np.int64))]
    exclude = [src["id"]]
    if src["content_hash"]:
        exclude += [r[0] for r in conn.execute(
            "SELECT id FROM assets WHERE content_hash = ?", (src["content_hash"],))]
    hits = index.topk(vec, max(1, int(limit)), restrict=index.mask(allowed), exclude=exclude)
    return Similar(src, name, [], hits)


def find_similar(
    conn: sqlite3.Connection,
    asset_id: int,
    filters: SearchFilters | None = None,
    limit: int = 20,
    project: str | None = None,
    *,
    settings: SearchSettings | None = None,
) -> dict:
    filters = filters or SearchFilters()
    limit = max(1, int(limit))
    sim = similar_ranking(conn, asset_id, filters, project, max(limit * 5, 100),
                          settings=settings)
    rows = _rows_by_id(conn, [aid for aid, _ in sim.hits])
    hits = [Hit(cos, rows[aid], cos) for aid, cos in sim.hits if aid in rows]
    results = [_compact(conn, h.score, h.row, filters.include_missing, h.similarity)
               for h in collapse(hits, limit)]
    out = {
        "note": _join_notes(_staleness_note(conn), *sim.notes),
        "source": {"id": sim.source["id"], "path": sim.source["rel_path"]},
        "results": results,
    }
    if sim.model:
        out["model"] = sim.model
    return out


def _compact(
    conn: sqlite3.Connection, score: float, row: sqlite3.Row, include_missing: bool,
    similarity: float | None = None,
) -> dict:
    aid = row["id"]
    tag_rows = conn.execute(
        "SELECT label FROM tags WHERE asset_id = ? ORDER BY score DESC", (aid,)
    ).fetchall()
    labels = list(dict.fromkeys(r[0] for r in tag_rows))[:3]
    dupes = 0
    if row["content_hash"]:
        statuses = "('active', 'missing')" if include_missing else "('active')"
        # `+status` keeps SQLite on the content_hash index; without ANALYZE statistics it
        # otherwise walks every active row through assets_status (~8 ms per result).
        dupes = conn.execute(
            f"SELECT COUNT(*) FROM assets WHERE content_hash = ? AND id != ? AND +status IN {statuses}",  # noqa: E501
            (row["content_hash"], aid),
        ).fetchone()[0]
    return {
        "id": aid,
        "path": row["rel_path"],
        "score": round(score, 3),
        "similarity": None if similarity is None else round(similarity, 3),
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
