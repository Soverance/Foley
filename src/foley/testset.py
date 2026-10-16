"""The search test set: a hand-written TOML file read by `foley eval` and edited by
`foley browse`'s test-set mode.

CONTRACT:

    Format (see eval/testset.example.toml): a header (any comment lines before the first
    `[[query]]`), then one `[[query]]` table per query:
        text = "..."            required, the query as typed into search
        cue = "..."             optional, informational (e.g. the game slot); ignored by eval
        good = ["rel/path", ...] library-relative paths (forward slashes) accepted as a good
                                answer; may be empty (eval skips the query)
        filters = { ... }       optional, the search_audio filter names (FILTER_TYPES)
    Any other key of a query is kept (round-tripped) and ignored.

    FILTER_TYPES: dict[str, type]          name -> float | bool | str
    MAX_ANSWERS = 3                        the suggested ceiling per query (not enforced)
    class TestsetError(ValueError)         bad file / bad value, with the query number

    @dataclass Query(text, good=[], cue=None, filters={}, extra={})
    @dataclass Testset(header, queries, path=None)
    parse(text, path=None) -> Testset
    load(path) -> Testset                  a missing file is an empty set (DEFAULT_HEADER)
    dumps(testset) -> str
        The header verbatim, then the queries in order with keys text, cue, good,
        filters, then the extras; `good` one path per line, filters an inline table.
        Strings are TOML basic strings (non-ASCII kept as UTF-8). parse(dumps(x)) == x.
    save(testset, path=None) -> Path       atomic: temp file in the same folder + replace
    search_filters(filters: dict) -> foley.search.SearchFilters

    Hand edits are welcome: the header block survives every rewrite, but comments
    between or inside `[[query]]` blocks do not (they are regenerated).
"""

from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

MAX_ANSWERS = 3
FILTER_TYPES: dict[str, type] = {
    "category": str,
    "pack": str,
    "is_music": bool,
    "min_duration_s": float,
    "max_duration_s": float,
    "min_lufs": float,
    "max_lufs": float,
    "min_attack_s": float,
    "max_attack_s": float,
}
DEFAULT_HEADER = (
    "# Foley search test set: the format `foley eval` reads (see eval/testset.example.toml).\n"
    "# For each query, list 1-3 library files (relative paths, forward slashes) you would\n"
    "# accept as a good answer. Queries with no good answers yet are skipped by foley eval.\n"
    "\n"
)
_KNOWN = ("text", "cue", "good", "filters")
_QUERY_LINE = re.compile(r"^[ \t]*\[\[[ \t]*query[ \t]*\]\]", re.MULTILINE)
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


class TestsetError(ValueError):
    pass


@dataclass
class Query:
    text: str
    good: list[str] = field(default_factory=list)
    cue: str | None = None
    filters: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)


@dataclass
class Testset:
    header: str = DEFAULT_HEADER
    queries: list[Query] = field(default_factory=list)
    path: Path | None = None
    # top-level keys outside the header (they would be lost on a rewrite)
    _foreign: tuple = ()


def check_filters(filters, where: str = "filters") -> dict:
    if filters is None:
        return {}
    if not isinstance(filters, dict):
        raise TestsetError(f"{where} must be a table like {{ max_duration_s = 1.0 }}")
    out = {}
    for key, value in filters.items():
        kind = FILTER_TYPES.get(key)
        if kind is None:
            raise TestsetError(
                f"{where}: unknown filter {key!r} (use {', '.join(FILTER_TYPES)})")
        if kind is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value):
                raise TestsetError(f"{where}: {key} must be a number, got {value!r}")
            value = float(value)
        elif not isinstance(value, kind):
            raise TestsetError(f"{where}: {key} must be a {kind.__name__}, got {value!r}")
        out[key] = value
    return out


def _query(raw, n: int) -> Query:
    where = f"query {n}"
    if not isinstance(raw, dict):
        raise TestsetError(f"{where} is not a table")
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        raise TestsetError(f"{where}: `text` must be a non-empty string")
    good = raw.get("good", [])
    if not isinstance(good, list) or not all(isinstance(g, str) for g in good):
        raise TestsetError(f"{where}: `good` must be a list of library-relative paths")
    cue = raw.get("cue")
    if cue is not None and not isinstance(cue, str):
        raise TestsetError(f"{where}: `cue` must be a string")
    filters = check_filters(raw.get("filters"), f"{where} filters")
    extra = {k: v for k, v in raw.items() if k not in _KNOWN}
    return Query(text, [g.replace("\\", "/").strip() for g in good], cue, filters, extra)


def parse(text: str, path: Path | None = None) -> Testset:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise TestsetError(f"{path or 'test set'} is not valid TOML: {e}") from e
    m = _QUERY_LINE.search(text)
    header = text[: m.start()] if m else text
    try:
        header_keys = set(tomllib.loads(header))
    except tomllib.TOMLDecodeError:
        header_keys = set()
    queries = data.get("query", [])
    if not isinstance(queries, list):
        raise TestsetError("`query` must be an array of tables ([[query]])")
    foreign = tuple(sorted(k for k in data if k != "query" and k not in header_keys))
    ts = Testset(header if header.strip() else DEFAULT_HEADER,
                 [_query(q, i + 1) for i, q in enumerate(queries)], path, foreign)
    return ts


def load(path: str | os.PathLike) -> Testset:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return Testset(DEFAULT_HEADER, [], path)
    return parse(text, path)


# --------------------------------------------------------------------------- writing

_ESCAPES = {"\\": "\\\\", '"': '\\"', "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f",
            "\r": "\\r"}


def _string(s: str) -> str:
    out = []
    for ch in s:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _key(k: str) -> str:
    return k if _BARE_KEY.fullmatch(k) else _string(k)


def _value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v):
            return "nan"
        if math.isinf(v):
            return "inf" if v > 0 else "-inf"
        text = repr(v)
        return text if any(c in text for c in ".eE") else text + ".0"
    if isinstance(v, str):
        return _string(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    if isinstance(v, dict):
        if not v:
            return "{}"
        return "{ " + ", ".join(f"{_key(k)} = {_value(x)}" for k, x in v.items()) + " }"
    if hasattr(v, "isoformat"):
        return v.isoformat()
    raise TestsetError(f"cannot write {v!r} to the test set")


def dumps(ts: Testset) -> str:
    if ts._foreign:
        raise TestsetError(
            "cannot rewrite the test set: it has top-level keys after the first [[query]] "
            f"({', '.join(ts._foreign)}); move them into the header or remove them")
    header = ts.header if ts.header.strip() else DEFAULT_HEADER
    if not header.endswith("\n"):
        header += "\n"
    if not header.endswith("\n\n"):
        header += "\n"
    blocks = []
    for q in ts.queries:
        lines = ["[[query]]", f"text = {_string(q.text)}"]
        if q.cue is not None:
            lines.append(f"cue = {_string(q.cue)}")
        if q.good:
            lines.append("good = [")
            lines += [f"    {_string(g)}," for g in q.good]
            lines.append("]")
        else:
            lines.append("good = []")
        if q.filters:
            lines.append(f"filters = {_value(check_filters(q.filters))}")
        for k, v in q.extra.items():
            lines.append(f"{_key(k)} = {_value(v)}")
        blocks.append("\n".join(lines) + "\n")
    return header + "\n".join(blocks)


def save(ts: Testset, path: str | os.PathLike | None = None) -> Path:
    target = Path(path) if path is not None else ts.path
    if target is None:
        raise TestsetError("no test set path")
    text = dumps(ts)
    parse(text)  # never write a file eval could not read back
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)
    ts.path = target
    return target


def search_filters(filters: dict):
    from foley.search import SearchFilters

    return SearchFilters(**check_filters(filters))
