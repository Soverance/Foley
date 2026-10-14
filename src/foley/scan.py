"""Walk the library and read file-level metadata (pipeline stage 1).

CONTRACT:

    METADATA_VERSION: str

    @dataclass ScannedFile: rel_path (posix, relative to root), size_bytes, mtime_ns, ext (lower)
    @dataclass ScanResult: audio: list[ScannedFile], archives: list[ScannedFile],
                           errors: list[tuple[str, str]]   # (rel_path or dir, reason)

    scan_library(cfg) -> ScanResult
        os.scandir recursion (size and mtime come from the directory listing on
        Windows: no per-file stat over SMB). Audio = config.AUDIO_EXTENSIONS,
        archives = config.ARCHIVE_EXTENSIONS, matched case-insensitively. Everything
        else is ignored. Skips hidden/system entries and never descends into the
        quarantine folder. Unreadable directories become errors, not crashes.

    classify_path(cfg, rel_path) -> tuple[str | None, str | None]   # (pack, category_path)
        Under a pack root: packs/<pack>/<a>/<b>/f.wav -> ("<pack>", "<a>/<b>").
        Elsewhere: sfx/lightsabers/f.wav -> (None, "sfx/lightsabers").

    read_metadata(data: bytes, ext: str, rel_path: str) -> dict
        JSON-serializable. Keys present only when found:
          bext: {description, originator, origination_date, ...}  (WAV BWF chunk)
          ixml: {selected fields, e.g. note, category, subcategory, keywords}
          info: {RIFF LIST/INFO fields}
          tags: {ID3 / Vorbis / FLAC tags via mutagen}
          ucs:  parse_ucs(stem) result
        Never raises on malformed chunks; record {"warnings": [...]} instead.

    parse_ucs(stem: str) -> dict | None
        UCS filename "CatIDSubcat_FXName_CreatorID_SourceID" e.g.
        "GOREMisc_Cladding_NailScratch19_InMotionAudio_SinisterTextures5" ->
        {cat_id, category, subcategory, fx_name, creator_id, source_id}.
        Recognize CatIDs from the official UCS list (bundle it as data); None if no match.

    find_license(cfg, rel_path) -> str | None
        rel_path of the nearest ancestor-folder license file (LICENSE*, *licen[cs]e*.pdf/
        .txt/.md, "please read"), cached per folder; None if none up to the root.

    kind_hint(rel_path, metadata) -> 'music' | 'ambience' | 'sfx' | 'voice' | None
        From top folder, UCS category (AMB*, VOX*; MUSICAL only for its loop, song,
        stinger and performance CatIDs), and words like loop/_lp/amb.

Implementation notes (not contract):
    scan_library also records license-file candidates for every folder it lists, so
    find_license needs no extra directory listing during an index run. The cache is
    reset at the start of each scan_library call (or with clear_license_cache()).
"""

from __future__ import annotations

import io
import os
import re
import stat
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import PurePosixPath

from foley.config import ARCHIVE_EXTENSIONS, AUDIO_EXTENSIONS, Config

METADATA_VERSION = "1"

# Folder names that are never library content (NAS recycle bins, Synology/macOS metadata).
_SKIP_DIRS = frozenset(
    {"@eadir", "#recycle", "#snapshot", "$recycle.bin", "system volume information",
     ".appledouble", "__macosx"}
)
_HIDDEN_ATTRS = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 2) | getattr(
    stat, "FILE_ATTRIBUTE_SYSTEM", 4
)
_MAX_TEXT = 2000  # cap per metadata string so source_metadata stays small


@dataclass
class ScannedFile:
    rel_path: str
    size_bytes: int
    mtime_ns: int
    ext: str


@dataclass
class ScanResult:
    audio: list[ScannedFile] = field(default_factory=list)
    archives: list[ScannedFile] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)


# --------------------------------------------------------------------------- scanning


def _norm(path: str | os.PathLike) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _is_hidden(entry: os.DirEntry) -> bool:
    name = entry.name
    if name.startswith(".") or name.casefold() in _SKIP_DIRS:
        return True
    try:
        attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & _HIDDEN_ATTRS)


def scan_library(cfg: Config) -> ScanResult:
    result = ScanResult()
    root = os.fspath(cfg.library_root)
    quarantine = _norm(cfg.quarantine_root) if cfg.quarantine_root else None
    _LICENSE_CACHE.clear()
    root_key = _norm(root)

    stack: list[str] = [""]  # rel dirs, posix
    while stack:
        rel_dir = stack.pop()
        abs_dir = os.path.join(root, *rel_dir.split("/")) if rel_dir else root
        if quarantine and _norm(abs_dir) == quarantine:
            continue
        licenses: list[str] = []
        try:
            with os.scandir(abs_dir) as it:
                entries = list(it)
        except OSError as e:
            result.errors.append((rel_dir or ".", f"unreadable directory: {e}"))
            continue
        for entry in entries:
            try:
                if _is_hidden(entry):
                    continue
                rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                if entry.is_dir(follow_symlinks=False):
                    stack.append(rel)
                    continue
                if not entry.is_file():
                    continue
                ext = os.path.splitext(entry.name)[1].lower()
                if ext in AUDIO_EXTENSIONS or ext in ARCHIVE_EXTENSIONS:
                    st = entry.stat()
                    item = ScannedFile(rel, st.st_size, st.st_mtime_ns, ext)
                    (result.audio if ext in AUDIO_EXTENSIONS else result.archives).append(item)
                elif _license_rank(entry.name) is not None:
                    licenses.append(entry.name)
            except OSError as e:
                result.errors.append((f"{rel_dir}/{entry.name}" if rel_dir else entry.name,
                                      f"cannot stat: {e}"))
        _LICENSE_CACHE[(root_key, rel_dir)] = _best_license(rel_dir, licenses)

    result.audio.sort(key=lambda f: f.rel_path)
    result.archives.sort(key=lambda f: f.rel_path)
    return result


def classify_path(cfg: Config, rel_path: str) -> tuple[str | None, str | None]:
    dirs = rel_path.split("/")[:-1]
    folded = [d.casefold() for d in dirs]
    for pack_root in cfg.pack_roots:
        prefix = [p.casefold() for p in pack_root.replace("\\", "/").strip("/").split("/") if p]
        n = len(prefix)
        if prefix and len(dirs) > n and folded[:n] == prefix:
            return dirs[n], "/".join(dirs[n + 1:]) or None
    return None, "/".join(dirs) or None


# --------------------------------------------------------------------------- licenses

_LICENSE_CACHE: dict[tuple[str, str], str | None] = {}
_DOC_EXTS = frozenset({"", ".txt", ".md", ".pdf", ".rtf", ".htm", ".html", ".doc", ".docx"})
_LICENSE_WORD = re.compile(r"licen[cs]e")


def clear_license_cache() -> None:
    _LICENSE_CACHE.clear()


def _license_rank(name: str) -> int | None:
    """Lower is better; None means not a license file."""
    lower = name.casefold()
    stem, ext = os.path.splitext(lower)
    if ext not in _DOC_EXTS:
        return None
    if stem.startswith(("license", "licence")):
        return 0
    if _LICENSE_WORD.search(stem) and ext in {".pdf", ".txt", ".md", ".rtf", ".htm", ".html"}:
        return 1
    if "please read" in stem or "please_read" in stem or "pleaseread" in stem:
        return 2
    return None


def _best_license(rel_dir: str, names: list[str]) -> str | None:
    ranked = [(r, n) for n in names if (r := _license_rank(n)) is not None]
    if not ranked:
        return None
    name = min(ranked)[1]
    return f"{rel_dir}/{name}" if rel_dir else name


def _license_in_dir(cfg: Config, rel_dir: str) -> str | None:
    key = (_norm(cfg.library_root), rel_dir)
    if key in _LICENSE_CACHE:
        return _LICENSE_CACHE[key]
    abs_dir = cfg.abs_path(rel_dir) if rel_dir else cfg.library_root
    names: list[str] = []
    try:
        with os.scandir(abs_dir) as it:
            for entry in it:
                try:
                    if entry.is_file() and _license_rank(entry.name) is not None:
                        names.append(entry.name)
                except OSError:
                    continue
    except OSError:
        pass
    found = _best_license(rel_dir, names)
    _LICENSE_CACHE[key] = found
    return found


def find_license(cfg: Config, rel_path: str) -> str | None:
    parts = rel_path.split("/")[:-1]
    while True:
        found = _license_in_dir(cfg, "/".join(parts))
        if found:
            return found
        if not parts:
            return None
        parts.pop()


# --------------------------------------------------------------------------- UCS


@lru_cache(maxsize=1)
def _ucs_table() -> dict[str, tuple[str, str]]:
    """CatID -> (category, subcategory) from the bundled official UCS list."""
    table: dict[str, tuple[str, str]] = {}
    text = (resources.files("foley.data") / "ucs_catids.csv").read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        cells = _csv_cells(line)
        if len(cells) < 3 or cells[0] == "CatID":
            continue
        table[cells[0]] = (cells[1], cells[2])
    return table


def _csv_cells(line: str) -> list[str]:
    import csv

    return next(csv.reader([line]))


@lru_cache(maxsize=1)
def _ucs_folded() -> dict[str, str]:
    return {k.casefold(): k for k in _ucs_table()}


def parse_ucs(stem: str) -> dict | None:
    tokens = stem.split("_")
    if len(tokens) < 2:
        return None
    head = tokens[0]
    cat_token, _, user_category = head.partition("-")
    table = _ucs_table()
    cat_id = cat_token if cat_token in table else None
    if cat_id is None and cat_token[:2].isupper():
        cat_id = _ucs_folded().get(cat_token.casefold())
    if cat_id is None:
        return None
    category, subcategory = table[cat_id]
    rest = [t for t in tokens[1:]]
    creator_id = source_id = None
    if len(rest) >= 3:
        creator_id, source_id = rest[-2], rest[-1]
        fx_words = rest[:-2]
    elif len(rest) == 2:
        creator_id = rest[1]
        fx_words = rest[:1]
    else:
        fx_words = rest
    out = {
        "cat_id": cat_id,
        "category": category,
        "subcategory": subcategory,
        "fx_name": " ".join(w for w in fx_words if w) or None,
        "creator_id": creator_id or None,
        "source_id": source_id or None,
    }
    if user_category:
        out["user_category"] = user_category
    return out


# --------------------------------------------------------------------------- kind hint

_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_TOP_KIND = {
    "music": "music", "musics": "music", "bgm": "music", "songs": "music",
    "ambiance": "ambience", "ambience": "ambience", "ambiences": "ambience",
    "ambiances": "ambience", "amb": "ambience", "atmos": "ambience", "atmospheres": "ambience",
    "voice": "voice", "voices": "voice", "vo": "voice", "dialog": "voice", "dialogue": "voice",
    "sfx": "sfx", "fx": "sfx",
}
_UCS_MUSIC_CATIDS = frozenset({"MUSCLOOP", "MUSCSONG", "MUSCSTNGR", "MUSCPRFM"})
_MUSIC_WORDS = frozenset({"music", "song", "bgm", "theme", "stinger", "soundtrack", "ost"})
_AMB_WORDS = frozenset(
    {"amb", "ambience", "ambiance", "ambient", "ambiences", "atmos", "atmosphere",
     "roomtone", "bed", "loop", "lp", "loops"}
)
_VOICE_WORDS = frozenset({"vo", "voice", "vox", "dialog", "dialogue", "speech", "voiceover"})


def _words(text: str) -> list[str]:
    out: list[str] = []
    for chunk in re.split(r"[^A-Za-z0-9]+", text):
        out += [w.lower() for w in _CAMEL.findall(chunk)]
    return out


def kind_hint(rel_path: str, metadata: dict | None) -> str | None:
    parts = rel_path.split("/")
    if len(parts) > 1:
        top = _TOP_KIND.get(parts[0].casefold())
        if top:
            return top
    ucs = (metadata or {}).get("ucs") or parse_ucs(PurePosixPath(rel_path).stem)
    if ucs and ucs.get("cat_id"):
        cat = ucs["cat_id"].upper()
        if cat.startswith("AMB"):
            return "ambience"
        # UCS MUSICAL is mostly instrument one-shots (sfx); only these read as music,
        # where tempo and loop-seam analysis are useful.
        if cat in _UCS_MUSIC_CATIDS:
            return "music"
        if cat.startswith(("VOX", "VOI")):
            return "voice"
        return "sfx"
    words = set(_words(rel_path.rsplit(".", 1)[0]))
    if words & _MUSIC_WORDS:
        return "music"
    if words & _VOICE_WORDS:
        return "voice"
    if words & _AMB_WORDS:
        return "ambience"
    return None


# --------------------------------------------------------------------------- metadata


def _text(raw: bytes) -> str:
    raw = raw.split(b"\x00", 1)[0]
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        s = raw.decode("latin-1")
    return s.strip()[:_MAX_TEXT]


def _plausible_id(chunk_id: bytes) -> bool:
    return len(chunk_id) == 4 and all(0x20 <= b <= 0x7E for b in chunk_id)


_INFO_NAMES = {
    b"INAM": "title", b"IART": "artist", b"ICMT": "comment", b"ICOP": "copyright",
    b"ICRD": "date", b"IGNR": "genre", b"IKEY": "keywords", b"ISFT": "software",
    b"IPRD": "product", b"ISBJ": "subject", b"ISRC": "source", b"IENG": "engineer",
    b"ITCH": "technician", b"ICMS": "commissioned", b"IPRT": "part", b"ITRK": "track",
}

_IXML_FIELDS = {
    "NOTE", "PROJECT", "SCENE", "TAKE", "TAPE", "CATEGORY", "SUBCATEGORY", "CATID",
    "CATEGORYFULL", "FXNAME", "DESCRIPTION", "KEYWORDS", "LIBRARY", "DESIGNER",
    "RECORDIST", "MICROPHONE", "MANUFACTURER", "SHOW", "SOURCE", "VENDORCATEGORY",
    "USERCATEGORY", "LOCATION", "TRACKTITLE", "ARTIST", "COMPOSER", "LONGID", "SHORTID",
}


def _parse_bext(body: bytes, warnings: list[str]) -> dict:
    if len(body) < 346:
        warnings.append(f"bext chunk truncated ({len(body)} bytes)")
    out: dict = {}
    fields = (("description", 0, 256), ("originator", 256, 288),
              ("originator_reference", 288, 320), ("origination_date", 320, 330),
              ("origination_time", 330, 338))
    for name, a, b in fields:
        if len(body) > a:
            value = _text(body[a:b])
            if value:
                out[name] = value
    if len(body) >= 348:
        lo, hi, version = struct.unpack_from("<IIH", body, 338)
        out["time_reference"] = (hi << 32) | lo
        out["version"] = version
        if version >= 2 and len(body) >= 422:
            names = ("loudness_value", "loudness_range", "max_true_peak_level",
                     "max_momentary_loudness", "max_short_term_loudness")
            for i, name in enumerate(names):
                (v,) = struct.unpack_from("<h", body, 412 + 2 * i)
                if v not in (0x7FFF, -0x8000) and v != 0:
                    out[name] = v / 100.0
    if len(body) > 602:
        history = _text(body[602:])
        if history:
            out["coding_history"] = history
    return out


def _parse_ixml(body: bytes, warnings: list[str]) -> dict:
    raw = body.rstrip(b"\x00 \r\n\t")
    if not raw:
        return {}
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        # iXML never needs a DTD; refusing one rules out entity-expansion attacks
        # without adding a defusedxml dependency.
        warnings.append("iXML with DTD/entities ignored")
        return {}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        try:
            text = raw.decode("utf-8", errors="replace")
            text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
            root = ET.fromstring(text)
        except ET.ParseError as e:
            warnings.append(f"iXML not parseable: {e}")
            return {}
    out: dict = {}
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1].upper() if isinstance(el.tag, str) else ""
        if tag in _IXML_FIELDS and el.text and el.text.strip():
            key = tag.lower()
            if key not in out:
                out[key] = el.text.strip()[:_MAX_TEXT]
    return out


def riff_next_offset(read8, pos: int, size: int, total: int) -> int:
    """Offset of the chunk after the one at `pos` (header + `size` bytes of body).

    Odd-sized chunks should be followed by a pad byte, but some writers omit it. Take
    the padded offset unless only the unpadded one holds a plausible chunk header whose
    size fits in the file. `read8(offset)` returns the 8 bytes at an offset.
    """
    nxt = pos + 8 + size
    if not size & 1 or nxt + 1 >= total:
        return nxt + (size & 1)

    def fits(at: int) -> bool:
        head = read8(at)
        if len(head) < 8 or not _plausible_id(head[:4]):
            return False
        (sz,) = struct.unpack("<I", head[4:8])
        return head[:4] == b"data" or at + 8 + sz <= total

    if fits(nxt + 1) or not fits(nxt):
        return nxt + 1
    return nxt  # odd chunk written without its pad byte


def _parse_info(body: bytes, warnings: list[str]) -> dict:
    out: dict = {}
    pos = 4  # after the b"INFO" list type
    while pos + 8 <= len(body):
        sub_id = body[pos:pos + 4]
        (size,) = struct.unpack_from("<I", body, pos + 4)
        if not _plausible_id(sub_id):
            warnings.append("malformed LIST/INFO sub-chunk")
            break
        value = _text(body[pos + 8:pos + 8 + size])
        if value:
            out[_INFO_NAMES.get(sub_id, sub_id.decode("ascii").strip().lower())] = value
        pos = riff_next_offset(lambda at: body[at:at + 8], pos, size, len(body))
    return out


def _parse_riff(data: bytes, meta: dict, warnings: list[str]) -> None:
    magic = data[:4]
    if magic not in (b"RIFF", b"RF64", b"BW64") or len(data) < 12:
        warnings.append("not a RIFF file")
        return
    ds64_data_size: int | None = None
    pos = 12
    seen = 0
    while pos + 8 <= len(data) and seen < 10000:
        seen += 1
        chunk_id = data[pos:pos + 4]
        if not _plausible_id(chunk_id):
            warnings.append(f"garbage chunk id at offset {pos}; stopped")
            break
        (size,) = struct.unpack_from("<I", data, pos + 4)
        if chunk_id == b"ds64" and size >= 24 and pos + 8 + 24 <= len(data):
            _riff64, data64, _frames = struct.unpack_from("<QQQ", data, pos + 8)
            ds64_data_size = data64
        if size == 0xFFFFFFFF and chunk_id == b"data" and ds64_data_size is not None:
            size = ds64_data_size
        start, end = pos + 8, pos + 8 + size
        if end > len(data):
            if chunk_id != b"data":
                warnings.append(f"chunk {chunk_id.decode('ascii')!r} truncated")
            end = len(data)
        body = data[start:end] if chunk_id != b"data" else b""
        if chunk_id == b"bext":
            meta["bext"] = _parse_bext(body, warnings)
        elif chunk_id in (b"iXML", b"IXML"):
            ixml = _parse_ixml(body, warnings)
            if ixml:
                meta["ixml"] = ixml
        elif chunk_id == b"LIST" and body[:4] == b"INFO":
            info = _parse_info(body, warnings)
            if info:
                meta.setdefault("info", {}).update(info)
        elif chunk_id in (b"id3 ", b"ID3 ", b"ID32"):
            try:
                from mutagen.id3 import ID3

                tags = _tags_dict(ID3(io.BytesIO(body)))
                if tags:
                    meta.setdefault("tags", {}).update(tags)
            except Exception as e:  # noqa: BLE001 - mutagen raises many types
                warnings.append(f"id3 chunk not parseable: {type(e).__name__}")
        pos = riff_next_offset(lambda at: data[at:at + 8], pos, size, len(data))
    if 1 < len(data) - pos < 8:
        warnings.append(f"{len(data) - pos} stray bytes after the last chunk")
    if not meta.get("bext"):
        meta.pop("bext", None)


def _tags_dict(tags) -> dict:
    """Mutagen tag container -> {name: text}."""
    out: dict = {}
    if tags is None:
        return out
    try:
        from mutagen.id3 import ID3

        if isinstance(tags, ID3):
            names = {"TIT2": "title", "TPE1": "artist", "TALB": "album", "TCON": "genre",
                     "TDRC": "date", "TCOM": "composer", "TCOP": "copyright",
                     "TPUB": "publisher", "TIT1": "grouping", "TIT3": "subtitle"}
            for frame in tags.values():
                fid = frame.FrameID
                if fid in names:
                    out.setdefault(names[fid], "; ".join(str(t) for t in frame.text))
                elif fid == "COMM" and getattr(frame, "text", None):
                    out.setdefault("comment", "; ".join(str(t) for t in frame.text))
                elif fid == "TXXX" and getattr(frame, "text", None):
                    out.setdefault(f"txxx:{frame.desc}".lower(),
                                   "; ".join(str(t) for t in frame.text))
        else:
            for key, value in tags.items():
                if isinstance(value, list):
                    text = "; ".join(str(v) for v in value if isinstance(v, (str, int, float)))
                elif isinstance(value, (str, int, float)):
                    text = str(value)
                else:
                    continue
                if text:
                    out.setdefault(str(key).lower(), text)
    except Exception:  # noqa: BLE001
        return out
    return {k: v.strip()[:_MAX_TEXT] for k, v in out.items() if v and v.strip()}


def read_metadata(data: bytes, ext: str, rel_path: str) -> dict:
    meta: dict = {}
    warnings: list[str] = []
    ext = (ext or "").lower()
    try:
        if data[:4] in (b"RIFF", b"RF64", b"BW64"):
            _parse_riff(data, meta, warnings)
        elif ext in (".wav", ".wave"):
            warnings.append("WAV without RIFF header")
        else:
            try:
                import mutagen

                bio = io.BytesIO(data)
                bio.name = f"file{ext}"  # helps mutagen pick a format
                audio = mutagen.File(bio)
                if audio is not None:
                    tags = _tags_dict(audio.tags)
                    if tags:
                        meta["tags"] = tags
            except Exception as e:  # noqa: BLE001
                warnings.append(f"tags not readable: {type(e).__name__}: {e}"[:200])
    except Exception as e:  # noqa: BLE001 - never raise on malformed input
        warnings.append(f"metadata parse failed: {type(e).__name__}: {e}"[:200])
    try:
        ucs = parse_ucs(PurePosixPath(rel_path).stem)
        if ucs:
            meta["ucs"] = ucs
    except Exception as e:  # noqa: BLE001
        warnings.append(f"UCS parse failed: {e}"[:200])
    if warnings:
        meta["warnings"] = warnings
    return meta
