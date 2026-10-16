# Foley

Foley indexes a sound library so that Claude Code, which cannot hear, can search it, inspect it and pick audio from it.

It analyzes every file once, offline, and keeps that analysis in sync as the library changes:

- **Semantic:** filename, folder and embedded metadata (BWF, iXML, ID3, UCS names), with text search; CLAP embeddings and AudioSet tags are coming next
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

## foley browse

`foley browse` starts a local web UI for people: browse the library's folders, search it, listen to files with loudness matching, and see Foley's analysis (waveform, spectrogram, caption, features, metadata, licence, duplicates, decisions) while you audition.

```
foley browse                 # http://127.0.0.1:8765/, opens your browser
foley browse --port 9000 --no-open
```

It listens on 127.0.0.1 only and answers only requests addressed to localhost. Keep it that way: sample licences usually forbid serving a library to anyone else. Star files to put them on a shortlist (`shortlist.json` in the data folder, keyed by file content so stars survive moves); Claude Code reads it with the MCP tool `get_shortlist`. Set the loudness-match target with `[browse] loudness_target_lufs` in `foley.toml` (default -20). The browser never writes to the library. Everything it caches (peaks, images, the shortlist) lives in the data folder.

## Safety

Foley treats the library as read-only. The only commands that change it are `foley dedupe apply`, `restore` and `purge`. They move duplicates to a quarantine folder rather than deleting them, verify every file byte for byte first, and log each action. No MCP tool can modify the library.

## License

MIT
