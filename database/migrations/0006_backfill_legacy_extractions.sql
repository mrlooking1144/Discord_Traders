-- Recovery Milestone R1: backfill one synthetic "legacy" message_extractions
-- row per pre-existing raw_messages row, so join-based parse-status/
-- checkpoint queries never break for v0.1.0 data. On a fresh database this
-- inserts zero rows, since raw_messages is empty at migration time.

INSERT INTO message_extractions
    (raw_message_id, parser_version, parse_status, confidence, ambiguity_flags, is_current, created_at)
SELECT
    rm.id, 'legacy', 'parsed', NULL, NULL, 1, rm.ingested_at
FROM raw_messages rm
WHERE NOT EXISTS (
    SELECT 1 FROM message_extractions me WHERE me.raw_message_id = rm.id
);
