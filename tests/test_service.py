"""Tests for database/service.py.

Covers Milestone 2B.6a: TradeService scaffold and the
check_duplicate_signal advisory duplicate check.
"""

import os
import tempfile
import unittest
from decimal import Decimal

from database.config import DatabaseConfig
from database.db import get_connection, initialize_database
from database.repository import (
    create_raw_message,
    create_trade_signal,
    create_trader,
    get_or_create_source,
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


if __name__ == "__main__":
    unittest.main()
