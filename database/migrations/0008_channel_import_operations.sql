-- Recovery Milestone R9a: add channel_import_operations.
-- Source of truth: the approved R9a planning contract (Recovery Milestone
-- R9 - Bulk Channel Import UI - planning correspondence).
--
-- One row per successfully completed confirmed Bulk Channel Import
-- operation for one channel - never a failed or rolled-back attempt. A
-- failed/rolled-back attempt is never persisted here: the atomic
-- ingest-plus-lifecycle-rebuild transaction that will insert this row
-- (Recovery Milestone R9b, not yet implemented) inserts it only as its
-- own final step, inside the same transaction as the ingestion and
-- lifecycle rebuild it summarizes - so this table's mere row existence
-- already is the success signal. No status, started_at, exception
-- information, or raw pasted text is stored here - that would require a
-- separate post-rollback transaction and is explicitly out of scope for
-- Recovery Milestone R9 (see the approved planning correspondence).
--
-- import_batch_id is nullable exactly for a duplicate-only successful
-- operation (every segmented message already stored, so no
-- import_batches row was created for it), mirroring import_batches'
-- own pre-existing "not created when nothing new" contract from
-- Recovery Milestone R5.

CREATE TABLE IF NOT EXISTS channel_import_operations (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id            INTEGER NOT NULL REFERENCES channels(id),
    import_batch_id       INTEGER REFERENCES import_batches(id),
    reference_date        TEXT NOT NULL,
    timezone              TEXT NOT NULL,
    processed_count       INTEGER NOT NULL CHECK (processed_count >= 0),
    stored_count          INTEGER NOT NULL CHECK (stored_count >= 0),
    duplicate_count       INTEGER NOT NULL CHECK (duplicate_count >= 0),
    unrecognized_count    INTEGER NOT NULL CHECK (unrecognized_count >= 0),
    failed_count          INTEGER NOT NULL CHECK (failed_count >= 0),
    committed_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Recovery Milestone R9's own approved minimum-batch-size floor
    -- (Bulk Channel Import rejects a paste segmenting into fewer than 15
    -- messages before ever reaching a confirmed operation), enforced at
    -- the database level, not only in service-layer validation.
    CHECK (processed_count >= 15),
    CHECK (stored_count + duplicate_count = processed_count),
    CHECK (unrecognized_count <= stored_count),
    CHECK (failed_count <= stored_count),
    CHECK (unrecognized_count + failed_count <= stored_count),

    -- A duplicate-only successful operation (stored_count = 0) never has
    -- an import_batches row (Recovery Milestone R5's own contract) -
    -- any operation that stored at least one new message always does.
    CHECK (
        (stored_count = 0 AND import_batch_id IS NULL)
        OR
        (stored_count > 0 AND import_batch_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_channel_import_operations_channel_id
    ON channel_import_operations (channel_id);
