-- Recovery Milestone R1: add message_extractions.
-- One row per parse *attempt* against a raw message. Reprocessing (a later
-- milestone) inserts a new row and marks the prior current row superseded;
-- raw_messages itself is never modified. The partial unique index below
-- enforces at most one current (is_current = 1) extraction per raw message
-- at the database level.

CREATE TABLE IF NOT EXISTS message_extractions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_message_id  INTEGER NOT NULL REFERENCES raw_messages(id),
    parser_version  TEXT NOT NULL,
    parse_status    TEXT NOT NULL
        CHECK (parse_status IN ('parsed', 'partially_parsed', 'unrecognized', 'failed')),
    confidence      REAL
        CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    ambiguity_flags TEXT,
    is_current      INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
    superseded_at   TEXT,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_message_extractions_raw_message_id
    ON message_extractions (raw_message_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_message_extractions_current
    ON message_extractions (raw_message_id)
    WHERE is_current = 1;
