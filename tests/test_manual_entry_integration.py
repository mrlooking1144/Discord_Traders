"""Real-SQLite integration tests for Milestone 2C.4.

Exercises the complete manual-entry path: pasted raw text -> the Streamlit
UI (app/app.py) -> app.parser.parse_message() -> the reviewed structured
signal -> the "Submit to Database" action ->
database.service.TradeService.ingest_message() -> database/repository.py ->
a real temporary SQLite database initialized through
database.db.initialize_database() and database/schema.sql.

Only database.config.DatabaseConfig is patched, to redirect app.py's
hardcoded db path ("discord_traders.db") to a unique per-test temporary
file. database.db.initialize_database, database.db.get_connection,
database.service.TradeService, and database/repository.py are never
mocked on the successful path - schema initialization, connections,
service orchestration, and persistence are all real. The single exception
is test_mid_transaction_failure_rolls_back_leaving_no_partial_rows, which
narrowly patches database.service.create_trade_signal to manufacture a
controlled mid-transaction failure after earlier real writes have already
occurred, in order to exercise app.py's real rollback path.

Milestone 2D.2: app.py now calls app.logging_config.configure_file_logging()
at the start of every Submit click, which - unmocked here, matching this
file's real-I/O philosophy for everything else - performs real file-logging
initialization. Each test overrides DISCORD_TRADERS_LOG_PATH to a path
inside its own tempfile.TemporaryDirectory(), so that real initialization
writes only inside a temporary directory removed by normal cleanup, never
to the developer's actual LOCALAPPDATA. Because the discord_traders
logger's file handler is otherwise a process-wide singleton (idempotent
by design - see app/logging_config.py), any handler attached during a
test is explicitly removed and closed before that test's temporary
directory is cleaned up, so every test genuinely re-resolves its own
override rather than reusing a handler left over from an earlier test.
"""

import hashlib
import logging
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app import logging_config
from database.config import DatabaseConfig as RealDatabaseConfig
from database.db import get_connection, initialize_database
from database.service import TradeService

_SAMPLE_MESSAGE = "BTO SPY 450C 7/19/2025 @3.25 10 contracts"


class _ManualEntryIntegrationTestCase(unittest.TestCase):
    """Shared fixture for all Milestone 2C.4 integration tests.

    Provides a unique temporary SQLite database per test (real schema
    applied via the real DatabaseConfig/initialize_database before any
    AppTest run), a test-scoped patch of database.config.DatabaseConfig
    that redirects app.py to that temporary file, and protection of any
    existing repository-root discord_traders.db via a before/after
    existence + size + mtime + SHA-256 snapshot comparison.
    """

    def setUp(self):
        self._prod_db_path = (
            Path(__file__).resolve().parent.parent / "discord_traders.db"
        )
        self._prod_db_before = self._snapshot_prod_db()
        self.addCleanup(self._assert_prod_db_unchanged)

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = path
        self.addCleanup(self._remove_temp_db)

        # Real DatabaseConfig, bound via this alias before any patch exists,
        # so the patch on database.config.DatabaseConfig (below) can never
        # affect fixture initialization or verification connections that use
        # RealDatabaseConfig/self.config directly.
        self.config = RealDatabaseConfig(db_path=self.db_path)
        initialize_database(self.config)

        self._config_patch = patch(
            "database.config.DatabaseConfig", side_effect=self._config_factory
        )
        self._config_patch.start()
        self.addCleanup(self._config_patch.stop)

        self._log_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._log_dir.cleanup)
        self.log_path = str(Path(self._log_dir.name) / "logs" / "discord_traders.log")

        # The discord_traders logger's file handler is a process-wide
        # singleton, idempotent by design (app/logging_config.py). Remove
        # any handler left attached by an earlier test before this test
        # runs, so app.py's Submit-triggered configure_file_logging() call
        # genuinely re-resolves DISCORD_TRADERS_LOG_PATH against this
        # test's own temporary directory rather than reusing a handler
        # already pointed elsewhere.
        self._remove_file_log_handler()
        self.addCleanup(self._remove_file_log_handler)

        log_path_patch = patch.dict(
            os.environ, {"DISCORD_TRADERS_LOG_PATH": self.log_path}, clear=False
        )
        log_path_patch.start()
        self.addCleanup(log_path_patch.stop)

    def _config_factory(self, db_path=None, **kwargs):
        """Ignore the db_path app.py passes; always point at the temp file."""
        return RealDatabaseConfig(db_path=self.db_path)

    def _remove_file_log_handler(self):
        """Detach and close any discord_traders file handler.

        Ensures each test starts and ends without a lingering file handler
        pointed at a temporary directory that either hasn't been created
        yet or is about to be removed by cleanup.
        """
        logger = logging.getLogger(logging_config._LOGGER_NAME)
        for handler in list(logger.handlers):
            if getattr(handler, logging_config._FILE_HANDLER_MARKER, False):
                logger.removeHandler(handler)
                handler.close()

    def _snapshot_prod_db(self):
        if not self._prod_db_path.exists():
            return {"exists": False}
        stat = self._prod_db_path.stat()
        return {
            "exists": True,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "sha256": hashlib.sha256(self._prod_db_path.read_bytes()).hexdigest(),
        }

    def _remove_temp_db(self):
        self.assertTrue(
            os.path.exists(self.db_path),
            f"Expected temporary database {self.db_path} to still exist "
            "before cleanup.",
        )
        os.remove(self.db_path)

    def _assert_prod_db_unchanged(self):
        self.assertEqual(self._snapshot_prod_db(), self._prod_db_before)

    def _counts(self, connection):
        tables = ("sources", "traders", "raw_messages", "trade_signals")
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }

    def _run_to_review(self, at, raw_text=_SAMPLE_MESSAGE):
        """Drive the app through Parse Message, landing on the review step."""
        at.run()
        at.text_area[0].input(raw_text).run()
        at.button[0].click().run()
        return at

    def _submit(self, at, trader_name="alice", external_trader_id="disc-123"):
        """Fill the trader fields and click Submit to Database."""
        at.text_input[0].input(trader_name).run()
        at.text_input[1].input(external_trader_id).run()
        at.button[1].click().run()
        return at


class ManualEntryHappyPathPersistenceTests(_ManualEntryIntegrationTestCase):
    def test_full_manual_entry_persists_all_tables_with_correct_relationships(self):
        at = AppTest.from_file("app/app.py")
        self._run_to_review(at, raw_text=_SAMPLE_MESSAGE)
        self._submit(at, trader_name="  alice  ", external_trader_id="  disc-123  ")

        self.assertEqual(len(at.error), 0)

        verify_connection = get_connection(self.config)
        try:
            counts = self._counts(verify_connection)
            self.assertEqual(counts["sources"], 1)
            self.assertEqual(counts["traders"], 1)
            self.assertEqual(counts["raw_messages"], 1)
            self.assertEqual(counts["trade_signals"], 1)

            source_row = verify_connection.execute(
                "SELECT id, name FROM sources"
            ).fetchone()
            self.assertEqual(source_row["name"], "manual")

            trader_row = verify_connection.execute(
                "SELECT id, source_id, name, external_trader_id FROM traders"
            ).fetchone()
            self.assertEqual(trader_row["source_id"], source_row["id"])
            self.assertEqual(trader_row["name"], "alice")
            self.assertEqual(trader_row["external_trader_id"], "disc-123")

            raw_message_row = verify_connection.execute(
                "SELECT id, source_id, raw_text FROM raw_messages"
            ).fetchone()
            self.assertEqual(raw_message_row["source_id"], source_row["id"])
            self.assertEqual(raw_message_row["raw_text"], _SAMPLE_MESSAGE)

            signal_row = verify_connection.execute(
                "SELECT raw_message_id, trader_id, symbol, action, option_type, "
                "price, expiration, position_size FROM trade_signals"
            ).fetchone()
            self.assertEqual(signal_row["raw_message_id"], raw_message_row["id"])
            self.assertEqual(signal_row["trader_id"], trader_row["id"])
            self.assertEqual(signal_row["symbol"], "SPY")
            self.assertEqual(signal_row["action"], "BTO")
            self.assertEqual(signal_row["option_type"], "call")
            self.assertEqual(Decimal(str(signal_row["price"])), Decimal("3.25"))
            self.assertEqual(signal_row["expiration"], "2025-07-19")
            self.assertEqual(signal_row["position_size"], "10 contracts")
        finally:
            verify_connection.close()


class ManualEntryStableTraderIdentityTests(_ManualEntryIntegrationTestCase):
    def test_same_external_trader_id_reuses_single_trader_row_across_two_messages(self):
        at = AppTest.from_file("app/app.py")
        self._run_to_review(at, raw_text="BTO SPY 450C 7/19/2025 @3.25")
        self._submit(at, trader_name="alice", external_trader_id="disc-shared")

        self._run_to_review(at, raw_text="STC AAPL 190P 12/15/2025 @1.10")
        self._submit(at, trader_name="alice", external_trader_id="disc-shared")

        self.assertEqual(len(at.error), 0)

        verify_connection = get_connection(self.config)
        try:
            trader_rows = verify_connection.execute("SELECT id FROM traders").fetchall()
            self.assertEqual(len(trader_rows), 1)
            trader_id = trader_rows[0]["id"]

            signal_rows = verify_connection.execute(
                "SELECT trader_id FROM trade_signals"
            ).fetchall()
            self.assertEqual(len(signal_rows), 2)
            self.assertEqual({row["trader_id"] for row in signal_rows}, {trader_id})
        finally:
            verify_connection.close()

    def test_trader_display_name_not_updated_on_resubmission_with_different_name(self):
        at = AppTest.from_file("app/app.py")
        self._run_to_review(at, raw_text="BTO SPY 450C 7/19/2025 @3.25")
        self._submit(at, trader_name="alice", external_trader_id="disc-rename")

        self._run_to_review(at, raw_text="STC AAPL 190P 12/15/2025 @1.10")
        self._submit(at, trader_name="alice-updated", external_trader_id="disc-rename")

        self.assertEqual(len(at.error), 0)

        verify_connection = get_connection(self.config)
        try:
            trader_rows = verify_connection.execute("SELECT name FROM traders").fetchall()
            # database/service.py's ingest_message() reuses an existing trader
            # found by external_trader_id as-is; it never renames it, so the
            # second submission's "alice-updated" is not persisted anywhere.
            self.assertEqual(len(trader_rows), 1)
            self.assertEqual(trader_rows[0]["name"], "alice")
        finally:
            verify_connection.close()


class ManualEntryDuplicateWarningTests(_ManualEntryIntegrationTestCase):
    def test_duplicate_signal_within_window_persists_both_with_advisory_warning(self):
        # Deterministic pre-seed: create the first matching signal directly
        # through the real TradeService (not via the UI), on the same real
        # temp database AppTest will use once the DatabaseConfig patch (set
        # up in setUp()) is in effect.
        seed_connection = get_connection(self.config)
        try:
            TradeService(seed_connection).ingest_message(
                source_name="manual",
                trader_name="alice",
                raw_text="BTO SPY 500C @3.25",
                reference_time="2026-07-13 09:00:00",
                external_trader_id="dup-trader-1",
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
            seed_connection.commit()
        finally:
            seed_connection.close()

        # Real path under test: AppTest -> parser -> TradeService ->
        # repository -> real SQLite, matching the seeded signal exactly.
        at = AppTest.from_file("app/app.py")
        self._run_to_review(at, raw_text="BTO SPY 500C 12/18/2026 @3.25")
        self._submit(at, trader_name="alice", external_trader_id="dup-trader-1")

        self.assertEqual(len(at.error), 0)
        self.assertEqual(len(at.warning), 1)
        self.assertIn("Possible duplicate", at.warning[0].value)

        verify_connection = get_connection(self.config)
        try:
            source_rows = verify_connection.execute(
                "SELECT id, name FROM sources"
            ).fetchall()
            self.assertEqual(len(source_rows), 1)
            self.assertEqual(source_rows[0]["name"], "manual")
            manual_source_id = source_rows[0]["id"]

            trader_rows = verify_connection.execute("SELECT id FROM traders").fetchall()
            self.assertEqual(len(trader_rows), 1)
            trader_id = trader_rows[0]["id"]

            raw_message_rows = verify_connection.execute(
                "SELECT id, source_id FROM raw_messages ORDER BY id"
            ).fetchall()
            self.assertEqual(len(raw_message_rows), 2)
            for row in raw_message_rows:
                self.assertEqual(row["source_id"], manual_source_id)

            signal_rows = verify_connection.execute(
                "SELECT id, trader_id, symbol, action, option_type, price, "
                "expiration FROM trade_signals ORDER BY id"
            ).fetchall()
            self.assertEqual(len(signal_rows), 2)
            for row in signal_rows:
                self.assertEqual(row["trader_id"], trader_id)
                self.assertEqual(row["symbol"], "SPY")
                self.assertEqual(row["action"], "BTO")
                self.assertEqual(row["option_type"], "call")
                self.assertEqual(Decimal(str(row["price"])), Decimal("3.25"))
                self.assertEqual(row["expiration"], "2026-12-18")

            # The second (UI-submitted) signal was actually persisted despite
            # the advisory warning, not suppressed or deduplicated.
            self.assertGreater(signal_rows[1]["id"], signal_rows[0]["id"])
        finally:
            verify_connection.close()


class ManualEntryMalformedInputTests(_ManualEntryIntegrationTestCase):
    def test_malformed_input_persists_no_rows_and_blocks_submission(self):
        at = AppTest.from_file("app/app.py")
        at.run()
        at.text_area[0].input("just some random text, nothing parseable here").run()
        at.button[0].click().run()

        # No trader/external-ID fields render and no Submit button exists -
        # submission is structurally impossible, not merely validation-blocked.
        # Milestone 2D.3: "Create Backup" is always rendered, so only
        # "Submit to Database" is absent when there is no preview.
        self.assertEqual(len(at.text_input), 0)
        self.assertEqual({b.label for b in at.button}, {"Parse Message", "Create Backup"})

        verify_connection = get_connection(self.config)
        try:
            self.assertEqual(
                self._counts(verify_connection),
                {"sources": 0, "traders": 0, "raw_messages": 0, "trade_signals": 0},
            )
        finally:
            verify_connection.close()


class ManualEntryTransactionTests(_ManualEntryIntegrationTestCase):
    def test_successful_submission_commits_and_is_durable_via_fresh_connection(self):
        at = AppTest.from_file("app/app.py")
        self._run_to_review(at)
        self._submit(at)

        self.assertEqual(len(at.error), 0)

        # A brand-new connection (not the one app.py opened and closed
        # internally) must see the committed data.
        fresh_connection = get_connection(self.config)
        try:
            counts = self._counts(fresh_connection)
            self.assertEqual(counts["sources"], 1)
            self.assertEqual(counts["traders"], 1)
            self.assertEqual(counts["raw_messages"], 1)
            self.assertEqual(counts["trade_signals"], 1)
        finally:
            fresh_connection.close()

    def test_mid_transaction_failure_rolls_back_leaving_no_partial_rows(self):
        # Narrowly scoped failure injection: database.service.create_trade_signal
        # is the module-level name TradeService.ingest_message() actually calls.
        # Real get_or_create_source, create_trader, and create_raw_message
        # execute for real first (uncommitted); this raises before any
        # trade_signals row is written and before app.py's conn.commit() runs,
        # so app.py's real except/rollback path is exercised.
        with patch(
            "database.service.create_trade_signal",
            side_effect=sqlite3.OperationalError("simulated ingestion failure"),
        ):
            at = AppTest.from_file("app/app.py")
            self._run_to_review(at)
            self._submit(at)

            self.assertEqual(len(at.exception), 0)
            self.assertEqual(len(at.error), 1)

        verify_connection = get_connection(self.config)
        try:
            self.assertEqual(
                self._counts(verify_connection),
                {"sources": 0, "traders": 0, "raw_messages": 0, "trade_signals": 0},
            )
        finally:
            verify_connection.close()


class ManualEntryProductionDatabaseProtectionTests(_ManualEntryIntegrationTestCase):
    def test_production_database_file_never_created_or_modified(self):
        at = AppTest.from_file("app/app.py")
        self._run_to_review(at)
        self._submit(at)

        self.assertEqual(len(at.error), 0)
        self.assertEqual(self._snapshot_prod_db(), self._prod_db_before)


if __name__ == "__main__":
    unittest.main()
