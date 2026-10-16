# Foley

Foley indexes a sound library so that Claude Code, which cannot hear, can search it, inspect it and pick audio from it.

It analyzes every file once, offline, and keeps that analysis in sync as the library changes:

- **Semantic:** filename, folder and embedded metadata (BWF, iXML, ID3, UCS names), AudioSet tags (PANNs CNN14) and CLAP audio embeddings, with hybrid text search
- **Technical:** loudness (ITU-R BS.1770), timing, envelope and spectral features
- **Visual:** a spectrogram and waveform PNG per file at fixed scales, which Claude can read with its vision

Claude Code reaches the catalog through an MCP server (`foley-mcp`); people use the `foley` CLI.

> Foley is an internal Soverance Studios tool shared as-is. It is not a supported product.

## Install

Requires [uv](https://docs.astral.sh/uv/) and, for formats libsndfile cannot read, [ffmpeg](https://ffmpeg.org/) on `PATH`.

```
uv tool install .
cp foley.toml.example foley.toml   # then set library.root
foley index
foley search "metal impact with a short tail"
```

Register the MCP server with Claude Code in a project's `.mcp.json`:

```json
{"mcpServers": {"foley": {"command": "foley-mcp"}}}
```

## Models

`foley index` tags every file with AudioSet labels (PANNs CNN14) and embeds it with CLAP, after the other analysis: a file is searchable as soon as its metadata, features and loudness are in, and the model stages fill in afterwards (resumable; `foley status` shows their progress under `models`). Inference runs on an NVIDIA GPU when one is present (`uv sync` installs PyTorch's CUDA 12.6 build on Windows and Linux; it needs an NVIDIA driver 560 or newer and works from a GTX 900 series up) and otherwise on the CPU, more slowly. The weights download on first use: PANNs (~330 MB, Zenodo, CC BY 4.0) into the data folder, the CLAP checkpoints (0.6-0.8 GB each, Hugging Face, Apache-2.0) into the Hugging Face cache. `[models]` in `foley.toml` turns the stages off for plain `foley index` runs, picks the device and the CLAP checkpoints; `foley index --stage tags --stage embeddings` runs just these stages. `eval/verify_models.py` checks the preprocessing against each model's reference code on a few library files.

## Search

Search is hybrid once the catalog has CLAP embeddings: keyword matching (file names, folders, UCS categories, embedded metadata, AudioSet tags, captions) is blended with CLAP similarity between the query text and the audio itself, so a file can be found by how it sounds even when its name says nothing. Until embeddings exist, or for a CLAP checkpoint that has none, search is keyword-only and says so in its `note`. The first query in a long-running process (`foley-mcp`, `foley browse`) loads the CLAP text encoder on the CPU, which takes a few seconds.

- `foley search "heavy metal impact with a long ringing tail"` (CLI) and the MCP tool `search_audio` rank the same way; exact duplicates collapse to one result.
- The MCP tool `find_similar` (and **Find similar** in `foley browse`) lists the sounds closest to a given file by CLAP audio embedding, with the same filters.
- `[search] clap_model` and `blend_weight` in `foley.toml` pick the checkpoint and the weight (0 = keyword-only, 1 = CLAP only). Leave them unset to use what `foley eval --adopt` recorded.

### foley eval

`foley eval` measures search on a hand-written test set (`[eval] testset`; format in `eval/testset.example.toml`): for each query, whether one of its good answers (or an exact copy) is in the top 10, and the mean reciprocal rank. It always reports keyword-only next to every CLAP checkpoint that has embeddings at blend weights 0.1 to 1, prints a table and the best configuration's misses, and saves a JSON report under `<data folder>/eval/`.

```
foley eval                                   # every checkpoint with embeddings, weights 0..1
foley eval --clap htsat-unfused --weights 0,0.3,0.5,0.7 --k 10
foley eval --adopt                           # record the winner in the catalog
```

`--adopt` records the best checkpoint and weight in the catalog's `meta` table, where search picks them up. CLAP has to earn its place: if no hybrid configuration beats keyword-only, the adopted weight is 0 (keyword-only search). Queries whose `good` list is still empty are skipped.

## foley browse

`foley browse` starts a local web UI for people: browse the library's folders, search it, listen to files with loudness matching, and see Foley's analysis (waveform, spectrogram, caption, features, metadata, licence, duplicates, decisions) while you audition.

```
foley browse                 # http://127.0.0.1:8765/, opens your browser
foley browse --port 9000 --no-open
```

By default it listens on 127.0.0.1 and answers only requests addressed to localhost. To use it from other machines in the studio, set `[browse] host`, `port` and `allowed_hosts` in `foley.toml` (requests for any other host name get 403); mind your sample licences, which usually forbid serving a library beyond its licensees. `--log-file PATH` logs server and scan events to a rotating file, for running it unattended (e.g. with `pythonw.exe -m foley.cli browse --no-open` from a logon task).

On Windows, `scripts\windows\install-browse-task.ps1` registers exactly that logon task (no admin needed; `-Uninstall` removes it), and `scripts\windows\allow-browse-firewall.ps1 -Port <port>` (elevated) opens the port to Domain/Private networks on the local subnet.

Left running, it also keeps the catalog current: the Scans dialog (click the scan status in the top bar) turns on a daily `foley index` at a time you pick, shows the live log and the history, and has Scan now / Stop. Defaults come from `[schedule]` in `foley.toml`; see `foley.toml.example`.

With CLAP embeddings, searching in the browser is hybrid (each result shows how well it matches), the detail pane lists **Similar sounds**, and **Find similar** (`F`) fills the results with every sound like the selected one.

With `[eval] testset` set, a **Test set** entry in the left pane lists the test queries and how many answers each has. Pick one, browse and listen as usual (folders, search with any words, similar sounds), and press `T` on each file you would accept; the browser writes the answers to the TOML file, keeping its header comments. Aim for 1 to 3 answers per query, found by browsing as well as searching, so the test set does not favour keyword search.

Star files to put them on a shortlist (`shortlist.json` in the data folder, keyed by file content so stars survive moves); Claude Code reads it with the MCP tool `get_shortlist`. Set the loudness-match target with `[browse] loudness_target_lufs` in `foley.toml` (default -20). The browser never writes to the library. Everything it caches (peaks, images, the shortlist) lives in the data folder; the only other file it writes is the test set you configured.

The UI works offline: it ships its fonts (Michroma, Barlow and JetBrains Mono, under the SIL Open Font License; see `src/foley/browse_ui/fonts/`) and loads nothing from the internet. The gear button in the top bar switches the console decoration (corner brackets, rulers, dot grid) on or off and picks comfortable or compact rows; both are remembered per browser. To show your own logo, point `[browse] brand_dir` at a folder holding `logo.png` and, optionally, `wordmark.png`; without it the UI shows a neutral mark.

The overview images (and the grids `render_comparison` makes for Claude) use fixed scales and one colour key: the waveform is cyan with its RMS envelope in orange, and in the spectrogram near-black navy is silence, rising through blue and cyan to white at full scale.

## Safety

Foley treats the library as read-only. The only commands that change it are `foley dedupe apply`, `restore` and `purge`. They move duplicates to a quarantine folder rather than deleting them, verify every file byte for byte first, and log each action. No MCP tool can modify the library.

## License

MIT
