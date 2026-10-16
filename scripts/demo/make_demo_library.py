"""Build a small demo library by copying a curated selection out of a real one.

Foley's screenshots and demos run against a throwaway library, never the real one, so
that nothing studio-specific shows up and the real catalog stays untouched. This script
makes that library in two steps:

    suggest   read an existing Foley catalog.db (opened read-only) and write a
              selection file: a varied pick of files per sound category, matched by
              name, capped in size and duration.
    copy      copy the files named in a selection file from the source library into a
              local folder, keeping their library-relative paths, and optionally write
              a foley.toml for the demo.

The selection file is plain text: one library-relative path per line (forward slashes,
as Foley stores them), blank lines and `#` comments allowed. Edit it by hand between the
two steps if you like. It names files in your library, so keep it out of the repo, and
never commit the copied audio: sample licences usually forbid redistributing it.

The source library is only ever read: files are opened "rb" and copied to the
destination, which must lie outside the source root. Re-running `copy` skips files that
are already there with the same size, so an interrupted copy resumes.

Examples (PowerShell; paths are placeholders):

    # 1. pick ~300 files from the catalog of an indexed library
    uv run python scripts/demo/make_demo_library.py suggest `
        --catalog "$env:LOCALAPPDATA/Foley/catalog.db" --root-prefix packs/ `
        --out "$env:LOCALAPPDATA/FoleyDemo/selection.txt"

    # 2. copy them and write a demo config next to the library
    uv run python scripts/demo/make_demo_library.py copy `
        --source "//nas.example.local/Audio" `
        --selection "$env:LOCALAPPDATA/FoleyDemo/selection.txt" `
        --dest "$env:LOCALAPPDATA/FoleyDemo/Library" `
        --write-config "$env:LOCALAPPDATA/FoleyDemo/foley.toml" --port 8793

    # 3. index the demo library with its own catalog
    uv run --extra models foley --config "$env:LOCALAPPDATA/FoleyDemo/foley.toml" index

`--source` may be omitted when `--source-config` points at the real foley.toml; its
[library] root is used. Only the standard library is needed, so the script also runs
outside Foley's environment.
"""

from __future__ import annotations

import argparse
import collections
import os
import random
import re
import shutil
import sqlite3
import sys
import tomllib
from pathlib import Path

# Default categories for `suggest`: (name, regex matched against the path, regex of paths
# to leave out or None, how many files to take, longest duration in seconds). A file joins
# the first category that matches it. The regexes are generic sound-design vocabulary.
CATEGORIES: list[tuple[str, str, str | None, int, float]] = [
    ("laser", r"laser|blaster|plasma|zap", None, 18, 6),
    ("scifi-weapon", r"sci.?fi.*(gun|weapon|shot|fire)|future.*(gun|weapon)|railgun", None, 14, 6),
    ("firearm", r"gunshot|pistol|rifle|shotgun|reload", r"sci|laser", 10, 6),
    ("metal", r"metal.*(hit|impact|clang|crash)|anvil|clang", None, 22, 15),
    ("impact", r"impact|punch|thud|slam", r"metal", 18, 8),
    ("explosion", r"explosion|explode|blast|detonat|grenade|debris", r"laser|blaster", 20, 12),
    ("ui", r"(^|/)ui|interface|menu|button|click|confirm|beep|notification|hud", None, 30, 2.5),
    ("whoosh", r"whoosh|swoosh|swish|swing|pass.?by|flyby", None, 20, 6),
    ("ambience", r"ambien|ambiance|atmos|room.?tone|forest|wind", r"whoosh", 20, 90),
    ("creature", r"creature|monster|growl|roar|beast|dragon|zombie|orc", None, 22, 10),
    ("mechanical", r"servo|motor|robot|mech|hydraulic|gear|machine", r"voice", 20, 12),
    ("magic", r"magic|spell|enchant|fairy|sparkle|shimmer", None, 20, 8),
    ("footsteps", r"footstep|step|walk", None, 18, 10),
    ("water", r"water|splash|rain|river|stream|drip|underwater|bubbl|ocean|wave", None, 20, 45),
    ("electric", r"electric|spark|voltage|buzz", r"laser", 10, 8),
    ("door", r"door|lock|latch|drawer", None, 8, 6),
    ("glass", r"glass.*(break|smash|shatter)|shatter", None, 6, 6),
    ("music", r"music|melod|stinger|orchestra|piano|pad_|_key_", r"ambien|engine", 12, 120),
]

# Paths no category takes: voices (they may be actors) and anything named as copyrighted.
DEFAULT_EXCLUDE = r"voice|vox|dialog|copyright"


def read_selection(path: Path) -> list[str]:
    """Library-relative paths from a selection file (comments and blanks dropped)."""
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line.replace("\\", "/").lstrip("/"))
    return list(dict.fromkeys(out))


def _descriptive(rel: str) -> bool:
    """A file name with at least two real words, so the demo reads well."""
    stem = rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    words = [w for w in re.split(r"[\s_\-.(),]+", stem) if re.fullmatch(r"[A-Za-z]{3,}", w)]
    return len(words) >= 2 and len(stem) <= 70


def suggest(args) -> int:
    catalog = Path(args.catalog)
    if not catalog.is_file():
        print(f"no catalog at {catalog}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(f"{catalog.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    max_bytes = args.max_mb * 1e6
    rows = conn.execute(
        "SELECT a.id, a.rel_path, a.size_bytes, f.duration_s FROM assets a "
        "JOIN features f ON f.asset_id = a.id "
        "WHERE a.status = 'active' AND a.size_bytes <= ? AND f.duration_s >= 0.15",
        (max_bytes,),
    ).fetchall()
    conn.close()
    prefixes = tuple(args.root_prefix or ())
    exclude = re.compile(args.exclude, re.I) if args.exclude else None
    rows = [
        r for r in rows
        if (not prefixes or r["rel_path"].startswith(prefixes))
        and not (exclude and exclude.search(r["rel_path"]))
        and _descriptive(r["rel_path"])
    ]
    rng = random.Random(args.seed)
    taken: set[int] = set()
    picks: dict[str, list[str]] = {}
    total = 0
    for name, inc, exc, count, max_dur in CATEGORIES:
        inc_re = re.compile(inc, re.I)
        exc_re = re.compile(exc, re.I) if exc else None
        cands = [
            r for r in rows
            if r["id"] not in taken and r["duration_s"] <= max_dur
            and inc_re.search(r["rel_path"]) and not (exc_re and exc_re.search(r["rel_path"]))
        ]
        # Round-robin over sub-packs (the first three path parts) so one big pack does
        # not fill a category.
        groups: dict[str, list] = collections.defaultdict(list)
        for r in cands:
            groups["/".join(r["rel_path"].split("/")[:3])].append(r)
        keys = sorted(groups)
        rng.shuffle(keys)
        chosen: list = []
        while len(chosen) < count and any(groups[k] for k in keys):
            for k in keys:
                if groups[k] and len(chosen) < count:
                    chosen.append(groups[k].pop(rng.randrange(len(groups[k]))))
        for r in chosen:
            taken.add(r["id"])
            total += r["size_bytes"]
        picks[name] = sorted(r["rel_path"] for r in chosen)
        print(f"{name:13} {len(chosen):3} of {len(cands)} candidates")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="\n") as f:
        f.write("# Demo library selection (make_demo_library.py suggest). One library-relative\n"
                "# path per line; edit freely, then run `copy`.\n")
        for name, paths in picks.items():
            f.write(f"\n# {name}\n")
            f.writelines(p + "\n" for p in paths)
    print(f"{len(taken)} files, {total / 1e6:.0f} MB -> {out}")
    return 0


def _inside(child: Path, parent: Path) -> bool:
    c = os.path.normcase(os.path.realpath(child))
    p = os.path.normcase(os.path.realpath(parent)).rstrip("\\/")
    return c == p or c.startswith(p + os.sep)


def _source_root(args) -> Path:
    if args.source:
        return Path(args.source)
    if not args.source_config:
        raise SystemExit("give --source or --source-config")
    with open(args.source_config, "rb") as f:
        root = tomllib.load(f).get("library", {}).get("root")
    if not root:
        raise SystemExit(f"{args.source_config} has no [library] root")
    return Path(root)


DEMO_CONFIG = """\
# Demo configuration written by scripts/demo/make_demo_library.py.
[library]
root = "{root}"
pack_roots = ["packs"]
settle_minutes = 0

[storage]
data_dir = "{data_dir}"

[models]
enabled = true
device = "auto"

[browse]
host = "127.0.0.1"
port = {port}

[schedule]
enabled = false
"""


def copy(args) -> int:
    source = _source_root(args)
    dest = Path(args.dest)
    if not source.is_dir():
        print(f"source library not reachable: {source}", file=sys.stderr)
        return 2
    if _inside(dest, source) or _inside(source, dest):
        print("--dest must be outside the source library (and not contain it)", file=sys.stderr)
        return 2
    selection = read_selection(Path(args.selection))
    copied = skipped = missing = 0
    size = 0
    max_bytes = args.max_mb * 1e6 if args.max_mb else None
    for rel in selection:
        src = source.joinpath(*rel.split("/"))
        dst = dest.joinpath(*rel.split("/"))
        try:
            st = src.stat()
        except OSError:
            print(f"  missing: {rel}", file=sys.stderr)
            missing += 1
            continue
        if max_bytes and st.st_size > max_bytes:
            print(f"  too big, skipped: {rel}", file=sys.stderr)
            continue
        size += st.st_size
        if dst.exists() and dst.stat().st_size == st.st_size:
            skipped += 1
            continue
        if args.dry_run:
            copied += 1
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".part")
        with src.open("rb") as fin, tmp.open("wb") as fout:  # source opened read-only
            shutil.copyfileobj(fin, fout, 1 << 20)
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))  # on the copy, never the source
        os.replace(tmp, dst)
        copied += 1
    verb = "would copy" if args.dry_run else "copied"
    print(f"{verb} {copied}, already there {skipped}, missing {missing}; "
          f"{size / 1e6:.0f} MB in {dest}")
    if args.write_config and not args.dry_run:
        cfg = Path(args.write_config)
        data_dir = Path(args.data_dir) if args.data_dir else cfg.parent / "data"
        if _inside(data_dir, dest):
            print("--data-dir must be outside --dest", file=sys.stderr)
            return 2
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(DEMO_CONFIG.format(root=dest.resolve().as_posix(),
                                          data_dir=data_dir.resolve().as_posix(),
                                          port=args.port), encoding="utf-8")
        print(f"wrote {cfg}")
    return 1 if missing and not copied and not skipped else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("suggest", help="write a selection file from a catalog.db (read-only)")
    s.add_argument("--catalog", required=True, help="catalog.db of the indexed source library")
    s.add_argument("--out", required=True, help="selection file to write")
    s.add_argument("--root-prefix", action="append", metavar="PREFIX",
                   help="only paths starting with this (repeatable), e.g. packs/")
    s.add_argument("--exclude", default=DEFAULT_EXCLUDE,
                   help=f"regex of paths to leave out (default: {DEFAULT_EXCLUDE!r})")
    s.add_argument("--max-mb", type=float, default=12, help="largest file to pick (MB)")
    s.add_argument("--seed", type=int, default=7, help="random seed for the pick")
    s.set_defaults(func=suggest)

    s = sub.add_parser("copy", help="copy the selected files into a demo library folder")
    s.add_argument("--source", help="source library root (read-only)")
    s.add_argument("--source-config", help="foley.toml whose [library] root is the source")
    s.add_argument("--selection", required=True, help="selection file")
    s.add_argument("--dest", required=True, help="demo library folder to create or update")
    s.add_argument("--max-mb", type=float, default=30, help="skip files larger than this (MB)")
    s.add_argument("--write-config", metavar="PATH", help="also write a demo foley.toml here")
    s.add_argument("--data-dir", help="data_dir for that config (default: <config dir>/data)")
    s.add_argument("--port", type=int, default=8793, help="[browse] port for that config")
    s.add_argument("--dry-run", action="store_true", help="report what would be copied")
    s.set_defaults(func=copy)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
