# Foley — Claude Code Handoff Spec

Oct 14, 2026 · @Scott · revised Oct 14, 2026 after a library survey and design review

## Purpose and goals

**Foley** is a standalone Soverance Studios tool that lets Claude Code search, understand and pick audio from the studio asset library without a human reviewing every file. It is named for Foley, the film craft of creating everyday sound effects. Repo: `soverance/foley` (private for now; intended to be open-sourced later, see [Open source readiness](#open-source-readiness)).

**Problem.** Claude Code cannot hear. Today it picks audio from filenames, and Scott reviews each pick by hand in the Godot Sound Board. That works but does not scale to the tens of thousands of SFX and music files the library is growing to.

**Approach.** Analyze the whole library once, offline, keep that analysis in sync as the library changes, and store what each file sounds like as data Claude Code can query:

- **Semantic:** text-searchable embeddings (CLAP), AudioSet tags, and a one-line caption per file
- **Technical:** loudness, timing, envelope and spectral features
- **Visual:** a spectrogram + waveform PNG per file, which Claude can view with its vision

**Success looks like:**

- Claude Code finds good candidates from a text description in one tool call
- Low-stakes sounds (UI, pickups, ambient variations) are picked and loudness-normalized with no manual review
- Scott reviews only hero sounds and anything flagged low-confidence: dozens of reviews, not hundreds
- The catalog stays in sync as files are added, moved or removed, without a full re-index
- Duplicate files in the library can be found and safely removed to reclaim NAS storage
- The same catalog serves Endless Reach, the tactics project and later games

**Non-goals:** generating or editing audio, mixing, replacing Scott's ear on hero sounds, shipping any of this inside a game build, and turning Foley into a supported product. If open-sourced, it ships as-is.

## Architecture

A Python indexer and MCP server write and serve a local catalog; the Godot Sound Board reads the same catalog from C#, and nothing else crosses the language boundary.

&#91;embedded content: Foley architecture · 6 parts around one catalog\]

The catalog is two SQLite files plus a PNG cache, and they are the only interface between the two halves. Neither side calls the other's code.

- **`catalog.db`** holds everything *derived* from the library: assets, features, tags, captions, embeddings. It can be deleted and rebuilt from the NAS at any time.
- **`ledger.db`** holds everything that *cannot* be regenerated: Scott's decisions, project usages, and the log of every change Foley has made to the NAS. It is backed up, never rebuilt, and never overwritten by `foley pull`.

**Suggested repo layout**

```
foley/
  pyproject.toml, uv.lock, foley.toml.example, LICENSE (MIT)
  src/foley/
    scan.py, sync.py, decode.py, features.py, loudness.py, images.py
    dedupe.py, nas_write.py
    models/   (clap.py, panns.py)
    catalog/  (schema.sql, ledger.sql, migrations/, db.py)
    search.py, cli.py, mcp_server.py
  eval/       (eval.py, testset.example.toml)
  docs/       (schema.md, handoffs/)
  tests/      (fixtures generated at test time, no real audio)
```

`nas_write.py` is the **only** module allowed to modify the library (see [Duplicate cleanup](#duplicate-cleanup)). Everything else opens library files read-only, and a test enforces this.

## Environment and storage

Foley runs Windows-native, managed by `uv`; the audio library stays on the NAS and the catalog lives on local disk.

**Runtime**

- Python 3.12 project managed with [uv](https://docs.astral.sh/uv/) (`winget install astral-sh.uv`). Scott never touches pip or virtual environments directly.
- Entry points: `foley` (CLI) and `foley-mcp` (MCP server). Installable with `uv tool install` so `foley.exe` is on PATH.
- PyTorch with CUDA for the model pass, on the dev machine's RTX 2070 Super (8 GB VRAM: ample for CLAP and PANNs inference). CPU fallback must work, just slower. The CUDA wheel index is configured in `pyproject.toml`.
- WSL is not required. It is reserved for an optional later phase (Essentia music descriptors, which lack native Windows builds) that writes into the same catalog.
- Keep the code portable (pathlib, no Windows-only APIs on the core path) so it also runs on Linux and macOS for other developers.

**Source library**

- Root: `\\data.soverance.net\Files\Assets\Audio`, set in a `foley.toml` config, never hard-coded.
- Read-only for every command except the explicit `foley dedupe` actions. Foley never renames, moves or edits library files otherwise.
- Store every path **relative to the library root**, so the catalog works from any machine whether the share is reached by UNC path or a mapped drive.
- Formats: WAV, OGG, FLAC, MP3, matched case-insensitively (`.WAV` exists in the library). Decode via soundfile, falling back to ffmpeg. Non-audio files (license PDFs, readmes, editor sidecars such as `.gpk` and `.mdd`) are ignored, except archives, which are reported (see sync rules).
- **Size as of Oct 14, 2026:** 2,256 audio files, 18.7 GB, in `ambiance/`, `music/`, `packs/`, `sfx/` and `voice/`. Pack filenames are descriptive, and Sonniss 2026 uses UCS naming. The studio's own files in `sfx/` are tersely named (`lasrhit3.WAV`).
- **Incoming:** Sonniss GDC bundles 2016–2024, about 225 GB zipped. Expect roughly 25–40k files and about 250 GB once extracted. Design and test for that scale.
- Licensing: the library README states every file is licensed for commercial use. Packs carry their own license files, so license is recorded per pack folder.

**Catalog location**

- `catalog.db`, `ledger.db` and the image cache live on **local disk** (default `%LOCALAPPDATA%\Foley\`, configurable). SQLite over an SMB share risks locking failures and corruption.
- Other dev machines get a copy: `foley publish` writes a read-only snapshot of `catalog.db` to a configured NAS folder, and `foley pull` copies it down. `ledger.db` is never replaced by a pull; it lives on the master machine and is backed up.

## Library sync

The library is a living folder: Scott adds packs, removes files and reorganizes. `foley index` is incremental and idempotent, and it is the only sync mechanism. Running it is always safe.

**How a run works**

1. List the tree with `os.scandir`. On Windows, size and modified time come back with the directory listing, so checking 40k files over SMB takes about a minute and opens nothing.
2. Compare each file's relative path, size and modified time against `catalog.db`.
3. Analyze only what changed, decoding each file from the NAS once.
4. Commit after each file, so search keeps working during a long run and a crash loses at most one file.

**Rules**

| Change on the NAS | What Foley does |
| --- | --- |
| New file | Analyze it |
| Size or modified time changed | Re-analyze it, keeping the same asset id |
| Path no longer exists | Set `status = missing` and keep the row, so decisions and usages stay valid |
| Moved or renamed (new path whose content hash matches a `missing` asset) | Re-point the existing asset to the new path; keep its analysis and decisions |
| Still being written (modified within the last 10 minutes, configurable) | Skip it until a later run. Packs are extracted onto the share in bulk |
| Archive (`.zip`, `.rar`, `.7z`) | Do not index; count it under "archives not extracted" in `catalog_status` |
| Decode failure | Set `status = error` with the reason; retry on later runs instead of failing permanently |

**Housekeeping**

- `foley prune` removes catalog rows (and cached images) for assets that have been `missing` longer than a configured period **and** have no ledger entries. It touches only local files, never the NAS.
- If a file a game already uses disappears from the library, the game is unaffected (`import_to_project` made a copy). `catalog_status` warns that the library source for that usage is gone.
- Only one indexer runs at a time, guarded by a lock file. The MCP server and Sound Board keep reading through WAL mode while it runs.
- Each run is recorded in `index_runs`, which drives resuming an interrupted run and the staleness report.

**When it runs**

- **On demand:** `foley index` after adding packs.
- **Daily, from the browse server:** the master machine runs `foley browse` permanently (started at logon), and its built-in scheduler runs the scan. It is set in the browser's Scans dialog (on/off, a local time such as 03:00, catch-up) with defaults from `[schedule]` in `foley.toml`; the dialog's settings are saved to `schedule.json` in the data folder. Each scan is an ordinary `foley index` subprocess, so it behaves exactly like a manual run: incremental, resumable, read-only on the library. Its output goes to `logs/scan-*.log` in the data folder (the newest 30 are kept) and its outcome (`ok`, `failed`, `skipped`, `stopped`) to `logs/scans.json`. The dialog shows the live log, the history with each run's counts, and Scan now / Stop; stopping kills the whole process tree and is safe because the next run resumes. Only one scan runs at a time: if a CLI `foley index` holds the lock at the scheduled time, the scan is recorded as skipped and the next day's slot tries again.
- **Catch-up:** if the server was off at the scheduled time (machine asleep or shut down), it scans a few minutes after starting (`startup_delay_minutes`, default 5, so the network and NAS are up after logon) whenever the last completed scan is older than the most recent scheduled time. Times are the server's local wall clock, so the slot stays at 03:00 across daylight-saving changes.
- **Staleness is visible:** `catalog_status` and every `search_audio` response report when the library was last scanned and how many files are pending or missing, so Claude Code can say the catalog is stale instead of silently searching an old index.
- No live file watching. Watching an SMB share drops events during bulk copies and does not survive disconnects, so the daily scan would still be needed as the backstop.

## Duplicate cleanup

Sonniss bundles and other packs repeat content, and duplicates waste NAS storage and clutter search. Foley finds duplicates as a side effect of indexing and offers a deliberate, reversible cleanup.

**Deleting from the NAS is the most dangerous thing Foley can do. Every rule below exists to make a mistake recoverable.**

**Detection** (read-only, runs during indexing)

- **Exact duplicates:** identical `content_hash` (xxHash3-128 over the file bytes). These are the only files cleanup may act on.
- **Audio-identical:** identical `audio_hash` (a hash of the decoded PCM samples) but different bytes, usually the same sound with different embedded metadata. Report only.
- **Near-duplicates:** CLAP similarity above a high threshold plus matching duration. Report only, never acted on.
- `search_audio` collapses each exact-duplicate group to one result.

**Commands**

| Command | Effect on the NAS |
| --- | --- |
| `foley dedupe report` | None. Lists duplicate groups, the copy that would be kept, and the bytes reclaimable |
| `foley dedupe plan` | None. Writes a plan file naming every file to remove and every survivor, for Scott to read |
| `foley dedupe apply <plan>` | Moves the planned files to quarantine |
| `foley dedupe restore <plan or files>` | Moves quarantined files back to their original paths |
| `foley dedupe purge --older-than <days>` | Permanently deletes quarantined files past the retention period |

**Safety rules**

- **Nothing is deleted directly.** `apply` *moves* files to a quarantine folder that mirrors their relative paths, such as `\\data.soverance.net\Files\Assets\Audio-Quarantine\`. It must be on the same share (so a move is a rename and copies nothing) and **outside** the library root (so it is never indexed). Foley refuses to run if either condition fails.
- **Only `purge` deletes**, only from quarantine, only after a retention period (default 30 days), and only with an explicit confirmation.
- **A dry run is the default.** `apply` without a reviewed plan file does nothing. `apply` shows a summary (file count, GB) and requires typing the count to confirm.
- **Re-verify before every move.** At apply time each file's size and modified time must still match the plan, and it must match its survivor byte for byte (not just by hash). Any mismatch skips that group and reports why. A plan older than the last index run is rejected.
- **Never remove the last copy.** Every group keeps exactly one survivor, which must exist and be readable at apply time.
- **Choose survivors predictably.** In order: the copy with ledger entries (decisions or usages); then the copy in the pack ranked highest in `dedupe.keep_priority` in `foley.toml`; then the oldest `first_seen`; then the shortest path.
- **Log everything.** Every move, restore and purge writes a row to `dedupe_actions` in `ledger.db` (path, hash, size, plan id, timestamp, user). That log is what `restore` reads.
- **The CLI only, by a human.** No MCP tool can move or delete library files, so Claude Code can report duplicates but never remove them.
- **Tested off the NAS.** The dedupe code is tested against a generated local fixture library before it ever touches the NAS.

## Catalog schema

`catalog.db` and `ledger.db` are the contract between Foley and every consumer, including the Godot Sound Board. Treat both schemas as a public API: version them, migrate them, and document them in `docs/schema.md`. Never change them silently.

**`catalog.db`** (derived, rebuildable)

| Table | Key columns | Purpose |
| --- | --- | --- |
| `assets` | `id`, `rel_path`, `pack`, `category_path`, `ext`, `size_bytes`, `mtime`, `content_hash`, `audio_hash`, `license`, `source_metadata` (JSON), `first_seen`, `last_seen`, `indexed_at`, `status` | One row per audio file. `status` is `active`, `pending`, `missing` or `error`. `pack` and `category_path` come from folder names; `source_metadata` holds BWF/iXML/ID3 fields |
| `features` | `asset_id`, `duration_s`, `sample_rate`, `channels`, `lufs_integrated`, `lufs_short_max`, `true_peak_db`, `lead_silence_s`, `tail_silence_s`, `attack_s`, `decay_s`, `onset_count`, `centroid_hz_mean`, `rolloff_hz_mean`, `flatness_mean`, `zcr_mean`, `band_energy` (JSON), `tempo_bpm`, `loop_seam_score`, `is_music` | Objective DSP measurements |
| `tags` | `asset_id`, `label`, `score`, `model` | AudioSet labels above a threshold, from PANNs |
| `captions` | `asset_id`, `text`, `model` | One-sentence description per file |
| `embeddings` | `asset_id`, `model`, `dim`, `vector` (float32 BLOB, L2-normalized) | CLAP audio embedding for text search and find-similar |
| `search_fts` | FTS5 over filename, pack, category, metadata, tags, caption | Keyword half of hybrid search |
| `images` | `asset_id`, `kind`, `local_path`, `rendered_at` | Cache of rendered spectrogram + waveform PNGs |
| `index_runs` | `id`, `started_at`, `finished_at`, `stage`, `scanned`, `added`, `changed`, `moved`, `missing`, `errors` | Sync history, resume point and staleness |
| `meta` | `schema_version`, `models` (JSON), `library_root` | Versioning and provenance |

**`ledger.db`** (human decisions and NAS actions, never rebuilt)

| Table | Key columns | Purpose |
| --- | --- | --- |
| `decisions` | `id`, `content_hash`, `rel_path`, `project`, `usage_key`, `verdict`, `gain_db`, `note`, `decided_by`, `decided_at` | Approvals, rejections and level changes from the Sound Board or Claude Code |
| `usages` | `content_hash`, `rel_path`, `project`, `project_path`, `imported_at`, `normalized_to_lufs`, `gain_db`, `license` | Which game uses which file, where it was copied to, and its provenance |
| `dedupe_actions` | `id`, `plan_id`, `action`, `content_hash`, `rel_path`, `quarantine_path`, `size_bytes`, `acted_at`, `acted_by` | Audit log of every move, restore and purge on the NAS |
| `meta` | `schema_version` | Versioning |

- Ledger rows key on **`content_hash`**, not `asset_id`, so they survive moves, renames, duplicate cleanup and a full catalog rebuild. `rel_path` is kept for readability. Consumers join the two files with SQLite `ATTACH`. If a file's bytes change, its old decisions no longer attach. That is intended, since it is a different sound now.
- No sqlite-vec. Even 40k 512-dimension vectors are about 80 MB, and brute-force cosine over them takes milliseconds in both numpy and C#. This keeps native SQLite extensions out of both languages. Revisit only past roughly 500k files.
- `verdict` values: `approved`, `rejected`, `replaced`, `auto` (picked by Claude Code without review).
- `usage_key` is the game-side slot, such as `sfx.weapon.laser.fire` or `music.stage2.loop`. Decisions are judged per slot, not globally: a file can be wrong for one slot and right for another.
- Every model-derived row records its `model` (name + version) so a model upgrade can re-run just that stage.

## Analysis pipeline

Each file passes through seven stages in order; each stage is independently re-runnable (`foley index --stage tags`) so a model upgrade does not force a full re-index.

1. **Scan and metadata.** Walk the library root (see [Library sync](#library-sync)), record path, pack and category from folder names, and read embedded BWF/iXML/ID3 metadata. Parse UCS-style filenames (`CATsub_Description_Creator`) where present. This is free semantic signal: do it first and do it well. With these pack filenames it carries a lot of the search.
2. **Decode.** Load each file once at native rate and compute `content_hash` and `audio_hash` from that read. Derive the mono and resampled copies each later stage needs (PANNs expects 32 kHz, CLAP 48 kHz) from that one decode. Long high-rate files (multi-minute 96 kHz ambiences) must be handled without exhausting RAM.
3. **DSP features.** librosa with `n_fft=2048`, `hop=512`. Definitions to implement exactly:
   - Leading/tail silence: time before/after the signal first/last exceeds −60 dBFS
   - Attack: time from the envelope reaching 10% of peak to peak
   - Decay: time from peak to 40 dB below peak
   - Band energy: share of energy in sub (<60 Hz), low (60–250), low-mid (250–500), mid (500–2k), upper-mid (2k–4k), presence (4k–6k), air (>6k)
   - Loop seam score (music and ambience only): sample discontinuity plus spectral distance between the last and first 50 ms; lower is a cleaner loop
   - Tempo (music only) via librosa beat tracking
4. **Loudness.** pyloudnorm (ITU-R BS.1770) for integrated LUFS, max short-term loudness and true peak. Short one-shots under 0.4 s have no valid integrated LUFS: store null and rely on short-term and peak values.
5. **Images.** Rendered **on demand** by `describe` and `render_comparison`, then cached (pre-rendering every file would cost 4–8 GB at the full library size; `foley index --stage images` pre-renders if wanted). One PNG per file, 1024×512: waveform with RMS envelope on top, log-mel spectrogram below (128 mel bands, 20 Hz to Nyquist, fixed −80 to 0 dB range, one fixed colormap). Duration, LUFS and peak in the header. **Fixed scales are mandatory** so images are comparable side by side.
6. **Tags.** PANNs CNN14 on AudioSet; store every label scoring ≥ 0.1. Set `is_music` from the Music tag score.
7. **Embeddings.** LAION-CLAP audio embedding per file, normalized. Load checkpoints through Hugging Face `transformers` (`ClapModel`) rather than the `laion_clap` package, whose version pins are awkward on Windows. CLAP sees about 10 s at a time, so longer files are embedded in 10 s windows (evenly spaced, capped at 12 windows) and mean-pooled into one vector. Claude Code benchmarks two or three public CLAP checkpoints on the test set (Phase 1 acceptance) and keeps the best.

**Captions** are split across phases. Phase 1 generates a templated summary line from tags, features and filename ("0.42 s bright metallic impact, sharp attack, short tail, −14 LUFS"). An audio-captioning model is evaluated in Phase 3, and kept only if it beats the template on the test set. Large audio-language models may not fit in 8 GB VRAM without quantization.

## MCP tool surface

Claude Code reaches Foley through nine MCP tools. Results stay compact: ids, paths, a caption, top tags and a handful of key numbers, never raw feature arrays, so a search costs few tokens. **No MCP tool modifies the NAS library.** `import_to_project` writes only into a game project.

| Tool | Inputs | Returns |
| --- | --- | --- |
| `search_audio` | `query` (text), `filters` (category, pack, `is_music`, duration / LUFS / attack ranges), `project` (to hide files rejected for it), `limit` (default 20) | Ranked candidates: id, path, similarity, caption, top 3 tags, duration, LUFS, attack. Exact duplicates are collapsed. Header reports catalog staleness |
| `find_similar` | `asset_id`, `filters`, `limit` | Nearest neighbors by CLAP embedding |
| `describe` | `asset_id` | Full record: features, tags, caption, image path, decision history, duplicate copies |
| `render_comparison` | `asset_ids` (2–6) | Path to one grid PNG of their images at identical scales; Claude Code views it with Read |
| `check_mix_fit` | `sfx_id`, `music_id` | Per-band overlap scores and a one-line verdict on masking risk |
| `get_examples` | `project`, `usage_key` or category | Approved and rejected files for that slot, with Scott's notes, as few-shot context |
| `import_to_project` | `asset_id`, `project_root`, `dest` (path under `res://`), `usage_key`, `target_lufs` (optional) | Copies the file into the Godot project, optionally loudness-normalized, and records the usage. Refuses to overwrite unless `overwrite=true` |
| `record_decision` | `asset_id`, `project`, `usage_key`, `verdict`, `note` | Logs an `auto` pick or other verdict from Claude Code |
| `catalog_status` | none | Asset counts by status, last scan time, pending files, archives not extracted, duplicate groups and reclaimable GB, used-but-missing sources, model versions, files that failed to analyze |

**Search** is hybrid: CLAP cosine similarity blended with FTS5 keyword rank. The blend weight is tuned on the test set, and `foley eval` always reports a keyword-only baseline next to the hybrid score so CLAP has to earn its place.

**Per-project loudness targets** live in a `foley.project.toml` in each game repo (for example UI −20 LUFS, weapons −14, music −16, plus a `oneshot_peak_db` target and the list of hero slots). `import_to_project` reads it when `target_lufs` is omitted. Normalization rules:

- Clips 0.4 s or longer are normalized to integrated LUFS; shorter clips are normalized on true peak to `oneshot_peak_db`.
- Lossless sources (WAV, FLAC) are written normalized.
- Lossy sources (MP3, OGG) are copied unmodified to avoid a second lossy encode. The computed gain is recorded in `usages.gain_db` and applied in Godot via `volume_db`.

**Claude Code registration**, in the game repo's `.mcp.json` or user scope:

```json
{"mcpServers": {"foley": {"command": "foley-mcp"}}}
```

**Expected selection workflow**, to be written into each game repo's CLAUDE.md:

1. `get_examples` for the slot or category
2. `search_audio` with a descriptive query and technical filters
3. `render_comparison` on the top 4–6 and view the image
4. `check_mix_fit` against the music for that stage, if one exists
5. `import_to_project`, then `record_decision` with verdict `auto`, or flag for review if confidence is low or the slot is a hero sound

## Godot Sound Board integration

The Sound Board stays C# and becomes a review and calibration view over the shared catalog; it never calls Python.

**Reading and writing the catalog**

- A small data-access class in the Sound Board using `Microsoft.Data.Sqlite`. No shared library is needed yet.
- Read from `catalog.db`: assets, features, tags, captions, embeddings, images. Read from `ledger.db`: decisions, usages.
- Write: the `decisions` table in `ledger.db` only.
- Both files run in SQLite WAL mode so the Sound Board, the MCP server and the indexer can be open at the same time.

**Changes to the Sound Board**

- A **Needs review** queue: files Claude Code imported and flagged, plus `auto` picks Scott has not yet seen, grouped by `usage_key`.
- Show Foley's spectrogram PNG and caption beside the existing waveform analyzer, plus the top tags and key numbers.
- Approve, reject, replace and gain changes each write a `decisions` row, with an optional one-line note on *why*. The notes are the most valuable training signal: they become the few-shot examples `get_examples` returns.
- A replace action finds similar files to the rejected one by brute-force cosine similarity over the `embeddings` BLOBs in C# (about 20 lines, no extension), so Scott picks a substitute without leaving the board.

**The feedback loop**

- **Phase 2:** Claude Code reads past decisions through `get_examples` before choosing. This alone lets it learn Scott's taste per slot.
- **Phase 3:** once a category has roughly 30 or more decisions, train a small logistic-regression model per category on CLAP embeddings plus features, and expose its output as `predicted_approval` in search results. Average gain changes per category also suggest corrections to the loudness targets.
- Low `predicted_approval`, or any slot marked as a hero sound in `foley.project.toml`, always routes to the review queue.

## Open source readiness

The repo is private today, but Foley should be publishable later with little cleanup, as a useful tool for other game developers and a portfolio piece for soverance.com. It is not a product: no support commitment, no release cadence, and a README that says so. Build it publishable from the first commit:

- **No studio specifics in code.** Library root, quarantine path, pack priorities, project names and loudness targets all come from `foley.toml` / `foley.project.toml`. Ship `.example` versions with neutral values.
- **No audio, catalogs or weights in the repo.** Tests generate their fixtures (tones, noise bursts, clicks, silence padding, duplicate and moved files) at test time. Model weights download at first run from their official sources.
- **The test set stays private.** It names files in Scott's library and reflects Endless Reach taste. It lives at a path set in `foley.toml`; the repo ships `eval/testset.example.toml` showing the format.
- **Dependency licenses stay permissive.** librosa (ISC), pyloudnorm (MIT), soundfile (BSD), PyTorch (BSD) and transformers (Apache-2.0) are fine. ffmpeg is called as an external binary, never bundled. **Essentia is AGPL-3.0**: the optional Phase 3 Essentia stage must be a separate, optional process that writes into the catalog, never imported by the core package. Record the license of each model checkpoint used, and prefer checkpoints whose weights allow redistribution of derived data.
- **The Sound Board is not part of Foley.** It stays in its own repo. The public contract is `docs/schema.md` plus the MCP tools, so anyone can build their own front end.
- **History is fine as-is.** This brief names the internal NAS host and Endless Reach dates. That is acceptable: the studio network is not reachable from the public internet. The brief can be removed once it is no longer needed.
- **License: MIT.** `LICENSE` is committed from the start.

## Phased plan and acceptance criteria

Only Phase 1 is meant to land before the Endless Reach launch on November 12. If it is not done and validated by **October 29** (about two weeks before launch), Endless Reach finishes with the current workflow, and Foley continues after launch.

Development and testing run against the current 2,256-file library. The full index of the expanded library (Sonniss 2016–2024) runs once the pipeline passes acceptance on it.

### Phase 1a: catalog and keyword search, no ML (first)

Scope: repo scaffold with uv, config, `catalog.db` and `ledger.db` with schema versioning and migrations, library sync, pipeline stages 1–5, FTS5 search, templated captions from metadata and features, CLI (`foley index`, `foley search`, `foley describe`, `foley status`, `foley prune`, `foley dedupe report`), and the MCP tools `search_audio`, `describe`, `render_comparison`, `import_to_project`, `catalog_status`. No PyTorch dependency yet.

- [x] Full library indexes from the NAS share; an interrupted run resumes without redoing finished files *(Oct 14: 2,256 files in 22 min, 0 errors; a 40-file trial run was resumed by the full run)*
- [x] Re-running with no library changes skips every file *(Oct 14: 7,917 files scanned in 3.4 s, 0 analyzed)*
- [x] On a generated fixture library, sync correctly handles added, changed, moved, deleted, still-being-written and undecodable files, and archives
- [x] LUFS values agree with ffmpeg's `ebur128` filter within 0.5 LU on 10 sample files *(13 library files: within 0.07 LU, true peak within 0.06 dB)*
- [x] `render_comparison` produces a grid Claude Code can read and correctly describe (silence, tail length, brightness)
- [ ] `import_to_project` copies and normalizes a file into a Godot project and records the usage, following the lossless/lossy and one-shot rules
- [x] `foley dedupe report` lists exact-duplicate groups and reclaimable space, and makes no change to the NAS
- [x] A test proves no module other than `nas_write.py` opens library files for writing

### Phase 1b: semantic search

Before measuring search, Scott writes a **test set**: about 25 text queries typical of Endless Reach, each with 1–3 files he considers good answers. Write it **after** the new Sonniss packs are in the library, or better matches from those packs will look like misses.

Scope: pipeline stages 6–7 (PANNs tags, CLAP embeddings), hybrid search, `find_similar`, `foley eval`.

- [ ] Each model's preprocessing is verified against its reference example code on a few files before indexing the library
- [ ] At least 80% of test queries have a good answer in the top 10 results, measured by `foley eval`
- [ ] `foley eval` reports keyword-only and hybrid scores; CLAP is kept only if hybrid beats keyword-only
- [ ] Two or three CLAP checkpoints compared on the test set; the winner is recorded in `meta`

### Phase 2: the review loop and duplicate cleanup (launch window if Phase 1 is early, else after launch)

Scope: `decisions` and `usages` writes, Sound Board review queue and catalog view, `get_examples`, `record_decision`, `check_mix_fit`, `foley.project.toml` with loudness targets and hero slots, the CLAUDE.md selection workflow, daily scheduled sync (delivered early by the `foley browse` scheduler; see Library sync), and `foley dedupe plan / apply / restore / purge`. Duplicate cleanup does not touch Endless Reach, so Scott may pull it forward once Phase 1a passes.

- [ ] Every Sound Board action writes a correct `decisions` row
- [ ] Claude Code follows the workflow end to end on 10 new slots without manual steering
- [ ] Hero slots and low-confidence picks always land in the review queue
- [ ] On a fixture library: `apply` moves only planned files to quarantine, refuses a stale plan or a changed file, never removes a group's last copy, and `restore` returns every file byte-identical to its original path
- [ ] Every dedupe action is logged in `dedupe_actions`, and decisions and usages still resolve after cleanup
- [ ] First dedupe run on the real NAS: Scott reviews the plan file before `apply`

### Phase 3: learning Scott's taste (after launch)

Scope: per-category preference model and `predicted_approval`, audio-captioning model evaluation, `foley publish` / `pull` for other dev machines, optional Essentia music descriptors in WSL (as a separate process; see the license note), and open-source publication.

- [ ] Preference model beats a CLAP-similarity-only baseline on held-out decisions
- [ ] A captioning model is adopted only if it raises the test-set score over templated captions

## Risks, constraints and open questions

The biggest technical risk is CLAP underperforming on stylized game SFX; hybrid search is the required mitigation. The biggest operational risk is deleting something from the NAS by mistake; quarantine, re-verification and the audit log are the required mitigation.

**Risks and required mitigations**

- **CLAP is weak on synthetic and stylized sounds.** `search_audio` must be hybrid: CLAP similarity blended with SQLite FTS5 keyword matching over filename, pack, category, metadata, tags and caption. Tune the blend weight on the test set.
- **Silent preprocessing mismatch.** Wrong sample rate or mel settings produce plausible but useless embeddings. Verify each model against its reference example code on a few files before indexing the library.
- **Library scale.** The library is growing about tenfold to roughly 250 GB. The first full index may run for hours over the network. It must be resumable, commit per file and be safe to run overnight.
- **Data loss on the NAS.** Duplicate cleanup is the only path that changes the library. It is CLI-only, quarantines instead of deleting, re-verifies byte equality at apply time, never removes a last copy and logs every action.
- **Losing Scott's decisions.** They are the hardest data to replace. They live in `ledger.db`, separate from the rebuildable catalog, keyed by content hash, and backed up.
- **Licensing.** Record license per pack where known; `import_to_project` writes provenance into `usages`, and files with unknown license are flagged in search results.
- **Scope creep before launch.** Phase 2 and 3 work does not start until Phase 1 acceptance passes, and none of it delays Endless Reach.

**Constraints for Claude Code**

- Never write to the NAS library, except through `nas_write.py` when running `foley dedupe apply`, `restore` or `purge`, which a human runs from the CLI. Never run those commands against the real NAS on Claude Code's own initiative.
- Keep both schemas versioned with migrations; the Sound Board depends on them.
- Everything runs on Windows without WSL in Phases 1 and 2.
- Keep the repo publishable: no studio-specific values in code, no audio, catalogs or model weights committed.

**Open questions for Scott**

- [x] Library size: 2,256 files / 18.7 GB on Oct 14, 2026, growing to roughly 25–40k files / 250 GB with Sonniss 2016–2024
- [x] Is license information recorded per pack anywhere today? Yes: the library README covers all files, and packs carry their own license files
- [ ] Endless Reach audio lock date, which sets the real Phase 1 deadline
- [ ] How many Endless Reach audio slots are still unfilled? If few, Foley's pre-launch value is mostly a loudness audit of the audio already in the game
- [ ] Does the Sound Board live in the Endless Reach repo or its own?
- [ ] Which machine holds the master catalog and `ledger.db`, and runs the daily sync (`foley browse` left running)?
- [ ] Quarantine folder location and retention period (proposed: `\\data.soverance.net\Files\Assets\Audio-Quarantine`, 30 days)
- [ ] `dedupe.keep_priority`: when the same file is in two packs, which pack keeps it?
- [x] Open-source license: MIT. Git history is published as-is
