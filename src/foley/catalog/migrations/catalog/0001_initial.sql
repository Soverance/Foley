-- catalog.db v1: everything derived from the library. Safe to delete and rebuild.
-- Timestamps are ISO-8601 UTC text. Paths are relative to the library root, with
-- forward slashes.

CREATE TABLE assets (
    id              INTEGER PRIMARY KEY,
    rel_path        TEXT NOT NULL UNIQUE,
    pack            TEXT,                -- folder directly under a pack root, else NULL
    category_path   TEXT,                -- remaining folders between pack and file
    ext             TEXT NOT NULL,       -- lowercase, with dot
    size_bytes      INTEGER NOT NULL,
    mtime_ns        INTEGER NOT NULL,
    content_hash    TEXT,                -- xxh3-128 hex of the file bytes
    audio_hash      TEXT,                -- xxh3-128 hex of the decoded PCM
    license         TEXT,                -- license file governing this asset (rel path), if found
    source_metadata TEXT,                -- JSON: BWF/iXML/ID3/INFO fields and parsed UCS name
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    missing_since   TEXT,
    indexed_at      TEXT,                -- when analysis last completed
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'active', 'missing', 'error')),
    error           TEXT,                -- last failure reason when status = 'error'
    error_count     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX assets_content_hash ON assets(content_hash);
CREATE INDEX assets_audio_hash ON assets(audio_hash);
CREATE INDEX assets_status ON assets(status);
CREATE INDEX assets_size ON assets(size_bytes);

-- Which pipeline stages have completed for an asset, at which stage version.
-- A stage whose version changes is re-run for every asset.
CREATE TABLE asset_stages (
    asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    stage       TEXT NOT NULL,
    version     TEXT NOT NULL,
    done_at     TEXT NOT NULL,
    PRIMARY KEY (asset_id, stage)
);

CREATE TABLE features (
    asset_id         INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    duration_s       REAL,
    sample_rate      INTEGER,
    channels         INTEGER,
    lufs_integrated  REAL,               -- NULL for clips under 0.4 s
    lufs_short_max   REAL,
    true_peak_db     REAL,
    lead_silence_s   REAL,
    tail_silence_s   REAL,
    attack_s         REAL,
    decay_s          REAL,
    onset_count      INTEGER,
    centroid_hz_mean REAL,
    rolloff_hz_mean  REAL,
    flatness_mean    REAL,
    zcr_mean         REAL,
    band_energy      TEXT,               -- JSON {sub, low, low_mid, mid, upper_mid, presence, air}
    tempo_bpm        REAL,
    loop_seam_score  REAL,
    is_music         INTEGER              -- 0/1, NULL until tagged
);

CREATE TABLE tags (
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    label    TEXT NOT NULL,
    score    REAL NOT NULL,
    model    TEXT NOT NULL,
    PRIMARY KEY (asset_id, model, label)
);

CREATE TABLE captions (
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    model    TEXT NOT NULL,              -- 'template-v1' for templated captions
    text     TEXT NOT NULL,
    PRIMARY KEY (asset_id, model)
);

CREATE TABLE embeddings (
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    model    TEXT NOT NULL,
    dim      INTEGER NOT NULL,
    vector   BLOB NOT NULL,              -- little-endian float32, L2-normalized
    PRIMARY KEY (asset_id, model)
);

CREATE TABLE images (
    asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,           -- 'overview' (waveform + spectrogram)
    local_path  TEXT NOT NULL,           -- relative to data_dir
    version     TEXT NOT NULL,           -- render settings version
    rendered_at TEXT NOT NULL,
    PRIMARY KEY (asset_id, kind)
);

-- Keyword half of hybrid search. Rebuilt per asset by foley.search.index_asset.
CREATE VIRTUAL TABLE search_fts USING fts5(
    name,          -- filename words (split on _ - . and camelCase)
    pack,
    category,
    metadata,      -- description-like metadata fields
    tags,
    caption,
    tokenize = 'unicode61 remove_diacritics 2'
);
-- search_fts.rowid = assets.id

CREATE TABLE archives (
    rel_path   TEXT PRIMARY KEY,
    size_bytes INTEGER NOT NULL,
    last_seen  TEXT NOT NULL
);

CREATE TABLE index_runs (
    id               INTEGER PRIMARY KEY,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    status           TEXT NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running', 'completed', 'interrupted', 'failed')),
    stages           TEXT,               -- JSON list of stages requested
    scanned          INTEGER NOT NULL DEFAULT 0,
    added            INTEGER NOT NULL DEFAULT 0,
    changed          INTEGER NOT NULL DEFAULT 0,
    moved            INTEGER NOT NULL DEFAULT 0,
    missing          INTEGER NOT NULL DEFAULT 0,
    analyzed         INTEGER NOT NULL DEFAULT 0,
    errors           INTEGER NOT NULL DEFAULT 0,
    settling         INTEGER NOT NULL DEFAULT 0,
    archives         INTEGER NOT NULL DEFAULT 0,
    note             TEXT
);

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
