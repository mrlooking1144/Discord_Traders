"""Real-SQLite integration tests for Milestone 2D.5.

Exercises the Correction and Audit Workflow against a real, unique
per-test temporary SQLite database: real ingestion through the existing
Manual Message Entry Submit workflow, real corrections through the new
"Correct Signal" UI path (database.service.TradeService.
update_trade_signal()'s controlled-correction mode), and direct
TradeService calls (bypassing the UI) for scenarios - legacy sparse
updates, simulated stale second-session conflicts, and forced rollback -
that are clearest to express against the service layer directly. Only
DISCORD_TRADERS_DB_PATH and DISCORD_TRADERS_LOG_PATH are overridden via
os.environ; nothing else is mocked.
"""

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
from database.config import DatabaseConfig
from database.db import get_connection, initialize_database
from database.service import StaleTradeSignalError, TradeService

_SAMPLE_MESSAGE = "BTO SPY 450C 7/19/2025 @3.25 10 contracts"


class _CorrectionIntegrationTestCase(unittest.TestCase):
    """Shared fixture: a unique, not-yet-existing temporary database path
    and log path, redirected via environment overrides - the same
    approach used by tests/test_review_integration.py."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
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
        logger = logging.getLogger(logging_config._LOGGER_NAME)
        for handler in list(logger.handlers):
            if getattr(handler, logging_config._FILE_HANDLER_MARKER, False):
                logger.removeHandler(handler)
                handler.close()

    def _ingest(self, at, raw_text, trader_name, external_trader_id):
        at.run()
        at.text_area[0].input(raw_text).run()
        at.button[0].click().run()
        at.text_input[0].input(trader_name).run()
        at.text_input[1].input(external_trader_id).run()
        at.button[1].click().run()
        return at

    def _open_review(self, at):
        return at.sidebar.radio[0].set_value("Review Signals").run()

    def _config(self):
        return DatabaseConfig(db_path=str(self.db_path))

    def _real_connection(self):
        initialize_database(self._config())
        return get_connection(self._config())


class LegacyServiceBehaviorTests(_CorrectionIntegrationTestCase):
    def test_legacy_sparse_update_still_works_against_real_database(self):
        conn = self._real_connection()
        try:
            from database.repository import (
                create_raw_message,
                create_trade_signal,
                create_trader,
                get_or_create_source,
            )

            source = get_or_create_source(conn, "discord")
            trader = create_trader(conn, source.id, "alice")
            raw_message = create_raw_message(conn, source.id, "BTO SPY 500c")
            signal = create_trade_signal(
                conn, raw_message.id, trader.id, "SPY", "BTO", price=Decimal("3.25")
            )
            conn.commit()

            service = TradeService(conn)
            updated = service.update_trade_signal(signal.id, symbol="QQQ")
            conn.commit()

            self.assertEqual(updated.symbol, "QQQ")
        finally:
            conn.close()


class UiCorrectionIntegrationTests(_CorrectionIntegrationTestCase):
    def _ingest_and_correct(
        self, action="STC", price="4.10", expiration="2026-12-19"
    ):
        at = AppTest.from_file("app/streamlit_app.py")
        self._ingest(at, _SAMPLE_MESSAGE, "alice", "disc-123")
        self.assertEqual(len(at.error), 0)

        at = self._open_review(at)
        df = at.dataframe[0].value
        signal_id = int(df.iloc[0]["ID"])
        at = at.selectbox[0].set_value(signal_id).run()
        at = next(b for b in at.button if b.label == "Correct Signal").click().run()

        action_box = next(w for w in at.selectbox if w.label == "Corrected action")
        at = action_box.set_value(action).run()
        price_input = next(w for w in at.text_input if w.label == "Corrected price")
        at = price_input.input(price).run()
        expiration_input = next(
            w
            for w in at.text_input
            if w.label == "Corrected expiration (YYYY-MM-DD)"
        )
        at = expiration_input.input(expiration).run()
        confirm = next(w for w in at.checkbox if w.label == "I confirm this correction")
        at = confirm.set_value(True).run()
        save_btn = next(b for b in at.button if b.label == "Save Correction")
        at = save_btn.click().run()

        return at, signal_id

    def test_ingest_then_controlled_correction_succeeds(self):
        at, signal_id = self._ingest_and_correct()

        self.assertEqual(len(at.error), 0)
        self.assertEqual(at.success[0].value, "Trade signal correction saved.")

    def test_persisted_corrected_values_are_exact(self):
        _, signal_id = self._ingest_and_correct(
            action="STC", price="4.10", expiration="2026-12-19"
        )

        conn = get_connection(self._config())
        try:
            row = conn.execute(
                "SELECT action, price, expiration FROM trade_signals WHERE id = ?",
                (signal_id,),
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(row["action"], "STC")
        self.assertEqual(row["price"], "4.10")
        self.assertEqual(row["expiration"], "2026-12-19")

    def test_exact_previous_audit_snapshot_persisted(self):
        import json

        _, signal_id = self._ingest_and_correct(
            action="STC", price="4.10", expiration="2026-12-19"
        )

        conn = get_connection(self._config())
        try:
            edit_row = conn.execute(
                "SELECT previous_values FROM trade_signal_edits "
                "WHERE trade_signal_id = ?",
                (signal_id,),
            ).fetchone()
        finally:
            conn.close()

        snapshot = json.loads(edit_row["previous_values"])
        self.assertEqual(snapshot["action"], "BTO")
        self.assertEqual(snapshot["price"], "3.25")
        self.assertEqual(snapshot["expiration"], "2025-07-19")

    def test_decimal_price_preserved_exactly_through_correction_and_audit(self):
        _, signal_id = self._ingest_and_correct(price="4.10")

        conn = get_connection(self._config())
        try:
            signal_row = conn.execute(
                "SELECT price FROM trade_signals WHERE id = ?", (signal_id,)
            ).fetchone()
            edit_row = conn.execute(
                "SELECT previous_values FROM trade_signal_edits "
                "WHERE trade_signal_id = ?",
                (signal_id,),
            ).fetchone()
        finally:
            conn.close()

        import json

        self.assertIsInstance(signal_row["price"], str)
        self.assertEqual(signal_row["price"], "4.10")
        previous = json.loads(edit_row["previous_values"])
        self.assertIsInstance(previous["price"], str)
        self.assertEqual(previous["price"], "3.25")

    def test_review_signals_reflects_corrected_values_and_audit_history(self):
        at, signal_id = self._ingest_and_correct(
            action="STC", price="4.10", expiration="2026-12-19"
        )

        # A fresh rerun re-queries the real database from scratch.
        at = at.sidebar.radio[0].set_value("Manual Message Entry").run()
        at = at.sidebar.radio[0].set_value("Review Signals").run()

        df = at.dataframe[0].value
        self.assertEqual(df.iloc[0]["Action"], "STC")
        self.assertEqual(df.iloc[0]["Price"], "4.10")

        self.assertEqual(len(at.dataframe), 2)
        history_df = at.dataframe[1].value
        self.assertEqual(len(history_df), 1)
        self.assertEqual(history_df.iloc[0]["Previous Action"], "BTO")
        self.assertEqual(history_df.iloc[0]["Previous Price"], "3.25")


class StaleConflictIntegrationTests(_CorrectionIntegrationTestCase):
    def _seed_signal(self, conn):
        from database.repository import (
            create_raw_message,
            create_trade_signal,
            create_trader,
            get_or_create_source,
        )

        source = get_or_create_source(conn, "discord")
        trader = create_trader(conn, source.id, "alice")
        raw_message = create_raw_message(conn, source.id, "BTO SPY 500c")
        signal = create_trade_signal(
            conn,
            raw_message.id,
            trader.id,
            "SPY",
            "BTO",
            option_type="call",
            price=Decimal("3.25"),
            expiration="2026-12-18",
            position_size="10 contracts",
        )
        conn.commit()
        return signal

    def _snapshot(self, signal):
        return {
            "symbol": signal.symbol,
            "action": signal.action,
            "option_type": signal.option_type,
            "price": Decimal(signal.price) if signal.price is not None else None,
            "expiration": signal.expiration,
            "position_size": signal.position_size,
        }

    def _edit_count(self, conn):
        return conn.execute(
            "SELECT COUNT(*) FROM trade_signal_edits"
        ).fetchone()[0]

    def test_second_stale_session_is_rejected(self):
        conn = self._real_connection()
        try:
            signal = self._seed_signal(conn)
            original_snapshot = self._snapshot(signal)
            service = TradeService(conn)

            # First "session" corrects successfully.
            first_changed = dict(original_snapshot, symbol="QQQ")
            service.update_trade_signal(
                signal.id,
                expected_current_values=original_snapshot,
                **first_changed,
            )
            conn.commit()

            # Second "session" still holds the ORIGINAL (now stale)
            # snapshot and attempts its own correction.
            second_changed = dict(original_snapshot, symbol="IWM")
            with self.assertRaises(StaleTradeSignalError):
                service.update_trade_signal(
                    signal.id,
                    expected_current_values=original_snapshot,
                    **second_changed,
                )
        finally:
            conn.close()

    def test_stale_rejection_creates_no_extra_audit_row(self):
        conn = self._real_connection()
        try:
            signal = self._seed_signal(conn)
            original_snapshot = self._snapshot(signal)
            service = TradeService(conn)

            first_changed = dict(original_snapshot, symbol="QQQ")
            service.update_trade_signal(
                signal.id,
                expected_current_values=original_snapshot,
                **first_changed,
            )
            conn.commit()
            self.assertEqual(self._edit_count(conn), 1)

            second_changed = dict(original_snapshot, symbol="IWM")
            with self.assertRaises(StaleTradeSignalError):
                service.update_trade_signal(
                    signal.id,
                    expected_current_values=original_snapshot,
                    **second_changed,
                )

            self.assertEqual(self._edit_count(conn), 1)
        finally:
            conn.close()

    def test_reload_then_correction_succeeds(self):
        conn = self._real_connection()
        try:
            signal = self._seed_signal(conn)
            original_snapshot = self._snapshot(signal)
            service = TradeService(conn)

            first_changed = dict(original_snapshot, symbol="QQQ")
            service.update_trade_signal(
                signal.id,
                expected_current_values=original_snapshot,
                **first_changed,
            )
            conn.commit()

            # "Reload" current values (as the UI would) before retrying.
            from database.repository import get_trade_signal_by_id

            reloaded = get_trade_signal_by_id(conn, signal.id)
            reloaded_snapshot = self._snapshot(reloaded)
            second_changed = dict(reloaded_snapshot, symbol="IWM")

            updated = service.update_trade_signal(
                signal.id,
                expected_current_values=reloaded_snapshot,
                **second_changed,
            )
            conn.commit()

            self.assertEqual(updated.symbol, "IWM")
            self.assertEqual(self._edit_count(conn), 2)
        finally:
            conn.close()

    def test_identity_field_change_rejected(self):
        conn = self._real_connection()
        try:
            signal = self._seed_signal(conn)
            original_snapshot = self._snapshot(signal)
            service = TradeService(conn)

            changed_with_identity = dict(original_snapshot, symbol="QQQ")
            del changed_with_identity["position_size"]
            changed_with_identity["trader_id"] = signal.trader_id

            with self.assertRaises(ValueError):
                service.update_trade_signal(
                    signal.id,
                    expected_current_values=original_snapshot,
                    **changed_with_identity,
                )

            self.assertEqual(self._edit_count(conn), 0)
            unchanged = conn.execute(
                "SELECT symbol, trader_id FROM trade_signals WHERE id = ?",
                (signal.id,),
            ).fetchone()
            self.assertEqual(unchanged["symbol"], "SPY")
            self.assertEqual(unchanged["trader_id"], signal.trader_id)
        finally:
            conn.close()


class RollbackAndDurabilityIntegrationTests(_CorrectionIntegrationTestCase):
    def _seed_signal(self, conn):
        from database.repository import (
            create_raw_message,
            create_trade_signal,
            create_trader,
            get_or_create_source,
        )

        source = get_or_create_source(conn, "discord")
        trader = create_trader(conn, source.id, "alice")
        raw_message = create_raw_message(conn, source.id, "BTO SPY 500c")
        signal = create_trade_signal(
            conn,
            raw_message.id,
            trader.id,
            "SPY",
            "BTO",
            option_type="call",
            price=Decimal("3.25"),
            expiration="2026-12-18",
            position_size="10 contracts",
        )
        conn.commit()
        return signal

    def _snapshot(self, signal):
        return {
            "symbol": signal.symbol,
            "action": signal.action,
            "option_type": signal.option_type,
            "price": Decimal(signal.price) if signal.price is not None else None,
            "expiration": signal.expiration,
            "position_size": signal.position_size,
        }

    def test_forced_rollback_leaves_signal_and_audit_unchanged(self):
        conn = self._real_connection()
        try:
            signal = self._seed_signal(conn)
            original_snapshot = self._snapshot(signal)
            service = TradeService(conn)
            changed = dict(original_snapshot, symbol="QQQ")

            with patch(
                "database.service._repository_update_trade_signal",
                side_effect=sqlite3.OperationalError(
                    "simulated mid-transaction failure"
                ),
            ):
                with self.assertRaises(Exception):
                    service.update_trade_signal(
                        signal.id,
                        expected_current_values=original_snapshot,
                        **changed,
                    )
            conn.rollback()

            edit_count = conn.execute(
                "SELECT COUNT(*) FROM trade_signal_edits"
            ).fetchone()[0]
            self.assertEqual(edit_count, 0)

            row = conn.execute(
                "SELECT symbol FROM trade_signals WHERE id = ?", (signal.id,)
            ).fetchone()
            self.assertEqual(row["symbol"], "SPY")
        finally:
            conn.close()

    def test_fresh_connection_confirms_commit_durability(self):
        conn = self._real_connection()
        try:
            signal = self._seed_signal(conn)
            original_snapshot = self._snapshot(signal)
            service = TradeService(conn)
            changed = dict(original_snapshot, symbol="QQQ")

            service.update_trade_signal(
                signal.id, expected_current_values=original_snapshot, **changed
            )
            conn.commit()
        finally:
            conn.close()

        fresh_conn = get_connection(self._config())
        try:
            row = fresh_conn.execute(
                "SELECT symbol FROM trade_signals WHERE id = ?", (signal.id,)
            ).fetchone()
            edit_count = fresh_conn.execute(
                "SELECT COUNT(*) FROM trade_signal_edits"
            ).fetchone()[0]
        finally:
            fresh_conn.close()

        self.assertEqual(row["symbol"], "QQQ")
        self.assertEqual(edit_count, 1)


class LifecycleAwareUiCorrectionIntegrationTests(_CorrectionIntegrationTestCase):
    """Real-SQLite integration coverage for Recovery Milestone R6.5b: the
    shipped "Correct Signal" UI now calls TradeService.correct_trade_signal()
    for every signal, including lifecycle-managed ones. Signals here are
    seeded directly through the repository/service layer (rather than the
    Manual Message Entry UI, which never sets event_type) so that a real
    lifecycle generation exists before the UI drives a correction against
    it. No mocking of the lifecycle rebuild, transaction context,
    repository writes, or integrity validator - every write below is a
    real SQLite write."""

    def _seed_lifecycle_managed_signal(self, conn, action="BOUGHT", symbol="IBM"):
        from database.repository import (
            create_raw_message,
            create_trade_signal,
            create_trader,
            get_or_create_source,
        )

        source = get_or_create_source(conn, "discord")
        trader = create_trader(conn, source.id, "frank")
        raw_message = create_raw_message(
            conn, source.id, f"{action} {symbol} 100C 12/19/2026 @2.50"
        )
        signal = create_trade_signal(
            conn,
            raw_message.id,
            trader.id,
            symbol,
            action,
            option_type="call",
            price=Decimal("2.50"),
            expiration="2026-12-19",
            strike=Decimal("100"),
            event_type="ENTRY",
        )
        conn.commit()
        TradeService(conn).rebuild_all_lifecycles()
        conn.commit()

        from database.repository import get_trade_signal_by_id

        return get_trade_signal_by_id(conn, signal.id)

    def _open_review_and_select(self, at, signal_id):
        at.run()
        at = self._open_review(at)
        at = at.selectbox[0].set_value(signal_id).run()
        at = next(b for b in at.button if b.label == "Correct Signal").click().run()
        return at

    def test_legacy_signal_correctable_through_ui_via_correct_trade_signal(self):
        at = AppTest.from_file("app/streamlit_app.py")
        self._ingest(at, _SAMPLE_MESSAGE, "alice", "disc-123")
        at = self._open_review(at)
        df = at.dataframe[0].value
        signal_id = int(df.iloc[0]["ID"])

        with patch.object(
            TradeService,
            "correct_trade_signal",
            autospec=True,
            side_effect=TradeService.correct_trade_signal,
        ) as spy_correct, patch.object(
            TradeService,
            "update_trade_signal",
            autospec=True,
            side_effect=AssertionError(
                "legacy update_trade_signal() must not be called by the UI"
            ),
        ):
            at = at.selectbox[0].set_value(signal_id).run()
            at = next(b for b in at.button if b.label == "Correct Signal").click().run()
            price_input = next(w for w in at.text_input if w.label == "Corrected price")
            at = price_input.input("5.00").run()
            confirm = next(
                w for w in at.checkbox if w.label == "I confirm this correction"
            )
            at = confirm.set_value(True).run()
            save_btn = next(b for b in at.button if b.label == "Save Correction")
            at = save_btn.click().run()

        self.assertEqual(len(at.error), 0)
        self.assertEqual(at.success[0].value, "Trade signal correction saved.")
        spy_correct.assert_called_once()

        # Neither the UI nor this test calls conn.commit() after the click
        # above - correct_trade_signal() owns and completes its own
        # commit. Durability is proven only via a brand new connection.
        fresh = get_connection(self._config())
        try:
            row = fresh.execute(
                "SELECT price FROM trade_signals WHERE id = ?", (signal_id,)
            ).fetchone()
            edit_count = fresh.execute(
                "SELECT COUNT(*) FROM trade_signal_edits WHERE trade_signal_id = ?",
                (signal_id,),
            ).fetchone()[0]
        finally:
            fresh.close()

        self.assertEqual(row["price"], "5.00")
        self.assertEqual(edit_count, 1)

    def test_lifecycle_managed_signal_accepts_price_only_ui_correction(self):
        conn = self._real_connection()
        try:
            signal = self._seed_lifecycle_managed_signal(conn)
            self.assertIsNotNone(signal.lifecycle_id)
        finally:
            conn.close()

        at = AppTest.from_file("app/streamlit_app.py")
        at = self._open_review_and_select(at, signal.id)
        price_input = next(w for w in at.text_input if w.label == "Corrected price")
        at = price_input.input("3.75").run()
        confirm = next(w for w in at.checkbox if w.label == "I confirm this correction")
        at = confirm.set_value(True).run()
        save_btn = next(b for b in at.button if b.label == "Save Correction")
        at = save_btn.click().run()

        self.assertEqual(len(at.error), 0)
        self.assertEqual(at.success[0].value, "Trade signal correction saved.")

        fresh = get_connection(self._config())
        try:
            row = fresh.execute(
                "SELECT price, lifecycle_id FROM trade_signals WHERE id = ?",
                (signal.id,),
            ).fetchone()
        finally:
            fresh.close()

        self.assertEqual(row["price"], "3.75")
        # A non-key correction never rebuilds - the signal keeps pointing
        # to the same lifecycle generation it started with.
        self.assertEqual(row["lifecycle_id"], signal.lifecycle_id)

    def test_bought_action_lifecycle_managed_price_only_correction_preserves_action(
        self,
    ):
        conn = self._real_connection()
        try:
            signal = self._seed_lifecycle_managed_signal(conn, action="BOUGHT")
        finally:
            conn.close()

        at = AppTest.from_file("app/streamlit_app.py")
        at = self._open_review_and_select(at, signal.id)
        action_box = next(w for w in at.selectbox if w.label == "Corrected action")
        self.assertEqual(action_box.value, "BOUGHT")

        price_input = next(w for w in at.text_input if w.label == "Corrected price")
        at = price_input.input("6.25").run()
        confirm = next(w for w in at.checkbox if w.label == "I confirm this correction")
        at = confirm.set_value(True).run()
        save_btn = next(b for b in at.button if b.label == "Save Correction")
        at = save_btn.click().run()

        self.assertEqual(len(at.error), 0)
        self.assertEqual(at.success[0].value, "Trade signal correction saved.")

        fresh = get_connection(self._config())
        try:
            row = fresh.execute(
                "SELECT action, price FROM trade_signals WHERE id = ?", (signal.id,)
            ).fetchone()
        finally:
            fresh.close()

        self.assertEqual(row["action"], "BOUGHT")
        self.assertEqual(row["price"], "6.25")

    def test_symbol_correction_through_ui_triggers_targeted_lifecycle_replacement(self):
        from database.repository import (
            get_current_lifecycle_ids_for_raw_message_ids,
            get_trade_lifecycle_by_id,
            get_trade_signal_by_id,
            validate_lifecycle_membership_integrity,
        )

        conn = self._real_connection()
        try:
            signal = self._seed_lifecycle_managed_signal(conn, symbol="IBM")
            old_lifecycle_id = signal.lifecycle_id
            self.assertIsNotNone(old_lifecycle_id)
        finally:
            conn.close()

        at = AppTest.from_file("app/streamlit_app.py")
        at = self._open_review_and_select(at, signal.id)
        symbol_input = next(w for w in at.text_input if w.label == "Corrected symbol")
        at = symbol_input.input("AVGO").run()
        confirm = next(w for w in at.checkbox if w.label == "I confirm this correction")
        at = confirm.set_value(True).run()
        save_btn = next(b for b in at.button if b.label == "Save Correction")
        at = save_btn.click().run()

        self.assertEqual(len(at.error), 0)
        self.assertEqual(at.success[0].value, "Trade signal correction saved.")

        fresh = get_connection(self._config())
        try:
            old_lifecycle = get_trade_lifecycle_by_id(fresh, old_lifecycle_id)
            self.assertFalse(old_lifecycle.is_current)

            corrected = get_trade_signal_by_id(fresh, signal.id)
            self.assertIsNotNone(corrected.lifecycle_id)
            self.assertNotEqual(corrected.lifecycle_id, old_lifecycle_id)

            new_lifecycle = get_trade_lifecycle_by_id(fresh, corrected.lifecycle_id)
            self.assertEqual(new_lifecycle.symbol, "AVGO")
            self.assertTrue(new_lifecycle.is_current)

            # No dual current membership: exactly one current lifecycle id
            # is associated with this raw message.
            current_ids = get_current_lifecycle_ids_for_raw_message_ids(
                fresh, [signal.raw_message_id]
            )
            self.assertEqual(current_ids, [corrected.lifecycle_id])

            self.assertEqual(validate_lifecycle_membership_integrity(fresh), [])
        finally:
            fresh.close()

    def test_managed_action_change_through_ui_is_rejected_atomically_and_form_stays_open(
        self,
    ):
        from database.repository import get_trade_lifecycle_by_id, get_trade_signal_by_id

        conn = self._real_connection()
        try:
            signal = self._seed_lifecycle_managed_signal(conn, action="BOUGHT")
        finally:
            conn.close()

        pre_conn = self._real_connection()
        try:
            pre_signal = get_trade_signal_by_id(pre_conn, signal.id)
            pre_edit_count = pre_conn.execute(
                "SELECT COUNT(*) FROM trade_signal_edits"
            ).fetchone()[0]
            pre_lifecycle_count = pre_conn.execute(
                "SELECT COUNT(*) FROM trade_lifecycles"
            ).fetchone()[0]
            pre_event_count = pre_conn.execute(
                "SELECT COUNT(*) FROM trade_lifecycle_events"
            ).fetchone()[0]
        finally:
            pre_conn.close()

        at = AppTest.from_file("app/streamlit_app.py")
        at = self._open_review_and_select(at, signal.id)
        action_box = next(w for w in at.selectbox if w.label == "Corrected action")
        at = action_box.set_value("STC").run()
        confirm = next(w for w in at.checkbox if w.label == "I confirm this correction")
        at = confirm.set_value(True).run()
        save_btn = next(b for b in at.button if b.label == "Save Correction")
        at = save_btn.click().run()

        self.assertEqual(
            at.error[0].value,
            "Please enter a valid correction that changes at least one field.",
        )
        self.assertIn("Save Correction", {b.label for b in at.button})
        action_box_after = next(w for w in at.selectbox if w.label == "Corrected action")
        self.assertEqual(action_box_after.value, "STC")

        fresh = get_connection(self._config())
        try:
            post_signal = get_trade_signal_by_id(fresh, signal.id)
            self.assertEqual(post_signal.action, "BOUGHT")
            self.assertEqual(post_signal.lifecycle_id, pre_signal.lifecycle_id)
            self.assertTrue(
                get_trade_lifecycle_by_id(fresh, pre_signal.lifecycle_id).is_current
            )
            post_edit_count = fresh.execute(
                "SELECT COUNT(*) FROM trade_signal_edits"
            ).fetchone()[0]
            post_lifecycle_count = fresh.execute(
                "SELECT COUNT(*) FROM trade_lifecycles"
            ).fetchone()[0]
            post_event_count = fresh.execute(
                "SELECT COUNT(*) FROM trade_lifecycle_events"
            ).fetchone()[0]
        finally:
            fresh.close()

        self.assertEqual(post_edit_count, pre_edit_count)
        self.assertEqual(post_lifecycle_count, pre_lifecycle_count)
        self.assertEqual(post_event_count, pre_event_count)


if __name__ == "__main__":
    unittest.main()
