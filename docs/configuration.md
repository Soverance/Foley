# Configuration

Foley reads one TOML file, `foley.toml`. Start from [`foley.toml.example`](../foley.toml.example) in the repo root; every key is optional except `[library] root`.

## Where Foley looks

The first of these that exists wins:

1. `--config PATH` on the command line (`foley --config PATH <command>`)
2. the `FOLEY_CONFIG` environment variable (this is how you point `foley-mcp` at a config)
3. `./foley.toml` in the current directory
4. `<default data folder>/foley.toml`

The default data folder is `%LOCALAPPDATA%\Foley` on Windows and `$XDG_DATA_HOME/foley` (usually `~/.local/share/foley`) elsewhere. Keeping `foley.toml` there means every shell, the MCP server and a logon task all find the same file without flags.

Paths can use forward slashes on Windows (`C:/Users/me/...`). A bad value stops Foley with a message naming the key, for example `browse.port must be a port number (0-65535)`.

## `[library]`

| Key | Default | Meaning |
| --- | --- | --- |
| `root` | (required) | Root of the audio library. A UNC path (`//nas.example.local/Audio`), a mapped drive or a local folder. Every catalog path is stored relative to it, so a UNC path and a drive letter for the same share are interchangeable. Foley only reads from here. |
| `pack_roots` | `["packs"]` | Folders directly under the root whose children are packs. For `packs/<pack>/<a>/<b>/file.wav`, the pack is `<pack>` and the category path `<a>/<b>`. Outside a pack root the pack is empty and the category path is the whole folder. Search can filter on both. |
| `settle_minutes` | `10` | Files modified more recently than this (or dated less than this in the future, for clock skew) are skipped until a later run, since they may still be copying or extracting. The scan log lists them by name. |

Audio extensions indexed: `.wav .flac .ogg .mp3 .aif .aiff`. Archives (`.zip .rar .7z`) are counted, never opened: `foley status` lists the ones without an extracted folder next to them.

## `[storage]`

| Key | Default | Meaning |
| --- | --- | --- |
| `data_dir` | `%LOCALAPPDATA%\Foley` or `~/.local/share/foley` | Local folder for `catalog.db`, `ledger.db`, the image and peaks caches, PANNs weights, logs, the shortlist and eval reports. It must be outside `library.root`; Foley refuses to start otherwise. |

Back up `ledger.db`: it holds decisions and project usages that cannot be regenerated. `catalog.db` and the caches can be deleted and rebuilt with `foley index`. See [schema.md](schema.md).

## `[models]`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Run the AudioSet tag (PANNs CNN14) and CLAP embedding stages in a plain `foley index`. With `false` they are skipped, but `foley index --stage tags --stage embeddings` still runs them. They need the `models` extra installed (`uv sync --extra models`). |
| `device` | `"auto"` | `"auto"` uses a CUDA GPU when PyTorch sees one, else the CPU. Or force `"cuda"` or `"cpu"`. |
| `clap` | all | CLAP checkpoints to compute: any of `"larger-general"`, `"htsat-unfused"`, `"htsat-fused"`. Several can coexist so `foley eval` can compare them. Adding one later computes only that one; removing one re-runs nothing. |

Weights download on first use: PANNs (about 330 MB, Zenodo, CC BY 4.0) into `<data_dir>/models`, CLAP (0.6 to 0.8 GB each, Hugging Face, Apache-2.0) into the Hugging Face cache (`HF_HOME`).

## `[search]`

| Key | Default | Meaning |
| --- | --- | --- |
| `clap_model` | adopted, else first of `[models] clap` | The CLAP checkpoint whose embeddings hybrid search uses. |
| `blend_weight` | adopted, else `0.5` | 0 = keyword matching only, 1 = CLAP similarity only. |

Leave both unset to use what `foley eval --adopt` recorded in the catalog (see [eval.md](eval.md)). A value set here overrides the adopted one, and `foley eval --adopt` reminds you of that.

## `[eval]`

| Key | Default | Meaning |
| --- | --- | --- |
| `testset` | unset | Path to your search test set (format: [`eval/testset.example.toml`](../eval/testset.example.toml)). `foley eval` scores search against it, and `foley browse` shows a **Test set** panel for marking answers while you listen, rewriting this file (its header comment block is kept). Keep it outside the repo: it names files in your library. |

## `[browse]`

| Key | Default | Meaning |
| --- | --- | --- |
| `loudness_target_lufs` | `-20` | With **loudness match** on, the browser plays every clip at this integrated loudness so quiet and hot files audition at the same level. Playback gain only. |
| `host` | `"127.0.0.1"` | Address `foley browse` listens on (`--host` overrides). `"0.0.0.0"` serves every interface. |
| `port` | `8765` | Port (`--port` overrides). |
| `allowed_hosts` | `[]` | Host names other machines will use to reach the server, such as `["foley.example.lan"]`. Requests are answered only when their `Host` header is localhost, a loopback IP, the bound address, one of these names (case-insensitive, port ignored) or, for a non-loopback bind, this machine's own name or IP. Anything else gets 403, which blocks DNS rebinding. |
| `brand_dir` | unset | Optional folder holding `logo.png` (round mark next to the product name, shown 30 px wide; about 128 px square is plenty) and `wordmark.png` (light-on-dark line under the name, shown 7 px tall). Either may be missing. Without it the UI shows a neutral mark. |

Serving beyond your own machine has licence and security implications; read [browse.md](browse.md#serving-it-on-your-network) first.

## `[schedule]`

The daily library scan that a running `foley browse` performs (an incremental `foley index`, read-only on the library).

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Run the daily scan. |
| `time` | `"03:00"` | Local 24-hour wall-clock time on the machine running `foley browse`. |
| `catch_up` | `true` | If the server was not running at the scheduled time, scan after it starts when the last completed scan is older than the most recent scheduled time. |
| `startup_delay_minutes` | `5` | How long after start that catch-up scan waits, so the network and the share are up after logon. |

These are defaults. Once someone saves the schedule in the browser's Scans dialog, `enabled`, `time` and `catch_up` live in `<data_dir>/schedule.json` and override this table; delete that file to fall back. `startup_delay_minutes` is read from `foley.toml` only.

## `[prune]`

| Key | Default | Meaning |
| --- | --- | --- |
| `missing_days` | `30` | `foley prune --apply` removes catalog rows for files missing longer than this that have no ledger entries. It never touches the library. |

## `[dedupe]`

| Key | Default | Meaning |
| --- | --- | --- |
| `quarantine_root` | unset | Where a future `foley dedupe apply` moves duplicates: on the same share as the library root and outside it. Unset disables it. |
| `retention_days` | `30` | How long quarantined files are kept before `purge`. |
| `keep_priority` | `[]` | When identical files live in several packs, keep the copy in the first pack listed here (matched as a prefix of the relative path). |

Only `foley dedupe report` exists today; it changes nothing.

## Per-project settings: `foley.project.toml`

`import_to_project` reads loudness targets from a `foley.project.toml` in the root of the game project it imports into. It is optional.

```toml
[project]
name = "my-game"                 # default: the project folder name

[loudness]
oneshot_peak_db = -3.0           # clips under 0.4 s are normalized on true peak to this

[loudness.targets]               # integrated LUFS per usage key; longest dotted prefix wins
"sfx.ui" = -20.0
"sfx.weapon" = -14.0
"music" = -16.0

[review]
hero_slots = ["music.title", "sfx.boss.*"]
```

With that file, `import_to_project(..., usage_key="sfx.weapon.laser")` normalizes to -14 LUFS. Gain is capped so true peak never exceeds -1 dBTP.

## A minimal config

```toml
[library]
root = "//nas.example.local/Audio"

[eval]
testset = "C:/Users/me/AppData/Local/Foley/testset.toml"
```

Everything else falls back to the defaults above.
