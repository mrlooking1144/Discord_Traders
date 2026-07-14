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
"""

import json
import sqlite3
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from streamlit.testing.v1 import AppTest

from app.parser import parse_message

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


class ManualEntryPersistenceTests(unittest.TestCase):
    """Covers Milestone 2C.3: submit-to-database wiring."""

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
            self.assertEqual(len(at.button), 1)
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
            self.assertEqual(len(at.button), 1)
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

        with resolve_p, config_p as mock_config_cls, init_p, conn_p, service_p as mock_service_cls:
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


if __name__ == "__main__":
    unittest.main()
