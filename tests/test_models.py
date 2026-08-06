"""Model construction tests for Recovery Milestone R6.1.

Covers TradeLifecycle and TradeLifecycleEvent only - the two new dataclasses
added by database/models.py alongside Recovery Milestone R6.1's schema
migration (database/migrations/0007_trade_lifecycles.sql). No test file
existed for database/models.py before this milestone (per
docs/HANDOFFS/2B.4_models.md, the original five V1 models were validated
manually rather than by an automated test file); this file covers only the
two R6.1 additions, not a retroactive test suite for the pre-existing
models, which remains out of R6.1's scope.

These are pure data-shape tests: no database access, no business logic, no
lifecycle-matching behavior - none exists yet as of R6.1.
"""

import dataclasses
import typing
import unittest

from database.models import (
    AtomicChannelImportResult,
    BatchIngestResult,
    Channel,
    ChannelCheckpoint,
    ChannelExternalIdAvailability,
    ChannelImportChannelSummary,
    ChannelImportDuplicatePrediction,
    ChannelImportOperation,
    LifecycleRebuildResult,
    TradeLifecycle,
    TradeLifecycleEvent,
)


class TradeLifecycleModelTests(unittest.TestCase):
    def test_is_a_real_dataclass(self):
        self.assertTrue(dataclasses.is_dataclass(TradeLifecycle))

    def test_is_frozen(self):
        lifecycle = TradeLifecycle(
            trader_id=1, symbol="IBM", status="open", remaining_fraction="1"
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            lifecycle.status = "closed"

    def test_construction_with_required_fields_only(self):
        lifecycle = TradeLifecycle(
            trader_id=1, symbol="IBM", status="open", remaining_fraction="1"
        )
        self.assertEqual(lifecycle.trader_id, 1)
        self.assertEqual(lifecycle.symbol, "IBM")
        self.assertEqual(lifecycle.status, "open")
        self.assertEqual(lifecycle.remaining_fraction, "1")
        # Optional fields default as expected.
        self.assertIsNone(lifecycle.id)
        self.assertIsNone(lifecycle.option_type)
        self.assertIsNone(lifecycle.strike)
        self.assertIsNone(lifecycle.expiration)
        self.assertIsNone(lifecycle.opened_by_signal_id)
        self.assertIsNone(lifecycle.closed_by_signal_id)
        self.assertIs(lifecycle.is_current, True)
        self.assertIsNone(lifecycle.superseded_at)
        self.assertIsNone(lifecycle.ambiguity_flags)
        self.assertIsNone(lifecycle.created_at)
        self.assertIsNone(lifecycle.updated_at)

    def test_construction_with_every_field_populated(self):
        lifecycle = TradeLifecycle(
            id=42,
            trader_id=1,
            symbol="SPX",
            option_type="put",
            strike="7430",
            expiration="2026-07-24",
            status="orphan",
            remaining_fraction="5/6",
            opened_by_signal_id=None,
            closed_by_signal_id=None,
            is_current=False,
            superseded_at="2026-07-28 12:00:00",
            ambiguity_flags=["ambiguous_add_no_open_position"],
            created_at="2026-07-28 11:00:00",
            updated_at="2026-07-28 11:00:00",
        )
        self.assertEqual(lifecycle.id, 42)
        self.assertEqual(lifecycle.option_type, "put")
        self.assertEqual(lifecycle.strike, "7430")
        self.assertEqual(lifecycle.expiration, "2026-07-24")
        self.assertEqual(lifecycle.status, "orphan")
        self.assertEqual(lifecycle.remaining_fraction, "5/6")
        self.assertFalse(lifecycle.is_current)
        self.assertEqual(lifecycle.superseded_at, "2026-07-28 12:00:00")
        self.assertEqual(lifecycle.ambiguity_flags, ["ambiguous_add_no_open_position"])

    def test_remaining_fraction_accepts_non_terminating_rational_string(self):
        # Exercises exactly the reason remaining_fraction is a
        # fractions.Fraction string, not a Decimal string: 1/3 and 1/6 do
        # not terminate in base 10.
        lifecycle = TradeLifecycle(
            trader_id=1, symbol="TSLA", status="closed", remaining_fraction="1/3"
        )
        self.assertEqual(lifecycle.remaining_fraction, "1/3")

    def test_missing_required_field_raises_type_error(self):
        with self.assertRaises(TypeError):
            TradeLifecycle(symbol="IBM", status="open", remaining_fraction="1")

    def test_field_names_match_approved_schema_exactly(self):
        expected_fields = {
            "id", "trader_id", "symbol", "option_type", "strike", "expiration",
            "status", "remaining_fraction", "opened_by_signal_id",
            "closed_by_signal_id", "is_current", "superseded_at",
            "ambiguity_flags", "created_at", "updated_at",
        }
        actual_fields = {f.name for f in dataclasses.fields(TradeLifecycle)}
        self.assertEqual(actual_fields, expected_fields)


class TradeLifecycleEventModelTests(unittest.TestCase):
    def test_is_a_real_dataclass(self):
        self.assertTrue(dataclasses.is_dataclass(TradeLifecycleEvent))

    def test_is_frozen(self):
        event = TradeLifecycleEvent(
            trade_lifecycle_id=1, trade_signal_id=2, sequence_index=1,
            signal_snapshot="{}",
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            event.sequence_index = 2

    def test_construction_with_required_fields_only(self):
        event = TradeLifecycleEvent(
            trade_lifecycle_id=1, trade_signal_id=2, sequence_index=1,
            signal_snapshot="{}",
        )
        self.assertEqual(event.trade_lifecycle_id, 1)
        self.assertEqual(event.trade_signal_id, 2)
        self.assertEqual(event.sequence_index, 1)
        self.assertEqual(event.signal_snapshot, "{}")
        self.assertIsNone(event.id)
        self.assertIsNone(event.created_at)

    def test_construction_with_every_field_populated(self):
        event = TradeLifecycleEvent(
            id=7,
            trade_lifecycle_id=1,
            trade_signal_id=2,
            sequence_index=3,
            signal_snapshot='{"trade_signal_id": 2, "symbol": "IBM"}',
            created_at="2026-07-28 11:00:00",
        )
        self.assertEqual(event.id, 7)
        self.assertEqual(event.sequence_index, 3)
        self.assertEqual(event.signal_snapshot, '{"trade_signal_id": 2, "symbol": "IBM"}')
        self.assertEqual(event.created_at, "2026-07-28 11:00:00")

    def test_signal_snapshot_is_required_not_optional(self):
        # signal_snapshot has no default - unlike every Optional[...] field
        # on this and the other models, a caller must always supply it,
        # matching the schema's TEXT NOT NULL constraint.
        with self.assertRaises(TypeError):
            TradeLifecycleEvent(trade_lifecycle_id=1, trade_signal_id=2, sequence_index=1)

    def test_missing_required_field_raises_type_error(self):
        with self.assertRaises(TypeError):
            TradeLifecycleEvent(trade_signal_id=2, sequence_index=1, signal_snapshot="{}")

    def test_field_names_match_approved_schema_exactly(self):
        expected_fields = {
            "id", "trade_lifecycle_id", "trade_signal_id", "sequence_index",
            "signal_snapshot", "created_at",
        }
        actual_fields = {f.name for f in dataclasses.fields(TradeLifecycleEvent)}
        self.assertEqual(actual_fields, expected_fields)


class ChannelImportOperationModelTests(unittest.TestCase):
    """Recovery Milestone R9a: mirrors channel_import_operations
    (database/migrations/0008_channel_import_operations.sql)
    field-for-field. A row represents only a successfully completed
    confirmed operation - no status/started_at/error field exists here."""

    def _make(self, **overrides):
        values = dict(
            id=1,
            channel_id=10,
            import_batch_id=100,
            reference_date="2026-01-01",
            timezone="UTC",
            processed_count=15,
            stored_count=15,
            duplicate_count=0,
            unrecognized_count=0,
            failed_count=0,
            committed_at="2026-01-01T00:00:00.000000+00:00",
        )
        values.update(overrides)
        return ChannelImportOperation(**values)

    def test_is_a_real_dataclass(self):
        self.assertTrue(dataclasses.is_dataclass(ChannelImportOperation))

    def test_is_frozen(self):
        operation = self._make()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            operation.stored_count = 999

    def test_construction_with_every_field_populated(self):
        operation = self._make()
        self.assertEqual(operation.id, 1)
        self.assertEqual(operation.channel_id, 10)
        self.assertEqual(operation.import_batch_id, 100)
        self.assertEqual(operation.reference_date, "2026-01-01")
        self.assertEqual(operation.timezone, "UTC")
        self.assertEqual(operation.processed_count, 15)
        self.assertEqual(operation.stored_count, 15)
        self.assertEqual(operation.duplicate_count, 0)
        self.assertEqual(operation.unrecognized_count, 0)
        self.assertEqual(operation.failed_count, 0)
        self.assertEqual(operation.committed_at, "2026-01-01T00:00:00.000000+00:00")

    def test_import_batch_id_accepts_none_for_duplicate_only_operation(self):
        operation = self._make(
            import_batch_id=None, stored_count=0, duplicate_count=15
        )
        self.assertIsNone(operation.import_batch_id)

    def test_no_field_has_a_default(self):
        # Every field mirrors a NOT NULL (or explicitly nullable-by-
        # design, import_batch_id) column with no implicit default -
        # every field must always be supplied explicitly.
        with self.assertRaises(TypeError):
            ChannelImportOperation(channel_id=10, import_batch_id=100)

    def test_field_names_match_migration_exactly(self):
        expected_fields = {
            "id", "channel_id", "import_batch_id", "reference_date",
            "timezone", "processed_count", "stored_count", "duplicate_count",
            "unrecognized_count", "failed_count", "committed_at",
        }
        actual_fields = {f.name for f in dataclasses.fields(ChannelImportOperation)}
        self.assertEqual(actual_fields, expected_fields)

    def test_no_field_has_a_default_value_or_factory(self):
        # Every field mirrors a real column with no implicit default -
        # a caller must always supply every value explicitly, including
        # import_batch_id (whose None is meaningful, per the migration's
        # own CHECK constraint, and must never be silently assumed).
        for f in dataclasses.fields(ChannelImportOperation):
            self.assertIs(f.default, dataclasses.MISSING, msg=f.name)
            self.assertIs(f.default_factory, dataclasses.MISSING, msg=f.name)

    def test_type_hints_exact(self):
        hints = typing.get_type_hints(ChannelImportOperation)
        self.assertEqual(hints["id"], int)
        self.assertEqual(hints["channel_id"], int)
        self.assertEqual(hints["import_batch_id"], typing.Optional[int])
        self.assertEqual(hints["reference_date"], str)
        self.assertEqual(hints["timezone"], str)
        self.assertEqual(hints["processed_count"], int)
        self.assertEqual(hints["stored_count"], int)
        self.assertEqual(hints["duplicate_count"], int)
        self.assertEqual(hints["unrecognized_count"], int)
        self.assertEqual(hints["failed_count"], int)
        self.assertEqual(hints["committed_at"], str)


class ChannelExternalIdAvailabilityModelTests(unittest.TestCase):
    def test_is_a_real_dataclass(self):
        self.assertTrue(dataclasses.is_dataclass(ChannelExternalIdAvailability))

    def test_is_frozen(self):
        availability = ChannelExternalIdAvailability(
            external_channel_id="chan-1", is_available=True, existing_channel=None
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            availability.is_available = False

    def test_available_construction(self):
        availability = ChannelExternalIdAvailability(
            external_channel_id="chan-1", is_available=True, existing_channel=None
        )
        self.assertEqual(availability.external_channel_id, "chan-1")
        self.assertTrue(availability.is_available)
        self.assertIsNone(availability.existing_channel)

    def test_unavailable_construction_with_existing_channel(self):
        channel = Channel(id=5, source_id=1, external_channel_id="chan-1")
        availability = ChannelExternalIdAvailability(
            external_channel_id="chan-1", is_available=False, existing_channel=channel
        )
        self.assertFalse(availability.is_available)
        self.assertIs(availability.existing_channel, channel)

    def test_field_names_exact(self):
        expected_fields = {"external_channel_id", "is_available", "existing_channel"}
        actual_fields = {
            f.name for f in dataclasses.fields(ChannelExternalIdAvailability)
        }
        self.assertEqual(actual_fields, expected_fields)

    def test_type_hints_exact(self):
        hints = typing.get_type_hints(ChannelExternalIdAvailability)
        self.assertEqual(hints["external_channel_id"], str)
        self.assertEqual(hints["is_available"], bool)
        self.assertEqual(hints["existing_channel"], typing.Optional[Channel])


class ChannelImportDuplicatePredictionModelTests(unittest.TestCase):
    def test_is_a_real_dataclass(self):
        self.assertTrue(dataclasses.is_dataclass(ChannelImportDuplicatePrediction))

    def test_is_frozen(self):
        prediction = ChannelImportDuplicatePrediction(
            sequence_in_batch=1, external_id="synthetic:abc",
            predicted_duplicate=False, predicted_content_differs=None,
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            prediction.predicted_duplicate = True

    def test_new_message_construction(self):
        prediction = ChannelImportDuplicatePrediction(
            sequence_in_batch=1, external_id="synthetic:abc",
            predicted_duplicate=False, predicted_content_differs=None,
        )
        self.assertEqual(prediction.sequence_in_batch, 1)
        self.assertEqual(prediction.external_id, "synthetic:abc")
        self.assertFalse(prediction.predicted_duplicate)
        self.assertIsNone(prediction.predicted_content_differs)

    def test_duplicate_construction_with_content_differs_populated(self):
        prediction = ChannelImportDuplicatePrediction(
            sequence_in_batch=2, external_id="synthetic:def",
            predicted_duplicate=True, predicted_content_differs=True,
        )
        self.assertTrue(prediction.predicted_duplicate)
        self.assertTrue(prediction.predicted_content_differs)

    def test_field_names_exact(self):
        expected_fields = {
            "sequence_in_batch", "external_id", "predicted_duplicate",
            "predicted_content_differs",
        }
        actual_fields = {
            f.name for f in dataclasses.fields(ChannelImportDuplicatePrediction)
        }
        self.assertEqual(actual_fields, expected_fields)

    def test_type_hints_exact(self):
        hints = typing.get_type_hints(ChannelImportDuplicatePrediction)
        self.assertEqual(hints["sequence_in_batch"], int)
        self.assertEqual(hints["external_id"], str)
        self.assertEqual(hints["predicted_duplicate"], bool)
        self.assertEqual(hints["predicted_content_differs"], typing.Optional[bool])


class ChannelImportChannelSummaryModelTests(unittest.TestCase):
    def _channel(self):
        return Channel(id=5, source_id=1, external_channel_id="chan-1", name="general")

    def test_is_a_real_dataclass(self):
        self.assertTrue(dataclasses.is_dataclass(ChannelImportChannelSummary))

    def test_is_frozen(self):
        summary = ChannelImportChannelSummary(
            channel=self._channel(), checkpoint=None, latest_operation=None
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            summary.checkpoint = None

    def test_construction_with_no_checkpoint_or_operation_yet(self):
        channel = self._channel()
        summary = ChannelImportChannelSummary(
            channel=channel, checkpoint=None, latest_operation=None
        )
        self.assertIs(summary.channel, channel)
        self.assertIsNone(summary.checkpoint)
        self.assertIsNone(summary.latest_operation)

    def test_construction_with_nested_checkpoint_and_operation(self):
        channel = self._channel()
        checkpoint = ChannelCheckpoint(
            channel_id=5, channel_external_id="chan-1", channel_name="general",
            latest_received_at=None, latest_received_raw_message_id=None,
            latest_received_external_id=None, last_ingested_raw_message_id=99,
            last_ingested_at="2026-01-01T00:00:00.000000+00:00", last_import_batch_id=7,
        )
        operation = ChannelImportOperation(
            id=1, channel_id=5, import_batch_id=7, reference_date="2026-01-01",
            timezone="UTC", processed_count=15, stored_count=15, duplicate_count=0,
            unrecognized_count=0, failed_count=0,
            committed_at="2026-01-01T00:00:00.000000+00:00",
        )
        summary = ChannelImportChannelSummary(
            channel=channel, checkpoint=checkpoint, latest_operation=operation
        )
        self.assertIs(summary.checkpoint, checkpoint)
        self.assertIs(summary.latest_operation, operation)
        # Checkpoint and operation fields never collapse into one
        # namespace - each nested model keeps its own fields separate.
        self.assertEqual(summary.checkpoint.last_import_batch_id, 7)
        self.assertEqual(summary.latest_operation.import_batch_id, 7)

    def test_field_names_exact(self):
        expected_fields = {"channel", "checkpoint", "latest_operation"}
        actual_fields = {
            f.name for f in dataclasses.fields(ChannelImportChannelSummary)
        }
        self.assertEqual(actual_fields, expected_fields)

    def test_type_hints_exact(self):
        hints = typing.get_type_hints(ChannelImportChannelSummary)
        self.assertEqual(hints["channel"], Channel)
        self.assertEqual(hints["checkpoint"], typing.Optional[ChannelCheckpoint])
        self.assertEqual(
            hints["latest_operation"], typing.Optional[ChannelImportOperation]
        )


class AtomicChannelImportResultModelTests(unittest.TestCase):
    """Recovery Milestone R9b: AtomicChannelImportResult - the atomic
    Bulk Channel Import operation's own result model. Mirrors
    ChannelImportChannelSummaryModelTests' own exact style above (the
    closest existing precedent - a flat composition of other models with
    no defaults)."""

    def _channel(self):
        return Channel(id=5, source_id=1, external_channel_id="chan-1", name="general")

    def _batch_result(self):
        return BatchIngestResult(
            import_batch_id=7, channel_id=5, total_segmented=15, stored_count=15,
            duplicate_count=0, unrecognized_count=0, failed_count=0, messages=[],
        )

    def _lifecycle_result(self):
        return LifecycleRebuildResult(
            keys_considered=1, keys_changed=1, keys_unchanged=0,
            lifecycles_superseded=0, lifecycles_created=1,
            lifecycle_events_created=1, signal_pointers_cleared=0,
            signal_pointers_assigned=1,
        )

    def _operation(self):
        return ChannelImportOperation(
            id=1, channel_id=5, import_batch_id=7, reference_date="2026-01-01",
            timezone="UTC", processed_count=15, stored_count=15, duplicate_count=0,
            unrecognized_count=0, failed_count=0,
            committed_at="2026-01-01T00:00:00.000000+00:00",
        )

    def test_is_a_real_dataclass(self):
        self.assertTrue(dataclasses.is_dataclass(AtomicChannelImportResult))

    def test_is_frozen(self):
        result = AtomicChannelImportResult(
            channel=self._channel(), batch_result=self._batch_result(),
            lifecycle_result=self._lifecycle_result(), operation=self._operation(),
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.channel = self._channel()

    def test_construction_with_real_nested_objects(self):
        channel = self._channel()
        batch_result = self._batch_result()
        lifecycle_result = self._lifecycle_result()
        operation = self._operation()
        result = AtomicChannelImportResult(
            channel=channel, batch_result=batch_result,
            lifecycle_result=lifecycle_result, operation=operation,
        )
        self.assertIs(result.channel, channel)
        self.assertIs(result.batch_result, batch_result)
        self.assertIs(result.lifecycle_result, lifecycle_result)
        self.assertIs(result.operation, operation)
        # Nested models keep their own fields separate - never collapsed
        # into one flat namespace.
        self.assertEqual(result.batch_result.stored_count, 15)
        self.assertEqual(result.operation.stored_count, 15)

    def test_field_names_exact(self):
        expected_fields = {
            "channel", "batch_result", "lifecycle_result", "operation"
        }
        actual_fields = {
            f.name for f in dataclasses.fields(AtomicChannelImportResult)
        }
        self.assertEqual(actual_fields, expected_fields)

    def test_field_order_exact(self):
        # Field order is part of the exact dataclass contract - a plain
        # set comparison alone would not catch a reordering.
        actual_order = [f.name for f in dataclasses.fields(AtomicChannelImportResult)]
        self.assertEqual(
            actual_order,
            ["channel", "batch_result", "lifecycle_result", "operation"],
        )

    def test_type_hints_exact(self):
        hints = typing.get_type_hints(AtomicChannelImportResult)
        self.assertEqual(hints["channel"], Channel)
        self.assertEqual(hints["batch_result"], BatchIngestResult)
        self.assertEqual(hints["lifecycle_result"], LifecycleRebuildResult)
        self.assertEqual(hints["operation"], ChannelImportOperation)

    def test_no_default_values(self):
        for f in dataclasses.fields(AtomicChannelImportResult):
            self.assertIs(f.default, dataclasses.MISSING)
            self.assertIs(f.default_factory, dataclasses.MISSING)


if __name__ == "__main__":
    unittest.main()
