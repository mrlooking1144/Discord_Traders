-- Recovery Milestone R1: extend raw_messages with channel scoping, and
-- transition raw-message uniqueness to be channel-scoped.
--
-- Additive-only for columns and data: no column is removed and no row is
-- touched. The uniqueness *index* strategy is replaced, not layered
-- alongside the old one, because leaving both in place would let the old
-- source-wide (source_id, external_id) index keep blocking a message ID
-- from validly repeating across two different channels of the same
-- source - exactly the scenario channel-scoped ingestion requires.
--
-- Final policy (see docs/DECISIONS/0001_recovery_message_storage_and_lifecycle_schema.md):
--   - Within a real channel (channel_id IS NOT NULL): external_id must be
--     unique per channel. The same external_id MAY repeat across two
--     different channels of the same source.
--   - For rows with no channel (channel_id IS NULL - e.g. today's
--     manual-entry path, which never sets a channel): external_id must be
--     unique per source, exactly matching the pre-migration behavior, so
--     existing callers that never adopt channels keep the same duplicate
--     protection they had before.
--
-- The corresponding CREATE UNIQUE INDEX for the old policy was removed
-- from database/schema.sql's baseline (not merely dropped here), because
-- schema.sql is re-applied via executescript() on every
-- initialize_database() call and its "IF NOT EXISTS" would otherwise
-- silently recreate the old index again on the very next application
-- start. The DROP INDEX below still runs so an already-upgraded v0.1.0
-- database (where the old index was physically created under the old
-- schema.sql) has it removed exactly once.

ALTER TABLE raw_messages ADD COLUMN channel_id INTEGER REFERENCES channels(id);
ALTER TABLE raw_messages ADD COLUMN import_batch_id INTEGER REFERENCES import_batches(id);
ALTER TABLE raw_messages ADD COLUMN sequence_in_batch INTEGER;

DROP INDEX IF EXISTS idx_raw_messages_source_external_id;

CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_messages_channel_external_id
    ON raw_messages (channel_id, external_id)
    WHERE channel_id IS NOT NULL AND external_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_messages_null_channel_source_external_id
    ON raw_messages (source_id, external_id)
    WHERE channel_id IS NULL AND external_id IS NOT NULL;
