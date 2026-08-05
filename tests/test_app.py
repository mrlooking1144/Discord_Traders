"""UI/smoke tests for app/streamlit_app.py.

Covers Milestone 2C.2: manual raw-message entry, invoking the Milestone 2C.1
parser, and displaying either the parsed structured result or a "nothing
found" message. Uses Streamlit's AppTest harness to drive the app without a
running server. No TradeService, repository, or database involvement.

Covers Milestone 2C.3: submitting a reviewed parse result to the database
through database.service.TradeService.ingest_message(). database.db.
get_connection, database.db.initialize_database, and database.service.
TradeService are patched as controlled test doubles throughout - no real
SQLite database is touched here (reserved for Milestone 2C.4).

Covers Milestone 2D.1: proving app.py passes the exact return value of
database.config.resolve_database_path() into DatabaseConfig(db_path=...).

Covers Milestone 2D.2: the safe UI error boundary (broadened exception
handling, sanitized ERROR diagnostics, a distinct parser-failure message)
and operational logging behavior (console-handler idempotency across
Streamlit reruns, file-logging failures never blocking submission, and
confirmation that raw message text, trader identifiers, parsed signal
values, exception message text, and the database path never appear in
any captured log record). database.db.initialize_database,
database.db.get_connection, database.service.TradeService, and
app.logging_config.configure_file_logging are patched as controlled test
doubles throughout these tests - no real SQLite database and no real
file logging are touched here.
"""

import json
import logging
import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from streamlit.testing.v1 import AppTest

from app import logging_config
from app.dashboard_formatting import (
    LIFECYCLE_CSV_FIELDNAMES,
    SORT_METRIC_CHOICES,
    SUMMARY_CSV_FIELDNAMES,
    build_lifecycle_csv_rows,
    build_summary_csv_rows,
    rank_trader_summaries,
    rows_to_csv_string,
)
from app.parser import parse_message
from database.config import resolve_database_path
from database.service import (
    AuditHistoryError,
    LifecycleIntegrityError,
    LifecycleSnapshotError,
    LifecycleUnsafeCorrectionError,
    StaleTradeSignalError,
    TradeSignalNotFoundError,
)

_SAMPLE_MESSAGE = "BTO SPY 450C 7/19/2025 @3.25 10 contracts"


class ManualMessageEntryTests(unittest.TestCase):
    def test_successful_parse_displays_structured_result(self):
        at = AppTest.from_file("app/streamlit_app.py")
        at.run()

        at.text_area[0].input("BTO SPY 450C 7/19/2025 @3.25 10 contracts").run()
        at.button[0].click().run()

        self.assertEqual(len(at.success), 1)
        self.assertIn("1 trade signal", at.success[0].value)
        self.assertEqual(len(at.json), 1)
        displayed = json.loads(at.json[0].value)
        self.assertEqual(displayed["symbol"], "SPY")
        self.assertEqual(displayed["action"], "BTO")
        self.assertEqual(displayed["option_type"], "call")
        self.assertEqual(displayed["expiration"], "2025-07-19")
        self.assertEqual(displayed["position_size"], "10 contracts")
        self.assertIn("3.25", displayed["price"])
        self.assertEqual(len(at.warning), 0)

    def test_multiple_signals_each_displayed(self):
        at = AppTest.from_file("app/streamlit_app.py")
        at.run()

        at.text_area[0].input("BTO SPY 450C 7/19/2025 @3.25\nSTC AAPL 190P 12/15/2025 @1.10").run()
        at.button[0].click().run()

        self.assertEqual(len(at.success), 1)
        self.assertIn("2 trade signal", at.success[0].value)
        self.assertEqual(len(at.json), 2)

    def test_no_signal_displays_nothing_found_message(self):
        at = AppTest.from_file("app/streamlit_app.py")
        at.run()

        at.text_area[0].input("just some random text").run()
        at.button[0].click().run()

        self.assertEqual(len(at.warning), 1)
        self.assertIn("No trade signals found", at.warning[0].value)
        self.assertEqual(len(at.success), 0)
        self.assertEqual(len(at.json), 0)

    def test_empty_input_displays_nothing_found_message(self):
        at = AppTest.from_file("app/streamlit_app.py")
        at.run()

        at.button[0].click().run()

        self.assertEqual(len(at.warning), 1)
        self.assertIn("No trade signals found", at.warning[0].value)

    def test_no_run_before_button_click_shows_no_result(self):
        at = AppTest.from_file("app/streamlit_app.py")
        at.run()

        self.assertEqual(len(at.success), 0)
        self.assertEqual(len(at.warning), 0)
        self.assertEqual(len(at.json), 0)


class _SubmissionWorkflowTestCase(unittest.TestCase):
    """Shared setUp and helpers for tests driving app.py's Submit
    workflow with mocked database/TradeService layers.

    Contains no test_* methods of its own, so it contributes no tests
    when discovered - only its concrete subclasses (ManualEntryPersistenceTests,
    SubmissionFailureLoggingTests) do.
    """

    def setUp(self):
        # Milestone 2D.2: app.py now calls configure_file_logging() at the
        # start of every Submit click. Patched here, once, for every
        # subclass, so none of these tests attempt real file I/O.
        file_log_patcher = patch("app.logging_config.configure_file_logging")
        file_log_patcher.start()
        self.addCleanup(file_log_patcher.stop)

    def _patches(self, mock_conn=None):
        """Return the three patches app.py's persistence path depends on."""
        mock_conn = mock_conn if mock_conn is not None else MagicMock()
        return (
            patch("database.db.initialize_database"),
            patch("database.db.get_connection", return_value=mock_conn),
            patch("database.service.TradeService"),
        )

    def _run_to_review(self, at, raw_text=_SAMPLE_MESSAGE):
        """Drive the app through Parse Message, landing on the review step."""
        at.run()
        at.text_area[0].input(raw_text).run()
        at.button[0].click().run()
        return at

    def _submit(self, at, trader_name="alice", external_trader_id="disc-123"):
        """Fill the trader fields and click Submit."""
        at.text_input[0].input(trader_name).run()
        at.text_input[1].input(external_trader_id).run()
        at.button[1].click().run()
        return at


class ManualEntryPersistenceTests(_SubmissionWorkflowTestCase):
    """Covers Milestone 2C.3: submit-to-database wiring."""

    def test_successful_submission_calls_ingest_message_once(self):
        mock_conn = MagicMock()
        init_p, conn_p, service_p = self._patches(mock_conn)
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value
            mock_service.ingest_message.return_value = {
                "trade_signals": [{"id": 1}],
                "duplicate_warnings": [None],
            }

            at = AppTest.from_file("app/streamlit_app.py")
            self._run_to_review(at)
            self._submit(at)

            self.assertEqual(mock_service.ingest_message.call_count, 1)

    def test_ingest_message_called_with_correctly_mapped_parser_signals(self):
        mock_conn = MagicMock()
        init_p, conn_p, service_p = self._patches(mock_conn)
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value
            mock_service.ingest_message.return_value = {
                "trade_signals": [{"id": 1}],
                "duplicate_warnings": [None],
            }

            at = AppTest.from_file("app/streamlit_app.py")
            self._run_to_review(at)
            self._submit(at)

            expected_signals = parse_message(_SAMPLE_MESSAGE)
            kwargs = mock_service.ingest_message.call_args.kwargs
            self.assertEqual(kwargs["trade_signals"], expected_signals)

    def test_ingest_message_called_with_required_metadata(self):
        mock_conn = MagicMock()
        init_p, conn_p, service_p = self._patches(mock_conn)
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value
            mock_service.ingest_message.return_value = {
                "trade_signals": [{"id": 1}],
                "duplicate_warnings": [None],
            }

            at = AppTest.from_file("app/streamlit_app.py")
            self._run_to_review(at)
            self._submit(at, trader_name="alice", external_trader_id="disc-123")

            kwargs = mock_service.ingest_message.call_args.kwargs
            self.assertEqual(kwargs["source_name"], "manual")
            self.assertEqual(kwargs["trader_name"], "alice")
            self.assertEqual(kwargs["external_trader_id"], "disc-123")
            self.assertEqual(kwargs["raw_text"], _SAMPLE_MESSAGE)
            self.assertEqual(kwargs["external_message_id"], None)
            self.assertEqual(kwargs["metadata"], None)
            self.assertEqual(kwargs["received_at"], None)
            # Must be a real, service-format timestamp, not a placeholder.
            datetime.strptime(kwargs["reference_time"], "%Y-%m-%d %H:%M:%S")

    def test_successful_creation_displays_success_message(self):
        mock_conn = MagicMock()
        init_p, conn_p, service_p = self._patches(mock_conn)
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value
            mock_service.ingest_message.return_value = {
                "trade_signals": [{"id": 1}],
                "duplicate_warnings": [None],
            }

            at = AppTest.from_file("app/streamlit_app.py")
            self._run_to_review(at)
            self._submit(at)

            self.assertEqual(len(at.error), 0)
            success_values = [s.value for s in at.success]
            self.assertTrue(any("Saved 1 trade signal" in v for v in success_values))

    def test_duplicate_warning_displayed_without_failure(self):
        mock_conn = MagicMock()
        init_p, conn_p, service_p = self._patches(mock_conn)
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value
            mock_service.ingest_message.return_value = {
                "trade_signals": [{"id": 1}],
                "duplicate_warnings": ["Possible duplicate: 1 matching trade signal(s)"],
            }

            at = AppTest.from_file("app/streamlit_app.py")
            self._run_to_review(at)
            self._submit(at)

            self.assertEqual(len(at.error), 0)
            self.assertEqual(len(at.warning), 1)
            self.assertIn("Possible duplicate", at.warning[0].value)
            mock_conn.commit.assert_called_once()
            mock_conn.rollback.assert_not_called()

    def test_unparseable_input_never_calls_service(self):
        init_p, conn_p, service_p = self._patches()
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value

            at = AppTest.from_file("app/streamlit_app.py")
            self._run_to_review(at, raw_text="just some random text")

            self.assertEqual(len(at.text_input), 0)
            # Milestone 2D.3: "Create Backup" is always rendered, so only
            # "Submit to Database" is absent when there is no preview.
            self.assertEqual({b.label for b in at.button}, {"Parse Message", "Create Backup"})
            self.assertEqual(mock_service.ingest_message.call_count, 0)

    def test_missing_trader_fields_prevent_submission(self):
        init_p, conn_p, service_p = self._patches()
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value

            at = AppTest.from_file("app/streamlit_app.py")
            self._run_to_review(at)
            # Leave both trader fields blank.
            at.button[1].click().run()

            self.assertEqual(mock_service.ingest_message.call_count, 0)
            self.assertEqual(len(at.error), 1)

    def test_service_exception_displays_controlled_error(self):
        mock_conn = MagicMock()
        init_p, conn_p, service_p = self._patches(mock_conn)
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value
            mock_service.ingest_message.side_effect = ValueError("bad input")

            at = AppTest.from_file("app/streamlit_app.py")
            self._run_to_review(at)
            self._submit(at)

            self.assertEqual(len(at.exception), 0)
            self.assertEqual(len(at.error), 1)
            self.assertEqual(
                at.error[0].value, "Could not save the message to the database."
            )
            self.assertNotIn("bad input", at.error[0].value)
            self.assertNotIn("ValueError", at.error[0].value)

    def test_database_init_failure_displays_controlled_error(self):
        init_p = patch(
            "database.db.initialize_database",
            side_effect=sqlite3.OperationalError("unable to open database file"),
        )
        conn_p = patch("database.db.get_connection", return_value=MagicMock())
        service_p = patch("database.service.TradeService")
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value

            at = AppTest.from_file("app/streamlit_app.py")
            self._run_to_review(at)
            self._submit(at)

            self.assertEqual(len(at.exception), 0)
            self.assertEqual(len(at.error), 1)
            self.assertEqual(mock_service.ingest_message.call_count, 0)

    def test_commit_called_after_successful_ingest(self):
        mock_conn = MagicMock()
        init_p, conn_p, service_p = self._patches(mock_conn)
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value
            mock_service.ingest_message.return_value = {
                "trade_signals": [{"id": 1}],
                "duplicate_warnings": [None],
            }

            at = AppTest.from_file("app/streamlit_app.py")
            self._run_to_review(at)
            self._submit(at)

            mock_conn.commit.assert_called_once()
            mock_conn.rollback.assert_not_called()

    def test_rollback_called_after_post_connection_failure(self):
        mock_conn = MagicMock()
        init_p, conn_p, service_p = self._patches(mock_conn)
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value
            mock_service.ingest_message.side_effect = sqlite3.IntegrityError(
                "UNIQUE constraint failed"
            )

            at = AppTest.from_file("app/streamlit_app.py")
            self._run_to_review(at)
            self._submit(at)

            mock_conn.rollback.assert_called_once()
            mock_conn.commit.assert_not_called()

    def test_no_rollback_when_connection_creation_itself_fails(self):
        init_p = patch("database.db.initialize_database")
        conn_p = patch(
            "database.db.get_connection",
            side_effect=sqlite3.OperationalError("unable to open database file"),
        )
        service_p = patch("database.service.TradeService")
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value

            at = AppTest.from_file("app/streamlit_app.py")
            self._run_to_review(at)
            self._submit(at)

            # No connection was ever created, so TradeService is never
            # instantiated or called - there is nothing to roll back.
            mock_service_cls.assert_not_called()
            self.assertEqual(mock_service.ingest_message.call_count, 0)
            self.assertEqual(len(at.exception), 0)
            self.assertEqual(len(at.error), 1)

    def test_connection_closed_after_success(self):
        mock_conn = MagicMock()
        init_p, conn_p, service_p = self._patches(mock_conn)
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value
            mock_service.ingest_message.return_value = {
                "trade_signals": [{"id": 1}],
                "duplicate_warnings": [None],
            }

            at = AppTest.from_file("app/streamlit_app.py")
            self._run_to_review(at)
            self._submit(at)

            mock_conn.close.assert_called_once()

    def test_connection_closed_after_failure(self):
        mock_conn = MagicMock()
        init_p, conn_p, service_p = self._patches(mock_conn)
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value
            mock_service.ingest_message.side_effect = ValueError("bad input")

            at = AppTest.from_file("app/streamlit_app.py")
            self._run_to_review(at)
            self._submit(at)

            mock_conn.close.assert_called_once()

    def test_edited_raw_text_clears_preview_and_prevents_stale_submit(self):
        init_p, conn_p, service_p = self._patches()
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value

            at = AppTest.from_file("app/streamlit_app.py")
            self._run_to_review(at)
            self.assertEqual(len(at.text_input), 2)

            # Editing the textarea alone (no re-parse) must drop the preview.
            at.text_area[0].input("something else entirely").run()

            self.assertEqual(len(at.text_input), 0)
            # Milestone 2D.3: "Create Backup" is always rendered, so only
            # "Submit to Database" is absent when there is no preview.
            self.assertEqual({b.label for b in at.button}, {"Parse Message", "Create Backup"})
            self.assertEqual(mock_service.ingest_message.call_count, 0)

    def test_failed_reparse_clears_previous_valid_preview(self):
        init_p, conn_p, service_p = self._patches()
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value

            at = AppTest.from_file("app/streamlit_app.py")
            self._run_to_review(at)
            self.assertEqual(len(at.text_input), 2)

            at.text_area[0].input("just chatting, nothing here").run()
            at.button[0].click().run()

            self.assertEqual(len(at.text_input), 0)
            self.assertEqual(len(at.warning), 1)
            self.assertIn("No trade signals found", at.warning[0].value)
            self.assertEqual(mock_service.ingest_message.call_count, 0)

    def test_commit_failure_displays_controlled_error(self):
        mock_conn = MagicMock()
        mock_conn.commit.side_effect = sqlite3.OperationalError("disk I/O error")
        init_p, conn_p, service_p = self._patches(mock_conn)
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value
            mock_service.ingest_message.return_value = {
                "trade_signals": [{"id": 1}],
                "duplicate_warnings": [None],
            }

            at = AppTest.from_file("app/streamlit_app.py")
            self._run_to_review(at)
            self._submit(at)

            self.assertEqual(len(at.exception), 0)
            self.assertEqual(len(at.error), 1)
            self.assertEqual(
                at.error[0].value, "Could not save the message to the database."
            )
            self.assertNotIn("disk I/O error", at.error[0].value)

    def test_commit_failure_attempts_rollback(self):
        mock_conn = MagicMock()
        mock_conn.commit.side_effect = sqlite3.OperationalError("disk I/O error")
        init_p, conn_p, service_p = self._patches(mock_conn)
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value
            mock_service.ingest_message.return_value = {
                "trade_signals": [{"id": 1}],
                "duplicate_warnings": [None],
            }

            at = AppTest.from_file("app/streamlit_app.py")
            self._run_to_review(at)
            self._submit(at)

            mock_conn.rollback.assert_called_once()

    def test_commit_failure_does_not_display_success_message(self):
        mock_conn = MagicMock()
        mock_conn.commit.side_effect = sqlite3.OperationalError("disk I/O error")
        init_p, conn_p, service_p = self._patches(mock_conn)
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value
            mock_service.ingest_message.return_value = {
                "trade_signals": [{"id": 1}],
                "duplicate_warnings": [None],
            }

            at = AppTest.from_file("app/streamlit_app.py")
            self._run_to_review(at)
            self._submit(at)

            success_values = [s.value for s in at.success]
            self.assertFalse(any("Saved" in v for v in success_values))
            self.assertEqual(len(at.warning), 0)

    def test_commit_failure_still_closes_connection(self):
        mock_conn = MagicMock()
        mock_conn.commit.side_effect = sqlite3.OperationalError("disk I/O error")
        init_p, conn_p, service_p = self._patches(mock_conn)
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value
            mock_service.ingest_message.return_value = {
                "trade_signals": [{"id": 1}],
                "duplicate_warnings": [None],
            }

            at = AppTest.from_file("app/streamlit_app.py")
            self._run_to_review(at)
            self._submit(at)

            mock_conn.close.assert_called_once()

    def test_trader_inputs_passed_without_surrounding_whitespace(self):
        mock_conn = MagicMock()
        init_p, conn_p, service_p = self._patches(mock_conn)
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value
            mock_service.ingest_message.return_value = {
                "trade_signals": [{"id": 1}],
                "duplicate_warnings": [None],
            }

            at = AppTest.from_file("app/streamlit_app.py")
            self._run_to_review(at)
            self._submit(
                at, trader_name="  alice  ", external_trader_id="  disc-123  "
            )

            kwargs = mock_service.ingest_message.call_args.kwargs
            self.assertEqual(kwargs["trader_name"], "alice")
            self.assertEqual(kwargs["external_trader_id"], "disc-123")
            # The raw message itself must stay verbatim, never stripped.
            self.assertEqual(kwargs["raw_text"], _SAMPLE_MESSAGE)


class DatabasePathResolutionWiringTests(unittest.TestCase):
    """Covers Milestone 2D.1: app.py -> resolve_database_path() -> DatabaseConfig."""

    def test_database_config_receives_resolved_path_from_resolve_database_path(self):
        sentinel_path = r"C:\sentinel\DiscordTraders\discord_traders.db"
        resolve_p = patch(
            "database.config.resolve_database_path", return_value=sentinel_path
        )
        config_p = patch("database.config.DatabaseConfig")
        init_p = patch("database.db.initialize_database")
        conn_p = patch("database.db.get_connection", return_value=MagicMock())
        service_p = patch("database.service.TradeService")
        file_log_p = patch("app.logging_config.configure_file_logging")

        with resolve_p, config_p as mock_config_cls, init_p, conn_p, service_p as mock_service_cls, file_log_p:
            mock_service = mock_service_cls.return_value
            mock_service.ingest_message.return_value = {
                "trade_signals": [{"id": 1}],
                "duplicate_warnings": [None],
            }

            at = AppTest.from_file("app/streamlit_app.py")
            at.run()
            at.text_area[0].input(_SAMPLE_MESSAGE).run()
            at.button[0].click().run()
            at.text_input[0].input("alice").run()
            at.text_input[1].input("disc-123").run()
            at.button[1].click().run()

            self.assertEqual(mock_config_cls.call_args.kwargs["db_path"], sentinel_path)


class ParserFailureLoggingTests(unittest.TestCase):
    """Covers Milestone 2D.2: unexpected parser exceptions vs. a valid
    empty parse result."""

    def test_unexpected_parser_exception_shows_safe_message(self):
        with patch(
            "app.parser.parse_message", side_effect=RuntimeError("parser bug")
        ):
            at = AppTest.from_file("app/streamlit_app.py")
            at.run()
            at.text_area[0].input(_SAMPLE_MESSAGE).run()
            at.button[0].click().run()

            self.assertEqual(len(at.exception), 0)
            self.assertEqual(len(at.error), 1)
            self.assertEqual(at.error[0].value, "Could not parse this message.")
            self.assertEqual(len(at.warning), 0)
            self.assertNotIn("parser bug", at.error[0].value)
            self.assertNotIn("RuntimeError", at.error[0].value)

    def test_unexpected_parser_exception_logs_sanitized_error(self):
        with patch(
            "app.parser.parse_message",
            side_effect=RuntimeError("SENTINEL_PARSER_EXC_113"),
        ):
            with self.assertLogs("discord_traders", level="ERROR") as captured:
                at = AppTest.from_file("app/streamlit_app.py")
                at.run()
                at.text_area[0].input(_SAMPLE_MESSAGE).run()
                at.button[0].click().run()

            joined = "\n".join(captured.output)
            self.assertIn("message parsing failed", joined)
            self.assertIn("RuntimeError", joined)
            self.assertNotIn("SENTINEL_PARSER_EXC_113", joined)
            self.assertTrue(
                all(record.levelno < logging.CRITICAL for record in captured.records)
            )

    def test_valid_empty_parse_result_still_shows_nothing_found_message(self):
        at = AppTest.from_file("app/streamlit_app.py")
        at.run()
        at.text_area[0].input("just some random text").run()
        at.button[0].click().run()

        self.assertEqual(len(at.error), 0)
        self.assertEqual(len(at.warning), 1)
        self.assertEqual(
            at.warning[0].value, "No trade signals found in this message."
        )


class SubmissionFailureLoggingTests(_SubmissionWorkflowTestCase):
    """Covers Milestone 2D.2: sanitized ERROR diagnostics and safe UI
    messages for every Submit-workflow failure category, and confirmation
    that sensitive values never reach any log record."""

    def test_database_init_failure_logs_sanitized_error(self):
        init_p = patch(
            "database.db.initialize_database",
            side_effect=OSError("SENTINEL_INIT_EXC_221"),
        )
        conn_p = patch("database.db.get_connection", return_value=MagicMock())
        service_p = patch("database.service.TradeService")
        with init_p, conn_p, service_p:
            with self.assertLogs("discord_traders", level="ERROR") as captured:
                at = AppTest.from_file("app/streamlit_app.py")
                self._run_to_review(at)
                self._submit(at)

            self.assertEqual(
                at.error[0].value, "Could not save the message to the database."
            )
            joined = "\n".join(captured.output)
            self.assertIn("message submission failed", joined)
            self.assertIn("OSError", joined)
            self.assertNotIn("SENTINEL_INIT_EXC_221", joined)

    def test_connection_failure_logs_sanitized_error(self):
        init_p = patch("database.db.initialize_database")
        conn_p = patch(
            "database.db.get_connection",
            side_effect=sqlite3.OperationalError("SENTINEL_CONN_EXC_332"),
        )
        service_p = patch("database.service.TradeService")
        with init_p, conn_p, service_p:
            with self.assertLogs("discord_traders", level="ERROR") as captured:
                at = AppTest.from_file("app/streamlit_app.py")
                self._run_to_review(at)
                self._submit(at)

            self.assertEqual(
                at.error[0].value, "Could not save the message to the database."
            )
            joined = "\n".join(captured.output)
            self.assertIn("message submission failed", joined)
            self.assertIn("OperationalError", joined)
            self.assertNotIn("SENTINEL_CONN_EXC_332", joined)

    def test_ingestion_failure_logs_sanitized_error(self):
        mock_conn = MagicMock()
        init_p, conn_p, service_p = self._patches(mock_conn)
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value
            mock_service.ingest_message.side_effect = ValueError(
                "SENTINEL_INGEST_EXC_443"
            )

            with self.assertLogs("discord_traders", level="ERROR") as captured:
                at = AppTest.from_file("app/streamlit_app.py")
                self._run_to_review(at)
                self._submit(at)

            self.assertEqual(
                at.error[0].value, "Could not save the message to the database."
            )
            joined = "\n".join(captured.output)
            self.assertIn("message submission failed", joined)
            self.assertIn("ValueError", joined)
            self.assertNotIn("SENTINEL_INGEST_EXC_443", joined)

    def test_unexpected_exception_caught_by_safety_net_logs_error_not_critical(self):
        mock_conn = MagicMock()
        init_p, conn_p, service_p = self._patches(mock_conn)
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value
            mock_service.ingest_message.side_effect = RuntimeError(
                "SENTINEL_UNEXPECTED_EXC_554"
            )

            with self.assertLogs("discord_traders", level="ERROR") as captured:
                at = AppTest.from_file("app/streamlit_app.py")
                self._run_to_review(at)
                self._submit(at)

            self.assertEqual(
                at.error[0].value, "Could not save the message to the database."
            )
            joined = "\n".join(captured.output)
            self.assertIn("message submission failed", joined)
            self.assertIn("RuntimeError", joined)
            self.assertNotIn("SENTINEL_UNEXPECTED_EXC_554", joined)
            self.assertTrue(
                all(record.levelno < logging.CRITICAL for record in captured.records)
            )

    def test_duplicate_advisory_logged_as_warning_not_error(self):
        mock_conn = MagicMock()
        init_p, conn_p, service_p = self._patches(mock_conn)
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value
            mock_service.ingest_message.return_value = {
                "trade_signals": [{"id": 1}],
                "duplicate_warnings": ["Possible duplicate: 1 matching trade signal(s)"],
            }

            with self.assertLogs("discord_traders", level="WARNING") as captured:
                at = AppTest.from_file("app/streamlit_app.py")
                self._run_to_review(at)
                self._submit(at)

            self.assertEqual(len(at.error), 0)
            self.assertTrue(
                all(record.levelno < logging.ERROR for record in captured.records)
            )
            joined = "\n".join(captured.output)
            self.assertIn("Duplicate advisory returned for 1 signal(s)", joined)

    def test_successful_submission_logs_exclude_sensitive_values(self):
        sentinel_raw_text = (
            "BTO SPY 450C 7/19/2025 @3.25 SENTINEL_RAW_TOKEN_665"
        )
        mock_conn = MagicMock()
        init_p, conn_p, service_p = self._patches(mock_conn)
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value
            mock_service.ingest_message.return_value = {
                "trade_signals": [{"id": 1}],
                "duplicate_warnings": [None],
            }

            with self.assertLogs("discord_traders", level="INFO") as captured:
                at = AppTest.from_file("app/streamlit_app.py")
                self._run_to_review(at, raw_text=sentinel_raw_text)
                self._submit(
                    at,
                    trader_name="SENTINEL_TRADER_NAME_776",
                    external_trader_id="SENTINEL_EXTERNAL_ID_887",
                )

            joined = "\n".join(captured.output)
            self.assertNotIn("SENTINEL_RAW_TOKEN_665", joined)
            self.assertNotIn("SENTINEL_TRADER_NAME_776", joined)
            self.assertNotIn("SENTINEL_EXTERNAL_ID_887", joined)
            self.assertNotIn(resolve_database_path(), joined)

    def test_failed_submission_logs_exclude_sensitive_values_including_exception_message(
        self,
    ):
        sentinel_raw_text = (
            "BTO SPY 450C 7/19/2025 @3.25 SENTINEL_RAW_TOKEN_998"
        )
        mock_conn = MagicMock()
        init_p, conn_p, service_p = self._patches(mock_conn)
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value
            mock_service.ingest_message.side_effect = ValueError(
                "SENTINEL_EXC_MSG_223"
            )

            with self.assertLogs("discord_traders", level="ERROR") as captured:
                at = AppTest.from_file("app/streamlit_app.py")
                self._run_to_review(at, raw_text=sentinel_raw_text)
                self._submit(
                    at,
                    trader_name="SENTINEL_TRADER_NAME_334",
                    external_trader_id="SENTINEL_EXTERNAL_ID_445",
                )

            joined = "\n".join(captured.output)
            self.assertNotIn("SENTINEL_RAW_TOKEN_998", joined)
            self.assertNotIn("SENTINEL_TRADER_NAME_334", joined)
            self.assertNotIn("SENTINEL_EXTERNAL_ID_445", joined)
            self.assertNotIn("SENTINEL_EXC_MSG_223", joined)

    def test_review_state_remains_after_failed_submission(self):
        mock_conn = MagicMock()
        init_p, conn_p, service_p = self._patches(mock_conn)
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value
            mock_service.ingest_message.side_effect = ValueError("bad input")

            at = AppTest.from_file("app/streamlit_app.py")
            self._run_to_review(at)
            self._submit(at)

            self.assertEqual(len(at.error), 1)
            self.assertEqual(len(at.text_input), 2)

    def test_connection_closed_after_oserror_class_failure(self):
        mock_conn = MagicMock()
        mock_conn.commit.side_effect = OSError("disk full")
        init_p, conn_p, service_p = self._patches(mock_conn)
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value
            mock_service.ingest_message.return_value = {
                "trade_signals": [{"id": 1}],
                "duplicate_warnings": [None],
            }

            at = AppTest.from_file("app/streamlit_app.py")
            self._run_to_review(at)
            self._submit(at)

            mock_conn.rollback.assert_called_once()
            mock_conn.close.assert_called_once()
            self.assertEqual(
                at.error[0].value, "Could not save the message to the database."
            )


class LoggingHandlerRerunTests(unittest.TestCase):
    """Covers Milestone 2D.2: console-handler idempotency across the
    repeated module re-execution Streamlit performs on every rerun."""

    def test_streamlit_reruns_do_not_duplicate_console_handler(self):
        at = AppTest.from_file("app/streamlit_app.py")
        at.run()
        at.text_area[0].input(_SAMPLE_MESSAGE).run()
        at.button[0].click().run()
        at.text_area[0].input(_SAMPLE_MESSAGE + " again").run()
        at.button[0].click().run()

        logger = logging.getLogger("discord_traders")
        console_handlers = [
            handler
            for handler in logger.handlers
            if getattr(handler, logging_config._CONSOLE_HANDLER_MARKER, False)
        ]
        self.assertEqual(len(console_handlers), 1)


class FileLoggingFailureSafetyTests(unittest.TestCase):
    """Covers Milestone 2D.2: a file-logging configuration failure never
    blocks the Submit workflow."""

    def test_file_logging_resolution_failure_does_not_block_submission(self):
        mock_conn = MagicMock()
        with patch("database.db.initialize_database"), patch(
            "database.db.get_connection", return_value=mock_conn
        ), patch("database.service.TradeService") as mock_service_cls, patch(
            "app.logging_config.resolve_log_path",
            side_effect=RuntimeError("SENTINEL_LOG_PATH_EXC_556"),
        ):
            mock_service = mock_service_cls.return_value
            mock_service.ingest_message.return_value = {
                "trade_signals": [{"id": 1}],
                "duplicate_warnings": [None],
            }

            at = AppTest.from_file("app/streamlit_app.py")
            at.run()
            at.text_area[0].input(_SAMPLE_MESSAGE).run()
            at.button[0].click().run()
            at.text_input[0].input("alice").run()
            at.text_input[1].input("disc-123").run()
            at.button[1].click().run()

            self.assertEqual(len(at.exception), 0)
            self.assertEqual(len(at.error), 0)
            success_values = [s.value for s in at.success]
            self.assertTrue(any("Saved 1 trade signal" in v for v in success_values))


class CreateBackupControlTests(unittest.TestCase):
    """Covers Milestone 2D.3: the minimal "Create Backup" UI control.

    database.backup.create_backup is patched throughout - no real SQLite
    database or backup file is touched here (reserved for
    tests/test_backup.py). No restore control exists in the UI.
    """

    def _click_create_backup(self, at):
        backup_button = next(b for b in at.button if b.label == "Create Backup")
        return backup_button.click().run()

    def test_create_backup_button_renders(self):
        at = AppTest.from_file("app/streamlit_app.py")
        at.run()

        self.assertIn("Create Backup", {b.label for b in at.button})

    def test_no_restore_control_is_present(self):
        at = AppTest.from_file("app/streamlit_app.py")
        at.run()

        self.assertEqual(
            {b.label for b in at.button}, {"Parse Message", "Create Backup"}
        )

    def test_successful_backup_shows_fixed_success_message(self):
        with patch("app.logging_config.configure_file_logging"), patch(
            "database.backup.create_backup", return_value="C:/sentinel/backups/x.db"
        ):
            at = AppTest.from_file("app/streamlit_app.py")
            at.run()
            at = self._click_create_backup(at)

        self.assertEqual(len(at.error), 0)
        self.assertEqual(len(at.success), 1)
        self.assertEqual(at.success[0].value, "Database backup created successfully.")
        self.assertNotIn("sentinel", at.success[0].value)

    def test_failed_backup_shows_fixed_message_and_logs_sanitized(self):
        with patch("app.logging_config.configure_file_logging"), patch(
            "database.backup.create_backup",
            side_effect=OSError("SENTINEL_BACKUP_EXC_991"),
        ):
            with self.assertLogs("discord_traders", level="ERROR") as captured:
                at = AppTest.from_file("app/streamlit_app.py")
                at.run()
                at = self._click_create_backup(at)

        self.assertEqual(len(at.success), 0)
        self.assertEqual(len(at.error), 1)
        self.assertEqual(at.error[0].value, "Could not create a database backup.")

        joined = "\n".join(captured.output)
        self.assertIn("database backup failed", joined)
        self.assertIn("OSError", joined)
        self.assertNotIn("SENTINEL_BACKUP_EXC_991", joined)
        self.assertTrue(
            all(record.levelno < logging.CRITICAL for record in captured.records)
        )


class WorkflowNavigationTests(unittest.TestCase):
    """Covers Milestone 2D.4: sidebar-selected workflow navigation.

    Uses a plain if/elif on st.sidebar.radio()'s value, not st.tabs(), so
    only the selected workflow's code executes on a given rerun.
    """

    def test_sidebar_offers_all_three_workflows(self):
        # Recovery Milestone R8a: the sidebar's options list itself must
        # change to add "Trader Performance" - this is the one approved
        # change to Manual Message Entry/Review Signals' own behavior;
        # every other assertion in this file proves the other two
        # workflows are otherwise unaffected.
        at = AppTest.from_file("app/streamlit_app.py")
        at.run()

        self.assertEqual(
            at.sidebar.radio[0].options,
            ["Manual Message Entry", "Review Signals", "Trader Performance"],
        )

    def test_manual_message_entry_is_the_default_selection(self):
        at = AppTest.from_file("app/streamlit_app.py")
        at.run()

        self.assertEqual(at.sidebar.radio[0].value, "Manual Message Entry")
        self.assertIn("Parse Message", {b.label for b in at.button})

    def test_create_backup_appears_only_in_manual_message_entry(self):
        at = AppTest.from_file("app/streamlit_app.py")
        at.run()
        self.assertIn("Create Backup", {b.label for b in at.button})

        at = at.sidebar.radio[0].set_value("Review Signals").run()
        self.assertNotIn("Create Backup", {b.label for b in at.button})

    def test_manual_message_entry_does_not_invoke_review_method(self):
        with patch("database.service.TradeService") as mock_service_cls:
            at = AppTest.from_file("app/streamlit_app.py")
            at.run()

            mock_service_cls.return_value.list_trade_signals_for_review.assert_not_called()

    def test_review_signals_does_not_invoke_parser_ingest_or_backup(self):
        with patch("app.parser.parse_message") as mock_parse, patch(
            "database.service.TradeService"
        ) as mock_service_cls, patch("database.backup.create_backup") as mock_backup:
            mock_service_cls.return_value.list_trade_signals_for_review.return_value = []

            at = AppTest.from_file("app/streamlit_app.py")
            at.run()
            at.sidebar.radio[0].set_value("Review Signals").run()

            mock_parse.assert_not_called()
            mock_service_cls.return_value.ingest_message.assert_not_called()
            mock_backup.assert_not_called()

    def test_review_signals_renders_no_write_capable_buttons(self):
        with patch("database.db.get_connection", return_value=MagicMock()), patch(
            "database.service.TradeService"
        ) as mock_service_cls:
            mock_service_cls.return_value.list_trade_signals_for_review.return_value = []

            with tempfile.TemporaryDirectory() as tmp:
                db_path = Path(tmp) / "discord_traders.db"
                db_path.write_bytes(b"")
                with patch.dict(
                    os.environ, {"DISCORD_TRADERS_DB_PATH": str(db_path)}, clear=False
                ):
                    at = AppTest.from_file("app/streamlit_app.py")
                    at.run()
                    at.sidebar.radio[0].set_value("Review Signals").run()

            self.assertEqual(len(at.button), 0)


class ReviewSignalsDatabaseNotFoundTests(unittest.TestCase):
    """Covers Milestone 2D.4: the Review workflow never creates the
    database. No patches on database.db are used here - the real
    Path.exists() check and the real absence of get_connection()/
    initialize_database() calls are exercised directly."""

    def test_nonexistent_database_shows_fixed_message_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "nested" / "discord_traders.db"
            self.assertFalse(db_path.exists())
            self.assertFalse(db_path.parent.exists())

            with patch.dict(
                os.environ, {"DISCORD_TRADERS_DB_PATH": str(db_path)}, clear=False
            ):
                at = AppTest.from_file("app/streamlit_app.py")
                at.run()
                at.sidebar.radio[0].set_value("Review Signals").run()

            self.assertEqual(len(at.info), 1)
            self.assertEqual(at.info[0].value, "No trade signals found.")
            self.assertEqual(len(at.error), 0)
            self.assertFalse(db_path.exists())
            self.assertFalse(db_path.parent.exists())


class ReviewSignalsEmptyAndFailureTests(unittest.TestCase):
    """Covers Milestone 2D.4: empty-result and query-failure states, once
    the database file already exists. database.db.get_connection and
    database.service.TradeService are patched so no real SQLite database
    is touched here (reserved for tests/test_review_integration.py)."""

    def _run_review(self, mock_service_cls):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "discord_traders.db"
            db_path.write_bytes(b"")
            with patch.dict(
                os.environ, {"DISCORD_TRADERS_DB_PATH": str(db_path)}, clear=False
            ), patch("database.db.get_connection", return_value=MagicMock()):
                at = AppTest.from_file("app/streamlit_app.py")
                at.run()
                at = at.sidebar.radio[0].set_value("Review Signals").run()
        return at

    def test_empty_result_shows_fixed_message(self):
        with patch("database.service.TradeService") as mock_service_cls:
            mock_service_cls.return_value.list_trade_signals_for_review.return_value = []
            at = self._run_review(mock_service_cls)

        self.assertEqual(len(at.info), 1)
        self.assertEqual(at.info[0].value, "No trade signals found.")
        self.assertEqual(len(at.error), 0)

    def test_query_failure_shows_fixed_message_and_logs_sanitized(self):
        with patch("database.service.TradeService") as mock_service_cls:
            mock_service_cls.return_value.list_trade_signals_for_review.side_effect = (
                sqlite3.OperationalError("SENTINEL_REVIEW_EXC_442")
            )
            with self.assertLogs("discord_traders", level="ERROR") as captured:
                at = self._run_review(mock_service_cls)

        self.assertEqual(len(at.error), 1)
        self.assertEqual(at.error[0].value, "Could not load stored trade signals.")
        self.assertEqual(len(at.info), 0)

        joined = "\n".join(captured.output)
        self.assertIn("stored-signal review failed", joined)
        self.assertIn("OperationalError", joined)
        self.assertNotIn("SENTINEL_REVIEW_EXC_442", joined)
        self.assertTrue(
            all(record.levelno < logging.CRITICAL for record in captured.records)
        )


class ReviewSignalsDisplayTests(unittest.TestCase):
    """Covers Milestone 2D.4: summary table, signal-ID selection, and the
    read-only detail view. database.db.get_connection and
    database.service.TradeService are patched for the lifetime of each
    test (via setUp/addCleanup, not a `with` block scoped only to the
    first .run()) because a later widget interaction (selecting a signal
    ID) triggers Streamlit to re-execute app.py's entire module body, and
    the patches must still be active for that second execution too."""

    _SIGNALS = [
        {
            "id": 2,
            "symbol": "AAPL",
            "action": "STC",
            "option_type": "put",
            "price": "1.10",
            "expiration": "2025-12-15",
            "position_size": "5 contracts",
            "created_at": "2026-07-15 11:00:00",
            "updated_at": "2026-07-15 11:00:00",
            "source_name": "discord",
            "trader_name": "bob",
            "external_trader_id": None,
            "raw_text": "STC AAPL 190P 12/15/2025 @1.10",
        },
        {
            "id": 1,
            "symbol": "SPY",
            "action": "BTO",
            "option_type": "call",
            "price": "3.25",
            "expiration": "2026-12-18",
            "position_size": "10 contracts",
            "created_at": "2026-07-15 10:00:00",
            "updated_at": "2026-07-15 10:00:00",
            "source_name": "discord",
            "trader_name": "alice",
            "external_trader_id": "disc-alice",
            "raw_text": "BTO SPY 450C 7/19/2025 @3.25 10 contracts",
        },
    ]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        db_path = Path(self._tmp.name) / "discord_traders.db"
        db_path.write_bytes(b"")

        env_patcher = patch.dict(
            os.environ, {"DISCORD_TRADERS_DB_PATH": str(db_path)}, clear=False
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

        conn_patcher = patch("database.db.get_connection", return_value=MagicMock())
        conn_patcher.start()
        self.addCleanup(conn_patcher.stop)

        service_patcher = patch("database.service.TradeService")
        mock_service_cls = service_patcher.start()
        self.addCleanup(service_patcher.stop)
        mock_service_cls.return_value.list_trade_signals_for_review.return_value = (
            self._SIGNALS
        )
        # Milestone 2D.5: the Review Signals branch also queries audit
        # history for the selected signal; an empty history keeps this
        # 2D.4-era fixture's dataframe-count assumptions (one dataframe:
        # the summary table) unaffected by that unrelated addition.
        mock_service_cls.return_value.list_trade_signal_audit_history.return_value = []
        self.mock_service_cls = mock_service_cls

    def _open_review(self):
        at = AppTest.from_file("app/streamlit_app.py")
        at.run()
        return at.sidebar.radio[0].set_value("Review Signals").run()

    def test_summary_table_shows_expected_fields_newest_first(self):
        at = self._open_review()

        self.assertEqual(len(at.dataframe), 1)
        df = at.dataframe[0].value
        self.assertEqual(list(df["ID"]), [2, 1])
        self.assertEqual(list(df["Symbol"]), ["AAPL", "SPY"])
        self.assertEqual(list(df["Source"]), ["discord", "discord"])
        self.assertEqual(list(df["Trader"]), ["bob", "alice"])
        self.assertEqual(list(df["Price"]), ["1.10", "3.25"])

    def test_raw_message_not_present_in_summary_table(self):
        at = self._open_review()

        df = at.dataframe[0].value
        self.assertNotIn("raw_text", df.columns)
        self.assertNotIn("Raw", " ".join(df.columns))

    def test_signal_id_selectbox_offers_newest_first_ids(self):
        at = self._open_review()

        self.assertEqual(at.selectbox[0].options, ["2", "1"])

    def test_selecting_a_signal_shows_correct_detail_including_raw_text(self):
        at = self._open_review()

        at = at.selectbox[0].set_value(1).run()

        detail_text = "\n".join(element.value for element in at.markdown)
        self.assertIn("Source: discord", detail_text)
        self.assertIn("Trader: alice", detail_text)
        self.assertIn("External trader ID: disc-alice", detail_text)
        self.assertIn("Symbol: SPY", detail_text)
        self.assertEqual(len(at.text_area), 1)
        self.assertEqual(at.text_area[0].value, "BTO SPY 450C 7/19/2025 @3.25 10 contracts")
        self.assertTrue(at.text_area[0].disabled)

    def test_raw_message_only_appears_in_detail_view_not_elsewhere(self):
        at = self._open_review()

        df = at.dataframe[0].value
        self.assertNotIn("BTO SPY 450C 7/19/2025 @3.25 10 contracts", df.to_string())

        at = at.selectbox[0].set_value(1).run()
        self.assertEqual(at.text_area[0].value, "BTO SPY 450C 7/19/2025 @3.25 10 contracts")

    def test_nullable_external_trader_id_displays_safely(self):
        at = self._open_review()

        at = at.selectbox[0].set_value(2).run()

        detail_text = "\n".join(element.value for element in at.markdown)
        self.assertNotIn("External trader ID", detail_text)

    def test_no_delete_control_and_correction_gated_behind_correct_signal(self):
        # Milestone 2D.5: "Correct Signal" is now an approved entry point
        # into correction mode, but no delete control exists and no
        # correction form (Save/Cancel, etc.) renders until it is clicked.
        at = self._open_review()

        self.assertEqual({b.label for b in at.button}, {"Correct Signal"})
        self.assertEqual(len(at.text_input), 3)  # source, trader, symbol filters only


class ReviewSignalsDateFilterTests(unittest.TestCase):
    """Covers Milestone 2D.4: the optional date filter's enable/disable
    control and the exact "YYYY-MM-DD" value passed to
    TradeService.list_trade_signals_for_review(). Patches (env var,
    connection, TradeService, parser, backup) are started in setUp and
    stopped via addCleanup so they remain active across every .run() call
    in a test, including the checkbox-enable and date-selection reruns."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        db_path = Path(self._tmp.name) / "discord_traders.db"
        db_path.write_bytes(b"")

        env_patcher = patch.dict(
            os.environ, {"DISCORD_TRADERS_DB_PATH": str(db_path)}, clear=False
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

        conn_patcher = patch("database.db.get_connection", return_value=MagicMock())
        conn_patcher.start()
        self.addCleanup(conn_patcher.stop)

        service_patcher = patch("database.service.TradeService")
        self.mock_service_cls = service_patcher.start()
        self.addCleanup(service_patcher.stop)
        self.mock_service_cls.return_value.list_trade_signals_for_review.return_value = []

        parse_patcher = patch("app.parser.parse_message")
        self.mock_parse = parse_patcher.start()
        self.addCleanup(parse_patcher.stop)

        backup_patcher = patch("database.backup.create_backup")
        self.mock_backup = backup_patcher.start()
        self.addCleanup(backup_patcher.stop)

    def _open_review(self):
        at = AppTest.from_file("app/streamlit_app.py")
        at.run()
        return at.sidebar.radio[0].set_value("Review Signals").run()

    def _last_call_kwargs(self):
        return (
            self.mock_service_cls.return_value.list_trade_signals_for_review.call_args.kwargs
        )

    def test_date_input_hidden_until_filter_by_date_is_enabled(self):
        at = self._open_review()

        self.assertEqual(len(at.checkbox), 1)
        self.assertEqual(at.checkbox[0].label, "Filter by date")
        self.assertFalse(at.checkbox[0].value)
        self.assertEqual(len(at.date_input), 0)
        self.assertIsNone(self._last_call_kwargs()["date"])

        at = at.checkbox[0].set_value(True).run()

        self.assertEqual(len(at.date_input), 1)

    def test_selected_date_passed_to_service_as_exact_yyyy_mm_dd(self):
        at = self._open_review()
        at = at.checkbox[0].set_value(True).run()

        at = at.date_input[0].set_value(date(2026, 7, 20)).run()

        self.assertEqual(self._last_call_kwargs()["date"], "2026-07-20")

    def test_combined_source_trader_symbol_filters_passed_alongside_date(self):
        at = self._open_review()
        at = at.text_input[0].input("discord").run()
        at = at.text_input[1].input("alice").run()
        at = at.text_input[2].input("spy").run()
        at = at.checkbox[0].set_value(True).run()
        at = at.date_input[0].set_value(date(2026, 7, 20)).run()

        call_kwargs = self._last_call_kwargs()
        self.assertEqual(call_kwargs["source_name"], "discord")
        self.assertEqual(call_kwargs["trader_name"], "alice")
        self.assertEqual(call_kwargs["symbol"], "spy")
        self.assertEqual(call_kwargs["date"], "2026-07-20")

    def test_no_parser_ingestion_backup_or_write_during_date_filter_interaction(self):
        at = self._open_review()
        at = at.checkbox[0].set_value(True).run()
        at = at.date_input[0].set_value(date(2026, 7, 20)).run()

        self.mock_parse.assert_not_called()
        self.mock_service_cls.return_value.ingest_message.assert_not_called()
        self.mock_backup.assert_not_called()
        self.assertEqual(len(at.button), 0)


class CorrectSignalWorkflowTests(unittest.TestCase):
    """Covers Milestone 2D.5 / Recovery Milestone R6.5b: the "Correct
    Signal" workflow inside Review Signals, migrated in R6.5b from
    TradeService.update_trade_signal()'s controlled-correction mode to the
    lifecycle-safe TradeService.correct_trade_signal(). Patches (env var,
    connection, TradeService, parser, backup) are started in setUp/stopped
    via addCleanup, so they remain active across every .run() call in a
    test, including the multiple reruns a correction/cancel/save
    interaction requires."""

    _SIGNALS = [
        {
            "id": 2,
            "symbol": "AAPL",
            "action": "STC",
            "option_type": "put",
            "price": "1.10",
            "expiration": "2025-12-15",
            "position_size": "5 contracts",
            "created_at": "2026-07-15 11:00:00",
            "updated_at": "2026-07-15 11:00:00",
            "source_name": "discord",
            "trader_name": "bob",
            "external_trader_id": None,
            "raw_text": "STC AAPL 190P 12/15/2025 @1.10",
        },
        {
            "id": 1,
            "symbol": "SPY",
            "action": "BTO",
            "option_type": "call",
            "price": "3.25",
            "expiration": "2026-12-18",
            "position_size": "10 contracts",
            "created_at": "2026-07-15 10:00:00",
            "updated_at": "2026-07-15 10:00:00",
            "source_name": "discord",
            "trader_name": "alice",
            "external_trader_id": "disc-alice",
            "raw_text": "BTO SPY 450C 7/19/2025 @3.25 10 contracts",
        },
        {
            "id": 3,
            "symbol": "TSLA",
            "action": "BOUGHT",
            "option_type": "call",
            "price": "9.50",
            "expiration": "2026-11-20",
            "position_size": "3 contracts",
            "created_at": "2026-07-15 12:00:00",
            "updated_at": "2026-07-15 12:00:00",
            "source_name": "discord",
            "trader_name": "carol",
            "external_trader_id": "disc-carol",
            "raw_text": "BOUGHT TSLA 900C 11/20/2026 @9.50 3 contracts",
        },
        {
            "id": 4,
            "symbol": "NVDA",
            "action": "SOLD",
            "option_type": "put",
            "price": "2.20",
            "expiration": "2026-10-16",
            "position_size": "2 contracts",
            "created_at": "2026-07-15 13:00:00",
            "updated_at": "2026-07-15 13:00:00",
            "source_name": "discord",
            "trader_name": "dave",
            "external_trader_id": "disc-dave",
            "raw_text": "SOLD NVDA 900P 10/16/2026 @2.20 2 contracts",
        },
        {
            "id": 5,
            "symbol": "IWM",
            "action": "BTO",
            "option_type": "spread",
            "price": "1.00",
            "expiration": "2026-09-18",
            "position_size": None,
            "created_at": "2026-07-15 14:00:00",
            "updated_at": "2026-07-15 14:00:00",
            "source_name": "discord",
            "trader_name": "erin",
            "external_trader_id": "disc-erin",
            "raw_text": "BTO IWM SPREAD 9/18/2026 @1.00",
        },
    ]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        db_path = Path(self._tmp.name) / "discord_traders.db"
        db_path.write_bytes(b"")

        env_patcher = patch.dict(
            os.environ, {"DISCORD_TRADERS_DB_PATH": str(db_path)}, clear=False
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

        self.mock_conn = MagicMock()
        conn_patcher = patch("database.db.get_connection", return_value=self.mock_conn)
        conn_patcher.start()
        self.addCleanup(conn_patcher.stop)

        service_patcher = patch("database.service.TradeService")
        self.mock_service_cls = service_patcher.start()
        self.addCleanup(service_patcher.stop)
        self.mock_service_cls.return_value.list_trade_signals_for_review.return_value = (
            self._SIGNALS
        )
        self.mock_service_cls.return_value.list_trade_signal_audit_history.return_value = (
            []
        )

        parse_patcher = patch("app.parser.parse_message")
        self.mock_parse = parse_patcher.start()
        self.addCleanup(parse_patcher.stop)

        backup_patcher = patch("database.backup.create_backup")
        self.mock_backup = backup_patcher.start()
        self.addCleanup(backup_patcher.stop)

        # Save Correction calls configure_file_logging(), exactly like
        # Submit; patched out entirely (not just redirected via an env
        # var) so no real file I/O of any kind is attempted here.
        file_log_patcher = patch("app.logging_config.configure_file_logging")
        file_log_patcher.start()
        self.addCleanup(file_log_patcher.stop)

    def _open_review(self):
        at = AppTest.from_file("app/streamlit_app.py")
        at.run()
        return at.sidebar.radio[0].set_value("Review Signals").run()

    def _enter_correction_mode(self, at, signal_id=1):
        selectbox = next(
            w for w in at.selectbox if w.label == "Select a signal ID for details"
        )
        if selectbox.value != signal_id:
            at = selectbox.set_value(signal_id).run()
        at = next(b for b in at.button if b.label == "Correct Signal").click().run()
        return at

    def _confirm_and_save(self, at):
        confirm = next(w for w in at.checkbox if w.label == "I confirm this correction")
        at = confirm.set_value(True).run()
        save_btn = next(b for b in at.button if b.label == "Save Correction")
        return save_btn.click().run()

    def _attempt_save_with_exception(self, at, exc, symbol="QQQ"):
        """Enter symbol, confirm, and click Save Correction while
        correct_trade_signal() is set to raise exc."""
        self.mock_service_cls.return_value.correct_trade_signal.side_effect = exc
        symbol_input = next(w for w in at.text_input if w.label == "Corrected symbol")
        at = symbol_input.input(symbol).run()
        return self._confirm_and_save(at)

    def test_correct_signal_button_present_only_when_signal_selected(self):
        at = self._open_review()

        self.assertIn("Correct Signal", {b.label for b in at.button})

    def test_action_choices_exactly_approved_for_standard_action(self):
        at = self._enter_correction_mode(self._open_review())

        action_box = next(w for w in at.selectbox if w.label == "Corrected action")
        self.assertEqual(
            action_box.options, ["BTO", "STC", "BTC", "STO", "BUY", "SELL"]
        )

    def test_option_type_choices_exactly_approved_for_standard_option_type(self):
        at = self._enter_correction_mode(self._open_review())

        option_box = next(
            w for w in at.selectbox if w.label == "Corrected option type"
        )
        self.assertEqual(option_box.options, ["", "call", "put"])

    def test_current_values_prefilled(self):
        at = self._enter_correction_mode(self._open_review())

        symbol_input = next(w for w in at.text_input if w.label == "Corrected symbol")
        self.assertEqual(symbol_input.value, "SPY")
        price_input = next(w for w in at.text_input if w.label == "Corrected price")
        self.assertEqual(price_input.value, "3.25")
        expiration_input = next(
            w
            for w in at.text_input
            if w.label == "Corrected expiration (YYYY-MM-DD)"
        )
        self.assertEqual(expiration_input.value, "2026-12-18")
        position_input = next(
            w for w in at.text_input if w.label == "Corrected position size"
        )
        self.assertEqual(position_input.value, "10 contracts")
        action_box = next(w for w in at.selectbox if w.label == "Corrected action")
        self.assertEqual(action_box.value, "BTO")
        option_box = next(
            w for w in at.selectbox if w.label == "Corrected option type"
        )
        self.assertEqual(option_box.value, "call")

    def test_immutable_fields_not_editable(self):
        at = self._enter_correction_mode(self._open_review())

        labels = {w.label for w in at.text_input}
        for forbidden in (
            "raw_message_id",
            "trader_id",
            "id",
            "created_at",
            "updated_at",
        ):
            self.assertNotIn(forbidden, labels)
        # Exactly the 3 filters plus the 4 correction text inputs (symbol,
        # price, expiration, position size) - action/option type are
        # selectboxes, not text inputs.
        self.assertEqual(len(at.text_input), 7)

    # -----------------------------------------------------------------
    # Out-of-list action/option-type preservation (Recovery Milestone
    # R6.5b): the Recovery extractor persists actions (BOUGHT/SOLD) and,
    # defensively, could persist option types outside the standard fixed
    # choice list. The correction form must offer the persisted value,
    # select it by default, and never silently substitute a different
    # value (e.g. index 0) in its place.
    # -----------------------------------------------------------------

    def test_bought_action_appears_in_options_and_is_selected_by_default(self):
        at = self._enter_correction_mode(self._open_review(), signal_id=3)

        action_box = next(w for w in at.selectbox if w.label == "Corrected action")
        self.assertIn("BOUGHT", action_box.options)
        self.assertEqual(action_box.value, "BOUGHT")
        # The six standard choices remain present alongside it.
        for standard in ("BTO", "STC", "BTC", "STO", "BUY", "SELL"):
            self.assertIn(standard, action_box.options)

    def test_bought_action_preserved_through_price_only_correction(self):
        at = self._enter_correction_mode(self._open_review(), signal_id=3)

        price_input = next(w for w in at.text_input if w.label == "Corrected price")
        at = price_input.input("11.00").run()
        at = self._confirm_and_save(at)

        self.mock_service_cls.return_value.correct_trade_signal.assert_called_once()
        call = self.mock_service_cls.return_value.correct_trade_signal.call_args
        self.assertEqual(call.args[0], 3)
        self.assertEqual(call.kwargs["action"], "BOUGHT")
        self.assertNotEqual(call.kwargs["action"], "BTO")

    def test_sold_action_appears_in_options_and_is_selected_by_default(self):
        at = self._enter_correction_mode(self._open_review(), signal_id=4)

        action_box = next(w for w in at.selectbox if w.label == "Corrected action")
        self.assertIn("SOLD", action_box.options)
        self.assertEqual(action_box.value, "SOLD")

    def test_sold_action_preserved_through_price_only_correction(self):
        at = self._enter_correction_mode(self._open_review(), signal_id=4)

        price_input = next(w for w in at.text_input if w.label == "Corrected price")
        at = price_input.input("2.75").run()
        at = self._confirm_and_save(at)

        self.mock_service_cls.return_value.correct_trade_signal.assert_called_once()
        call = self.mock_service_cls.return_value.correct_trade_signal.call_args
        self.assertEqual(call.args[0], 4)
        self.assertEqual(call.kwargs["action"], "SOLD")
        self.assertNotEqual(call.kwargs["action"], "STC")

    def test_out_of_list_option_type_appears_and_is_selected_by_default(self):
        at = self._enter_correction_mode(self._open_review(), signal_id=5)

        option_box = next(
            w for w in at.selectbox if w.label == "Corrected option type"
        )
        self.assertIn("spread", option_box.options)
        self.assertEqual(option_box.value, "spread")
        for standard in ("", "call", "put"):
            self.assertIn(standard, option_box.options)

    def test_out_of_list_option_type_preserved_through_price_only_correction(self):
        at = self._enter_correction_mode(self._open_review(), signal_id=5)

        price_input = next(w for w in at.text_input if w.label == "Corrected price")
        at = price_input.input("1.50").run()
        at = self._confirm_and_save(at)

        self.mock_service_cls.return_value.correct_trade_signal.assert_called_once()
        call = self.mock_service_cls.return_value.correct_trade_signal.call_args
        self.assertEqual(call.args[0], 5)
        self.assertEqual(call.kwargs["option_type"], "spread")

    # -----------------------------------------------------------------
    # Missing confirmation / malformed client-side input
    # -----------------------------------------------------------------

    def test_missing_confirmation_preserves_form_and_entered_values(self):
        at = self._enter_correction_mode(self._open_review())

        symbol_input = next(w for w in at.text_input if w.label == "Corrected symbol")
        at = symbol_input.input("QQQ").run()
        save_btn = next(b for b in at.button if b.label == "Save Correction")
        at = save_btn.click().run()

        self.mock_service_cls.return_value.correct_trade_signal.assert_not_called()
        self.assertEqual(len(at.error), 1)
        self.assertEqual(
            at.error[0].value,
            "Please enter a valid correction that changes at least one field.",
        )
        self.assertIn("Save Correction", {b.label for b in at.button})
        symbol_input = next(w for w in at.text_input if w.label == "Corrected symbol")
        self.assertEqual(symbol_input.value, "QQQ")

    def test_malformed_price_preserves_form_and_entered_values(self):
        at = self._enter_correction_mode(self._open_review())

        price_input = next(w for w in at.text_input if w.label == "Corrected price")
        at = price_input.input("not-a-number").run()
        at = self._confirm_and_save(at)

        self.mock_service_cls.return_value.correct_trade_signal.assert_not_called()
        self.assertEqual(
            at.error[0].value,
            "Please enter a valid correction that changes at least one field.",
        )
        self.assertIn("Save Correction", {b.label for b in at.button})
        price_input = next(w for w in at.text_input if w.label == "Corrected price")
        self.assertEqual(price_input.value, "not-a-number")

    def test_malformed_expiration_preserves_form_and_entered_values(self):
        at = self._enter_correction_mode(self._open_review())

        expiration_input = next(
            w
            for w in at.text_input
            if w.label == "Corrected expiration (YYYY-MM-DD)"
        )
        at = expiration_input.input("not-a-date").run()
        at = self._confirm_and_save(at)

        self.mock_service_cls.return_value.correct_trade_signal.assert_not_called()
        self.assertEqual(
            at.error[0].value,
            "Please enter a valid correction that changes at least one field.",
        )
        self.assertIn("Save Correction", {b.label for b in at.button})
        expiration_input = next(
            w
            for w in at.text_input
            if w.label == "Corrected expiration (YYYY-MM-DD)"
        )
        self.assertEqual(expiration_input.value, "not-a-date")

    def test_validation_failure_keeps_correction_mode_active(self):
        at = self._enter_correction_mode(self._open_review())

        save_btn = next(b for b in at.button if b.label == "Save Correction")
        at = save_btn.click().run()

        self.assertIn("Save Correction", {b.label for b in at.button})
        self.assertIn("Cancel", {b.label for b in at.button})

    # -----------------------------------------------------------------
    # State-clearing paths: success, Cancel, signal change, filter change
    # -----------------------------------------------------------------

    def test_cancel_performs_no_write_and_clears_state(self):
        at = self._enter_correction_mode(self._open_review())

        cancel_btn = next(b for b in at.button if b.label == "Cancel")
        at = cancel_btn.click().run()

        self.mock_service_cls.return_value.correct_trade_signal.assert_not_called()

        # Steady-state rerun (no new correction click) confirms the mode
        # actually cleared, not just a transitional render artifact.
        source_filter = next(
            w for w in at.text_input if w.label == "Source (exact match)"
        )
        at = source_filter.input("").run()
        self.assertIn("Correct Signal", {b.label for b in at.button})
        self.assertNotIn("Save Correction", {b.label for b in at.button})

    def test_selected_signal_change_clears_correction_state(self):
        at = self._enter_correction_mode(self._open_review(), signal_id=1)

        selectbox = next(
            w for w in at.selectbox if w.label == "Select a signal ID for details"
        )
        at = selectbox.set_value(2).run()

        self.assertNotIn("Save Correction", {b.label for b in at.button})
        self.assertIn("Correct Signal", {b.label for b in at.button})

    def test_filter_change_removing_selected_signal_clears_correction_state(self):
        at = self._enter_correction_mode(self._open_review(), signal_id=1)

        # Simulate the filtered result set narrowing to exclude signal 1.
        self.mock_service_cls.return_value.list_trade_signals_for_review.return_value = [
            self._SIGNALS[0]
        ]
        symbol_filter = next(w for w in at.text_input if w.label == "Symbol")
        at = symbol_filter.input("AAPL").run()

        self.assertNotIn("Save Correction", {b.label for b in at.button})

    # -----------------------------------------------------------------
    # Conflict message (StaleTradeSignalError / TradeSignalNotFoundError):
    # Recovery Milestone R6.5b now PRESERVES the form on these conflicts
    # (the prior 2D.5 UI cleared it) - see docs section 7 of the approved
    # R6.5b design.
    # -----------------------------------------------------------------

    def test_stale_conflict_shows_fixed_message_and_preserves_state(self):
        at = self._enter_correction_mode(self._open_review())
        at = self._attempt_save_with_exception(at, StaleTradeSignalError("stale"))

        self.assertEqual(
            at.error[0].value,
            "This trade signal changed or is no longer available. "
            "Reload it before correcting.",
        )
        self.assertIn("Save Correction", {b.label for b in at.button})
        symbol_input = next(w for w in at.text_input if w.label == "Corrected symbol")
        self.assertEqual(symbol_input.value, "QQQ")
        self.mock_conn.rollback.assert_not_called()
        self.mock_conn.commit.assert_not_called()

    def test_not_found_conflict_uses_the_same_fixed_message_and_preserves_state(self):
        at = self._enter_correction_mode(self._open_review())
        at = self._attempt_save_with_exception(at, TradeSignalNotFoundError("missing"))

        self.assertEqual(
            at.error[0].value,
            "This trade signal changed or is no longer available. "
            "Reload it before correcting.",
        )
        self.assertIn("Save Correction", {b.label for b in at.button})
        symbol_input = next(w for w in at.text_input if w.label == "Corrected symbol")
        self.assertEqual(symbol_input.value, "QQQ")
        self.mock_conn.rollback.assert_not_called()
        self.mock_conn.commit.assert_not_called()

    # -----------------------------------------------------------------
    # Validation message: service-side no-op and lifecycle-unsafe action
    # -----------------------------------------------------------------

    def test_service_no_op_rejection_shows_validation_message_and_preserves_state(self):
        at = self._enter_correction_mode(self._open_review())
        at = self._attempt_save_with_exception(
            at, ValueError("A correction must change at least one field.")
        )

        self.assertEqual(
            at.error[0].value,
            "Please enter a valid correction that changes at least one field.",
        )
        self.assertIn("Save Correction", {b.label for b in at.button})
        self.mock_conn.rollback.assert_not_called()

    def test_lifecycle_unsafe_action_correction_shows_validation_message_and_preserves_state(
        self,
    ):
        at = self._enter_correction_mode(self._open_review())
        at = self._attempt_save_with_exception(
            at, LifecycleUnsafeCorrectionError("cannot change action")
        )

        self.assertEqual(
            at.error[0].value,
            "Please enter a valid correction that changes at least one field.",
        )
        self.assertIn("Save Correction", {b.label for b in at.button})
        self.mock_conn.rollback.assert_not_called()

    # -----------------------------------------------------------------
    # Generic failure message: lifecycle/database/OS/runtime/unexpected
    # -----------------------------------------------------------------

    def test_lifecycle_integrity_error_shows_generic_message_and_preserves_state(self):
        at = self._enter_correction_mode(self._open_review())
        at = self._attempt_save_with_exception(
            at, LifecycleIntegrityError(["fake violation for testing"])
        )

        self.assertEqual(at.error[0].value, "Could not save the trade signal correction.")
        self.assertIn("Save Correction", {b.label for b in at.button})
        self.mock_conn.rollback.assert_not_called()

    def test_lifecycle_snapshot_error_shows_generic_message_and_preserves_state(self):
        at = self._enter_correction_mode(self._open_review())
        at = self._attempt_save_with_exception(
            at, LifecycleSnapshotError(1, 2, "malformed snapshot")
        )

        self.assertEqual(at.error[0].value, "Could not save the trade signal correction.")
        self.assertIn("Save Correction", {b.label for b in at.button})
        self.mock_conn.rollback.assert_not_called()

    def test_type_error_shows_generic_message_and_preserves_state(self):
        at = self._enter_correction_mode(self._open_review())
        at = self._attempt_save_with_exception(at, TypeError("price must be Decimal"))

        self.assertEqual(at.error[0].value, "Could not save the trade signal correction.")
        self.assertIn("Save Correction", {b.label for b in at.button})
        self.mock_conn.rollback.assert_not_called()

    def test_sqlite_error_shows_generic_message_and_preserves_state(self):
        at = self._enter_correction_mode(self._open_review())
        at = self._attempt_save_with_exception(
            at, sqlite3.OperationalError("SENTINEL_CORRECTION_EXC_771")
        )

        self.assertEqual(at.error[0].value, "Could not save the trade signal correction.")
        self.assertIn("Save Correction", {b.label for b in at.button})
        self.mock_conn.rollback.assert_not_called()

    def test_os_error_shows_generic_message_and_preserves_state(self):
        at = self._enter_correction_mode(self._open_review())
        at = self._attempt_save_with_exception(at, OSError("disk full"))

        self.assertEqual(at.error[0].value, "Could not save the trade signal correction.")
        self.assertIn("Save Correction", {b.label for b in at.button})
        self.mock_conn.rollback.assert_not_called()

    def test_runtime_error_shows_generic_message_and_preserves_state(self):
        at = self._enter_correction_mode(self._open_review())
        at = self._attempt_save_with_exception(
            at, RuntimeError("connection already has pending work")
        )

        self.assertEqual(at.error[0].value, "Could not save the trade signal correction.")
        self.assertIn("Save Correction", {b.label for b in at.button})
        self.mock_conn.rollback.assert_not_called()

    def test_unexpected_exception_shows_generic_message_and_preserves_state(self):
        at = self._enter_correction_mode(self._open_review())
        at = self._attempt_save_with_exception(at, KeyError("unexpected"))

        self.assertEqual(at.error[0].value, "Could not save the trade signal correction.")
        self.assertIn("Save Correction", {b.label for b in at.button})
        self.mock_conn.rollback.assert_not_called()

    def test_persistence_failure_logs_sanitized(self):
        at = self._enter_correction_mode(self._open_review())

        with self.assertLogs("discord_traders", level="ERROR") as captured:
            at = self._attempt_save_with_exception(
                at, sqlite3.OperationalError("SENTINEL_CORRECTION_EXC_771")
            )

        self.assertEqual(at.error[0].value, "Could not save the trade signal correction.")
        joined = "\n".join(captured.output)
        self.assertIn("trade signal correction failed", joined)
        self.assertIn("OperationalError", joined)
        self.assertNotIn("SENTINEL_CORRECTION_EXC_771", joined)
        self.assertNotIn("QQQ", joined)
        self.assertTrue(
            all(record.levelno < logging.CRITICAL for record in captured.records)
        )

    # -----------------------------------------------------------------
    # Success path
    # -----------------------------------------------------------------

    def test_valid_correction_calls_correct_trade_signal_with_exact_arguments(self):
        at = self._enter_correction_mode(self._open_review())

        symbol_input = next(w for w in at.text_input if w.label == "Corrected symbol")
        at = symbol_input.input("QQQ").run()
        at = self._confirm_and_save(at)

        self.assertEqual(at.success[0].value, "Trade signal correction saved.")

        self.mock_service_cls.return_value.correct_trade_signal.assert_called_once()
        self.mock_service_cls.return_value.update_trade_signal.assert_not_called()

        call = self.mock_service_cls.return_value.correct_trade_signal.call_args
        self.assertEqual(call.args[0], 1)
        self.assertEqual(
            call.kwargs["expected_current_values"],
            {
                "symbol": "SPY",
                "action": "BTO",
                "option_type": "call",
                "price": Decimal("3.25"),
                "expiration": "2026-12-18",
                "position_size": "10 contracts",
            },
        )
        proposed_fields = set(call.kwargs) - {"expected_current_values"}
        self.assertEqual(
            proposed_fields,
            {"symbol", "action", "option_type", "price", "expiration", "position_size"},
        )
        self.assertEqual(call.kwargs["symbol"], "QQQ")

        self.mock_conn.commit.assert_not_called()
        self.mock_conn.rollback.assert_not_called()

    def test_success_clears_correction_state(self):
        at = self._enter_correction_mode(self._open_review())

        symbol_input = next(w for w in at.text_input if w.label == "Corrected symbol")
        at = symbol_input.input("QQQ").run()
        at = self._confirm_and_save(at)

        source_filter = next(
            w for w in at.text_input if w.label == "Source (exact match)"
        )
        at = source_filter.input("").run()
        self.assertIn("Correct Signal", {b.label for b in at.button})
        self.assertNotIn("Save Correction", {b.label for b in at.button})

    # -----------------------------------------------------------------
    # Audit history, read-only invariants, and no cross-workflow leakage
    # -----------------------------------------------------------------

    def test_audit_history_displayed_newest_first(self):
        history = [
            {
                "id": 5,
                "edited_at": "2026-07-16 09:00:00",
                "symbol": "QQQ",
                "action": "BTO",
                "option_type": "call",
                "price": "3.00",
                "expiration": "2026-12-18",
                "position_size": "10 contracts",
            },
            {
                "id": 3,
                "edited_at": "2026-07-15 09:00:00",
                "symbol": "SPY",
                "action": "BTO",
                "option_type": "call",
                "price": "3.25",
                "expiration": "2026-12-18",
                "position_size": "10 contracts",
            },
        ]
        self.mock_service_cls.return_value.list_trade_signal_audit_history.return_value = (
            history
        )

        at = self._open_review()

        self.assertEqual(len(at.dataframe), 2)
        df = at.dataframe[1].value
        self.assertEqual(list(df["Audit ID"]), [5, 3])

    def test_audit_history_failure_shows_fixed_message_and_logs_sanitized(self):
        self.mock_service_cls.return_value.list_trade_signal_audit_history.side_effect = (
            AuditHistoryError("SENTINEL_AUDIT_EXC_882")
        )

        with self.assertLogs("discord_traders", level="ERROR") as captured:
            at = self._open_review()

        self.assertIn("Could not load correction history.", [e.value for e in at.error])
        joined = "\n".join(captured.output)
        self.assertNotIn("SENTINEL_AUDIT_EXC_882", joined)

    def test_raw_message_remains_read_only_in_correction_mode(self):
        at = self._enter_correction_mode(self._open_review())

        raw_text_area = next(w for w in at.text_area if w.label == "Raw message")
        self.assertTrue(raw_text_area.disabled)

    def test_no_delete_control_in_correction_mode(self):
        at = self._enter_correction_mode(self._open_review())

        self.assertNotIn("Delete", {b.label for b in at.button})

    def test_parser_ingestion_backup_never_execute_during_correction(self):
        at = self._enter_correction_mode(self._open_review())

        symbol_input = next(w for w in at.text_input if w.label == "Corrected symbol")
        at = symbol_input.input("QQQ").run()
        at = self._confirm_and_save(at)

        self.mock_parse.assert_not_called()
        self.mock_service_cls.return_value.ingest_message.assert_not_called()
        self.mock_backup.assert_not_called()


class TraderPerformanceWorkflowTests(unittest.TestCase):
    """Covers Recovery Milestone R8a: the Trader Performance dashboard
    workflow. database.db.get_connection and database.service.TradeService
    are patched for the lifetime of each test (setUp/addCleanup, not a
    `with` block scoped only to the first .run()), matching
    ReviewSignalsDisplayTests' own established pattern, since trader/
    filter/lifecycle selection each trigger a full module rerun and the
    patches must still be active for every one of them."""

    _SUMMARIES = [
        {
            "trader_id": 1, "trader_name": "TC", "total_lifecycle_count": 2,
            "open_count": 1, "partially_closed_count": 0, "closed_count": 1,
            "orphan_count": 0, "unresolved_count": 0, "invalid_count": 0,
            "snapshot_error_count": 0, "eligible_lifecycle_count": 1,
            "not_scored_count": 1, "winning_count": 1, "losing_count": 0,
            "breakeven_count": 0, "win_rate_pct": "100.000000",
            "loss_rate_pct": "0.000000", "breakeven_rate_pct": "0.000000",
            "average_gross_price_return_pct": "100.000000",
            "median_gross_price_return_pct": "100.000000",
            "average_winner_price_return_pct": "100.000000",
            "average_loser_price_return_pct": None,
            "all_current_lifecycle_ids": [1, 2], "eligible_lifecycle_ids": [1],
            "return_ineligible_lifecycle_ids": [2],
            "snapshot_error_lifecycle_ids": [],
            "exclusion_reason_counts": {"status_open": 1},
        },
        {
            "trader_id": 2, "trader_name": "TC", "total_lifecycle_count": 1,
            "open_count": 0, "partially_closed_count": 0, "closed_count": 0,
            "orphan_count": 0, "unresolved_count": 0, "invalid_count": 0,
            "snapshot_error_count": 1, "eligible_lifecycle_count": 0,
            "not_scored_count": 0, "winning_count": 0, "losing_count": 0,
            "breakeven_count": 0, "win_rate_pct": None,
            "loss_rate_pct": None, "breakeven_rate_pct": None,
            "average_gross_price_return_pct": None,
            "median_gross_price_return_pct": None,
            "average_winner_price_return_pct": None,
            "average_loser_price_return_pct": None,
            "all_current_lifecycle_ids": [3], "eligible_lifecycle_ids": [],
            "return_ineligible_lifecycle_ids": [],
            "snapshot_error_lifecycle_ids": [3],
            "exclusion_reason_counts": {},
        },
    ]

    _LIFECYCLE_RESULTS_TRADER_1 = [
        {
            "trade_lifecycle_id": 1, "trader_id": 1, "trader_name": "TC",
            "is_current": True, "superseded_at": None, "status": "closed",
            "outcome": "win", "direction": "long", "symbol": "IBM",
            "option_type": "call", "strike": "207.5", "expiration": "2026-07-24",
            "opened_at": "2026-07-20T10:00:00+00:00",
            "closed_at": "2026-07-21T10:00:00+00:00",
            "entry_price": "1.00", "terminal_exit_price": "2.00",
            "weighted_average_exit_price": "2.00",
            "exit_legs": [
                {
                    "trade_lifecycle_event_id": 10, "trade_signal_id": 100,
                    "sequence_index": 2, "event_type": "FULL_EXIT",
                    "consumed_fraction": "1", "exit_price": "2.00",
                },
            ],
            "gross_price_return_pct": "100.000000",
            "eligible_for_status_counts": True, "eligible_for_outcome_metrics": True,
            "eligible_for_return_metrics": True,
            "lifecycle_ambiguity_flags": [], "analytics_exclusion_reasons": [],
            "analytics_error_detail": None, "source_event_ids": [9, 10],
        },
        {
            "trade_lifecycle_id": 2, "trader_id": 1, "trader_name": "TC",
            "is_current": True, "superseded_at": None, "status": "open",
            "outcome": "not_scored", "direction": None, "symbol": "NVDA",
            "option_type": "call", "strike": "950", "expiration": "2026-08-15",
            "opened_at": "2026-07-22T10:00:00+00:00", "closed_at": None,
            "entry_price": "5.00", "terminal_exit_price": None,
            "weighted_average_exit_price": None,
            "exit_legs": [],
            "gross_price_return_pct": None,
            "eligible_for_status_counts": True, "eligible_for_outcome_metrics": False,
            "eligible_for_return_metrics": False,
            "lifecycle_ambiguity_flags": [], "analytics_exclusion_reasons": ["status_open"],
            "analytics_error_detail": None, "source_event_ids": [11],
        },
    ]

    _LIFECYCLE_RESULTS_TRADER_2 = [
        {
            "trade_lifecycle_id": 3, "trader_id": 2, "trader_name": "TC",
            "is_current": True, "superseded_at": None, "status": "closed",
            "outcome": "data_error", "direction": None, "symbol": "AVGO",
            "option_type": "call", "strike": "150", "expiration": "2026-09-01",
            "opened_at": None, "closed_at": None,
            "entry_price": None, "terminal_exit_price": None,
            "weighted_average_exit_price": None,
            "exit_legs": [],
            "gross_price_return_pct": None,
            "eligible_for_status_counts": True, "eligible_for_outcome_metrics": False,
            "eligible_for_return_metrics": False,
            "lifecycle_ambiguity_flags": [], "analytics_exclusion_reasons": [],
            "analytics_error_detail": "trade_lifecycle_id 3 has no membership events.",
            "source_event_ids": [],
        },
    ]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        db_path = Path(self._tmp.name) / "discord_traders.db"
        db_path.write_bytes(b"")

        env_patcher = patch.dict(
            os.environ, {"DISCORD_TRADERS_DB_PATH": str(db_path)}, clear=False
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

        self.mock_conn = MagicMock()
        conn_patcher = patch("database.db.get_connection", return_value=self.mock_conn)
        conn_patcher.start()
        self.addCleanup(conn_patcher.stop)

        service_patcher = patch("database.service.TradeService")
        mock_service_cls = service_patcher.start()
        self.addCleanup(service_patcher.stop)
        mock_service_cls.return_value.list_trader_performance_summaries.return_value = (
            self._SUMMARIES
        )

        def _fake_lifecycles(*, trader_id):
            if trader_id == 1:
                return self._LIFECYCLE_RESULTS_TRADER_1
            if trader_id == 2:
                return self._LIFECYCLE_RESULTS_TRADER_2
            return []

        mock_service_cls.return_value.list_current_trade_lifecycle_analytics.side_effect = (
            _fake_lifecycles
        )
        self.mock_service_cls = mock_service_cls

    def _open_dashboard(self):
        at = AppTest.from_file("app/streamlit_app.py")
        at.run()
        return at.sidebar.radio[0].set_value("Trader Performance").run()

    # -- summary table ---------------------------------------------------

    def test_summary_table_renders_expected_columns_and_row_count(self):
        at = self._open_dashboard()

        df = at.dataframe[0].value
        self.assertEqual(len(df), 2)
        for column in (
            # Recovery Milestone R8b adds "Meets Minimum Sample" to the
            # existing R8a column set - every other column is unchanged.
            "Trader", "Meets Minimum Sample", "Total Lifecycles", "Open",
            "Partially Closed", "Closed", "Orphan", "Unresolved", "Invalid",
            "Data Errors", "Eligible", "Not Scored", "Wins", "Losses",
            "Breakeven", "Win Rate", "Loss Rate", "Breakeven Rate",
            "Avg Return", "Median Return", "Avg Winner Return", "Avg Loser Return",
        ):
            self.assertIn(column, df.columns)

    def test_duplicate_trader_names_render_distinct_labels(self):
        at = self._open_dashboard()

        df = at.dataframe[0].value
        self.assertEqual(list(df["Trader"]), ["TC (ID 1)", "TC (ID 2)"])

    def test_none_percentage_fields_render_as_em_dash_not_zero(self):
        at = self._open_dashboard()

        df = at.dataframe[0].value
        row_two = df[df["Trader"] == "TC (ID 2)"].iloc[0]
        self.assertEqual(row_two["Win Rate"], "—")
        self.assertEqual(row_two["Avg Return"], "—")
        self.assertEqual(row_two["Avg Loser Return"], "—")

    def test_data_error_indicator_visible_without_verbatim_detail_in_summary(self):
        at = self._open_dashboard()

        df = at.dataframe[0].value
        row_one = df[df["Trader"] == "TC (ID 1)"].iloc[0]
        row_two = df[df["Trader"] == "TC (ID 2)"].iloc[0]
        self.assertEqual(row_one["Data Errors"], "0")
        self.assertEqual(row_two["Data Errors"], "⚠ 1")
        self.assertNotIn("has no membership events", df.to_string())

    def test_summary_order_reflects_ranking_not_raw_service_return_order(self):
        # Recovery Milestone R8b superseded R8a's "order always equals
        # service-return order" guarantee with the approved ranking
        # rules. Local fixture (never mutating the shared class-level
        # _SUMMARIES list), deliberately returned by the service in
        # descending-trader_id order (9 then 2) while both entries tie
        # on the default ranking metric (Average Return) and are both
        # below the default minimum-sample threshold - so the displayed
        # order can only match trader_id-ascending (2 then 9) if the UI
        # genuinely applies rank_trader_summaries()'s own tie-break
        # rule, not merely because it preserved whatever order the
        # service happened to return.
        template = self._SUMMARIES[0]
        local_summaries = [
            {**template, "trader_id": 9, "trader_name": "Zeta"},
            {**template, "trader_id": 2, "trader_name": "Alpha"},
        ]
        self.mock_service_cls.return_value.list_trader_performance_summaries.return_value = (
            local_summaries
        )

        at = self._open_dashboard()

        df = at.dataframe[0].value
        self.assertEqual(list(df["Trader"]), ["Alpha (ID 2)", "Zeta (ID 9)"])

    # -- Recovery Milestone R8b: ranking controls, minimum-sample -----------
    # threshold, and CSV export -----------------------------------------

    def test_ranking_controls_render_with_exact_labels_options_and_defaults(self):
        at = self._open_dashboard()

        self.assertEqual(at.selectbox[0].label, "Rank traders by")
        self.assertEqual(list(at.selectbox[0].options), list(SORT_METRIC_CHOICES))
        self.assertEqual(at.selectbox[0].value, "Average Return")

        self.assertEqual(at.selectbox[1].label, "Sort direction")
        self.assertEqual(list(at.selectbox[1].options), ["Descending", "Ascending"])
        self.assertEqual(at.selectbox[1].value, "Descending")

        self.assertEqual(at.number_input[0].label, "Minimum eligible lifecycles")
        self.assertEqual(at.number_input[0].value, 3)

    def test_default_ranking_uses_average_return_descending(self):
        # Local fixture: three qualifying traders (eligible >= default
        # threshold 3) with distinct average returns, returned by the
        # service in a scrambled (non-ranked) order - proves the
        # displayed order is genuinely descending by Average Return.
        template = self._SUMMARIES[0]
        local_summaries = [
            {
                **template, "trader_id": 1, "trader_name": "Low",
                "eligible_lifecycle_count": 3,
                "average_gross_price_return_pct": "10.000000",
            },
            {
                **template, "trader_id": 2, "trader_name": "High",
                "eligible_lifecycle_count": 3,
                "average_gross_price_return_pct": "90.000000",
            },
            {
                **template, "trader_id": 3, "trader_name": "Mid",
                "eligible_lifecycle_count": 3,
                "average_gross_price_return_pct": "50.000000",
            },
        ]
        self.mock_service_cls.return_value.list_trader_performance_summaries.return_value = (
            local_summaries
        )

        at = self._open_dashboard()

        df = at.dataframe[0].value
        self.assertEqual(
            list(df["Trader"]), ["High (ID 2)", "Mid (ID 3)", "Low (ID 1)"]
        )

    def test_ascending_direction_reverses_order(self):
        template = self._SUMMARIES[0]
        local_summaries = [
            {
                **template, "trader_id": 1, "trader_name": "Low",
                "eligible_lifecycle_count": 3,
                "average_gross_price_return_pct": "10.000000",
            },
            {
                **template, "trader_id": 2, "trader_name": "High",
                "eligible_lifecycle_count": 3,
                "average_gross_price_return_pct": "90.000000",
            },
        ]
        self.mock_service_cls.return_value.list_trader_performance_summaries.return_value = (
            local_summaries
        )

        at = self._open_dashboard()
        at = at.selectbox[1].set_value("Ascending").run()

        df = at.dataframe[0].value
        self.assertEqual(list(df["Trader"]), ["Low (ID 1)", "High (ID 2)"])

    def test_alternate_metric_selection_reorders_by_win_rate(self):
        template = self._SUMMARIES[0]
        local_summaries = [
            {
                **template, "trader_id": 1, "trader_name": "A",
                "eligible_lifecycle_count": 3,
                "average_gross_price_return_pct": "90.000000",
                "win_rate_pct": "10.000000",
            },
            {
                **template, "trader_id": 2, "trader_name": "B",
                "eligible_lifecycle_count": 3,
                "average_gross_price_return_pct": "10.000000",
                "win_rate_pct": "90.000000",
            },
        ]
        self.mock_service_cls.return_value.list_trader_performance_summaries.return_value = (
            local_summaries
        )

        at = self._open_dashboard()
        at = at.selectbox[0].set_value("Win Rate").run()

        df = at.dataframe[0].value
        self.assertEqual(list(df["Trader"]), ["B (ID 2)", "A (ID 1)"])

    def test_below_threshold_traders_ranked_after_qualifying_traders(self):
        template = self._SUMMARIES[0]
        local_summaries = [
            {
                **template, "trader_id": 1, "trader_name": "BelowButHighReturn",
                "eligible_lifecycle_count": 1,
                "average_gross_price_return_pct": "999.000000",
            },
            {
                **template, "trader_id": 2, "trader_name": "Qualifies",
                "eligible_lifecycle_count": 3,
                "average_gross_price_return_pct": "1.000000",
            },
        ]
        self.mock_service_cls.return_value.list_trader_performance_summaries.return_value = (
            local_summaries
        )

        at = self._open_dashboard()

        df = at.dataframe[0].value
        self.assertEqual(
            list(df["Trader"]), ["Qualifies (ID 2)", "BelowButHighReturn (ID 1)"]
        )

    def test_threshold_change_moves_trader_between_tiers(self):
        template = self._SUMMARIES[0]
        local_summaries = [
            {
                **template, "trader_id": 1, "trader_name": "TwoEligible",
                "eligible_lifecycle_count": 2,
                "average_gross_price_return_pct": "50.000000",
            },
            {
                **template, "trader_id": 2, "trader_name": "ThreeEligible",
                "eligible_lifecycle_count": 3,
                "average_gross_price_return_pct": "10.000000",
            },
        ]
        self.mock_service_cls.return_value.list_trader_performance_summaries.return_value = (
            local_summaries
        )

        at = self._open_dashboard()
        df_before = at.dataframe[0].value
        self.assertEqual(
            list(df_before["Trader"]), ["ThreeEligible (ID 2)", "TwoEligible (ID 1)"]
        )

        at = at.number_input[0].set_value(2).run()
        df_after = at.dataframe[0].value
        self.assertEqual(
            list(df_after["Trader"]), ["TwoEligible (ID 1)", "ThreeEligible (ID 2)"]
        )

    def test_meets_minimum_sample_indicator_values(self):
        # Default fixture: trader 1 has eligible_lifecycle_count=1,
        # trader 2 has eligible_lifecycle_count=0 - both below the
        # default threshold of 3.
        at = self._open_dashboard()

        df = at.dataframe[0].value
        row_one = df[df["Trader"] == "TC (ID 1)"].iloc[0]
        row_two = df[df["Trader"] == "TC (ID 2)"].iloc[0]
        self.assertEqual(row_one["Meets Minimum Sample"], "No (1 < 3)")
        self.assertEqual(row_two["Meets Minimum Sample"], "No (0 < 3)")

    def test_trader_selector_options_follow_ranked_order(self):
        template = self._SUMMARIES[0]
        local_summaries = [
            {
                **template, "trader_id": 9, "trader_name": "Zeta",
                "eligible_lifecycle_count": 3,
                "average_gross_price_return_pct": "10.000000",
            },
            {
                **template, "trader_id": 2, "trader_name": "Alpha",
                "eligible_lifecycle_count": 3,
                "average_gross_price_return_pct": "90.000000",
            },
        ]
        self.mock_service_cls.return_value.list_trader_performance_summaries.return_value = (
            local_summaries
        )

        at = self._open_dashboard()

        # .options holds the format_func-rendered labels, not the raw
        # trader_id values - the label order itself proves the
        # selector's options follow ranked_summaries' order (Alpha
        # ranked first for its 90% return, ahead of Zeta's 10%).
        self.assertEqual(
            list(at.selectbox[2].options), ["Alpha (ID 2)", "Zeta (ID 9)"]
        )
        self.assertEqual(at.selectbox[2].value, 2)

    def test_selected_trader_remains_valid_after_changing_ranking_controls(self):
        at = self._open_dashboard()
        at = at.selectbox[2].set_value(2).run()

        at = at.selectbox[1].set_value("Ascending").run()

        self.assertEqual(at.selectbox[2].value, 2)

    def test_summary_csv_download_button_exact_label_and_filename(self):
        with patch("streamlit.download_button") as mock_download_button:
            mock_download_button.return_value = False
            self._open_dashboard()

        calls = {c.args[0]: c.kwargs for c in mock_download_button.call_args_list}
        self.assertIn("Download Trader Summary CSV", calls)
        self.assertEqual(
            calls["Download Trader Summary CSV"]["file_name"],
            "trader_performance_summary.csv",
        )
        self.assertEqual(calls["Download Trader Summary CSV"]["mime"], "text/csv")

    def test_drilldown_csv_download_button_exact_label_and_filename(self):
        with patch("streamlit.download_button") as mock_download_button:
            mock_download_button.return_value = False
            self._open_dashboard()

        calls = {c.args[0]: c.kwargs for c in mock_download_button.call_args_list}
        self.assertIn("Download Lifecycle Drill-down CSV", calls)
        self.assertEqual(
            calls["Download Lifecycle Drill-down CSV"]["file_name"],
            "trader_lifecycle_drilldown_1.csv",
        )
        self.assertEqual(
            calls["Download Lifecycle Drill-down CSV"]["mime"], "text/csv"
        )

    def test_summary_csv_exact_content(self):
        with patch("streamlit.download_button") as mock_download_button:
            mock_download_button.return_value = False
            self._open_dashboard()

        ranked = rank_trader_summaries(
            self._SUMMARIES, sort_metric="Average Return", descending=True,
            min_eligible_lifecycles=3,
        )
        expected_rows = build_summary_csv_rows(ranked, min_eligible_lifecycles=3)
        expected_csv = rows_to_csv_string(expected_rows, SUMMARY_CSV_FIELDNAMES)

        calls = {c.args[0]: c.kwargs for c in mock_download_button.call_args_list}
        self.assertEqual(calls["Download Trader Summary CSV"]["data"], expected_csv)

    def test_drilldown_csv_exact_content(self):
        with patch("streamlit.download_button") as mock_download_button:
            mock_download_button.return_value = False
            self._open_dashboard()

        expected_rows = build_lifecycle_csv_rows(self._LIFECYCLE_RESULTS_TRADER_1)
        expected_csv = rows_to_csv_string(expected_rows, LIFECYCLE_CSV_FIELDNAMES)

        calls = {c.args[0]: c.kwargs for c in mock_download_button.call_args_list}
        self.assertEqual(
            calls["Download Lifecycle Drill-down CSV"]["data"], expected_csv
        )

    def test_csv_buttons_absent_when_database_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_path = Path(tmp) / "does_not_exist.db"
            with patch.dict(
                os.environ, {"DISCORD_TRADERS_DB_PATH": str(missing_path)}, clear=False
            ), patch("database.db.get_connection"), patch(
                "database.db.initialize_database"
            ), patch("streamlit.download_button") as mock_download_button:
                at = AppTest.from_file("app/streamlit_app.py")
                at.run()
                at = at.sidebar.radio[0].set_value("Trader Performance").run()

        mock_download_button.assert_not_called()

    def test_csv_buttons_absent_when_summaries_empty(self):
        self.mock_service_cls.return_value.list_trader_performance_summaries.return_value = (
            []
        )
        with patch("streamlit.download_button") as mock_download_button:
            self._open_dashboard()
        mock_download_button.assert_not_called()

    def test_csv_buttons_absent_when_summary_load_fails(self):
        self.mock_service_cls.return_value.list_trader_performance_summaries.side_effect = (
            sqlite3.OperationalError("SENTINEL_DASH_R8B_SUMMARY_EXC")
        )
        with patch("streamlit.download_button") as mock_download_button:
            self._open_dashboard()
        mock_download_button.assert_not_called()

    def test_drilldown_csv_button_absent_when_drilldown_load_fails(self):
        self.mock_service_cls.return_value.list_current_trade_lifecycle_analytics.side_effect = (
            sqlite3.OperationalError("SENTINEL_DASH_R8B_DRILLDOWN_EXC")
        )
        with patch("streamlit.download_button") as mock_download_button:
            mock_download_button.return_value = False
            self._open_dashboard()

        # The summary CSV button still renders (summaries loaded fine);
        # only the drilldown button is absent.
        calls = {c.args[0]: c.kwargs for c in mock_download_button.call_args_list}
        self.assertIn("Download Trader Summary CSV", calls)
        self.assertNotIn("Download Lifecycle Drill-down CSV", calls)

    def test_drilldown_csv_button_absent_when_no_lifecycles_match_filters(self):
        with patch("streamlit.download_button") as mock_download_button:
            mock_download_button.return_value = False
            at = self._open_dashboard()
            # Reset here: the initial unfiltered render already showed
            # the drilldown button once - only the post-filter rerun's
            # calls matter for this assertion.
            mock_download_button.reset_mock()
            at.text_input[0].set_value("ZZZZ").run()

        calls = {c.args[0]: c.kwargs for c in mock_download_button.call_args_list}
        self.assertIn("Download Trader Summary CSV", calls)
        self.assertNotIn("Download Lifecycle Drill-down CSV", calls)

    def test_ranking_and_csv_export_never_write_to_database(self):
        at = self._open_dashboard()
        at = at.selectbox[1].set_value("Ascending").run()
        at = at.number_input[0].set_value(1).run()

        self.mock_conn.commit.assert_not_called()
        self.mock_conn.rollback.assert_not_called()

    # -- empty/missing/failure states -------------------------------------

    def test_missing_database_shows_empty_message_and_opens_no_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_path = Path(tmp) / "does_not_exist.db"
            with patch.dict(
                os.environ, {"DISCORD_TRADERS_DB_PATH": str(missing_path)}, clear=False
            ), patch("database.db.get_connection") as mock_get_connection, patch(
                "database.db.initialize_database"
            ) as mock_init:
                at = AppTest.from_file("app/streamlit_app.py")
                at.run()
                at = at.sidebar.radio[0].set_value("Trader Performance").run()

        self.assertEqual(len(at.info), 1)
        self.assertEqual(at.info[0].value, "No trader performance data found.")
        mock_get_connection.assert_not_called()
        mock_init.assert_not_called()

    def test_empty_summary_shows_fixed_message(self):
        self.mock_service_cls.return_value.list_trader_performance_summaries.return_value = (
            []
        )
        at = self._open_dashboard()

        self.assertEqual(len(at.info), 1)
        self.assertEqual(at.info[0].value, "No trader performance data found.")

    def test_summary_load_failure_shows_fixed_message_and_logs_sanitized(self):
        self.mock_service_cls.return_value.list_trader_performance_summaries.side_effect = (
            sqlite3.OperationalError("SENTINEL_DASH_SUMMARY_EXC_991")
        )
        with self.assertLogs("discord_traders", level="ERROR") as captured:
            at = self._open_dashboard()

        self.assertEqual(len(at.error), 1)
        self.assertEqual(at.error[0].value, "Could not load trader performance data.")
        joined = "\n".join(captured.output)
        self.assertNotIn("SENTINEL_DASH_SUMMARY_EXC_991", joined)
        self.assertTrue(
            all(record.levelno < logging.CRITICAL for record in captured.records)
        )

    def test_drilldown_load_failure_shows_fixed_message_and_logs_sanitized(self):
        self.mock_service_cls.return_value.list_current_trade_lifecycle_analytics.side_effect = (
            sqlite3.OperationalError("SENTINEL_DASH_DRILLDOWN_EXC_552")
        )
        with self.assertLogs("discord_traders", level="ERROR") as captured:
            at = self._open_dashboard()

        self.assertEqual(len(at.error), 1)
        self.assertEqual(
            at.error[0].value, "Could not load lifecycle details for this trader."
        )
        joined = "\n".join(captured.output)
        self.assertNotIn("SENTINEL_DASH_DRILLDOWN_EXC_552", joined)

    def test_no_lifecycles_match_filters_shows_message(self):
        at = self._open_dashboard()

        at = at.text_input[0].set_value("ZZZZ").run()

        self.assertEqual(len(at.info), 1)
        self.assertEqual(at.info[0].value, "No lifecycles match the current filters.")

    # -- trader selection and drill-down -----------------------------------

    def test_trader_selector_defaults_to_first_available_trader(self):
        at = self._open_dashboard()

        # selectbox[0]/[1] are the R8b "Rank traders by"/"Sort
        # direction" controls; selectbox[2] is the trader selector.
        self.assertEqual(at.selectbox[2].value, 1)

    def test_selecting_trader_invokes_drilldown_with_correct_trader_id(self):
        at = self._open_dashboard()

        at.selectbox[2].set_value(2).run()

        self.mock_service_cls.return_value.list_current_trade_lifecycle_analytics.assert_called_with(
            trader_id=2
        )

    def test_drilldown_table_renders_expected_columns(self):
        at = self._open_dashboard()

        df = at.dataframe[1].value
        for column in (
            "Lifecycle ID", "Symbol", "Option Type", "Strike", "Expiration",
            "Status", "Outcome", "Direction", "Entry Price", "Terminal Exit Price",
            "Weighted Avg Exit Price", "Return", "Opened At", "Closed At",
            "Ambiguity Flags", "Exclusion Reasons", "Data Error",
        ):
            self.assertIn(column, df.columns)
        self.assertNotIn("Error Detail", df.columns)

    def test_status_filter_narrows_drilldown_rows(self):
        at = self._open_dashboard()

        at = at.multiselect[0].set_value(["open"]).run()

        df = at.dataframe[1].value
        self.assertEqual(list(df["Lifecycle ID"]), [2])

    def test_outcome_filter_narrows_drilldown_rows(self):
        at = self._open_dashboard()

        at = at.multiselect[1].set_value(["win"]).run()

        df = at.dataframe[1].value
        self.assertEqual(list(df["Lifecycle ID"]), [1])

    def test_symbol_filter_exact_uppercase_match(self):
        at = self._open_dashboard()

        at = at.text_input[0].set_value("ibm").run()

        df = at.dataframe[1].value
        self.assertEqual(list(df["Lifecycle ID"]), [1])

    def test_combined_filters_apply_together(self):
        # Local fixture (never mutating the shared class-level
        # _LIFECYCLE_RESULTS_TRADER_1 list): status=["closed"] alone
        # matches two rows (1, 2), outcome=["win"] alone also matches
        # two different rows (1, 3) - only their intersection can ever
        # produce the single expected row, proving genuine AND
        # combination rather than one filter happening to be sufficient
        # by itself.
        template = self._LIFECYCLE_RESULTS_TRADER_1[0]
        local_results = [
            {**template, "trade_lifecycle_id": 1, "status": "closed", "outcome": "win"},
            {**template, "trade_lifecycle_id": 2, "status": "closed", "outcome": "loss"},
            {**template, "trade_lifecycle_id": 3, "status": "open", "outcome": "win"},
            {
                **template, "trade_lifecycle_id": 4, "status": "open",
                "outcome": "not_scored",
            },
        ]
        self.mock_service_cls.return_value.list_current_trade_lifecycle_analytics.side_effect = (
            lambda *, trader_id: local_results
        )

        at = self._open_dashboard()

        at.multiselect[0].set_value(["closed"])
        at = at.multiselect[1].set_value(["win"]).run()

        df = at.dataframe[1].value
        self.assertEqual(list(df["Lifecycle ID"]), [1])

    def test_exclusion_reasons_visible_in_drilldown(self):
        at = self._open_dashboard()

        df = at.dataframe[1].value
        not_scored_row = df[df["Lifecycle ID"] == 2].iloc[0]
        self.assertEqual(not_scored_row["Exclusion Reasons"], "status_open")

    def test_ambiguity_flags_visible_in_drilldown(self):
        # Local fixture (never mutating the shared class-level list) -
        # no existing fixture lifecycle carries a non-empty
        # lifecycle_ambiguity_flags value, so the real st.dataframe()
        # column rendering for that case was previously proven only at
        # the pure-formatting unit level (test_dashboard_formatting.py),
        # never through the actual Streamlit wiring.
        template = self._LIFECYCLE_RESULTS_TRADER_1[0]
        local_results = [
            {
                **template,
                "trade_lifecycle_id": 99,
                "lifecycle_ambiguity_flags": ["ambiguous_add_no_open_position"],
            },
        ]
        self.mock_service_cls.return_value.list_current_trade_lifecycle_analytics.side_effect = (
            lambda *, trader_id: local_results
        )

        at = self._open_dashboard()

        df = at.dataframe[1].value
        row = df[df["Lifecycle ID"] == 99].iloc[0]
        self.assertEqual(row["Ambiguity Flags"], "ambiguous_add_no_open_position")

    # -- lifecycle detail --------------------------------------------------

    def test_lifecycle_detail_shows_exit_legs(self):
        at = self._open_dashboard()

        at = at.selectbox[3].set_value(1).run()

        df = at.dataframe[2].value
        self.assertEqual(list(df["Event Type"]), ["FULL_EXIT"])
        self.assertEqual(list(df["Consumed Fraction"]), ["1"])
        self.assertEqual(list(df["Exit Price"]), ["2.00"])
        self.assertEqual(list(df["Sequence Index"]), [2])

    def test_lifecycle_detail_shows_verbatim_error_detail_for_data_error_lifecycle(self):
        at = self._open_dashboard()

        at = at.selectbox[2].set_value(2).run()
        at = at.selectbox[3].set_value(3).run()

        detail_text = "\n".join(element.value for element in at.markdown)
        self.assertIn(
            "trade_lifecycle_id 3 has no membership events.", detail_text
        )

    def test_lifecycle_detail_shows_em_dash_for_normal_lifecycle(self):
        at = self._open_dashboard()

        at = at.selectbox[3].set_value(1).run()

        detail_text = "\n".join(element.value for element in at.markdown)
        self.assertIn("Data Error Detail: —", detail_text)

    def test_open_lifecycle_with_no_exit_legs_shows_message_not_empty_table(self):
        at = self._open_dashboard()

        at = at.selectbox[3].set_value(2).run()

        detail_text = "\n".join(element.value for element in at.markdown)
        self.assertIn("No exit events for this lifecycle.", detail_text)
        self.assertEqual(len(at.dataframe), 2)  # summary + drilldown only

    # -- stale-selection safety ---------------------------------------------

    def test_selected_trader_no_longer_present_is_cleared_without_raising(self):
        at = self._open_dashboard()
        at = at.selectbox[2].set_value(2).run()

        self.mock_service_cls.return_value.list_trader_performance_summaries.return_value = (
            [self._SUMMARIES[0]]
        )
        at = at.run()

        self.assertEqual(at.selectbox[2].value, 1)

    def test_selected_lifecycle_no_longer_matching_filters_is_cleared_without_raising(
        self,
    ):
        at = self._open_dashboard()
        at = at.selectbox[3].set_value(2).run()

        at = at.multiselect[0].set_value(["closed"]).run()

        self.assertEqual(list(at.dataframe[1].value["Lifecycle ID"]), [1])
        self.assertEqual(at.selectbox[3].value, 1)

    # -- isolation from other workflows --------------------------------------

    def test_trader_performance_does_not_invoke_other_workflow_methods(self):
        at = self._open_dashboard()

        self.mock_service_cls.return_value.ingest_message.assert_not_called()
        self.mock_service_cls.return_value.list_trade_signals_for_review.assert_not_called()
        self.mock_service_cls.return_value.correct_trade_signal.assert_not_called()

    def test_trader_performance_never_writes(self):
        at = self._open_dashboard()

        self.mock_conn.commit.assert_not_called()
        self.mock_conn.rollback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
