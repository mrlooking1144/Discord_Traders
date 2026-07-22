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
        at = AppTest.from_file("app/app.py")
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


if __name__ == "__main__":
    unittest.main()
