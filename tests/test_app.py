"""UI/smoke tests for app/app.py.

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
from pathlib import Path
from unittest.mock import MagicMock, patch

from streamlit.testing.v1 import AppTest

from app import logging_config
from app.parser import parse_message
from database.config import resolve_database_path

_SAMPLE_MESSAGE = "BTO SPY 450C 7/19/2025 @3.25 10 contracts"


class ManualMessageEntryTests(unittest.TestCase):
    def test_successful_parse_displays_structured_result(self):
        at = AppTest.from_file("app/app.py")
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
        at = AppTest.from_file("app/app.py")
        at.run()

        at.text_area[0].input("BTO SPY 450C 7/19/2025 @3.25\nSTC AAPL 190P 12/15/2025 @1.10").run()
        at.button[0].click().run()

        self.assertEqual(len(at.success), 1)
        self.assertIn("2 trade signal", at.success[0].value)
        self.assertEqual(len(at.json), 2)

    def test_no_signal_displays_nothing_found_message(self):
        at = AppTest.from_file("app/app.py")
        at.run()

        at.text_area[0].input("just some random text").run()
        at.button[0].click().run()

        self.assertEqual(len(at.warning), 1)
        self.assertIn("No trade signals found", at.warning[0].value)
        self.assertEqual(len(at.success), 0)
        self.assertEqual(len(at.json), 0)

    def test_empty_input_displays_nothing_found_message(self):
        at = AppTest.from_file("app/app.py")
        at.run()

        at.button[0].click().run()

        self.assertEqual(len(at.warning), 1)
        self.assertIn("No trade signals found", at.warning[0].value)

    def test_no_run_before_button_click_shows_no_result(self):
        at = AppTest.from_file("app/app.py")
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

            at = AppTest.from_file("app/app.py")
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

            at = AppTest.from_file("app/app.py")
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

            at = AppTest.from_file("app/app.py")
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

            at = AppTest.from_file("app/app.py")
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

            at = AppTest.from_file("app/app.py")
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

            at = AppTest.from_file("app/app.py")
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

            at = AppTest.from_file("app/app.py")
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

            at = AppTest.from_file("app/app.py")
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

            at = AppTest.from_file("app/app.py")
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

            at = AppTest.from_file("app/app.py")
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

            at = AppTest.from_file("app/app.py")
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

            at = AppTest.from_file("app/app.py")
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

            at = AppTest.from_file("app/app.py")
            self._run_to_review(at)
            self._submit(at)

            mock_conn.close.assert_called_once()

    def test_connection_closed_after_failure(self):
        mock_conn = MagicMock()
        init_p, conn_p, service_p = self._patches(mock_conn)
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value
            mock_service.ingest_message.side_effect = ValueError("bad input")

            at = AppTest.from_file("app/app.py")
            self._run_to_review(at)
            self._submit(at)

            mock_conn.close.assert_called_once()

    def test_edited_raw_text_clears_preview_and_prevents_stale_submit(self):
        init_p, conn_p, service_p = self._patches()
        with init_p, conn_p, service_p as mock_service_cls:
            mock_service = mock_service_cls.return_value

            at = AppTest.from_file("app/app.py")
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

            at = AppTest.from_file("app/app.py")
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

            at = AppTest.from_file("app/app.py")
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

            at = AppTest.from_file("app/app.py")
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

            at = AppTest.from_file("app/app.py")
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

            at = AppTest.from_file("app/app.py")
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

            at = AppTest.from_file("app/app.py")
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

            at = AppTest.from_file("app/app.py")
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
            at = AppTest.from_file("app/app.py")
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
                at = AppTest.from_file("app/app.py")
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
        at = AppTest.from_file("app/app.py")
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
                at = AppTest.from_file("app/app.py")
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
                at = AppTest.from_file("app/app.py")
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
                at = AppTest.from_file("app/app.py")
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
                at = AppTest.from_file("app/app.py")
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
                at = AppTest.from_file("app/app.py")
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
                at = AppTest.from_file("app/app.py")
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
                at = AppTest.from_file("app/app.py")
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

            at = AppTest.from_file("app/app.py")
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

            at = AppTest.from_file("app/app.py")
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
        at = AppTest.from_file("app/app.py")
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

            at = AppTest.from_file("app/app.py")
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
        at = AppTest.from_file("app/app.py")
        at.run()

        self.assertIn("Create Backup", {b.label for b in at.button})

    def test_no_restore_control_is_present(self):
        at = AppTest.from_file("app/app.py")
        at.run()

        self.assertEqual(
            {b.label for b in at.button}, {"Parse Message", "Create Backup"}
        )

    def test_successful_backup_shows_fixed_success_message(self):
        with patch("app.logging_config.configure_file_logging"), patch(
            "database.backup.create_backup", return_value="C:/sentinel/backups/x.db"
        ):
            at = AppTest.from_file("app/app.py")
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
                at = AppTest.from_file("app/app.py")
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

    def test_sidebar_offers_both_workflows(self):
        at = AppTest.from_file("app/app.py")
        at.run()

        self.assertEqual(
            at.sidebar.radio[0].options, ["Manual Message Entry", "Review Signals"]
        )

    def test_manual_message_entry_is_the_default_selection(self):
        at = AppTest.from_file("app/app.py")
        at.run()

        self.assertEqual(at.sidebar.radio[0].value, "Manual Message Entry")
        self.assertIn("Parse Message", {b.label for b in at.button})

    def test_create_backup_appears_only_in_manual_message_entry(self):
        at = AppTest.from_file("app/app.py")
        at.run()
        self.assertIn("Create Backup", {b.label for b in at.button})

        at = at.sidebar.radio[0].set_value("Review Signals").run()
        self.assertNotIn("Create Backup", {b.label for b in at.button})

    def test_manual_message_entry_does_not_invoke_review_method(self):
        with patch("database.service.TradeService") as mock_service_cls:
            at = AppTest.from_file("app/app.py")
            at.run()

            mock_service_cls.return_value.list_trade_signals_for_review.assert_not_called()

    def test_review_signals_does_not_invoke_parser_ingest_or_backup(self):
        with patch("app.parser.parse_message") as mock_parse, patch(
            "database.service.TradeService"
        ) as mock_service_cls, patch("database.backup.create_backup") as mock_backup:
            mock_service_cls.return_value.list_trade_signals_for_review.return_value = []

            at = AppTest.from_file("app/app.py")
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
                    at = AppTest.from_file("app/app.py")
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
                at = AppTest.from_file("app/app.py")
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
                at = AppTest.from_file("app/app.py")
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

    def _open_review(self):
        at = AppTest.from_file("app/app.py")
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

    def test_no_edit_correction_or_delete_controls_present(self):
        at = self._open_review()

        self.assertEqual(len(at.button), 0)
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
        at = AppTest.from_file("app/app.py")
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


if __name__ == "__main__":
    unittest.main()
