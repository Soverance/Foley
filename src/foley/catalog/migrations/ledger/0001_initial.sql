-- ledger.db v1: human decisions and NAS actions. Never rebuilt; back it up.
-- Rows key on content_hash (not catalog asset ids) so they survive moves,
-- duplicate cleanup and a full catalog rebuild. rel_path is kept for readability.

CREATE TABLE decisions (
    id           INTEGER PRIMARY KEY,
    content_hash TEXT NOT NULL,
    rel_path     TEXT NOT NULL,
    project      TEXT NOT NULL,
    usage_key    TEXT NOT NULL,          -- game-side slot, e.g. sfx.weapon.laser.fire
    verdict      TEXT NOT NULL CHECK (verdict IN ('approved', 'rejected', 'replaced', 'auto')),
    gain_db      REAL,
    note         TEXT,
    decided_by   TEXT NOT NULL,          -- 'scott', 'claude-code', ...
    decided_at   TEXT NOT NULL
);
CREATE INDEX decisions_hash ON decisions(content_hash);
CREATE INDEX decisions_slot ON decisions(project, usage_key);

CREATE TABLE usages (
    id                 INTEGER PRIMARY KEY,
    content_hash       TEXT NOT NULL,
    rel_path           TEXT NOT NULL,
    project            TEXT NOT NULL,
    project_path       TEXT NOT NULL,    -- res:// path the file was copied to
    usage_key          TEXT,
    imported_at        TEXT NOT NULL,
    normalized_to_lufs REAL,
    gain_db            REAL,             -- gain baked in, or to apply via volume_db for lossy files
    gain_applied       INTEGER NOT NULL DEFAULT 0,  -- 1 if baked into the copied file
    license            TEXT
);
CREATE INDEX usages_hash ON usages(content_hash);
CREATE INDEX usages_project ON usages(project);

CREATE TABLE dedupe_actions (
    id              INTEGER PRIMARY KEY,
    plan_id         TEXT NOT NULL,
    action          TEXT NOT NULL CHECK (action IN ('quarantine', 'restore', 'purge')),
    content_hash    TEXT NOT NULL,
    rel_path        TEXT NOT NULL,
    survivor_path   TEXT,
    quarantine_path TEXT,
    size_bytes      INTEGER NOT NULL,
    acted_at        TEXT NOT NULL,
    acted_by        TEXT NOT NULL
);
CREATE INDEX dedupe_actions_plan ON dedupe_actions(plan_id);

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
