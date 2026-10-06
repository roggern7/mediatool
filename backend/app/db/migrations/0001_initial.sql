-- Schema inicial (contrato, seção 6). Nenhuma tabela guarda sessão ou cookies.

CREATE TABLE presets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    is_builtin  INTEGER NOT NULL DEFAULT 0 CHECK (is_builtin IN (0, 1)),
    params      TEXT    NOT NULL CHECK (json_valid(params)),
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE media (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_platform     TEXT    NOT NULL DEFAULT 'instagram',
    source_profile      TEXT,
    source_url          TEXT,
    source_post_id      TEXT,
    source_media_index  INTEGER NOT NULL DEFAULT 0,
    post_id             TEXT,
    media_id_remote     TEXT,
    media_type          TEXT    NOT NULL CHECK (media_type IN ('video', 'image')),
    original_filename   TEXT,
    original_path       TEXT    NOT NULL,
    original_sha256     TEXT    NOT NULL UNIQUE,
    file_size           INTEGER NOT NULL,
    duration            REAL,
    width               INTEGER,
    height              INTEGER,
    fps                 REAL,
    has_audio           INTEGER,
    mime_type           TEXT,
    thumbnail_path      TEXT,
    taken_at            TEXT,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL
);

CREATE UNIQUE INDEX ux_media_source
    ON media (source_platform, source_post_id, source_media_index)
    WHERE source_post_id IS NOT NULL;
CREATE INDEX ix_media_profile ON media (source_profile);
CREATE INDEX ix_media_type ON media (media_type);

CREATE TABLE renditions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id          INTEGER NOT NULL REFERENCES media (id) ON DELETE CASCADE,
    preset_id         INTEGER REFERENCES presets (id) ON DELETE SET NULL,
    preset_name       TEXT,
    params            TEXT    NOT NULL CHECK (json_valid(params)),
    job_id            INTEGER,
    processed_path    TEXT    NOT NULL,
    processed_sha256  TEXT    NOT NULL,
    file_size         INTEGER NOT NULL,
    duration          REAL,
    width             INTEGER,
    height            INTEGER,
    fps               REAL,
    has_audio         INTEGER,
    created_at        TEXT    NOT NULL
);

CREATE INDEX ix_renditions_media ON renditions (media_id);

CREATE TABLE batches (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source           TEXT    NOT NULL CHECK (source IN ('instagram_import', 'library')),
    source_profile   TEXT,
    content_type     TEXT,
    requested_limit  INTEGER,
    preset_id        INTEGER REFERENCES presets (id) ON DELETE SET NULL,
    preset_name      TEXT,
    params           TEXT    CHECK (params IS NULL OR json_valid(params)),
    process          INTEGER NOT NULL DEFAULT 0 CHECK (process IN (0, 1)),
    status           TEXT    NOT NULL
                     CHECK (status IN ('listing', 'running', 'completed', 'failed', 'cancelled')),
    found            INTEGER NOT NULL DEFAULT 0,
    error_code       TEXT,
    error_message    TEXT,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL,
    finished_at      TEXT
);

CREATE INDEX ix_batches_status ON batches (status);

CREATE TABLE jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id            INTEGER REFERENCES batches (id),
    media_id            INTEGER REFERENCES media (id) ON DELETE SET NULL,
    rendition_id        INTEGER REFERENCES renditions (id) ON DELETE SET NULL,
    kind                TEXT    NOT NULL CHECK (kind IN ('ingest', 'process')),
    preset_id           INTEGER REFERENCES presets (id) ON DELETE SET NULL,
    preset_name         TEXT,
    params              TEXT    CHECK (params IS NULL OR json_valid(params)),
    status              TEXT    NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'downloading', 'downloaded', 'processing',
                                          'completed', 'failed', 'cancelled')),
    stage               TEXT,
    progress            REAL    CHECK (progress IS NULL OR (progress >= 0 AND progress <= 1)),
    error_code          TEXT,
    error_message       TEXT,
    attempts            INTEGER NOT NULL DEFAULT 0,
    max_attempts        INTEGER NOT NULL DEFAULT 3,
    next_attempt_at     TEXT,
    cancel_requested    INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
    is_duplicate        INTEGER NOT NULL DEFAULT 0 CHECK (is_duplicate IN (0, 1)),
    duplicate_reason    TEXT    CHECK (duplicate_reason IS NULL OR duplicate_reason IN ('source_id', 'sha256')),
    source_platform     TEXT,
    source_profile      TEXT,
    source_url          TEXT,
    source_post_id      TEXT,
    source_media_index  INTEGER,
    post_id             TEXT,
    media_id_remote     TEXT,
    media_type          TEXT,
    media_url           TEXT,
    thumbnail_url       TEXT,
    taken_at            TEXT,
    temp_path           TEXT,
    created_at          TEXT    NOT NULL,
    started_at          TEXT,
    finished_at         TEXT,
    updated_at          TEXT    NOT NULL,
    heartbeat_at        TEXT
);

CREATE UNIQUE INDEX ux_jobs_batch_source
    ON jobs (batch_id, source_post_id, source_media_index)
    WHERE kind = 'ingest' AND batch_id IS NOT NULL AND source_post_id IS NOT NULL;
CREATE INDEX ix_jobs_claim ON jobs (status, next_attempt_at, id);
CREATE INDEX ix_jobs_batch ON jobs (batch_id);
CREATE INDEX ix_jobs_media ON jobs (media_id);

CREATE TABLE settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL CHECK (json_valid(value)),
    updated_at  TEXT NOT NULL
);

CREATE TABLE app_state (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT NOT NULL
);

INSERT INTO presets (name, is_builtin, params, created_at, updated_at) VALUES
    ('Padrão', 1,
     '{"metadata_cleanup": true, "trim_start": 0.5, "mirror": true, "speed": 1.05, "auto_adjust": true, "reencode": true}',
     strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ('Anti-duplicidade', 1,
     '{"metadata_cleanup": true, "trim_start": 0.5, "mirror": true, "speed": 1.05, "auto_adjust": true, "reencode": true}',
     strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
