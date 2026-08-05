"""Tests for Recovery Milestone R9a's Bulk Channel Import backend read
contracts on TradeService.

Covers exactly the four new, strictly read-only public methods added to
database/service.py: list_bulk_import_channels(),
get_bulk_import_channel_summary(), check_new_channel_external_id_availability(),
and predict_channel_import_duplicate_statuses() - plus
database.repository.compute_content_hash() (promoted from a private
repository-only helper to the one shared content-hash implementation
both database/repository.py and database/service.py import and call)
and _duplicate_outcome()'s continued correctness after being updated to
call it.

Recovery Milestone R9b's atomic ingest-plus-lifecycle-rebuild transaction
(_ingest_batch_no_commit(), import_channel_batch_with_lifecycle_rebuild())
is explicitly out of scope here and is not implemented - every fixture in
this file uses only already-existing, already-tested TradeService/
database.repository calls (ingest_batch(), create_channel(),
create_channel_import_operation(), etc.) to build realistic state for
these read-only methods to read back.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from app.discord_adapter import segment_discord_batch
from database.config import DatabaseConfig
from database.db import get_connection, initialize_database
from database.models import (
    ChannelExternalIdAvailability,
    ChannelImportChannelSummary,
    ChannelImportDuplicatePrediction,
)
from database.repository import (
    UNSPECIFIED_CHANNEL_EXTERNAL_ID,
    create_channel,
    create_channel_import_operation,
    get_or_create_channel,
    get_or_create_source,
    get_or_create_unspecified_channel,
)
from database.service import TradeService, _resolve_external_id


class _BulkChannelImportServiceTestCase(unittest.TestCase):
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

    def _counts(self):
        return {
            table: self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "sources", "channels", "raw_messages", "trade_signals",
                "channel_import_operations", "schema_migrations",
            )
        }


class ListBulkImportChannelsServiceTests(_BulkChannelImportServiceTestCase):
    def test_missing_source_returns_empty_list_and_performs_no_write(self):
        before = self._counts()

        result = self.service.list_bulk_import_channels(source_name="discord")

        self.assertEqual(result, [])
        self.assertEqual(self._counts(), before)

    def test_empty_channel_appears_in_selectable_channels(self):
        source = get_or_create_source(self.connection, "discord")
        create_channel(self.connection, source.id, external_channel_id="chan-empty")
        self.connection.commit()

        result = self.service.list_bulk_import_channels(source_name="discord")

        self.assertEqual(len(result), 1)
        summary = result[0]
        self.assertIsInstance(summary, ChannelImportChannelSummary)
        self.assertEqual(summary.channel.external_channel_id, "chan-empty")
        self.assertIsNone(summary.checkpoint)
        self.assertIsNone(summary.latest_operation)

    def test_unspecified_sentinel_is_excluded(self):
        source = get_or_create_source(self.connection, "discord")
        get_or_create_unspecified_channel(self.connection, source.id)
        create_channel(self.connection, source.id, external_channel_id="real-chan")
        self.connection.commit()

        result = self.service.list_bulk_import_channels(source_name="discord")

        external_ids = {s.channel.external_channel_id for s in result}
        self.assertEqual(external_ids, {"real-chan"})
        self.assertNotIn("__unspecified__", external_ids)

    def test_includes_real_checkpoint_when_available(self):
        self.service.ingest_batch(
            source_name="discord", reference_date="2026-01-01", timezone="UTC",
            raw_batch_text=_batch_of_15(), channel_external_id="chan-with-messages",
        )

        result = self.service.list_bulk_import_channels(source_name="discord")

        summary = next(
            s for s in result if s.channel.external_channel_id == "chan-with-messages"
        )
        self.assertIsNotNone(summary.checkpoint)

    def test_includes_latest_operation_when_available(self):
        source = get_or_create_source(self.connection, "discord")
        channel = create_channel(self.connection, source.id, external_channel_id="chan-op")
        self.connection.commit()
        create_channel_import_operation(
            self.connection, channel_id=channel.id, import_batch_id=None,
            reference_date="2026-01-01", timezone="UTC", processed_count=15,
            stored_count=0, duplicate_count=15, unrecognized_count=0, failed_count=0,
        )
        self.connection.commit()

        result = self.service.list_bulk_import_channels(source_name="discord")

        summary = next(s for s in result if s.channel.external_channel_id == "chan-op")
        self.assertIsNotNone(summary.latest_operation)
        self.assertEqual(summary.latest_operation.stored_count, 0)
        self.assertEqual(summary.latest_operation.duplicate_count, 15)

    def test_latest_operation_reflects_only_the_most_recent_row(self):
        source = get_or_create_source(self.connection, "discord")
        channel = create_channel(self.connection, source.id, external_channel_id="chan-multi")
        self.connection.commit()
        create_channel_import_operation(
            self.connection, channel_id=channel.id, import_batch_id=None,
            reference_date="2026-01-01", timezone="UTC", processed_count=15,
            stored_count=0, duplicate_count=15, unrecognized_count=0, failed_count=0,
        )
        self.connection.commit()
        create_channel_import_operation(
            self.connection, channel_id=channel.id, import_batch_id=None,
            reference_date="2026-01-02", timezone="UTC", processed_count=20,
            stored_count=0, duplicate_count=20, unrecognized_count=0, failed_count=0,
        )
        self.connection.commit()

        result = self.service.list_bulk_import_channels(source_name="discord")

        summary = next(s for s in result if s.channel.external_channel_id == "chan-multi")
        # Latest-operation counts are not lifetime totals - only the
        # second (most recent) row's own values are reflected.
        self.assertEqual(summary.latest_operation.processed_count, 20)

    def test_never_calls_get_or_create_source_or_channel(self):
        source = get_or_create_source(self.connection, "discord")
        create_channel(self.connection, source.id, external_channel_id="chan-1")
        self.connection.commit()

        with patch(
            "database.service.get_or_create_source"
        ) as mock_get_or_create_source, patch(
            "database.service.get_or_create_channel"
        ) as mock_get_or_create_channel:
            self.service.list_bulk_import_channels(source_name="discord")

        mock_get_or_create_source.assert_not_called()
        mock_get_or_create_channel.assert_not_called()

    def test_no_write_transaction_opened(self):
        get_or_create_source(self.connection, "discord")
        self.connection.commit()

        self.service.list_bulk_import_channels(source_name="discord")

        self.assertFalse(self.connection.in_transaction)

    def test_calls_get_channel_checkpoints_exactly_once_for_multiple_channels(self):
        source = get_or_create_source(self.connection, "discord")
        create_channel(self.connection, source.id, external_channel_id="chan-a")
        create_channel(self.connection, source.id, external_channel_id="chan-b")
        create_channel(self.connection, source.id, external_channel_id="chan-c")
        self.connection.commit()

        with patch.object(
            TradeService,
            "get_channel_checkpoints",
            wraps=self.service.get_channel_checkpoints,
        ) as spy:
            result = self.service.list_bulk_import_channels(source_name="discord")

        self.assertEqual(len(result), 3)
        spy.assert_called_once()


class GetBulkImportChannelSummaryServiceTests(_BulkChannelImportServiceTestCase):
    def test_missing_source_returns_none(self):
        result = self.service.get_bulk_import_channel_summary(
            source_name="discord", channel_id=1
        )
        self.assertIsNone(result)

    def test_missing_channel_id_returns_none(self):
        get_or_create_source(self.connection, "discord")
        self.connection.commit()

        result = self.service.get_bulk_import_channel_summary(
            source_name="discord", channel_id=999999
        )
        self.assertIsNone(result)

    def test_channel_from_another_source_returns_none(self):
        get_or_create_source(self.connection, "discord")
        other_source = get_or_create_source(self.connection, "telegram")
        other_channel = create_channel(
            self.connection, other_source.id, external_channel_id="telegram-chan"
        )
        self.connection.commit()

        result = self.service.get_bulk_import_channel_summary(
            source_name="discord", channel_id=other_channel.id
        )
        self.assertIsNone(result)

    def test_unspecified_sentinel_returns_none(self):
        source = get_or_create_source(self.connection, "discord")
        sentinel = get_or_create_unspecified_channel(self.connection, source.id)
        self.connection.commit()

        result = self.service.get_bulk_import_channel_summary(
            source_name="discord", channel_id=sentinel.id
        )
        self.assertIsNone(result)

    def test_valid_channel_returns_summary(self):
        source = get_or_create_source(self.connection, "discord")
        channel = create_channel(self.connection, source.id, external_channel_id="chan-x")
        self.connection.commit()

        result = self.service.get_bulk_import_channel_summary(
            source_name="discord", channel_id=channel.id
        )

        self.assertIsInstance(result, ChannelImportChannelSummary)
        self.assertEqual(result.channel.id, channel.id)
        self.assertIsNone(result.checkpoint)
        self.assertIsNone(result.latest_operation)

    def test_never_creates_a_missing_channel(self):
        get_or_create_source(self.connection, "discord")
        self.connection.commit()
        before = self._counts()

        self.service.get_bulk_import_channel_summary(
            source_name="discord", channel_id=999999
        )

        self.assertEqual(self._counts(), before)


class CheckNewChannelExternalIdAvailabilityServiceTests(_BulkChannelImportServiceTestCase):
    def test_available_when_source_does_not_exist(self):
        result = self.service.check_new_channel_external_id_availability(
            source_name="discord", external_channel_id="brand-new"
        )
        self.assertIsInstance(result, ChannelExternalIdAvailability)
        self.assertTrue(result.is_available)
        self.assertIsNone(result.existing_channel)

    def test_available_when_source_exists_but_channel_does_not(self):
        get_or_create_source(self.connection, "discord")
        self.connection.commit()

        result = self.service.check_new_channel_external_id_availability(
            source_name="discord", external_channel_id="brand-new"
        )
        self.assertTrue(result.is_available)
        self.assertIsNone(result.existing_channel)

    def test_unavailable_when_channel_exists(self):
        source = get_or_create_source(self.connection, "discord")
        create_channel(self.connection, source.id, external_channel_id="taken")
        self.connection.commit()

        result = self.service.check_new_channel_external_id_availability(
            source_name="discord", external_channel_id="taken"
        )
        self.assertFalse(result.is_available)
        self.assertIsNotNone(result.existing_channel)
        self.assertEqual(result.existing_channel.external_channel_id, "taken")

    def test_collision_result_includes_the_existing_channel_object(self):
        source = get_or_create_source(self.connection, "discord")
        channel = create_channel(
            self.connection, source.id, external_channel_id="taken", name="my-channel"
        )
        self.connection.commit()

        result = self.service.check_new_channel_external_id_availability(
            source_name="discord", external_channel_id="taken"
        )
        self.assertEqual(result.existing_channel.id, channel.id)
        self.assertEqual(result.existing_channel.name, "my-channel")

    def test_blank_external_channel_id_raises(self):
        with self.assertRaises(ValueError):
            self.service.check_new_channel_external_id_availability(
                source_name="discord", external_channel_id="   "
            )

    def test_sentinel_rejected_when_source_absent(self):
        before = self._counts()

        with self.assertRaises(ValueError):
            self.service.check_new_channel_external_id_availability(
                source_name="discord",
                external_channel_id=UNSPECIFIED_CHANNEL_EXTERNAL_ID,
            )

        self.assertEqual(self._counts(), before)

    def test_sentinel_rejected_when_source_exists(self):
        source = get_or_create_source(self.connection, "discord")
        get_or_create_unspecified_channel(self.connection, source.id)
        self.connection.commit()
        before = self._counts()

        with self.assertRaises(ValueError):
            self.service.check_new_channel_external_id_availability(
                source_name="discord",
                external_channel_id=UNSPECIFIED_CHANNEL_EXTERNAL_ID,
            )

        self.assertEqual(self._counts(), before)

    def test_sentinel_rejection_opens_no_write_transaction(self):
        with self.assertRaises(ValueError):
            self.service.check_new_channel_external_id_availability(
                source_name="discord",
                external_channel_id=UNSPECIFIED_CHANNEL_EXTERNAL_ID,
            )

        self.assertFalse(self.connection.in_transaction)

    def test_whitespace_is_stripped_in_result(self):
        result = self.service.check_new_channel_external_id_availability(
            source_name="discord", external_channel_id="  padded-id  "
        )
        self.assertEqual(result.external_channel_id, "padded-id")

    def test_never_calls_get_or_create_source_or_channel(self):
        with patch(
            "database.service.get_or_create_source"
        ) as mock_get_or_create_source, patch(
            "database.service.get_or_create_channel"
        ) as mock_get_or_create_channel:
            self.service.check_new_channel_external_id_availability(
                source_name="discord", external_channel_id="chan-1"
            )

        mock_get_or_create_source.assert_not_called()
        mock_get_or_create_channel.assert_not_called()

    def test_no_write_transaction_opened(self):
        self.service.check_new_channel_external_id_availability(
            source_name="discord", external_channel_id="chan-1"
        )
        self.assertFalse(self.connection.in_transaction)

    def test_performs_no_write(self):
        before = self._counts()
        self.service.check_new_channel_external_id_availability(
            source_name="discord", external_channel_id="chan-1"
        )
        self.assertEqual(self._counts(), before)


class PredictChannelImportDuplicateStatusesServiceTests(_BulkChannelImportServiceTestCase):
    def _batch_text(self):
        return (
            "Bdorts\nAPP\n — 04:30 PM\n"
            "BOUGHT AVGO 07/24 380P $1.14 [SMALL]\n"
            "Bdorts•Today at 04:30 PM\n"
        )

    def test_uses_the_exact_resolve_external_id_result(self):
        source = get_or_create_source(self.connection, "discord")
        channel = create_channel(self.connection, source.id, external_channel_id="pred-chan")
        self.connection.commit()

        segmented = segment_discord_batch(self._batch_text())
        expected_external_id = _resolve_external_id(None, segmented[0].synthetic_id_input)

        predictions = self.service.predict_channel_import_duplicate_statuses(
            channel_id=channel.id, segmented_messages=segmented
        )

        self.assertEqual(predictions[0].external_id, expected_external_id)

    def test_new_message_predicted_correctly(self):
        source = get_or_create_source(self.connection, "discord")
        channel = create_channel(self.connection, source.id, external_channel_id="pred-chan-2")
        self.connection.commit()

        segmented = segment_discord_batch(self._batch_text())
        predictions = self.service.predict_channel_import_duplicate_statuses(
            channel_id=channel.id, segmented_messages=segmented
        )

        self.assertEqual(len(predictions), 1)
        prediction = predictions[0]
        self.assertIsInstance(prediction, ChannelImportDuplicatePrediction)
        self.assertFalse(prediction.predicted_duplicate)
        self.assertIsNone(prediction.predicted_content_differs)

    def test_ordinary_duplicate_predicted_correctly(self):
        batch_text = self._batch_text()
        self.service.ingest_batch(
            source_name="discord", reference_date="2026-01-01", timezone="UTC",
            raw_batch_text=batch_text, channel_external_id="pred-chan-3",
        )
        source = get_or_create_source(self.connection, "discord")
        channel = get_or_create_channel(self.connection, source.id, "pred-chan-3")
        self.connection.commit()

        segmented = segment_discord_batch(batch_text)
        predictions = self.service.predict_channel_import_duplicate_statuses(
            channel_id=channel.id, segmented_messages=segmented
        )

        self.assertTrue(predictions[0].predicted_duplicate)
        self.assertFalse(predictions[0].predicted_content_differs)

    def test_content_different_duplicate_predicted_correctly(self):
        original_text = self._batch_text()
        self.service.ingest_batch(
            source_name="discord", reference_date="2026-01-01", timezone="UTC",
            raw_batch_text=original_text, channel_external_id="pred-chan-4",
        )
        source = get_or_create_source(self.connection, "discord")
        channel = get_or_create_channel(self.connection, source.id, "pred-chan-4")
        self.connection.commit()

        # Same identity inputs (channel tag/trader/timestamp/body feed
        # synthetic_id_input) - segment_discord_batch() only derives
        # synthetic_id_input from the *cleaned* body, so this fabricates
        # a scenario with the same segmented message content but a
        # deliberately different raw_text by re-segmenting the original
        # and mutating raw_text directly (raw_text differences are what
        # content_hash predicts on, independent of synthetic_id_input).
        segmented = segment_discord_batch(original_text)
        message = segmented[0]
        mutated = message.__class__(
            **{**message.__dict__, "raw_text": message.raw_text + "\n[edited]"}
        )

        predictions = self.service.predict_channel_import_duplicate_statuses(
            channel_id=channel.id, segmented_messages=[mutated]
        )

        self.assertTrue(predictions[0].predicted_duplicate)
        self.assertTrue(predictions[0].predicted_content_differs)

    def test_multiple_identical_messages_retain_distinct_occurrence_identities(self):
        duplicate_pair = (
            "Bdorts\nAPP\n — 04:30 PM\n"
            "BOUGHT AVGO 07/24 380P $1.14 [SMALL]\n"
            "Bdorts•Today at 04:30 PM\n"
            "BOUGHT AVGO 07/24 380P $1.14 [SMALL]\n"
            "Bdorts•Today at 04:30 PM\n"
        )
        source = get_or_create_source(self.connection, "discord")
        channel = create_channel(self.connection, source.id, external_channel_id="pred-chan-5")
        self.connection.commit()

        segmented = segment_discord_batch(duplicate_pair)
        self.assertEqual(len(segmented), 2)

        predictions = self.service.predict_channel_import_duplicate_statuses(
            channel_id=channel.id, segmented_messages=segmented
        )

        self.assertEqual(len(predictions), 2)
        self.assertNotEqual(predictions[0].external_id, predictions[1].external_id)
        self.assertEqual(predictions[0].sequence_in_batch, 1)
        self.assertEqual(predictions[1].sequence_in_batch, 2)

    def test_never_calls_get_or_create_source_or_channel(self):
        source = get_or_create_source(self.connection, "discord")
        channel = create_channel(self.connection, source.id, external_channel_id="pred-chan-6")
        self.connection.commit()
        segmented = segment_discord_batch(self._batch_text())

        with patch(
            "database.service.get_or_create_source"
        ) as mock_get_or_create_source, patch(
            "database.service.get_or_create_channel"
        ) as mock_get_or_create_channel:
            self.service.predict_channel_import_duplicate_statuses(
                channel_id=channel.id, segmented_messages=segmented
            )

        mock_get_or_create_source.assert_not_called()
        mock_get_or_create_channel.assert_not_called()

    def test_no_write_transaction_opened(self):
        source = get_or_create_source(self.connection, "discord")
        channel = create_channel(self.connection, source.id, external_channel_id="pred-chan-7")
        self.connection.commit()
        segmented = segment_discord_batch(self._batch_text())

        self.service.predict_channel_import_duplicate_statuses(
            channel_id=channel.id, segmented_messages=segmented
        )

        self.assertFalse(self.connection.in_transaction)

    def test_performs_no_write(self):
        source = get_or_create_source(self.connection, "discord")
        channel = create_channel(self.connection, source.id, external_channel_id="pred-chan-8")
        self.connection.commit()
        segmented = segment_discord_batch(self._batch_text())
        before = self._counts()

        self.service.predict_channel_import_duplicate_statuses(
            channel_id=channel.id, segmented_messages=segmented
        )

        self.assertEqual(self._counts(), before)

    def test_valid_empty_channel_still_predicts_new(self):
        # A genuinely empty channel (zero raw_messages rows) is a valid
        # target - every message correctly predicts as new, never an
        # error, unlike a missing or sentinel channel_id.
        source = get_or_create_source(self.connection, "discord")
        channel = create_channel(self.connection, source.id, external_channel_id="pred-empty")
        self.connection.commit()
        segmented = segment_discord_batch(self._batch_text())

        predictions = self.service.predict_channel_import_duplicate_statuses(
            channel_id=channel.id, segmented_messages=segmented
        )

        self.assertEqual(len(predictions), 1)
        self.assertFalse(predictions[0].predicted_duplicate)
        self.assertIsNone(predictions[0].predicted_content_differs)

    def test_missing_channel_id_rejected(self):
        segmented = segment_discord_batch(self._batch_text())

        with self.assertRaises(ValueError):
            self.service.predict_channel_import_duplicate_statuses(
                channel_id=999999, segmented_messages=segmented
            )

    def test_unspecified_channel_id_rejected(self):
        source = get_or_create_source(self.connection, "discord")
        sentinel = get_or_create_unspecified_channel(self.connection, source.id)
        self.connection.commit()
        segmented = segment_discord_batch(self._batch_text())

        with self.assertRaises(ValueError):
            self.service.predict_channel_import_duplicate_statuses(
                channel_id=sentinel.id, segmented_messages=segmented
            )

    def test_missing_channel_id_performs_no_write_and_opens_no_transaction(self):
        segmented = segment_discord_batch(self._batch_text())
        before = self._counts()

        with self.assertRaises(ValueError):
            self.service.predict_channel_import_duplicate_statuses(
                channel_id=999999, segmented_messages=segmented
            )

        self.assertEqual(self._counts(), before)
        self.assertFalse(self.connection.in_transaction)

    def test_unspecified_channel_id_performs_no_write_and_opens_no_transaction(self):
        source = get_or_create_source(self.connection, "discord")
        sentinel = get_or_create_unspecified_channel(self.connection, source.id)
        self.connection.commit()
        segmented = segment_discord_batch(self._batch_text())
        before = self._counts()

        with self.assertRaises(ValueError):
            self.service.predict_channel_import_duplicate_statuses(
                channel_id=sentinel.id, segmented_messages=segmented
            )

        self.assertEqual(self._counts(), before)
        self.assertFalse(self.connection.in_transaction)


def _batch_of_15():
    """A pure literal fixture of exactly 15 distinct footer-line-format
    messages, used only to exercise a real ChannelCheckpoint via a real
    ingest_batch() call - the minimum-batch-size floor itself belongs to
    Recovery Milestone R9b's atomic method, not tested here."""
    messages = []
    for i in range(15):
        messages.append(
            f"Bdorts\nAPP\n — 04:{i:02d} PM\n"
            f"BOUGHT AVGO 07/24 {380 + i}P $1.{i:02d} [SMALL]\n"
            f"Bdorts•Today at 04:{i:02d} PM\n"
        )
    return "".join(messages)


if __name__ == "__main__":
    unittest.main()
