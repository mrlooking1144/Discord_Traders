-- Recovery Milestone R1: extend trade_signals with the fields the noisy-text
-- extractor (a later milestone) will populate. Additive-only: existing rows
-- get NULL for every new column. action keeps storing the raw verb
-- unchanged (BOUGHT/SOLD are added to the vocabulary in a later milestone,
-- not aliased to BTO/STC/BTC/STO/BUY/SELL).
--
-- lifecycle_id is intentionally NOT added here: trade_lifecycles is an R6
-- deliverable, out of scope for this R1 schema-only milestone. It will be
-- added as its own additive migration alongside that table.

ALTER TABLE trade_signals ADD COLUMN strike TEXT;
ALTER TABLE trade_signals ADD COLUMN expiration_raw TEXT;
ALTER TABLE trade_signals ADD COLUMN event_type TEXT;
ALTER TABLE trade_signals ADD COLUMN qualifier TEXT;
ALTER TABLE trade_signals ADD COLUMN stated_entry_price TEXT;
ALTER TABLE trade_signals ADD COLUMN stated_return_pct TEXT;
ALTER TABLE trade_signals ADD COLUMN notes TEXT;
ALTER TABLE trade_signals ADD COLUMN extraction_id INTEGER REFERENCES message_extractions(id);

CREATE INDEX IF NOT EXISTS idx_trade_signals_extraction_id
    ON trade_signals (extraction_id);
