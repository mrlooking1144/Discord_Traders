"""Real-SQLite integration tests for the Trader Performance dashboard
(Recovery Milestone R8a's core dashboard, extended by Recovery
Milestone R8b's ranking/minimum-sample/CSV export additions).

Exercises the complete Trader Performance dashboard path against a real,
unique per-test temporary SQLite database: real trade_signals -> real
TradeService.rebuild_all_lifecycles() -> real persisted
trade_lifecycles/trade_lifecycle_events, followed by a real read through
the Trader Performance workflow (database.service.TradeService.
list_trader_performance_summaries()/list_current_trade_lifecycle_analytics()
-> the real database.analytics engine -> app.dashboard_formatting's pure
helpers, now including R8b's rank_trader_summaries()/CSV export
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

import csv
import io
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.dashboard_formatting import (
    LIFECYCLE_CSV_FIELDNAMES,
    SUMMARY_CSV_FIELDNAMES,
    build_lifecycle_csv_rows,
    build_summary_csv_rows,
    filter_lifecycle_results,
    rank_trader_summaries,
    rows_to_csv_string,
)
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
        at = at.selectbox[2].set_value(trader_one.id).run()

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
        at = at.selectbox[2].set_value(trader_two.id).run()

        df = at.dataframe[1].value
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["Symbol"], "AVGO")
        self.assertEqual(df.iloc[0]["Data Error"], "⚠")

        detail_text = "\n".join(element.value for element in at.markdown)
        self.assertIn("has no membership events", detail_text)

    def test_not_scored_lifecycle_surfaced_end_to_end(self):
        trader_one, _ = self._seed_database()

        at = self._open_dashboard()
        at = at.selectbox[2].set_value(trader_one.id).run()

        df = at.dataframe[1].value
        mu_row = df[df["Symbol"] == "MU"].iloc[0]
        self.assertIn("status_open", mu_row["Exclusion Reasons"])

    def test_exit_leg_evidence_matches_real_partial_exit_sequence(self):
        trader_one, _ = self._seed_database()

        at = self._open_dashboard()
        at = at.selectbox[2].set_value(trader_one.id).run()

        df = at.dataframe[1].value
        nvda_lifecycle_id = int(df[df["Symbol"] == "NVDA"].iloc[0]["Lifecycle ID"])
        at = at.selectbox[3].set_value(nvda_lifecycle_id).run()

        exit_leg_df = at.dataframe[2].value
        self.assertEqual(list(exit_leg_df["Event Type"]), ["PARTIAL_EXIT", "FULL_EXIT"])
        self.assertEqual(list(exit_leg_df["Consumed Fraction"]), ["1/2", "1/2"])
        self.assertEqual(list(exit_leg_df["Exit Price"]), ["1.50", "2.00"])
        self.assertEqual(list(exit_leg_df["Sequence Index"]), [2, 3])

    def test_dashboard_workflow_never_writes_to_real_database(self):
        trader_one, _ = self._seed_database()
        before = self._real_row_counts()

        at = self._open_dashboard()
        at.selectbox[2].set_value(trader_one.id).run()

        after = self._real_row_counts()
        self.assertEqual(before, after)

    def test_missing_database_shows_empty_message_end_to_end(self):
        self.assertFalse(self.db_path.exists())

        at = self._open_dashboard()

        self.assertEqual(len(at.info), 1)
        self.assertEqual(at.info[0].value, "No trader performance data found.")
        self.assertFalse(self.db_path.exists())


class RankingAndExportIntegrationTests(_DashboardIntegrationTestCase):
    """Recovery Milestone R8b: ranking, minimum-sample tiering, and CSV
    export, proven against real persisted data through the real
    TradeService analytics pipeline - no mocking."""

    def _seed_ranking_database(self):
        """Build a real database with four distinctly-ranked traders:
        trader_a (avg 100%, 3 eligible - qualifies), trader_b (avg 50%,
        3 eligible - qualifies), trader_c (avg 100%, only 2 eligible -
        below the default threshold of 3 despite tying trader_a's
        average), and trader_d (no eligible lifecycles at all - an
        open-only lifecycle). Proves metric ordering, minimum-sample
        tiering, and None-always-last behavior together against real
        persisted data. Traders are created in alphabetical order, so
        trader_a.id < trader_b.id < trader_c.id < trader_d.id -
        deterministic tie-break assertions never hardcode raw ids."""
        config = DatabaseConfig(db_path=str(self.db_path))
        initialize_database(config)
        conn = get_connection(config)
        try:
            service = TradeService(conn)
            self.source = repository.get_or_create_source(conn, "discord")
            conn.commit()
            trader_a = repository.create_trader(conn, self.source.id, "Alpha")
            trader_b = repository.create_trader(conn, self.source.id, "Bravo")
            trader_c = repository.create_trader(conn, self.source.id, "Charlie")
            trader_d = repository.create_trader(conn, self.source.id, "Delta")
            conn.commit()

            for symbol in ("TA1", "TA2", "TA3"):
                self._make_signal(
                    conn, trader_id=trader_a.id, symbol=symbol, action="BTO",
                    event_type="ENTRY", price=Decimal("1.00"), strike=Decimal("100"),
                )
                self._make_signal(
                    conn, trader_id=trader_a.id, symbol=symbol, action="STC",
                    event_type="FULL_EXIT", price=Decimal("2.00"), strike=Decimal("100"),
                    qualifier="ALL OUT",
                )

            for symbol in ("TB1", "TB2", "TB3"):
                self._make_signal(
                    conn, trader_id=trader_b.id, symbol=symbol, action="BTO",
                    event_type="ENTRY", price=Decimal("1.00"), strike=Decimal("100"),
                )
                self._make_signal(
                    conn, trader_id=trader_b.id, symbol=symbol, action="STC",
                    event_type="FULL_EXIT", price=Decimal("1.50"), strike=Decimal("100"),
                    qualifier="ALL OUT",
                )

            for symbol in ("TC1", "TC2"):
                self._make_signal(
                    conn, trader_id=trader_c.id, symbol=symbol, action="BTO",
                    event_type="ENTRY", price=Decimal("1.00"), strike=Decimal("100"),
                )
                self._make_signal(
                    conn, trader_id=trader_c.id, symbol=symbol, action="STC",
                    event_type="FULL_EXIT", price=Decimal("2.00"), strike=Decimal("100"),
                    qualifier="ALL OUT",
                )

            self._make_signal(
                conn, trader_id=trader_d.id, symbol="TD1", action="BTO",
                event_type="ENTRY", price=Decimal("5.00"), strike=Decimal("100"),
            )

            service.rebuild_all_lifecycles()
            conn.commit()
        finally:
            conn.close()

        return trader_a, trader_b, trader_c, trader_d

    def test_default_ranking_orders_qualifying_traders_then_tiers_below_threshold(
        self,
    ):
        trader_a, trader_b, trader_c, trader_d = self._seed_ranking_database()

        at = self._open_dashboard()

        df = at.dataframe[0].value
        self.assertEqual(
            list(df["Trader"]),
            [
                f"Alpha (ID {trader_a.id})",
                f"Bravo (ID {trader_b.id})",
                f"Charlie (ID {trader_c.id})",
                f"Delta (ID {trader_d.id})",
            ],
        )

    def test_meets_minimum_sample_column_reflects_real_eligible_counts(self):
        trader_a, trader_b, trader_c, trader_d = self._seed_ranking_database()

        at = self._open_dashboard()

        df = at.dataframe[0].value
        row_a = df[df["Trader"] == f"Alpha (ID {trader_a.id})"].iloc[0]
        row_c = df[df["Trader"] == f"Charlie (ID {trader_c.id})"].iloc[0]
        row_d = df[df["Trader"] == f"Delta (ID {trader_d.id})"].iloc[0]
        self.assertEqual(row_a["Meets Minimum Sample"], "Yes")
        self.assertEqual(row_c["Meets Minimum Sample"], "No (2 < 3)")
        self.assertEqual(row_d["Meets Minimum Sample"], "No (0 < 3)")

    def test_ascending_direction_through_real_ui_reverses_qualifying_tier_only(self):
        trader_a, trader_b, trader_c, trader_d = self._seed_ranking_database()

        at = self._open_dashboard()
        at = at.selectbox[1].set_value("Ascending").run()

        df = at.dataframe[0].value
        self.assertEqual(
            list(df["Trader"]),
            [
                f"Bravo (ID {trader_b.id})",
                f"Alpha (ID {trader_a.id})",
                f"Charlie (ID {trader_c.id})",
                f"Delta (ID {trader_d.id})",
            ],
        )

    def test_raising_threshold_through_real_ui_requalifies_charlie_and_reorders(self):
        trader_a, trader_b, trader_c, trader_d = self._seed_ranking_database()

        at = self._open_dashboard()
        at = at.number_input[0].set_value(2).run()

        df = at.dataframe[0].value
        self.assertEqual(
            list(df["Trader"]),
            [
                f"Alpha (ID {trader_a.id})",
                f"Charlie (ID {trader_c.id})",
                f"Bravo (ID {trader_b.id})",
                f"Delta (ID {trader_d.id})",
            ],
        )
        row_c = df[df["Trader"] == f"Charlie (ID {trader_c.id})"].iloc[0]
        self.assertEqual(row_c["Meets Minimum Sample"], "Yes")

    def test_summary_csv_exact_content_parsed_back_with_real_data(self):
        trader_a, trader_b, trader_c, trader_d = self._seed_ranking_database()
        config = DatabaseConfig(db_path=str(self.db_path))
        conn = get_connection(config)
        try:
            service = TradeService(conn)
            summaries = service.list_trader_performance_summaries()
        finally:
            conn.close()

        ranked = rank_trader_summaries(
            summaries, sort_metric="Average Return", descending=True,
            min_eligible_lifecycles=3,
        )
        csv_rows = build_summary_csv_rows(ranked, min_eligible_lifecycles=3)
        csv_text = rows_to_csv_string(csv_rows, SUMMARY_CSV_FIELDNAMES)

        reader = csv.DictReader(io.StringIO(csv_text))
        self.assertEqual(reader.fieldnames, list(SUMMARY_CSV_FIELDNAMES))
        parsed_rows = list(reader)
        self.assertEqual(len(parsed_rows), 4)

        self.assertEqual(parsed_rows[0]["Trader Name"], "Alpha")
        self.assertEqual(parsed_rows[0]["Trader ID"], str(trader_a.id))
        self.assertEqual(parsed_rows[0]["Meets Minimum Sample"], "Yes")
        self.assertEqual(parsed_rows[0]["Avg Return"], "100.000000%")

        self.assertEqual(parsed_rows[3]["Trader Name"], "Delta")
        self.assertEqual(parsed_rows[3]["Trader ID"], str(trader_d.id))
        self.assertEqual(parsed_rows[3]["Meets Minimum Sample"], "No (0 < 3)")
        self.assertEqual(parsed_rows[3]["Avg Return"], "—")

    def test_drilldown_csv_exact_content_for_selected_trader_with_filters(self):
        trader_a, _, _, _ = self._seed_ranking_database()
        config = DatabaseConfig(db_path=str(self.db_path))
        conn = get_connection(config)
        try:
            service = TradeService(conn)
            lifecycle_results = service.list_current_trade_lifecycle_analytics(
                trader_id=trader_a.id
            )
        finally:
            conn.close()

        filtered = filter_lifecycle_results(lifecycle_results, statuses=["closed"])
        csv_rows = build_lifecycle_csv_rows(filtered)
        csv_text = rows_to_csv_string(csv_rows, LIFECYCLE_CSV_FIELDNAMES)

        reader = csv.DictReader(io.StringIO(csv_text))
        self.assertEqual(reader.fieldnames, list(LIFECYCLE_CSV_FIELDNAMES))
        parsed_rows = list(reader)
        self.assertEqual(len(parsed_rows), 3)
        self.assertEqual(
            {row["Symbol"] for row in parsed_rows}, {"TA1", "TA2", "TA3"}
        )
        self.assertEqual({row["Return"] for row in parsed_rows}, {"100.000000%"})
        for row in parsed_rows:
            self.assertNotIn("analytics_error_detail", row)

    def test_duplicate_trader_names_remain_distinct_in_ranked_csv_export(self):
        trader_one, trader_two = self._seed_database()
        config = DatabaseConfig(db_path=str(self.db_path))
        conn = get_connection(config)
        try:
            service = TradeService(conn)
            summaries = service.list_trader_performance_summaries()
        finally:
            conn.close()

        ranked = rank_trader_summaries(
            summaries, sort_metric="Average Return", descending=True,
            min_eligible_lifecycles=3,
        )
        csv_rows = build_summary_csv_rows(ranked, min_eligible_lifecycles=3)
        ids = {row["Trader ID"] for row in csv_rows}
        self.assertEqual(ids, {trader_one.id, trader_two.id})
        names = [row["Trader Name"] for row in csv_rows]
        self.assertEqual(names, ["TC", "TC"])

    def test_database_unchanged_after_ranking_and_csv_export_through_real_ui(self):
        self._seed_ranking_database()
        before = self._real_row_counts()

        at = self._open_dashboard()
        at = at.selectbox[1].set_value("Ascending").run()
        at = at.number_input[0].set_value(2).run()

        after = self._real_row_counts()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
