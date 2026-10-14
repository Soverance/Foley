"""`foley-mcp`: the MCP server Claude Code talks to (stdio transport).

CONTRACT (Phase 1a tools; Phase 1b adds find_similar, Phase 2 the ledger tools):

    search_audio(query, category?, pack?, is_music?, min_duration_s?, max_duration_s?,
                 min_lufs?, max_lufs?, min_attack_s?, max_attack_s?, project?, limit=20)
        -> search.search(...) result
    describe(asset)                      asset id or library-relative path -> search.describe
    render_comparison(asset_ids)         2-6 ids -> {"image": absolute PNG path, "assets": [...]}
                                         (tool description tells Claude to Read the PNG)
    import_to_project(asset_id, project_root, dest, usage_key?, target_lufs?, overwrite=False)
        -> importer.import_to_project result
    catalog_status()                     -> status.catalog_status

    Results are compact JSON. Tool descriptions are written for Claude: say what each
    returns and when to use it. Config comes from foley.config.load() (FOLEY_CONFIG
    env or default locations). No tool may modify the library.

    main() runs the server over stdio.

DESIGN NOTES:

    Written against the official `mcp` SDK. mcp 2.x names the server class
    `mcp.server.mcpserver.MCPServer`; mcp 1.x names it
    `mcp.server.fastmcp.FastMCP`. Both are supported. Each tool is a plain function
    (`tool_*`) that raises ToolError for expected failures; `build_server()` registers
    them. The catalog connection is opened lazily on first use and reopened when
    catalog.db is replaced (rebuilt) or deleted. Tools are async so that they always
    run on the event-loop thread (sqlite connections are thread-bound).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from foley import config as config_mod
from foley.catalog import db

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.server.fastmcp.exceptions import ToolError

INSTRUCTIONS = (
    "Foley indexes a sound library you cannot hear. Search it by words, look at spectrogram "
    "images, then import the chosen file into the game project. Typical flow: search_audio, "
    "render_comparison on the top 4-6 candidates (then view the PNG with your Read tool), "
    "import_to_project. Durations are in seconds, loudness in LUFS, peaks in dBTP. "
    "No tool modifies the audio library."
)


class _State:
    cfg = None
    conn: sqlite3.Connection | None = None
    identity: tuple | None = None


_state = _State()


def set_config(cfg) -> None:
    """Use an explicit Config (tests); drops any open connection."""
    close()
    _state.cfg = cfg


def close() -> None:
    if _state.conn is not None:
        try:
            _state.conn.close()
        except sqlite3.Error:
            pass
    _state.conn = None
    _state.identity = None


def get_config():
    if _state.cfg is None:
        try:
            _state.cfg = config_mod.load(os.environ.get("FOLEY_CONFIG") or None)
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"Foley is not configured: {e}") from e
    return _state.cfg


def _identity(path: Path) -> tuple | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def get_conn() -> sqlite3.Connection:
    """The shared catalog connection; reopened if catalog.db was replaced."""
    cfg = get_config()
    identity = _identity(cfg.catalog_path)
    if _state.conn is not None and identity is not None and identity == _state.identity:
        return _state.conn
    close()
    try:
        _state.conn = db.open_catalog(cfg)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"cannot open the catalog: {e}") from e
    _state.identity = _identity(cfg.catalog_path)
    return _state.conn


def _fail(e: Exception) -> ToolError:
    if isinstance(e, ToolError):
        return e
    if isinstance(e, NotImplementedError):
        return ToolError("this feature is not available yet in this Foley build")
    if isinstance(e, sqlite3.Error):
        return ToolError(f"catalog database error: {e}")
    return ToolError(str(e) or e.__class__.__name__)


# ----------------------------------------------------------------------------- tools


def tool_search_audio(
    query: str,
    category: str | None = None,
    pack: str | None = None,
    is_music: bool | None = None,
    min_duration_s: float | None = None,
    max_duration_s: float | None = None,
    min_lufs: float | None = None,
    max_lufs: float | None = None,
    min_attack_s: float | None = None,
    max_attack_s: float | None = None,
    project: str | None = None,
    limit: int = 20,
) -> dict:
    from foley import search

    try:
        filters = search.SearchFilters(
            category=category,
            pack=pack,
            is_music=is_music,
            min_duration_s=min_duration_s,
            max_duration_s=max_duration_s,
            min_lufs=min_lufs,
            max_lufs=max_lufs,
            min_attack_s=min_attack_s,
            max_attack_s=max_attack_s,
        )
        return search.search(
            get_conn(), query, filters, project=project, limit=max(1, min(int(limit), 100))
        )
    except Exception as e:  # noqa: BLE001
        raise _fail(e) from e


def tool_describe(asset: int | str) -> dict:
    from foley import search

    try:
        return search.describe(get_conn(), asset)
    except Exception as e:  # noqa: BLE001
        raise _fail(e) from e


def tool_render_comparison(asset_ids: list[int]) -> dict:
    from foley import images

    try:
        ids = [int(i) for i in asset_ids]
        if not 2 <= len(ids) <= 6:
            raise ToolError("render_comparison needs between 2 and 6 asset ids")
        conn = get_conn()
        rows = []
        for i in ids:
            r = conn.execute(
                "SELECT a.id, a.rel_path, f.duration_s, f.lufs_integrated "
                "FROM assets a LEFT JOIN features f ON f.asset_id = a.id WHERE a.id = ?",
                (i,),
            ).fetchone()
            if r is None:
                raise ToolError(f"no such asset: {i}")
            rows.append(
                {
                    "id": r["id"],
                    "path": r["rel_path"],
                    "duration_s": r["duration_s"],
                    "lufs": r["lufs_integrated"],
                }
            )
        path = images.render_comparison(get_config(), conn, ids)
        return {"image": str(Path(path).resolve()), "assets": rows}
    except Exception as e:  # noqa: BLE001
        raise _fail(e) from e


def tool_import_to_project(
    asset_id: int,
    project_root: str,
    dest: str,
    usage_key: str | None = None,
    target_lufs: float | None = None,
    overwrite: bool = False,
) -> dict:
    from foley import importer

    try:
        return importer.import_to_project(
            get_config(),
            get_conn(),
            int(asset_id),
            Path(project_root),
            dest,
            usage_key=usage_key,
            target_lufs=target_lufs,
            overwrite=overwrite,
        )
    except Exception as e:  # noqa: BLE001
        raise _fail(e) from e


def tool_catalog_status() -> dict:
    from foley import status

    try:
        return status.catalog_status(get_config(), get_conn())
    except Exception as e:  # noqa: BLE001
        raise _fail(e) from e


# ----------------------------------------------------------------------------- server

SEARCH_DESCRIPTION = (
    "Search the sound library by keywords. Returns ranked candidates, each with id, "
    "library-relative path, score, a one-line caption, top 3 tags, duration_s (seconds), "
    "lufs (integrated LUFS; null for clips under 0.4 s), attack_s (seconds), license and "
    "`duplicates` (number of other exact copies, already collapsed out of the list). "
    "`note` warns when the catalog is stale. Search is KEYWORD-ONLY for now (file names, "
    "folders, UCS categories, metadata, tags, captions): use concrete words a file name might "
    "contain ('nail scratch', 'metal impact', 'ui confirm'), try synonyms, and combine with "
    "filters. Words are OR-ed, so files matching more of them rank higher. An empty query "
    "lists files matching the filters ordered by path. Filters: `category` (folder or UCS "
    "category prefix), `pack`, `is_music`, duration_s / LUFS / attack_s ranges. Pass `project` "
    "to hide files rejected for that project. Next steps: describe for one file, or "
    "render_comparison on the top 4-6 and view the image."
)
DESCRIBE_DESCRIPTION = (
    "Full record for one file: metadata, measured features (duration_s, LUFS, true peak in "
    "dBTP, attack_s, decay_s, spectral centroid, band energy), tags, caption, cached image "
    "path, exact duplicate copies, and past decisions and project usages. `asset` is the "
    "numeric id or the library-relative path. Use after search_audio when you need detail "
    "on a candidate; it is larger than a search result."
)
COMPARISON_DESCRIPTION = (
    "Render 2-6 candidate sounds into ONE grid PNG (waveform on top, log-mel spectrogram "
    "below, identical scales and a shared time axis so lengths and spectra compare "
    "visually). Returns {image: absolute PNG path, assets: [...]}. You cannot hear audio: "
    "view the image with your Read tool on the returned `image` path, then choose by "
    "duration, attack shape, brightness and noisiness. May take a few seconds the first "
    "time while images render."
)
IMPORT_DESCRIPTION = (
    "Copy a library file into a Godot project and record the usage. `dest` is a res:// "
    "path (for example res://audio/sfx/laser.wav) under `project_root`; it must keep the "
    "source file's extension and it refuses to overwrite unless overwrite=true. Loudness: "
    "pass target_lufs, or give `usage_key` (like sfx.ui.click) to use the project's "
    "foley.project.toml targets. Clips of 0.4 s or longer are normalized to integrated LUFS, "
    "shorter ones on true peak; the gain never pushes true peak above -1 dBTP (reported as "
    "`capped`). WAV/FLAC/AIFF are written normalized (gain_applied=true). MP3/OGG are copied "
    "unmodified: set Godot's volume_db to the returned `gain_db`. Never modifies the library."
)
STATUS_DESCRIPTION = (
    "Catalog health: asset counts by status, last scan time, pending files, archives not "
    "extracted, duplicate groups and reclaimable bytes, used-but-missing sources, model "
    "versions and files that failed analysis. Call it when search results look stale or empty."
)


def build_server():
    server = _Server("foley", instructions=INSTRUCTIONS)

    @server.tool(name="search_audio", description=SEARCH_DESCRIPTION)
    async def search_audio(
        query: str,
        category: str | None = None,
        pack: str | None = None,
        is_music: bool | None = None,
        min_duration_s: float | None = None,
        max_duration_s: float | None = None,
        min_lufs: float | None = None,
        max_lufs: float | None = None,
        min_attack_s: float | None = None,
        max_attack_s: float | None = None,
        project: str | None = None,
        limit: int = 20,
    ) -> dict:
        return tool_search_audio(
            query,
            category,
            pack,
            is_music,
            min_duration_s,
            max_duration_s,
            min_lufs,
            max_lufs,
            min_attack_s,
            max_attack_s,
            project,
            limit,
        )

    @server.tool(name="describe", description=DESCRIBE_DESCRIPTION)
    async def describe(asset: int | str) -> dict:
        return tool_describe(asset)

    @server.tool(name="render_comparison", description=COMPARISON_DESCRIPTION)
    async def render_comparison(asset_ids: list[int]) -> dict:
        return tool_render_comparison(asset_ids)

    @server.tool(name="import_to_project", description=IMPORT_DESCRIPTION)
    async def import_to_project(
        asset_id: int,
        project_root: str,
        dest: str,
        usage_key: str | None = None,
        target_lufs: float | None = None,
        overwrite: bool = False,
    ) -> dict:
        return tool_import_to_project(
            asset_id, project_root, dest, usage_key, target_lufs, overwrite
        )

    @server.tool(name="catalog_status", description=STATUS_DESCRIPTION)
    async def catalog_status() -> dict:
        return tool_catalog_status()

    return server


def main() -> None:
    build_server().run("stdio")


if __name__ == "__main__":
    main()
