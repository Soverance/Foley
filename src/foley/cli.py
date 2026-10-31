"""`foley` command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from foley import __version__
from foley import config as config_mod
from foley.catalog import db


def _print_json(data) -> None:
    print(json.dumps(data, indent=2, default=str, ensure_ascii=False))


def _human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def cmd_index(cfg, args) -> int:
    from foley import sync

    stages = args.stage or None
    try:
        summary = sync.run_index(
            cfg,
            stages=stages,
            force=args.force,
            limit=args.limit,
            progress=print,
            allow_mass_missing=args.allow_mass_missing,
            workers=args.workers,
        )
    except (sync.IndexLocked, sync.LibraryUnavailable, config_mod.ConfigError, ValueError) as e:
        print(f"foley: {e}", file=sys.stderr)
        return 2
    print(
        f"run {summary.run_id}: scanned {summary.scanned}, added {summary.added}, "
        f"changed {summary.changed}, moved {summary.moved}, missing {summary.missing}, "
        f"analyzed {summary.analyzed}, errors {summary.errors}, "
        f"settling {summary.settling}, archives {summary.archives}"
        + (" (interrupted; run again to resume)" if summary.interrupted else "")
    )
    return 130 if summary.interrupted else 0


def _filters(args):
    from foley.search import SearchFilters

    is_music = {"yes": True, "no": False}.get(args.music) if args.music else None
    return SearchFilters(
        category=args.category,
        pack=args.pack,
        is_music=is_music,
        min_duration_s=args.min_duration,
        max_duration_s=args.max_duration,
        min_lufs=args.min_lufs,
        max_lufs=args.max_lufs,
    )


def cmd_search(cfg, args) -> int:
    from foley import search

    with db.open_catalog(cfg) as conn:
        result = search.search(
            conn, " ".join(args.query), _filters(args), project=args.project, limit=args.limit,
            settings=search.SearchSettings.from_config(cfg),
        )
    if args.json:
        _print_json(result)
        return 0
    if result.get("note"):
        print(f"note: {result['note']}")
    for r in result["results"]:
        lufs = f"{r['lufs']:.1f} LUFS" if r.get("lufs") is not None else "short"
        dur = f"{r['duration_s']:.2f}s" if r.get("duration_s") is not None else "?"
        sim = f"  sim {r['similarity']:.2f}" if r.get("similarity") is not None else ""
        print(f"[{r['id']}] {r['path']}")
        print(f"      {dur}  {lufs}{sim}  {r.get('caption') or ''}")
    if not result["results"]:
        print("no results")
    return 0


def cmd_eval(cfg, args) -> int:
    from foley import evaluate

    try:
        weights = [float(w) for w in args.weights.split(",") if w.strip()] \
            if args.weights else None
    except ValueError:
        print(f"foley: --weights must be comma-separated numbers, got {args.weights!r}",
              file=sys.stderr)
        return 2
    conn = db.open_catalog(cfg)
    try:
        try:
            report = evaluate.run(cfg, conn, args.testset, clap=args.clap, weights=weights,
                                  k=args.k, progress=None if args.json else print)
        except evaluate.EvalError as e:
            print(f"foley: {e}", file=sys.stderr)
            return 2
        path = evaluate.write_report(cfg, report)
        try:
            adopted = evaluate.adopt(conn, report) if args.adopt else None
        except evaluate.EvalError as e:
            print(f"foley: {e}", file=sys.stderr)
            return 2
    finally:
        conn.close()
    if args.json:
        _print_json({**report, "adopted": adopted})
        return 0
    print(evaluate.format_report(report))
    print(f"report: {path}")
    if adopted:
        print(f"adopted: meta search_clap_model={adopted['clap_model']}, "
              f"search_blend_weight={adopted['blend_weight']:g}")
        for key, name in (("search_blend_weight", "blend_weight"),
                          ("search_clap_model", "clap_model")):
            if getattr(cfg, key) is not None:
                print(f"note: [search] {name} in {cfg.source or 'foley.toml'} overrides the "
                      "adopted value; remove it to use the adopted one")
    elif report["best"]:
        print("run with --adopt to record the recommendation in the catalog's meta table")
    return 0


def cmd_describe(cfg, args) -> int:
    from foley import search

    target = int(args.asset) if args.asset.isdigit() else args.asset
    with db.open_catalog(cfg) as conn:
        try:
            _print_json(search.describe(conn, target))
        except LookupError as e:
            print(f"foley: {e.args[0] if e.args else e}", file=sys.stderr)
            return 1
    return 0


def cmd_render(cfg, args) -> int:
    from foley import images

    with db.open_catalog(cfg) as conn:
        if len(args.asset_ids) == 1:
            print(images.render_overview(cfg, conn, args.asset_ids[0], force=args.force))
        else:
            print(images.render_comparison(cfg, conn, args.asset_ids))
    return 0


def cmd_status(cfg, args) -> int:
    from foley import status

    with db.open_catalog(cfg) as conn:
        _print_json(status.catalog_status(cfg, conn))
    return 0


def cmd_prune(cfg, args) -> int:
    from foley import sync

    try:
        removed = sync.prune(cfg, dry_run=not args.apply)
    except (sync.IndexLocked, config_mod.ConfigError) as e:
        print(f"foley: {e}", file=sys.stderr)
        return 2
    verb = "removed" if args.apply else "would remove"
    for rel in removed:
        print(f"  {rel}")
    print(f"{verb} {len(removed)} catalog rows (the library is never touched)")
    return 0


def cmd_dedupe(cfg, args) -> int:
    from foley import dedupe

    if args.action != "report":
        print("foley: only `dedupe report` is available yet (plan/apply arrive in Phase 2)",
              file=sys.stderr)
        return 2
    with db.open_catalog(cfg) as conn:
        rep = dedupe.report(cfg, conn)
    if args.json:
        _print_json(rep)
        return 0
    for group in rep["groups"]:
        size = _human_bytes(group["size_bytes"])
        print(f"{size} x{len(group['members'])}  keep {group['survivor']}")
        for member in group["members"]:
            if member != group["survivor"]:
                print(f"    remove {member}")
    print(
        f"{rep['exact_groups']} duplicate groups, {rep['files_removable']} removable files, "
        f"{_human_bytes(rep['reclaimable_bytes'])} reclaimable "
        f"({len(rep['audio_identical'])} audio-identical groups reported only). "
        "Nothing was changed."
    )
    return 0


def cmd_browse(cfg, args) -> int:
    import logging

    from foley import browse

    host = args.host if args.host is not None else cfg.browse_host
    port = args.port if args.port is not None else cfg.browse_port
    try:
        browse.serve(cfg, host=host, port=port, open_browser=not args.no_open,
                     verbose=args.verbose, stop_on_eof=args.stop_on_stdin_eof)
    except OSError as e:  # port in use, address not available, ...
        # Logged, not printed: under pythonw.exe there is no stderr (configure_logging
        # sends it to the console when there is one, and to --log-file).
        logging.getLogger("foley.browse").error(
            "foley: cannot serve on %s:%s: %s (is another foley browse or web server "
            "using the port?)", host, port, e)
        return 2
    except Exception:
        logging.getLogger("foley.browse").exception("foley browse crashed")
        raise
    return 0


def cmd_tray(cfg, args) -> int:
    from foley import tray

    return tray.run(cfg, log_file=args.log_file)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="foley", description=__doc__)
    p.add_argument("--config", help="path to foley.toml")
    p.add_argument("--version", action="version", version=f"foley {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("index", help="sync the catalog with the library and analyze new files")
    s.add_argument(
        "--stage", action="append",
        help="only run this stage (repeatable): metadata, features, loudness, caption, fts, "
        "tags, embeddings",
    )
    s.add_argument("--force", action="store_true", help="re-run the stages even if current")
    s.add_argument("--limit", type=int, help="analyze at most N files (trial runs)")
    s.add_argument(
        "--workers",
        type=int,
        help="analysis processes (default: auto, up to 8; 1 = in-process)",
    )
    s.add_argument(
        "--allow-mass-missing",
        action="store_true",
        help="proceed even if this scan would mark many files missing (e.g. after a reorg)",
    )
    s.set_defaults(func=cmd_index)

    s = sub.add_parser("search", help="search the catalog")
    s.add_argument("query", nargs="+")
    s.add_argument("--category")
    s.add_argument("--pack")
    s.add_argument("--music", choices=["yes", "no"])
    s.add_argument("--min-duration", type=float)
    s.add_argument("--max-duration", type=float)
    s.add_argument("--min-lufs", type=float)
    s.add_argument("--max-lufs", type=float)
    s.add_argument("--project", help="hide files rejected for this project")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("eval", help="score search on the test set (keyword-only vs hybrid)")
    s.add_argument("--testset", help="test set TOML (default: [eval] testset)")
    s.add_argument("--clap", nargs="+", action="extend", metavar="NAME",
                   help="CLAP checkpoints to compare (default: every one with embeddings)")
    s.add_argument("--weights", metavar="W,W,...",
                   help="blend weights to sweep (default 0,0.1,...,1; 0 = keyword-only)")
    s.add_argument("--k", type=int, default=10, help="a query passes with a good answer in "
                   "the top k (default 10)")
    s.add_argument("--adopt", action="store_true",
                   help="record the best checkpoint and weight in the catalog meta table")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_eval)

    s = sub.add_parser("describe", help="full record for one asset (id or relative path)")
    s.add_argument("asset")
    s.set_defaults(func=cmd_describe)

    s = sub.add_parser("render", help="render an asset image, or a comparison grid of 2-6")
    s.add_argument("asset_ids", nargs="+", type=int)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_render)

    s = sub.add_parser("status", help="catalog health and staleness")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("prune", help="drop catalog rows for long-missing files (dry run)")
    s.add_argument("--apply", action="store_true", help="actually remove the rows")
    s.set_defaults(func=cmd_prune)

    s = sub.add_parser("dedupe", help="find duplicate files in the library")
    s.add_argument("action", choices=["report"])
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_dedupe)

    s = sub.add_parser("browse", help="local web UI to browse, search and audition the library")
    s.add_argument("--host", default=None,
                   help="address to listen on (default: [browse] host, else 127.0.0.1)")
    s.add_argument("--port", type=int, default=None,
                   help="port (default: [browse] port, else 8765)")
    s.add_argument("--no-open", action="store_true", help="do not open a browser")
    s.add_argument("--verbose", action="store_true", help="log every request")
    s.add_argument("--log-file", metavar="PATH",
                   help="also log server and scan events to this size-rotated file")
    s.add_argument("--stop-on-stdin-eof", action="store_true",
                   help="stop cleanly when standard input closes (used by foley tray)")
    s.set_defaults(func=cmd_browse)

    s = sub.add_parser("tray", help="run foley browse from a system-tray icon (Windows)")
    s.add_argument("--log-file", metavar="PATH",
                   help="the server's log (default <data dir>/logs/browse.log); the tray "
                   "logs to tray.log beside it")
    s.set_defaults(func=cmd_tray)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "browse":
        # Before the config loads, so a bad config is in the log file too (under
        # pythonw.exe, e.g. a logon task, there is no console to print it to).
        from foley import browse

        browse.configure_logging(args.log_file)
    elif args.command == "tray":
        # Same reason; the tray must not import browse (numpy), so it has its own.
        from foley import tray

        tray.configure_logging(tray.tray_log_path(args.log_file))
    try:
        cfg = config_mod.load(args.config)
    except config_mod.ConfigError as e:
        if args.command in ("browse", "tray"):
            import logging

            logging.getLogger(f"foley.{args.command}").error("foley: %s", e)
        else:
            print(f"foley: {e}", file=sys.stderr)
        return 2
    if not Path(cfg.library_root).exists() and args.command == "index":
        print(f"foley: library root not reachable: {cfg.library_root}", file=sys.stderr)
        return 2
    return args.func(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
