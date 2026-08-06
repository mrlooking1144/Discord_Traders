"""Real-SQLite integration tests for Recovery Milestone R9c's Bulk
Channel Import UI.

Exercises the complete Bulk Channel Import workflow - the real Streamlit
app.streamlit_app.py script driven through streamlit.testing.v1.AppTest,
against a real, unique per-test temporary SQLite database (via
database/db.py + database/schema.sql, exactly as production code will use
them) - following the same real-I/O philosophy as
tests/test_dashboard_integration.py and
tests/test_batch_ingestion_integration.py. Only DISCORD_TRADERS_DB_PATH is
overridden; nothing else is mocked, except in
RollbackFaultInjectionIntegrationTests, which patches exactly one internal
call (database.service.create_channel_import_operation) to force a late
failure and prove the whole transaction rolls back - the one approved
targeted fault-injection exception to this file's no-mocking rule.

tests/test_bulk_channel_import_service.py and
tests/test_app.py already cover this milestone's full contract at the
service level (real SQLite, single connection) and the UI level (mocked
service) respectively. This file focuses on what those cannot show: the
real Streamlit script driving the real TradeService against a real
database end to end, across connections, including a downstream Trader
Performance dashboard read of the same data.
"""

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.bulk_import_formatting import is_synthetic_external_id
from database import repository
from database.config import DatabaseConfig
from database.db import get_connection, initialize_database
from database.service import TradeService

_BULK_IMPORT_SUCCESS_TEXT = "Bulk channel import completed successfully."

_ALL_WRITE_TABLES = (
    "sources", "channels", "import_batches", "raw_messages",
    "message_extractions", "trade_signals", "traders", "trade_lifecycles",
    "trade_lifecycle_events", "channel_import_operations",
)


def _batch_text(count, start=0, trader="Bdorts", symbol="AVGO"):
    """A pure literal fixture of `count` distinct, valid, segmentable,
    lifecycle-eligible footer-line-format BOUGHT messages, starting at
    `start` (so two calls with different `start` values never collide on
    synthetic identity, and two calls with the same `start`/`count`
    prefix produce byte-identical messages - used to build genuine
    duplicate overlap). Mirrors
    tests/test_bulk_channel_import_service.py's own _valid_batch_text()
    and tests/test_app.py's own _bulk_import_batch_text()."""
    messages = []
    for offset in range(count):
        i = start + offset
        messages.append(
            f"{trader}\nAPP\n — 04:{i:02d} PM\n"
            f"BOUGHT {symbol} 07/24 {380 + i}P $1.{i:02d} [SMALL]\n"
            f"{trader}•Today at 04:{i:02d} PM\n"
        )
    return "".join(messages)


def _closeable_batch_text(trader="Bdorts", symbol="AVGO"):
    """15 messages: a real BOUGHT entry + a real SOLD ALL OUT full exit
    for the same symbol/strike (a genuine closed, win-scored lifecycle),
    padded with 13 further distinct open-only BOUGHT messages so the
    batch clears the 15-message floor."""
    messages = [
        f"{trader}\nAPP\n — 04:00 PM\n"
        f"BOUGHT {symbol} 07/24 380P $1.00 [SMALL]\n"
        f"{trader}•Today at 04:00 PM\n",
        f"{trader}\nAPP\n — 04:01 PM\n"
        f"SOLD {symbol} 07/24 380P $2.00 ALL OUT\n"
        f"{trader}•Today at 04:01 PM\n",
    ]
    messages.append(_batch_text(13, start=100, trader=trader, symbol=symbol))
    return "".join(messages)


class _BulkImportIntegrationTestCase(unittest.TestCase):
    """Shared fixture: a unique temporary database path, exercised
    exclusively through the real Bulk Channel Import UI workflow (plus,
    where a test needs to pre-seed or independently verify state, direct
    database.repository/database.service calls on their own
    connections)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "discord_traders.db"

        env_patch = patch.dict(
            os.environ, {"DISCORD_TRADERS_DB_PATH": str(self.db_path)}, clear=False
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

        self.config = DatabaseConfig(db_path=str(self.db_path))

    def _fresh_connection(self):
        initialize_database(self.config)
        return get_connection(self.config)

    def _row_counts(self):
        conn = self._fresh_connection()
        try:
            return {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in _ALL_WRITE_TABLES
            }
        finally:
            conn.close()

    def _seed_channel(self, *, external_id, name=None):
        conn = self._fresh_connection()
        try:
            source = repository.get_or_create_source(conn, "discord")
            channel = repository.create_channel(
                conn, source.id, external_channel_id=external_id, name=name
            )
            conn.commit()
            return channel
        finally:
            conn.close()

    def _open_bulk_import(self):
        at = AppTest.from_file("app/streamlit_app.py")
        at.run()
        return at.sidebar.radio[0].set_value("Bulk Channel Import").run()

    def _fill_existing_mode(self, at, *, raw_text, channel_id, timezone="Asia/Riyadh"):
        at.text_area(key="bulk_import_raw_text_input").set_value(raw_text)
        at.text_input(key="bulk_import_timezone").set_value(timezone)
        at = at.date_input(key="bulk_import_reference_date").set_value(
            date(2026, 7, 24)
        ).run()
        at.selectbox(key="bulk_import_existing_channel_select").set_value(channel_id)
        return at.run()

    def _fill_create_mode(
        self, at, *, raw_text, external_id, name="", timezone="Asia/Riyadh"
    ):
        at = at.radio(key="bulk_import_channel_mode").set_value(
            "Create a new channel"
        ).run()
        at.text_area(key="bulk_import_raw_text_input").set_value(raw_text)
        at.text_input(key="bulk_import_new_channel_external_id").set_value(external_id)
        at.text_input(key="bulk_import_new_channel_name").set_value(name)
        at.text_input(key="bulk_import_timezone").set_value(timezone)
        at = at.date_input(key="bulk_import_reference_date").set_value(
            date(2026, 7, 24)
        ).run()
        return at.run()

    def _confirm(self, at):
        at = at.checkbox(key="bulk_import_confirm_checkbox").set_value(True).run()
        return at.button(key="bulk_import_confirm_button").click().run()


class ExistingChannelHappyPathIntegrationTests(_BulkImportIntegrationTestCase):
    def test_existing_channel_import_stores_real_data_end_to_end(self):
        channel = self._seed_channel(external_id="chan-existing-1", name="Existing")

        at = self._open_bulk_import()
        at = self._fill_existing_mode(
            at, raw_text=_closeable_batch_text(), channel_id=channel.id
        )
        at = at.button(key="bulk_import_preview_button").click().run()
        self.assertEqual(len(at.exception), 0)

        at = self._confirm(at)
        self.assertEqual(len(at.exception), 0)
        self.assertEqual(len(at.success), 1)

        verify = self._fresh_connection()
        try:
            counts = {
                table: verify.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("raw_messages", "trade_signals", "channel_import_operations")
            }
            self.assertEqual(counts["raw_messages"], 15)
            self.assertEqual(counts["trade_signals"], 15)
            self.assertEqual(counts["channel_import_operations"], 1)

            service = TradeService(verify)
            checkpoints = service.get_channel_checkpoints()
            checkpoint = next(c for c in checkpoints if c.channel_id == channel.id)
            self.assertEqual(checkpoint.last_ingested_raw_message_id is not None, True)
        finally:
            verify.close()


class CreateChannelHappyPathIntegrationTests(_BulkImportIntegrationTestCase):
    def test_create_channel_import_stores_real_data_end_to_end(self):
        at = self._open_bulk_import()
        at = self._fill_create_mode(
            at, raw_text=_closeable_batch_text(), external_id="chan-create-1",
            name="Created Channel",
        )
        at = at.button(key="bulk_import_preview_button").click().run()
        self.assertEqual(len(at.exception), 0)
        # Create mode: prediction is never called - "provisionally new"
        # for every one of the 15 segmented messages.
        self.assertTrue(
            any("Provisionally new: 15" in m.value for m in at.markdown)
        )

        at = self._confirm(at)
        self.assertEqual(len(at.exception), 0)
        self.assertEqual(len(at.success), 1)

        verify = self._fresh_connection()
        try:
            channel = repository.get_channel_by_external_id(
                verify,
                repository.get_or_create_source(verify, "discord").id,
                "chan-create-1",
            )
            self.assertIsNotNone(channel)
            self.assertEqual(channel.name, "Created Channel")

            raw_message_count = verify.execute(
                "SELECT COUNT(*) FROM raw_messages WHERE channel_id = ?",
                (channel.id,),
            ).fetchone()[0]
            self.assertEqual(raw_message_count, 15)
        finally:
            verify.close()

    def test_fresh_session_after_completed_import_starts_with_blank_form(self):
        # Recovery Milestone R9c: a completed import's bulk_import_result/
        # bulk_import_preview live only in that one session's
        # session_state - never anywhere durable. Proven here with a
        # genuinely separate AppTest session (equivalent to a fresh
        # browser load) rather than a second .run() on the same session,
        # since the underlying streamlit.testing.v1 AppTest harness
        # cannot safely issue a further .run() call on the same object
        # once a script pass both conditionally renders a widget and
        # calls st.rerun() in that same pass (a confirmed AppTest-harness
        # limitation, not a production bug - see the sibling
        # tests/test_app.py::BulkChannelImportWorkflowTests class
        # docstring for the full isolated diagnosis). Real durability
        # (the real database rows persisting) is separately proven by
        # every other test in this file reading back through a fresh
        # database connection.
        at = self._open_bulk_import()
        at = self._fill_create_mode(
            at, raw_text=_closeable_batch_text(), external_id="chan-create-reset",
        )
        at = at.button(key="bulk_import_preview_button").click().run()
        at = self._confirm(at)
        self.assertTrue(any(_BULK_IMPORT_SUCCESS_TEXT in s.value for s in at.success))

        fresh = self._open_bulk_import()

        self.assertEqual(fresh.text_area(key="bulk_import_raw_text_input").value, "")
        self.assertIsNone(
            fresh.selectbox(key="bulk_import_existing_channel_select").value
        )
        self.assertFalse(
            any(_BULK_IMPORT_SUCCESS_TEXT in s.value for s in fresh.success)
        )


class DuplicateOnlyReimportIntegrationTests(_BulkImportIntegrationTestCase):
    def test_reimporting_identical_batch_stores_zero_new_rows(self):
        # Two separate AppTest sessions (see
        # test_fresh_session_after_completed_import_starts_with_blank_form's
        # docstring for why): the first performs and stops immediately
        # after the real first import; the second - a fresh session
        # reading the same real database - performs the real duplicate
        # reimport.
        channel = self._seed_channel(external_id="chan-dup-1")
        batch = _closeable_batch_text()

        at = self._open_bulk_import()
        at = self._fill_existing_mode(at, raw_text=batch, channel_id=channel.id)
        at = at.button(key="bulk_import_preview_button").click().run()
        at = self._confirm(at)
        self.assertTrue(any(_BULK_IMPORT_SUCCESS_TEXT in s.value for s in at.success))

        before = self._row_counts()

        at2 = self._open_bulk_import()
        at2 = self._fill_existing_mode(at2, raw_text=batch, channel_id=channel.id)
        at2 = at2.button(key="bulk_import_preview_button").click().run()
        self.assertTrue(
            any("Predicted duplicate: 15" in m.value for m in at2.markdown)
        )

        at2 = self._confirm(at2)
        self.assertEqual(len(at2.exception), 0)
        self.assertTrue(any(_BULK_IMPORT_SUCCESS_TEXT in s.value for s in at2.success))

        after = self._row_counts()
        # Zero new business-data rows anywhere - only the new,
        # zero-stored channel_import_operations row is allowed to
        # change, per import_channel_batch_with_lifecycle_rebuild()'s
        # own documented duplicate-only-batch contract.
        for table in _ALL_WRITE_TABLES:
            if table == "channel_import_operations":
                self.assertEqual(after[table], before[table] + 1)
            else:
                self.assertEqual(after[table], before[table], table)


class TraderPerformanceRefreshIntegrationTests(_BulkImportIntegrationTestCase):
    def test_trader_performance_reflects_bulk_imported_data(self):
        at = self._open_bulk_import()
        at = self._fill_create_mode(
            at, raw_text=_closeable_batch_text(), external_id="chan-refresh-1",
        )
        at = at.button(key="bulk_import_preview_button").click().run()
        at = self._confirm(at)
        self.assertTrue(any(_BULK_IMPORT_SUCCESS_TEXT in s.value for s in at.success))

        # A separate, fresh AppTest session (see
        # test_fresh_session_after_completed_import_starts_with_blank_form's
        # docstring) reading the real database the first session just
        # committed to - proving the Trader Performance dashboard reads
        # real, durable data, not in-memory session state.
        dashboard = AppTest.from_file("app/streamlit_app.py")
        dashboard.run()
        dashboard = dashboard.sidebar.radio[0].set_value("Trader Performance").run()

        df = dashboard.dataframe[0].value
        self.assertEqual(len(df), 1)
        row = df.iloc[0]
        self.assertTrue(row["Trader"].startswith("Bdorts"))
        # The BOUGHT+SOLD ALL OUT pair merges into 1 closed win lifecycle
        # (same trader/symbol/strike/expiration); the other 13 padding
        # messages are each a distinct, still-open (not_scored)
        # lifecycle - 14 lifecycles total from 15 stored messages.
        self.assertEqual(row["Total Lifecycles"], 14)
        self.assertEqual(row["Wins"], 1)


class PreviewWritesNothingIntegrationTests(_BulkImportIntegrationTestCase):
    def test_preview_batch_alone_writes_zero_business_data_rows(self):
        before = self._row_counts()
        self.assertTrue(all(count == 0 for count in before.values()))

        at = self._open_bulk_import()
        at = self._fill_create_mode(
            at, raw_text=_closeable_batch_text(), external_id="chan-preview-only",
        )
        at = at.button(key="bulk_import_preview_button").click().run()
        self.assertEqual(len(at.exception), 0)
        self.assertIn("bulk_import_preview", at.session_state)

        after = self._row_counts()
        self.assertEqual(before, after)


class RealCheckpointExternalIdIntegrationTests(_BulkImportIntegrationTestCase):
    def test_checkpoint_ingestion_external_id_is_real_synthetic_id_of_last_stored_message(
        self,
    ):
        channel = self._seed_channel(external_id="chan-checkpoint-1")

        at = self._open_bulk_import()
        at = self._fill_existing_mode(
            at, raw_text=_closeable_batch_text(), channel_id=channel.id
        )
        at = at.button(key="bulk_import_preview_button").click().run()
        at = self._confirm(at)
        self.assertEqual(len(at.success), 1)

        verify = self._fresh_connection()
        try:
            last_raw_message = verify.execute(
                "SELECT id, external_id FROM raw_messages "
                "WHERE channel_id = ? ORDER BY id DESC LIMIT 1",
                (channel.id,),
            ).fetchone()

            service = TradeService(verify)
            checkpoint = next(
                c for c in service.get_channel_checkpoints()
                if c.channel_id == channel.id
            )

            self.assertEqual(
                checkpoint.last_ingested_raw_message_id, last_raw_message["id"]
            )
            self.assertEqual(
                checkpoint.last_ingested_external_id, last_raw_message["external_id"]
            )
            # Discord-sourced messages never carry a real platform message
            # id - every resolved id here is the durable synthetic form.
            self.assertTrue(is_synthetic_external_id(checkpoint.last_ingested_external_id))
        finally:
            verify.close()


class RollbackFaultInjectionIntegrationTests(_BulkImportIntegrationTestCase):
    """The one approved targeted fault-injection exception to this file's
    no-mocking rule: patches exactly
    database.service.create_channel_import_operation (the last write in
    the atomic transaction) to force a late failure, and proves the
    whole transaction - channel creation, every ingested row, and every
    lifecycle write - rolls back completely, and that the UI shows only
    its fixed, sanitized failure message, never raw exception text."""

    def test_late_failure_rolls_back_the_entire_transaction(self):
        before = self._row_counts()
        self.assertTrue(all(count == 0 for count in before.values()))

        at = self._open_bulk_import()
        at = self._fill_create_mode(
            at, raw_text=_closeable_batch_text(), external_id="chan-rollback-1",
        )
        at = at.button(key="bulk_import_preview_button").click().run()

        with patch(
            "database.service.create_channel_import_operation",
            side_effect=RuntimeError("boom"),
        ):
            at = self._confirm(at)

        self.assertEqual(len(at.exception), 0)
        # The failed attempt returns to the FORM (not the COMPLETED
        # view), which may still independently show its own unrelated
        # "external ID is available" advisory success - only the
        # distinct "Bulk channel import completed successfully" text is
        # asserted absent here.
        self.assertFalse(
            any(_BULK_IMPORT_SUCCESS_TEXT in s.value for s in at.success)
        )
        self.assertTrue(len(at.error) >= 1)
        # A fixed, sanitized message only - never the raw "boom" text.
        for e in at.error:
            self.assertNotIn("boom", e.value)
        self.assertNotIn("bulk_import_result", at.session_state)

        after = self._row_counts()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
