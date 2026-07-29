"""Tests for Recovery Milestone R6.4: TradeService lifecycle rebuild
orchestration (rebuild_all_lifecycles / rebuild_lifecycles_for_raw_message_ids).

Covers database/service.py's new lifecycle methods only - no UI, no
correction-workflow integration (that is R6.5a), no real-corpus acceptance
(that is R6.6). database/lifecycle.py (the pure engine) and
database/repository.py's persistence layer are exercised only through
TradeService's public API, never re-tested directly here.
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
from database.lifecycle import FLAG_INCOMPLETE_CONTRACT_IDENTITY
from database.lifecycle import build_lifecycle_sequence as real_build_lifecycle_sequence
from database.repository import (
    create_message_extraction,
    create_raw_message,
    create_trade_signal,
    create_trader,
    get_or_create_source,
    get_trade_lifecycle_by_id,
    get_trade_lifecycle_events,
    get_trade_signal_by_id,
    supersede_extraction,
)
from database.repository import persist_lifecycle_builds as real_persist_lifecycle_builds
from database.service import LifecycleIntegrityError, LifecycleSnapshotError, TradeService


class _LifecycleServiceTestCase(unittest.TestCase):
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
        strike=None,
        expiration="2026-07-24",
        event_type="ENTRY",
        qualifier=None,
        action="BOUGHT",
        received_at=None,
        extraction_id=None,
        price=None,
    ):
        raw_message = create_raw_message(
            self.connection, self.source.id, "x", received_at=received_at
        )
        signal = create_trade_signal(
            self.connection,
            raw_message.id,
            self.trader.id,
            symbol,
            action,
            option_type=option_type,
            strike=strike,
            expiration=expiration,
            event_type=event_type,
            qualifier=qualifier,
            extraction_id=extraction_id,
            price=price,
        )
        self.connection.commit()
        return signal, raw_message

    def _make_signal_with_extraction(self, **kwargs):
        """Like _make_signal(), but wires a real, current message_extractions
        row so the caller can later supersede it (an extraction_id of None
        is always treated as current, regardless of the extractions table -
        needed to simulate a signal "disappearing" via reprocessing)."""
        raw_message = create_raw_message(self.connection, self.source.id, "x")
        self.connection.commit()
        extraction = create_message_extraction(
            self.connection, raw_message.id, parser_version="v1", parse_status="parsed"
        )
        self.connection.commit()
        symbol = kwargs.pop("symbol", "IBM")
        option_type = kwargs.pop("option_type", "call")
        strike = kwargs.pop("strike", None)
        expiration = kwargs.pop("expiration", "2026-07-24")
        event_type = kwargs.pop("event_type", "ENTRY")
        qualifier = kwargs.pop("qualifier", None)
        action = kwargs.pop("action", "BOUGHT")
        signal = create_trade_signal(
            self.connection,
            raw_message.id,
            self.trader.id,
            symbol,
            action,
            option_type=option_type,
            strike=strike,
            expiration=expiration,
            event_type=event_type,
            qualifier=qualifier,
            extraction_id=extraction.id,
        )
        self.connection.commit()
        return signal, raw_message, extraction

    def _counts(self):
        return {
            "trade_lifecycles": self.connection.execute(
                "SELECT COUNT(*) FROM trade_lifecycles"
            ).fetchone()[0],
            "trade_lifecycle_events": self.connection.execute(
                "SELECT COUNT(*) FROM trade_lifecycle_events"
            ).fetchone()[0],
        }

    def _capture_full_state(self):
        """Every trade_lifecycles row (id, is_current), every
        trade_lifecycle_events row (id, trade_lifecycle_id, trade_signal_id,
        sequence_index, signal_snapshot), and every trade_signals.lifecycle_id
        pointer - the complete state a rebuild call could possibly touch."""
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

    def _assert_zero_result(self, result):
        self.assertEqual(result.keys_considered, 0)
        self.assertEqual(result.keys_changed, 0)
        self.assertEqual(result.keys_unchanged, 0)
        self.assertEqual(result.lifecycles_superseded, 0)
        self.assertEqual(result.lifecycles_created, 0)
        self.assertEqual(result.lifecycle_events_created, 0)
        self.assertEqual(result.signal_pointers_cleared, 0)
        self.assertEqual(result.signal_pointers_assigned, 0)

    def _valid_snapshot_dict(self, trade_signal_id, raw_message_id):
        return {
            "trade_signal_id": trade_signal_id,
            "raw_message_id": raw_message_id,
            "trader_id": self.trader.id,
            "symbol": "IBM",
            "option_type": "call",
            "strike": "207.5",
            "expiration": "2026-07-24",
            "event_type": "ENTRY",
            "qualifier": None,
            "action": "BOUGHT",
            "price": None,
            "stated_entry_price": None,
            "stated_return_pct": None,
            "notes": None,
            "extraction_id": None,
            "ordering_key": [raw_message_id, trade_signal_id],
        }


class BasicOrchestrationTests(_LifecycleServiceTestCase):
    def test_empty_database_full_rebuild(self):
        self._assert_zero_result(self.service.rebuild_all_lifecycles())
        self.assertEqual(self._counts(), {"trade_lifecycles": 0, "trade_lifecycle_events": 0})

    def test_empty_targeted_raw_message_list(self):
        self._assert_zero_result(self.service.rebuild_lifecycles_for_raw_message_ids([]))
        self.assertEqual(self._counts(), {"trade_lifecycles": 0, "trade_lifecycle_events": 0})

    def test_single_entry_produces_one_open_lifecycle(self):
        signal, _ = self._make_signal(strike=Decimal("207.5"))

        result = self.service.rebuild_all_lifecycles()

        self.assertEqual(result.keys_considered, 1)
        self.assertEqual(result.keys_changed, 1)
        self.assertEqual(result.lifecycles_created, 1)
        self.assertEqual(result.lifecycle_events_created, 1)
        self.assertEqual(result.signal_pointers_assigned, 1)
        lifecycle_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id
        self.assertIsNotNone(lifecycle_id)
        self.assertEqual(get_trade_lifecycle_by_id(self.connection, lifecycle_id).status, "open")

    def test_entry_plus_partial_exit(self):
        entry, _ = self._make_signal(strike=Decimal("207.5"))
        self._make_signal(
            strike=Decimal("207.5"), event_type="PARTIAL_EXIT", qualifier="1/2", action="SOLD"
        )

        self.service.rebuild_all_lifecycles()

        lifecycle_id = get_trade_signal_by_id(self.connection, entry.id).lifecycle_id
        lifecycle = get_trade_lifecycle_by_id(self.connection, lifecycle_id)
        self.assertEqual(lifecycle.status, "partially_closed")
        self.assertEqual(lifecycle.remaining_fraction, "1/2")

    def test_entry_plus_full_exit(self):
        entry, _ = self._make_signal(strike=Decimal("207.5"))
        self._make_signal(
            strike=Decimal("207.5"), event_type="FULL_EXIT", qualifier="ALL OUT", action="SOLD"
        )

        self.service.rebuild_all_lifecycles()

        lifecycle_id = get_trade_signal_by_id(self.connection, entry.id).lifecycle_id
        self.assertEqual(get_trade_lifecycle_by_id(self.connection, lifecycle_id).status, "closed")

    def test_orphan_exit(self):
        exit_signal, _ = self._make_signal(
            strike=Decimal("207.5"), event_type="FULL_EXIT", qualifier="ALL OUT", action="SOLD"
        )

        self.service.rebuild_all_lifecycles()

        lifecycle_id = get_trade_signal_by_id(self.connection, exit_signal.id).lifecycle_id
        self.assertEqual(get_trade_lifecycle_by_id(self.connection, lifecycle_id).status, "orphan")

    def test_unresolved_add_with_no_entry(self):
        add_signal, _ = self._make_signal(strike=Decimal("207.5"), event_type="ADD")

        self.service.rebuild_all_lifecycles()

        lifecycle_id = get_trade_signal_by_id(self.connection, add_signal.id).lifecycle_id
        self.assertEqual(
            get_trade_lifecycle_by_id(self.connection, lifecycle_id).status, "unresolved"
        )

    def test_repeated_closed_generations_at_one_key(self):
        entry1, _ = self._make_signal(
            strike=Decimal("207.5"), received_at="2026-07-24T01:00:00.000000+00:00"
        )
        exit1, _ = self._make_signal(
            strike=Decimal("207.5"), event_type="FULL_EXIT", qualifier="ALL OUT", action="SOLD",
            received_at="2026-07-24T02:00:00.000000+00:00",
        )
        entry2, _ = self._make_signal(
            strike=Decimal("207.5"), received_at="2026-07-24T03:00:00.000000+00:00"
        )
        exit2, _ = self._make_signal(
            strike=Decimal("207.5"), event_type="FULL_EXIT", qualifier="ALL OUT", action="SOLD",
            received_at="2026-07-24T04:00:00.000000+00:00",
        )

        result = self.service.rebuild_all_lifecycles()

        self.assertEqual(result.lifecycles_created, 2)
        lifecycle1_id = get_trade_signal_by_id(self.connection, entry1.id).lifecycle_id
        lifecycle2_id = get_trade_signal_by_id(self.connection, entry2.id).lifecycle_id
        self.assertNotEqual(lifecycle1_id, lifecycle2_id)
        self.assertEqual(get_trade_signal_by_id(self.connection, exit1.id).lifecycle_id, lifecycle1_id)
        self.assertEqual(get_trade_signal_by_id(self.connection, exit2.id).lifecycle_id, lifecycle2_id)
        self.assertNotEqual(exit1, exit2)

    def test_equity_lifecycle_key(self):
        signal, _ = self._make_signal(symbol="AAPL", option_type=None, strike=None, expiration=None)

        result = self.service.rebuild_all_lifecycles()

        self.assertEqual(result.lifecycles_created, 1)
        lifecycle_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id
        lifecycle = get_trade_lifecycle_by_id(self.connection, lifecycle_id)
        self.assertIsNone(lifecycle.option_type)
        self.assertIsNone(lifecycle.strike)
        self.assertIsNone(lifecycle.expiration)


class IdempotencyTests(_LifecycleServiceTestCase):
    def test_second_identical_full_rebuild_creates_no_rows(self):
        self._make_signal(strike=Decimal("207.5"))
        self.service.rebuild_all_lifecycles()
        counts_after_first = self._counts()

        result2 = self.service.rebuild_all_lifecycles()

        self.assertEqual(self._counts(), counts_after_first)
        self.assertEqual(result2.keys_changed, 0)
        self.assertEqual(result2.keys_unchanged, 1)
        self.assertEqual(result2.lifecycles_created, 0)
        self.assertEqual(result2.lifecycles_superseded, 0)

    def test_second_identical_targeted_rebuild_creates_no_rows(self):
        _, raw = self._make_signal(strike=Decimal("207.5"))
        self.service.rebuild_lifecycles_for_raw_message_ids([raw.id])
        counts_after_first = self._counts()

        result2 = self.service.rebuild_lifecycles_for_raw_message_ids([raw.id])

        self.assertEqual(self._counts(), counts_after_first)
        self.assertEqual(result2.keys_changed, 0)

    def test_unchanged_lifecycle_ids_remain_unchanged(self):
        signal, _ = self._make_signal(strike=Decimal("207.5"))
        self.service.rebuild_all_lifecycles()
        lifecycle_id_before = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id

        self.service.rebuild_all_lifecycles()

        lifecycle_id_after = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id
        self.assertEqual(lifecycle_id_before, lifecycle_id_after)

    def test_lifecycle_event_counts_remain_unchanged(self):
        self._make_signal(strike=Decimal("207.5"))
        self.service.rebuild_all_lifecycles()
        count_before = self.connection.execute(
            "SELECT COUNT(*) FROM trade_lifecycle_events"
        ).fetchone()[0]

        self.service.rebuild_all_lifecycles()

        count_after = self.connection.execute(
            "SELECT COUNT(*) FROM trade_lifecycle_events"
        ).fetchone()[0]
        self.assertEqual(count_before, count_after)

    def test_superseded_history_count_remains_unchanged(self):
        self._make_signal(strike=Decimal("207.5"))
        self.service.rebuild_all_lifecycles()
        superseded_before = self.connection.execute(
            "SELECT COUNT(*) FROM trade_lifecycles WHERE is_current = 0"
        ).fetchone()[0]

        self.service.rebuild_all_lifecycles()

        superseded_after = self.connection.execute(
            "SELECT COUNT(*) FROM trade_lifecycles WHERE is_current = 0"
        ).fetchone()[0]
        self.assertEqual(superseded_before, 0)
        self.assertEqual(superseded_before, superseded_after)

    def test_two_element_ordering_key_persists_and_is_idempotent(self):
        # No received_at supplied - the real ordering_key falls back to
        # the canonical two-element (raw_message_id, trade_signal_id)
        # form (see database.repository._order_signal_rows()).
        entry, _ = self._make_signal(strike=Decimal("207.5"))
        self.service.rebuild_all_lifecycles()
        lifecycle_id = get_trade_signal_by_id(self.connection, entry.id).lifecycle_id
        event = get_trade_lifecycle_events(self.connection, lifecycle_id)[0]
        stored = json.loads(event.signal_snapshot)
        self.assertEqual(len(stored["ordering_key"]), 2)

        result2 = self.service.rebuild_all_lifecycles()

        self.assertEqual(result2.keys_changed, 0)
        self.assertEqual(result2.keys_unchanged, 1)

    def test_three_element_ordering_key_persists_and_is_idempotent(self):
        # A resolved received_at produces the canonical three-element
        # (received_at, raw_message_id, trade_signal_id) form.
        entry, _ = self._make_signal(
            strike=Decimal("207.5"), received_at="2026-07-24T01:00:00.000000+00:00"
        )
        self.service.rebuild_all_lifecycles()
        lifecycle_id = get_trade_signal_by_id(self.connection, entry.id).lifecycle_id
        event = get_trade_lifecycle_events(self.connection, lifecycle_id)[0]
        stored = json.loads(event.signal_snapshot)
        self.assertEqual(len(stored["ordering_key"]), 3)

        result2 = self.service.rebuild_all_lifecycles()

        self.assertEqual(result2.keys_changed, 0)
        self.assertEqual(result2.keys_unchanged, 1)


class ReplacementAndHistoryTests(_LifecycleServiceTestCase):
    def test_changed_sequence_supersedes_old_generations(self):
        entry, _ = self._make_signal(strike=Decimal("207.5"))
        self.service.rebuild_all_lifecycles()
        old_lifecycle_id = get_trade_signal_by_id(self.connection, entry.id).lifecycle_id

        self._make_signal(
            strike=Decimal("207.5"), event_type="PARTIAL_EXIT", qualifier="1/2", action="SOLD"
        )
        result = self.service.rebuild_all_lifecycles()

        self.assertEqual(result.keys_changed, 1)
        self.assertEqual(result.lifecycles_superseded, 1)
        self.assertFalse(get_trade_lifecycle_by_id(self.connection, old_lifecycle_id).is_current)

    def test_replacement_rows_become_current(self):
        entry, _ = self._make_signal(strike=Decimal("207.5"))
        self.service.rebuild_all_lifecycles()

        self._make_signal(
            strike=Decimal("207.5"), event_type="PARTIAL_EXIT", qualifier="1/2", action="SOLD"
        )
        self.service.rebuild_all_lifecycles()

        new_lifecycle_id = get_trade_signal_by_id(self.connection, entry.id).lifecycle_id
        new_lifecycle = get_trade_lifecycle_by_id(self.connection, new_lifecycle_id)
        self.assertTrue(new_lifecycle.is_current)
        self.assertEqual(new_lifecycle.status, "partially_closed")

    def test_old_lifecycle_rows_remain_queryable(self):
        entry, _ = self._make_signal(strike=Decimal("207.5"))
        self.service.rebuild_all_lifecycles()
        old_lifecycle_id = get_trade_signal_by_id(self.connection, entry.id).lifecycle_id

        self._make_signal(
            strike=Decimal("207.5"), event_type="PARTIAL_EXIT", qualifier="1/2", action="SOLD"
        )
        self.service.rebuild_all_lifecycles()

        self.assertIsNotNone(get_trade_lifecycle_by_id(self.connection, old_lifecycle_id))

    def test_old_lifecycle_event_snapshots_remain_unchanged(self):
        entry, _ = self._make_signal(strike=Decimal("207.5"))
        self.service.rebuild_all_lifecycles()
        old_lifecycle_id = get_trade_signal_by_id(self.connection, entry.id).lifecycle_id
        old_events_before = get_trade_lifecycle_events(self.connection, old_lifecycle_id)

        self._make_signal(
            strike=Decimal("207.5"), event_type="PARTIAL_EXIT", qualifier="1/2", action="SOLD"
        )
        self.service.rebuild_all_lifecycles()

        old_events_after = get_trade_lifecycle_events(self.connection, old_lifecycle_id)
        self.assertEqual(
            [(e.trade_signal_id, e.signal_snapshot) for e in old_events_before],
            [(e.trade_signal_id, e.signal_snapshot) for e in old_events_after],
        )

    def test_old_pointers_cleared_before_replacement(self):
        entry, _ = self._make_signal(strike=Decimal("207.5"))
        self.service.rebuild_all_lifecycles()
        old_lifecycle_id = get_trade_signal_by_id(self.connection, entry.id).lifecycle_id

        self._make_signal(
            strike=Decimal("207.5"), event_type="PARTIAL_EXIT", qualifier="1/2", action="SOLD"
        )
        self.service.rebuild_all_lifecycles()

        new_lifecycle_id = get_trade_signal_by_id(self.connection, entry.id).lifecycle_id
        self.assertNotEqual(old_lifecycle_id, new_lifecycle_id)

    def test_new_pointers_target_only_current_generations(self):
        entry, _ = self._make_signal(strike=Decimal("207.5"))
        self.service.rebuild_all_lifecycles()

        self._make_signal(
            strike=Decimal("207.5"), event_type="PARTIAL_EXIT", qualifier="1/2", action="SOLD"
        )
        self.service.rebuild_all_lifecycles()

        new_lifecycle_id = get_trade_signal_by_id(self.connection, entry.id).lifecycle_id
        self.assertTrue(get_trade_lifecycle_by_id(self.connection, new_lifecycle_id).is_current)


class EmptyGenerationTests(_LifecycleServiceTestCase):
    def test_all_signals_disappear_supersedes_with_no_replacement(self):
        entry, _, extraction = self._make_signal_with_extraction(strike=Decimal("207.5"))

        result1 = self.service.rebuild_all_lifecycles()
        self.assertEqual(result1.lifecycles_created, 1)
        old_lifecycle_id = get_trade_signal_by_id(self.connection, entry.id).lifecycle_id

        supersede_extraction(self.connection, extraction.id)
        self.connection.commit()

        result2 = self.service.rebuild_all_lifecycles()

        self.assertEqual(result2.keys_changed, 1)
        self.assertEqual(result2.lifecycles_created, 0)
        self.assertEqual(result2.lifecycles_superseded, 1)
        old_lifecycle = get_trade_lifecycle_by_id(self.connection, old_lifecycle_id)
        self.assertFalse(old_lifecycle.is_current)
        events = get_trade_lifecycle_events(self.connection, old_lifecycle_id)
        self.assertEqual(len(events), 1)
        self.assertIsNone(get_trade_signal_by_id(self.connection, entry.id).lifecycle_id)


class KeyChangingLineageTests(_LifecycleServiceTestCase):
    def test_old_and_new_key_both_rebuilt_exactly_once(self):
        signal_a, raw_a, extraction_a = self._make_signal_with_extraction(strike=Decimal("207.5"))
        signal_b, _ = self._make_signal(
            strike=Decimal("207.5"), event_type="FULL_EXIT", qualifier="ALL OUT", action="SOLD"
        )

        self.service.rebuild_all_lifecycles()
        old_ibm_lifecycle_id = get_trade_signal_by_id(self.connection, signal_a.id).lifecycle_id
        self.assertEqual(
            old_ibm_lifecycle_id, get_trade_signal_by_id(self.connection, signal_b.id).lifecycle_id
        )

        # Simulate a key-changing reprocessing event for raw_a: supersede
        # its extraction, create a new current extraction + a new current
        # trade_signal for the SAME raw_message representing AVGO 380 put.
        supersede_extraction(self.connection, extraction_a.id)
        self.connection.commit()
        extraction_a2 = create_message_extraction(
            self.connection, raw_a.id, parser_version="v2", parse_status="parsed"
        )
        self.connection.commit()
        signal_a2 = create_trade_signal(
            self.connection, raw_a.id, self.trader.id, "AVGO", "BOUGHT",
            option_type="put", strike=Decimal("380"), expiration="2026-07-24",
            event_type="ENTRY", extraction_id=extraction_a2.id,
        )
        self.connection.commit()

        result = self.service.rebuild_lifecycles_for_raw_message_ids([raw_a.id])

        self.assertEqual(result.keys_changed, 2)

        old_ibm = get_trade_lifecycle_by_id(self.connection, old_ibm_lifecycle_id)
        self.assertFalse(old_ibm.is_current)
        self.assertEqual(len(get_trade_lifecycle_events(self.connection, old_ibm_lifecycle_id)), 2)

        new_ibm_lifecycle_id = get_trade_signal_by_id(self.connection, signal_b.id).lifecycle_id
        self.assertNotEqual(new_ibm_lifecycle_id, old_ibm_lifecycle_id)
        new_ibm = get_trade_lifecycle_by_id(self.connection, new_ibm_lifecycle_id)
        self.assertEqual(new_ibm.status, "orphan")

        avgo_lifecycle_id = get_trade_signal_by_id(self.connection, signal_a2.id).lifecycle_id
        self.assertIsNotNone(avgo_lifecycle_id)
        avgo_lifecycle = get_trade_lifecycle_by_id(self.connection, avgo_lifecycle_id)
        self.assertEqual(avgo_lifecycle.symbol, "AVGO")
        self.assertEqual(avgo_lifecycle.status, "open")

        self.assertIsNone(get_trade_signal_by_id(self.connection, signal_a.id).lifecycle_id)


class IncompleteIdentityTests(_LifecycleServiceTestCase):
    def test_each_incomplete_signal_becomes_unresolved_singleton(self):
        signal, _ = self._make_signal(option_type="call", strike=None, expiration=None)

        result = self.service.rebuild_all_lifecycles()

        self.assertEqual(result.lifecycles_created, 1)
        lifecycle_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id
        lifecycle = get_trade_lifecycle_by_id(self.connection, lifecycle_id)
        self.assertEqual(lifecycle.status, "unresolved")
        self.assertEqual(lifecycle.ambiguity_flags, [FLAG_INCOMPLETE_CONTRACT_IDENTITY])

    def test_no_missing_component_guessed(self):
        signal, _ = self._make_signal(option_type="call", strike=None, expiration=None)
        self.service.rebuild_all_lifecycles()

        lifecycle_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id
        lifecycle = get_trade_lifecycle_by_id(self.connection, lifecycle_id)
        self.assertEqual(lifecycle.option_type, "call")
        self.assertIsNone(lifecycle.strike)
        self.assertIsNone(lifecycle.expiration)

    def test_repeated_identical_rebuild_is_idempotent(self):
        self._make_signal(option_type="call", strike=None, expiration=None)
        self.service.rebuild_all_lifecycles()
        counts_before = self._counts()

        result2 = self.service.rebuild_all_lifecycles()

        self.assertEqual(self._counts(), counts_before)
        self.assertEqual(result2.keys_changed, 0)

    def test_removal_of_incomplete_signal_supersedes_singleton_with_no_replacement(self):
        signal, _, extraction = self._make_signal_with_extraction(
            option_type="call", strike=None, expiration=None
        )
        self.service.rebuild_all_lifecycles()
        singleton_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id
        self.assertIsNotNone(singleton_id)

        supersede_extraction(self.connection, extraction.id)
        self.connection.commit()

        result = self.service.rebuild_all_lifecycles()

        self.assertEqual(result.lifecycles_created, 0)
        self.assertEqual(result.lifecycles_superseded, 1)
        self.assertFalse(get_trade_lifecycle_by_id(self.connection, singleton_id).is_current)


class SnapshotSafetyTests(_LifecycleServiceTestCase):
    def _corrupt_snapshot(self, event_id, raw_text):
        self.connection.execute(
            "UPDATE trade_lifecycle_events SET signal_snapshot = ? WHERE id = ?",
            (raw_text, event_id),
        )
        self.connection.commit()

    def test_malformed_json_raises_and_rolls_back(self):
        entry, _ = self._make_signal(strike=Decimal("207.5"))
        self.service.rebuild_all_lifecycles()
        lifecycle_id = get_trade_signal_by_id(self.connection, entry.id).lifecycle_id
        event = get_trade_lifecycle_events(self.connection, lifecycle_id)[0]
        self._corrupt_snapshot(event.id, "not json at all")
        counts_before = self._counts()

        with self.assertRaises(LifecycleSnapshotError):
            self.service.rebuild_all_lifecycles()

        self.assertEqual(self._counts(), counts_before)
        self.assertTrue(get_trade_lifecycle_by_id(self.connection, lifecycle_id).is_current)

    def test_decoded_non_object_raises_and_rolls_back(self):
        entry, _ = self._make_signal(strike=Decimal("207.5"))
        self.service.rebuild_all_lifecycles()
        lifecycle_id = get_trade_signal_by_id(self.connection, entry.id).lifecycle_id
        event = get_trade_lifecycle_events(self.connection, lifecycle_id)[0]
        self._corrupt_snapshot(event.id, json.dumps(42))
        counts_before = self._counts()

        with self.assertRaises(LifecycleSnapshotError):
            self.service.rebuild_all_lifecycles()

        self.assertEqual(self._counts(), counts_before)

    def test_mismatched_trade_signal_id_raises_and_rolls_back(self):
        entry, raw = self._make_signal(strike=Decimal("207.5"))
        self.service.rebuild_all_lifecycles()
        lifecycle_id = get_trade_signal_by_id(self.connection, entry.id).lifecycle_id
        event = get_trade_lifecycle_events(self.connection, lifecycle_id)[0]
        snap = self._valid_snapshot_dict(event.trade_signal_id, raw.id)
        snap["trade_signal_id"] = event.trade_signal_id + 999999
        self._corrupt_snapshot(event.id, json.dumps(snap))
        counts_before = self._counts()

        with self.assertRaises(LifecycleSnapshotError):
            self.service.rebuild_all_lifecycles()

        self.assertEqual(self._counts(), counts_before)

    def test_boolean_trade_signal_id_rejected_even_when_equal_to_one(self):
        # Regression: bool is a Python int subclass, so True == 1. Without
        # an explicit isinstance/bool check before the equality compare,
        # a decoded trade_signal_id of JSON `true` would silently pass
        # whenever event.trade_signal_id == 1 - this is the first signal
        # inserted into a fresh temp database, so its id is guaranteed 1.
        entry, raw = self._make_signal(strike=Decimal("207.5"))
        self.assertEqual(entry.id, 1)
        self.service.rebuild_all_lifecycles()
        lifecycle_id = get_trade_signal_by_id(self.connection, entry.id).lifecycle_id
        event = get_trade_lifecycle_events(self.connection, lifecycle_id)[0]
        snap = self._valid_snapshot_dict(entry.id, raw.id)
        snap["trade_signal_id"] = True
        snap["ordering_key"] = [raw.id, 1]
        self._corrupt_snapshot(event.id, json.dumps(snap))
        counts_before = self._counts()

        with self.assertRaises(LifecycleSnapshotError):
            self.service.rebuild_all_lifecycles()

        self.assertEqual(self._counts(), counts_before)

    def test_non_integer_trade_signal_id_raises_and_rolls_back(self):
        entry, raw = self._make_signal(strike=Decimal("207.5"))
        self.service.rebuild_all_lifecycles()
        lifecycle_id = get_trade_signal_by_id(self.connection, entry.id).lifecycle_id
        event = get_trade_lifecycle_events(self.connection, lifecycle_id)[0]
        snap = self._valid_snapshot_dict(event.trade_signal_id, raw.id)
        snap["trade_signal_id"] = str(event.trade_signal_id)
        self._corrupt_snapshot(event.id, json.dumps(snap))
        counts_before = self._counts()

        with self.assertRaises(LifecycleSnapshotError):
            self.service.rebuild_all_lifecycles()

        self.assertEqual(self._counts(), counts_before)

    def test_missing_raw_message_id_raises_and_rolls_back(self):
        entry, raw = self._make_signal(strike=Decimal("207.5"))
        self.service.rebuild_all_lifecycles()
        lifecycle_id = get_trade_signal_by_id(self.connection, entry.id).lifecycle_id
        event = get_trade_lifecycle_events(self.connection, lifecycle_id)[0]
        snap = self._valid_snapshot_dict(event.trade_signal_id, raw.id)
        del snap["raw_message_id"]
        self._corrupt_snapshot(event.id, json.dumps(snap))
        counts_before = self._counts()

        with self.assertRaises(LifecycleSnapshotError):
            self.service.rebuild_all_lifecycles()

        self.assertEqual(self._counts(), counts_before)

    def test_invalid_ordering_key_raises_and_rolls_back(self):
        entry, raw = self._make_signal(strike=Decimal("207.5"))
        self.service.rebuild_all_lifecycles()
        lifecycle_id = get_trade_signal_by_id(self.connection, entry.id).lifecycle_id
        event = get_trade_lifecycle_events(self.connection, lifecycle_id)[0]
        snap = self._valid_snapshot_dict(event.trade_signal_id, raw.id)
        snap["ordering_key"] = "not-a-list"
        self._corrupt_snapshot(event.id, json.dumps(snap))
        counts_before = self._counts()

        with self.assertRaises(LifecycleSnapshotError):
            self.service.rebuild_all_lifecycles()

        self.assertEqual(self._counts(), counts_before)

    def _assert_ordering_key_rejected(self, entry, raw, ordering_key):
        self.service.rebuild_all_lifecycles()
        lifecycle_id = get_trade_signal_by_id(self.connection, entry.id).lifecycle_id
        event = get_trade_lifecycle_events(self.connection, lifecycle_id)[0]
        snap = self._valid_snapshot_dict(event.trade_signal_id, raw.id)
        snap["ordering_key"] = ordering_key
        self._corrupt_snapshot(event.id, json.dumps(snap))
        counts_before = self._counts()

        with self.assertRaises(LifecycleSnapshotError):
            self.service.rebuild_all_lifecycles()

        self.assertEqual(self._counts(), counts_before)
        self.assertFalse(self.connection.in_transaction)

    def test_ordering_key_empty_array_raises_and_rolls_back(self):
        entry, raw = self._make_signal(strike=Decimal("207.5"))
        self._assert_ordering_key_rejected(entry, raw, [])

    def test_ordering_key_wrong_length_raises_and_rolls_back(self):
        # entry.id is this generation's own trade_signal_id (the sole
        # member of a fresh single-ENTRY lifecycle) - no need to rebuild
        # first just to read it back off the persisted event.
        entry, raw = self._make_signal(strike=Decimal("207.5"))
        self._assert_ordering_key_rejected(
            entry, raw, ["2026-07-24T00:00:00+00:00", raw.id, entry.id, 999],
        )

    def test_ordering_key_two_element_boolean_raw_message_id_raises_and_rolls_back(self):
        entry, raw = self._make_signal(strike=Decimal("207.5"))
        self._assert_ordering_key_rejected(entry, raw, [True, entry.id])

    def test_ordering_key_three_element_boolean_trade_signal_id_raises_and_rolls_back(self):
        entry, raw = self._make_signal(strike=Decimal("207.5"))
        self._assert_ordering_key_rejected(
            entry, raw, ["2026-07-24T00:00:00+00:00", raw.id, True],
        )

    def test_ordering_key_blank_received_at_raises_and_rolls_back(self):
        entry, raw = self._make_signal(strike=Decimal("207.5"))
        self._assert_ordering_key_rejected(entry, raw, ["   ", raw.id, entry.id])

    def test_ordering_key_non_integer_element_raises_and_rolls_back(self):
        entry, raw = self._make_signal(strike=Decimal("207.5"))
        self._assert_ordering_key_rejected(entry, raw, [str(raw.id), entry.id])

    def test_ordering_key_mismatched_raw_message_id_raises_and_rolls_back(self):
        entry, raw = self._make_signal(strike=Decimal("207.5"))
        self._assert_ordering_key_rejected(entry, raw, [raw.id + 999999, entry.id])

    def test_ordering_key_mismatched_trade_signal_id_raises_and_rolls_back(self):
        entry, raw = self._make_signal(strike=Decimal("207.5"))
        self._assert_ordering_key_rejected(entry, raw, [raw.id, entry.id + 999999])


class IncompleteSingletonComparisonTests(_LifecycleServiceTestCase):
    """Recovery Milestone R6.4 quality-gate correction: the incomplete-
    identity-singleton idempotency check must be provably snapshot-safe,
    never trusting the live trade_signals row to stand in for recorded
    evidence, and a correction that changes symbol/option_type/strike/
    expiration while remaining incomplete must always supersede and
    replace - never be reported as unchanged."""

    def _incomplete_snapshot_dict(self, trade_signal_id, raw_message_id, symbol="IBM"):
        return {
            "trade_signal_id": trade_signal_id,
            "raw_message_id": raw_message_id,
            "trader_id": self.trader.id,
            "symbol": symbol,
            "option_type": "call",
            "strike": None,
            "expiration": None,
            "event_type": "ENTRY",
            "qualifier": None,
            "action": "BOUGHT",
            "price": None,
            "stated_entry_price": None,
            "stated_return_pct": None,
            "notes": None,
            "extraction_id": None,
            "ordering_key": [raw_message_id, trade_signal_id],
        }

    def _corrupt_snapshot(self, event_id, raw_text):
        self.connection.execute(
            "UPDATE trade_lifecycle_events SET signal_snapshot = ? WHERE id = ?",
            (raw_text, event_id),
        )
        self.connection.commit()

    def test_malformed_incomplete_singleton_snapshot_raises_and_rolls_back(self):
        signal, _ = self._make_signal(option_type="call", strike=None, expiration=None)
        self.service.rebuild_all_lifecycles()
        singleton_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id
        event = get_trade_lifecycle_events(self.connection, singleton_id)[0]
        self._corrupt_snapshot(event.id, "not json at all")
        counts_before = self._counts()

        with self.assertRaises(LifecycleSnapshotError):
            self.service.rebuild_all_lifecycles()

        self.assertEqual(self._counts(), counts_before)
        self.assertFalse(self.connection.in_transaction)
        self.assertTrue(get_trade_lifecycle_by_id(self.connection, singleton_id).is_current)

    def test_incomplete_singleton_non_object_snapshot_raises_and_rolls_back(self):
        signal, _ = self._make_signal(option_type="call", strike=None, expiration=None)
        self.service.rebuild_all_lifecycles()
        singleton_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id
        event = get_trade_lifecycle_events(self.connection, singleton_id)[0]
        self._corrupt_snapshot(event.id, json.dumps([1, 2, 3]))
        counts_before = self._counts()

        with self.assertRaises(LifecycleSnapshotError):
            self.service.rebuild_all_lifecycles()

        self.assertEqual(self._counts(), counts_before)

    def test_incomplete_singleton_invalid_ordering_key_raises_and_rolls_back(self):
        signal, raw = self._make_signal(option_type="call", strike=None, expiration=None)
        self.service.rebuild_all_lifecycles()
        singleton_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id
        event = get_trade_lifecycle_events(self.connection, singleton_id)[0]
        snap = self._incomplete_snapshot_dict(event.trade_signal_id, raw.id)
        snap["ordering_key"] = "not-a-list"
        self._corrupt_snapshot(event.id, json.dumps(snap))
        counts_before = self._counts()

        with self.assertRaises(LifecycleSnapshotError):
            self.service.rebuild_all_lifecycles()

        self.assertEqual(self._counts(), counts_before)

    def test_incomplete_singleton_mismatched_trade_signal_id_raises_and_rolls_back(self):
        signal, raw = self._make_signal(option_type="call", strike=None, expiration=None)
        self.service.rebuild_all_lifecycles()
        singleton_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id
        event = get_trade_lifecycle_events(self.connection, singleton_id)[0]
        snap = self._incomplete_snapshot_dict(event.trade_signal_id, raw.id)
        snap["trade_signal_id"] = event.trade_signal_id + 999999
        self._corrupt_snapshot(event.id, json.dumps(snap))
        counts_before = self._counts()

        with self.assertRaises(LifecycleSnapshotError):
            self.service.rebuild_all_lifecycles()

        self.assertEqual(self._counts(), counts_before)

    def test_true_unchanged_incomplete_singleton_is_idempotent_with_zero_writes(self):
        signal, _ = self._make_signal(option_type="call", strike=None, expiration=None)
        self.service.rebuild_all_lifecycles()
        singleton_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id
        counts_before = self._counts()

        result2 = self.service.rebuild_all_lifecycles()

        self.assertEqual(self._counts(), counts_before)
        self.assertEqual(result2.keys_changed, 0)
        self.assertEqual(result2.keys_unchanged, 1)
        self.assertEqual(result2.lifecycles_created, 0)
        self.assertEqual(result2.lifecycles_superseded, 0)
        self.assertEqual(result2.signal_pointers_assigned, 0)
        self.assertEqual(result2.signal_pointers_cleared, 0)
        self.assertEqual(
            get_trade_signal_by_id(self.connection, signal.id).lifecycle_id, singleton_id
        )

    def test_incomplete_ibm_to_incomplete_avgo_correction_supersedes(self):
        signal, raw, extraction = self._make_signal_with_extraction(
            symbol="IBM", option_type="call", strike=None, expiration=None
        )
        self.service.rebuild_all_lifecycles()
        old_singleton_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id
        old_singleton = get_trade_lifecycle_by_id(self.connection, old_singleton_id)
        self.assertEqual(old_singleton.symbol, "IBM")

        supersede_extraction(self.connection, extraction.id)
        self.connection.commit()
        extraction2 = create_message_extraction(
            self.connection, raw.id, parser_version="v2", parse_status="parsed"
        )
        self.connection.commit()
        signal2 = create_trade_signal(
            self.connection, raw.id, self.trader.id, "AVGO", "BOUGHT",
            option_type="call", strike=None, expiration=None,
            event_type="ENTRY", extraction_id=extraction2.id,
        )
        self.connection.commit()

        result = self.service.rebuild_all_lifecycles()

        self.assertEqual(result.keys_changed, 1)
        self.assertFalse(get_trade_lifecycle_by_id(self.connection, old_singleton_id).is_current)
        new_singleton_id = get_trade_signal_by_id(self.connection, signal2.id).lifecycle_id
        self.assertIsNotNone(new_singleton_id)
        self.assertNotEqual(new_singleton_id, old_singleton_id)
        new_singleton = get_trade_lifecycle_by_id(self.connection, new_singleton_id)
        self.assertEqual(new_singleton.symbol, "AVGO")
        self.assertEqual(new_singleton.status, "unresolved")
        self.assertIsNone(get_trade_signal_by_id(self.connection, signal.id).lifecycle_id)

    def test_populated_option_type_change_while_incomplete_supersedes(self):
        signal, raw, extraction = self._make_signal_with_extraction(
            symbol="IBM", option_type="call", strike=None, expiration=None
        )
        self.service.rebuild_all_lifecycles()
        old_singleton_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id

        supersede_extraction(self.connection, extraction.id)
        self.connection.commit()
        extraction2 = create_message_extraction(
            self.connection, raw.id, parser_version="v2", parse_status="parsed"
        )
        self.connection.commit()
        signal2 = create_trade_signal(
            self.connection, raw.id, self.trader.id, "IBM", "BOUGHT",
            option_type="put", strike=None, expiration=None,
            event_type="ENTRY", extraction_id=extraction2.id,
        )
        self.connection.commit()

        result = self.service.rebuild_all_lifecycles()

        self.assertEqual(result.keys_changed, 1)
        self.assertFalse(get_trade_lifecycle_by_id(self.connection, old_singleton_id).is_current)
        new_singleton_id = get_trade_signal_by_id(self.connection, signal2.id).lifecycle_id
        new_singleton = get_trade_lifecycle_by_id(self.connection, new_singleton_id)
        self.assertEqual(new_singleton.option_type, "put")

    def test_populated_strike_change_while_incomplete_supersedes(self):
        signal, raw, extraction = self._make_signal_with_extraction(
            symbol="IBM", option_type=None, strike=Decimal("207.5"), expiration=None
        )
        self.service.rebuild_all_lifecycles()
        old_singleton_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id

        supersede_extraction(self.connection, extraction.id)
        self.connection.commit()
        extraction2 = create_message_extraction(
            self.connection, raw.id, parser_version="v2", parse_status="parsed"
        )
        self.connection.commit()
        signal2 = create_trade_signal(
            self.connection, raw.id, self.trader.id, "IBM", "BOUGHT",
            option_type=None, strike=Decimal("210"), expiration=None,
            event_type="ENTRY", extraction_id=extraction2.id,
        )
        self.connection.commit()

        result = self.service.rebuild_all_lifecycles()

        self.assertEqual(result.keys_changed, 1)
        self.assertFalse(get_trade_lifecycle_by_id(self.connection, old_singleton_id).is_current)
        new_singleton_id = get_trade_signal_by_id(self.connection, signal2.id).lifecycle_id
        new_singleton = get_trade_lifecycle_by_id(self.connection, new_singleton_id)
        self.assertEqual(new_singleton.strike, "210")

    def test_populated_expiration_change_while_incomplete_supersedes(self):
        signal, raw, extraction = self._make_signal_with_extraction(
            symbol="IBM", option_type=None, strike=None, expiration="2026-07-24"
        )
        self.service.rebuild_all_lifecycles()
        old_singleton_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id

        supersede_extraction(self.connection, extraction.id)
        self.connection.commit()
        extraction2 = create_message_extraction(
            self.connection, raw.id, parser_version="v2", parse_status="parsed"
        )
        self.connection.commit()
        signal2 = create_trade_signal(
            self.connection, raw.id, self.trader.id, "IBM", "BOUGHT",
            option_type=None, strike=None, expiration="2026-08-21",
            event_type="ENTRY", extraction_id=extraction2.id,
        )
        self.connection.commit()

        result = self.service.rebuild_all_lifecycles()

        self.assertEqual(result.keys_changed, 1)
        self.assertFalse(get_trade_lifecycle_by_id(self.connection, old_singleton_id).is_current)
        new_singleton_id = get_trade_signal_by_id(self.connection, signal2.id).lifecycle_id
        new_singleton = get_trade_lifecycle_by_id(self.connection, new_singleton_id)
        self.assertEqual(new_singleton.expiration, "2026-08-21")

    def test_replacement_counters_and_pointer_cleanup(self):
        signal, raw, extraction = self._make_signal_with_extraction(
            symbol="IBM", option_type="call", strike=None, expiration=None
        )
        self.service.rebuild_all_lifecycles()
        old_singleton_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id

        supersede_extraction(self.connection, extraction.id)
        self.connection.commit()
        extraction2 = create_message_extraction(
            self.connection, raw.id, parser_version="v2", parse_status="parsed"
        )
        self.connection.commit()
        signal2 = create_trade_signal(
            self.connection, raw.id, self.trader.id, "AVGO", "BOUGHT",
            option_type="call", strike=None, expiration=None,
            event_type="ENTRY", extraction_id=extraction2.id,
        )
        self.connection.commit()

        result = self.service.rebuild_all_lifecycles()

        self.assertEqual(result.lifecycles_superseded, 1)
        self.assertEqual(result.lifecycles_created, 1)
        self.assertEqual(result.signal_pointers_cleared, 1)
        self.assertEqual(result.signal_pointers_assigned, 1)
        self.assertIsNone(get_trade_signal_by_id(self.connection, signal.id).lifecycle_id)
        new_singleton_id = get_trade_signal_by_id(self.connection, signal2.id).lifecycle_id
        self.assertIsNotNone(new_singleton_id)
        self.assertNotEqual(new_singleton_id, old_singleton_id)


class IntegrityRollbackTests(_LifecycleServiceTestCase):
    def test_violation_after_writes_raises_and_rolls_back_everything(self):
        # Create and commit an existing current lifecycle generation.
        entry, _ = self._make_signal(strike=Decimal("207.5"))
        self.service.rebuild_all_lifecycles()
        old_lifecycle_id = get_trade_signal_by_id(self.connection, entry.id).lifecycle_id
        self.assertIsNotNone(old_lifecycle_id)
        self.assertTrue(get_trade_lifecycle_by_id(self.connection, old_lifecycle_id).is_current)

        # Change the current signal sequence so the next rebuild would (if
        # it succeeded) clear the old pointer, supersede the existing
        # generation, and create a replacement generation + event + pointer.
        self._make_signal(
            strike=Decimal("207.5"), event_type="PARTIAL_EXIT", qualifier="1/2", action="SOLD"
        )

        pre_lifecycles, pre_events, pre_pointers = self._capture_full_state()

        violations = [
            "Invariant A violated: fake violation A for testing.",
            "Invariant H violated: fake violation H for testing.",
        ]
        with patch(
            "database.service.validate_lifecycle_membership_integrity",
            return_value=violations,
        ):
            with self.assertRaises(LifecycleIntegrityError) as ctx:
                self.service.rebuild_all_lifecycles()

        # The entire ordered violations list is preserved, not just the
        # first entry.
        self.assertEqual(ctx.exception.violations, violations)
        self.assertIn("Invariant A violated", str(ctx.exception))
        self.assertIn("Invariant H violated", str(ctx.exception))

        post_lifecycles, post_events, post_pointers = self._capture_full_state()
        self.assertEqual(post_lifecycles, pre_lifecycles)
        self.assertEqual(post_events, pre_events)
        self.assertEqual(post_pointers, pre_pointers)

        # The old generation remains current; no replacement lifecycle or
        # event exists (already implied by post_lifecycles/post_events
        # equaling their pre-capture, but checked explicitly here too);
        # the old pointer is restored; and no new pointer remains.
        old_lifecycle = get_trade_lifecycle_by_id(self.connection, old_lifecycle_id)
        self.assertTrue(old_lifecycle.is_current)
        self.assertEqual(
            get_trade_signal_by_id(self.connection, entry.id).lifecycle_id, old_lifecycle_id
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM trade_lifecycles WHERE id != ?", (old_lifecycle_id,)
            ).fetchone()[0],
            0,
        )
        self.assertFalse(self.connection.in_transaction)


class _CountingConnection(sqlite3.Connection):
    """Test-only sqlite3.Connection subclass counting commit() calls -
    a live connection's commit() is a read-only C-level attribute and
    cannot be patched directly (see tests.test_service._FailingConnection
    for the same technique applied to BEGIN/ROLLBACK failures)."""

    commit_count = 0

    def commit(self):
        self.commit_count += 1
        return super().commit()


class TransactionDisciplineTests(_LifecycleServiceTestCase):
    def test_exactly_one_commit_on_success(self):
        connection = sqlite3.connect(self.db_path, factory=_CountingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        service = TradeService(connection)
        source = get_or_create_source(connection, "discord")
        connection.commit()
        trader = create_trader(connection, source.id, "TC2")
        connection.commit()
        raw_message = create_raw_message(connection, source.id, "x")
        connection.commit()
        create_trade_signal(
            connection, raw_message.id, trader.id, "IBM", "BOUGHT",
            option_type="call", strike=Decimal("207.5"), expiration="2026-07-24",
            event_type="ENTRY",
        )
        connection.commit()
        connection.commit_count = 0

        service.rebuild_all_lifecycles()

        self.assertEqual(connection.commit_count, 1)
        connection.close()

    def test_rollback_on_pure_engine_exception(self):
        self._make_signal(strike=Decimal("207.5"))

        with patch("database.service.build_lifecycle_sequence", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.service.rebuild_all_lifecycles()

        self.assertEqual(self._counts(), {"trade_lifecycles": 0, "trade_lifecycle_events": 0})
        self.assertFalse(self.connection.in_transaction)

    def test_rollback_on_repository_exception(self):
        self._make_signal(strike=Decimal("207.5"))

        with patch(
            "database.service.persist_lifecycle_builds",
            side_effect=sqlite3.OperationalError("boom"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.service.rebuild_all_lifecycles()

        self.assertEqual(self._counts(), {"trade_lifecycles": 0, "trade_lifecycle_events": 0})

    def test_rollback_on_snapshot_error(self):
        entry, _ = self._make_signal(strike=Decimal("207.5"))
        self.service.rebuild_all_lifecycles()
        lifecycle_id = get_trade_signal_by_id(self.connection, entry.id).lifecycle_id
        event = get_trade_lifecycle_events(self.connection, lifecycle_id)[0]
        self.connection.execute(
            "UPDATE trade_lifecycle_events SET signal_snapshot = ? WHERE id = ?",
            ("bad json", event.id),
        )
        self.connection.commit()
        counts_before = self._counts()

        with self.assertRaises(LifecycleSnapshotError):
            self.service.rebuild_all_lifecycles()

        self.assertEqual(self._counts(), counts_before)
        self.assertFalse(self.connection.in_transaction)

    def test_rollback_on_integrity_error(self):
        self._make_signal(strike=Decimal("207.5"))

        with patch(
            "database.service.validate_lifecycle_membership_integrity",
            return_value=["fake"],
        ):
            with self.assertRaises(LifecycleIntegrityError):
                self.service.rebuild_all_lifecycles()

        self.assertFalse(self.connection.in_transaction)


class PartialWriteRollbackTests(_LifecycleServiceTestCase):
    """Recovery Milestone R6.4 final quality-gate correction: proves
    rollback restores the *entire* pre-call state even when real writes
    already occurred earlier in the same rebuild call, for all three
    exception categories. Unlike TransactionDisciplineTests' equivalent
    tests (which fail immediately, before any real write happens for
    their single key), every test here forces the failure to happen only
    after at least one earlier key/generation has already been written
    for real within the same transaction."""

    def test_pure_engine_exception_after_earlier_key_writes_rolls_back_everything(self):
        # Two deterministically ordered (_key_sort_key sorts by symbol)
        # equity keys: AAPL sorts before MSFT, so AAPL is rebuilt (real
        # write) before build_lifecycle_sequence() is ever called for MSFT.
        aapl_signal, _ = self._make_signal(
            symbol="AAPL", option_type=None, strike=None, expiration=None
        )
        msft_signal, _ = self._make_signal(
            symbol="MSFT", option_type=None, strike=None, expiration=None
        )

        def fake_build(snapshots):
            if snapshots and snapshots[0].symbol == "MSFT":
                raise RuntimeError("boom - pure engine failure on the later key")
            return real_build_lifecycle_sequence(snapshots)

        with patch("database.service.build_lifecycle_sequence", side_effect=fake_build):
            with self.assertRaises(RuntimeError):
                self.service.rebuild_all_lifecycles()

        self.assertEqual(self._counts(), {"trade_lifecycles": 0, "trade_lifecycle_events": 0})
        self.assertIsNone(get_trade_signal_by_id(self.connection, aapl_signal.id).lifecycle_id)
        self.assertIsNone(get_trade_signal_by_id(self.connection, msft_signal.id).lifecycle_id)
        self.assertFalse(self.connection.in_transaction)

    def test_repository_exception_after_real_persistence_rolls_back_everything(self):
        # Build a real existing generation first, so this call's real
        # persist_lifecycle_builds() invocation also has to reverse a real
        # supersession, not just a first-ever insert.
        entry, _ = self._make_signal(strike=Decimal("207.5"))
        self.service.rebuild_all_lifecycles()
        old_lifecycle_id = get_trade_signal_by_id(self.connection, entry.id).lifecycle_id

        self._make_signal(
            strike=Decimal("207.5"), event_type="PARTIAL_EXIT", qualifier="1/2", action="SOLD"
        )
        pre_state = self._capture_full_state()

        def fake_persist(*args, **kwargs):
            # Allow the real repository function to perform its real
            # writes (supersession already happened by the time this is
            # called; this inserts the replacement lifecycle/events and
            # assigns replacement pointers) - only then raise, so the
            # exception is never used to bypass real persistence.
            result = real_persist_lifecycle_builds(*args, **kwargs)
            raise sqlite3.OperationalError("boom - after real persistence")

        with patch("database.service.persist_lifecycle_builds", side_effect=fake_persist):
            with self.assertRaises(sqlite3.OperationalError):
                self.service.rebuild_all_lifecycles()

        self.assertEqual(self._capture_full_state(), pre_state)
        self.assertTrue(get_trade_lifecycle_by_id(self.connection, old_lifecycle_id).is_current)
        self.assertEqual(
            get_trade_signal_by_id(self.connection, entry.id).lifecycle_id, old_lifecycle_id
        )
        self.assertFalse(self.connection.in_transaction)

    def test_snapshot_exception_after_earlier_key_writes_rolls_back_everything(self):
        # MSFT gets a real prior generation, which is then corrupted.
        msft_entry, _ = self._make_signal(
            symbol="MSFT", option_type=None, strike=None, expiration=None
        )
        self.service.rebuild_all_lifecycles()
        msft_lifecycle_id = get_trade_signal_by_id(self.connection, msft_entry.id).lifecycle_id
        msft_event = get_trade_lifecycle_events(self.connection, msft_lifecycle_id)[0]
        self.connection.execute(
            "UPDATE trade_lifecycle_events SET signal_snapshot = ? WHERE id = ?",
            ("not json at all", msft_event.id),
        )
        self.connection.commit()

        # AAPL is a brand-new key with no prior generation - it sorts
        # before MSFT, so this rebuild call writes it for real before it
        # ever reaches MSFT's corrupted snapshot.
        aapl_signal, _ = self._make_signal(
            symbol="AAPL", option_type=None, strike=None, expiration=None
        )

        with self.assertRaises(LifecycleSnapshotError):
            self.service.rebuild_all_lifecycles()

        self.assertIsNone(get_trade_signal_by_id(self.connection, aapl_signal.id).lifecycle_id)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM trade_lifecycles WHERE symbol = 'AAPL'"
            ).fetchone()[0],
            0,
        )
        self.assertTrue(get_trade_lifecycle_by_id(self.connection, msft_lifecycle_id).is_current)
        self.assertFalse(self.connection.in_transaction)


class TargetedIncompleteIdentityTransitionTests(_LifecycleServiceTestCase):
    """Recovery Milestone R6.4 final quality-gate correction: targeted
    (rebuild_lifecycles_for_raw_message_ids()) coverage of every
    incomplete-identity transition, complementing IncompleteSingletonComparisonTests'
    full-rebuild coverage of the same underlying logic."""

    def test_targeted_incomplete_ibm_to_incomplete_avgo(self):
        signal, raw, extraction = self._make_signal_with_extraction(
            symbol="IBM", option_type="call", strike=None, expiration=None
        )
        self.service.rebuild_lifecycles_for_raw_message_ids([raw.id])
        old_singleton_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id
        self.assertIsNotNone(old_singleton_id)

        supersede_extraction(self.connection, extraction.id)
        self.connection.commit()
        extraction2 = create_message_extraction(
            self.connection, raw.id, parser_version="v2", parse_status="parsed"
        )
        self.connection.commit()
        signal2 = create_trade_signal(
            self.connection, raw.id, self.trader.id, "AVGO", "BOUGHT",
            option_type="call", strike=None, expiration=None,
            event_type="ENTRY", extraction_id=extraction2.id,
        )
        self.connection.commit()

        result = self.service.rebuild_lifecycles_for_raw_message_ids([raw.id])

        self.assertEqual(result.keys_changed, 1)
        self.assertEqual(result.keys_considered, result.keys_changed + result.keys_unchanged)
        # Old singleton superseded, old pointer cleared.
        self.assertFalse(get_trade_lifecycle_by_id(self.connection, old_singleton_id).is_current)
        self.assertIsNone(get_trade_signal_by_id(self.connection, signal.id).lifecycle_id)
        # Current signal receives exactly one correct pointer.
        self.assertEqual(result.signal_pointers_assigned, 1)
        new_singleton_id = get_trade_signal_by_id(self.connection, signal2.id).lifecycle_id
        self.assertIsNotNone(new_singleton_id)
        self.assertNotEqual(new_singleton_id, old_singleton_id)
        new_singleton = get_trade_lifecycle_by_id(self.connection, new_singleton_id)
        self.assertEqual(new_singleton.symbol, "AVGO")
        self.assertEqual(new_singleton.status, "unresolved")

    def test_targeted_incomplete_option_becomes_complete(self):
        signal, raw, extraction = self._make_signal_with_extraction(
            symbol="IBM", option_type="call", strike=None, expiration=None
        )
        self.service.rebuild_lifecycles_for_raw_message_ids([raw.id])
        old_singleton_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id
        self.assertIsNotNone(old_singleton_id)

        supersede_extraction(self.connection, extraction.id)
        self.connection.commit()
        extraction2 = create_message_extraction(
            self.connection, raw.id, parser_version="v2", parse_status="parsed"
        )
        self.connection.commit()
        signal2 = create_trade_signal(
            self.connection, raw.id, self.trader.id, "IBM", "BOUGHT",
            option_type="call", strike=Decimal("207.5"), expiration="2026-07-24",
            event_type="ENTRY", extraction_id=extraction2.id,
        )
        self.connection.commit()

        result = self.service.rebuild_lifecycles_for_raw_message_ids([raw.id])

        self.assertEqual(result.keys_considered, result.keys_changed + result.keys_unchanged)
        self.assertFalse(get_trade_lifecycle_by_id(self.connection, old_singleton_id).is_current)
        self.assertIsNone(get_trade_signal_by_id(self.connection, signal.id).lifecycle_id)
        new_lifecycle_id = get_trade_signal_by_id(self.connection, signal2.id).lifecycle_id
        self.assertIsNotNone(new_lifecycle_id)
        # No signal remains in both an incomplete singleton and a complete
        # lifecycle at once - the new pointer is a different lifecycle.
        self.assertNotEqual(new_lifecycle_id, old_singleton_id)
        new_lifecycle = get_trade_lifecycle_by_id(self.connection, new_lifecycle_id)
        self.assertEqual(new_lifecycle.status, "open")
        self.assertEqual(new_lifecycle.option_type, "call")
        self.assertEqual(new_lifecycle.strike, "207.5")
        self.assertEqual(new_lifecycle.expiration, "2026-07-24")

    def test_targeted_incomplete_signal_disappears_via_superseded_extraction(self):
        signal, raw, extraction = self._make_signal_with_extraction(
            option_type="call", strike=None, expiration=None
        )
        self.service.rebuild_lifecycles_for_raw_message_ids([raw.id])
        singleton_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id
        self.assertIsNotNone(singleton_id)

        supersede_extraction(self.connection, extraction.id)
        self.connection.commit()

        result = self.service.rebuild_lifecycles_for_raw_message_ids([raw.id])

        self.assertEqual(result.keys_considered, result.keys_changed + result.keys_unchanged)
        self.assertEqual(result.lifecycles_created, 0)
        self.assertEqual(result.lifecycles_superseded, 1)
        self.assertFalse(get_trade_lifecycle_by_id(self.connection, singleton_id).is_current)
        self.assertIsNone(get_trade_signal_by_id(self.connection, signal.id).lifecycle_id)

    def test_targeted_incomplete_signal_becomes_lifecycle_ineligible(self):
        signal, raw, extraction = self._make_signal_with_extraction(
            option_type="call", strike=None, expiration=None
        )
        self.service.rebuild_lifecycles_for_raw_message_ids([raw.id])
        singleton_id = get_trade_signal_by_id(self.connection, signal.id).lifecycle_id
        self.assertIsNotNone(singleton_id)

        supersede_extraction(self.connection, extraction.id)
        self.connection.commit()
        extraction2 = create_message_extraction(
            self.connection, raw.id, parser_version="v2", parse_status="unrecognized"
        )
        self.connection.commit()
        # event_type left None - a recognized-but-ineligible signal, not a
        # departed one, yet the same "no current lifecycle-eligible
        # signal" outcome applies.
        ineligible_signal = create_trade_signal(
            self.connection, raw.id, self.trader.id, "IBM", "BOUGHT",
            option_type="call", extraction_id=extraction2.id,
        )
        self.connection.commit()

        result = self.service.rebuild_lifecycles_for_raw_message_ids([raw.id])

        self.assertEqual(result.keys_considered, result.keys_changed + result.keys_unchanged)
        self.assertEqual(result.lifecycles_created, 0)
        self.assertEqual(result.lifecycles_superseded, 1)
        self.assertFalse(get_trade_lifecycle_by_id(self.connection, singleton_id).is_current)
        self.assertIsNone(get_trade_signal_by_id(self.connection, ineligible_signal.id).lifecycle_id)

    def test_targeted_duplicate_raw_message_ids_deduplicated(self):
        signal, raw = self._make_signal(option_type="call", strike=None, expiration=None)

        result = self.service.rebuild_lifecycles_for_raw_message_ids([raw.id, raw.id, raw.id])

        self.assertEqual(result.keys_considered, 1)
        self.assertEqual(result.keys_considered, result.keys_changed + result.keys_unchanged)
        self.assertEqual(result.lifecycles_created, 1)
        self.assertIsNotNone(get_trade_signal_by_id(self.connection, signal.id).lifecycle_id)

    def test_targeted_rebuild_does_not_touch_unrelated_key_or_singleton(self):
        unrelated_signal, _ = self._make_signal(
            symbol="MSFT", strike=Decimal("380"), option_type="put"
        )
        unrelated_incomplete_signal, _ = self._make_signal(
            symbol="TSLA", option_type="put", strike=None, expiration=None
        )
        target_signal, target_raw, target_extraction = self._make_signal_with_extraction(
            symbol="IBM", option_type="call", strike=None, expiration=None
        )
        self.service.rebuild_all_lifecycles()

        unrelated_lifecycle_id = get_trade_signal_by_id(
            self.connection, unrelated_signal.id
        ).lifecycle_id
        unrelated_singleton_id = get_trade_signal_by_id(
            self.connection, unrelated_incomplete_signal.id
        ).lifecycle_id

        supersede_extraction(self.connection, target_extraction.id)
        self.connection.commit()
        extraction2 = create_message_extraction(
            self.connection, target_raw.id, parser_version="v2", parse_status="parsed"
        )
        self.connection.commit()
        target_signal2 = create_trade_signal(
            self.connection, target_raw.id, self.trader.id, "GOOG", "BOUGHT",
            option_type="call", strike=None, expiration=None,
            event_type="ENTRY", extraction_id=extraction2.id,
        )
        self.connection.commit()

        result = self.service.rebuild_lifecycles_for_raw_message_ids([target_raw.id])

        self.assertEqual(result.keys_changed, 1)
        self.assertEqual(result.keys_considered, result.keys_changed + result.keys_unchanged)
        self.assertIsNotNone(get_trade_signal_by_id(self.connection, target_signal2.id).lifecycle_id)
        # Neither unrelated key/singleton was superseded, recreated, or
        # repointed by this targeted call.
        self.assertEqual(
            get_trade_signal_by_id(self.connection, unrelated_signal.id).lifecycle_id,
            unrelated_lifecycle_id,
        )
        self.assertTrue(
            get_trade_lifecycle_by_id(self.connection, unrelated_lifecycle_id).is_current
        )
        self.assertEqual(
            get_trade_signal_by_id(self.connection, unrelated_incomplete_signal.id).lifecycle_id,
            unrelated_singleton_id,
        )
        self.assertTrue(
            get_trade_lifecycle_by_id(self.connection, unrelated_singleton_id).is_current
        )


class RepeatedGenerationChronologyTests(_LifecycleServiceTestCase):
    """Recovery Milestone R6.4 final quality-gate correction: proves
    generation order follows whole-set chronology (received_at when every
    relevant signal has one, otherwise raw_message_id for the entire
    window - never a per-signal mix), not raw_message_id/lifecycle
    insertion order, and that a second rebuild over the same reversed-
    insertion-order data is fully idempotent."""

    def test_generation_order_follows_received_at_not_insertion_order(self):
        # The chronologically LATER generation's messages are created
        # FIRST (lower raw_message_id), and the EARLIER generation's
        # messages SECOND - so insertion order and chronological order
        # are deliberately reversed.
        entry2, _ = self._make_signal(
            strike=Decimal("207.5"), received_at="2026-07-24T03:00:00.000000+00:00"
        )
        exit2, _ = self._make_signal(
            strike=Decimal("207.5"), event_type="FULL_EXIT", qualifier="ALL OUT", action="SOLD",
            received_at="2026-07-24T04:00:00.000000+00:00",
        )
        entry1, _ = self._make_signal(
            strike=Decimal("207.5"), received_at="2026-07-24T01:00:00.000000+00:00"
        )
        exit1, _ = self._make_signal(
            strike=Decimal("207.5"), event_type="FULL_EXIT", qualifier="ALL OUT", action="SOLD",
            received_at="2026-07-24T02:00:00.000000+00:00",
        )
        # Confirm insertion order really is reversed relative to
        # chronology, so this test actually exercises the case it claims.
        self.assertLess(entry2.id, entry1.id)

        result = self.service.rebuild_all_lifecycles()

        self.assertEqual(result.lifecycles_created, 2)
        lifecycle1_id = get_trade_signal_by_id(self.connection, entry1.id).lifecycle_id
        lifecycle2_id = get_trade_signal_by_id(self.connection, entry2.id).lifecycle_id
        self.assertNotEqual(lifecycle1_id, lifecycle2_id)
        self.assertEqual(
            get_trade_signal_by_id(self.connection, exit1.id).lifecycle_id, lifecycle1_id
        )
        self.assertEqual(
            get_trade_signal_by_id(self.connection, exit2.id).lifecycle_id, lifecycle2_id
        )
        # Generation order (persisted trade_lifecycles.id order) follows
        # whole-set received_at chronology, not raw_message_id/creation
        # order: the chronologically-earlier generation (entry1/exit1)
        # must be persisted before the later one (entry2/exit2), even
        # though entry2/exit2's own raw messages were created first.
        self.assertLess(lifecycle1_id, lifecycle2_id)

    def test_repeated_rebuild_of_reversed_insertion_generations_is_idempotent(self):
        entry2, _ = self._make_signal(
            strike=Decimal("207.5"), received_at="2026-07-24T03:00:00.000000+00:00"
        )
        exit2, _ = self._make_signal(
            strike=Decimal("207.5"), event_type="FULL_EXIT", qualifier="ALL OUT", action="SOLD",
            received_at="2026-07-24T04:00:00.000000+00:00",
        )
        entry1, _ = self._make_signal(
            strike=Decimal("207.5"), received_at="2026-07-24T01:00:00.000000+00:00"
        )
        exit1, _ = self._make_signal(
            strike=Decimal("207.5"), event_type="FULL_EXIT", qualifier="ALL OUT", action="SOLD",
            received_at="2026-07-24T02:00:00.000000+00:00",
        )
        self.service.rebuild_all_lifecycles()
        lifecycle1_id_before = get_trade_signal_by_id(self.connection, entry1.id).lifecycle_id
        lifecycle2_id_before = get_trade_signal_by_id(self.connection, entry2.id).lifecycle_id
        _, events_before, _ = self._capture_full_state()

        result2 = self.service.rebuild_all_lifecycles()

        self.assertEqual(result2.keys_changed, 0)
        self.assertEqual(result2.keys_unchanged, 1)
        lifecycle1_id_after = get_trade_signal_by_id(self.connection, entry1.id).lifecycle_id
        lifecycle2_id_after = get_trade_signal_by_id(self.connection, entry2.id).lifecycle_id
        self.assertEqual(lifecycle1_id_before, lifecycle1_id_after)
        self.assertEqual(lifecycle2_id_before, lifecycle2_id_after)
        _, events_after, _ = self._capture_full_state()
        self.assertEqual(events_before, events_after)
        self.assertTrue(get_trade_lifecycle_by_id(self.connection, lifecycle1_id_after).is_current)
        self.assertTrue(get_trade_lifecycle_by_id(self.connection, lifecycle2_id_after).is_current)

    def test_whole_set_fallback_to_raw_message_id_when_one_first_member_lacks_received_at(self):
        # entry1 carries a LATER real timestamp than exit2's real
        # timestamp, and entry2 has no received_at at all. Because even
        # one missing timestamp forces the ENTIRE window to fall back to
        # (raw_message_id, trade_signal_id) ordering - never a partial
        # mix of received_at-where-present plus insertion-order-elsewhere
        # - the persisted generation order must follow raw_message_id/
        # insertion order (entry1/exit1 first, entry2/exit2 second), the
        # opposite of what entry1's/exit2's own timestamps would suggest.
        entry1, _ = self._make_signal(
            strike=Decimal("207.5"), received_at="2026-07-24T03:00:00.000000+00:00"
        )
        exit1, _ = self._make_signal(
            strike=Decimal("207.5"), event_type="FULL_EXIT", qualifier="ALL OUT", action="SOLD",
            received_at="2026-07-24T04:00:00.000000+00:00",
        )
        entry2, _ = self._make_signal(strike=Decimal("207.5"), received_at=None)
        exit2, _ = self._make_signal(
            strike=Decimal("207.5"), event_type="FULL_EXIT", qualifier="ALL OUT", action="SOLD",
            received_at="2026-07-24T02:00:00.000000+00:00",
        )

        result = self.service.rebuild_all_lifecycles()

        self.assertEqual(result.lifecycles_created, 2)
        lifecycle1_id = get_trade_signal_by_id(self.connection, entry1.id).lifecycle_id
        lifecycle2_id = get_trade_signal_by_id(self.connection, entry2.id).lifecycle_id
        self.assertEqual(
            get_trade_signal_by_id(self.connection, exit1.id).lifecycle_id, lifecycle1_id
        )
        self.assertEqual(
            get_trade_signal_by_id(self.connection, exit2.id).lifecycle_id, lifecycle2_id
        )
        self.assertLess(lifecycle1_id, lifecycle2_id)


class ScopeProtectionTests(unittest.TestCase):
    def test_lifecycle_module_remains_pure_and_unchanged(self):
        # Mirrors tests.test_lifecycle.PureModuleBoundaryTests' own
        # technique: checks only the import lines, not the whole file
        # text, since the module's docstring legitimately mentions
        # "sqlite3" in prose describing what it does NOT import.
        import database.lifecycle as lifecycle_module

        with open(lifecycle_module.__file__, "r", encoding="utf-8") as f:
            import_lines = [
                line
                for line in f
                if line.strip().startswith("import ") or line.strip().startswith("from ")
            ]

        self.assertTrue(import_lines, "expected at least the stdlib imports")
        for line in import_lines:
            self.assertNotIn("sqlite3", line)
            self.assertNotIn("database.repository", line)
            self.assertNotIn("database.service", line)

    def test_service_module_has_no_direct_lifecycle_table_sql(self):
        import database.service as service_module

        with open(service_module.__file__, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertNotIn("FROM trade_lifecycles", content)
        self.assertNotIn("INTO trade_lifecycles", content)
        self.assertNotIn("FROM trade_lifecycle_events", content)
        self.assertNotIn("INTO trade_lifecycle_events", content)

    def test_no_ui_import_in_service_module(self):
        import database.service as service_module

        with open(service_module.__file__, "r", encoding="utf-8") as f:
            import_lines = [
                line
                for line in f
                if line.strip().startswith("import ") or line.strip().startswith("from ")
            ]
        for line in import_lines:
            self.assertNotIn("streamlit", line)
            self.assertNotIn("app.streamlit_app", line)


if __name__ == "__main__":
    unittest.main()
