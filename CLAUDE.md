# Foley

Offline audio catalog + MCP server so Claude Code can search a sound library it cannot hear. The design is `docs/design.md`; read the relevant section before changing behaviour.

## Commands

- `uv sync` (core, no torch) or `uv sync --extra models` (adds torch + transformers for the tags/CLAP stages) · `uv run pytest -q` · `uv run ruff check src tests` · `uv run foley ...`
- The default test run needs neither torch nor model weights; `uv run pytest -m models` uses the real weights (needs the extra).
- On Windows, if uv or ffmpeg were installed with winget and a shell cannot find them, prepend `%LOCALAPPDATA%\Microsoft\WinGet\Packages\astral-sh.uv_*` and `...\Gyan.FFmpeg_*\ffmpeg-*\bin` to PATH.

## Rules

- **Never write to the audio library** (the configured `library.root`, often a shared network drive). Only `src/foley/nas_write.py` may modify it, and only for `foley dedupe apply/restore/purge` run by a human. `tests/test_readonly.py` enforces this.
- Both SQLite files are public contracts (other programs, such as a game engine's sound tool, read them directly). Schema changes go in a new numbered file under `src/foley/catalog/migrations/<catalog|ledger>/` and are documented in `docs/schema.md`. Never edit an applied migration.
- `catalog.db` is derived and rebuildable; `ledger.db` (decisions, usages, dedupe log) is not. Ledger rows key on `content_hash`.
- Keep the repo publishable (MIT): no site-specific values in code (everything via `foley.toml`), no audio, catalogs or model weights committed. Tests generate audio with `tests/audiogen.py`; the fixture library is in `tests/conftest.py`. Third-party code, fonts and data need an entry in `THIRD_PARTY_NOTICES.md`.
- torch and transformers are optional (the `models` extra): import them only inside the functions that load or run a model (see `foley.models`); `tests/test_without_models.py` checks that importing any module does not import torch.
- Each module's docstring is its contract. Change a contract only together with every caller.
- Code must run on Windows natively and on Linux (CI runs both): pathlib, no OS-specific APIs on the core path.
