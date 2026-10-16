"""`foley eval`: measure search on the hand-labelled test set.

CONTRACT:

    DEFAULT_WEIGHTS = (0.0, 0.1, ..., 1.0)     blend weights swept per checkpoint
    RANK_DEPTH = 100                           how deep a good answer is looked for (MRR)
    class EvalError(Exception)                 unusable test set / options

    run(cfg, conn, testset_path=None, clap=None, weights=None, k=10, progress=None) -> dict
        Reads the test set (foley.testset; default [eval] testset). Queries without good
        answers are skipped (counted). For every answered query and config it runs the
        search_audio ranking (search.candidates with search's own candidate depth for
        limit k, then exact duplicates collapsed) and finds the rank of the first good
        answer: a result counts when its rel_path is listed OR it is an exact duplicate
        (same content_hash) of a listed file, since search collapses copies.
        Configs: keyword-only (weight 0, always) and each CLAP checkpoint x weight > 0.
        `clap` None = every checkpoint with embeddings in the catalog. Per config:
        hits (good answer in top k), recall (hits / answered queries, the spec's
        "recall@10"), mrr (mean of 1/rank, rank <= RANK_DEPTH, else 0).
        Report (JSON-serializable):
          {created_at, testset, k, rank_depth, queries_total, queries_answered,
           skipped: [text], warnings: [str],
           models: {name: {key, embeddings, status}},
           configs: [{name, clap_model, weight, hits, recall, mrr,
                      per_query: [{index, text, rank, hit, found, top: [paths]}]}],
           keyword: {recall, mrr}, best: {name, clap_model, weight, recall, mrr} | None,
           hybrid_beats_keyword: bool,
           recommendation: {clap_model, blend_weight, reason}}
        Warnings name good paths that are not active assets (not in the catalog,
        missing, error, pending), noting when an active exact copy still lets it pass.
        Without CLAP (vectors.clap_api() None: the optional `models` extra is not
        installed) only keyword-only is measured; a warning and the recommendation's
        reason say so.
        Read-only on the catalog.
    format_report(report) -> str               compact table, best config, its misses
    write_report(cfg, report) -> Path          data_dir/eval/<UTC timestamp>.json
    adopt(conn, report) -> dict                writes meta search_clap_model,
        search_blend_weight and search_eval (JSON summary); CLAP is kept only if the
        best hybrid config beats keyword-only on (recall, MRR), else weight 0 is
        adopted (the checkpoint that did best is still recorded). Returns
        {clap_model, blend_weight, reason}. Raises EvalError, writing nothing, when no
        CLAP configuration was measured (models extra missing or no embeddings yet), so
        a machine that cannot compare never overwrites a tuned setting.

DESIGN NOTES:

    Each query is ranked once per checkpoint (one text embedding, one keyword query)
    and re-blended for every weight via search.Candidates.order, so a sweep of 11
    weights costs about one search per checkpoint. Ties between configs go to
    keyword-only, then to the middle of a plateau of equally good weights.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from foley import search, testset, vectors
from foley.catalog import db

DEFAULT_WEIGHTS = tuple(round(i / 10, 1) for i in range(11))
RANK_DEPTH = 100


class EvalError(Exception):
    pass


def _answers(conn: sqlite3.Connection, n: int, q: testset.Query, warnings: list[str]):
    """(good paths, good content hashes) for one query, warning about unusable paths."""
    paths, hashes = set(), set()
    usable = False
    for rel in q.good:
        paths.add(rel)
        row = conn.execute(
            "SELECT status, content_hash FROM assets WHERE rel_path = ?", (rel,)
        ).fetchone()
        if row is None:
            warnings.append(f"query {n}: good answer is not in the catalog: {rel}")
            continue
        if row["content_hash"]:
            hashes.add(row["content_hash"])
        if row["status"] == "active":
            usable = True
            continue
        copy = row["content_hash"] and conn.execute(
            "SELECT 1 FROM assets WHERE content_hash = ? AND status = 'active' LIMIT 1",
            (row["content_hash"],),
        ).fetchone()
        if copy:
            usable = True
            warnings.append(f"query {n}: good answer is {row['status']}, but an active exact "
                            f"copy can still match: {rel}")
        else:
            warnings.append(f"query {n}: good answer is {row['status']}, not an active "
                            f"asset: {rel}")
    if q.good and not usable:
        warnings.append(f"query {n}: no good answer is an active asset, so it cannot pass: "
                        f"{q.text!r}")
    return paths, hashes


def _first_good(hits: list[search.Hit], paths: set, hashes: set) -> tuple[int | None, str | None]:
    for i, h in enumerate(hits, 1):
        if h.row["rel_path"] in paths or (h.row["content_hash"] and h.row["content_hash"]
                                          in hashes):
            return i, h.row["rel_path"]
    return None, None


def _parse_weights(weights) -> list[float]:
    out = []
    for w in weights if weights is not None else DEFAULT_WEIGHTS:
        w = float(w)
        if not 0 <= w <= 1:
            raise EvalError(f"blend weights must be between 0 and 1, got {w}")
        if round(w, 4) not in out:
            out.append(round(w, 4))
    return sorted(out)


def _models(conn: sqlite3.Connection, clap, warnings: list[str]) -> dict:
    present = vectors.embedding_models(conn)
    if vectors.clap_api() is None:
        warnings.append("CLAP is not installed (the optional `models` extra), so only "
                        "keyword-only search is measured")
    names = list(clap) if clap else vectors.checkpoints()
    models = {}
    for name in names:
        key = vectors.model_key(name)
        if key is None:
            status = "unavailable" if vectors.clap_api() is None else "unknown checkpoint"
            models[name] = {"key": None, "embeddings": 0, "status": status}
            if clap:
                warnings.append(f"CLAP checkpoint {name!r}: {status}")
            continue
        count = present.get(key, 0)
        models[name] = {"key": key, "embeddings": count,
                        "status": "ok" if count else "no embeddings"}
        if clap and not count:
            warnings.append(f"CLAP checkpoint {name!r} has no embeddings in the catalog "
                            "(`foley index` computes the checkpoints in [models] clap)")
    if not clap:  # only report checkpoints the catalog has
        models = {n: m for n, m in models.items() if m["status"] == "ok"}
    return models


def _score(per_query: list[dict], k: int) -> tuple[int, float, float]:
    n = len(per_query)
    hits = sum(1 for p in per_query if p["hit"])
    mrr = sum(1.0 / p["rank"] for p in per_query if p["rank"]) / n if n else 0.0
    return hits, (hits / n if n else 0.0), mrr


def _entry(index: int, q: testset.Query, hits: list[search.Hit], paths, hashes, k) -> dict:
    rank, found = _first_good(hits, paths, hashes)
    return {"index": index, "text": q.text, "rank": rank, "hit": rank is not None and rank <= k,
            "found": found, "top": [h.row["rel_path"] for h in hits[:3]]}


def run(cfg, conn: sqlite3.Connection, testset_path=None, clap=None, weights=None, k: int = 10,
        progress=None) -> dict:
    say = progress or (lambda line: None)
    path = Path(testset_path) if testset_path else cfg.testset
    if path is None:
        raise EvalError("no test set: pass --testset or set [eval] testset in foley.toml "
                        "(format: eval/testset.example.toml)")
    if not Path(path).is_file():
        raise EvalError(f"test set not found: {path}")
    try:
        ts = testset.load(path)
    except testset.TestsetError as e:
        raise EvalError(str(e)) from e
    if k < 1:
        raise EvalError("k must be at least 1")
    weights = _parse_weights(weights)
    hybrid_weights = [w for w in weights if w > 0]
    warnings: list[str] = []

    answered = []
    skipped = []
    for n, q in enumerate(ts.queries, 1):
        if not q.good:
            skipped.append(q.text)
            continue
        paths, hashes = _answers(conn, n, q, warnings)
        answered.append((n, q, testset.search_filters(q.filters), paths, hashes))
    if not answered:
        raise EvalError(f"none of the {len(ts.queries)} queries in {path} has good answers "
                        "yet; mark some in `foley browse` (Test set) or edit the file")

    models = _models(conn, clap, warnings) if hybrid_weights else {}
    fetch = max(k * 10, 200)  # what search.search uses for limit k
    keyword_rows = []
    for n, q, filters, paths, hashes in answered:
        c = search.candidates(conn, q.text, filters, fetch=fetch,
                              settings=search.SearchSettings(semantic=False))
        keyword_rows.append(_entry(n, q, search.collapse(c.order(0.0), RANK_DEPTH), paths,
                                   hashes, k))
    configs = [_config("keyword only", None, 0.0, keyword_rows, k)]

    for name, info in models.items():
        if info["status"] != "ok":
            continue
        say(f"ranking {len(answered)} queries with CLAP {name} "
            "(the first query loads the text encoder)")
        per_weight: dict[float, list[dict]] = {w: [] for w in hybrid_weights}
        failed = None
        for n, q, filters, paths, hashes in answered:
            c = search.candidates(conn, q.text, filters, fetch=fetch,
                                  settings=search.SearchSettings(clap_model=name),
                                  semantic=True)
            if c.mode != "hybrid":
                failed = "; ".join(c.notes) or "no vector scores"
                break
            for w in hybrid_weights:
                per_weight[w].append(_entry(n, q, search.collapse(c.order(w), RANK_DEPTH),
                                            paths, hashes, k))
        if failed:
            info["status"] = f"failed: {failed}"
            warnings.append(f"CLAP checkpoint {name!r} skipped: {failed}")
            continue
        for w in hybrid_weights:
            configs.append(_config(f"{name} w={w:g}", name, w, per_weight[w], k))

    keyword = configs[0]
    best = _best(configs)
    hybrid_best = _best([c for c in configs if c["clap_model"]])
    beats = hybrid_best is not None and (hybrid_best["recall"], hybrid_best["mrr"]) > (
        keyword["recall"], keyword["mrr"])
    if beats:
        rec = {"clap_model": hybrid_best["clap_model"], "blend_weight": hybrid_best["weight"],
               "reason": f"hybrid {hybrid_best['name']} beats keyword-only"}
    elif hybrid_best is not None:
        rec = {"clap_model": hybrid_best["clap_model"], "blend_weight": 0.0,
               "reason": "no hybrid config beats keyword-only, so search stays keyword-only "
                         f"(blend weight 0); best checkpoint was {hybrid_best['clap_model']}"}
    else:
        why = ("CLAP is not installed" if vectors.clap_api() is None
               else "no CLAP embeddings to compare")
        rec = {"clap_model": None, "blend_weight": 0.0, "reason": f"{why}: keyword-only"}

    def summary(c):
        return None if c is None else {k2: c[k2] for k2 in
                                       ("name", "clap_model", "weight", "hits", "recall", "mrr")}

    return {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "testset": str(path),
        "k": k,
        "rank_depth": RANK_DEPTH,
        "queries_total": len(ts.queries),
        "queries_answered": len(answered),
        "skipped": skipped,
        "warnings": warnings,
        "models": models,
        "configs": configs,
        "keyword": summary(keyword),
        "best": summary(best),
        "hybrid_best": summary(hybrid_best),
        "hybrid_beats_keyword": beats,
        "recommendation": rec,
    }


def _config(name, model, weight, per_query, k) -> dict:
    hits, recall, mrr = _score(per_query, k)
    return {"name": name, "clap_model": model, "weight": weight, "hits": hits,
            "recall": round(recall, 4), "mrr": round(mrr, 4), "per_query": per_query}


def _best(configs: list[dict]) -> dict | None:
    if not configs:
        return None
    top = max((c["recall"], c["mrr"]) for c in configs)
    tied = [c for c in configs if (c["recall"], c["mrr"]) == top]
    for c in tied:
        if c["clap_model"] is None:
            return c  # keyword-only wins ties: CLAP has to earn its place
    model = tied[0]["clap_model"]
    same = sorted((c for c in tied if c["clap_model"] == model), key=lambda c: c["weight"])
    return same[(len(same) - 1) // 2]  # middle of a plateau of equally good weights


# --------------------------------------------------------------------------- output


def format_report(report: dict) -> str:
    k = report["k"]
    lines = [
        f"foley eval: {report['queries_answered']} of {report['queries_total']} queries have "
        f"answers"
        + (f" ({len(report['skipped'])} skipped)" if report["skipped"] else "")
        + f", k={k}, testset {report['testset']}",
    ]
    if report["models"]:
        lines.append("checkpoints: " + ", ".join(
            f"{n} ({m['embeddings']:,} embeddings{'' if m['status'] == 'ok' else ', ' + m['status']})"  # noqa: E501
            for n, m in report["models"].items()))
    else:
        lines.append("checkpoints: none with embeddings in the catalog (keyword-only)")
    best = report["best"]
    width = max([len("config")] + [len(c["name"]) for c in report["configs"]]) + 2
    lines.append("")
    lines.append(f"  {'config':<{width}}{'recall@' + str(k):>10}{'MRR':>8}{'hits':>9}")
    n = report["queries_answered"]
    for c in report["configs"]:
        mark = "*" if best and c["name"] == best["name"] else " "
        lines.append(f"{mark} {c['name']:<{width}}{c['recall']:>10.3f}{c['mrr']:>8.3f}"
                     f"{c['hits']:>5}/{n:<3}")
    lines.append("")
    kw = report["keyword"]
    hb = report.get("hybrid_best")
    if hb:
        verdict = "beats" if report["hybrid_beats_keyword"] else "does NOT beat"
        lines.append(f"best hybrid: {hb['name']}: recall@{k} {hb['recall']:.3f}, "
                     f"MRR {hb['mrr']:.3f}; {verdict} keyword-only "
                     f"({kw['recall']:.3f}, {kw['mrr']:.3f})")
    rec = report["recommendation"]
    lines.append(f"recommendation: clap_model={rec['clap_model']}, "
                 f"blend_weight={rec['blend_weight']:g} ({rec['reason']})")
    if best:
        cfg = next(c for c in report["configs"] if c["name"] == best["name"])
        misses = [p for p in cfg["per_query"] if not p["hit"]]
        lines.append(f"misses for {cfg['name']} ({len(misses)} of {n}):")
        for p in misses:
            where = (f"first good answer at rank {p['rank']}" if p["rank"]
                     else f"no good answer in the top {report['rank_depth']}")
            top = f"; top: {p['top'][0]}" if p["top"] else "; no results"
            lines.append(f"  {p['index']:>3}. {p['text']!r}: {where}{top}")
        if not misses:
            lines.append("  none")
    if report["warnings"]:
        lines.append("warnings:")
        lines += [f"  {w}" for w in report["warnings"]]
    return "\n".join(lines)


def write_report(cfg, report: dict) -> Path:
    out_dir = cfg.data_dir / "eval"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{stamp}.json"
    i = 1
    while path.exists():
        path = out_dir / f"{stamp}-{i}.json"
        i += 1
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    report["report_path"] = str(path)
    return path


def adopt(conn: sqlite3.Connection, report: dict) -> dict:
    if not report.get("hybrid_best"):
        raise EvalError("nothing to adopt: no CLAP configuration was measured "
                        f"({report['recommendation']['reason']}); meta left unchanged")
    rec = report["recommendation"]
    best = report.get("hybrid_best") or report.get("keyword")
    summary = {
        "at": report["created_at"],
        "testset": report["testset"],
        "k": report["k"],
        "queries": report["queries_answered"],
        "keyword": {"recall": report["keyword"]["recall"], "mrr": report["keyword"]["mrr"]},
        "best_hybrid": None if not report.get("hybrid_best") else {
            kk: best[kk] for kk in ("clap_model", "weight", "recall", "mrr")},
        "report": report.get("report_path"),
    }
    with db.transaction(conn):
        if rec["clap_model"]:
            db.set_meta(conn, search.META_CLAP_MODEL, rec["clap_model"])
        db.set_meta(conn, search.META_BLEND_WEIGHT, f"{rec['blend_weight']:g}")
        db.set_meta(conn, "search_eval", json.dumps(summary, ensure_ascii=False))
    return dict(rec)
