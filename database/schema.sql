-- Discord Traders — Version 1 database schema.
-- Source of truth: docs/DATABASE_DESIGN_V1.md (Milestone 2A, approved/frozen).
-- Implements Milestone 2B.1 of docs/IMPLEMENTATION_ROADMAP.md.
-- Do not add tables, columns, or constraints beyond what that design specifies.

CREATE TABLE IF NOT EXISTS sources (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS traders (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id          INTEGER NOT NULL REFERENCES sources(id),
    name               TEXT NOT NULL,
    external_trader_id TEXT,
    created_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_traders_source_external_id
    ON traders (source_id, external_trader_id)
    WHERE external_trader_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_traders_source_name
    ON traders (source_id, name);

CREATE TABLE IF NOT EXISTS raw_messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id    INTEGER NOT NULL REFERENCES sources(id),
    external_id  TEXT,
    raw_text     TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    metadata     TEXT,
    received_at  TEXT,
    ingested_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_messages_source_external_id
    ON raw_messages (source_id, external_id)
    WHERE external_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_raw_messages_content_hash
    ON raw_messages (content_hash);

CREATE TABLE IF NOT EXISTS trade_signals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_message_id INTEGER NOT NULL REFERENCES raw_messages(id),
    trader_id      INTEGER NOT NULL REFERENCES traders(id),
    symbol         TEXT NOT NULL,
    action         TEXT NOT NULL,
    option_type    TEXT,
    price          TEXT,
    expiration     TEXT,
    position_size  TEXT,
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_trade_signals_raw_message_id
    ON trade_signals (raw_message_id);

CREATE INDEX IF NOT EXISTS idx_trade_signals_trader_id
    ON trade_signals (trader_id);

CREATE INDEX IF NOT EXISTS idx_trade_signals_symbol
    ON trade_signals (symbol);

CREATE TABLE IF NOT EXISTS trade_signal_edits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_signal_id INTEGER NOT NULL REFERENCES trade_signals(id),
    previous_values TEXT NOT NULL,
    edited_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_trade_signal_edits_trade_signal_id
    ON trade_signal_edits (trade_signal_id);
