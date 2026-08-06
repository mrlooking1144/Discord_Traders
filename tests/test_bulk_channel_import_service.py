"""Tests for Recovery Milestone R9a's Bulk Channel Import backend read
contracts on TradeService, plus Recovery Milestone R9b's atomic
ingest-plus-lifecycle-rebuild transaction.

R9a section covers the four strictly read-only public methods added to
database/service.py: list_bulk_import_channels(),
get_bulk_import_channel_summary(), check_new_channel_external_id_availability(),
and predict_channel_import_duplicate_statuses() - plus
database.repository.compute_content_hash() (promoted from a private
repository-only helper to the one shared content-hash implementation
both database/repository.py and database/service.py import and call)
and _duplicate_outcome()'s continued correctness after being updated to
call it.

R9b section (added below the R9a section, unchanged) covers
TradeService._ingest_batch_no_commit(),
TradeService._collect_newly_stored_raw_message_ids(), and
TradeService.import_channel_batch_with_lifecycle_rebuild() - the atomic
public operation that accepts a previewed batch of at least 15 exact
SegmentedMessage objects, resolves/creates a channel per an explicit
channel_mode, ingests, rebuilds only the newly affected lifecycles, and
records one successful channel_import_operations row, all inside one
transaction.
"""

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app.discord_adapter import segment_discord_batch
from app.parser import extract_trade_event as _real_extract_trade_event
from database.config import DatabaseConfig
from database.db import get_connection, initialize_database
from database.models import (
    AtomicChannelImportResult,
    BatchIngestResult,
    Channel,
    ChannelExternalIdAvailability,
    ChannelImportChannelSummary,
    ChannelImportDuplicatePrediction,
    LifecycleRebuildResult,
    MessageIngestOutcome,
)
from database.repository import (
    UNSPECIFIED_CHANNEL_EXTERNAL_ID,
    create_channel,
    create_channel_import_operation,
    create_message_extraction as _real_create_message_extraction,
    get_channel_by_external_id,
    get_latest_channel_import_operation,
    get_or_create_channel,
    get_or_create_source,
    get_or_create_unspecified_channel,
)
from database.service import (
    ChannelExternalIdCollisionError,
    LifecycleIntegrityError,
    TradeService,
    _resolve_external_id,
)


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


# ---------------------------------------------------------------------------
# Recovery Milestone R9b: TradeService._ingest_batch_no_commit(),
# TradeService._collect_newly_stored_raw_message_ids(), and
# TradeService.import_channel_batch_with_lifecycle_rebuild() - the atomic
# public operation. Every test class below extends
# _AtomicImportServiceTestCase (itself extending the same real-temporary-
# SQLite-database fixture the R9a tests above already use); mocking is
# used only where the approved test matrix explicitly calls for it (a
# deterministic extraction "failed" fixture, a deterministic channel-
# collision race, and forced-failure rollback proofs).
# ---------------------------------------------------------------------------


def _valid_batch_text(count, start=0, trader="Bdorts", symbol="AVGO"):
    """A pure literal fixture of `count` distinct, valid, lifecycle-
    eligible footer-line-format BOUGHT messages, starting at `start` (so
    two calls with different `start` values never collide on synthetic
    identity, and two calls with the same `start`/`count` prefix produce
    byte-identical messages - used to build genuine duplicate overlap).
    Mirrors _batch_of_15()'s own exact message shape."""
    messages = []
    for offset in range(count):
        i = start + offset
        messages.append(
            f"{trader}\nAPP\n — 04:{i:02d} PM\n"
            f"BOUGHT {symbol} 07/24 {380 + i}P $1.{i:02d} [SMALL]\n"
            f"{trader}•Today at 04:{i:02d} PM\n"
        )
    return "".join(messages)


def _unrecognized_message_text(i, trader="Bdorts"):
    """One valid header/footer-shaped message whose body contains no
    recognizable trade action - segments successfully but always parses
    to parse_status == "unrecognized"."""
    return (
        f"{trader}\nAPP\n — 04:{i:02d} PM\n"
        f"gm everyone, no trade today\n"
        f"{trader}•Today at 04:{i:02d} PM\n"
    )


class _AtomicImportServiceTestCase(_BulkChannelImportServiceTestCase):
    """Shared setup for every R9b atomic-operation test class below."""

    def _make_source_and_channel(
        self, source_name="discord", external_channel_id="atomic-chan"
    ):
        source = get_or_create_source(self.connection, source_name)
        channel = create_channel(
            self.connection, source.id, external_channel_id=external_channel_id
        )
        self.connection.commit()
        return source, channel

    def _segmented(self, count, start=0, **kwargs):
        return segment_discord_batch(_valid_batch_text(count, start=start, **kwargs))

    def _atomic_write_counts(self):
        """Every table a single import_channel_batch_with_lifecycle_rebuild()
        call can possibly write to, in one dict - the complete rollback
        proof this milestone's forced-failure tests use, wider than the
        R9a _counts() helper above (which this method never changes)."""
        tables = (
            "sources",
            "channels",
            "traders",
            "import_batches",
            "raw_messages",
            "message_extractions",
            "trade_signals",
            "trade_lifecycles",
            "trade_lifecycle_events",
            "channel_import_operations",
        )
        return {
            table: self.connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in tables
        }


class AtomicImportValidationTests(_AtomicImportServiceTestCase):
    def test_14_messages_rejected(self):
        _, channel = self._make_source_and_channel()
        segmented = self._segmented(14)
        before = self._counts()

        with self.assertRaises(ValueError) as ctx:
            self.service.import_channel_batch_with_lifecycle_rebuild(
                source_name="discord", channel_mode="existing",
                existing_channel_id=channel.id, reference_date="2026-01-01",
                timezone="UTC", raw_batch_text=_valid_batch_text(14),
                segmented_messages=segmented,
            )

        self.assertEqual(
            str(ctx.exception),
            "At least 15 segmented messages are required for a Bulk "
            "Channel Import batch; got 14.",
        )
        self.assertEqual(self._counts(), before)
        self.assertFalse(self.connection.in_transaction)

    def test_exactly_15_accepted(self):
        _, channel = self._make_source_and_channel()
        segmented = self._segmented(15)

        result = self.service.import_channel_batch_with_lifecycle_rebuild(
            source_name="discord", channel_mode="existing",
            existing_channel_id=channel.id, reference_date="2026-01-01",
            timezone="UTC", raw_batch_text=_valid_batch_text(15),
            segmented_messages=segmented,
        )

        self.assertIsInstance(result, AtomicChannelImportResult)
        self.assertEqual(result.batch_result.stored_count, 15)
        self.assertEqual(result.operation.processed_count, 15)

    def test_invalid_channel_mode_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.service.import_channel_batch_with_lifecycle_rebuild(
                source_name="discord", channel_mode="bogus",
                reference_date="2026-01-01", timezone="UTC",
                raw_batch_text=_valid_batch_text(15),
                segmented_messages=self._segmented(15),
            )
        self.assertEqual(
            str(ctx.exception), "channel_mode must be exactly 'existing' or 'create'."
        )

    def test_exact_segmented_sequence_used_without_resegmentation(self):
        _, channel = self._make_source_and_channel()
        segmented = self._segmented(15)

        with patch("database.service.segment_discord_batch") as mock_segment:
            result = self.service.import_channel_batch_with_lifecycle_rebuild(
                source_name="discord", channel_mode="existing",
                existing_channel_id=channel.id, reference_date="2026-01-01",
                timezone="UTC", raw_batch_text=_valid_batch_text(15),
                segmented_messages=segmented,
            )

        mock_segment.assert_not_called()
        # A set comparison alone cannot detect reordering or a duplicate
        # loss (two distinct messages silently collapsing to one entry) -
        # compare exact ordered (sequence_in_batch, external_id) pairs.
        expected = [
            (
                message.sequence_in_batch,
                _resolve_external_id(None, message.synthetic_id_input),
            )
            for message in segmented
        ]
        actual = [
            (outcome.sequence_in_batch, outcome.external_id)
            for outcome in result.batch_result.messages
        ]
        self.assertEqual(actual, expected)
        self.assertEqual(len(result.batch_result.messages), len(segmented))

    def test_ingest_batch_no_commit_does_not_commit(self):
        source, channel = self._make_source_and_channel()
        segmented = self._segmented(15)

        saved_isolation_level = self.connection.isolation_level
        self.connection.isolation_level = None
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.service._ingest_batch_no_commit(
                source=source, channel=channel,
                raw_batch_text=_valid_batch_text(15), reference_date="2026-01-01",
                timezone="UTC", segmented_messages=segmented,
            )
            self.assertTrue(self.connection.in_transaction)
        finally:
            self.connection.rollback()
            self.connection.isolation_level = saved_isolation_level


class AtomicImportExistingModeTests(_AtomicImportServiceTestCase):
    def test_existing_channel_success(self):
        source, channel = self._make_source_and_channel()
        segmented = self._segmented(15)
        before_channels = self._counts()["channels"]

        result = self.service.import_channel_batch_with_lifecycle_rebuild(
            source_name="discord", channel_mode="existing",
            existing_channel_id=channel.id, reference_date="2026-01-01",
            timezone="UTC", raw_batch_text=_valid_batch_text(15),
            segmented_messages=segmented,
        )

        self.assertEqual(result.channel.id, channel.id)
        self.assertEqual(self._counts()["channels"], before_channels)

    def test_wrong_source_channel_rejected(self):
        get_or_create_source(self.connection, "discord")
        telegram_source = get_or_create_source(self.connection, "telegram")
        telegram_channel = create_channel(
            self.connection, telegram_source.id, external_channel_id="tg-chan"
        )
        self.connection.commit()
        before = self._counts()
        segmented = self._segmented(15)

        with self.assertRaises(ValueError) as ctx:
            self.service.import_channel_batch_with_lifecycle_rebuild(
                source_name="discord", channel_mode="existing",
                existing_channel_id=telegram_channel.id, reference_date="2026-01-01",
                timezone="UTC", raw_batch_text=_valid_batch_text(15),
                segmented_messages=segmented,
            )

        self.assertEqual(
            str(ctx.exception),
            f"channel {telegram_channel.id} does not belong to source 'discord'.",
        )
        self.assertEqual(self._counts(), before)

    def test_missing_existing_channel_rejected(self):
        get_or_create_source(self.connection, "discord")
        self.connection.commit()
        before = self._counts()
        segmented = self._segmented(15)

        with self.assertRaises(ValueError) as ctx:
            self.service.import_channel_batch_with_lifecycle_rebuild(
                source_name="discord", channel_mode="existing",
                existing_channel_id=999999, reference_date="2026-01-01",
                timezone="UTC", raw_batch_text=_valid_batch_text(15),
                segmented_messages=segmented,
            )

        self.assertEqual(
            str(ctx.exception), "existing_channel_id 999999 does not exist."
        )
        self.assertEqual(self._counts(), before)

    def test_sentinel_channel_rejected(self):
        source = get_or_create_source(self.connection, "discord")
        sentinel = get_or_create_unspecified_channel(self.connection, source.id)
        self.connection.commit()
        before = self._counts()
        segmented = self._segmented(15)

        with self.assertRaises(ValueError) as ctx:
            self.service.import_channel_batch_with_lifecycle_rebuild(
                source_name="discord", channel_mode="existing",
                existing_channel_id=sentinel.id, reference_date="2026-01-01",
                timezone="UTC", raw_batch_text=_valid_batch_text(15),
                segmented_messages=segmented,
            )

        self.assertEqual(
            str(ctx.exception),
            "existing_channel_id must not refer to the reserved unspecified "
            "channel.",
        )
        self.assertEqual(self._counts(), before)

    def test_absent_source_rejected_no_sources_row_created(self):
        before = self._counts()
        segmented = self._segmented(15)

        with self.assertRaises(ValueError) as ctx:
            self.service.import_channel_batch_with_lifecycle_rebuild(
                source_name="discord", channel_mode="existing",
                existing_channel_id=1, reference_date="2026-01-01",
                timezone="UTC", raw_batch_text=_valid_batch_text(15),
                segmented_messages=segmented,
            )

        self.assertEqual(str(ctx.exception), "Source 'discord' does not exist.")
        self.assertEqual(self._counts(), before)
        self.assertFalse(self.connection.in_transaction)

    def test_missing_existing_channel_id_argument_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.service.import_channel_batch_with_lifecycle_rebuild(
                source_name="discord", channel_mode="existing",
                reference_date="2026-01-01", timezone="UTC",
                raw_batch_text=_valid_batch_text(15),
                segmented_messages=self._segmented(15),
            )
        self.assertEqual(
            str(ctx.exception),
            "existing_channel_id is required when channel_mode is 'existing'.",
        )

    def test_existing_channel_id_must_be_positive_integer(self):
        with self.assertRaises(ValueError) as ctx:
            self.service.import_channel_batch_with_lifecycle_rebuild(
                source_name="discord", channel_mode="existing",
                existing_channel_id=0, reference_date="2026-01-01", timezone="UTC",
                raw_batch_text=_valid_batch_text(15),
                segmented_messages=self._segmented(15),
            )
        self.assertEqual(
            str(ctx.exception), "existing_channel_id must be a positive integer."
        )

    def test_existing_channel_id_rejects_boolean(self):
        with self.assertRaises(ValueError) as ctx:
            self.service.import_channel_batch_with_lifecycle_rebuild(
                source_name="discord", channel_mode="existing",
                existing_channel_id=True, reference_date="2026-01-01", timezone="UTC",
                raw_batch_text=_valid_batch_text(15),
                segmented_messages=self._segmented(15),
            )
        self.assertEqual(
            str(ctx.exception), "existing_channel_id must be a positive integer."
        )

    def test_new_channel_external_id_must_be_none_in_existing_mode(self):
        _, channel = self._make_source_and_channel()
        with self.assertRaises(ValueError) as ctx:
            self.service.import_channel_batch_with_lifecycle_rebuild(
                source_name="discord", channel_mode="existing",
                existing_channel_id=channel.id, new_channel_external_id="nope",
                reference_date="2026-01-01", timezone="UTC",
                raw_batch_text=_valid_batch_text(15),
                segmented_messages=self._segmented(15),
            )
        self.assertEqual(
            str(ctx.exception),
            "new_channel_external_id must not be supplied when channel_mode "
            "is 'existing'.",
        )

    def test_new_channel_name_must_be_none_in_existing_mode(self):
        _, channel = self._make_source_and_channel()
        with self.assertRaises(ValueError) as ctx:
            self.service.import_channel_batch_with_lifecycle_rebuild(
                source_name="discord", channel_mode="existing",
                existing_channel_id=channel.id, new_channel_name="nope",
                reference_date="2026-01-01", timezone="UTC",
                raw_batch_text=_valid_batch_text(15),
                segmented_messages=self._segmented(15),
            )
        self.assertEqual(
            str(ctx.exception),
            "new_channel_name must not be supplied when channel_mode is "
            "'existing'.",
        )


class AtomicImportCreateModeTests(_AtomicImportServiceTestCase):
    def test_new_channel_success(self):
        get_or_create_source(self.connection, "discord")
        self.connection.commit()
        segmented = self._segmented(15)

        result = self.service.import_channel_batch_with_lifecycle_rebuild(
            source_name="discord", channel_mode="create",
            new_channel_external_id="fresh-chan", new_channel_name="Fresh Channel",
            reference_date="2026-01-01", timezone="UTC",
            raw_batch_text=_valid_batch_text(15), segmented_messages=segmented,
        )

        self.assertEqual(result.channel.external_channel_id, "fresh-chan")
        self.assertEqual(result.channel.name, "Fresh Channel")
        source = get_or_create_source(self.connection, "discord")
        looked_up = get_channel_by_external_id(self.connection, source.id, "fresh-chan")
        self.assertEqual(looked_up.id, result.channel.id)

    def test_sentinel_new_channel_external_id_rejected(self):
        get_or_create_source(self.connection, "discord")
        self.connection.commit()
        before = self._counts()

        with self.assertRaises(ValueError) as ctx:
            self.service.import_channel_batch_with_lifecycle_rebuild(
                source_name="discord", channel_mode="create",
                new_channel_external_id=UNSPECIFIED_CHANNEL_EXTERNAL_ID,
                reference_date="2026-01-01", timezone="UTC",
                raw_batch_text=_valid_batch_text(15),
                segmented_messages=self._segmented(15),
            )

        self.assertEqual(
            str(ctx.exception),
            "new_channel_external_id must not be the reserved "
            "unspecified-channel sentinel value.",
        )
        self.assertEqual(self._counts(), before)

    def test_blank_new_channel_external_id_rejected(self):
        get_or_create_source(self.connection, "discord")
        self.connection.commit()

        with self.assertRaises(ValueError) as ctx:
            self.service.import_channel_batch_with_lifecycle_rebuild(
                source_name="discord", channel_mode="create",
                new_channel_external_id="   ",
                reference_date="2026-01-01", timezone="UTC",
                raw_batch_text=_valid_batch_text(15),
                segmented_messages=self._segmented(15),
            )

        self.assertEqual(
            str(ctx.exception),
            "new_channel_external_id must not be empty or whitespace-only "
            "when channel_mode is 'create'.",
        )

    def test_none_new_channel_external_id_rejected(self):
        get_or_create_source(self.connection, "discord")
        self.connection.commit()
        before = self._counts()

        with self.assertRaises(ValueError) as ctx:
            self.service.import_channel_batch_with_lifecycle_rebuild(
                source_name="discord", channel_mode="create",
                new_channel_external_id=None,
                reference_date="2026-01-01", timezone="UTC",
                raw_batch_text=_valid_batch_text(15),
                segmented_messages=self._segmented(15),
            )

        self.assertEqual(
            str(ctx.exception),
            "new_channel_external_id must not be empty or whitespace-only "
            "when channel_mode is 'create'.",
        )
        self.assertEqual(self._counts(), before)
        self.assertFalse(self.connection.in_transaction)

    def test_colliding_new_channel_external_id_rejected_up_front(self):
        source = get_or_create_source(self.connection, "discord")
        create_channel(self.connection, source.id, external_channel_id="taken")
        self.connection.commit()
        before = self._counts()

        with self.assertRaises(ChannelExternalIdCollisionError):
            self.service.import_channel_batch_with_lifecycle_rebuild(
                source_name="discord", channel_mode="create",
                new_channel_external_id="taken",
                reference_date="2026-01-01", timezone="UTC",
                raw_batch_text=_valid_batch_text(15),
                segmented_messages=self._segmented(15),
            )

        self.assertEqual(self._counts(), before)

    def test_existing_channel_id_must_be_none_in_create_mode(self):
        get_or_create_source(self.connection, "discord")
        self.connection.commit()

        with self.assertRaises(ValueError) as ctx:
            self.service.import_channel_batch_with_lifecycle_rebuild(
                source_name="discord", channel_mode="create",
                existing_channel_id=1, new_channel_external_id="whatever",
                reference_date="2026-01-01", timezone="UTC",
                raw_batch_text=_valid_batch_text(15),
                segmented_messages=self._segmented(15),
            )
        self.assertEqual(
            str(ctx.exception),
            "existing_channel_id must not be supplied when channel_mode is "
            "'create'.",
        )

    def test_confirmed_collision_translation_after_race(self):
        get_or_create_source(self.connection, "discord")
        self.connection.commit()
        raced_channel = Channel(
            id=999, source_id=1, external_channel_id="race-chan", name=None
        )

        with patch(
            "database.service.get_channel_by_external_id",
            side_effect=[None, raced_channel],
        ) as mock_lookup, patch(
            "database.service.create_channel",
            side_effect=sqlite3.IntegrityError("UNIQUE constraint failed"),
        ) as mock_create:
            with self.assertRaises(ChannelExternalIdCollisionError):
                self.service.import_channel_batch_with_lifecycle_rebuild(
                    source_name="discord", channel_mode="create",
                    new_channel_external_id="race-chan",
                    reference_date="2026-01-01", timezone="UTC",
                    raw_batch_text=_valid_batch_text(15),
                    segmented_messages=self._segmented(15),
                )

        self.assertEqual(mock_lookup.call_count, 2)
        mock_create.assert_called_once()

    def test_unconfirmed_integrity_error_propagates_unchanged(self):
        get_or_create_source(self.connection, "discord")
        self.connection.commit()
        before = self._atomic_write_counts()

        with patch(
            "database.service.get_channel_by_external_id",
            side_effect=[None, None],
        ), patch(
            "database.service.create_channel",
            side_effect=sqlite3.IntegrityError("some other constraint"),
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                self.service.import_channel_batch_with_lifecycle_rebuild(
                    source_name="discord", channel_mode="create",
                    new_channel_external_id="whatever-chan",
                    reference_date="2026-01-01", timezone="UTC",
                    raw_batch_text=_valid_batch_text(15),
                    segmented_messages=self._segmented(15),
                )

        self.assertEqual(self._atomic_write_counts(), before)
        self.assertFalse(self.connection.in_transaction)

    def test_absent_source_is_created(self):
        before_sources = self._counts()["sources"]

        result = self.service.import_channel_batch_with_lifecycle_rebuild(
            source_name="discord", channel_mode="create",
            new_channel_external_id="brand-new-chan",
            reference_date="2026-01-01", timezone="UTC",
            raw_batch_text=_valid_batch_text(15),
            segmented_messages=self._segmented(15),
        )

        self.assertEqual(self._counts()["sources"], before_sources + 1)
        source = get_or_create_source(self.connection, "discord")
        self.assertEqual(result.channel.source_id, source.id)

    def test_forced_failure_after_source_creation_rolls_back_new_source(self):
        before = self._atomic_write_counts()

        with patch(
            "database.service.create_channel_import_operation",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                self.service.import_channel_batch_with_lifecycle_rebuild(
                    source_name="discord", channel_mode="create",
                    new_channel_external_id="rolled-back-chan",
                    reference_date="2026-01-01", timezone="UTC",
                    raw_batch_text=_valid_batch_text(15),
                    segmented_messages=self._segmented(15),
                )

        self.assertEqual(self._atomic_write_counts(), before)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 0
        )
        self.assertFalse(self.connection.in_transaction)


class AtomicImportMixedOutcomeTests(_AtomicImportServiceTestCase):
    def test_mixed_new_and_duplicate_messages(self):
        _, channel = self._make_source_and_channel()
        full_text = _valid_batch_text(15)
        # Pre-ingest only the first 10 messages via the legacy
        # ingest_batch() path against the same channel, so the atomic
        # call below sees a genuine mix of already-present and newly-new
        # messages - _valid_batch_text(10) and _valid_batch_text(15) share
        # the same first 10 messages (i=0..9) by construction.
        subset_text = _valid_batch_text(10)
        self.service.ingest_batch(
            source_name="discord", reference_date="2026-01-01", timezone="UTC",
            raw_batch_text=subset_text, channel_external_id=channel.external_channel_id,
        )
        segmented = segment_discord_batch(full_text)

        result = self.service.import_channel_batch_with_lifecycle_rebuild(
            source_name="discord", channel_mode="existing",
            existing_channel_id=channel.id, reference_date="2026-01-01",
            timezone="UTC", raw_batch_text=full_text, segmented_messages=segmented,
        )

        self.assertEqual(
            result.batch_result.stored_count + result.batch_result.duplicate_count,
            result.batch_result.total_segmented,
        )
        self.assertEqual(result.batch_result.duplicate_count, 10)
        self.assertEqual(result.batch_result.stored_count, 5)
        self.assertEqual(result.operation.stored_count, 5)
        self.assertEqual(result.operation.duplicate_count, 10)
        # Each message carries a distinct strike, so each of the 5 newly
        # stored messages is its own distinct lifecycle key - the
        # targeted rebuild considers exactly those 5 keys, never the
        # full-batch 15 and never zero.
        self.assertEqual(result.lifecycle_result.keys_considered, 5)
        self.assertEqual(result.lifecycle_result.lifecycles_created, 5)


class AtomicImportDuplicateOnlyTests(_AtomicImportServiceTestCase):
    def test_duplicate_only_success(self):
        _, channel = self._make_source_and_channel()
        full_text = _valid_batch_text(15)
        self.service.ingest_batch(
            source_name="discord", reference_date="2026-01-01", timezone="UTC",
            raw_batch_text=full_text, channel_external_id=channel.external_channel_id,
        )
        segmented = segment_discord_batch(full_text)
        before_import_batches = self.connection.execute(
            "SELECT COUNT(*) FROM import_batches"
        ).fetchone()[0]

        with patch.object(
            TradeService, "_rebuild_lifecycles_for_raw_message_ids_no_commit",
        ) as mock_rebuild:
            result = self.service.import_channel_batch_with_lifecycle_rebuild(
                source_name="discord", channel_mode="existing",
                existing_channel_id=channel.id, reference_date="2026-01-01",
                timezone="UTC", raw_batch_text=full_text, segmented_messages=segmented,
            )

        mock_rebuild.assert_not_called()
        self.assertIsNone(result.batch_result.import_batch_id)
        self.assertEqual(result.batch_result.stored_count, 0)
        self.assertEqual(result.batch_result.duplicate_count, 15)
        self.assertEqual(
            result.lifecycle_result,
            LifecycleRebuildResult(
                keys_considered=0, keys_changed=0, keys_unchanged=0,
                lifecycles_superseded=0, lifecycles_created=0,
                lifecycle_events_created=0, signal_pointers_cleared=0,
                signal_pointers_assigned=0,
            ),
        )
        self.assertEqual(result.operation.stored_count, 0)
        self.assertIsNone(result.operation.import_batch_id)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0],
            before_import_batches,
        )

        latest = get_latest_channel_import_operation(self.connection, channel.id)
        self.assertEqual(latest.id, result.operation.id)


class AtomicImportOperationRecordTests(_AtomicImportServiceTestCase):
    def test_unrecognized_message_counted(self):
        _, channel = self._make_source_and_channel()
        text = _unrecognized_message_text(0) + _valid_batch_text(14, start=100)
        segmented = segment_discord_batch(text)
        self.assertEqual(len(segmented), 15)

        result = self.service.import_channel_batch_with_lifecycle_rebuild(
            source_name="discord", channel_mode="existing",
            existing_channel_id=channel.id, reference_date="2026-01-01",
            timezone="UTC", raw_batch_text=text, segmented_messages=segmented,
        )

        self.assertEqual(result.batch_result.unrecognized_count, 1)
        self.assertEqual(result.operation.unrecognized_count, 1)

    def test_failed_message_counted_via_patched_extractor(self):
        _, channel = self._make_source_and_channel()
        text = _valid_batch_text(15)
        segmented = segment_discord_batch(text)
        target_cleaned_text = segmented[0].cleaned_text

        def side_effect(cleaned_text):
            if cleaned_text == target_cleaned_text:
                return {
                    "symbol": None,
                    "action": None,
                    "option_type": None,
                    "price": None,
                    "expiration": None,
                    "position_size": None,
                    "strike": None,
                    "expiration_raw": None,
                    "event_type": None,
                    "qualifier": None,
                    "stated_entry_price": None,
                    "stated_return_pct": None,
                    "notes": None,
                    "parse_status": "failed",
                    "ambiguity_flags": [],
                }
            return _real_extract_trade_event(cleaned_text)

        with patch("database.service.extract_trade_event", side_effect=side_effect):
            result = self.service.import_channel_batch_with_lifecycle_rebuild(
                source_name="discord", channel_mode="existing",
                existing_channel_id=channel.id, reference_date="2026-01-01",
                timezone="UTC", raw_batch_text=text, segmented_messages=segmented,
            )

        self.assertEqual(result.batch_result.failed_count, 1)
        self.assertEqual(result.operation.failed_count, 1)

        failed_external_id = _resolve_external_id(None, segmented[0].synthetic_id_input)
        raw_row = self.connection.execute(
            "SELECT id FROM raw_messages WHERE channel_id = ? AND external_id = ?",
            (channel.id, failed_external_id),
        ).fetchone()
        self.assertIsNotNone(raw_row)
        raw_message_id = raw_row[0]

        extraction_row = self.connection.execute(
            "SELECT parse_status FROM message_extractions WHERE raw_message_id = ?",
            (raw_message_id,),
        ).fetchone()
        self.assertEqual(extraction_row[0], "failed")

        signal_count = self.connection.execute(
            "SELECT COUNT(*) FROM trade_signals WHERE raw_message_id = ?",
            (raw_message_id,),
        ).fetchone()[0]
        self.assertEqual(signal_count, 0)


class AtomicImportLifecycleRebuildTests(_AtomicImportServiceTestCase):
    def test_targeted_rebuild_leaves_unrelated_lifecycle_untouched(self):
        # Seed an unrelated pre-existing current lifecycle on a different
        # channel/trader/symbol.
        self.service.ingest_batch(
            source_name="discord", reference_date="2026-01-01", timezone="UTC",
            raw_batch_text=_valid_batch_text(15, trader="OtherTrader", symbol="MSFT"),
            channel_external_id="unrelated-chan",
        )
        self.service.rebuild_all_lifecycles()
        unrelated_lifecycle_id = self.connection.execute(
            "SELECT id FROM trade_lifecycles WHERE is_current = 1 LIMIT 1"
        ).fetchone()[0]
        events_before = self.connection.execute(
            "SELECT COUNT(*) FROM trade_lifecycle_events WHERE trade_lifecycle_id = ?",
            (unrelated_lifecycle_id,),
        ).fetchone()[0]

        _, channel = self._make_source_and_channel(external_channel_id="new-import-chan")
        segmented = self._segmented(15, start=500)

        self.service.import_channel_batch_with_lifecycle_rebuild(
            source_name="discord", channel_mode="existing",
            existing_channel_id=channel.id, reference_date="2026-01-01",
            timezone="UTC", raw_batch_text=_valid_batch_text(15, start=500),
            segmented_messages=segmented,
        )

        is_current_after = self.connection.execute(
            "SELECT is_current FROM trade_lifecycles WHERE id = ?",
            (unrelated_lifecycle_id,),
        ).fetchone()[0]
        events_after = self.connection.execute(
            "SELECT COUNT(*) FROM trade_lifecycle_events WHERE trade_lifecycle_id = ?",
            (unrelated_lifecycle_id,),
        ).fetchone()[0]

        self.assertEqual(is_current_after, 1)
        self.assertEqual(events_after, events_before)


class AtomicImportRollbackTests(_AtomicImportServiceTestCase):
    def test_ingestion_failure_rolls_back_everything(self):
        _, channel = self._make_source_and_channel()
        segmented = self._segmented(15)
        before = self._atomic_write_counts()

        call_count = {"n": 0}

        def fail_after_first(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _real_create_message_extraction(*args, **kwargs)
            raise sqlite3.OperationalError("boom")

        with patch(
            "database.service.create_message_extraction", side_effect=fail_after_first
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.service.import_channel_batch_with_lifecycle_rebuild(
                    source_name="discord", channel_mode="existing",
                    existing_channel_id=channel.id, reference_date="2026-01-01",
                    timezone="UTC", raw_batch_text=_valid_batch_text(15),
                    segmented_messages=segmented,
                )

        # Complete rollback proof: sources, channels, traders,
        # import_batches, raw_messages, message_extractions,
        # trade_signals, trade_lifecycles, trade_lifecycle_events, and
        # channel_import_operations are all unchanged - not just the two
        # tables this specific failure point touches directly.
        self.assertEqual(self._atomic_write_counts(), before)
        self.assertFalse(self.connection.in_transaction)

    def test_lifecycle_rebuild_failure_rolls_back_everything(self):
        _, channel = self._make_source_and_channel()
        segmented = self._segmented(15)
        before = self._atomic_write_counts()

        with patch(
            "database.service.validate_lifecycle_membership_integrity",
            return_value=["fake violation"],
        ):
            with self.assertRaises(LifecycleIntegrityError):
                self.service.import_channel_batch_with_lifecycle_rebuild(
                    source_name="discord", channel_mode="existing",
                    existing_channel_id=channel.id, reference_date="2026-01-01",
                    timezone="UTC", raw_batch_text=_valid_batch_text(15),
                    segmented_messages=segmented,
                )

        self.assertEqual(self._atomic_write_counts(), before)
        self.assertFalse(self.connection.in_transaction)

    def test_operation_insert_failure_rolls_back_everything(self):
        # The patched create_channel_import_operation() runs only after
        # ingestion and the targeted lifecycle rebuild have both already
        # succeeded for real inside this same transaction - this is the
        # final-step rollback proof.
        _, channel = self._make_source_and_channel()
        segmented = self._segmented(15)
        before = self._atomic_write_counts()

        with patch(
            "database.service.create_channel_import_operation",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                self.service.import_channel_batch_with_lifecycle_rebuild(
                    source_name="discord", channel_mode="existing",
                    existing_channel_id=channel.id, reference_date="2026-01-01",
                    timezone="UTC", raw_batch_text=_valid_batch_text(15),
                    segmented_messages=segmented,
                )

        after = self._atomic_write_counts()
        # Explicit per-table assertions - the rows this failure point's
        # own already-completed earlier steps (ingestion, the targeted
        # lifecycle rebuild) genuinely wrote for real before the forced
        # failure, proven individually rolled back, not merely absent
        # from an aggregate dict that could theoretically compensate a
        # miscount in one table against another.
        self.assertEqual(after["traders"], before["traders"])
        self.assertEqual(after["import_batches"], before["import_batches"])
        self.assertEqual(after["message_extractions"], before["message_extractions"])
        self.assertEqual(after["trade_lifecycles"], before["trade_lifecycles"])
        self.assertEqual(
            after["trade_lifecycle_events"], before["trade_lifecycle_events"]
        )
        self.assertEqual(after, before)
        self.assertFalse(self.connection.in_transaction)

    def test_stored_id_invariant_failure_rolls_back_everything(self):
        _, channel = self._make_source_and_channel()
        segmented = self._segmented(15)
        before = self._atomic_write_counts()

        with patch.object(
            TradeService, "_collect_newly_stored_raw_message_ids",
            side_effect=RuntimeError(
                "Stored batch outcome is missing raw_message_id."
            ),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self.service.import_channel_batch_with_lifecycle_rebuild(
                    source_name="discord", channel_mode="existing",
                    existing_channel_id=channel.id, reference_date="2026-01-01",
                    timezone="UTC", raw_batch_text=_valid_batch_text(15),
                    segmented_messages=segmented,
                )

        self.assertEqual(
            str(ctx.exception), "Stored batch outcome is missing raw_message_id."
        )
        self.assertEqual(self._atomic_write_counts(), before)
        self.assertFalse(self.connection.in_transaction)


class CollectNewlyStoredRawMessageIdsTests(_AtomicImportServiceTestCase):
    """Recovery Milestone R9b mandatory refinement: direct unit coverage
    of TradeService._collect_newly_stored_raw_message_ids(), in addition
    to the public rollback proof in AtomicImportRollbackTests."""

    def test_stored_outcome_missing_raw_message_id_raises_exact_error(self):
        outcome = MessageIngestOutcome(
            sequence_in_batch=1, outcome="stored", channel_id=1,
            raw_message_id=None,  # deliberately malformed - a defensive-
            # validation test, not a realistically reachable outcome.
            external_id="ext-1", parse_status="parsed",
        )
        batch_result = BatchIngestResult(
            import_batch_id=1, channel_id=1, total_segmented=1, stored_count=1,
            duplicate_count=0, unrecognized_count=0, failed_count=0,
            messages=[outcome],
        )

        with self.assertRaises(RuntimeError) as ctx:
            self.service._collect_newly_stored_raw_message_ids(batch_result)

        self.assertEqual(
            str(ctx.exception), "Stored batch outcome is missing raw_message_id."
        )

    def test_count_mismatch_raises_exact_error(self):
        outcome = MessageIngestOutcome(
            sequence_in_batch=1, outcome="stored", channel_id=1,
            raw_message_id=42, external_id="ext-1", parse_status="parsed",
        )
        batch_result = BatchIngestResult(
            import_batch_id=1, channel_id=1, total_segmented=2,
            stored_count=2,  # claims 2 stored, but only 1 outcome is present
            duplicate_count=0, unrecognized_count=0, failed_count=0,
            messages=[outcome],
        )

        with self.assertRaises(RuntimeError) as ctx:
            self.service._collect_newly_stored_raw_message_ids(batch_result)

        self.assertEqual(
            str(ctx.exception),
            "newly_stored_ids count does not match batch_result.stored_count.",
        )

    def test_returns_only_stored_ids_in_order(self):
        stored_1 = MessageIngestOutcome(
            sequence_in_batch=1, outcome="stored", channel_id=1,
            raw_message_id=10, external_id="ext-1", parse_status="parsed",
        )
        duplicate = MessageIngestOutcome(
            sequence_in_batch=2, outcome="duplicate", channel_id=1,
            raw_message_id=99, external_id="ext-2", parse_status=None,
        )
        stored_2 = MessageIngestOutcome(
            sequence_in_batch=3, outcome="stored", channel_id=1,
            raw_message_id=11, external_id="ext-3", parse_status="parsed",
        )
        batch_result = BatchIngestResult(
            import_batch_id=1, channel_id=1, total_segmented=3, stored_count=2,
            duplicate_count=1, unrecognized_count=0, failed_count=0,
            messages=[stored_1, duplicate, stored_2],
        )

        result = self.service._collect_newly_stored_raw_message_ids(batch_result)

        self.assertEqual(result, [10, 11])


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
