"""Real-SQLite integration tests for Recovery Milestone R5.

Exercises the complete batch-ingestion, reprocessing, checkpoint, and
current-signal-review pipeline against a real, temp-file SQLite database
(via database/db.py + database/schema.sql, exactly as production code will
use them) - following the same real-I/O philosophy as
tests/test_integration_transactions.py and
tests/test_manual_entry_integration.py: TradeService's new R5 methods each
own and commit their own transaction, so durability is verified through a
second, independent connection wherever it matters, not just the
originating one.

tests/test_service.py already covers the full R5 contract at the
unit-test level (validation, trader identity, timestamps, provenance, the
transaction context manager's failure paths, etc.) against a real
temp-file database on a single connection. This file focuses on what that
level of testing cannot show: durability across connections, the full
real 68-message corpus flowing through every table with correct foreign
keys, and R5/legacy compatibility observed from a fresh connection.
"""

import os
import tempfile
import unittest
from decimal import Decimal

from database.config import DatabaseConfig
from database.db import get_connection, initialize_database
from database.repository import get_current_extraction, get_raw_message_by_id
from database.service import TradeService
from tests.discord_corpus_fixture import CORPUS


class _BatchIntegrationTestCase(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = path
        self.config = DatabaseConfig(db_path=path)
        initialize_database(self.config)
        self.connection = get_connection(self.config)
        self.service = TradeService(self.connection)

    def tearDown(self):
        self.connection.close()
        os.remove(self.db_path)

    def _fresh_connection(self):
        return get_connection(self.config)


class FullCorpusBatchIngestionIntegrationTests(_BatchIntegrationTestCase):
    """The complete real 68-message Discord corpus, end to end, verified
    through a fresh connection for true commit durability."""

    def test_full_corpus_persists_every_table_with_correct_relationships(self):
        result = self.service.ingest_batch(
            source_name="discord",
            reference_date="2026-07-24",
            timezone="Asia/Riyadh",
            raw_batch_text=CORPUS,
            channel_external_id="chan-integration",
            channel_name="integration-test-channel",
        )

        verify = self._fresh_connection()
        try:
            counts = {
                table: verify.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "sources", "channels", "import_batches", "raw_messages",
                    "message_extractions", "trade_signals", "traders",
                )
            }
            self.assertEqual(counts["sources"], 1)
            self.assertEqual(counts["channels"], 1)
            self.assertEqual(counts["import_batches"], 1)
            self.assertEqual(counts["raw_messages"], 68)
            self.assertEqual(counts["message_extractions"], 68)
            # Every one of the 68 real corpus alerts reaches PARSE_STATUS_PARSED
            # (per the R3 handoff's own FullCorpusExtractionAcceptanceTests),
            # and every one has a resolvable trader (Bdorts/TC/spacemonkey/
            # Matae/Sarang) - the whole corpus reaches trade_signals.
            self.assertEqual(counts["trade_signals"], 68)
            self.assertEqual(counts["traders"], 5)

            raw_message_row = verify.execute(
                "SELECT id, source_id, channel_id, import_batch_id, sequence_in_batch "
                "FROM raw_messages ORDER BY id LIMIT 1"
            ).fetchone()
            self.assertEqual(raw_message_row["import_batch_id"], result.import_batch_id)
            self.assertEqual(raw_message_row["sequence_in_batch"], 1)

            extraction_row = verify.execute(
                "SELECT id, raw_message_id, parser_version, parse_status, is_current "
                "FROM message_extractions WHERE raw_message_id = ?",
                (raw_message_row["id"],),
            ).fetchone()
            self.assertEqual(extraction_row["parser_version"], "v2")
            self.assertEqual(extraction_row["parse_status"], "parsed")
            self.assertEqual(extraction_row["is_current"], 1)

            signal_row = verify.execute(
                "SELECT raw_message_id, trader_id, extraction_id, symbol, action, strike "
                "FROM trade_signals WHERE raw_message_id = ?",
                (raw_message_row["id"],),
            ).fetchone()
            self.assertEqual(signal_row["extraction_id"], extraction_row["id"])
            self.assertEqual(signal_row["symbol"], "AVGO")
            self.assertEqual(signal_row["action"], "BOUGHT")
            self.assertEqual(Decimal(signal_row["strike"]), Decimal("380"))
        finally:
            verify.close()

    def test_full_duplicate_reimport_produces_zero_persistent_changes(self):
        self.service.ingest_batch(
            source_name="discord", reference_date="2026-07-24", timezone="Asia/Riyadh",
            raw_batch_text=CORPUS, channel_external_id="chan-integration-2",
        )

        def _snapshot(conn):
            return {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "sources", "channels", "import_batches", "raw_messages",
                    "message_extractions", "trade_signals", "traders",
                )
            }

        verify = self._fresh_connection()
        try:
            before = _snapshot(verify)
        finally:
            verify.close()

        result2 = self.service.ingest_batch(
            source_name="discord", reference_date="2026-07-24", timezone="Asia/Riyadh",
            raw_batch_text=CORPUS, channel_external_id="chan-integration-2",
        )

        verify2 = self._fresh_connection()
        try:
            after = _snapshot(verify2)
        finally:
            verify2.close()

        self.assertIsNone(result2.import_batch_id)
        self.assertEqual(before, after)


class ReprocessingIntegrationTests(_BatchIntegrationTestCase):
    def test_reprocessing_is_durable_and_supersedes_via_fresh_connection(self):
        result = self.service.ingest_batch(
            source_name="discord", reference_date="2026-07-24", timezone="Asia/Riyadh",
            raw_batch_text=CORPUS, channel_external_id="chan-reproc-integration",
        )
        first_message_id = result.messages[0].raw_message_id
        original_signal_id = result.messages[0].trade_signal_ids[0]

        reprocessed = self.service.reprocess_raw_message(first_message_id)

        verify = self._fresh_connection()
        try:
            current = get_current_extraction(verify, first_message_id)
            self.assertEqual(current.id, reprocessed.new_extraction_id)

            old_extraction_row = verify.execute(
                "SELECT is_current, superseded_at FROM message_extractions WHERE id = ?",
                (reprocessed.previous_extraction_id,),
            ).fetchone()
            self.assertEqual(old_extraction_row["is_current"], 0)
            self.assertIsNotNone(old_extraction_row["superseded_at"])

            # Old signal remains, untouched, for audit.
            old_signal_row = verify.execute(
                "SELECT id, symbol, extraction_id FROM trade_signals WHERE id = ?",
                (original_signal_id,),
            ).fetchone()
            self.assertIsNotNone(old_signal_row)
            self.assertEqual(old_signal_row["extraction_id"], reprocessed.previous_extraction_id)

            # raw_text and provenance are immutable.
            raw_message = get_raw_message_by_id(verify, first_message_id)
            self.assertIn("BOUGHT AVGO 07/24 380P $1.14 [SMALL]", raw_message.raw_text)
        finally:
            verify.close()

    def test_import_batch_reprocessing_is_durable(self):
        result = self.service.ingest_batch(
            source_name="discord", reference_date="2026-07-24", timezone="Asia/Riyadh",
            raw_batch_text=CORPUS, channel_external_id="chan-batch-reproc-integration",
        )

        batch_result = self.service.reprocess_import_batch(result.import_batch_id)

        verify = self._fresh_connection()
        try:
            current_count = verify.execute(
                "SELECT COUNT(*) FROM message_extractions WHERE is_current = 1"
            ).fetchone()[0]
            total_extraction_count = verify.execute(
                "SELECT COUNT(*) FROM message_extractions"
            ).fetchone()[0]
            self.assertEqual(current_count, 68)
            self.assertEqual(total_extraction_count, 136)
            self.assertEqual(len(batch_result.outcomes), 68)
        finally:
            verify.close()


class CurrentSignalReviewIntegrationTests(_BatchIntegrationTestCase):
    def test_review_shows_only_current_signals_after_reprocessing(self):
        result = self.service.ingest_batch(
            source_name="discord", reference_date="2026-07-24", timezone="Asia/Riyadh",
            raw_batch_text=CORPUS, channel_external_id="chan-review-integration",
        )
        first_message_id = result.messages[0].raw_message_id
        original_signal_id = result.messages[0].trade_signal_ids[0]

        reprocessed = self.service.reprocess_raw_message(first_message_id)

        verify = self._fresh_connection()
        try:
            review_service = TradeService(verify)
            visible_ids = {
                row["id"] for row in review_service.list_trade_signals_for_review(limit=1000)
            }
            self.assertNotIn(original_signal_id, visible_ids)
            self.assertIn(reprocessed.new_trade_signal_ids[0], visible_ids)
        finally:
            verify.close()

    def test_legacy_and_r5_signals_coexist_in_review_from_fresh_connection(self):
        self.service.ingest_message(
            "manual", "alice", "BTO SPY 500c @3.25",
            reference_time="2026-07-13 09:00:00",
            trade_signals=[{"symbol": "SPY", "action": "BTO"}],
        )
        self.connection.commit()

        self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw="bob",
            raw_text="BOUGHT AAPL 07/24 200C $2.00",
            cleaned_text="BOUGHT AAPL 07/24 200C $2.00",
            synthetic_id_input="s-legacy-mix-integration",
            reference_date="2026-07-24", timezone="UTC",
        )

        verify = self._fresh_connection()
        try:
            review_service = TradeService(verify)
            symbols = {
                row["symbol"] for row in review_service.list_trade_signals_for_review()
            }
            self.assertIn("SPY", symbols)
            self.assertIn("AAPL", symbols)
        finally:
            verify.close()


class ChannelCheckpointIntegrationTests(_BatchIntegrationTestCase):
    def test_checkpoint_reflects_full_corpus_via_fresh_connection(self):
        result = self.service.ingest_batch(
            source_name="discord", reference_date="2026-07-24", timezone="Asia/Riyadh",
            raw_batch_text=CORPUS, channel_external_id="chan-checkpoint-integration",
        )
        last_message = result.messages[-1]

        verify = self._fresh_connection()
        try:
            checkpoint_service = TradeService(verify)
            checkpoints = checkpoint_service.get_channel_checkpoints()
            checkpoint = next(c for c in checkpoints if c.channel_id == result.channel_id)

            self.assertEqual(
                checkpoint.last_ingested_raw_message_id, last_message.raw_message_id
            )
            self.assertEqual(checkpoint.last_import_batch_id, result.import_batch_id)
            self.assertIsNotNone(checkpoint.latest_received_at)
        finally:
            verify.close()


if __name__ == "__main__":
    unittest.main()
