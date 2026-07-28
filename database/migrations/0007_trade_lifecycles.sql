-- Recovery Milestone R6.1: add trade_lifecycles and trade_lifecycle_events,
-- and trade_signals.lifecycle_id.
-- Source of truth: docs/DECISIONS/0002_trade_lifecycle_schema.md
--
-- Schema/migration only - no matching/linking logic exists yet (that is
-- R6.2-R6.4). This migration performs no backfill: every pre-existing
-- trade_signals row gets lifecycle_id = NULL via the new column's default,
-- exactly as every other additive column in this project's migrations has
-- left pre-existing rows NULL until a later milestone populates them.
--
-- trade_lifecycles: one row per lifecycle *generation*. A generation is
-- never edited in place once created, aside from is_current/superseded_at -
-- reprocessing or a key-changing correction supersedes the old row and
-- inserts a fresh one, exactly mirroring message_extractions'
-- is_current/superseded_at contract (database/migrations/0002_message_extractions.sql).
-- remaining_fraction stores the exact string form of a rational
-- (fractions.Fraction), not a Decimal string, since several approved
-- fraction tokens (1/3, 1/6) do not terminate in base 10.

CREATE TABLE IF NOT EXISTS trade_lifecycles (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trader_id           INTEGER NOT NULL REFERENCES traders(id),
    symbol              TEXT NOT NULL,
    option_type         TEXT,
    strike              TEXT,
    expiration          TEXT,
    status              TEXT NOT NULL
        CHECK (status IN ('open', 'partially_closed', 'closed', 'orphan', 'unresolved', 'invalid')),
    remaining_fraction  TEXT NOT NULL,
    opened_by_signal_id INTEGER REFERENCES trade_signals(id),
    closed_by_signal_id INTEGER REFERENCES trade_signals(id),
    is_current          INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
    superseded_at       TEXT,
    ambiguity_flags     TEXT,
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_trade_lifecycles_key
    ON trade_lifecycles (trader_id, symbol, option_type, strike, expiration, is_current);

-- trade_lifecycle_events: the membership/audit table linking one
-- trade_lifecycles generation to the trade_signals rows that made it up,
-- in chronological order (sequence_index). signal_snapshot is an
-- immutable, write-once canonical JSON capture of that signal's field
-- values (and the ordering key used) at the moment this membership row is
-- created - required because the existing 2D.5 correction workflow
-- (TradeService.update_trade_signal()) edits trade_signals fields in
-- place, so a later join to the live row would otherwise show corrected
-- values instead of the values the generation was actually built from.
-- No repository function ever updates or deletes a row in this table,
-- matching raw_messages.raw_text's existing write-once contract.

CREATE TABLE IF NOT EXISTS trade_lifecycle_events (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_lifecycle_id INTEGER NOT NULL REFERENCES trade_lifecycles(id),
    trade_signal_id    INTEGER NOT NULL REFERENCES trade_signals(id),
    sequence_index     INTEGER NOT NULL,
    signal_snapshot    TEXT NOT NULL,
    created_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_lifecycle_events_unique_membership
    ON trade_lifecycle_events (trade_lifecycle_id, trade_signal_id);

CREATE INDEX IF NOT EXISTS idx_trade_lifecycle_events_signal_id
    ON trade_lifecycle_events (trade_signal_id);

-- trade_signals.lifecycle_id: a maintained pointer to the signal's current
-- lifecycle generation, or NULL. The one narrow, explicitly-scoped
-- exception to trade_signals' otherwise strict immutability - written only
-- by the lifecycle engine (a later R6 slice), never by ingestion,
-- reprocessing-of-extraction, or the correction workflow directly. NULL
-- for every pre-existing row and for every legacy (event_type IS NULL)
-- signal, permanently.

ALTER TABLE trade_signals ADD COLUMN lifecycle_id INTEGER REFERENCES trade_lifecycles(id);

CREATE INDEX IF NOT EXISTS idx_trade_signals_lifecycle_id ON trade_signals (lifecycle_id);
