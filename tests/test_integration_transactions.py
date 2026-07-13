"""Integration and transaction tests for Milestone 2B.7.

Exercises TradeService against a real, temp-file SQLite database (via
database/db.py + database/schema.sql, exactly as production code will use
them), confirming transactional integrity: TradeService never commits or
rolls back on its own, so the caller's commit()/rollback() decisions are
what determine what is actually persisted.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from database.config import DatabaseConfig
from database.db import get_connection, initialize_database
from database.service import TradeService


class IntegrationTransactionTests(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = path
        self.config = DatabaseConfig(db_path=path)
        initialize_database(self.config)
        self.connection = get_connection(self.config)
        self.service = TradeService(self.connection)

    def tearDown(self):
        if self.connection is not None:
            self.connection.close()
        os.remove(self.db_path)

    def _reference_time(self):
        # trade_signals.created_at is DB-generated using SQLite's
        # CURRENT_TIMESTAMP, which is UTC, so tests relying on the
        # duplicate window matching "now" need a UTC reference_time
        # bracketing real now, not a fixed fictional timestamp.
        return (datetime.now(timezone.utc) + timedelta(minutes=1)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    def _counts(self, connection):
        tables = ("sources", "traders", "raw_messages", "trade_signals")
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }

    def test_full_ingest_happy_path_commits_and_persists(self):
        result = self.service.ingest_message(
            "discord",
            "alice",
            "BTO SPY 500c @3.25, BTO QQQ 400p @1.10",
            reference_time="2026-07-13 09:00:00",
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
        self.connection.commit()

        other_connection = get_connection(self.config)
        try:
            counts = self._counts(other_connection)
            self.assertEqual(counts["sources"], 1)
            self.assertEqual(counts["traders"], 1)
            self.assertEqual(counts["raw_messages"], 1)
            self.assertEqual(counts["trade_signals"], 2)

            raw_message_row = other_connection.execute(
                "SELECT id, source_id FROM raw_messages"
            ).fetchone()
            self.assertEqual(raw_message_row["id"], result["raw_message"].id)
            self.assertEqual(raw_message_row["source_id"], result["source"].id)

            trader_row = other_connection.execute(
                "SELECT id, source_id, external_trader_id FROM traders"
            ).fetchone()
            self.assertEqual(trader_row["id"], result["trader"].id)
            self.assertEqual(trader_row["source_id"], result["source"].id)
            self.assertEqual(trader_row["external_trader_id"], "disc-1")

            signal_rows = other_connection.execute(
                "SELECT id, raw_message_id, trader_id, symbol "
                "FROM trade_signals ORDER BY id"
            ).fetchall()
            self.assertEqual(len(signal_rows), 2)
            for row, expected_signal in zip(signal_rows, result["trade_signals"]):
                self.assertEqual(row["id"], expected_signal.id)
                self.assertEqual(row["raw_message_id"], result["raw_message"].id)
                self.assertEqual(row["trader_id"], result["trader"].id)
        finally:
            other_connection.close()

    def test_partial_failure_rollback_leaves_no_partial_rows(self):
        with self.assertRaises(ValueError):
            self.service.ingest_message(
                "discord",
                "alice",
                "BTO SPY 500c @3.25, then a broken second line",
                reference_time="2026-07-13 09:00:00",
                trade_signals=[
                    {
                        "symbol": "SPY",
                        "action": "BTO",
                        "price": Decimal("3.25"),
                    },
                    {
                        "symbol": None,
                        "action": "BTO",
                    },
                ],
            )

        self.connection.rollback()

        same_connection_counts = self._counts(self.connection)
        self.assertEqual(
            same_connection_counts,
            {"sources": 0, "traders": 0, "raw_messages": 0, "trade_signals": 0},
        )

        other_connection = get_connection(self.config)
        try:
            self.assertEqual(
                self._counts(other_connection),
                {"sources": 0, "traders": 0, "raw_messages": 0, "trade_signals": 0},
            )
        finally:
            other_connection.close()

    def test_duplicate_warning_non_blocking_persists_both_signals(self):
        reference_time = self._reference_time()
        signal_fields = {
            "symbol": "SPY",
            "action": "BTO",
            "option_type": "call",
            "price": Decimal("3.25"),
            "expiration": "2026-12-18",
        }

        result = self.service.ingest_message(
            "discord",
            "alice",
            "BTO SPY 500c @3.25, BTO SPY 500c @3.25 again",
            reference_time=reference_time,
            trade_signals=[dict(signal_fields), dict(signal_fields)],
        )
        self.connection.commit()

        self.assertEqual(result["duplicate_warnings"][0], None)
        self.assertIsNotNone(result["duplicate_warnings"][1])

        other_connection = get_connection(self.config)
        try:
            row = other_connection.execute(
                "SELECT COUNT(*) FROM trade_signals"
            ).fetchone()
            self.assertEqual(row[0], 2)
        finally:
            other_connection.close()

    def test_edit_history_after_multiple_corrections(self):
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
        signal_id = result["trade_signals"][0].id

        self.service.update_trade_signal(signal_id, price=Decimal("3.50"))
        self.service.update_trade_signal(signal_id, option_type="put")
        self.connection.commit()

        other_connection = get_connection(self.config)
        try:
            edit_rows = other_connection.execute(
                "SELECT previous_values FROM trade_signal_edits "
                "WHERE trade_signal_id = ? ORDER BY id",
                (signal_id,),
            ).fetchall()
            self.assertEqual(len(edit_rows), 2)

            first_snapshot = json.loads(edit_rows[0]["previous_values"])
            second_snapshot = json.loads(edit_rows[1]["previous_values"])
            self.assertEqual(first_snapshot["price"], "3.25")
            self.assertEqual(second_snapshot["price"], "3.50")
            self.assertEqual(second_snapshot["option_type"], "call")

            current_row = other_connection.execute(
                "SELECT price, option_type FROM trade_signals WHERE id = ?",
                (signal_id,),
            ).fetchone()
            self.assertEqual(current_row["price"], "3.50")
            self.assertEqual(current_row["option_type"], "put")
        finally:
            other_connection.close()

    def test_commit_durability_via_fresh_connection(self):
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
        self.connection.commit()
        self.connection.close()
        self.connection = None

        fresh_connection = get_connection(self.config)
        try:
            counts = self._counts(fresh_connection)
            self.assertEqual(counts["sources"], 1)
            self.assertEqual(counts["traders"], 1)
            self.assertEqual(counts["raw_messages"], 1)
            self.assertEqual(counts["trade_signals"], 1)

            signal_row = fresh_connection.execute(
                "SELECT id, symbol, price FROM trade_signals WHERE id = ?",
                (result["trade_signals"][0].id,),
            ).fetchone()
            self.assertEqual(signal_row["symbol"], "SPY")
            self.assertEqual(signal_row["price"], "3.25")
        finally:
            fresh_connection.close()
