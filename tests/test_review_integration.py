"""Real-SQLite integration tests for Milestone 2D.4.

Exercises the complete Stored-Signal Review path against a real, unique
per-test temporary SQLite database: real ingestion through the existing
Manual Message Entry Submit workflow (app.parser.parse_message ->
database.service.TradeService.ingest_message() ->
database/repository.py), followed by a real read through the new Review
Signals workflow (database.service.TradeService.
list_trade_signals_for_review() -> database.repository.
get_trade_signals_for_review()). Only DISCORD_TRADERS_DB_PATH and
DISCORD_TRADERS_LOG_PATH are overridden (via os.environ, redirecting
app.py's own resolve_database_path()/resolve_log_path() calls to unique
temporary locations) - nothing else is mocked. Schema initialization,
connections, service orchestration, persistence, and the review query are
all real.
"""

import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app import logging_config

_SAMPLE_MESSAGE = "BTO SPY 450C 7/19/2025 @3.25 10 contracts"
_SECOND_MESSAGE = "STC AAPL 190P 12/15/2025 @1.10"


class _ReviewIntegrationTestCase(unittest.TestCase):
    """Shared fixture: a unique, not-yet-existing temporary database path
    and log path, redirected via environment overrides (not a DatabaseConfig
    patch), so both the real Path.exists() check in the Review workflow and
    initialize_database()'s real directory creation in the Submit workflow
    are exercised exactly as they run in production.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Nested, so "the parent directory was never created" is a
        # meaningful assertion - not already true merely because the
        # TemporaryDirectory itself exists.
        self.db_path = Path(self._tmp.name) / "nested" / "discord_traders.db"
        self.log_path = Path(self._tmp.name) / "logs" / "discord_traders.log"

        self._remove_file_log_handler()
        self.addCleanup(self._remove_file_log_handler)

        env_patch = patch.dict(
            os.environ,
            {
                "DISCORD_TRADERS_DB_PATH": str(self.db_path),
                "DISCORD_TRADERS_LOG_PATH": str(self.log_path),
            },
            clear=False,
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def _remove_file_log_handler(self):
        """Detach and close any discord_traders file handler.

        The handler is a process-wide idempotent singleton
        (app/logging_config.py); removing it before and after each test
        guarantees Submit's configure_file_logging() call genuinely
        re-resolves DISCORD_TRADERS_LOG_PATH against this test's own
        temporary directory rather than reusing a handler left by an
        earlier test.
        """
        logger = logging.getLogger(logging_config._LOGGER_NAME)
        for handler in list(logger.handlers):
            if getattr(handler, logging_config._FILE_HANDLER_MARKER, False):
                logger.removeHandler(handler)
                handler.close()

    def _ingest(self, at, raw_text, trader_name, external_trader_id):
        """Drive a full real Parse -> review -> Submit cycle on at."""
        at.run()
        at.text_area[0].input(raw_text).run()
        at.button[0].click().run()
        at.text_input[0].input(trader_name).run()
        at.text_input[1].input(external_trader_id).run()
        at.button[1].click().run()
        return at

    def _open_review(self, at):
        return at.sidebar.radio[0].set_value("Review Signals").run()


class RealDatabaseReviewTests(_ReviewIntegrationTestCase):
    def test_ingest_then_review_shows_source_trader_signal_and_raw_message(self):
        at = AppTest.from_file("app/app.py")
        self._ingest(at, _SAMPLE_MESSAGE, "alice", "disc-123")
        self.assertEqual(len(at.error), 0)

        at = self._open_review(at)

        self.assertEqual(len(at.dataframe), 1)
        df = at.dataframe[0].value
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["Symbol"], "SPY")
        self.assertEqual(df.iloc[0]["Action"], "BTO")
        self.assertEqual(df.iloc[0]["Source"], "manual")
        self.assertEqual(df.iloc[0]["Trader"], "alice")

        signal_id = int(df.iloc[0]["ID"])
        at = at.selectbox[0].set_value(signal_id).run()

        detail_text = "\n".join(element.value for element in at.markdown)
        self.assertIn("Source: manual", detail_text)
        self.assertIn("Trader: alice", detail_text)
        self.assertIn("External trader ID: disc-123", detail_text)
        self.assertIn("Symbol: SPY", detail_text)
        self.assertEqual(len(at.text_area), 1)
        self.assertEqual(at.text_area[0].value, _SAMPLE_MESSAGE)

    def test_filters_and_newest_first_ordering_against_real_sqlite(self):
        at = AppTest.from_file("app/app.py")
        self._ingest(at, _SAMPLE_MESSAGE, "alice", "disc-123")
        self._ingest(at, _SECOND_MESSAGE, "bob", "disc-456")
        self.assertEqual(len(at.error), 0)

        at = self._open_review(at)

        df_all = at.dataframe[0].value
        self.assertEqual(len(df_all), 2)
        # Newest first: the second (AAPL) ingestion appears before the
        # first (SPY) one.
        self.assertEqual(list(df_all["Symbol"]), ["AAPL", "SPY"])

        at = at.text_input[2].input("aapl").run()
        df_filtered = at.dataframe[0].value
        self.assertEqual(len(df_filtered), 1)
        self.assertEqual(df_filtered.iloc[0]["Symbol"], "AAPL")
        self.assertEqual(df_filtered.iloc[0]["Trader"], "bob")

    def test_trader_filter_against_real_sqlite(self):
        at = AppTest.from_file("app/app.py")
        self._ingest(at, _SAMPLE_MESSAGE, "alice", "disc-123")
        self._ingest(at, _SECOND_MESSAGE, "bob", "disc-456")

        at = self._open_review(at)
        at = at.text_input[1].input("alice").run()

        df = at.dataframe[0].value
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["Trader"], "alice")

    def test_nonexistent_database_remains_uncreated_via_real_review_workflow(self):
        self.assertFalse(self.db_path.exists())
        self.assertFalse(self.db_path.parent.exists())

        at = AppTest.from_file("app/app.py")
        at.run()
        at = self._open_review(at)

        self.assertEqual(len(at.info), 1)
        self.assertEqual(at.info[0].value, "No trade signals found.")
        self.assertFalse(self.db_path.exists())
        self.assertFalse(self.db_path.parent.exists())


if __name__ == "__main__":
    unittest.main()
