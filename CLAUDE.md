# Foley

Offline audio catalog + MCP server so Claude Code can search a sound library it cannot hear. The spec is `docs/handoffs/AudioCatalog.md`; read the section relevant to your work before changing behavior.

## Commands

- `uv sync` · `uv run pytest -q` · `uv run ruff check src tests` · `uv run foley ...`
- On the Soverance dev machine, uv and ffmpeg were installed by winget; if a shell cannot find them, prepend `%LOCALAPPDATA%\Microsoft\WinGet\Packages\astral-sh.uv_*` and `...\Gyan.FFmpeg_*\ffmpeg-*\bin` to PATH.

## Rules

- **Never write to the audio library** (configured `library.root`; the studio NAS share). Only `src/foley/nas_write.py` may modify it, and only for `foley dedupe apply/restore/purge` run by a human. `tests/test_readonly.py` enforces this.
- Both SQLite files are public contracts (the Godot Sound Board reads them from C#). Schema changes go in a new numbered file under `src/foley/catalog/migrations/<catalog|ledger>/` and are documented in `docs/schema.md`. Never edit an applied migration.
- `catalog.db` is derived and rebuildable; `ledger.db` (decisions, usages, dedupe log) is not. Ledger rows key on `content_hash`.
- Keep the repo publishable (MIT): no studio-specific values in code (everything via `foley.toml`), no audio, catalogs or model weights committed. Tests generate audio with `tests/audiogen.py`; the fixture library is in `tests/conftest.py`.
- Each module's docstring is its contract. Change a contract only together with every caller.
- Code must run on Windows natively and stay portable (pathlib, no Windows-only APIs on the core path).
