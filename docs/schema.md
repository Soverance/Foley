# Catalog schema

Foley keeps two SQLite files in its data folder (default `%LOCALAPPDATA%\Foley\` on Windows, `~/.local/share/foley/` elsewhere). Together they are Foley's public interface: any tool can read them without calling Foley's code. The Soverance Godot Sound Board, for example, reads them from C# with `Microsoft.Data.Sqlite`.

| File | Contents | Rebuildable? |
| --- | --- | --- |
| `catalog.db` | Everything derived from the library: assets, features, tags, captions, embeddings, search index, image cache records, sync history | Yes. Delete it and run `foley index` |
| `ledger.db` | Human decisions, game usages and the log of every change Foley made to the library | **No.** Back it up |

Both run in WAL mode, so readers and the indexer can have them open at the same time. Open them read-only unless you are writing decisions.

## Versioning

Each file has a `meta` table whose `schema_version` row holds the applied migration number. Migrations live in `src/foley/catalog/migrations/<catalog|ledger>/NNNN_*.sql` and are never edited once released. Consumers should check `schema_version` and refuse versions newer than they understand.

| File | Current version |
| --- | --- |
| `catalog.db` | 1 |
| `ledger.db` | 1 |

## Conventions

- Paths (`rel_path`) are relative to the library root, with forward slashes. Join them to the root from your own `foley.toml`.
- Timestamps are ISO-8601 UTC text.
- `content_hash` is the xxh3-128 hex digest of the file bytes. `audio_hash` is the same digest over the decoded PCM, so files that differ only in metadata share it.
- Ledger rows identify audio by `content_hash`, never by catalog `id`, because ids do not survive a catalog rebuild. To join, attach the ledger: `ATTACH 'ledger.db' AS ledger` and join `ledger.decisions.content_hash = assets.content_hash`.
- A file whose bytes change gets a new `content_hash`, and its old decisions stop attaching. That is intended: it is a different sound now.

## catalog.db

### assets

One row per audio file seen in the library.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | INTEGER | Stable while the file exists; kept across moves and renames |
| `rel_path` | TEXT | Unique |
| `pack` | TEXT | Folder directly under a configured pack root, else NULL |
| `category_path` | TEXT | Folders between the pack (or root) and the file |
| `ext` | TEXT | Lowercase, with dot |
| `size_bytes`, `mtime_ns` | INTEGER | As listed on disk; drive change detection |
| `content_hash`, `audio_hash` | TEXT | See conventions |
| `license` | TEXT | Relative path of the nearest license file above the asset |
| `source_metadata` | TEXT (JSON) | Keys present when found: `bext`, `ixml`, `info`, `tags`, `ucs` |
| `first_seen`, `last_seen`, `missing_since`, `indexed_at` | TEXT | |
| `status` | TEXT | `pending` (not yet analyzed), `active`, `missing` (gone from the library), `error` |
| `error`, `error_count` | TEXT, INTEGER | Last analysis failure |

Most consumers want `WHERE status = 'active'`.

### features

One row per analyzed asset. Units: seconds, Hz, LUFS, dBTP.

| Column | Notes |
| --- | --- |
| `duration_s`, `sample_rate`, `channels` | Source file properties |
| `lufs_integrated` | ITU-R BS.1770. NULL for clips under 0.4 s |
| `lufs_short_max` | Maximum 3 s short-term loudness |
| `true_peak_db` | dBTP, 4x oversampled |
| `lead_silence_s`, `tail_silence_s` | Time before/after the signal first/last exceeds −60 dBFS |
| `attack_s` | Envelope 10% of peak → peak |
| `decay_s` | Peak → 40 dB below peak |
| `onset_count` | |
| `centroid_hz_mean`, `rolloff_hz_mean`, `flatness_mean`, `zcr_mean` | librosa, n_fft 2048, hop 512 |
| `band_energy` | JSON shares summing to 1: `sub` <60, `low` 60–250, `low_mid` 250–500, `mid` 500–2k, `upper_mid` 2k–4k, `presence` 4k–6k, `air` >6k Hz |
| `tempo_bpm` | Music only |
| `loop_seam_score` | Music and ambience only; lower is a cleaner loop |
| `is_music` | 0/1 from AudioSet tags; NULL until tagged |

### tags, captions, embeddings

| Table | Key | Columns |
| --- | --- | --- |
| `tags` | (`asset_id`, `model`, `label`) | `score` 0–1. AudioSet labels scoring ≥ 0.1 |
| `captions` | (`asset_id`, `model`) | `text`. `model = 'template-v1'` for templated captions |
| `embeddings` | (`asset_id`, `model`) | `dim`, `vector`: little-endian float32 BLOB, L2-normalized, so cosine similarity is a dot product |

### images

`(asset_id, kind)` → `local_path` (relative to the data folder), `version`, `rendered_at`. Images are rendered on demand and cached, so a row may be absent.

### search_fts

FTS5 table whose `rowid` is `assets.id`. Columns: `name`, `pack`, `category`, `metadata`, `tags`, `caption`.

### asset_stages, index_runs, archives, meta

- `asset_stages`: which pipeline stage ran for each asset, at which version.
- `index_runs`: one row per `foley index` run, with counts and `status` (`running`, `completed`, `interrupted`, `failed`).
- `archives`: `.zip`, `.rar` and `.7z` files found in the library and not indexed.
- `meta`: `schema_version`, `library_root`, and model provenance as JSON under `models`.

## ledger.db

### decisions

Written by the Sound Board and by Claude Code (`record_decision`).

| Column | Notes |
| --- | --- |
| `content_hash`, `rel_path` | The audio judged, and where it was at the time |
| `project` | e.g. `endless-reach` |
| `usage_key` | Game-side slot, e.g. `sfx.weapon.laser.fire`. Decisions are per slot, not global |
| `verdict` | `approved`, `rejected`, `replaced`, `auto` (picked by Claude Code without review) |
| `gain_db` | Level change, if any |
| `note` | One line on *why*. The most valuable column: it becomes few-shot context |
| `decided_by`, `decided_at` | |

### usages

Written by `import_to_project`: which game uses which file and where it was copied (`project_path`, a `res://` path). `gain_db` with `gain_applied = 1` was baked into the copy; with `gain_applied = 0` (lossy sources) the game should apply it, e.g. via `volume_db`.

### dedupe_actions

Audit log of every quarantine, restore and purge `foley dedupe` performed on the library.
