-- Recovery Milestone R1: add traders.canonical_name for case-insensitive
-- trader identity resolution (e.g. "Matae" and "matae" are the same
-- trader). Additive-only, not unique: duplicate (source_id, name) rows are
-- already allowed today and remain allowed - this column only makes
-- case-insensitive lookup possible for a later ingestion milestone. Backfill
-- covers every pre-existing row.

ALTER TABLE traders ADD COLUMN canonical_name TEXT;

CREATE INDEX IF NOT EXISTS idx_traders_source_canonical_name
    ON traders (source_id, canonical_name);

UPDATE traders SET canonical_name = LOWER(TRIM(name)) WHERE canonical_name IS NULL;
