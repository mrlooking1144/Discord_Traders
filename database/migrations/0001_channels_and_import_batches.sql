-- Recovery Milestone R1: add the channels and import_batches tables.
-- Source of truth: docs/DECISIONS/0001_recovery_message_storage_and_lifecycle_schema.md
-- channels lets a raw message be scoped to a specific source channel, which
-- is required for the "idempotent ingestion using channel ID plus message
-- ID" and per-channel checkpoint requirements. import_batches anchors the
-- reference_date/timezone used to resolve year-less expirations and
-- "Today at HH:MM" timestamps for every message segmented from one paste.

CREATE TABLE IF NOT EXISTS channels (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id           INTEGER NOT NULL REFERENCES sources(id),
    external_channel_id TEXT,
    name                TEXT,
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_channels_source_external_id
    ON channels (source_id, external_channel_id)
    WHERE external_channel_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS import_batches (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id      INTEGER NOT NULL REFERENCES sources(id),
    reference_date TEXT NOT NULL,
    timezone       TEXT NOT NULL,
    raw_input_text TEXT,
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_import_batches_source_id
    ON import_batches (source_id);
