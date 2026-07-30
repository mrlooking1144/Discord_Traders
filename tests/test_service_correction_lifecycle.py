"""Tests for Recovery Milestone R6.5a: TradeService.correct_trade_signal()
- the lifecycle-safe trade-signal correction service contract.

Covers database/service.py's new correct_trade_signal() method and its
private no-commit helper only - no UI integration (that is R6.5b), no
real-corpus acceptance (that is R6.6). The pre-existing legacy/controlled
TradeService.update_trade_signal() contract (Milestone 2B.6b/2D.5) and the
R6.4 lifecycle rebuild orchestration (rebuild_all_lifecycles/
rebuild_lifecycles_for_raw_message_ids) are exercised only through
TradeService's public API, never re-tested directly here - see
tests/test_service.py and tests/test_service_lifecycle.py for their own
dedicated coverage.
"""

import json
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

from database.config import DatabaseConfig
from database.db import get_connection, initialize_database
from database.repository import (
    create_raw_message,
    create_trade_lifecycle,
    create_trade_lifecycle_event,
    create_trade_signal,
    create_trader,
    get_current_lifecycle_ids_for_raw_message_ids,
    get_or_create_source,
    get_trade_lifecycle_by_id,
    get_trade_lifecycle_events,
    get_trade_signal_by_id,
    get_trade_signal_edits,
    update_trade_signal_lifecycle_pointer,
    validate_lifecycle_membership_integrity,
)
from database.repository import persist_lifecycle_builds as real_persist_lifecycle_builds
from database.service import (
    LifecycleIntegrityError,
    LifecycleSnapshotError,
    LifecycleUnsafeCorrectionError,
    StaleTradeSignalError,
    TradeService,
    TradeSignalNotFoundError,
)

_CORRECTION_FIELDS = ("symbol", "action", "option_type", "price", "expiration", "position_size")


class _CorrectionLifecycleTestCase(unittest.TestCase):
    """Shared fixture: a unique temporary real SQLite database per test,
    the same setup shape as tests/test_service_lifecycle.py's
    _LifecycleServiceTestCase, kept independent (not imported) so this
    module remains a clearly isolated, standalone R6.5a test module."""

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = path
        self.config = DatabaseConfig(db_path=path)
        initialize_database(self.config)
        self.connection = get_connection(self.config)
        self.service = TradeService(self.connection)
        self.source = get_or_create_source(self.connection, "discord")
        self.connection.commit()
        self.trader = create_trader(self.connection, self.source.id, "TC")
        self.connection.commit()

    def tearDown(self):
        self.connection.close()
        os.remove(self.db_path)

    def _make_signal(
        self,
        symbol="IBM",
        option_type="call",
        strike=Decimal("207.5"),
        expiration="2026-07-24",
        event_type="ENTRY",
        action="BOUGHT",
        price=None,
        position_size=None,
        qualifier=None,
        extraction_id=None,
    ):
        raw_message = create_raw_message(self.connection, self.source.id, "x")
        signal = create_trade_signal(
            self.connection,
            raw_message.id,
            self.trader.id,
            symbol,
            action,
            option_type=option_type,
            price=price,
            expiration=expiration,
            position_size=position_size,
            strike=strike,
            event_type=event_type,
            qualifier=qualifier,
            extraction_id=extraction_id,
        )
        self.connection.commit()
        return signal, raw_message

    def _values(self, symbol, action, option_type, price, expiration, position_size):
        return {
            "symbol": symbol,
            "action": action,
            "option_type": option_type,
            "price": price,
            "expiration": expiration,
            "position_size": position_size,
        }

    def _edit_count(self, trade_signal_id=None):
        if trade_signal_id is None:
            return self.connection.execute(
                "SELECT COUNT(*) FROM trade_signal_edits"
            ).fetchone()[0]
        return len(get_trade_signal_edits(self.connection, trade_signal_id))

    def _capture_full_state(self):
        """Every trade_lifecycles row (id, is_current), every
        trade_lifecycle_events row (id, trade_lifecycle_id, trade_signal_id,
        sequence_index, signal_snapshot), and every trade_signals.lifecycle_id
        pointer - the complete state a correction's rebuild could touch."""
        lifecycles = self.connection.execute(
            "SELECT id, is_current FROM trade_lifecycles ORDER BY id"
        ).fetchall()
        events = self.connection.execute(
            "SELECT id, trade_lifecycle_id, trade_signal_id, sequence_index, signal_snapshot "
            "FROM trade_lifecycle_events ORDER BY id"
        ).fetchall()
        pointers = self.connection.execute(
            "SELECT id, lifecycle_id FROM trade_signals ORDER BY id"
        ).fetchall()
        return (
            [tuple(row) for row in lifecycles],
            [tuple(row) for row in events],
            [tuple(row) for row in pointers],
        )

    def _assert_zero_rebuild_result(self, result):
        self.assertEqual(result.keys_considered, 0)
        self.assertEqual(result.keys_changed, 0)
        self.assertEqual(result.keys_unchanged, 0)
        self.assertEqual(result.lifecycles_superseded, 0)
        self.assertEqual(result.lifecycles_created, 0)
        self.assertEqual(result.lifecycle_events_created, 0)
        self.assertEqual(result.signal_pointers_cleared, 0)
        self.assertEqual(result.signal_pointers_assigned, 0)


class BasicCorrectionTests(_CorrectionLifecycleTestCase):
    """Non-key-field corrections: succeed, never rebuild, always audit
    exactly once, and are durably committed."""

    def test_price_only_correction_succeeds(self):
        signal, _ = self._make_signal(price=Decimal("3.00"))
        expected = self._values("IBM", "BOUGHT", "call", Decimal("3.00"), "2026-07-24", None)
        changed = dict(expected, price=Decimal("3.50"))

        result = self.service.correct_trade_signal(signal.id, expected, **changed)
        self.connection.commit()

        self.assertEqual(Decimal(result.trade_signal.price), Decimal("3.50"))
        self.assertFalse(result.lifecycle_rebuild_performed)

    def test_position_size_only_correction_succeeds(self):
        signal, _ = self._make_signal(position_size="10 contracts")
        expected = self._values("IBM", "BOUGHT", "call", None, "2026-07-24", "10 contracts")
        changed = dict(expected, position_size="20 contracts")

        result = self.service.correct_trade_signal(signal.id, expected, **changed)
        self.connection.commit()

        self.assertEqual(result.trade_signal.position_size, "20 contracts")
        self.assertFalse(result.lifecycle_rebuild_performed)

    def test_non_key_correction_does_not_rebuild_even_when_lifecycle_managed(self):
        signal, _ = self._make_signal(event_type="ENTRY", price=Decimal("3.00"))
        self.service.rebuild_all_lifecycles()
        pre_lifecycle_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id
        self.assertIsNotNone(pre_lifecycle_id)

        expected = self._values("IBM", "BOUGHT", "call", Decimal("3.00"), "2026-07-24", None)
        changed = dict(expected, price=Decimal("3.75"))

        result = self.service.correct_trade_signal(signal.id, expected, **changed)
        self.connection.commit()

        self.assertFalse(result.lifecycle_rebuild_performed)
        self._assert_zero_rebuild_result(result.lifecycle_rebuild_result)
        self.assertEqual(result.trade_signal.lifecycle_id, pre_lifecycle_id)

    def test_exactly_one_audit_record_created(self):
        signal, _ = self._make_signal(price=Decimal("3.00"))
        expected = self._values("IBM", "BOUGHT", "call", Decimal("3.00"), "2026-07-24", None)
        changed = dict(expected, price=Decimal("3.50"))

        self.assertEqual(self._edit_count(signal.id), 0)
        self.service.correct_trade_signal(signal.id, expected, **changed)
        self.connection.commit()
        self.assertEqual(self._edit_count(signal.id), 1)

        edits = get_trade_signal_edits(self.connection, signal.id)
        previous = json.loads(edits[0].previous_values)
        self.assertEqual(previous["price"], "3.00")

    def test_result_contains_corrected_authoritative_signal(self):
        signal, _ = self._make_signal(price=Decimal("3.00"))
        expected = self._values("IBM", "BOUGHT", "call", Decimal("3.00"), "2026-07-24", None)
        changed = dict(expected, price=Decimal("4.25"))

        result = self.service.correct_trade_signal(signal.id, expected, **changed)

        self.assertEqual(result.trade_signal.id, signal.id)
        self.assertEqual(Decimal(result.trade_signal.price), Decimal("4.25"))

    def test_fresh_connection_sees_committed_correction(self):
        signal, _ = self._make_signal(price=Decimal("3.00"))
        expected = self._values("IBM", "BOUGHT", "call", Decimal("3.00"), "2026-07-24", None)
        changed = dict(expected, price=Decimal("5.00"))

        self.service.correct_trade_signal(signal.id, expected, **changed)

        fresh = get_connection(self.config)
        try:
            reloaded = get_trade_signal_by_id(fresh, signal.id)
            self.assertEqual(Decimal(reloaded.price), Decimal("5.00"))
        finally:
            fresh.close()


class LifecycleKeyCorrectionTests(_CorrectionLifecycleTestCase):
    """Corrections to symbol/option_type/expiration on a lifecycle-managed
    signal must trigger a targeted rebuild through the corrected signal's
    raw_message_id, correctly moving membership from the old key to the
    new one."""

    def _base_values(self):
        return self._values("IBM", "BOUGHT", "call", None, "2026-07-24", None)

    def test_symbol_correction_rebuilds_and_moves_membership(self):
        signal, raw = self._make_signal()
        self.service.rebuild_all_lifecycles()
        old_lifecycle_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id
        self.assertIsNotNone(old_lifecycle_id)

        # An unrelated signal/key, to prove it is left untouched.
        unrelated_signal, _ = self._make_signal(symbol="MSFT", strike=Decimal("380"))
        self.service.rebuild_all_lifecycles()
        unrelated_lifecycle_id = get_trade_signal_by_id(
            self.connection, unrelated_signal.id
        ).lifecycle_id
        self.assertIsNotNone(unrelated_lifecycle_id)
        pre_unrelated_state = (
            get_trade_lifecycle_by_id(self.connection, unrelated_lifecycle_id).is_current,
            get_trade_signal_by_id(self.connection, unrelated_signal.id).lifecycle_id,
        )

        expected = self._base_values()
        changed = dict(expected, symbol="AVGO")

        result = self.service.correct_trade_signal(signal.id, expected, **changed)
        self.connection.commit()

        self.assertTrue(result.lifecycle_rebuild_performed)
        self.assertFalse(get_trade_lifecycle_by_id(self.connection, old_lifecycle_id).is_current)

        new_lifecycle_id = result.trade_signal.lifecycle_id
        self.assertIsNotNone(new_lifecycle_id)
        self.assertNotEqual(new_lifecycle_id, old_lifecycle_id)
        new_lifecycle = get_trade_lifecycle_by_id(self.connection, new_lifecycle_id)
        self.assertEqual(new_lifecycle.symbol, "AVGO")

        # Corrected signal points only to the new lifecycle, never the old.
        self.assertEqual(
            get_trade_signal_by_id(self.connection, signal.id).lifecycle_id, new_lifecycle_id
        )

        # Unrelated key/signal is completely untouched.
        post_unrelated_state = (
            get_trade_lifecycle_by_id(self.connection, unrelated_lifecycle_id).is_current,
            get_trade_signal_by_id(self.connection, unrelated_signal.id).lifecycle_id,
        )
        self.assertEqual(pre_unrelated_state, post_unrelated_state)

        # Old and new keys are each rebuilt exactly once (2 changed keys:
        # IBM superseded-with-no-replacement, AVGO newly created).
        self.assertEqual(result.lifecycle_rebuild_result.keys_considered, 2)
        self.assertEqual(result.lifecycle_rebuild_result.keys_changed, 2)
        self.assertEqual(result.lifecycle_rebuild_result.keys_unchanged, 0)

    def test_option_type_correction_rebuilds(self):
        signal, raw = self._make_signal()
        self.service.rebuild_all_lifecycles()
        old_lifecycle_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id

        expected = self._base_values()
        changed = dict(expected, option_type="put")

        result = self.service.correct_trade_signal(signal.id, expected, **changed)
        self.connection.commit()

        self.assertTrue(result.lifecycle_rebuild_performed)
        self.assertFalse(get_trade_lifecycle_by_id(self.connection, old_lifecycle_id).is_current)
        new_lifecycle_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id
        self.assertNotEqual(new_lifecycle_id, old_lifecycle_id)
        self.assertEqual(
            get_trade_lifecycle_by_id(self.connection, new_lifecycle_id).option_type, "put"
        )

    def test_expiration_correction_rebuilds(self):
        signal, raw = self._make_signal()
        self.service.rebuild_all_lifecycles()
        old_lifecycle_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id

        expected = self._base_values()
        changed = dict(expected, expiration="2026-08-21")

        result = self.service.correct_trade_signal(signal.id, expected, **changed)
        self.connection.commit()

        self.assertTrue(result.lifecycle_rebuild_performed)
        self.assertFalse(get_trade_lifecycle_by_id(self.connection, old_lifecycle_id).is_current)
        new_lifecycle_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id
        self.assertNotEqual(new_lifecycle_id, old_lifecycle_id)
        self.assertEqual(
            get_trade_lifecycle_by_id(self.connection, new_lifecycle_id).expiration, "2026-08-21"
        )

    def test_old_membership_event_remains_but_pointer_no_longer_targets_it(self):
        signal, raw = self._make_signal()
        self.service.rebuild_all_lifecycles()
        old_lifecycle_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id

        expected = self._base_values()
        changed = dict(expected, symbol="AVGO")
        self.service.correct_trade_signal(signal.id, expected, **changed)
        self.connection.commit()

        # The old generation's own membership event is immutable audit
        # history - it is never deleted - but the signal's live pointer no
        # longer targets it.
        old_events = get_trade_lifecycle_events(self.connection, old_lifecycle_id)
        self.assertEqual(len(old_events), 1)
        self.assertEqual(old_events[0].trade_signal_id, signal.id)
        self.assertNotEqual(
            get_trade_signal_by_id(self.connection, signal.id).lifecycle_id, old_lifecycle_id
        )


class CompleteIncompleteTransitionTests(_CorrectionLifecycleTestCase):
    def test_incomplete_becomes_complete_when_missing_components_supplied(self):
        signal, raw = self._make_signal(option_type=None, strike=Decimal("207.5"), expiration=None)
        self.service.rebuild_all_lifecycles()
        old_singleton_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id
        old_singleton = get_trade_lifecycle_by_id(self.connection, old_singleton_id)
        self.assertEqual(old_singleton.status, "unresolved")

        expected = self._values("IBM", "BOUGHT", None, None, None, None)
        changed = dict(expected, option_type="call", expiration="2026-07-24")

        result = self.service.correct_trade_signal(signal.id, expected, **changed)
        self.connection.commit()

        self.assertTrue(result.lifecycle_rebuild_performed)
        self.assertFalse(get_trade_lifecycle_by_id(self.connection, old_singleton_id).is_current)

        new_lifecycle_id = result.trade_signal.lifecycle_id
        self.assertNotEqual(new_lifecycle_id, old_singleton_id)
        new_lifecycle = get_trade_lifecycle_by_id(self.connection, new_lifecycle_id)
        self.assertEqual(new_lifecycle.status, "open")
        self.assertEqual(new_lifecycle.option_type, "call")
        self.assertEqual(new_lifecycle.expiration, "2026-07-24")

        events = get_trade_lifecycle_events(self.connection, new_lifecycle_id)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].trade_signal_id, signal.id)

    def test_complete_becomes_incomplete_when_option_type_cleared(self):
        signal, raw = self._make_signal(option_type="call", strike=Decimal("207.5"), expiration="2026-07-24")
        self.service.rebuild_all_lifecycles()
        old_lifecycle_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id
        self.assertEqual(
            get_trade_lifecycle_by_id(self.connection, old_lifecycle_id).status, "open"
        )

        expected = self._values("IBM", "BOUGHT", "call", None, "2026-07-24", None)
        changed = dict(expected, option_type=None)

        result = self.service.correct_trade_signal(signal.id, expected, **changed)
        self.connection.commit()

        self.assertTrue(result.lifecycle_rebuild_performed)
        self.assertFalse(get_trade_lifecycle_by_id(self.connection, old_lifecycle_id).is_current)

        new_singleton_id = result.trade_signal.lifecycle_id
        self.assertNotEqual(new_singleton_id, old_lifecycle_id)
        new_singleton = get_trade_lifecycle_by_id(self.connection, new_singleton_id)
        self.assertEqual(new_singleton.status, "unresolved")
        self.assertEqual(new_singleton.remaining_fraction, "0")
        self.assertEqual(new_singleton.ambiguity_flags, ["incomplete_contract_identity"])

        events = get_trade_lifecycle_events(self.connection, new_singleton_id)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].trade_signal_id, signal.id)


class ActionRuleTests(_CorrectionLifecycleTestCase):
    def test_managed_signal_action_change_rejected(self):
        signal, _ = self._make_signal(event_type="ENTRY", action="BOUGHT")
        self.service.rebuild_all_lifecycles()
        pre_state = self._capture_full_state()
        pre_edit_count = self._edit_count()

        expected = self._values("IBM", "BOUGHT", "call", None, "2026-07-24", None)
        changed = dict(expected, action="SOLD")

        with self.assertRaises(LifecycleUnsafeCorrectionError):
            self.service.correct_trade_signal(signal.id, expected, **changed)

        self.assertEqual(self._edit_count(), pre_edit_count)
        self.assertEqual(get_trade_signal_by_id(self.connection, signal.id).action, "BOUGHT")
        self.assertEqual(self._capture_full_state(), pre_state)
        self.assertFalse(self.connection.in_transaction)

    def test_legacy_signal_action_correction_still_allowed(self):
        signal, _ = self._make_signal(event_type=None, action="BOUGHT")

        expected = self._values("IBM", "BOUGHT", "call", None, "2026-07-24", None)
        changed = dict(expected, action="SOLD")

        result = self.service.correct_trade_signal(signal.id, expected, **changed)
        self.connection.commit()

        self.assertEqual(result.trade_signal.action, "SOLD")
        self.assertFalse(result.lifecycle_rebuild_performed)
        self.assertEqual(self._edit_count(signal.id), 1)

    def test_same_action_with_other_change_is_not_treated_as_unsafe(self):
        signal, _ = self._make_signal(event_type="ENTRY", action="BOUGHT", price=Decimal("3.00"))

        expected = self._values("IBM", "BOUGHT", "call", Decimal("3.00"), "2026-07-24", None)
        changed = dict(expected, action="BOUGHT", price=Decimal("3.50"))

        result = self.service.correct_trade_signal(signal.id, expected, **changed)
        self.connection.commit()

        self.assertEqual(Decimal(result.trade_signal.price), Decimal("3.50"))
        self.assertFalse(result.lifecycle_rebuild_performed)


class StaleAndNoOpProtectionTests(_CorrectionLifecycleTestCase):
    def test_stale_expected_values_rejected_before_writes(self):
        signal, _ = self._make_signal(price=Decimal("3.00"))
        wrong_expected = self._values("IBM", "BOUGHT", "call", Decimal("2.00"), "2026-07-24", None)
        changed = dict(wrong_expected, price=Decimal("3.50"))

        with self.assertRaises(StaleTradeSignalError):
            self.service.correct_trade_signal(signal.id, wrong_expected, **changed)

        self.assertEqual(self._edit_count(signal.id), 0)
        self.assertEqual(Decimal(get_trade_signal_by_id(self.connection, signal.id).price), Decimal("3.00"))
        self.assertFalse(self.connection.in_transaction)

    def test_missing_signal_rejected_before_writes(self):
        expected = self._values("IBM", "BOUGHT", "call", None, "2026-07-24", None)
        changed = dict(expected, price=Decimal("1.00"))

        pre_edit_count = self._edit_count()
        with self.assertRaises(TradeSignalNotFoundError):
            self.service.correct_trade_signal(999999, expected, **changed)

        self.assertEqual(self._edit_count(), pre_edit_count)
        self.assertFalse(self.connection.in_transaction)

    def test_no_op_correction_rejected_before_writes(self):
        signal, _ = self._make_signal(price=Decimal("3.00"))
        expected = self._values("IBM", "BOUGHT", "call", Decimal("3.00"), "2026-07-24", None)

        with self.assertRaises(ValueError) as ctx:
            self.service.correct_trade_signal(signal.id, expected, **expected)
        self.assertIs(type(ctx.exception), ValueError)

        self.assertEqual(self._edit_count(signal.id), 0)
        self.assertFalse(self.connection.in_transaction)

    def test_wrong_field_shape_rejected(self):
        signal, _ = self._make_signal()
        expected = self._values("IBM", "BOUGHT", "call", None, "2026-07-24", None)
        bad_changed = dict(expected)
        bad_changed.pop("price")

        with self.assertRaises(ValueError):
            self.service.correct_trade_signal(signal.id, expected, **bad_changed)

        self.assertEqual(self._edit_count(signal.id), 0)

    def test_non_decimal_price_raises_type_error_before_writes(self):
        signal, _ = self._make_signal(price=Decimal("3.00"))
        expected = self._values("IBM", "BOUGHT", "call", Decimal("3.00"), "2026-07-24", None)
        changed = dict(expected, price="3.50")

        with self.assertRaises(TypeError):
            self.service.correct_trade_signal(signal.id, expected, **changed)

        self.assertEqual(self._edit_count(signal.id), 0)


class TransactionRollbackTests(_CorrectionLifecycleTestCase):
    """Every test here forces a real failure only after at least the
    audit insert and the signal update have already happened for real
    within the same transaction, then verifies (via a fresh connection
    where practical) that the entire operation - audit, signal update,
    and any lifecycle writes - was rolled back atomically."""

    def test_rollback_on_targeted_rebuild_engine_failure(self):
        signal, raw = self._make_signal()
        self.service.rebuild_all_lifecycles()
        old_lifecycle_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id
        pre_state = self._capture_full_state()
        pre_edit_count = self._edit_count()

        expected = self._values("IBM", "BOUGHT", "call", None, "2026-07-24", None)
        changed = dict(expected, symbol="AVGO")

        with patch("database.service.build_lifecycle_sequence", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.service.correct_trade_signal(signal.id, expected, **changed)

        fresh = get_connection(self.config)
        try:
            reloaded = get_trade_signal_by_id(fresh, signal.id)
            self.assertEqual(reloaded.symbol, "IBM")
            self.assertEqual(reloaded.lifecycle_id, old_lifecycle_id)
        finally:
            fresh.close()

        self.assertEqual(self._edit_count(), pre_edit_count)
        self.assertEqual(self._capture_full_state(), pre_state)
        self.assertTrue(get_trade_lifecycle_by_id(self.connection, old_lifecycle_id).is_current)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM trade_lifecycles WHERE symbol = 'AVGO'"
            ).fetchone()[0],
            0,
        )
        self.assertFalse(self.connection.in_transaction)

    def test_rollback_on_repository_persist_failure_after_real_rebuild_work(self):
        signal, raw = self._make_signal()
        self.service.rebuild_all_lifecycles()
        old_lifecycle_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id
        pre_edit_count = self._edit_count()

        expected = self._values("IBM", "BOUGHT", "call", None, "2026-07-24", None)
        changed = dict(expected, symbol="AVGO")

        def fake_persist(*args, **kwargs):
            result = real_persist_lifecycle_builds(*args, **kwargs)
            raise sqlite3.OperationalError("boom - after real persistence")

        with patch("database.service.persist_lifecycle_builds", side_effect=fake_persist):
            with self.assertRaises(sqlite3.OperationalError):
                self.service.correct_trade_signal(signal.id, expected, **changed)

        self.assertEqual(self._edit_count(), pre_edit_count)
        self.assertEqual(get_trade_signal_by_id(self.connection, signal.id).symbol, "IBM")
        self.assertEqual(
            get_trade_signal_by_id(self.connection, signal.id).lifecycle_id, old_lifecycle_id
        )
        self.assertTrue(get_trade_lifecycle_by_id(self.connection, old_lifecycle_id).is_current)
        self.assertFalse(self.connection.in_transaction)

    def test_rollback_on_membership_integrity_violation_without_rebuild(self):
        signal, raw = self._make_signal(price=Decimal("3.00"))
        self.service.rebuild_all_lifecycles()
        pre_edit_count = self._edit_count()
        pre_state = self._capture_full_state()

        expected = self._values("IBM", "BOUGHT", "call", Decimal("3.00"), "2026-07-24", None)
        changed = dict(expected, price=Decimal("3.50"))

        with patch(
            "database.service.validate_lifecycle_membership_integrity",
            return_value=["fake violation for testing"],
        ):
            with self.assertRaises(LifecycleIntegrityError):
                self.service.correct_trade_signal(signal.id, expected, **changed)

        self.assertEqual(self._edit_count(), pre_edit_count)
        self.assertEqual(
            Decimal(get_trade_signal_by_id(self.connection, signal.id).price), Decimal("3.00")
        )
        self.assertEqual(self._capture_full_state(), pre_state)
        self.assertFalse(self.connection.in_transaction)

    def test_rollback_on_snapshot_error_during_required_rebuild(self):
        signal, raw = self._make_signal()
        self.service.rebuild_all_lifecycles()
        old_lifecycle_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id
        event = get_trade_lifecycle_events(self.connection, old_lifecycle_id)[0]
        self.connection.execute(
            "UPDATE trade_lifecycle_events SET signal_snapshot = ? WHERE id = ?",
            ("bad json", event.id),
        )
        self.connection.commit()
        pre_edit_count = self._edit_count()

        expected = self._values("IBM", "BOUGHT", "call", None, "2026-07-24", None)
        changed = dict(expected, symbol="AVGO")

        with self.assertRaises(LifecycleSnapshotError):
            self.service.correct_trade_signal(signal.id, expected, **changed)

        self.assertEqual(self._edit_count(), pre_edit_count)
        reloaded = get_trade_signal_by_id(self.connection, signal.id)
        self.assertEqual(reloaded.symbol, "IBM")
        self.assertEqual(reloaded.lifecycle_id, old_lifecycle_id)
        self.assertTrue(get_trade_lifecycle_by_id(self.connection, old_lifecycle_id).is_current)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM trade_lifecycles WHERE symbol = 'AVGO'"
            ).fetchone()[0],
            0,
        )
        self.assertFalse(self.connection.in_transaction)


class LegacyStaleLifecycleStateTests(_CorrectionLifecycleTestCase):
    """Covers the defensive rebuild-decision rule for a legacy
    (event_type IS NULL) signal that unexpectedly carries stale lifecycle
    state - a non-null lifecycle_id pointer and/or current lifecycle-event
    lineage referencing its raw_message_id. This should never arise
    through the approved API surface (ingestion never sets a legacy
    signal's event_type, and the lifecycle engine never links an
    ineligible signal), but the correction service must self-heal it if
    found, per the approved R6.5a rebuild-decision rules."""

    def test_legacy_signal_with_stale_lifecycle_state_triggers_cleanup_rebuild(self):
        signal, raw = self._make_signal(
            event_type=None, action="BOUGHT", price=Decimal("3.00")
        )

        # Manually simulate stale residue: a real current lifecycle
        # generation, a real membership event, and a real pointer, all
        # referencing this legacy signal - never producible through the
        # approved API, but exactly the shape the rebuild-decision rule
        # must detect and clean up.
        stale_lifecycle = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open",
            remaining_fraction="1", option_type="call", strike=Decimal("207.5"),
            expiration="2026-07-24", opened_by_signal_id=signal.id,
        )
        self.connection.commit()
        snapshot = {
            "trade_signal_id": signal.id, "raw_message_id": raw.id,
            "trader_id": self.trader.id, "symbol": "IBM", "option_type": "call",
            "strike": "207.5", "expiration": "2026-07-24", "event_type": None,
            "qualifier": None, "action": "BOUGHT", "price": "3.00",
            "stated_entry_price": None, "stated_return_pct": None, "notes": None,
            "extraction_id": None, "ordering_key": [raw.id, signal.id],
        }
        create_trade_lifecycle_event(
            self.connection, stale_lifecycle.id, signal.id, 1, json.dumps(snapshot)
        )
        update_trade_signal_lifecycle_pointer(self.connection, signal.id, stale_lifecycle.id)
        self.connection.commit()

        pre_signal = get_trade_signal_by_id(self.connection, signal.id)
        self.assertIsNotNone(pre_signal.lifecycle_id)
        self.assertEqual(
            get_current_lifecycle_ids_for_raw_message_ids(self.connection, [raw.id]),
            [stale_lifecycle.id],
        )
        pre_edit_count = self._edit_count(signal.id)

        expected = self._values("IBM", "BOUGHT", "call", Decimal("3.00"), "2026-07-24", None)
        changed = dict(expected, price=Decimal("3.75"))

        # Only the public API is exercised here - no internal helper is
        # called directly. No mocking of the transaction, the rebuild, the
        # repository update, or the integrity validator.
        result = self.service.correct_trade_signal(signal.id, expected, **changed)

        self.assertTrue(result.lifecycle_rebuild_performed)
        self.assertEqual(result.lifecycle_rebuild_result.lifecycles_superseded, 1)
        self.assertEqual(result.lifecycle_rebuild_result.signal_pointers_cleared, 1)
        self.assertEqual(result.lifecycle_rebuild_result.lifecycles_created, 0)

        self.assertFalse(
            get_trade_lifecycle_by_id(self.connection, stale_lifecycle.id).is_current
        )
        self.assertIsNone(result.trade_signal.lifecycle_id)
        self.assertEqual(Decimal(result.trade_signal.price), Decimal("3.75"))
        self.assertEqual(self._edit_count(signal.id), pre_edit_count + 1)

        self.assertEqual(
            get_current_lifecycle_ids_for_raw_message_ids(self.connection, [raw.id]), []
        )
        self.assertEqual(validate_lifecycle_membership_integrity(self.connection), [])

        fresh = get_connection(self.config)
        try:
            reloaded = get_trade_signal_by_id(fresh, signal.id)
            self.assertIsNone(reloaded.lifecycle_id)
            self.assertEqual(Decimal(reloaded.price), Decimal("3.75"))
        finally:
            fresh.close()


class CoexistenceWithLegacyApiTests(_CorrectionLifecycleTestCase):
    """Proves correct_trade_signal() and the pre-existing
    update_trade_signal() (both legacy and its own controlled-correction
    mode) coexist on the same connection without interference."""

    def test_legacy_update_and_new_correction_on_distinct_signals(self):
        legacy_signal, _ = self._make_signal(
            symbol="SPY", event_type=None, strike=None, option_type=None, expiration=None
        )
        managed_signal, _ = self._make_signal(symbol="QQQ", price=Decimal("1.00"))

        updated = self.service.update_trade_signal(legacy_signal.id, position_size="5 contracts")
        self.connection.commit()
        self.assertEqual(updated.position_size, "5 contracts")

        expected = self._values("QQQ", "BOUGHT", "call", Decimal("1.00"), "2026-07-24", None)
        changed = dict(expected, price=Decimal("1.25"))
        result = self.service.correct_trade_signal(managed_signal.id, expected, **changed)
        self.connection.commit()

        self.assertEqual(Decimal(result.trade_signal.price), Decimal("1.25"))
        self.assertEqual(self._edit_count(legacy_signal.id), 1)
        self.assertEqual(self._edit_count(managed_signal.id), 1)


if __name__ == "__main__":
    unittest.main()
