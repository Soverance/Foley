# Contributing

Foley is shared as-is. It is a tool we use for our own work, published in case it is
useful to others; there is no support commitment, roadmap or release schedule. Issues
and pull requests are welcome, but they may go unanswered or be closed without a
detailed review. For anything large, open an issue first so you do not spend time on a
change that will not be merged.

## Development setup

You need [uv](https://docs.astral.sh/uv/) and Python 3.12 (uv can install it), plus
[ffmpeg](https://ffmpeg.org/) on `PATH` for the formats libsndfile cannot read.

```
uv sync                   # core install: no torch
uv sync --extra models    # add PyTorch + transformers for the tags and CLAP stages
```

## Tests and lint

```
uv run pytest -q
uv run ruff check src tests
```

- The default run needs neither model weights nor torch. Tests that need torch skip
  themselves when the `models` extra is not installed.
- `uv run pytest -m models` runs the tests against the real PANNs and CLAP weights
  (needs the extra; downloads about 2 GB on first use).
- Tests never use real audio: `tests/audiogen.py` generates it and `tests/conftest.py`
  builds the fixture library. Do not commit audio, catalogs or model weights.
- CI runs the suite on Ubuntu and Windows, so keep code portable: `pathlib`, no
  Windows-only or POSIX-only APIs on the core path.

## Rules worth knowing before you change anything

- **Foley never writes to the audio library.** Only `src/foley/nas_write.py` may modify
  it, and only for `foley dedupe apply/restore/purge` run by a person.
  `tests/test_readonly.py` enforces this; do not weaken it.
- **The SQLite files are a public contract.** Other programs read `catalog.db` and
  `ledger.db` directly. A schema change goes in a new numbered migration under
  `src/foley/catalog/migrations/<catalog|ledger>/` and is documented in
  `docs/schema.md`. Never edit a migration that has been released.
- `catalog.db` is derived and can be rebuilt; `ledger.db` (decisions, usages, dedupe
  log) cannot, so treat ledger changes with extra care.
- Each module's docstring is its contract. If you change a contract, update every
  caller in the same change.
- No site-specific values in code: paths, hosts and studio preferences belong in
  `foley.toml` (see `foley.toml.example`).

By contributing you agree that your contribution is licensed under the MIT License
(see `LICENSE`).
