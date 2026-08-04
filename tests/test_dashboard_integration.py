"""Real-SQLite integration tests for Recovery Milestone R8a.

Exercises the complete Trader Performance dashboard path against a real,
unique per-test temporary SQLite database: real trade_signals -> real
TradeService.rebuild_all_lifecycles() -> real persisted
trade_lifecycles/trade_lifecycle_events, followed by a real read through
the Trader Performance workflow (database.service.TradeService.
list_trader_performance_summaries()/list_current_trade_lifecycle_analytics()
-> the real database.analytics engine -> app.dashboard_formatting's pure
helpers). Only DISCORD_TRADERS_DB_PATH is overridden (redirecting
app.streamlit_app.py's own resolve_database_path() call) - nothing else
is mocked. Schema initialization, connections, service orchestration,
lifecycle rebuild, and the analytics computation are all real.

Seed data is built directly via database.repository/database.service
calls (mirroring tests/test_service.py's TradeServiceAnalyticsTests
precedent), not through the Manual Message Entry UI, since UI ingestion
alone never triggers a lifecycle rebuild - matching this project's
existing, documented separation between ingest_message() and
rebuild_all_lifecycles().
"""

import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from database import repository
from database.config import DatabaseConfig
from database.db import get_connection, initialize_database
from database.service import TradeService


class _DashboardIntegrationTestCase(unittest.TestCase):
    """Shared fixture: a unique temporary database path, seeded with real
    signals/lifecycles directly via repository/service calls, then read
    back exclusively through the real Trader Performance UI workflow."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "discord_traders.db"

        env_patch = patch.dict(
            os.environ, {"DISCORD_TRADERS_DB_PATH": str(self.db_path)}, clear=False
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def _make_signal(
        self, conn, *, trader_id, symbol, action, event_type,
        price, strike, qualifier=None, expiration="2026-07-24",
    ):
        raw_message = repository.create_raw_message(conn, self.source.id, "x")
        repository.create_trade_signal(
            conn, raw_message.id, trader_id, symbol, action,
            option_type="call", strike=strike, expiration=expiration,
            event_type=event_type, qualifier=qualifier, price=price,
        )
        conn.commit()

    def _seed_database(self):
        """Build the real database at self.db_path:

        - trader_one ("TC"): a closed IBM win (single FULL_EXIT), a
          closed NVDA win with a fraction-weighted two-leg partial exit
          (75% - never the naive 100% shortcut), and an open MU
          lifecycle (not_scored).
        - trader_two ("TC", a second, distinct trader sharing the same
          name): a single bare zero-event lifecycle (data_error).

        Returns (trader_one, trader_two) - the real created Trader rows,
        so tests can assert on their real, distinct database ids without
        hardcoding them.
        """
        config = DatabaseConfig(db_path=str(self.db_path))
        initialize_database(config)
        conn = get_connection(config)
        try:
            service = TradeService(conn)
            self.source = repository.get_or_create_source(conn, "discord")
            conn.commit()
            trader_one = repository.create_trader(conn, self.source.id, "TC")
            trader_two = repository.create_trader(conn, self.source.id, "TC")
            conn.commit()

            # trader_one: closed IBM win.
            self._make_signal(
                conn, trader_id=trader_one.id, symbol="IBM", action="BTO",
                event_type="ENTRY", price=Decimal("1.00"), strike=Decimal("207.5"),
            )
            self._make_signal(
                conn, trader_id=trader_one.id, symbol="IBM", action="STC",
                event_type="FULL_EXIT", price=Decimal("2.00"), strike=Decimal("207.5"),
                qualifier="ALL OUT",
            )

            # trader_one: closed NVDA win, two-leg fraction-weighted
            # partial exit (1/2 @1.50, then 1/2 @2.00 -> 75%, not 100%).
            self._make_signal(
                conn, trader_id=trader_one.id, symbol="NVDA", action="BTO",
                event_type="ENTRY", price=Decimal("1.00"), strike=Decimal("950"),
            )
            self._make_signal(
                conn, trader_id=trader_one.id, symbol="NVDA", action="STC",
                event_type="PARTIAL_EXIT", price=Decimal("1.50"), strike=Decimal("950"),
                qualifier="1/2",
            )
            self._make_signal(
                conn, trader_id=trader_one.id, symbol="NVDA", action="STC",
                event_type="FULL_EXIT", price=Decimal("2.00"), strike=Decimal("950"),
                qualifier="ALL OUT",
            )

            # trader_one: still-open MU lifecycle (not_scored).
            self._make_signal(
                conn, trader_id=trader_one.id, symbol="MU", action="BTO",
                event_type="ENTRY", price=Decimal("5.00"), strike=Decimal("955"),
            )

            service.rebuild_all_lifecycles()

            # trader_two: a bare, zero-event lifecycle - never routed
            # through the matching engine - the same real-world shape
            # TradeServiceAnalyticsTests.test_strict_raises_analytics_error_for_zero_events
            # already proves surfaces as a data_error result.
            repository.create_trade_lifecycle(
                conn, trader_two.id, "AVGO", status="open", remaining_fraction="1",
                option_type="call", strike=Decimal("150"), expiration="2026-09-01",
            )
            conn.commit()
        finally:
            conn.close()

        return trader_one, trader_two

    def _real_row_counts(self):
        config = DatabaseConfig(db_path=str(self.db_path))
        conn = get_connection(config)
        try:
            signals = conn.execute("SELECT COUNT(*) FROM trade_signals").fetchone()[0]
            lifecycles = conn.execute(
                "SELECT COUNT(*) FROM trade_lifecycles"
            ).fetchone()[0]
            events = conn.execute(
                "SELECT COUNT(*) FROM trade_lifecycle_events"
            ).fetchone()[0]
            return (signals, lifecycles, events)
        finally:
            conn.close()

    def _open_dashboard(self):
        at = AppTest.from_file("app/streamlit_app.py")
        at.run()
        return at.sidebar.radio[0].set_value("Trader Performance").run()


class RealDatabaseTraderPerformanceTests(_DashboardIntegrationTestCase):
    def test_summary_table_reflects_real_multi_trader_data(self):
        trader_one, trader_two = self._seed_database()

        at = self._open_dashboard()

        df = at.dataframe[0].value
        self.assertEqual(len(df), 2)

        row_one = df[df["Trader"] == f"TC (ID {trader_one.id})"].iloc[0]
        self.assertEqual(row_one["Total Lifecycles"], 3)
        self.assertEqual(row_one["Eligible"], 2)
        self.assertEqual(row_one["Not Scored"], 1)
        self.assertEqual(row_one["Wins"], 2)
        self.assertEqual(row_one["Win Rate"], "100.000000%")
        self.assertEqual(row_one["Avg Return"], "87.500000%")

        row_two = df[df["Trader"] == f"TC (ID {trader_two.id})"].iloc[0]
        self.assertEqual(row_two["Total Lifecycles"], 1)
        self.assertEqual(row_two["Data Errors"], "⚠ 1")
        self.assertEqual(row_two["Eligible"], 0)
        self.assertEqual(row_two["Win Rate"], "—")

    def test_duplicate_trader_names_separated_by_id_end_to_end(self):
        trader_one, trader_two = self._seed_database()
        self.assertNotEqual(trader_one.id, trader_two.id)

        at = self._open_dashboard()

        df = at.dataframe[0].value
        self.assertEqual(
            set(df["Trader"]),
            {f"TC (ID {trader_one.id})", f"TC (ID {trader_two.id})"},
        )

    def test_drilldown_reflects_real_lifecycle_evidence_for_one_trader(self):
        trader_one, _ = self._seed_database()

        at = self._open_dashboard()
        at = at.selectbox[0].set_value(trader_one.id).run()

        df = at.dataframe[1].value
        self.assertEqual(len(df), 3)
        symbols_by_status = dict(zip(df["Symbol"], df["Status"]))
        self.assertEqual(symbols_by_status["IBM"], "closed")
        self.assertEqual(symbols_by_status["NVDA"], "closed")
        self.assertEqual(symbols_by_status["MU"], "open")

        outcomes_by_symbol = dict(zip(df["Symbol"], df["Outcome"]))
        self.assertEqual(outcomes_by_symbol["IBM"], "win")
        self.assertEqual(outcomes_by_symbol["NVDA"], "win")
        self.assertEqual(outcomes_by_symbol["MU"], "not_scored")

    def test_data_error_lifecycle_surfaced_end_to_end(self):
        _, trader_two = self._seed_database()

        at = self._open_dashboard()
        at = at.selectbox[0].set_value(trader_two.id).run()

        df = at.dataframe[1].value
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["Symbol"], "AVGO")
        self.assertEqual(df.iloc[0]["Data Error"], "⚠")

        detail_text = "\n".join(element.value for element in at.markdown)
        self.assertIn("has no membership events", detail_text)

    def test_not_scored_lifecycle_surfaced_end_to_end(self):
        trader_one, _ = self._seed_database()

        at = self._open_dashboard()
        at = at.selectbox[0].set_value(trader_one.id).run()

        df = at.dataframe[1].value
        mu_row = df[df["Symbol"] == "MU"].iloc[0]
        self.assertIn("status_open", mu_row["Exclusion Reasons"])

    def test_exit_leg_evidence_matches_real_partial_exit_sequence(self):
        trader_one, _ = self._seed_database()

        at = self._open_dashboard()
        at = at.selectbox[0].set_value(trader_one.id).run()

        df = at.dataframe[1].value
        nvda_lifecycle_id = int(df[df["Symbol"] == "NVDA"].iloc[0]["Lifecycle ID"])
        at = at.selectbox[1].set_value(nvda_lifecycle_id).run()

        exit_leg_df = at.dataframe[2].value
        self.assertEqual(list(exit_leg_df["Event Type"]), ["PARTIAL_EXIT", "FULL_EXIT"])
        self.assertEqual(list(exit_leg_df["Consumed Fraction"]), ["1/2", "1/2"])
        self.assertEqual(list(exit_leg_df["Exit Price"]), ["1.50", "2.00"])
        self.assertEqual(list(exit_leg_df["Sequence Index"]), [2, 3])

    def test_dashboard_workflow_never_writes_to_real_database(self):
        trader_one, _ = self._seed_database()
        before = self._real_row_counts()

        at = self._open_dashboard()
        at.selectbox[0].set_value(trader_one.id).run()

        after = self._real_row_counts()
        self.assertEqual(before, after)

    def test_missing_database_shows_empty_message_end_to_end(self):
        self.assertFalse(self.db_path.exists())

        at = self._open_dashboard()

        self.assertEqual(len(at.info), 1)
        self.assertEqual(at.info[0].value, "No trader performance data found.")
        self.assertFalse(self.db_path.exists())


if __name__ == "__main__":
    unittest.main()
