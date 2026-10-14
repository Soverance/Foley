# Foley — Claude Code Handoff Spec

Oct 14, 2026 · @Scott

## Purpose and goals

**Foley** is a standalone Soverance Studios tool that lets Claude Code search, understand and pick audio from the studio asset library without a human reviewing every file. It is named for Foley, the film craft of creating everyday sound effects. Repo: `soverance/foley` (to be created by Scott).

**Problem.** Claude Code cannot hear. Today it picks audio from filenames, and Scott reviews each pick by hand in the Godot Sound Board. That works but does not scale to hundreds of SFX and music files.

**Approach.** Analyze the whole library once, offline, and store what each file sounds like as data Claude Code can query:

- **Semantic:** text-searchable embeddings (CLAP), AudioSet tags, and a one-line caption per file
- **Technical:** loudness, timing, envelope and spectral features
- **Visual:** a spectrogram + waveform PNG per file, which Claude can view with its vision

**Success looks like:**

- Claude Code finds good candidates from a text description in one tool call
- Low-stakes sounds (UI, pickups, ambient variations) are picked and loudness-normalized with no manual review
- Scott reviews only hero sounds and anything flagged low-confidence: dozens of reviews, not hundreds
- The same catalog serves Endless Reach, the tactics project and later games

**Non-goals:** generating or editing audio, mixing, replacing Scott's ear on hero sounds, and shipping any of this inside a game build.

## Architecture

A Python indexer and MCP server write and serve one local SQLite catalog; the Godot Sound Board reads the same catalog from C#, and nothing else crosses the language boundary.

&#91;embedded content: Foley architecture · 6 parts around one catalog\]

The catalog file and its PNGs are the only interface between the two halves. Neither side calls the other's code.

**Suggested repo layout**

```
foley/
  pyproject.toml, uv.lock, foley.toml.example
  src/foley/
    scan.py, decode.py, features.py, loudness.py, images.py
    models/   (clap.py, panns.py)
    catalog/  (schema.sql, migrations/, db.py)
    search.py, cli.py, mcp_server.py
  eval/       (testset.toml, eval.py)
  tests/
```

## Environment and storage

Foley runs Windows-native, managed by `uv`; the audio library stays read-only on the NAS and the catalog lives on local disk.

**Runtime**

- Python 3.12 project managed with [uv](https://docs.astral.sh/uv/) (`winget install astral-sh.uv`). Scott never touches pip or virtual environments directly.
- Entry points: `foley` (CLI) and `foley-mcp` (MCP server). Installable with `uv tool install` so `foley.exe` is on PATH.
- PyTorch with CUDA for the model pass, on the dev machine's RTX 2070 Super (8 GB VRAM: ample for CLAP and PANNs inference). CPU fallback must work, just slower.
- WSL is not required. It is reserved for an optional later phase (Essentia music descriptors, which lack native Windows builds) that writes into the same catalog.

**Source library**

- Root: `\\data.soverance.net\Files\Assets\Audio`, set in a `foley.toml` config, never hard-coded.
- Read-only. Foley never writes, renames or moves anything on the NAS.
- Store every path **relative to the library root**, so the catalog works from any machine whether the share is reached by UNC path or a mapped drive.
- Formats: WAV, OGG, FLAC, MP3 (decode via soundfile, falling back to ffmpeg).

**Catalog location**

- SQLite catalog and spectrogram PNGs on **local disk** (default `%LOCALAPPDATA%\Foley\`, configurable). SQLite over an SMB share risks locking failures and corruption.
- Other dev machines get a copy: `foley publish` writes a read-only snapshot to a configured NAS folder, and `foley pull` copies it down.

**Indexing over the network**

- Incremental: a file is re-analyzed only if its relative path, size or modified time changed. A content hash (xxHash) detects moved or duplicate files.
- Each file is read from the NAS once per run; decode once, compute every feature from that single in-memory copy.
- Resumable: an interrupted run picks up where it stopped.

## Catalog schema

One SQLite file (`catalog.db`) with the `sqlite-vec` extension is the contract between Foley and every consumer, including the Godot Sound Board. Treat the schema as a public API: version it and migrate it, never change it silently.

| Table | Key columns | Purpose |
| --- | --- | --- |
| `assets` | `id`, `rel_path`, `pack`, `category_path`, `ext`, `size_bytes`, `mtime`, `content_hash`, `license`, `source_metadata` (JSON), `indexed_at`, `status` | One row per audio file. `pack` and `category_path` come from folder names; `source_metadata` holds BWF/iXML/ID3 fields |
| `features` | `asset_id`, `duration_s`, `sample_rate`, `channels`, `lufs_integrated`, `lufs_short_max`, `true_peak_db`, `lead_silence_s`, `tail_silence_s`, `attack_s`, `decay_s`, `onset_count`, `centroid_hz_mean`, `rolloff_hz_mean`, `flatness_mean`, `zcr_mean`, `band_energy` (JSON), `tempo_bpm`, `loop_seam_score`, `is_music` | Objective DSP measurements |
| `tags` | `asset_id`, `label`, `score`, `model` | AudioSet labels above a threshold, from PANNs |
| `captions` | `asset_id`, `text`, `model` | One-sentence description per file |
| `embeddings` | `asset_id`, `model`, `vector` (sqlite-vec) | CLAP audio embedding for text search and find-similar |
| `images` | `asset_id`, `kind`, `local_path` | Rendered spectrogram + waveform PNG |
| `decisions` | `id`, `asset_id`, `project`, `usage_key`, `verdict`, `gain_db`, `note`, `decided_by`, `decided_at` | Approvals, rejections and level changes from the Sound Board or Claude Code |
| `usages` | `asset_id`, `project`, `project_path`, `imported_at`, `normalized_to_lufs` | Which game uses which file, and where it was copied to |
| `meta` | `schema_version`, `models` (JSON), `library_root` | Versioning and provenance |

- `verdict` values: `approved`, `rejected`, `replaced`, `auto` (picked by Claude Code without review).
- `usage_key` is the game-side slot, such as `sfx.weapon.laser.fire` or `music.stage2.loop`. Decisions are judged per slot, not globally: a file can be wrong for one slot and right for another.
- Every model-derived row records its `model` (name + version) so a model upgrade can re-run just that stage.

## Analysis pipeline

Each file passes through seven stages in order; each stage is independently re-runnable (`foley index --stage tags`) so a model upgrade does not force a full re-index.

1. **Scan and metadata.** Walk the library root, record path, pack and category from folder names, and read embedded BWF/iXML/ID3 metadata. Parse UCS-style filenames (`CATsub_Description_Creator`) where present. This is free semantic signal: do it first and well.
2. **Decode.** Load each file once at native rate. Derive the mono and resampled copies each later stage needs (PANNs expects 32 kHz, CLAP 48 kHz) from that one decode.
3. **DSP features.** librosa with `n_fft=2048`, `hop=512`. Definitions to implement exactly:
   - Leading/tail silence: time before/after the signal first/last exceeds −60 dBFS
   - Attack: time from the envelope reaching 10% of peak to peak
   - Decay: time from peak to 40 dB below peak
   - Band energy: share of energy in sub (<60 Hz), low (60–250), low-mid (250–500), mid (500–2k), upper-mid (2k–4k), presence (4k–6k), air (>6k)
   - Loop seam score (music and ambience only): sample discontinuity plus spectral distance between the last and first 50 ms; lower is a cleaner loop
   - Tempo (music only) via librosa beat tracking
4. **Loudness.** pyloudnorm (ITU-R BS.1770) for integrated LUFS, max short-term loudness and true peak. Short one-shots under 0.4 s have no valid integrated LUFS: store null and rely on short-term and peak values.
5. **Images.** One PNG per file, 1024×512: waveform with RMS envelope on top, log-mel spectrogram below (128 mel bands, 20 Hz to Nyquist, fixed −80 to 0 dB range, one fixed colormap). Duration, LUFS and peak in the header. **Fixed scales are mandatory** so images are comparable side by side.
6. **Tags.** PANNs CNN14 on AudioSet; store every label scoring ≥ 0.1. Set `is_music` from the Music tag score.
7. **Embeddings.** LAION-CLAP audio embedding per file, normalized, stored in sqlite-vec. Claude Code should benchmark two or three public CLAP checkpoints on a hand-labeled test set (Phase 1 acceptance) and keep the best.

**Captions** are split across phases. Phase 1 generates a templated summary line from tags, features and filename ("0.42 s bright metallic impact, sharp attack, short tail, −14 LUFS"). An audio-captioning model is evaluated in Phase 3, and kept only if it beats the template on the test set. Large audio-language models may not fit in 8 GB VRAM without quantization.

## MCP tool surface

Claude Code reaches Foley through nine MCP tools. Results stay compact: ids, paths, a caption, top tags and a handful of key numbers, never raw feature arrays, so a search costs few tokens.

| Tool | Inputs | Returns |
| --- | --- | --- |
| `search_audio` | `query` (text), `filters` (category, pack, `is_music`, duration / LUFS / attack ranges), `project` (to hide files rejected for it), `limit` (default 20) | Ranked candidates: id, path, similarity, caption, top 3 tags, duration, LUFS, attack |
| `find_similar` | `asset_id`, `filters`, `limit` | Nearest neighbors by CLAP embedding |
| `describe` | `asset_id` | Full record: features, tags, caption, image path, decision history |
| `render_comparison` | `asset_ids` (2–6) | Path to one grid PNG of their images at identical scales; Claude Code views it with Read |
| `check_mix_fit` | `sfx_id`, `music_id` | Per-band overlap scores and a one-line verdict on masking risk |
| `get_examples` | `project`, `usage_key` or category | Approved and rejected files for that slot, with Scott's notes, as few-shot context |
| `import_to_project` | `asset_id`, `project_root`, `dest` (path under `res://`), `usage_key`, `target_lufs` (optional) | Copies the file into the Godot project, optionally loudness-normalized, and records the usage. Refuses to overwrite unless `overwrite=true` |
| `record_decision` | `asset_id`, `project`, `usage_key`, `verdict`, `note` | Logs an `auto` pick or other verdict from Claude Code |
| `catalog_status` | none | Asset counts, last index run, model versions, any files that failed to analyze |

**Per-project loudness targets** live in a `foley.project.toml` in each game repo (for example UI −20 LUFS, weapons −14, music −16). `import_to_project` reads it when `target_lufs` is omitted.

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
- Read: assets, features, tags, captions, images, usages.
- Write: the `decisions` table only.
- The catalog runs in SQLite WAL mode so the Sound Board and the MCP server can be open at the same time.

**Changes to the Sound Board**

- A **Needs review** queue: files Claude Code imported and flagged, plus `auto` picks Scott has not yet seen, grouped by `usage_key`.
- Show Foley's spectrogram PNG and caption beside the existing waveform analyzer, plus the top tags and key numbers.
- Approve, reject, replace and gain changes each write a `decisions` row, with an optional one-line note on *why*. The notes are the most valuable training signal: they become the few-shot examples `get_examples` returns.
- A replace action searches the catalog with `find_similar` on the rejected file, so Scott picks a substitute without leaving the board.

**The feedback loop**

- **Phase 2:** Claude Code reads past decisions through `get_examples` before choosing. This alone lets it learn Scott's taste per slot.
- **Phase 3:** once a category has roughly 30 or more decisions, train a small logistic-regression model per category on CLAP embeddings plus features, and expose its output as `predicted_approval` in search results. Average gain changes per category also suggest corrections to the loudness targets.
- Low `predicted_approval`, or any slot marked as a hero sound in `foley.project.toml`, always routes to the review queue.

## Phased plan and acceptance criteria

Only Phase 1 is meant to land before the Endless Reach launch on November 12. If it is not done and validated about two weeks before launch, Endless Reach finishes with the current workflow, and Foley continues after launch.

### Phase 1: search that works (about one week)

Scope: repo scaffold with uv, config, the seven pipeline stages (templated captions), catalog with schema versioning, CLI (`foley index`, `foley search`, `foley describe`), and the MCP tools `search_audio`, `find_similar`, `describe`, `render_comparison`, `import_to_project`, `catalog_status`.

Before building search, Scott writes a **test set**: about 25 text queries typical of Endless Reach, each with 1–3 files he considers good answers.

- [ ] Full library indexes from the NAS share; an interrupted run resumes without redoing finished files
- [ ] Re-running with no library changes skips every file
- [ ] LUFS values agree with ffmpeg's `ebur128` filter within 0.5 LU on 10 sample files
- [ ] At least 80% of test queries have a good answer in the top 10 results, measured by `foley eval`
- [ ] Two or three CLAP checkpoints compared on the test set; the winner is recorded in `meta`
- [ ] `render_comparison` produces a grid Claude Code can read and correctly describe (silence, tail length, brightness)
- [ ] `import_to_project` copies and normalizes a file into a Godot project and records the usage

### Phase 2: the review loop (launch window if Phase 1 is early, else after launch)

Scope: `decisions` and `usages` writes, Sound Board review queue and catalog view, `get_examples`, `record_decision`, `check_mix_fit`, `foley.project.toml` with loudness targets and hero slots, and the CLAUDE.md selection workflow.

- [ ] Every Sound Board action writes a correct `decisions` row
- [ ] Claude Code follows the workflow end to end on 10 new slots without manual steering
- [ ] Hero slots and low-confidence picks always land in the review queue

### Phase 3: learning Scott's taste (after launch)

Scope: per-category preference model and `predicted_approval`, audio-captioning model evaluation, `foley publish` / `pull` for other dev machines, and optional Essentia music descriptors in WSL.

- [ ] Preference model beats a CLAP-similarity-only baseline on held-out decisions
- [ ] A captioning model is adopted only if it raises the test-set score over templated captions

## Risks, constraints and open questions

The biggest technical risk is CLAP underperforming on stylized game SFX; hybrid search is the required mitigation.

**Risks and required mitigations**

- **CLAP is weak on synthetic and stylized sounds.** `search_audio` must be hybrid: CLAP similarity blended with SQLite FTS5 keyword matching over filename, pack, category, metadata, tags and caption. Tune the blend weight on the test set.
- **Silent preprocessing mismatch.** Wrong sample rate or mel settings produce plausible but useless embeddings. Verify each model against its reference example code on a few files before indexing the library.
- **NAS throughput.** The first full index may run for hours over the network. It must be resumable and safe to run overnight.
- **Licensing.** Record license per pack where known; `import_to_project` writes provenance into `usages`, and files with unknown license are flagged in search results.
- **Scope creep before launch.** Phase 2 and 3 work does not start until Phase 1 acceptance passes, and none of it delays Endless Reach.

**Constraints for Claude Code**

- Never write to the NAS library.
- Keep the catalog schema versioned with migrations; the Sound Board depends on it.
- Everything runs on Windows without WSL in Phases 1 and 2.

**Open questions for Scott**

- [ ] Library size: roughly how many audio files, and how many GB?
- [ ] Endless Reach audio lock date, which sets the real Phase 1 deadline
- [ ] Is license information recorded per pack anywhere today?
- [ ] Does the Sound Board live in the Endless Reach repo or its own?
- [ ] Which machine holds the master catalog and decision log?
