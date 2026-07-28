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
import unittest

from database.models import TradeLifecycle, TradeLifecycleEvent


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


if __name__ == "__main__":
    unittest.main()
