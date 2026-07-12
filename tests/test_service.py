"""Tests for database/service.py.

Covers Milestone 2B.6a: TradeService scaffold and the
check_duplicate_signal advisory duplicate check.
Covers Milestone 2B.6b: the edit-on-update rule.
Covers Milestone 2B.6c: the ingest_message entry point.
"""

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from database import repository
from database.config import DatabaseConfig
from database.db import get_connection, initialize_database
from database.repository import (
    create_raw_message,
    create_trade_signal,
    create_trader,
    get_or_create_source,
    get_trade_signal_edits,
)
from database.service import DUPLICATE_WINDOW_MINUTES, TradeService


class TradeServiceCheckDuplicateSignalTests(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = path
        self.config = DatabaseConfig(db_path=path)
        initialize_database(self.config)
        self.connection = get_connection(self.config)

        source_id = get_or_create_source(self.connection, "discord").id
        trader = create_trader(self.connection, source_id, "alice")
        raw_message = create_raw_message(self.connection, source_id, "BTO SPY 500c")
        self.connection.commit()

        self.source_id = source_id
        self.trader_id = trader.id
        self.raw_message_id = raw_message.id
        self.service = TradeService(self.connection)

    def tearDown(self):
        self.connection.close()
        os.remove(self.db_path)

    def _create_signal_at(self, created_at, **kwargs):
        fields = {
            "raw_message_id": self.raw_message_id,
            "trader_id": self.trader_id,
            "symbol": "SPY",
            "action": "BTO",
            "option_type": "call",
            "price": Decimal("3.25"),
            "expiration": "2026-12-18",
        }
        fields.update(kwargs)
        signal = create_trade_signal(self.connection, **fields)
        self.connection.execute(
            "UPDATE trade_signals SET created_at = ? WHERE id = ?",
            (created_at, signal.id),
        )
        self.connection.commit()
        return signal

    def test_match_found_within_window_returns_warning_string(self):
        self._create_signal_at("2026-07-12 12:03:00")

        result = self.service.check_duplicate_signal(
            self.trader_id,
            "SPY",
            "BTO",
            "call",
            Decimal("3.25"),
            "2026-12-18",
            reference_time="2026-07-12 12:05:00",
        )

        self.assertIsInstance(result, str)

    def test_no_match_outside_window_returns_none(self):
        self._create_signal_at("2026-07-12 11:00:00")

        result = self.service.check_duplicate_signal(
            self.trader_id,
            "SPY",
            "BTO",
            "call",
            Decimal("3.25"),
            "2026-12-18",
            reference_time="2026-07-12 12:05:00",
        )

        self.assertIsNone(result)

    def test_no_match_on_differing_fields_returns_none(self):
        self._create_signal_at("2026-07-12 12:03:00", symbol="QQQ")

        result = self.service.check_duplicate_signal(
            self.trader_id,
            "SPY",
            "BTO",
            "call",
            Decimal("3.25"),
            "2026-12-18",
            reference_time="2026-07-12 12:05:00",
        )

        self.assertIsNone(result)

    def test_window_lower_boundary_is_inclusive(self):
        reference_time = "2026-07-12 12:05:00"
        self._create_signal_at("2026-07-12 12:00:00")

        result = self.service.check_duplicate_signal(
            self.trader_id,
            "SPY",
            "BTO",
            "call",
            Decimal("3.25"),
            "2026-12-18",
            reference_time=reference_time,
        )

        self.assertIsNotNone(result)

    def test_just_outside_lower_boundary_returns_none(self):
        reference_time = "2026-07-12 12:05:00"
        self._create_signal_at("2026-07-12 11:59:59")

        result = self.service.check_duplicate_signal(
            self.trader_id,
            "SPY",
            "BTO",
            "call",
            Decimal("3.25"),
            "2026-12-18",
            reference_time=reference_time,
        )

        self.assertIsNone(result)

    def test_window_upper_boundary_is_inclusive(self):
        reference_time = "2026-07-12 12:05:00"
        self._create_signal_at(reference_time)

        result = self.service.check_duplicate_signal(
            self.trader_id,
            "SPY",
            "BTO",
            "call",
            Decimal("3.25"),
            "2026-12-18",
            reference_time=reference_time,
        )

        self.assertIsNotNone(result)

    def test_multiple_matches_still_returns_single_string(self):
        self._create_signal_at("2026-07-12 12:01:00")
        self._create_signal_at("2026-07-12 12:02:00")

        result = self.service.check_duplicate_signal(
            self.trader_id,
            "SPY",
            "BTO",
            "call",
            Decimal("3.25"),
            "2026-12-18",
            reference_time="2026-07-12 12:05:00",
        )

        self.assertIsInstance(result, str)

    def test_no_existing_signals_returns_none_without_raising(self):
        result = self.service.check_duplicate_signal(
            self.trader_id,
            "SPY",
            "BTO",
            "call",
            Decimal("3.25"),
            "2026-12-18",
            reference_time="2026-07-12 12:05:00",
        )

        self.assertIsNone(result)

    def test_none_option_type_price_expiration_matches(self):
        self._create_signal_at(
            "2026-07-12 12:03:00", option_type=None, price=None, expiration=None
        )

        result = self.service.check_duplicate_signal(
            self.trader_id,
            "SPY",
            "BTO",
            None,
            None,
            None,
            reference_time="2026-07-12 12:05:00",
        )

        self.assertIsNotNone(result)

    def test_missing_trader_id_rejected(self):
        with self.assertRaises(ValueError):
            self.service.check_duplicate_signal(
                None,
                "SPY",
                "BTO",
                "call",
                Decimal("3.25"),
                "2026-12-18",
                reference_time="2026-07-12 12:05:00",
            )

    def test_whitespace_only_symbol_rejected(self):
        with self.assertRaises(ValueError):
            self.service.check_duplicate_signal(
                self.trader_id,
                "   ",
                "BTO",
                "call",
                Decimal("3.25"),
                "2026-12-18",
                reference_time="2026-07-12 12:05:00",
            )

    def test_invalid_price_type_rejected(self):
        with self.assertRaises(TypeError):
            self.service.check_duplicate_signal(
                self.trader_id,
                "SPY",
                "BTO",
                "call",
                3.25,
                "2026-12-18",
                reference_time="2026-07-12 12:05:00",
            )

    def test_missing_reference_time_rejected(self):
        with self.assertRaises(ValueError):
            self.service.check_duplicate_signal(
                self.trader_id,
                "SPY",
                "BTO",
                "call",
                Decimal("3.25"),
                "2026-12-18",
                reference_time=None,
            )

    def test_malformed_reference_time_rejected(self):
        with self.assertRaises(ValueError):
            self.service.check_duplicate_signal(
                self.trader_id,
                "SPY",
                "BTO",
                "call",
                Decimal("3.25"),
                "2026-12-18",
                reference_time="not-a-timestamp",
            )

    def test_duplicate_window_minutes_constant_is_five(self):
        self.assertEqual(DUPLICATE_WINDOW_MINUTES, 5)

    def test_check_does_not_commit_pending_writes(self):
        other_connection = get_connection(self.config)
        try:
            signal = create_trade_signal(
                self.connection,
                self.raw_message_id,
                self.trader_id,
                "SPY",
                "BTO",
                price=Decimal("3.25"),
            )
            self.connection.execute(
                "UPDATE trade_signals SET created_at = ? WHERE id = ?",
                ("2026-07-12 12:03:00", signal.id),
            )

            self.service.check_duplicate_signal(
                self.trader_id,
                "SPY",
                "BTO",
                None,
                Decimal("3.25"),
                None,
                reference_time="2026-07-12 12:05:00",
            )

            row = other_connection.execute(
                "SELECT COUNT(*) FROM trade_signals"
            ).fetchone()
            self.assertEqual(row[0], 0)
        finally:
            other_connection.close()


class TradeServiceUpdateTradeSignalTests(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = path
        self.config = DatabaseConfig(db_path=path)
        initialize_database(self.config)
        self.connection = get_connection(self.config)

        source_id = get_or_create_source(self.connection, "discord").id
        trader = create_trader(self.connection, source_id, "alice")
        raw_message = create_raw_message(self.connection, source_id, "BTO SPY 500c")
        signal = create_trade_signal(
            self.connection,
            raw_message.id,
            trader.id,
            "SPY",
            "BTO",
            option_type="call",
            price=Decimal("3.25"),
        )
        self.connection.commit()

        self.trader_id = trader.id
        self.raw_message_id = raw_message.id
        self.signal_id = signal.id
        self.original_symbol = signal.symbol
        self.service = TradeService(self.connection)

    def tearDown(self):
        self.connection.close()
        os.remove(self.db_path)

    def _edit_count(self):
        row = self.connection.execute(
            "SELECT COUNT(*) FROM trade_signal_edits"
        ).fetchone()
        return row[0]

    def test_update_returns_updated_signal(self):
        updated = self.service.update_trade_signal(self.signal_id, symbol="QQQ")

        self.assertEqual(updated.symbol, "QQQ")
        self.assertEqual(updated.id, self.signal_id)

    def test_update_writes_exactly_one_edit_with_correct_pre_edit_snapshot(self):
        self.service.update_trade_signal(self.signal_id, symbol="QQQ")

        edits = get_trade_signal_edits(self.connection, self.signal_id)

        self.assertEqual(len(edits), 1)
        snapshot = json.loads(edits[0].previous_values)
        self.assertEqual(snapshot["symbol"], "SPY")
        self.assertEqual(snapshot["option_type"], "call")
        self.assertEqual(snapshot["price"], "3.25")

    def test_multiple_updates_produce_ordered_history(self):
        self.service.update_trade_signal(self.signal_id, symbol="QQQ")
        self.service.update_trade_signal(self.signal_id, symbol="IWM")

        edits = get_trade_signal_edits(self.connection, self.signal_id)

        self.assertEqual(len(edits), 2)
        self.assertLess(edits[0].id, edits[1].id)
        self.assertEqual(json.loads(edits[0].previous_values)["symbol"], "SPY")
        self.assertEqual(json.loads(edits[1].previous_values)["symbol"], "QQQ")

    def test_missing_trade_signal_raises_and_writes_no_edit(self):
        with self.assertRaises(ValueError):
            self.service.update_trade_signal(999999, symbol="QQQ")

        self.assertEqual(self._edit_count(), 0)

    def test_empty_update_raises_and_writes_no_edit(self):
        with self.assertRaises(ValueError):
            self.service.update_trade_signal(self.signal_id)

        self.assertEqual(self._edit_count(), 0)

    def test_unknown_field_raises_and_writes_no_edit(self):
        with self.assertRaises(ValueError):
            self.service.update_trade_signal(self.signal_id, not_a_real_field="x")

        self.assertEqual(self._edit_count(), 0)

    def test_invalid_price_type_raises_and_writes_no_edit(self):
        with self.assertRaises(TypeError):
            self.service.update_trade_signal(self.signal_id, price=3.25)

        self.assertEqual(self._edit_count(), 0)

    def test_service_and_repository_reject_the_same_invalid_updates(self):
        invalid_cases = [
            ({}, ValueError),
            ({"not_a_real_field": "x"}, ValueError),
            ({"id": 999}, ValueError),
            ({"created_at": "2000-01-01T00:00:00"}, ValueError),
            ({"updated_at": "2000-01-01T00:00:00"}, ValueError),
            ({"raw_message_id": None}, ValueError),
            ({"trader_id": None}, ValueError),
            ({"symbol": "   "}, ValueError),
            ({"action": "   "}, ValueError),
            ({"price": 3.25}, TypeError),
            ({"price": "3.25"}, TypeError),
        ]

        for changed_fields, expected_exception in invalid_cases:
            with self.subTest(changed_fields=changed_fields):
                with self.assertRaises(expected_exception):
                    repository.update_trade_signal(
                        self.connection, self.signal_id, **changed_fields
                    )
                with self.assertRaises(expected_exception):
                    self.service.update_trade_signal(self.signal_id, **changed_fields)

                self.assertEqual(self._edit_count(), 0)


class TradeServiceIngestMessageTests(unittest.TestCase):
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

    def _signal_count(self):
        row = self.connection.execute("SELECT COUNT(*) FROM trade_signals").fetchone()
        return row[0]

    def _raw_message_count(self):
        row = self.connection.execute("SELECT COUNT(*) FROM raw_messages").fetchone()
        return row[0]

    def _trader_count(self):
        row = self.connection.execute("SELECT COUNT(*) FROM traders").fetchone()
        return row[0]

    def _reference_time(self):
        # trade_signals.created_at is DB-generated using SQLite's
        # CURRENT_TIMESTAMP, which is UTC, so tests exercising duplicate
        # detection through ingest_message (as opposed to
        # check_duplicate_signal directly, which takes signal created_at as
        # an explicit fixture) need a UTC reference_time bracketing real
        # "now", not a fixed fictional timestamp or local wall-clock time.
        return (datetime.now(timezone.utc) + timedelta(minutes=1)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    def test_new_message_with_no_signals_persists_message_only(self):
        result = self.service.ingest_message(
            "discord",
            "alice",
            "just chatting, no trades here",
            reference_time="2026-07-13 09:00:00",
        )

        self.assertEqual(result["raw_message"].raw_text, "just chatting, no trades here")
        self.assertEqual(result["trade_signals"], [])
        self.assertEqual(result["duplicate_warnings"], [])
        self.assertEqual(self._raw_message_count(), 1)
        self.assertEqual(self._signal_count(), 0)

    def test_new_message_with_one_signal_and_no_duplicate(self):
        result = self.service.ingest_message(
            "discord",
            "alice",
            "BTO SPY 500c @3.25",
            reference_time="2026-07-13 09:00:00",
            trade_signals=[
                {
                    "symbol": "SPY",
                    "action": "BTO",
                    "option_type": "call",
                    "price": Decimal("3.25"),
                    "expiration": "2026-12-18",
                }
            ],
        )

        self.assertEqual(len(result["trade_signals"]), 1)
        self.assertEqual(result["trade_signals"][0].symbol, "SPY")
        self.assertEqual(result["duplicate_warnings"], [None])

    def test_signal_matching_prior_signal_within_window_still_persists_with_warning(self):
        reference_time = self._reference_time()

        self.service.ingest_message(
            "discord",
            "alice",
            "BTO SPY 500c @3.25",
            reference_time=reference_time,
            external_trader_id="disc-1",
            trade_signals=[
                {
                    "symbol": "SPY",
                    "action": "BTO",
                    "option_type": "call",
                    "price": Decimal("3.25"),
                    "expiration": "2026-12-18",
                }
            ],
        )

        result = self.service.ingest_message(
            "discord",
            "alice",
            "BTO SPY 500c @3.25 again",
            reference_time=reference_time,
            external_trader_id="disc-1",
            trade_signals=[
                {
                    "symbol": "SPY",
                    "action": "BTO",
                    "option_type": "call",
                    "price": Decimal("3.25"),
                    "expiration": "2026-12-18",
                }
            ],
        )

        self.assertEqual(len(result["trade_signals"]), 1)
        self.assertIsNotNone(result["duplicate_warnings"][0])
        self.assertEqual(self._signal_count(), 2)

    def test_multiple_signals_mixed_duplicate_and_non_duplicate(self):
        reference_time = self._reference_time()

        self.service.ingest_message(
            "discord",
            "alice",
            "BTO SPY 500c @3.25",
            reference_time=reference_time,
            external_trader_id="disc-1",
            trade_signals=[
                {
                    "symbol": "SPY",
                    "action": "BTO",
                    "option_type": "call",
                    "price": Decimal("3.25"),
                    "expiration": "2026-12-18",
                }
            ],
        )

        result = self.service.ingest_message(
            "discord",
            "alice",
            "BTO SPY 500c @3.25 and BTO QQQ 400p @1.10",
            reference_time=reference_time,
            external_trader_id="disc-1",
            trade_signals=[
                {
                    "symbol": "SPY",
                    "action": "BTO",
                    "option_type": "call",
                    "price": Decimal("3.25"),
                    "expiration": "2026-12-18",
                },
                {
                    "symbol": "QQQ",
                    "action": "BTO",
                    "option_type": "put",
                    "price": Decimal("1.10"),
                    "expiration": "2026-12-18",
                },
            ],
        )

        self.assertEqual(len(result["trade_signals"]), 2)
        self.assertIsNotNone(result["duplicate_warnings"][0])
        self.assertIsNone(result["duplicate_warnings"][1])

    def test_two_identical_signals_in_same_message_second_flags_first(self):
        result = self.service.ingest_message(
            "discord",
            "alice",
            "BTO SPY 500c @3.25 x2",
            reference_time=self._reference_time(),
            trade_signals=[
                {"symbol": "SPY", "action": "BTO", "price": Decimal("3.25")},
                {"symbol": "SPY", "action": "BTO", "price": Decimal("3.25")},
            ],
        )

        self.assertEqual(len(result["trade_signals"]), 2)
        self.assertIsNone(result["duplicate_warnings"][0])
        self.assertIsNotNone(result["duplicate_warnings"][1])

    def test_external_trader_id_reuses_existing_trader(self):
        first = self.service.ingest_message(
            "discord",
            "alice",
            "first message",
            reference_time="2026-07-13 09:00:00",
            external_trader_id="disc-123",
        )
        second = self.service.ingest_message(
            "discord",
            "alice",
            "second message",
            reference_time="2026-07-13 09:01:00",
            external_trader_id="disc-123",
        )

        self.assertEqual(first["trader"].id, second["trader"].id)
        self.assertEqual(self._trader_count(), 1)

    def test_no_external_trader_id_always_creates_new_trader(self):
        first = self.service.ingest_message(
            "discord",
            "alice",
            "first message",
            reference_time="2026-07-13 09:00:00",
        )
        second = self.service.ingest_message(
            "discord",
            "alice",
            "second message",
            reference_time="2026-07-13 09:01:00",
        )

        self.assertNotEqual(first["trader"].id, second["trader"].id)
        self.assertEqual(self._trader_count(), 2)

    def test_created_signals_reference_correct_raw_message_and_trader(self):
        result = self.service.ingest_message(
            "discord",
            "alice",
            "BTO SPY 500c @3.25",
            reference_time="2026-07-13 09:00:00",
            trade_signals=[{"symbol": "SPY", "action": "BTO"}],
        )

        signal = result["trade_signals"][0]
        self.assertEqual(signal.raw_message_id, result["raw_message"].id)
        self.assertEqual(signal.trader_id, result["trader"].id)

    def test_metadata_round_trips(self):
        result = self.service.ingest_message(
            "discord",
            "alice",
            "BTO SPY 500c @3.25",
            reference_time="2026-07-13 09:00:00",
            metadata={"channel_id": "123", "guild_id": "456"},
        )

        self.assertEqual(
            result["raw_message"].metadata,
            {"channel_id": "123", "guild_id": "456"},
        )

    def test_empty_source_name_raises_and_writes_nothing(self):
        with self.assertRaises(ValueError):
            self.service.ingest_message(
                "   ",
                "alice",
                "BTO SPY 500c",
                reference_time="2026-07-13 09:00:00",
            )

        self.assertEqual(self._raw_message_count(), 0)

    def test_empty_trader_name_raises_and_writes_nothing(self):
        with self.assertRaises(ValueError):
            self.service.ingest_message(
                "discord",
                "   ",
                "BTO SPY 500c",
                reference_time="2026-07-13 09:00:00",
            )

        self.assertEqual(self._raw_message_count(), 0)
        self.assertEqual(self._trader_count(), 0)

    def test_missing_reference_time_raises(self):
        with self.assertRaises(ValueError):
            self.service.ingest_message(
                "discord",
                "alice",
                "BTO SPY 500c",
                reference_time=None,
                trade_signals=[{"symbol": "SPY", "action": "BTO"}],
            )

    def test_malformed_reference_time_raises(self):
        with self.assertRaises(ValueError):
            self.service.ingest_message(
                "discord",
                "alice",
                "BTO SPY 500c",
                reference_time="not-a-timestamp",
                trade_signals=[{"symbol": "SPY", "action": "BTO"}],
            )

    def test_invalid_price_type_on_signal_raises(self):
        with self.assertRaises(TypeError):
            self.service.ingest_message(
                "discord",
                "alice",
                "BTO SPY 500c",
                reference_time="2026-07-13 09:00:00",
                trade_signals=[{"symbol": "SPY", "action": "BTO", "price": 3.25}],
            )

    def test_signal_missing_symbol_raises(self):
        with self.assertRaises(ValueError):
            self.service.ingest_message(
                "discord",
                "alice",
                "BTO SPY 500c",
                reference_time="2026-07-13 09:00:00",
                trade_signals=[{"action": "BTO"}],
            )

    def test_duplicate_external_message_id_raises_integrity_error(self):
        self.service.ingest_message(
            "discord",
            "alice",
            "BTO SPY 500c",
            reference_time="2026-07-13 09:00:00",
            external_message_id="msg-1",
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.service.ingest_message(
                "discord",
                "alice",
                "BTO SPY 500c, edited",
                reference_time="2026-07-13 09:05:00",
                external_message_id="msg-1",
            )

    def test_does_not_commit(self):
        other_connection = get_connection(self.config)
        try:
            self.service.ingest_message(
                "discord",
                "alice",
                "BTO SPY 500c",
                reference_time="2026-07-13 09:00:00",
                trade_signals=[{"symbol": "SPY", "action": "BTO"}],
            )

            row = other_connection.execute(
                "SELECT COUNT(*) FROM raw_messages"
            ).fetchone()
            self.assertEqual(row[0], 0)
        finally:
            other_connection.close()


if __name__ == "__main__":
    unittest.main()
