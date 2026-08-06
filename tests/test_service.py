"""Tests for database/service.py.

Covers Milestone 2B.6a: TradeService scaffold and the
check_duplicate_signal advisory duplicate check.
Covers Milestone 2B.6b: the edit-on-update rule.
Covers Milestone 2B.6c: the ingest_message entry point.
Covers Milestone 2D.4: list_trade_signals_for_review()'s thin delegation
to database.repository.get_trade_signals_for_review().
Covers Milestone 2D.5: update_trade_signal()'s backward-compatible
controlled-correction mode (expected_current_values), and the new
list_trade_signal_audit_history() read-only delegation.
"""

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import get_type_hints
from unittest.mock import patch

from app.discord_adapter import segment_discord_batch
from database import repository
from database.config import DatabaseConfig
from database.db import get_connection, initialize_database
from database.lifecycle import SignalSnapshot
from database.models import (
    BatchIngestResult,
    ChannelCheckpoint,
    MessageIngestOutcome,
    ReprocessBatchResult,
    ReprocessOutcome,
    TradeLifecycleEvent,
)
from database.repository import (
    build_signal_snapshot_json,
    create_raw_message,
    create_trade_signal,
    create_trader,
    get_current_extraction,
    get_import_batch_by_id,
    get_or_create_source,
    get_raw_message_by_id,
    get_trade_signal_edits,
    get_traders_by_canonical_name,
)
from database.service import (
    AMBIGUITY_FLAG_INVALID_NATIVE_TIMESTAMP,
    AMBIGUITY_FLAG_TRADER_IDENTITY_AMBIGUOUS,
    AMBIGUITY_FLAG_TRADER_IDENTITY_MISSING,
    DUPLICATE_WINDOW_MINUTES,
    AuditHistoryError,
    LifecycleAnalyticsError,
    LifecycleSnapshotError,
    ReprocessingNotSupportedError,
    StaleTradeSignalError,
    TradeLifecycleNotFoundError,
    TradeService,
    TradeSignalNotFoundError,
    _REQUIRED_SNAPSHOT_FIELDS,
    _resolve_external_id,
)
from tests.discord_corpus_fixture import CORPUS


class _FailingConnection(sqlite3.Connection):
    """Test-only sqlite3.Connection subclass that can inject a controlled
    failure at BEGIN IMMEDIATE, COMMIT, rollback(), or a raw SQL ROLLBACK.

    A live sqlite3.Connection instance's execute/commit/rollback methods
    cannot be patched directly (they are read-only C-level attributes), so
    this subclass - constructed via sqlite3.connect(..., factory=...) -
    is the mechanism used to exercise TradeService._r5_write_transaction's
    documented failure points. fail_on_rollback (the rollback() method) and
    fail_on_rollback_sql (a raw SQL "ROLLBACK" statement, issued by
    TradeService._cleanup_failed_r5_transaction()'s fallback) are
    independently injectable, so both the "rollback() fails but the SQL
    fallback succeeds" and "both fail" cleanup paths can be exercised.
    """

    fail_on_begin = False
    fail_on_commit = False
    fail_on_rollback = False
    fail_on_rollback_sql = False
    rollback_called = False
    rollback_sql_called = False

    def execute(self, sql, *args, **kwargs):
        if sql == "BEGIN IMMEDIATE" and self.fail_on_begin:
            raise sqlite3.OperationalError("simulated BEGIN IMMEDIATE failure")
        if sql == "ROLLBACK":
            self.rollback_sql_called = True
            if self.fail_on_rollback_sql:
                raise sqlite3.OperationalError("simulated SQL ROLLBACK failure")
        return super().execute(sql, *args, **kwargs)

    def commit(self):
        if self.fail_on_commit:
            raise sqlite3.OperationalError("simulated COMMIT failure")
        return super().commit()

    def rollback(self):
        self.rollback_called = True
        if self.fail_on_rollback:
            raise sqlite3.OperationalError("simulated ROLLBACK failure")
        return super().rollback()


def _open_failing_connection(config: DatabaseConfig) -> _FailingConnection:
    """Mirrors database.db.get_connection()'s setup, using _FailingConnection
    as the connection factory. database/db.py itself is never modified."""
    connection = sqlite3.connect(
        config.db_path, timeout=config.busy_timeout_seconds, factory=_FailingConnection
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


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


class TradeServiceListTradeSignalsForReviewTests(unittest.TestCase):
    """Covers Milestone 2D.4: TradeService.list_trade_signals_for_review()."""

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = path
        self.config = DatabaseConfig(db_path=path)
        initialize_database(self.config)
        self.connection = get_connection(self.config)
        self.service = TradeService(self.connection)

        source_id = get_or_create_source(self.connection, "discord").id
        trader = create_trader(self.connection, source_id, "alice")
        raw_message = create_raw_message(self.connection, source_id, "BTO SPY 500c")
        create_trade_signal(
            self.connection,
            raw_message.id,
            trader.id,
            "SPY",
            "BTO",
            "call",
            Decimal("3.25"),
            "2026-12-18",
            "10 contracts",
        )
        self.connection.commit()

    def tearDown(self):
        self.connection.close()
        os.remove(self.db_path)

    def test_delegates_to_repository_function_with_given_arguments(self):
        with patch(
            "database.service.get_trade_signals_for_review",
            return_value=["sentinel"],
        ) as mock_get:
            result = self.service.list_trade_signals_for_review(
                source_name="discord",
                trader_name="alice",
                symbol="spy",
                date="2026-07-15",
                limit=5,
            )

        mock_get.assert_called_once_with(
            self.connection,
            source_name="discord",
            trader_name="alice",
            symbol="SPY",
            date="2026-07-15",
            limit=5,
        )
        self.assertEqual(result, ["sentinel"])

    def test_result_is_returned_unchanged(self):
        result = self.service.list_trade_signals_for_review()

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["symbol"], "SPY")
        self.assertEqual(result[0]["source_name"], "discord")
        self.assertEqual(result[0]["trader_name"], "alice")

    def test_nonblank_symbol_is_uppercased_before_delegation(self):
        result = self.service.list_trade_signals_for_review(symbol="spy")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["symbol"], "SPY")

    def test_blank_symbol_is_not_uppercased_or_treated_as_a_filter(self):
        with patch(
            "database.service.get_trade_signals_for_review",
            return_value=[],
        ) as mock_get:
            self.service.list_trade_signals_for_review(symbol="")

        mock_get.assert_called_once_with(
            self.connection,
            source_name=None,
            trader_name=None,
            symbol="",
            date=None,
            limit=100,
        )

    def test_none_symbol_is_passed_through_as_none(self):
        with patch(
            "database.service.get_trade_signals_for_review",
            return_value=[],
        ) as mock_get:
            self.service.list_trade_signals_for_review(symbol=None)

        mock_get.assert_called_once_with(
            self.connection,
            source_name=None,
            trader_name=None,
            symbol=None,
            date=None,
            limit=100,
        )

    def test_default_limit_is_100(self):
        with patch(
            "database.service.get_trade_signals_for_review",
            return_value=[],
        ) as mock_get:
            self.service.list_trade_signals_for_review()

        self.assertEqual(mock_get.call_args.kwargs["limit"], 100)

    def test_other_arguments_pass_through_unchanged(self):
        with patch(
            "database.service.get_trade_signals_for_review",
            return_value=[],
        ) as mock_get:
            self.service.list_trade_signals_for_review(
                source_name="telegram", trader_name="bob", date="2026-01-01"
            )

        mock_get.assert_called_once_with(
            self.connection,
            source_name="telegram",
            trader_name="bob",
            symbol=None,
            date="2026-01-01",
            limit=100,
        )


class TradeServiceLifecycleReadTests(unittest.TestCase):
    """Covers Recovery Milestone R6.7:
    TradeService.list_trade_lifecycle_events()."""

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

    @staticmethod
    def _snapshot_json(trade_signal_id, raw_message_id, ordering_key=None):
        """Build byte-correct canonical snapshot JSON via the real
        production serializer (database.repository.build_signal_snapshot_json()),
        never hand-rolled, so these fixtures can never silently drift from
        what production code actually persists."""
        if ordering_key is None:
            ordering_key = (raw_message_id, trade_signal_id)
        snapshot = SignalSnapshot(
            trade_signal_id=trade_signal_id,
            raw_message_id=raw_message_id,
            trader_id=1,
            symbol="SPY",
            option_type="call",
            strike="500",
            expiration="2026-12-18",
            event_type="ENTRY",
            qualifier=None,
            action="BTO",
            price="3.25",
            stated_entry_price=None,
            stated_return_pct=None,
            notes=None,
            extraction_id=None,
            ordering_key=ordering_key,
        )
        return build_signal_snapshot_json(snapshot)

    def _valid_event(
        self, *, id=1, trade_lifecycle_id=42, trade_signal_id=7,
        sequence_index=1, raw_message_id=99, created_at="2026-07-24 04:30:00",
        ordering_key=None,
    ):
        return TradeLifecycleEvent(
            id=id,
            trade_lifecycle_id=trade_lifecycle_id,
            trade_signal_id=trade_signal_id,
            sequence_index=sequence_index,
            signal_snapshot=self._snapshot_json(
                trade_signal_id, raw_message_id, ordering_key
            ),
            created_at=created_at,
        )

    # -- 1. Repository delegation -----------------------------------

    def test_delegates_to_repository_function_with_given_arguments(self):
        with patch(
            "database.service.get_trade_lifecycle_events", return_value=[],
        ) as mock_get:
            self.service.list_trade_lifecycle_events(42)

        mock_get.assert_called_once_with(self.connection, 42)

    # -- 2. Empty result ----------------------------------------------

    def test_empty_repository_result_returns_empty_list(self):
        with patch(
            "database.service.get_trade_lifecycle_events", return_value=[],
        ):
            result = self.service.list_trade_lifecycle_events(42)

        self.assertEqual(result, [])

    # -- 3. Decoded snapshot result -----------------------------------

    def test_snapshot_is_decoded_dict_not_raw_json(self):
        event = self._valid_event()
        with patch(
            "database.service.get_trade_lifecycle_events", return_value=[event],
        ):
            result = self.service.list_trade_lifecycle_events(event.trade_lifecycle_id)

        snapshot = result[0]["snapshot"]
        self.assertIsInstance(snapshot, dict)
        self.assertNotIsInstance(snapshot, str)
        self.assertEqual(set(snapshot.keys()), set(_REQUIRED_SNAPSHOT_FIELDS))

    # -- 4. Exact event metadata ---------------------------------------

    def test_result_contains_exactly_the_approved_metadata_keys(self):
        event = self._valid_event(
            id=5, trade_lifecycle_id=42, trade_signal_id=7, sequence_index=3,
            created_at="2026-07-24 05:00:00",
        )
        with patch(
            "database.service.get_trade_lifecycle_events", return_value=[event],
        ):
            result = self.service.list_trade_lifecycle_events(42)

        self.assertEqual(len(result), 1)
        entry = result[0]
        self.assertEqual(
            set(entry.keys()),
            {"id", "trade_lifecycle_id", "trade_signal_id", "sequence_index",
             "created_at", "snapshot"},
        )
        self.assertEqual(entry["id"], event.id)
        self.assertEqual(entry["trade_lifecycle_id"], event.trade_lifecycle_id)
        self.assertEqual(entry["trade_signal_id"], event.trade_signal_id)
        self.assertEqual(entry["sequence_index"], event.sequence_index)
        self.assertEqual(entry["created_at"], event.created_at)

    # -- 5. Ordering preservation ---------------------------------------

    def test_returned_order_matches_repository_order_exactly(self):
        # Deliberately fed out of sequence_index numeric order (5, 1, 3),
        # so a test that merely checked "ascending order" could not
        # distinguish "preserved" from "silently re-sorted." The service
        # must return exactly this order, proving no sort is applied.
        event_a = self._valid_event(id=1, trade_signal_id=10, sequence_index=5)
        event_b = self._valid_event(id=2, trade_signal_id=11, sequence_index=1)
        event_c = self._valid_event(id=3, trade_signal_id=12, sequence_index=3)
        with patch(
            "database.service.get_trade_lifecycle_events",
            return_value=[event_a, event_b, event_c],
        ):
            result = self.service.list_trade_lifecycle_events(42)

        self.assertEqual(
            [entry["sequence_index"] for entry in result], [5, 1, 3],
        )
        self.assertEqual(
            [entry["id"] for entry in result], [1, 2, 3],
        )

    # -- 6. Invalid JSON --------------------------------------------------

    def test_invalid_json_raises_lifecycle_snapshot_error(self):
        event = self._valid_event()
        event = TradeLifecycleEvent(
            id=event.id, trade_lifecycle_id=event.trade_lifecycle_id,
            trade_signal_id=event.trade_signal_id,
            sequence_index=event.sequence_index,
            signal_snapshot="not json at all", created_at=event.created_at,
        )
        with patch(
            "database.service.get_trade_lifecycle_events", return_value=[event],
        ):
            with self.assertRaises(LifecycleSnapshotError):
                self.service.list_trade_lifecycle_events(event.trade_lifecycle_id)

    # -- 7. Non-object JSON -------------------------------------------------

    def _event_with_raw_snapshot(self, raw_text):
        return TradeLifecycleEvent(
            id=1, trade_lifecycle_id=42, trade_signal_id=7,
            sequence_index=1, signal_snapshot=raw_text,
            created_at="2026-07-24 04:30:00",
        )

    def test_json_array_raises_lifecycle_snapshot_error(self):
        event = self._event_with_raw_snapshot(json.dumps([1, 2, 3]))
        with patch(
            "database.service.get_trade_lifecycle_events", return_value=[event],
        ):
            with self.assertRaises(LifecycleSnapshotError):
                self.service.list_trade_lifecycle_events(42)

    def test_json_scalar_raises_lifecycle_snapshot_error(self):
        event = self._event_with_raw_snapshot(json.dumps(42))
        with patch(
            "database.service.get_trade_lifecycle_events", return_value=[event],
        ):
            with self.assertRaises(LifecycleSnapshotError):
                self.service.list_trade_lifecycle_events(42)

    def test_json_string_raises_lifecycle_snapshot_error(self):
        event = self._event_with_raw_snapshot(json.dumps("just a string"))
        with patch(
            "database.service.get_trade_lifecycle_events", return_value=[event],
        ):
            with self.assertRaises(LifecycleSnapshotError):
                self.service.list_trade_lifecycle_events(42)

    # -- 8. Missing required field --------------------------------------

    def test_missing_required_field_raises_lifecycle_snapshot_error(self):
        decoded = json.loads(self._snapshot_json(7, 99))
        del decoded["price"]
        event = self._event_with_raw_snapshot(json.dumps(decoded))
        with patch(
            "database.service.get_trade_lifecycle_events", return_value=[event],
        ):
            with self.assertRaises(LifecycleSnapshotError):
                self.service.list_trade_lifecycle_events(42)

    # -- 9. trade_signal_id mismatch --------------------------------------

    def test_mismatched_trade_signal_id_raises_lifecycle_snapshot_error(self):
        # The snapshot's own embedded trade_signal_id (7) must match the
        # event row's trade_signal_id column - here the event column is a
        # different value (8), which the validator must reject.
        event = TradeLifecycleEvent(
            id=1, trade_lifecycle_id=42, trade_signal_id=8,
            sequence_index=1, signal_snapshot=self._snapshot_json(7, 99),
            created_at="2026-07-24 04:30:00",
        )
        with patch(
            "database.service.get_trade_lifecycle_events", return_value=[event],
        ):
            with self.assertRaises(LifecycleSnapshotError):
                self.service.list_trade_lifecycle_events(42)

    # -- 10. ordering_key validation --------------------------------------

    def _assert_ordering_key_rejected(self, ordering_key):
        event = self._valid_event(ordering_key=ordering_key)
        with patch(
            "database.service.get_trade_lifecycle_events", return_value=[event],
        ):
            with self.assertRaises(LifecycleSnapshotError):
                self.service.list_trade_lifecycle_events(event.trade_lifecycle_id)

    def test_ordering_key_wrong_type_raises(self):
        # _assert_ordering_key_rejected() cannot express this case: it
        # passes ordering_key through SignalSnapshot ->
        # build_signal_snapshot_json(), whose serializer stores
        # list(snapshot.ordering_key) - so a string would silently become
        # a JSON array of individual characters (wrong length, not wrong
        # type). Building and mutating the decoded dict directly, then
        # re-serializing with plain json.dumps(), is the only way to
        # persist a genuinely non-array ordering_key value and exercise
        # the validator's "ordering_key is not a JSON array" branch.
        decoded = json.loads(self._snapshot_json(7, 99))
        decoded["ordering_key"] = "not-a-list"
        event = self._event_with_raw_snapshot(json.dumps(decoded))
        with patch(
            "database.service.get_trade_lifecycle_events", return_value=[event],
        ):
            with self.assertRaises(LifecycleSnapshotError):
                self.service.list_trade_lifecycle_events(42)

    def test_ordering_key_wrong_length_raises(self):
        self._assert_ordering_key_rejected([99])

    def test_ordering_key_two_element_mismatched_raw_message_id_raises(self):
        # Canonical two-element form: [raw_message_id, trade_signal_id].
        self._assert_ordering_key_rejected([99 + 999999, 7])

    def test_ordering_key_two_element_mismatched_trade_signal_id_raises(self):
        self._assert_ordering_key_rejected([99, 7 + 999999])

    def test_ordering_key_three_element_mismatched_raw_message_id_raises(self):
        # Canonical three-element form:
        # [received_at, raw_message_id, trade_signal_id].
        self._assert_ordering_key_rejected(
            ["2026-07-24T04:30:00+00:00", 99 + 999999, 7]
        )

    def test_ordering_key_three_element_mismatched_trade_signal_id_raises(self):
        self._assert_ordering_key_rejected(
            ["2026-07-24T04:30:00+00:00", 99, 7 + 999999]
        )

    def test_ordering_key_three_element_blank_received_at_raises(self):
        self._assert_ordering_key_rejected(["   ", 99, 7])

    # -- 11. Caller-owned transaction preservation ------------------------

    def test_read_neither_commits_nor_rolls_back_caller_owned_work(self):
        self.connection.execute(
            "INSERT INTO sources (name) VALUES ('r6_7_probe')"
        )
        self.assertTrue(self.connection.in_transaction)

        result = self.service.list_trade_lifecycle_events(999999)
        self.assertEqual(result, [])

        self.assertTrue(
            self.connection.in_transaction,
            "list_trade_lifecycle_events() must not implicitly commit or "
            "roll back the caller's own pending transaction.",
        )

        other_connection = get_connection(self.config)
        try:
            row = other_connection.execute(
                "SELECT 1 FROM sources WHERE name = 'r6_7_probe'"
            ).fetchone()
            self.assertIsNone(
                row,
                "The uncommitted probe row must not be visible on a "
                "second connection - visibility here would prove the "
                "read silently committed the caller's pending work.",
            )
        finally:
            self.connection.rollback()
            row_after_rollback = other_connection.execute(
                "SELECT 1 FROM sources WHERE name = 'r6_7_probe'"
            ).fetchone()
            self.assertIsNone(row_after_rollback)
            other_connection.close()


class TradeServiceAnalyticsTests(unittest.TestCase):
    """Covers Recovery Milestone R7:
    TradeService.get_trade_lifecycle_analytics(),
    TradeService.list_current_trade_lifecycle_analytics(), and
    TradeService.list_trader_performance_summaries().

    Exercises the real pipeline (real trade_signals -> real
    rebuild_all_lifecycles()/correct_trade_signal() -> real persisted
    trade_lifecycles/trade_lifecycle_events) through a real, temporary,
    file-backed SQLite database - no lifecycle-engine or repository
    mocking - so these tests prove the three new methods' real,
    end-to-end orchestration, not just their call arguments.
    """

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
        self, *, symbol="IBM", action="BOUGHT", event_type="ENTRY",
        qualifier=None, price=None, strike=None, expiration="2026-07-24",
        trader_id=None,
    ):
        raw_message = create_raw_message(self.connection, self.source.id, "x")
        signal = create_trade_signal(
            self.connection,
            raw_message.id,
            trader_id if trader_id is not None else self.trader.id,
            symbol,
            action,
            option_type="call",
            strike=strike,
            expiration=expiration,
            event_type=event_type,
            qualifier=qualifier,
            price=price,
        )
        self.connection.commit()
        return signal, raw_message

    def _build_closed_long_lifecycle(self, symbol="IBM", trader_id=None):
        """Persist a real ENTRY + FULL_EXIT pair for one key and rebuild,
        returning the resulting current trade_lifecycles.id."""
        self._make_signal(
            symbol=symbol, action="BTO", event_type="ENTRY",
            price=Decimal("1.00"), strike=Decimal("207.5"), trader_id=trader_id,
        )
        self._make_signal(
            symbol=symbol, action="STC", event_type="FULL_EXIT", qualifier="ALL OUT",
            price=Decimal("2.00"), strike=Decimal("207.5"), trader_id=trader_id,
        )
        self.service.rebuild_all_lifecycles()
        lifecycles = repository.get_current_lifecycles_for_key(
            self.connection,
            trader_id if trader_id is not None else self.trader.id,
            symbol, "call", Decimal("207.5"), "2026-07-24",
        )
        self.assertEqual(len(lifecycles), 1)
        return lifecycles[0].id

    # -- get_trade_lifecycle_analytics(): strict ------------------------

    def test_strict_raises_not_found_for_missing_id(self):
        with self.assertRaises(TradeLifecycleNotFoundError):
            self.service.get_trade_lifecycle_analytics(999999)

    def test_strict_raises_analytics_error_for_zero_events(self):
        bare = repository.create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open",
            remaining_fraction="1",
        )
        self.connection.commit()

        with self.assertRaises(LifecycleAnalyticsError):
            self.service.get_trade_lifecycle_analytics(bare.id)

    def test_strict_propagates_lifecycle_snapshot_error_unchanged(self):
        signal, _ = self._make_signal(action="BTO", event_type="ENTRY", price=Decimal("1.00"))
        bare = repository.create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="closed",
            remaining_fraction="0",
        )
        repository.create_trade_lifecycle_event(
            self.connection, bare.id, signal.id, 1, "not valid json",
        )
        self.connection.commit()

        with self.assertRaises(LifecycleSnapshotError):
            self.service.get_trade_lifecycle_analytics(bare.id)

    def test_strict_returns_correct_result_for_real_long_win(self):
        lifecycle_id = self._build_closed_long_lifecycle()

        result = self.service.get_trade_lifecycle_analytics(lifecycle_id)

        self.assertEqual(result["outcome"], "win")
        self.assertEqual(result["direction"], "long")
        self.assertEqual(Decimal(result["gross_price_return_pct"]), Decimal("100.000000"))
        self.assertTrue(result["is_current"])
        self.assertIsNone(result["superseded_at"])
        self.assertIsInstance(result["exit_legs"], list)
        self.assertIsInstance(result["source_event_ids"], list)
        self.assertEqual(len(result["exit_legs"]), 1)
        self.assertIsInstance(result["exit_legs"][0], dict)

    def test_strict_accepts_superseded_id_and_exposes_supersession_metadata(self):
        old_lifecycle_id = self._build_closed_long_lifecycle(symbol="IBM")
        self._make_signal(
            symbol="AVGO", action="BTO", event_type="ENTRY",
            price=Decimal("5.00"),
        )
        # Correct the original IBM entry's symbol to a new key - a
        # key-changing correction that triggers a targeted rebuild,
        # superseding the old IBM generation.
        original_entry = repository.get_current_trade_signals_for_key(
            self.connection, self.trader.id, "IBM", "call", Decimal("207.5"), "2026-07-24",
        )[0]
        self.service.correct_trade_signal(
            original_entry.trade_signal_id,
            expected_current_values={
                "symbol": "IBM", "action": "BTO", "option_type": "call",
                "price": Decimal("1.00"), "expiration": "2026-07-24",
                "position_size": None,
            },
            symbol="IBMX", action="BTO", option_type="call",
            price=Decimal("1.00"), expiration="2026-07-24", position_size=None,
        )

        old_result = self.service.get_trade_lifecycle_analytics(old_lifecycle_id)
        self.assertFalse(old_result["is_current"])
        self.assertIsNotNone(old_result["superseded_at"])

    # -- list_current_trade_lifecycle_analytics(): tolerant -------------

    def test_tolerant_isolates_data_error_lifecycle_from_clean_ones(self):
        clean_id = self._build_closed_long_lifecycle(symbol="IBM")
        broken_signal, _ = self._make_signal(
            symbol="AVGO", action="BTO", event_type="ENTRY", price=Decimal("1.00"),
        )
        broken = repository.create_trade_lifecycle(
            self.connection, self.trader.id, "AVGO", status="closed",
            remaining_fraction="0",
        )
        broken_event = repository.create_trade_lifecycle_event(
            self.connection, broken.id, broken_signal.id, 1, "not valid json",
        )
        self.connection.commit()

        results = self.service.list_current_trade_lifecycle_analytics()
        by_id = {r["trade_lifecycle_id"]: r for r in results}

        self.assertEqual(len(results), 2)
        self.assertEqual(by_id[clean_id]["outcome"], "win")
        self.assertEqual(by_id[broken.id]["outcome"], "data_error")
        self.assertIsNotNone(by_id[broken.id]["analytics_error_detail"])
        # The membership row was found (unlike the zero-event case) - its
        # own content just failed to decode - so source_event_ids
        # reflects the event id(s) that were actually found, never
        # assumed empty just because the call ultimately failed.
        self.assertEqual(by_id[broken.id]["source_event_ids"], [broken_event.id])

    def test_tolerant_zero_event_lifecycle_becomes_data_error_not_omitted(self):
        bare = repository.create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open",
            remaining_fraction="1",
        )
        self.connection.commit()

        results = self.service.list_current_trade_lifecycle_analytics()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["outcome"], "data_error")
        self.assertEqual(results[0]["trade_lifecycle_id"], bare.id)

    def test_no_truncation_over_one_hundred_current_lifecycles(self):
        created_ids = []
        for _ in range(101):
            lifecycle = repository.create_trade_lifecycle(
                self.connection, self.trader.id, "IBM", status="open",
                remaining_fraction="1",
            )
            created_ids.append(lifecycle.id)
        self.connection.commit()

        results = self.service.list_current_trade_lifecycle_analytics()

        self.assertEqual(len(results), 101)
        self.assertEqual(
            {r["trade_lifecycle_id"] for r in results}, set(created_ids)
        )

    def test_trader_id_filter_scopes_to_one_trader(self):
        other_trader = create_trader(self.connection, self.source.id, "Sarang")
        self.connection.commit()
        own_id = self._build_closed_long_lifecycle(symbol="IBM")
        self._build_closed_long_lifecycle(symbol="AVGO", trader_id=other_trader.id)

        results = self.service.list_current_trade_lifecycle_analytics(
            trader_id=self.trader.id
        )

        self.assertEqual([r["trade_lifecycle_id"] for r in results], [own_id])

    # -- list_trader_performance_summaries() ----------------------------

    def test_several_trader_ids_sharing_the_same_name_are_never_merged(self):
        duplicate_named_trader = create_trader(self.connection, self.source.id, "TC")
        self.connection.commit()
        self._build_closed_long_lifecycle(symbol="IBM")
        self._build_closed_long_lifecycle(symbol="AVGO", trader_id=duplicate_named_trader.id)

        summaries = self.service.list_trader_performance_summaries()

        self.assertEqual(len(summaries), 2)
        trader_ids = [s["trader_id"] for s in summaries]
        self.assertEqual(trader_ids, sorted(trader_ids))
        self.assertEqual(set(trader_ids), {self.trader.id, duplicate_named_trader.id})
        for summary in summaries:
            self.assertEqual(summary["eligible_lifecycle_count"], 1)
            self.assertEqual(summary["winning_count"], 1)

    def test_ordered_by_trader_id_ascending_never_ranked_by_performance(self):
        losing_trader = create_trader(self.connection, self.source.id, "Loser")
        self.connection.commit()
        # Ensure the losing trader has the *lower* id if created second is
        # actually higher - build it explicitly with a losing lifecycle so
        # a performance-based sort would reorder these, then assert it did
        # not.
        self._build_closed_long_lifecycle(symbol="IBM")
        self._make_signal(
            symbol="MU", action="BTO", event_type="ENTRY", price=Decimal("2.00"),
            strike=Decimal("950"), trader_id=losing_trader.id,
        )
        self._make_signal(
            symbol="MU", action="STC", event_type="FULL_EXIT", qualifier="ALL OUT",
            price=Decimal("1.00"), strike=Decimal("950"), trader_id=losing_trader.id,
        )
        self.service.rebuild_all_lifecycles()

        summaries = self.service.list_trader_performance_summaries()

        trader_ids_in_result = [s["trader_id"] for s in summaries]
        self.assertEqual(trader_ids_in_result, sorted(trader_ids_in_result))

    def test_no_trader_id_filter_lists_every_trader_with_current_lifecycles(self):
        other_trader = create_trader(self.connection, self.source.id, "Sarang")
        self.connection.commit()
        self._build_closed_long_lifecycle(symbol="IBM")
        self._build_closed_long_lifecycle(symbol="AVGO", trader_id=other_trader.id)

        summaries = self.service.list_trader_performance_summaries()

        self.assertEqual({s["trader_id"] for s in summaries}, {self.trader.id, other_trader.id})

    def test_reconciliation_invariants_hold_for_real_mixed_data(self):
        self._build_closed_long_lifecycle(symbol="IBM")
        repository.create_trade_lifecycle(
            self.connection, self.trader.id, "NVDA", status="open",
            remaining_fraction="1",
        )
        self.connection.commit()

        summaries = self.service.list_trader_performance_summaries()
        summary = next(s for s in summaries if s["trader_id"] == self.trader.id)

        self.assertEqual(
            summary["total_lifecycle_count"],
            summary["open_count"] + summary["partially_closed_count"]
            + summary["closed_count"] + summary["orphan_count"]
            + summary["unresolved_count"] + summary["invalid_count"],
        )
        self.assertEqual(
            summary["total_lifecycle_count"],
            summary["eligible_lifecycle_count"] + summary["not_scored_count"]
            + summary["snapshot_error_count"],
        )

    def test_summary_and_list_share_one_underlying_computation(self):
        self._build_closed_long_lifecycle(symbol="IBM")
        self._build_closed_long_lifecycle(symbol="AVGO")

        with patch.object(
            TradeService, "_lifecycle_analytics_or_error",
            wraps=TradeService._lifecycle_analytics_or_error,
            autospec=True,
        ) as spy:
            summaries = self.service.list_trader_performance_summaries()

        # Exactly one computation per current lifecycle for this one
        # call - never twice (e.g. once to build a list, once again to
        # summarize it).
        self.assertEqual(spy.call_count, 2)
        self.assertEqual(summaries[0]["eligible_lifecycle_count"], 2)


class TradeServiceControlledCorrectionTests(unittest.TestCase):
    """Covers Milestone 2D.5: update_trade_signal()'s controlled-
    correction mode, selected only by passing expected_current_values."""

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
            expiration="2026-12-18",
            position_size="10 contracts",
        )
        self.connection.commit()

        self.trader_id = trader.id
        self.raw_message_id = raw_message.id
        self.signal_id = signal.id
        self.service = TradeService(self.connection)

    def tearDown(self):
        self.connection.close()
        os.remove(self.db_path)

    def _edit_count(self):
        row = self.connection.execute(
            "SELECT COUNT(*) FROM trade_signal_edits"
        ).fetchone()
        return row[0]

    def _current_values(self):
        return {
            "symbol": "SPY",
            "action": "BTO",
            "option_type": "call",
            "price": Decimal("3.25"),
            "expiration": "2026-12-18",
            "position_size": "10 contracts",
        }

    def _changed_values(self, **overrides):
        values = self._current_values()
        values.update(overrides)
        return values

    def test_legacy_mode_ignores_correction_rules_when_expected_current_values_omitted(self):
        updated = self.service.update_trade_signal(self.signal_id, symbol="QQQ")
        self.assertEqual(updated.symbol, "QQQ")

        # Resubmitting an identical value succeeds in legacy mode - no
        # no-op rule applies when expected_current_values is omitted.
        again = self.service.update_trade_signal(self.signal_id, symbol="QQQ")
        self.assertEqual(again.symbol, "QQQ")
        self.assertEqual(self._edit_count(), 2)

    def test_controlled_mode_rejects_missing_changed_field(self):
        expected = self._current_values()
        incomplete = self._changed_values(symbol="QQQ")
        del incomplete["position_size"]

        with self.assertRaises(ValueError):
            self.service.update_trade_signal(
                self.signal_id, expected_current_values=expected, **incomplete
            )
        self.assertEqual(self._edit_count(), 0)

    def test_controlled_mode_rejects_missing_expected_field(self):
        expected = self._current_values()
        del expected["expiration"]
        changed = self._changed_values(symbol="QQQ")

        with self.assertRaises(ValueError):
            self.service.update_trade_signal(
                self.signal_id, expected_current_values=expected, **changed
            )
        self.assertEqual(self._edit_count(), 0)

    def test_raw_message_id_rejected_in_controlled_mode(self):
        expected = self._current_values()
        changed = self._changed_values(symbol="QQQ")
        del changed["position_size"]
        changed["raw_message_id"] = self.raw_message_id

        with self.assertRaises(ValueError):
            self.service.update_trade_signal(
                self.signal_id, expected_current_values=expected, **changed
            )
        self.assertEqual(self._edit_count(), 0)

    def test_trader_id_rejected_in_controlled_mode(self):
        expected = self._current_values()
        changed = self._changed_values(symbol="QQQ")
        del changed["option_type"]
        changed["trader_id"] = self.trader_id

        with self.assertRaises(ValueError):
            self.service.update_trade_signal(
                self.signal_id, expected_current_values=expected, **changed
            )
        self.assertEqual(self._edit_count(), 0)

    def test_protected_fields_rejected_in_controlled_mode(self):
        expected = self._current_values()
        for protected_field, value in (
            ("id", self.signal_id),
            ("created_at", "2000-01-01 00:00:00"),
            ("updated_at", "2000-01-01 00:00:00"),
        ):
            changed = self._changed_values(symbol="QQQ")
            del changed["position_size"]
            changed[protected_field] = value

            with self.assertRaises(ValueError):
                self.service.update_trade_signal(
                    self.signal_id, expected_current_values=expected, **changed
                )
        self.assertEqual(self._edit_count(), 0)

    def test_typed_decimal_and_none_values_compare_correctly(self):
        expected = self._current_values()
        changed = self._changed_values(
            option_type=None, price=None, expiration=None, position_size=None
        )

        updated = self.service.update_trade_signal(
            self.signal_id, expected_current_values=expected, **changed
        )

        self.assertIsNone(updated.option_type)
        self.assertIsNone(updated.price)
        self.assertIsNone(updated.expiration)
        self.assertIsNone(updated.position_size)

    def test_stale_check_wins_over_noop_check(self):
        wrong_expected = self._changed_values(symbol="WRONG")
        changed = self._current_values()  # identical to actual current values

        with self.assertRaises(StaleTradeSignalError):
            self.service.update_trade_signal(
                self.signal_id, expected_current_values=wrong_expected, **changed
            )
        self.assertEqual(self._edit_count(), 0)

    def test_stale_conflict_creates_no_audit_row_and_does_not_change_updated_at(self):
        before = repository.get_trade_signal_by_id(self.connection, self.signal_id)
        wrong_expected = self._changed_values(symbol="WRONG")
        changed = self._changed_values(symbol="QQQ")

        with self.assertRaises(StaleTradeSignalError):
            self.service.update_trade_signal(
                self.signal_id, expected_current_values=wrong_expected, **changed
            )

        after = repository.get_trade_signal_by_id(self.connection, self.signal_id)
        self.assertEqual(self._edit_count(), 0)
        self.assertEqual(before.updated_at, after.updated_at)

    def test_noop_correction_creates_no_audit_row_and_does_not_change_updated_at(self):
        before = repository.get_trade_signal_by_id(self.connection, self.signal_id)
        expected = self._current_values()
        changed = self._current_values()

        with self.assertRaises(ValueError):
            self.service.update_trade_signal(
                self.signal_id, expected_current_values=expected, **changed
            )

        after = repository.get_trade_signal_by_id(self.connection, self.signal_id)
        self.assertEqual(self._edit_count(), 0)
        self.assertEqual(before.updated_at, after.updated_at)

    def test_missing_signal_raises_not_found_error_in_controlled_mode(self):
        expected = self._current_values()
        changed = self._changed_values(symbol="QQQ")

        with self.assertRaises(TradeSignalNotFoundError):
            self.service.update_trade_signal(
                999999, expected_current_values=expected, **changed
            )
        self.assertEqual(self._edit_count(), 0)

    def test_legacy_missing_signal_raises_plain_value_error_not_the_subclass(self):
        try:
            self.service.update_trade_signal(999999, symbol="QQQ")
            self.fail("expected ValueError")
        except TradeSignalNotFoundError:
            self.fail("legacy mode must not raise TradeSignalNotFoundError")
        except ValueError:
            pass

    def test_successful_controlled_correction_writes_exactly_one_audit_row(self):
        expected = self._current_values()
        changed = self._changed_values(symbol="QQQ")

        self.service.update_trade_signal(
            self.signal_id, expected_current_values=expected, **changed
        )

        self.assertEqual(self._edit_count(), 1)

    def test_price_never_converted_to_float(self):
        expected = self._current_values()
        changed = self._changed_values(price=Decimal("4.10"))

        updated = self.service.update_trade_signal(
            self.signal_id, expected_current_values=expected, **changed
        )

        self.assertIsInstance(updated.price, str)
        self.assertEqual(updated.price, "4.10")

        edits = get_trade_signal_edits(self.connection, self.signal_id)
        snapshot = json.loads(edits[0].previous_values)
        self.assertIsInstance(snapshot["price"], str)
        self.assertEqual(snapshot["price"], "3.25")


class TradeServiceAuditHistoryTests(unittest.TestCase):
    """Covers Milestone 2D.5: list_trade_signal_audit_history()."""

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
            expiration="2026-12-18",
            position_size="10 contracts",
        )
        self.connection.commit()

        self.signal_id = signal.id
        self.service = TradeService(self.connection)

    def tearDown(self):
        self.connection.close()
        os.remove(self.db_path)

    def _current_values(self):
        return {
            "symbol": "SPY",
            "action": "BTO",
            "option_type": "call",
            "price": Decimal("3.25"),
            "expiration": "2026-12-18",
            "position_size": "10 contracts",
        }

    def test_empty_history_for_never_corrected_signal(self):
        self.assertEqual(
            self.service.list_trade_signal_audit_history(self.signal_id), []
        )

    def test_history_contains_exactly_approved_keys(self):
        expected = self._current_values()
        changed = dict(expected, symbol="QQQ")
        self.service.update_trade_signal(
            self.signal_id, expected_current_values=expected, **changed
        )

        history = self.service.list_trade_signal_audit_history(self.signal_id)

        self.assertEqual(len(history), 1)
        self.assertEqual(
            set(history[0]),
            {
                "id",
                "edited_at",
                "symbol",
                "action",
                "option_type",
                "price",
                "expiration",
                "position_size",
            },
        )
        self.assertEqual(history[0]["symbol"], "SPY")
        self.assertIsInstance(history[0]["price"], str)
        self.assertEqual(history[0]["price"], "3.25")

    def test_history_returned_newest_first(self):
        expected1 = self._current_values()
        self.service.update_trade_signal(
            self.signal_id, expected_current_values=expected1, **dict(expected1, symbol="QQQ")
        )
        expected2 = dict(expected1, symbol="QQQ")
        self.service.update_trade_signal(
            self.signal_id, expected_current_values=expected2, **dict(expected2, symbol="IWM")
        )

        history = self.service.list_trade_signal_audit_history(self.signal_id)

        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["symbol"], "QQQ")
        self.assertEqual(history[1]["symbol"], "SPY")
        self.assertGreater(history[0]["id"], history[1]["id"])

    def test_malformed_audit_json_raises_audit_history_error(self):
        self.connection.execute(
            "INSERT INTO trade_signal_edits (trade_signal_id, previous_values) "
            "VALUES (?, ?)",
            (self.signal_id, "not valid json{"),
        )
        self.connection.commit()

        with self.assertRaises(AuditHistoryError):
            self.service.list_trade_signal_audit_history(self.signal_id)

    def test_non_dict_audit_json_raises_audit_history_error(self):
        self.connection.execute(
            "INSERT INTO trade_signal_edits (trade_signal_id, previous_values) "
            "VALUES (?, ?)",
            (self.signal_id, json.dumps([1, 2, 3])),
        )
        self.connection.commit()

        with self.assertRaises(AuditHistoryError):
            self.service.list_trade_signal_audit_history(self.signal_id)


# =============================================================================
# Recovery Milestone R5
# =============================================================================


class _R5ServiceTestCase(unittest.TestCase):
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
                "import_batches", "raw_messages", "message_extractions",
                "trade_signals", "traders",
            )
        }


class R5ResultDataclassFieldTests(unittest.TestCase):
    """Item 1: exact fields/defaults of all five result dataclasses."""

    def test_message_ingest_outcome_fields_and_defaults(self):
        names = [f.name for f in fields(MessageIngestOutcome)]
        self.assertEqual(
            names,
            [
                "sequence_in_batch", "outcome", "channel_id", "raw_message_id",
                "external_id", "parse_status", "trade_signal_ids",
                "ambiguity_flags", "content_differs",
            ],
        )
        outcome = MessageIngestOutcome(
            sequence_in_batch=1, outcome="stored", channel_id=1, raw_message_id=1,
            external_id="ext-1", parse_status="parsed",
        )
        self.assertEqual(outcome.trade_signal_ids, [])
        self.assertEqual(outcome.ambiguity_flags, [])
        self.assertIsNone(outcome.content_differs)

    def test_message_ingest_outcome_default_lists_are_independent_per_instance(self):
        first = MessageIngestOutcome(1, "stored", 1, 1, "e", "parsed")
        second = MessageIngestOutcome(2, "stored", 1, 2, "f", "parsed")
        first.trade_signal_ids.append(99)
        self.assertEqual(second.trade_signal_ids, [])

    def test_message_ingest_outcome_is_frozen(self):
        outcome = MessageIngestOutcome(1, "stored", 1, 1, "e", "parsed")
        with self.assertRaises(Exception):
            outcome.outcome = "duplicate"

    def test_batch_ingest_result_fields_and_defaults(self):
        names = [f.name for f in fields(BatchIngestResult)]
        self.assertEqual(
            names,
            [
                "import_batch_id", "channel_id", "total_segmented", "stored_count",
                "duplicate_count", "unrecognized_count", "failed_count", "messages",
            ],
        )
        result = BatchIngestResult(
            import_batch_id=None, channel_id=1, total_segmented=0, stored_count=0,
            duplicate_count=0, unrecognized_count=0, failed_count=0,
        )
        self.assertEqual(result.messages, [])

    def test_reprocess_outcome_fields_and_defaults(self):
        names = [f.name for f in fields(ReprocessOutcome)]
        self.assertEqual(
            names,
            [
                "raw_message_id", "previous_extraction_id", "new_extraction_id",
                "parse_status", "new_trade_signal_ids", "ambiguity_flags",
            ],
        )
        outcome = ReprocessOutcome(1, None, 2, "parsed")
        self.assertEqual(outcome.new_trade_signal_ids, [])
        self.assertEqual(outcome.ambiguity_flags, [])

    def test_reprocess_batch_result_fields_and_defaults(self):
        names = [f.name for f in fields(ReprocessBatchResult)]
        self.assertEqual(names, ["import_batch_id", "outcomes"])
        result = ReprocessBatchResult(import_batch_id=1)
        self.assertEqual(result.outcomes, [])

    def test_channel_checkpoint_fields(self):
        names = [f.name for f in fields(ChannelCheckpoint)]
        self.assertEqual(
            names,
            [
                "channel_id", "channel_external_id", "channel_name",
                "latest_received_at", "latest_received_raw_message_id",
                "latest_received_external_id", "last_ingested_raw_message_id",
                "last_ingested_external_id", "last_ingested_at",
                "last_import_batch_id",
            ],
        )

    def test_message_ingest_outcome_precise_list_type_annotations(self):
        hints = get_type_hints(MessageIngestOutcome)
        self.assertEqual(hints["trade_signal_ids"], list[int])
        self.assertEqual(hints["ambiguity_flags"], list[str])

    def test_batch_ingest_result_messages_precise_type_annotation(self):
        hints = get_type_hints(BatchIngestResult)
        self.assertEqual(hints["messages"], list[MessageIngestOutcome])

    def test_reprocess_outcome_precise_list_type_annotations(self):
        hints = get_type_hints(ReprocessOutcome)
        self.assertEqual(hints["new_trade_signal_ids"], list[int])
        self.assertEqual(hints["ambiguity_flags"], list[str])

    def test_reprocess_batch_result_outcomes_precise_type_annotation(self):
        hints = get_type_hints(ReprocessBatchResult)
        self.assertEqual(hints["outcomes"], list[ReprocessOutcome])


class SyntheticIdContractTests(_R5ServiceTestCase):
    """Item 2: approved synthetic-ID output."""

    def test_approved_synthetic_id_format(self):
        synthetic_id_input = "chan\x1ftrader\x1ftimestamp\x1fbody\x1f0"
        expected = "synthetic:" + hashlib.sha256(
            synthetic_id_input.encode("utf-8")
        ).hexdigest()

        outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw="alice",
            raw_text="hello", cleaned_text="hello",
            synthetic_id_input=synthetic_id_input,
            reference_date="2026-07-24", timezone="UTC",
        )

        self.assertEqual(outcome.external_id, expected)
        self.assertTrue(expected.startswith("synthetic:"))
        digest = expected[len("synthetic:") :]
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, digest.lower())
        int(digest, 16)  # raises ValueError if not valid hexadecimal

    def test_external_id_and_synthetic_id_input_mutually_exclusive(self):
        with self.assertRaises(ValueError):
            self.service.ingest_channel_message(
                source_name="discord", channel_external_id=None, trader_raw="alice",
                raw_text="hello", cleaned_text="hello",
                external_id="real-1", synthetic_id_input="s1",
                reference_date="2026-07-24", timezone="UTC",
            )

    def test_neither_external_id_nor_synthetic_id_input_raises(self):
        with self.assertRaises(ValueError):
            self.service.ingest_channel_message(
                source_name="discord", channel_external_id=None, trader_raw="alice",
                raw_text="hello", cleaned_text="hello",
                reference_date="2026-07-24", timezone="UTC",
            )


class ResolveExternalIdValidationTests(_R5ServiceTestCase):
    """Item 5: _resolve_external_id() must reject a blank real external_id
    or a blank synthetic_id_input, not merely a None one - a nonblank
    string is required whenever either argument is supplied."""

    def _kwargs(self, **overrides):
        kwargs = dict(
            source_name="discord", channel_external_id=None, trader_raw="alice",
            raw_text="hi", cleaned_text="hi",
            reference_date="2026-07-24", timezone="UTC",
        )
        kwargs.update(overrides)
        return kwargs

    def test_empty_external_id_rejected(self):
        with self.assertRaises(ValueError):
            self.service.ingest_channel_message(**self._kwargs(external_id=""))
        self.assertEqual(self._counts()["raw_messages"], 0)

    def test_whitespace_only_external_id_rejected(self):
        with self.assertRaises(ValueError):
            self.service.ingest_channel_message(**self._kwargs(external_id="   "))
        self.assertEqual(self._counts()["raw_messages"], 0)

    def test_empty_synthetic_id_input_rejected(self):
        with self.assertRaises(ValueError):
            self.service.ingest_channel_message(**self._kwargs(synthetic_id_input=""))
        self.assertEqual(self._counts()["raw_messages"], 0)

    def test_whitespace_only_synthetic_id_input_rejected(self):
        with self.assertRaises(ValueError):
            self.service.ingest_channel_message(**self._kwargs(synthetic_id_input="   "))
        self.assertEqual(self._counts()["raw_messages"], 0)

    def test_direct_empty_external_id_raises(self):
        with self.assertRaises(ValueError):
            _resolve_external_id("", None)

    def test_direct_whitespace_only_external_id_raises(self):
        with self.assertRaises(ValueError):
            _resolve_external_id("   ", None)

    def test_direct_empty_synthetic_id_input_raises(self):
        with self.assertRaises(ValueError):
            _resolve_external_id(None, "")

    def test_direct_whitespace_only_synthetic_id_input_raises(self):
        with self.assertRaises(ValueError):
            _resolve_external_id(None, "   ")

    def test_valid_external_id_not_stripped_or_mutated(self):
        outcome = self.service.ingest_channel_message(
            **self._kwargs(external_id="  keep-exact  ")
        )
        self.assertEqual(outcome.external_id, "  keep-exact  ")


class IngestChannelMessageValidationTests(_R5ServiceTestCase):
    def test_blank_source_name_rejected(self):
        with self.assertRaises(ValueError):
            self.service.ingest_channel_message(
                source_name="   ", channel_external_id=None, trader_raw="alice",
                raw_text="hi", cleaned_text="hi", synthetic_id_input="s1",
                reference_date="2026-07-24", timezone="UTC",
            )
        self.assertEqual(self._counts()["raw_messages"], 0)

    def test_blank_raw_text_rejected(self):
        with self.assertRaises(ValueError):
            self.service.ingest_channel_message(
                source_name="discord", channel_external_id=None, trader_raw="alice",
                raw_text="   ", cleaned_text="hi", synthetic_id_input="s1",
                reference_date="2026-07-24", timezone="UTC",
            )
        self.assertEqual(self._counts()["raw_messages"], 0)

    def test_blank_cleaned_text_allowed_and_persists_as_unrecognized(self):
        outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw="alice",
            raw_text="hi there", cleaned_text="", synthetic_id_input="s1",
            reference_date="2026-07-24", timezone="UTC",
        )
        self.assertEqual(outcome.outcome, "stored")
        self.assertEqual(outcome.parse_status, "unrecognized")
        self.assertEqual(outcome.trade_signal_ids, [])

    def test_invalid_reference_date_rejected(self):
        with self.assertRaises(ValueError):
            self.service.ingest_channel_message(
                source_name="discord", channel_external_id=None, trader_raw="alice",
                raw_text="hi", cleaned_text="hi", synthetic_id_input="s1",
                reference_date="07/24/2026", timezone="UTC",
            )

    def test_invalid_timezone_rejected(self):
        with self.assertRaises(ValueError):
            self.service.ingest_channel_message(
                source_name="discord", channel_external_id=None, trader_raw="alice",
                raw_text="hi", cleaned_text="hi", synthetic_id_input="s1",
                reference_date="2026-07-24", timezone="Not/AZone",
            )


class PublicPrivateApiBoundaryTests(_R5ServiceTestCase):
    def test_public_signature_excludes_batch_linkage_and_internal_ids(self):
        import inspect

        signature = inspect.signature(TradeService.ingest_channel_message)
        forbidden = {"import_batch_id", "sequence_in_batch", "source_id", "channel_id"}
        self.assertEqual(forbidden & set(signature.parameters), set())

    def test_private_helper_rejects_mismatched_batch_linkage(self):
        source = repository.get_or_create_source(self.connection, "discord")
        channel = repository.get_or_create_unspecified_channel(self.connection, source.id)
        self.connection.commit()

        with self.assertRaises(ValueError):
            self.service._ingest_channel_message_no_commit(
                source_id=source.id, channel_id=channel.id, source_name="discord",
                trader_raw="alice", external_trader_id=None,
                raw_text="hi", cleaned_text="hi", resolved_external_id="ext-1",
                native_received_at=None, footer_timestamp_raw=None,
                footer_timestamp_kind=None, reference_date="2026-07-24", timezone="UTC",
                import_batch_id=1, sequence_in_batch=None,
                header_timestamp_raw=None, channel_tags=None,
                adapter_ambiguity_flags=None, metadata_extra=None,
            )

    def test_private_helper_rejects_nonexistent_import_batch(self):
        source = repository.get_or_create_source(self.connection, "discord")
        channel = repository.get_or_create_unspecified_channel(self.connection, source.id)
        self.connection.commit()

        with self.assertRaises(ValueError):
            self.service._ingest_channel_message_no_commit(
                source_id=source.id, channel_id=channel.id, source_name="discord",
                trader_raw="alice", external_trader_id=None,
                raw_text="hi", cleaned_text="hi", resolved_external_id="ext-1",
                native_received_at=None, footer_timestamp_raw=None,
                footer_timestamp_kind=None, reference_date="2026-07-24", timezone="UTC",
                import_batch_id=999999, sequence_in_batch=1,
                header_timestamp_raw=None, channel_tags=None,
                adapter_ambiguity_flags=None, metadata_extra=None,
            )

    def test_private_helper_rejects_batch_from_different_source(self):
        source_a = repository.get_or_create_source(self.connection, "discord")
        source_b = repository.get_or_create_source(self.connection, "telegram")
        channel = repository.get_or_create_unspecified_channel(self.connection, source_a.id)
        self.connection.commit()
        batch = repository.create_import_batch(
            self.connection, source_b.id, reference_date="2026-07-24", timezone="UTC"
        )
        self.connection.commit()

        with self.assertRaises(ValueError):
            self.service._ingest_channel_message_no_commit(
                source_id=source_a.id, channel_id=channel.id, source_name="discord",
                trader_raw="alice", external_trader_id=None,
                raw_text="hi", cleaned_text="hi", resolved_external_id="ext-1",
                native_received_at=None, footer_timestamp_raw=None,
                footer_timestamp_kind=None, reference_date="2026-07-24", timezone="UTC",
                import_batch_id=batch.id, sequence_in_batch=1,
                header_timestamp_raw=None, channel_tags=None,
                adapter_ambiguity_flags=None, metadata_extra=None,
            )

    def test_private_helper_rejects_non_positive_sequence_in_batch(self):
        source = repository.get_or_create_source(self.connection, "discord")
        channel = repository.get_or_create_unspecified_channel(self.connection, source.id)
        self.connection.commit()
        batch = repository.create_import_batch(
            self.connection, source.id, reference_date="2026-07-24", timezone="UTC"
        )
        self.connection.commit()

        with self.assertRaises(ValueError):
            self.service._ingest_channel_message_no_commit(
                source_id=source.id, channel_id=channel.id, source_name="discord",
                trader_raw="alice", external_trader_id=None,
                raw_text="hi", cleaned_text="hi", resolved_external_id="ext-1",
                native_received_at=None, footer_timestamp_raw=None,
                footer_timestamp_kind=None, reference_date="2026-07-24", timezone="UTC",
                import_batch_id=batch.id, sequence_in_batch=0,
                header_timestamp_raw=None, channel_tags=None,
                adapter_ambiguity_flags=None, metadata_extra=None,
            )


class TraderIdentityClassificationTests(_R5ServiceTestCase):
    def test_missing_trader_persists_without_signal(self):
        outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw=None,
            raw_text="BOUGHT AVGO 07/24 380P $1.14",
            cleaned_text="BOUGHT AVGO 07/24 380P $1.14",
            synthetic_id_input="s-missing",
            reference_date="2026-07-24", timezone="UTC",
        )
        self.assertEqual(outcome.outcome, "stored")
        self.assertEqual(outcome.trade_signal_ids, [])
        self.assertIn(AMBIGUITY_FLAG_TRADER_IDENTITY_MISSING, outcome.ambiguity_flags)

        raw_message = get_raw_message_by_id(self.connection, outcome.raw_message_id)
        self.assertIsNotNone(raw_message)
        extraction = get_current_extraction(self.connection, outcome.raw_message_id)
        self.assertIsNotNone(extraction)

    def test_blank_trader_raw_treated_as_missing(self):
        outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw="   ",
            raw_text="hi", cleaned_text="hi", synthetic_id_input="s-blank",
            reference_date="2026-07-24", timezone="UTC",
        )
        self.assertIn(AMBIGUITY_FLAG_TRADER_IDENTITY_MISSING, outcome.ambiguity_flags)

    def test_ambiguous_trader_persists_without_guessing(self):
        source = repository.get_or_create_source(self.connection, "discord")
        create_trader(self.connection, source.id, "Matae")
        create_trader(self.connection, source.id, "matae")
        self.connection.commit()

        outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw="MATAE",
            raw_text="BOUGHT TSLA 7/24 312.5P $1.70",
            cleaned_text="BOUGHT TSLA 7/24 312.5P $1.70",
            synthetic_id_input="s-ambiguous",
            reference_date="2026-07-24", timezone="UTC",
        )
        self.assertEqual(outcome.trade_signal_ids, [])
        self.assertIn(AMBIGUITY_FLAG_TRADER_IDENTITY_AMBIGUOUS, outcome.ambiguity_flags)

        raw_message = get_raw_message_by_id(self.connection, outcome.raw_message_id)
        self.assertIsNotNone(raw_message)
        extraction = get_current_extraction(self.connection, outcome.raw_message_id)
        self.assertIsNotNone(extraction)

    def test_exactly_one_canonical_match_reused(self):
        source = repository.get_or_create_source(self.connection, "discord")
        existing = create_trader(self.connection, source.id, "Bdorts")
        self.connection.commit()

        outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw="bdorts",
            raw_text="BOUGHT AVGO 07/24 380P $1.14",
            cleaned_text="BOUGHT AVGO 07/24 380P $1.14",
            synthetic_id_input="s-reuse",
            reference_date="2026-07-24", timezone="UTC",
        )
        self.assertEqual(len(outcome.trade_signal_ids), 1)
        signal = repository.get_trade_signal_by_id(self.connection, outcome.trade_signal_ids[0])
        self.assertEqual(signal.trader_id, existing.id)

    def test_zero_canonical_matches_creates_trader_with_original_casing(self):
        outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw="Sarang",
            raw_text="BOUGHT QQQ 07/24 690C $1.28",
            cleaned_text="BOUGHT QQQ 07/24 690C $1.28",
            synthetic_id_input="s-create",
            reference_date="2026-07-24", timezone="UTC",
        )
        self.assertEqual(len(outcome.trade_signal_ids), 1)
        source = repository.get_or_create_source(self.connection, "discord")
        matches = get_traders_by_canonical_name(self.connection, source.id, "sarang")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].name, "Sarang")

    def test_external_trader_id_resolved_first(self):
        source = repository.get_or_create_source(self.connection, "discord")
        existing = create_trader(self.connection, source.id, "TC", "ext-tc-1")
        self.connection.commit()

        outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None,
            trader_raw="a different display name entirely",
            external_trader_id="ext-tc-1",
            raw_text="BOUGHT NVDA 07/24 207.5C $1.17",
            cleaned_text="BOUGHT NVDA 07/24 207.5C $1.17",
            synthetic_id_input="s-ext",
            reference_date="2026-07-24", timezone="UTC",
        )
        signal = repository.get_trade_signal_by_id(self.connection, outcome.trade_signal_ids[0])
        self.assertEqual(signal.trader_id, existing.id)

    def test_no_trader_creation_for_duplicate_message(self):
        self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw="brand-new-trader",
            raw_text="hi", cleaned_text="hi", synthetic_id_input="s-dup-trader",
            reference_date="2026-07-24", timezone="UTC",
        )
        trader_count_after_first = self._counts()["traders"]

        second = self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw="brand-new-trader",
            raw_text="hi again", cleaned_text="hi again", synthetic_id_input="s-dup-trader",
            reference_date="2026-07-24", timezone="UTC",
        )
        trader_count_after_second = self._counts()["traders"]

        self.assertEqual(second.outcome, "duplicate")
        self.assertEqual(trader_count_after_first, trader_count_after_second)

    def test_external_trader_creation_deferred_until_after_duplicate_detection(self):
        self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw="alice",
            external_trader_id="ext-defer-1",
            raw_text="hi", cleaned_text="hi", synthetic_id_input="s-defer",
            reference_date="2026-07-24", timezone="UTC",
        )
        self.assertEqual(self._counts()["traders"], 1)

        # Re-ingesting the exact same message (same synthetic id) with a
        # different, still-unseen external_trader_id must not create a
        # second trader row, since the message is a duplicate.
        outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw="someone-else",
            external_trader_id="ext-defer-2",
            raw_text="hi", cleaned_text="hi", synthetic_id_input="s-defer",
            reference_date="2026-07-24", timezone="UTC",
        )

        self.assertEqual(outcome.outcome, "duplicate")
        self.assertEqual(self._counts()["traders"], 1)


class SignalCreationGateTests(_R5ServiceTestCase):
    def test_fully_parsed_with_unresolved_trader_creates_no_signal(self):
        outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw=None,
            raw_text="BOUGHT SPY 450C $3.25", cleaned_text="BOUGHT SPY 450C $3.25",
            synthetic_id_input="s-gate-1",
            reference_date="2026-07-24", timezone="UTC",
        )
        self.assertEqual(outcome.parse_status, "parsed")
        self.assertEqual(outcome.trade_signal_ids, [])

    def test_partially_parsed_missing_symbol_creates_no_signal(self):
        outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw="alice",
            raw_text="BOUGHT 450C $3.25", cleaned_text="BOUGHT 450C $3.25",
            synthetic_id_input="s-gate-2",
            reference_date="2026-07-24", timezone="UTC",
        )
        self.assertEqual(outcome.parse_status, "partially_parsed")
        self.assertEqual(outcome.trade_signal_ids, [])

    def test_partially_parsed_missing_price_with_resolved_trader_creates_signal(self):
        outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw="alice",
            raw_text="BOUGHT SPY 450C", cleaned_text="BOUGHT SPY 450C",
            synthetic_id_input="s-gate-3",
            reference_date="2026-07-24", timezone="UTC",
        )
        self.assertEqual(outcome.parse_status, "partially_parsed")
        self.assertEqual(len(outcome.trade_signal_ids), 1)

    def test_all_three_present_creates_signal(self):
        outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw="alice",
            raw_text="BOUGHT SPY 450C $3.25", cleaned_text="BOUGHT SPY 450C $3.25",
            synthetic_id_input="s-gate-4",
            reference_date="2026-07-24", timezone="UTC",
        )
        self.assertEqual(len(outcome.trade_signal_ids), 1)

    def test_unrecognized_creates_no_signal(self):
        outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw="alice",
            raw_text="just chatting", cleaned_text="just chatting",
            synthetic_id_input="s-gate-5",
            reference_date="2026-07-24", timezone="UTC",
        )
        self.assertEqual(outcome.parse_status, "unrecognized")
        self.assertEqual(outcome.trade_signal_ids, [])


class MetadataProvenanceTests(_R5ServiceTestCase):
    def _full_counts(self):
        counts = self._counts()
        counts["sources"] = self.connection.execute(
            "SELECT COUNT(*) FROM sources"
        ).fetchone()[0]
        counts["channels"] = self.connection.execute(
            "SELECT COUNT(*) FROM channels"
        ).fetchone()[0]
        return counts

    def test_reserved_key_rejected_in_metadata_extra(self):
        with self.assertRaises(ValueError):
            self.service.ingest_channel_message(
                source_name="discord", channel_external_id=None, trader_raw="alice",
                raw_text="hi", cleaned_text="hi", synthetic_id_input="s-meta-1",
                reference_date="2026-07-24", timezone="UTC",
                metadata_extra={"_r5_provenance": {"anything": True}},
            )
        self.assertEqual(self._counts()["raw_messages"], 0)

    def test_reserved_key_rejected_even_when_message_is_already_duplicate(self):
        # Item 1: invalid metadata_extra must never be silently ignored
        # just because the message turns out to be a duplicate - the
        # validation must run before the duplicate lookup, not after it.
        first = self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw="alice",
            raw_text="hi", cleaned_text="hi", synthetic_id_input="s-meta-dup-race",
            reference_date="2026-07-24", timezone="UTC",
        )
        original = get_raw_message_by_id(self.connection, first.raw_message_id)
        counts_before = self._full_counts()

        with self.assertRaises(ValueError):
            self.service.ingest_channel_message(
                source_name="discord", channel_external_id=None, trader_raw="alice",
                raw_text="hi", cleaned_text="hi", synthetic_id_input="s-meta-dup-race",
                reference_date="2026-07-24", timezone="UTC",
                metadata_extra={"_r5_provenance": {"invalid": True}},
            )

        self.assertEqual(self._full_counts(), counts_before)
        after = get_raw_message_by_id(self.connection, first.raw_message_id)
        self.assertEqual(after, original)

    def test_non_dict_metadata_extra_rejected(self):
        counts_before = self._full_counts()

        with self.assertRaises(ValueError):
            self.service.ingest_channel_message(
                source_name="discord", channel_external_id=None, trader_raw="alice",
                raw_text="hi", cleaned_text="hi", synthetic_id_input="s-meta-nondict",
                reference_date="2026-07-24", timezone="UTC",
                metadata_extra=["not", "a", "dict"],
            )

        self.assertEqual(self._full_counts(), counts_before)

    def test_metadata_extra_merges_alongside_reserved_block(self):
        outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw="alice",
            raw_text="hi", cleaned_text="hi", synthetic_id_input="s-meta-2",
            reference_date="2026-07-24", timezone="UTC",
            metadata_extra={"custom_key": "custom_value"},
        )
        raw_message = get_raw_message_by_id(self.connection, outcome.raw_message_id)
        self.assertEqual(raw_message.metadata["custom_key"], "custom_value")
        self.assertIn("_r5_provenance", raw_message.metadata)

    def test_provenance_contains_required_fields(self):
        outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw="alice",
            external_trader_id="ext-prov-1",
            raw_text="BOUGHT SPY 450C $3.25", cleaned_text="BOUGHT SPY 450C $3.25",
            synthetic_id_input="s-meta-3",
            footer_timestamp_raw="Today at 04:30 م", footer_timestamp_kind="relative_today",
            header_timestamp_raw="04:30 م",
            channel_tags=["analyst-tc"],
            adapter_ambiguity_flags=["missing_footer"],
            reference_date="2026-07-24", timezone="Asia/Riyadh",
        )
        raw_message = get_raw_message_by_id(self.connection, outcome.raw_message_id)
        provenance = raw_message.metadata["_r5_provenance"]

        self.assertEqual(provenance["cleaned_text"], "BOUGHT SPY 450C $3.25")
        self.assertEqual(provenance["reference_date"], "2026-07-24")
        self.assertEqual(provenance["timezone"], "Asia/Riyadh")
        self.assertEqual(provenance["footer_timestamp_raw"], "Today at 04:30 م")
        self.assertEqual(provenance["footer_timestamp_kind"], "relative_today")
        self.assertEqual(provenance["synthetic_id_input"], "s-meta-3")
        self.assertEqual(provenance["trader_raw"], "alice")
        self.assertEqual(provenance["external_trader_id"], "ext-prov-1")
        self.assertIsNotNone(provenance["resolved_trader_id"])
        self.assertEqual(provenance["header_timestamp_raw"], "04:30 م")
        self.assertEqual(provenance["channel_tags"], ["analyst-tc"])
        self.assertEqual(provenance["adapter_ambiguity_flags"], ["missing_footer"])


class TimestampPrecedenceAndCanonicalizationTests(_R5ServiceTestCase):
    def test_native_timestamp_takes_precedence_over_footer(self):
        outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw="alice",
            raw_text="hi", cleaned_text="hi", synthetic_id_input="s-ts-1",
            reference_date="2026-07-24", timezone="Asia/Riyadh",
            native_received_at="2026-07-24T20:30:00-04:00",
            footer_timestamp_raw="Today at 04:30 م", footer_timestamp_kind="relative_today",
        )
        raw_message = get_raw_message_by_id(self.connection, outcome.raw_message_id)
        self.assertEqual(raw_message.received_at, "2026-07-25T00:30:00.000000+00:00")
        self.assertNotIn(AMBIGUITY_FLAG_INVALID_NATIVE_TIMESTAMP, outcome.ambiguity_flags)

    def test_invalid_native_timestamp_falls_back_to_footer(self):
        outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw="alice",
            raw_text="hi", cleaned_text="hi", synthetic_id_input="s-ts-2",
            reference_date="2026-07-24", timezone="America/New_York",
            native_received_at="not-a-timestamp",
            footer_timestamp_raw="Today at 04:30 PM", footer_timestamp_kind="relative_today",
        )
        raw_message = get_raw_message_by_id(self.connection, outcome.raw_message_id)
        self.assertIsNotNone(raw_message.received_at)
        self.assertIn(AMBIGUITY_FLAG_INVALID_NATIVE_TIMESTAMP, outcome.ambiguity_flags)

    def test_naive_native_timestamp_rejected_not_guessed(self):
        outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw="alice",
            raw_text="hi", cleaned_text="hi", synthetic_id_input="s-ts-3",
            reference_date="2026-07-24", timezone="UTC",
            native_received_at="2026-07-24T20:30:00",
        )
        self.assertIn(AMBIGUITY_FLAG_INVALID_NATIVE_TIMESTAMP, outcome.ambiguity_flags)
        raw_message = get_raw_message_by_id(self.connection, outcome.raw_message_id)
        self.assertIsNone(raw_message.received_at)

    def test_no_timestamp_info_at_all_is_not_flagged(self):
        outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw="alice",
            raw_text="hi", cleaned_text="hi", synthetic_id_input="s-ts-4",
            reference_date="2026-07-24", timezone="UTC",
        )
        self.assertNotIn(AMBIGUITY_FLAG_INVALID_NATIVE_TIMESTAMP, outcome.ambiguity_flags)
        raw_message = get_raw_message_by_id(self.connection, outcome.raw_message_id)
        self.assertIsNone(raw_message.received_at)

    def test_utc_canonicalization_across_different_original_offsets(self):
        first = self.service.ingest_channel_message(
            source_name="discord", channel_external_id="chan-tz", trader_raw="alice",
            raw_text="first", cleaned_text="first", synthetic_id_input="s-ts-5",
            reference_date="2026-07-24", timezone="UTC",
            native_received_at="2026-07-24T12:00:00+00:00",
        )
        second = self.service.ingest_channel_message(
            source_name="discord", channel_external_id="chan-tz", trader_raw="alice",
            raw_text="second", cleaned_text="second", synthetic_id_input="s-ts-6",
            reference_date="2026-07-24", timezone="UTC",
            native_received_at="2026-07-24T08:00:00-04:00",
        )
        rm1 = get_raw_message_by_id(self.connection, first.raw_message_id)
        rm2 = get_raw_message_by_id(self.connection, second.raw_message_id)
        self.assertEqual(rm1.received_at, rm2.received_at)
        self.assertEqual(rm1.received_at, "2026-07-24T12:00:00.000000+00:00")

    def test_footer_timestamp_also_canonicalized_to_utc(self):
        outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw="alice",
            raw_text="hi", cleaned_text="hi", synthetic_id_input="s-ts-footer",
            reference_date="2026-07-24", timezone="Asia/Riyadh",
            footer_timestamp_raw="Today at 04:30 م", footer_timestamp_kind="relative_today",
        )
        raw_message = get_raw_message_by_id(self.connection, outcome.raw_message_id)
        self.assertEqual(raw_message.received_at, "2026-07-24T13:30:00.000000+00:00")

    def test_chronological_checkpoint_orders_correctly_across_offsets(self):
        earlier_in_utc = self.service.ingest_channel_message(
            source_name="discord", channel_external_id="chan-order", trader_raw="alice",
            raw_text="earlier", cleaned_text="earlier", synthetic_id_input="s-ts-7",
            reference_date="2026-07-24", timezone="UTC",
            native_received_at="2026-07-24T23:00:00+05:00",  # 18:00 UTC
        )
        later_in_utc = self.service.ingest_channel_message(
            source_name="discord", channel_external_id="chan-order", trader_raw="alice",
            raw_text="later", cleaned_text="later", synthetic_id_input="s-ts-8",
            reference_date="2026-07-24", timezone="UTC",
            native_received_at="2026-07-24T15:00:00-05:00",  # 20:00 UTC
        )
        checkpoints = self.service.get_channel_checkpoints()
        checkpoint = next(c for c in checkpoints if c.channel_id == later_in_utc.channel_id)
        self.assertEqual(
            checkpoint.latest_received_raw_message_id, later_in_utc.raw_message_id
        )


class IngestBatchIdempotencyServiceTests(_R5ServiceTestCase):
    def test_full_corpus_ingestion(self):
        result = self.service.ingest_batch(
            source_name="discord", reference_date="2026-07-24", timezone="Asia/Riyadh",
            raw_batch_text=CORPUS, channel_external_id="chan-corpus",
        )
        self.assertEqual(result.total_segmented, 68)
        self.assertEqual(result.stored_count, 68)
        self.assertEqual(result.duplicate_count, 0)
        self.assertIsNotNone(result.import_batch_id)

    def test_full_duplicate_reimport_creates_no_import_batches_row(self):
        self.service.ingest_batch(
            source_name="discord", reference_date="2026-07-24", timezone="Asia/Riyadh",
            raw_batch_text=CORPUS, channel_external_id="chan-corpus-2",
        )
        batch_count_after_first = self._counts()["import_batches"]

        result2 = self.service.ingest_batch(
            source_name="discord", reference_date="2026-07-24", timezone="Asia/Riyadh",
            raw_batch_text=CORPUS, channel_external_id="chan-corpus-2",
        )
        batch_count_after_second = self._counts()["import_batches"]

        self.assertIsNone(result2.import_batch_id)
        self.assertEqual(result2.duplicate_count, 68)
        self.assertEqual(result2.stored_count, 0)
        self.assertEqual(batch_count_after_first, batch_count_after_second)

    def test_partial_duplicate_batch_links_only_new_messages(self):
        self.service.ingest_batch(
            source_name="discord", reference_date="2026-07-24", timezone="Asia/Riyadh",
            raw_batch_text=CORPUS, channel_external_id="chan-partial",
        )

        extra_message = (
            "Bdorts\nAPP\n — 09:30 م\n"
            "BOUGHT MSFT 07/24 500C $2.00 [SMALL]\n"
            "Bdorts•Today at 09:30 م\n"
        )
        mixed_batch_text = CORPUS + extra_message

        result = self.service.ingest_batch(
            source_name="discord", reference_date="2026-07-24", timezone="Asia/Riyadh",
            raw_batch_text=mixed_batch_text, channel_external_id="chan-partial",
        )

        self.assertIsNotNone(result.import_batch_id)
        self.assertEqual(result.stored_count, 1)
        self.assertEqual(result.duplicate_count, 68)

        new_message_outcome = next(o for o in result.messages if o.outcome == "stored")
        new_raw_message = get_raw_message_by_id(
            self.connection, new_message_outcome.raw_message_id
        )
        self.assertEqual(new_raw_message.import_batch_id, result.import_batch_id)

        for outcome in (o for o in result.messages if o.outcome == "duplicate"):
            duplicate_raw_message = get_raw_message_by_id(
                self.connection, outcome.raw_message_id
            )
            self.assertNotEqual(duplicate_raw_message.import_batch_id, result.import_batch_id)

    def test_legitimate_identical_messages_remain_separate(self):
        duplicate_pair = (
            "Bdorts\nAPP\n — 04:30 PM\n"
            "BOUGHT AVGO 07/24 380P $1.14 [SMALL]\n"
            "Bdorts•Today at 04:30 PM\n"
            "BOUGHT AVGO 07/24 380P $1.14 [SMALL]\n"
            "Bdorts•Today at 04:30 PM\n"
        )
        result = self.service.ingest_batch(
            source_name="discord", reference_date="2026-07-24", timezone="UTC",
            raw_batch_text=duplicate_pair, channel_external_id="chan-identical",
        )
        self.assertEqual(result.total_segmented, 2)
        self.assertEqual(result.stored_count, 2)
        self.assertEqual(result.duplicate_count, 0)
        self.assertNotEqual(
            result.messages[0].raw_message_id, result.messages[1].raw_message_id
        )
        self.assertNotEqual(result.messages[0].external_id, result.messages[1].external_id)

    def test_empty_batch_validation_zero_writes(self):
        counts_before = self._counts()
        with self.assertRaises(ValueError):
            self.service.ingest_batch(
                source_name="discord", reference_date="2026-07-24", timezone="UTC",
                raw_batch_text="   ",
            )
        self.assertEqual(self._counts(), counts_before)

    def test_invalid_reference_date_zero_writes(self):
        counts_before = self._counts()
        with self.assertRaises(ValueError):
            self.service.ingest_batch(
                source_name="discord", reference_date="not-a-date", timezone="UTC",
                raw_batch_text=CORPUS,
            )
        self.assertEqual(self._counts(), counts_before)

    def test_invalid_timezone_zero_writes(self):
        counts_before = self._counts()
        with self.assertRaises(ValueError):
            self.service.ingest_batch(
                source_name="discord", reference_date="2026-07-24", timezone="Not/AZone",
                raw_batch_text=CORPUS,
            )
        self.assertEqual(self._counts(), counts_before)

    def test_blank_source_name_zero_writes(self):
        counts_before = self._counts()
        with self.assertRaises(ValueError):
            self.service.ingest_batch(
                source_name="   ", reference_date="2026-07-24", timezone="UTC",
                raw_batch_text=CORPUS,
            )
        self.assertEqual(self._counts(), counts_before)


class UniqueConstraintRaceServiceTests(_R5ServiceTestCase):
    """Item 2/4: the confirmed-race duplicate path - create_raw_message()
    raising sqlite3.IntegrityError, followed by a re-query that confirms a
    genuinely already-stored row - must never leave a provisional trader
    (or any other write made after the initial duplicate lookup) behind.
    See TradeService._ingest_channel_message_no_commit's message-level
    SAVEPOINT.

    Each test simulates the race by patching
    database.service.get_raw_message_by_channel_and_external_id so its
    first `skip_count` calls return None (as if the row had not yet been
    observed), then delegate to the real lookup - which finds whatever the
    test actually pre-created once create_raw_message's own UNIQUE
    constraint forces a re-query.
    """

    def _patch_lookup_with_delayed_race(self, skip_count):
        real_lookup = repository.get_raw_message_by_channel_and_external_id
        state = {"calls": 0}

        def fake_lookup(conn, channel_id, external_id):
            state["calls"] += 1
            if state["calls"] <= skip_count:
                return None
            return real_lookup(conn, channel_id, external_id)

        return patch(
            "database.service.get_raw_message_by_channel_and_external_id",
            side_effect=fake_lookup,
        )

    def test_confirmed_race_duplicate_direct_single_message_ingestion(self):
        # trader_raw resolves to an EXISTING trader (via external_trader_id)
        # so classification is "resolved" - isolating the raw_message race
        # itself from any trader-creation concern.
        source = repository.get_or_create_source(self.connection, "discord")
        channel = repository.get_or_create_channel(self.connection, source.id, "chan-race-1")
        repository.create_trader(self.connection, source.id, "alice", "ext-race-1")
        existing = repository.create_raw_message(
            self.connection, source.id, "already here",
            external_id="ext-msg-race-1", channel_id=channel.id,
        )
        self.connection.commit()
        trader_count_before = self._counts()["traders"]

        with self._patch_lookup_with_delayed_race(skip_count=1):
            outcome = self.service.ingest_channel_message(
                source_name="discord", channel_external_id="chan-race-1",
                trader_raw="alice", external_trader_id="ext-race-1",
                raw_text="incoming, different text",
                cleaned_text="incoming, different text",
                external_id="ext-msg-race-1",
                reference_date="2026-07-24", timezone="UTC",
            )

        self.assertEqual(outcome.outcome, "duplicate")
        self.assertEqual(outcome.raw_message_id, existing.id)
        self.assertEqual(self._counts()["traders"], trader_count_before)
        after = repository.get_raw_message_by_id(self.connection, existing.id)
        self.assertEqual(after.raw_text, "already here")
        self.assertEqual(after.content_hash, existing.content_hash)

    def test_confirmed_race_duplicate_with_deferred_trader_creation(self):
        # trader_raw is previously unseen, so classification is
        # "needs_creation" - the provisional trader created inside the
        # savepoint must be rolled back when the race is confirmed.
        source = repository.get_or_create_source(self.connection, "discord")
        channel = repository.get_or_create_channel(self.connection, source.id, "chan-race-2")
        existing = repository.create_raw_message(
            self.connection, source.id, "already here 2",
            external_id="ext-msg-race-2", channel_id=channel.id,
        )
        self.connection.commit()
        trader_count_before = self._counts()["traders"]

        with self._patch_lookup_with_delayed_race(skip_count=1):
            outcome = self.service.ingest_channel_message(
                source_name="discord", channel_external_id="chan-race-2",
                trader_raw="brand-new-racer",
                raw_text="incoming 2", cleaned_text="incoming 2",
                external_id="ext-msg-race-2",
                reference_date="2026-07-24", timezone="UTC",
            )

        self.assertEqual(outcome.outcome, "duplicate")
        self.assertEqual(outcome.raw_message_id, existing.id)
        self.assertEqual(self._counts()["traders"], trader_count_before)

    def test_confirmed_race_duplicate_inside_batch(self):
        source = repository.get_or_create_source(self.connection, "discord")
        channel = repository.get_or_create_channel(self.connection, source.id, "chan-race-batch")
        self.connection.commit()

        message_one = (
            "Bdorts\nAPP\n — 09:30 م\n"
            "BOUGHT MSFT 07/24 500C $2.00 [SMALL]\n"
            "Bdorts•Today at 09:30 م\n"
        )
        message_two = (
            "Bdorts\nAPP\n — 09:31 م\n"
            "BOUGHT NFLX 07/24 600C $3.00 [SMALL]\n"
            "Bdorts•Today at 09:31 م\n"
        )
        batch_text = message_one + message_two
        segmented = segment_discord_batch(batch_text)
        self.assertEqual(len(segmented), 2)
        resolved_id_one = _resolve_external_id(None, segmented[0].synthetic_id_input)

        existing = repository.create_raw_message(
            self.connection, source.id, "already here batch",
            external_id=resolved_id_one, channel_id=channel.id,
        )
        self.connection.commit()

        # Message one's preflight check, its idempotency check inside the
        # private helper, then message two's preflight check and its own
        # idempotency check all need to run before message one's raced
        # re-query - three prior calls to skip.
        with self._patch_lookup_with_delayed_race(skip_count=3):
            result = self.service.ingest_batch(
                source_name="discord", reference_date="2026-07-24", timezone="UTC",
                raw_batch_text=batch_text, channel_external_id="chan-race-batch",
            )

        self.assertIsNotNone(result.import_batch_id)
        self.assertEqual(result.stored_count, 1)
        self.assertEqual(result.duplicate_count, 1)
        duplicate_outcome = next(o for o in result.messages if o.outcome == "duplicate")
        self.assertEqual(duplicate_outcome.raw_message_id, existing.id)
        stored_outcome = next(o for o in result.messages if o.outcome == "stored")
        self.assertNotEqual(stored_outcome.raw_message_id, existing.id)

    def test_race_empty_batch_cleanup(self):
        # Every apparent new message in the batch becomes a confirmed
        # duplicate via the race - the otherwise-orphaned import_batches
        # row must be removed and no provisional trader may remain.
        source = repository.get_or_create_source(self.connection, "discord")
        channel = repository.get_or_create_channel(self.connection, source.id, "chan-race-empty")
        self.connection.commit()

        message_text = (
            "Bdorts\nAPP\n — 09:30 م\n"
            "BOUGHT MSFT 07/24 500C $2.00 [SMALL]\n"
            "Bdorts•Today at 09:30 م\n"
        )
        segmented = segment_discord_batch(message_text)
        resolved_id = _resolve_external_id(None, segmented[0].synthetic_id_input)

        repository.create_raw_message(
            self.connection, source.id, "already here empty",
            external_id=resolved_id, channel_id=channel.id,
        )
        self.connection.commit()
        batch_count_before = self._counts()["import_batches"]
        trader_count_before = self._counts()["traders"]

        with self._patch_lookup_with_delayed_race(skip_count=2):
            result = self.service.ingest_batch(
                source_name="discord", reference_date="2026-07-24", timezone="UTC",
                raw_batch_text=message_text, channel_external_id="chan-race-empty",
            )

        self.assertIsNone(result.import_batch_id)
        self.assertEqual(result.stored_count, 0)
        self.assertEqual(result.duplicate_count, 1)
        self.assertEqual(self._counts()["import_batches"], batch_count_before)
        self.assertEqual(self._counts()["traders"], trader_count_before)

    def test_unrelated_integrity_error_propagates_and_rolls_back_provisional_writes(self):
        counts_before = self._counts()

        with patch(
            "database.service.create_raw_message",
            side_effect=sqlite3.IntegrityError("simulated unrelated constraint violation"),
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                self.service.ingest_channel_message(
                    source_name="discord", channel_external_id="chan-race-unrelated",
                    trader_raw="brand-new-unrelated-trader",
                    raw_text="hi", cleaned_text="hi", synthetic_id_input="s-race-unrelated",
                    reference_date="2026-07-24", timezone="UTC",
                )

        self.assertEqual(self._counts(), counts_before)

    def test_confirmed_race_rolls_back_provisional_writes(self):
        source = repository.get_or_create_source(self.connection, "discord")
        channel = repository.get_or_create_channel(
            self.connection, source.id, "chan-race-provisional"
        )
        repository.create_raw_message(
            self.connection, source.id, "already here provisional",
            external_id="ext-msg-race-provisional", channel_id=channel.id,
        )
        self.connection.commit()
        counts_before = self._counts()

        with self._patch_lookup_with_delayed_race(skip_count=1):
            outcome = self.service.ingest_channel_message(
                source_name="discord", channel_external_id="chan-race-provisional",
                trader_raw="another-unseen-trader",
                raw_text="incoming provisional", cleaned_text="incoming provisional",
                external_id="ext-msg-race-provisional",
                reference_date="2026-07-24", timezone="UTC",
            )

        self.assertEqual(outcome.outcome, "duplicate")
        counts_after = self._counts()
        self.assertEqual(counts_after["traders"], counts_before["traders"])
        self.assertEqual(
            counts_after["message_extractions"], counts_before["message_extractions"]
        )
        self.assertEqual(counts_after["trade_signals"], counts_before["trade_signals"])
        self.assertEqual(counts_after["raw_messages"], counts_before["raw_messages"])

    def test_confirmed_race_preserves_existing_duplicate_row(self):
        source = repository.get_or_create_source(self.connection, "discord")
        channel = repository.get_or_create_channel(
            self.connection, source.id, "chan-race-preserve"
        )
        existing = repository.create_raw_message(
            self.connection, source.id, "original content preserved",
            external_id="ext-msg-race-preserve", channel_id=channel.id,
        )
        self.connection.commit()

        with self._patch_lookup_with_delayed_race(skip_count=1):
            outcome = self.service.ingest_channel_message(
                source_name="discord", channel_external_id="chan-race-preserve",
                trader_raw="alice",
                raw_text="a very different incoming body",
                cleaned_text="a very different incoming body",
                external_id="ext-msg-race-preserve",
                reference_date="2026-07-24", timezone="UTC",
            )

        self.assertEqual(outcome.outcome, "duplicate")
        self.assertEqual(outcome.raw_message_id, existing.id)
        after = repository.get_raw_message_by_id(self.connection, existing.id)
        self.assertEqual(after.raw_text, "original content preserved")
        self.assertEqual(after.content_hash, existing.content_hash)
        self.assertEqual(after.metadata, existing.metadata)


class ReprocessingServiceTests(_R5ServiceTestCase):
    def test_direct_single_message_reprocessing_from_provenance(self):
        outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw="alice",
            raw_text="BOUGHT SPY 450C $3.25", cleaned_text="BOUGHT SPY 450C $3.25",
            synthetic_id_input="s-reproc-1",
            reference_date="2026-07-24", timezone="UTC",
        )
        reprocessed = self.service.reprocess_raw_message(outcome.raw_message_id)

        self.assertEqual(reprocessed.raw_message_id, outcome.raw_message_id)
        self.assertEqual(reprocessed.parse_status, "parsed")
        self.assertEqual(len(reprocessed.new_trade_signal_ids), 1)

        current = get_current_extraction(self.connection, outcome.raw_message_id)
        self.assertEqual(current.id, reprocessed.new_extraction_id)

    def test_reprocessing_supersedes_prior_extraction_and_signal(self):
        outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw="alice",
            raw_text="BOUGHT SPY 450C $3.25", cleaned_text="BOUGHT SPY 450C $3.25",
            synthetic_id_input="s-reproc-2",
            reference_date="2026-07-24", timezone="UTC",
        )
        original_signal_id = outcome.trade_signal_ids[0]

        reprocessed = self.service.reprocess_raw_message(outcome.raw_message_id)

        self.assertNotEqual(reprocessed.new_trade_signal_ids[0], original_signal_id)
        old_signal = repository.get_trade_signal_by_id(self.connection, original_signal_id)
        self.assertIsNotNone(old_signal)
        self.assertEqual(old_signal.symbol, "SPY")

    def test_no_segmentation_rerun_during_reprocessing(self):
        outcome = self.service.ingest_batch(
            source_name="discord", reference_date="2026-07-24", timezone="Asia/Riyadh",
            raw_batch_text=CORPUS, channel_external_id="chan-noresegment",
        )
        raw_message_id = outcome.messages[0].raw_message_id

        with patch(
            "database.service.segment_discord_batch",
            side_effect=AssertionError("segmentation must never rerun during reprocessing"),
        ):
            self.service.reprocess_raw_message(raw_message_id)

    def test_import_batch_reprocessing(self):
        outcome = self.service.ingest_batch(
            source_name="discord", reference_date="2026-07-24", timezone="Asia/Riyadh",
            raw_batch_text=CORPUS, channel_external_id="chan-batchreproc",
        )

        batch_result = self.service.reprocess_import_batch(outcome.import_batch_id)

        self.assertEqual(len(batch_result.outcomes), 68)
        self.assertEqual(batch_result.import_batch_id, outcome.import_batch_id)
        for reprocess_outcome in batch_result.outcomes:
            self.assertEqual(reprocess_outcome.parse_status, "parsed")

    def test_reprocess_nonexistent_raw_message_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.service.reprocess_raw_message(999999)

    def test_reprocess_nonexistent_import_batch_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.service.reprocess_import_batch(999999)

    def test_reprocess_import_batch_with_zero_linked_messages_raises_value_error(self):
        source = repository.get_or_create_source(self.connection, "discord")
        self.connection.commit()
        batch = repository.create_import_batch(
            self.connection, source.id, reference_date="2026-07-24", timezone="UTC"
        )
        self.connection.commit()

        with self.assertRaises(ValueError):
            self.service.reprocess_import_batch(batch.id)

    def test_legacy_manual_entry_message_not_reprocessable(self):
        result = self.service.ingest_message(
            "manual", "alice", "BTO SPY 500c @3.25",
            reference_time="2026-07-13 09:00:00",
            trade_signals=[{"symbol": "SPY", "action": "BTO"}],
        )
        self.connection.commit()

        with self.assertRaises(ReprocessingNotSupportedError):
            self.service.reprocess_raw_message(result["raw_message"].id)

    def test_reprocessing_not_supported_error_is_a_value_error(self):
        self.assertTrue(issubclass(ReprocessingNotSupportedError, ValueError))

    def test_resolved_trader_id_preserved_during_reprocessing(self):
        outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw="alice",
            raw_text="BOUGHT SPY 450C $3.25", cleaned_text="BOUGHT SPY 450C $3.25",
            synthetic_id_input="s-reproc-trader-1",
            reference_date="2026-07-24", timezone="UTC",
        )
        original_signal = repository.get_trade_signal_by_id(
            self.connection, outcome.trade_signal_ids[0]
        )

        with patch.object(
            TradeService,
            "_classify_trader_identity",
            side_effect=AssertionError(
                "must not reclassify when resolved_trader_id is still valid"
            ),
        ):
            reprocessed = self.service.reprocess_raw_message(outcome.raw_message_id)

        new_signal = repository.get_trade_signal_by_id(
            self.connection, reprocessed.new_trade_signal_ids[0]
        )
        self.assertEqual(new_signal.trader_id, original_signal.trader_id)

    def test_unresolved_trader_identity_reevaluated_during_reprocessing(self):
        outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw=None,
            raw_text="BOUGHT SPY 450C $3.25", cleaned_text="BOUGHT SPY 450C $3.25",
            synthetic_id_input="s-reproc-trader-2",
            reference_date="2026-07-24", timezone="UTC",
        )
        self.assertEqual(outcome.trade_signal_ids, [])

        reprocessed = self.service.reprocess_raw_message(outcome.raw_message_id)

        self.assertEqual(reprocessed.new_trade_signal_ids, [])
        self.assertIn(AMBIGUITY_FLAG_TRADER_IDENTITY_MISSING, reprocessed.ambiguity_flags)

    def test_reprocessing_reclassifies_ambiguous_trader(self):
        source = repository.get_or_create_source(self.connection, "discord")
        create_trader(self.connection, source.id, "Dup")
        create_trader(self.connection, source.id, "dup")
        self.connection.commit()

        ambiguous_outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw="DUP",
            raw_text="SOLD SPY 450C $4.00 ALL OUT",
            cleaned_text="SOLD SPY 450C $4.00 ALL OUT",
            synthetic_id_input="s-reproc-trader-4",
            reference_date="2026-07-24", timezone="UTC",
        )
        self.assertEqual(ambiguous_outcome.trade_signal_ids, [])

        reprocessed = self.service.reprocess_raw_message(ambiguous_outcome.raw_message_id)

        self.assertIn(AMBIGUITY_FLAG_TRADER_IDENTITY_AMBIGUOUS, reprocessed.ambiguity_flags)
        self.assertEqual(reprocessed.new_trade_signal_ids, [])

    def test_reprocessing_produces_no_replacement_signal_when_unrecognized(self):
        outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id=None, trader_raw="alice",
            raw_text="just chatting here", cleaned_text="just chatting here",
            synthetic_id_input="s-reproc-empty",
            reference_date="2026-07-24", timezone="UTC",
        )
        self.assertEqual(outcome.trade_signal_ids, [])

        reprocessed = self.service.reprocess_raw_message(outcome.raw_message_id)

        self.assertEqual(reprocessed.parse_status, "unrecognized")
        self.assertEqual(reprocessed.new_trade_signal_ids, [])


class ChannelCheckpointServiceTests(_R5ServiceTestCase):
    def test_no_channels_returns_empty_list(self):
        self.assertEqual(self.service.get_channel_checkpoints(), [])

    def test_out_of_order_historical_import_does_not_move_checkpoint_backward(self):
        newer = self.service.ingest_channel_message(
            source_name="discord", channel_external_id="chan-hist", trader_raw="alice",
            raw_text="newer", cleaned_text="newer", synthetic_id_input="s-hist-newer",
            reference_date="2026-07-24", timezone="UTC",
            native_received_at="2026-07-24T20:00:00+00:00",
        )
        older = self.service.ingest_channel_message(
            source_name="discord", channel_external_id="chan-hist", trader_raw="alice",
            raw_text="older", cleaned_text="older", synthetic_id_input="s-hist-older",
            reference_date="2026-07-20", timezone="UTC",
            native_received_at="2026-07-20T10:00:00+00:00",
        )
        checkpoints = self.service.get_channel_checkpoints()
        checkpoint = next(c for c in checkpoints if c.channel_id == newer.channel_id)

        self.assertEqual(checkpoint.latest_received_raw_message_id, newer.raw_message_id)
        # The ingestion cursor still reflects the most recently INSERTED
        # row - the older-content message pasted second.
        self.assertEqual(checkpoint.last_ingested_raw_message_id, older.raw_message_id)
        self.assertEqual(checkpoint.last_ingested_external_id, older.external_id)

    def test_unresolved_timestamps_report_no_false_chronological_checkpoint(self):
        outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id="chan-unresolved", trader_raw="alice",
            raw_text="no timestamp info", cleaned_text="no timestamp info",
            synthetic_id_input="s-unresolved",
            reference_date="2026-07-24", timezone="UTC",
        )
        checkpoints = self.service.get_channel_checkpoints()
        checkpoint = next(c for c in checkpoints if c.channel_id == outcome.channel_id)

        self.assertIsNone(checkpoint.latest_received_at)
        self.assertIsNone(checkpoint.latest_received_raw_message_id)
        self.assertIsNone(checkpoint.latest_received_external_id)
        self.assertEqual(checkpoint.last_ingested_raw_message_id, outcome.raw_message_id)
        # The ingestion checkpoint's own external id remains available -
        # never None - even though the chronological half is entirely
        # unresolved (None above); it must never be confused with, or
        # used as a substitute for, a resolved Discord time.
        self.assertEqual(checkpoint.last_ingested_external_id, outcome.external_id)
        self.assertIsNotNone(checkpoint.last_ingested_external_id)

    def test_checkpoint_ingestion_external_id_is_synthetic_for_no_real_message_id(self):
        outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id="chan-synth", trader_raw="alice",
            raw_text="hi", cleaned_text="hi", synthetic_id_input="s-synth-checkpoint",
            reference_date="2026-07-24", timezone="UTC",
        )
        checkpoints = self.service.get_channel_checkpoints()
        checkpoint = next(c for c in checkpoints if c.channel_id == outcome.channel_id)

        self.assertTrue(checkpoint.last_ingested_external_id.startswith("synthetic:"))
        self.assertEqual(checkpoint.last_ingested_external_id, outcome.external_id)

    def test_checkpoint_ingestion_external_id_is_real_when_supplied(self):
        outcome = self.service.ingest_channel_message(
            source_name="discord", channel_external_id="chan-real-id", trader_raw="alice",
            raw_text="hi", cleaned_text="hi", external_id="discord-msg-12345",
            reference_date="2026-07-24", timezone="UTC",
        )
        checkpoints = self.service.get_channel_checkpoints()
        checkpoint = next(c for c in checkpoints if c.channel_id == outcome.channel_id)

        self.assertEqual(checkpoint.last_ingested_external_id, "discord-msg-12345")

    def test_duplicate_imports_do_not_change_checkpoint(self):
        self.service.ingest_channel_message(
            source_name="discord", channel_external_id="chan-dupcheckpoint", trader_raw="alice",
            raw_text="hi", cleaned_text="hi", synthetic_id_input="s-dupcheckpoint",
            reference_date="2026-07-24", timezone="UTC",
            native_received_at="2026-07-24T12:00:00+00:00",
        )
        before = self.service.get_channel_checkpoints()

        duplicate = self.service.ingest_channel_message(
            source_name="discord", channel_external_id="chan-dupcheckpoint", trader_raw="alice",
            raw_text="hi", cleaned_text="hi", synthetic_id_input="s-dupcheckpoint",
            reference_date="2026-07-24", timezone="UTC",
            native_received_at="2026-07-24T12:00:00+00:00",
        )
        after = self.service.get_channel_checkpoints()

        self.assertEqual(duplicate.outcome, "duplicate")
        self.assertEqual(before, after)


class LegacyIngestMessagePathUncontaminatedTests(_R5ServiceTestCase):
    """Item 17 and the "keep ingest_message() unchanged" requirement."""

    def test_ingest_message_creates_no_message_extraction(self):
        result = self.service.ingest_message(
            "manual", "alice", "BTO SPY 500c @3.25",
            reference_time="2026-07-13 09:00:00",
            trade_signals=[{"symbol": "SPY", "action": "BTO"}],
        )
        self.connection.commit()

        count = self.connection.execute(
            "SELECT COUNT(*) FROM message_extractions WHERE raw_message_id = ?",
            (result["raw_message"].id,),
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_ingest_message_signature_unchanged(self):
        import inspect

        signature = inspect.signature(TradeService.ingest_message)
        self.assertEqual(
            list(signature.parameters),
            [
                "self", "source_name", "trader_name", "raw_text", "reference_time",
                "external_trader_id", "external_message_id", "metadata", "received_at",
                "trade_signals",
            ],
        )

    def test_legacy_signal_visible_via_review_alongside_r5_signal(self):
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
            synthetic_id_input="s-legacy-mix",
            reference_date="2026-07-24", timezone="UTC",
        )

        results = self.service.list_trade_signals_for_review()
        symbols = {row["symbol"] for row in results}
        self.assertIn("SPY", symbols)
        self.assertIn("AAPL", symbols)


class R5TransactionContextManagerTests(unittest.TestCase):
    """Items 24-29: the safe transaction context manager contract."""

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = path
        self.config = DatabaseConfig(db_path=path)
        initialize_database(self.config)
        self.connection = _open_failing_connection(self.config)
        self.service = TradeService(self.connection)

    def tearDown(self):
        self.connection.close()
        os.remove(self.db_path)

    def _ingest_kwargs(self, synthetic_id_input="s-tx"):
        return dict(
            source_name="discord", channel_external_id=None, trader_raw="alice",
            raw_text="hi", cleaned_text="hi", synthetic_id_input=synthetic_id_input,
            reference_date="2026-07-24", timezone="UTC",
        )

    def test_rejects_entry_when_in_transaction_already_true(self):
        self.connection.execute("INSERT INTO sources (name) VALUES ('scratch')")
        self.assertTrue(self.connection.in_transaction)

        other_connection = get_connection(self.config)
        try:
            with self.assertRaises(RuntimeError):
                self.service.ingest_channel_message(**self._ingest_kwargs())

            self.assertTrue(self.connection.in_transaction)
            visible_to_other = other_connection.execute(
                "SELECT COUNT(*) FROM sources WHERE name = 'scratch'"
            ).fetchone()[0]
            self.assertEqual(
                visible_to_other, 0,
                "unrelated pending work must not be committed by a refused R5 call",
            )
        finally:
            other_connection.close()
            self.connection.rollback()

    def test_rejects_entry_for_every_public_write_method(self):
        self.connection.execute("INSERT INTO sources (name) VALUES ('scratch2')")

        with self.assertRaises(RuntimeError):
            self.service.ingest_channel_message(**self._ingest_kwargs())
        with self.assertRaises(RuntimeError):
            self.service.ingest_batch(
                source_name="discord", reference_date="2026-07-24", timezone="UTC",
                raw_batch_text="BOUGHT SPY 450C $3.25",
            )
        with self.assertRaises(RuntimeError):
            self.service.reprocess_raw_message(1)
        with self.assertRaises(RuntimeError):
            self.service.reprocess_import_batch(1)

        self.connection.rollback()

    def test_begin_failure_restores_isolation_level_and_leaves_no_open_transaction(self):
        saved_isolation_level = self.connection.isolation_level
        self.connection.fail_on_begin = True

        with self.assertRaises(sqlite3.OperationalError):
            self.service.ingest_channel_message(**self._ingest_kwargs())

        self.assertEqual(self.connection.isolation_level, saved_isolation_level)
        self.assertFalse(self.connection.in_transaction)
        self.assertFalse(self.connection.rollback_called)

    def test_body_failure_rolls_back_propagates_and_restores_isolation_level(self):
        saved_isolation_level = self.connection.isolation_level

        with patch.object(
            TradeService,
            "_ingest_channel_message_no_commit",
            side_effect=RuntimeError("simulated body failure"),
        ):
            with self.assertRaises(RuntimeError) as cm:
                self.service.ingest_channel_message(**self._ingest_kwargs())

        self.assertEqual(str(cm.exception), "simulated body failure")
        self.assertEqual(self.connection.isolation_level, saved_isolation_level)
        self.assertFalse(self.connection.in_transaction)
        self.assertTrue(self.connection.rollback_called)

    def test_commit_failure_rolls_back_propagates_and_restores_isolation_level(self):
        saved_isolation_level = self.connection.isolation_level
        self.connection.fail_on_commit = True

        with self.assertRaises(sqlite3.OperationalError) as cm:
            self.service.ingest_channel_message(**self._ingest_kwargs())

        self.assertEqual(str(cm.exception), "simulated COMMIT failure")
        self.assertEqual(self.connection.isolation_level, saved_isolation_level)
        self.assertFalse(self.connection.in_transaction)
        self.assertTrue(self.connection.rollback_called)

    def test_rollback_cleanup_failure_does_not_hide_primary_commit_exception(self):
        saved_isolation_level = self.connection.isolation_level
        self.connection.fail_on_commit = True
        self.connection.fail_on_rollback = True

        with self.assertRaises(sqlite3.OperationalError) as cm:
            self.service.ingest_channel_message(**self._ingest_kwargs())

        self.assertEqual(str(cm.exception), "simulated COMMIT failure")
        self.assertTrue(self.connection.rollback_called)
        self.assertEqual(self.connection.isolation_level, saved_isolation_level)

    def test_rollback_cleanup_failure_does_not_hide_primary_body_exception(self):
        saved_isolation_level = self.connection.isolation_level
        self.connection.fail_on_rollback = True

        with patch.object(
            TradeService,
            "_ingest_channel_message_no_commit",
            side_effect=RuntimeError("original body failure"),
        ):
            with self.assertRaises(RuntimeError) as cm:
                self.service.ingest_channel_message(**self._ingest_kwargs())

        self.assertEqual(str(cm.exception), "original body failure")
        self.assertEqual(self.connection.isolation_level, saved_isolation_level)

    def test_rollback_fails_but_sql_rollback_fallback_succeeds_leaves_connection_usable(self):
        saved_isolation_level = self.connection.isolation_level
        self.connection.fail_on_commit = True
        self.connection.fail_on_rollback = True

        with self.assertRaises(sqlite3.OperationalError) as cm:
            self.service.ingest_channel_message(**self._ingest_kwargs())

        self.assertEqual(str(cm.exception), "simulated COMMIT failure")
        self.assertTrue(self.connection.rollback_called)
        self.assertTrue(self.connection.rollback_sql_called)
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(self.connection.isolation_level, saved_isolation_level)
        # The connection remains open and fully usable after the fallback.
        self.connection.execute("SELECT 1")

    def test_rollback_and_sql_rollback_both_fail_closes_connection_but_preserves_original_exception(
        self,
    ):
        self.connection.fail_on_commit = True
        self.connection.fail_on_rollback = True
        self.connection.fail_on_rollback_sql = True

        with self.assertRaises(sqlite3.OperationalError) as cm:
            self.service.ingest_channel_message(**self._ingest_kwargs())

        self.assertEqual(str(cm.exception), "simulated COMMIT failure")
        self.assertTrue(self.connection.rollback_called)
        self.assertTrue(self.connection.rollback_sql_called)
        with self.assertRaises(sqlite3.ProgrammingError):
            self.connection.execute("SELECT 1")

    def test_successful_call_leaves_connection_clean(self):
        saved_isolation_level = self.connection.isolation_level

        self.service.ingest_channel_message(**self._ingest_kwargs())

        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(self.connection.isolation_level, saved_isolation_level)

    def test_reprocess_raw_message_owns_its_own_transaction(self):
        real_connection = get_connection(self.config)
        try:
            service = TradeService(real_connection)
            outcome = service.ingest_channel_message(**self._ingest_kwargs())
            real_connection.commit()
        finally:
            real_connection.close()

        saved_isolation_level = self.connection.isolation_level
        self.service.reprocess_raw_message(outcome.raw_message_id)
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(self.connection.isolation_level, saved_isolation_level)


if __name__ == "__main__":
    unittest.main()
