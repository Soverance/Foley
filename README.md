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

## Safety

Foley treats the library as read-only. The only commands that change it are `foley dedupe apply`, `restore` and `purge`. They move duplicates to a quarantine folder rather than deleting them, verify every file byte for byte first, and log each action. No MCP tool can modify the library.

## License

MIT
