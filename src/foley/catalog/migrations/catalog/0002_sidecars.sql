-- Sidecar JSON files: <stem>.json beside an audio file <stem>.<ext> in the same folder,
-- for example the record a generation service's pipeline writes (prompt, model, licence).
-- foley index reads one when it is new or changed (by size and mtime) and adds its text
-- fields to the asset's search_fts row. Docs: docs/schema.md, "sidecars".
CREATE TABLE sidecars (
    asset_id   INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    rel_path   TEXT NOT NULL,         -- the sidecar file, relative to the library root
    size_bytes INTEGER NOT NULL,
    mtime_ns   INTEGER NOT NULL,
    data       TEXT,                  -- the JSON object as read; NULL when unreadable
    error      TEXT                   -- why it could not be read or parsed, else NULL
);
