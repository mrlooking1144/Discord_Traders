"""Tests for Recovery Milestone R8a's pure presentation helper module
(app/dashboard_formatting.py).

Covers app/dashboard_formatting.py only - no database, no repository,
no service, no analytics, no Streamlit. All fixtures are synthetic
plain dicts matching the exact shape
TradeService.list_trader_performance_summaries()/
list_current_trade_lifecycle_analytics() already return.
"""

import csv
import io
import unittest

from app.dashboard_formatting import (
    LIFECYCLE_CSV_FIELDNAMES,
    SORT_DIRECTION_CHOICES,
    SORT_METRIC_CHOICES,
    SUMMARY_CSV_FIELDNAMES,
    build_exit_leg_display_rows,
    build_lifecycle_csv_rows,
    build_lifecycle_detail,
    build_lifecycle_display_row,
    build_lifecycle_display_rows,
    build_summary_csv_row,
    build_summary_csv_rows,
    build_summary_display_row,
    build_summary_display_rows,
    build_trader_label,
    filter_lifecycle_results,
    format_data_error_count,
    format_minimum_sample,
    format_optional_value,
    format_percent,
    meets_minimum_sample,
    rank_trader_summaries,
    rows_to_csv_string,
)

_MISSING = "—"


def _summary(**overrides):
    base = {
        "trader_id": 1,
        "trader_name": "TC",
        "total_lifecycle_count": 3,
        "open_count": 1,
        "partially_closed_count": 0,
        "closed_count": 1,
        "orphan_count": 0,
        "unresolved_count": 0,
        "invalid_count": 0,
        "snapshot_error_count": 1,
        "eligible_lifecycle_count": 1,
        "not_scored_count": 1,
        "winning_count": 1,
        "losing_count": 0,
        "breakeven_count": 0,
        "win_rate_pct": "100.000000",
        "loss_rate_pct": "0.000000",
        "breakeven_rate_pct": "0.000000",
        "average_gross_price_return_pct": "50.000000",
        "median_gross_price_return_pct": "50.000000",
        "average_winner_price_return_pct": "50.000000",
        "average_loser_price_return_pct": None,
        "all_current_lifecycle_ids": (1, 2, 3),
        "eligible_lifecycle_ids": (1,),
        "return_ineligible_lifecycle_ids": (2,),
        "snapshot_error_lifecycle_ids": (3,),
        "exclusion_reason_counts": {},
    }
    base.update(overrides)
    return base


def _lifecycle_result(**overrides):
    base = {
        "trade_lifecycle_id": 42,
        "trader_id": 1,
        "trader_name": "TC",
        "is_current": True,
        "superseded_at": None,
        "status": "closed",
        "outcome": "win",
        "direction": "long",
        "symbol": "IBM",
        "option_type": "call",
        "strike": "207.5",
        "expiration": "2026-07-24",
        "opened_at": "2026-07-20T10:00:00+00:00",
        "closed_at": "2026-07-21T10:00:00+00:00",
        "entry_price": "1.00",
        "terminal_exit_price": "2.00",
        "weighted_average_exit_price": "2.00",
        "exit_legs": [
            {
                "trade_lifecycle_event_id": 5,
                "trade_signal_id": 105,
                "sequence_index": 2,
                "event_type": "FULL_EXIT",
                "consumed_fraction": "1",
                "exit_price": "2.00",
            }
        ],
        "gross_price_return_pct": "100.000000",
        "eligible_for_status_counts": True,
        "eligible_for_outcome_metrics": True,
        "eligible_for_return_metrics": True,
        "lifecycle_ambiguity_flags": [],
        "analytics_exclusion_reasons": [],
        "analytics_error_detail": None,
        "source_event_ids": [4, 5],
    }
    base.update(overrides)
    return base


class FormatOptionalValueTests(unittest.TestCase):
    def test_none_renders_em_dash(self):
        self.assertEqual(format_optional_value(None), _MISSING)

    def test_string_unchanged(self):
        self.assertEqual(format_optional_value("call"), "call")

    def test_int_stringified(self):
        self.assertEqual(format_optional_value(42), "42")

    def test_decimal_string_unchanged(self):
        self.assertEqual(format_optional_value("207.5"), "207.5")


class FormatPercentTests(unittest.TestCase):
    def test_none_renders_em_dash_not_zero(self):
        self.assertEqual(format_percent(None), _MISSING)

    def test_zero_value_is_not_none(self):
        self.assertEqual(format_percent("0.000000"), "0.000000%")

    def test_positive_value_appends_percent_sign(self):
        self.assertEqual(format_percent("100.000000"), "100.000000%")

    def test_negative_value_sign_preserved_never_rerounded(self):
        self.assertEqual(format_percent("-50.000000"), "-50.000000%")


class FormatDataErrorCountTests(unittest.TestCase):
    def test_zero_renders_plain_zero(self):
        self.assertEqual(format_data_error_count(0), "0")

    def test_nonzero_renders_warning_indicator(self):
        self.assertEqual(format_data_error_count(1), "⚠ 1")
        self.assertEqual(format_data_error_count(5), "⚠ 5")


class BuildTraderLabelTests(unittest.TestCase):
    def test_label_format(self):
        self.assertEqual(build_trader_label(3, "TC"), "TC (ID 3)")

    def test_duplicate_names_produce_distinct_labels(self):
        label_one = build_trader_label(3, "TC")
        label_two = build_trader_label(7, "TC")
        self.assertNotEqual(label_one, label_two)
        self.assertIn("3", label_one)
        self.assertIn("7", label_two)


class MeetsMinimumSampleTests(unittest.TestCase):
    def test_below_threshold_returns_false(self):
        summary = _summary(eligible_lifecycle_count=2)
        self.assertFalse(meets_minimum_sample(summary, 3))

    def test_at_threshold_returns_true(self):
        summary = _summary(eligible_lifecycle_count=3)
        self.assertTrue(meets_minimum_sample(summary, 3))

    def test_above_threshold_returns_true(self):
        summary = _summary(eligible_lifecycle_count=4)
        self.assertTrue(meets_minimum_sample(summary, 3))

    def test_zero_threshold_always_qualifies(self):
        summary = _summary(eligible_lifecycle_count=0)
        self.assertTrue(meets_minimum_sample(summary, 0))


class FormatMinimumSampleTests(unittest.TestCase):
    def test_qualifying_trader_renders_yes(self):
        summary = _summary(eligible_lifecycle_count=3)
        self.assertEqual(format_minimum_sample(summary, 3), "Yes")

    def test_below_threshold_trader_renders_clear_count_comparison(self):
        summary = _summary(eligible_lifecycle_count=2)
        self.assertEqual(format_minimum_sample(summary, 3), "No (2 < 3)")

    def test_zero_eligible_renders_clear_count_comparison(self):
        summary = _summary(eligible_lifecycle_count=0)
        self.assertEqual(format_minimum_sample(summary, 3), "No (0 < 3)")


class BuildSummaryDisplayRowTests(unittest.TestCase):
    def test_all_optional_none_fields_render_as_em_dash(self):
        summary = _summary(
            win_rate_pct=None,
            loss_rate_pct=None,
            breakeven_rate_pct=None,
            average_gross_price_return_pct=None,
            median_gross_price_return_pct=None,
            average_winner_price_return_pct=None,
            average_loser_price_return_pct=None,
        )
        row = build_summary_display_row(summary, min_eligible_lifecycles=3)

        for column in (
            "Win Rate", "Loss Rate", "Breakeven Rate",
            "Avg Return", "Median Return",
            "Avg Winner Return", "Avg Loser Return",
        ):
            self.assertEqual(row[column], _MISSING, msg=column)

    def test_exact_column_key_set(self):
        row = build_summary_display_row(_summary(), min_eligible_lifecycles=3)
        expected_keys = {
            "Trader", "Meets Minimum Sample", "Total Lifecycles", "Open",
            "Partially Closed", "Closed", "Orphan", "Unresolved", "Invalid",
            "Data Errors", "Eligible", "Not Scored", "Wins", "Losses",
            "Breakeven", "Win Rate", "Loss Rate", "Breakeven Rate", "Avg Return",
            "Median Return", "Avg Winner Return", "Avg Loser Return",
        }
        self.assertEqual(set(row.keys()), expected_keys)

    def test_data_errors_column_reflects_snapshot_error_count(self):
        zero_errors = build_summary_display_row(
            _summary(snapshot_error_count=0), min_eligible_lifecycles=3
        )
        self.assertEqual(zero_errors["Data Errors"], "0")

        with_errors = build_summary_display_row(
            _summary(snapshot_error_count=2), min_eligible_lifecycles=3
        )
        self.assertEqual(with_errors["Data Errors"], "⚠ 2")

    def test_no_analytics_error_detail_key_ever_present(self):
        row = build_summary_display_row(_summary(), min_eligible_lifecycles=3)
        self.assertNotIn("analytics_error_detail", row)
        self.assertNotIn("Analytics Error Detail", row)
        self.assertNotIn("Error Detail", row)

    def test_trader_column_uses_disambiguated_label(self):
        row = build_summary_display_row(
            _summary(trader_id=9, trader_name="Sarang"), min_eligible_lifecycles=3
        )
        self.assertEqual(row["Trader"], "Sarang (ID 9)")

    def test_meets_minimum_sample_column_reflects_threshold(self):
        qualifying = build_summary_display_row(
            _summary(eligible_lifecycle_count=3), min_eligible_lifecycles=3
        )
        below = build_summary_display_row(
            _summary(eligible_lifecycle_count=1), min_eligible_lifecycles=3
        )
        self.assertEqual(qualifying["Meets Minimum Sample"], "Yes")
        self.assertEqual(below["Meets Minimum Sample"], "No (1 < 3)")


class BuildSummaryDisplayRowsTests(unittest.TestCase):
    def test_order_preserved(self):
        summaries = [_summary(trader_id=3), _summary(trader_id=1), _summary(trader_id=2)]
        rows = build_summary_display_rows(summaries, min_eligible_lifecycles=3)
        self.assertEqual(
            [row["Trader"] for row in rows],
            [build_trader_label(3, "TC"), build_trader_label(1, "TC"), build_trader_label(2, "TC")],
        )

    def test_empty_list(self):
        self.assertEqual(build_summary_display_rows([], min_eligible_lifecycles=3), [])


class RankTraderSummariesTests(unittest.TestCase):
    """Covers Recovery Milestone R8b's rank_trader_summaries()."""

    def test_sort_metric_choices_are_all_valid_and_in_approved_order(self):
        self.assertEqual(
            list(SORT_METRIC_CHOICES),
            [
                "Average Return", "Median Return", "Win Rate", "Loss Rate",
                "Avg Winner Return", "Avg Loser Return", "Eligible Lifecycles",
                "Total Lifecycles", "Trader Name", "Trader ID",
            ],
        )
        self.assertEqual(list(SORT_DIRECTION_CHOICES), ["Descending", "Ascending"])

    def test_unknown_metric_raises(self):
        summaries = [_summary(trader_id=1)]
        with self.assertRaises(ValueError):
            rank_trader_summaries(
                summaries, sort_metric="Not A Real Metric",
                descending=True, min_eligible_lifecycles=0,
            )

    def test_decimal_percentage_metric_sorts_numerically_not_lexicographically(self):
        # "10.000000" sorts before "9.000000" lexicographically as a
        # string, but numerically 10 > 9 - this only passes if the
        # comparison is Decimal, never a string compare.
        summaries = [
            _summary(trader_id=1, eligible_lifecycle_count=3, average_gross_price_return_pct="9.000000"),
            _summary(trader_id=2, eligible_lifecycle_count=3, average_gross_price_return_pct="10.000000"),
        ]
        ranked = rank_trader_summaries(
            summaries, sort_metric="Average Return", descending=True,
            min_eligible_lifecycles=0,
        )
        self.assertEqual([s["trader_id"] for s in ranked], [2, 1])

        ranked_asc = rank_trader_summaries(
            summaries, sort_metric="Average Return", descending=False,
            min_eligible_lifecycles=0,
        )
        self.assertEqual([s["trader_id"] for s in ranked_asc], [1, 2])

    def test_every_decimal_metric_sorts_descending_and_ascending(self):
        decimal_metrics = {
            "Average Return": "average_gross_price_return_pct",
            "Median Return": "median_gross_price_return_pct",
            "Win Rate": "win_rate_pct",
            "Loss Rate": "loss_rate_pct",
            "Avg Winner Return": "average_winner_price_return_pct",
            "Avg Loser Return": "average_loser_price_return_pct",
        }
        for metric, field in decimal_metrics.items():
            with self.subTest(metric=metric):
                summaries = [
                    _summary(trader_id=1, eligible_lifecycle_count=3, **{field: "20.000000"}),
                    _summary(trader_id=2, eligible_lifecycle_count=3, **{field: "80.000000"}),
                ]
                descending = rank_trader_summaries(
                    summaries, sort_metric=metric, descending=True,
                    min_eligible_lifecycles=0,
                )
                self.assertEqual([s["trader_id"] for s in descending], [2, 1])

                ascending = rank_trader_summaries(
                    summaries, sort_metric=metric, descending=False,
                    min_eligible_lifecycles=0,
                )
                self.assertEqual([s["trader_id"] for s in ascending], [1, 2])

    def test_integer_count_metric_sorts_numerically_not_lexicographically(self):
        for metric, field in (
            ("Eligible Lifecycles", "eligible_lifecycle_count"),
            ("Total Lifecycles", "total_lifecycle_count"),
        ):
            with self.subTest(metric=metric):
                # 20 sorts before 9 lexicographically as a string
                # ("20" < "9"), but numerically 20 > 9 - only passes if
                # the comparison is int, never a string compare.
                summaries = [
                    _summary(trader_id=1, **{field: 9}),
                    _summary(trader_id=2, **{field: 20}),
                ]
                descending = rank_trader_summaries(
                    summaries, sort_metric=metric, descending=True,
                    min_eligible_lifecycles=0,
                )
                self.assertEqual(descending[0][field], 20)
                self.assertEqual(descending[1][field], 9)

    def test_trader_id_metric_sorts_numerically_not_lexicographically(self):
        summaries = [
            _summary(trader_id=9, eligible_lifecycle_count=3),
            _summary(trader_id=20, eligible_lifecycle_count=3),
        ]
        descending = rank_trader_summaries(
            summaries, sort_metric="Trader ID", descending=True,
            min_eligible_lifecycles=0,
        )
        self.assertEqual([s["trader_id"] for s in descending], [20, 9])

        ascending = rank_trader_summaries(
            summaries, sort_metric="Trader ID", descending=False,
            min_eligible_lifecycles=0,
        )
        self.assertEqual([s["trader_id"] for s in ascending], [9, 20])

    def test_trader_name_sorts_case_insensitively(self):
        summaries = [
            _summary(trader_id=1, trader_name="bob", eligible_lifecycle_count=3),
            _summary(trader_id=2, trader_name="Alice", eligible_lifecycle_count=3),
            _summary(trader_id=3, trader_name="charlie", eligible_lifecycle_count=3),
        ]
        ascending = rank_trader_summaries(
            summaries, sort_metric="Trader Name", descending=False,
            min_eligible_lifecycles=0,
        )
        self.assertEqual(
            [s["trader_name"] for s in ascending], ["Alice", "bob", "charlie"]
        )

        descending = rank_trader_summaries(
            summaries, sort_metric="Trader Name", descending=True,
            min_eligible_lifecycles=0,
        )
        self.assertEqual(
            [s["trader_name"] for s in descending], ["charlie", "bob", "Alice"]
        )

    def test_none_metric_value_always_sorts_last_in_either_direction(self):
        summaries = [
            _summary(trader_id=1, eligible_lifecycle_count=3, average_gross_price_return_pct=None),
            _summary(trader_id=2, eligible_lifecycle_count=3, average_gross_price_return_pct="5.000000"),
        ]
        descending = rank_trader_summaries(
            summaries, sort_metric="Average Return", descending=True,
            min_eligible_lifecycles=0,
        )
        self.assertEqual([s["trader_id"] for s in descending], [2, 1])

        ascending = rank_trader_summaries(
            summaries, sort_metric="Average Return", descending=False,
            min_eligible_lifecycles=0,
        )
        self.assertEqual([s["trader_id"] for s in ascending], [2, 1])

    def test_qualifying_traders_always_before_below_threshold_traders(self):
        summaries = [
            # Below threshold but a far larger return value - must not
            # outrank the qualifying trader despite the metric.
            _summary(trader_id=1, eligible_lifecycle_count=1, average_gross_price_return_pct="999.000000"),
            _summary(trader_id=2, eligible_lifecycle_count=3, average_gross_price_return_pct="1.000000"),
        ]
        ranked = rank_trader_summaries(
            summaries, sort_metric="Average Return", descending=True,
            min_eligible_lifecycles=3,
        )
        self.assertEqual([s["trader_id"] for s in ranked], [2, 1])

    def test_below_threshold_tier_still_ordered_by_metric_internally(self):
        summaries = [
            _summary(trader_id=1, eligible_lifecycle_count=1, average_gross_price_return_pct="10.000000"),
            _summary(trader_id=2, eligible_lifecycle_count=1, average_gross_price_return_pct="50.000000"),
        ]
        ranked = rank_trader_summaries(
            summaries, sort_metric="Average Return", descending=True,
            min_eligible_lifecycles=3,
        )
        self.assertEqual([s["trader_id"] for s in ranked], [2, 1])

    def test_tie_breaks_by_trader_id_ascending_unconditionally(self):
        summaries = [
            _summary(trader_id=9, eligible_lifecycle_count=3, average_gross_price_return_pct="50.000000"),
            _summary(trader_id=2, eligible_lifecycle_count=3, average_gross_price_return_pct="50.000000"),
        ]
        descending = rank_trader_summaries(
            summaries, sort_metric="Average Return", descending=True,
            min_eligible_lifecycles=0,
        )
        self.assertEqual([s["trader_id"] for s in descending], [2, 9])

        ascending = rank_trader_summaries(
            summaries, sort_metric="Average Return", descending=False,
            min_eligible_lifecycles=0,
        )
        self.assertEqual([s["trader_id"] for s in ascending], [2, 9])

    def test_tie_break_applies_between_two_none_values_too(self):
        summaries = [
            _summary(trader_id=9, eligible_lifecycle_count=0, average_gross_price_return_pct=None),
            _summary(trader_id=2, eligible_lifecycle_count=0, average_gross_price_return_pct=None),
        ]
        ranked = rank_trader_summaries(
            summaries, sort_metric="Average Return", descending=True,
            min_eligible_lifecycles=0,
        )
        self.assertEqual([s["trader_id"] for s in ranked], [2, 9])

    def test_input_list_never_mutated(self):
        summaries = [
            _summary(trader_id=3, eligible_lifecycle_count=3),
            _summary(trader_id=1, eligible_lifecycle_count=3),
        ]
        original_order = [s["trader_id"] for s in summaries]
        rank_trader_summaries(
            summaries, sort_metric="Average Return", descending=True,
            min_eligible_lifecycles=0,
        )
        self.assertEqual([s["trader_id"] for s in summaries], original_order)

    def test_returns_new_list_object(self):
        summaries = [_summary(trader_id=1, eligible_lifecycle_count=3)]
        ranked = rank_trader_summaries(
            summaries, sort_metric="Average Return", descending=True,
            min_eligible_lifecycles=0,
        )
        self.assertIsNot(ranked, summaries)


class FilterLifecycleResultsTests(unittest.TestCase):
    def test_no_filters_returns_unchanged_order(self):
        results = [
            _lifecycle_result(trade_lifecycle_id=1),
            _lifecycle_result(trade_lifecycle_id=2),
        ]
        filtered = filter_lifecycle_results(results)
        self.assertEqual(filtered, results)

    def test_status_filter_narrows(self):
        results = [
            _lifecycle_result(trade_lifecycle_id=1, status="closed"),
            _lifecycle_result(trade_lifecycle_id=2, status="open"),
        ]
        filtered = filter_lifecycle_results(results, statuses=["open"])
        self.assertEqual([r["trade_lifecycle_id"] for r in filtered], [2])

    def test_outcome_filter_narrows(self):
        results = [
            _lifecycle_result(trade_lifecycle_id=1, outcome="win"),
            _lifecycle_result(trade_lifecycle_id=2, outcome="loss"),
        ]
        filtered = filter_lifecycle_results(results, outcomes=["loss"])
        self.assertEqual([r["trade_lifecycle_id"] for r in filtered], [2])

    def test_combined_filters_apply_together_as_and(self):
        results = [
            _lifecycle_result(trade_lifecycle_id=1, status="closed", outcome="win"),
            _lifecycle_result(trade_lifecycle_id=2, status="closed", outcome="loss"),
            _lifecycle_result(trade_lifecycle_id=3, status="open", outcome="win"),
        ]
        filtered = filter_lifecycle_results(results, statuses=["closed"], outcomes=["win"])
        self.assertEqual([r["trade_lifecycle_id"] for r in filtered], [1])

    def test_symbol_filter_is_exact_and_case_insensitive_input(self):
        results = [
            _lifecycle_result(trade_lifecycle_id=1, symbol="IBM"),
            _lifecycle_result(trade_lifecycle_id=2, symbol="AVGO"),
        ]
        filtered = filter_lifecycle_results(results, symbol="ibm")
        self.assertEqual([r["trade_lifecycle_id"] for r in filtered], [1])

    def test_symbol_filter_never_substring_matches(self):
        results = [
            _lifecycle_result(trade_lifecycle_id=1, symbol="IBM"),
            _lifecycle_result(trade_lifecycle_id=2, symbol="IBMX"),
        ]
        filtered = filter_lifecycle_results(results, symbol="IBM")
        self.assertEqual([r["trade_lifecycle_id"] for r in filtered], [1])

    def test_blank_symbol_is_no_filter(self):
        results = [_lifecycle_result(trade_lifecycle_id=1, symbol="IBM")]
        filtered = filter_lifecycle_results(results, symbol="   ")
        self.assertEqual(filtered, results)

    def test_empty_statuses_and_outcomes_lists_mean_no_filter(self):
        results = [_lifecycle_result(trade_lifecycle_id=1)]
        filtered = filter_lifecycle_results(results, statuses=[], outcomes=[])
        self.assertEqual(filtered, results)

    def test_original_list_never_mutated(self):
        results = [_lifecycle_result(trade_lifecycle_id=1, status="open")]
        original_copy = list(results)
        filter_lifecycle_results(results, statuses=["closed"])
        self.assertEqual(results, original_copy)


class BuildLifecycleDisplayRowTests(unittest.TestCase):
    def test_no_analytics_error_detail_key_ever_present(self):
        result = _lifecycle_result(outcome="data_error", analytics_error_detail="boom")
        row = build_lifecycle_display_row(result)
        self.assertNotIn("analytics_error_detail", row)
        self.assertNotIn("Analytics Error Detail", row)
        self.assertNotIn("Error Detail", row)

    def test_data_error_indicator_matches_outcome(self):
        error_row = build_lifecycle_display_row(_lifecycle_result(outcome="data_error"))
        self.assertEqual(error_row["Data Error"], "⚠")

        clean_row = build_lifecycle_display_row(_lifecycle_result(outcome="win"))
        self.assertEqual(clean_row["Data Error"], _MISSING)

    def test_empty_ambiguity_and_exclusion_render_em_dash(self):
        row = build_lifecycle_display_row(
            _lifecycle_result(lifecycle_ambiguity_flags=[], analytics_exclusion_reasons=[])
        )
        self.assertEqual(row["Ambiguity Flags"], _MISSING)
        self.assertEqual(row["Exclusion Reasons"], _MISSING)

    def test_nonempty_ambiguity_and_exclusion_joined(self):
        row = build_lifecycle_display_row(
            _lifecycle_result(
                lifecycle_ambiguity_flags=["ambiguous_add_no_open_position"],
                analytics_exclusion_reasons=["status_open", "no_verified_opener"],
            )
        )
        self.assertEqual(row["Ambiguity Flags"], "ambiguous_add_no_open_position")
        self.assertEqual(row["Exclusion Reasons"], "status_open, no_verified_opener")

    def test_exact_column_key_set(self):
        row = build_lifecycle_display_row(_lifecycle_result())
        expected_keys = {
            "Lifecycle ID", "Symbol", "Option Type", "Strike", "Expiration",
            "Status", "Outcome", "Direction", "Entry Price", "Terminal Exit Price",
            "Weighted Avg Exit Price", "Return", "Opened At", "Closed At",
            "Ambiguity Flags", "Exclusion Reasons", "Data Error",
        }
        self.assertEqual(set(row.keys()), expected_keys)

    def test_none_optional_fields_render_em_dash(self):
        row = build_lifecycle_display_row(
            _lifecycle_result(
                option_type=None, strike=None, expiration=None, direction=None,
                entry_price=None, terminal_exit_price=None,
                weighted_average_exit_price=None, opened_at=None, closed_at=None,
                gross_price_return_pct=None,
            )
        )
        for column in (
            "Option Type", "Strike", "Expiration", "Direction", "Entry Price",
            "Terminal Exit Price", "Weighted Avg Exit Price", "Opened At", "Closed At",
        ):
            self.assertEqual(row[column], _MISSING, msg=column)
        self.assertEqual(row["Return"], _MISSING)


class BuildLifecycleDisplayRowsTests(unittest.TestCase):
    def test_order_preserved(self):
        results = [
            _lifecycle_result(trade_lifecycle_id=3),
            _lifecycle_result(trade_lifecycle_id=1),
        ]
        rows = build_lifecycle_display_rows(results)
        self.assertEqual([r["Lifecycle ID"] for r in rows], [3, 1])

    def test_empty_list(self):
        self.assertEqual(build_lifecycle_display_rows([]), [])


class BuildExitLegDisplayRowsTests(unittest.TestCase):
    def test_column_mapping_and_order(self):
        exit_legs = [
            {
                "trade_lifecycle_event_id": 10,
                "trade_signal_id": 100,
                "sequence_index": 2,
                "event_type": "PARTIAL_EXIT",
                "consumed_fraction": "1/2",
                "exit_price": "1.50",
            },
            {
                "trade_lifecycle_event_id": 11,
                "trade_signal_id": 101,
                "sequence_index": 3,
                "event_type": "FULL_EXIT",
                "consumed_fraction": "1/2",
                "exit_price": "2.00",
            },
        ]
        rows = build_exit_leg_display_rows(exit_legs)

        self.assertEqual(
            rows,
            [
                {
                    "Event Type": "PARTIAL_EXIT",
                    "Consumed Fraction": "1/2",
                    "Exit Price": "1.50",
                    "Sequence Index": 2,
                },
                {
                    "Event Type": "FULL_EXIT",
                    "Consumed Fraction": "1/2",
                    "Exit Price": "2.00",
                    "Sequence Index": 3,
                },
            ],
        )

    def test_none_exit_price_renders_em_dash(self):
        exit_legs = [
            {
                "trade_lifecycle_event_id": 10,
                "trade_signal_id": 100,
                "sequence_index": 1,
                "event_type": "PARTIAL_EXIT",
                "consumed_fraction": "1/2",
                "exit_price": None,
            }
        ]
        rows = build_exit_leg_display_rows(exit_legs)
        self.assertEqual(rows[0]["Exit Price"], _MISSING)

    def test_empty_list(self):
        self.assertEqual(build_exit_leg_display_rows([]), [])


class BuildLifecycleDetailTests(unittest.TestCase):
    def test_data_error_result_shows_verbatim_error_detail(self):
        result = _lifecycle_result(
            outcome="data_error",
            analytics_error_detail="trade_lifecycle_id 42 has no membership events.",
            exit_legs=[],
        )
        detail = build_lifecycle_detail(result)
        self.assertEqual(
            detail["error_detail"],
            "trade_lifecycle_id 42 has no membership events.",
        )

    def test_non_data_error_result_shows_em_dash(self):
        result = _lifecycle_result(outcome="win", analytics_error_detail=None)
        detail = build_lifecycle_detail(result)
        self.assertEqual(detail["error_detail"], _MISSING)

    def test_exit_leg_rows_delegates_to_build_exit_leg_display_rows(self):
        exit_legs = [
            {
                "trade_lifecycle_event_id": 5,
                "trade_signal_id": 105,
                "sequence_index": 1,
                "event_type": "FULL_EXIT",
                "consumed_fraction": "1",
                "exit_price": "2.00",
            }
        ]
        result = _lifecycle_result(exit_legs=exit_legs)
        detail = build_lifecycle_detail(result)
        self.assertEqual(detail["exit_leg_rows"], build_exit_leg_display_rows(exit_legs))

    def test_empty_exit_legs_yields_empty_rows(self):
        result = _lifecycle_result(exit_legs=[])
        detail = build_lifecycle_detail(result)
        self.assertEqual(detail["exit_leg_rows"], [])


class BuildSummaryCsvRowTests(unittest.TestCase):
    """Covers Recovery Milestone R8b's build_summary_csv_row(s)."""

    def test_exact_column_key_set_matches_fieldnames_contract(self):
        row = build_summary_csv_row(_summary(), min_eligible_lifecycles=3)
        self.assertEqual(set(row.keys()), set(SUMMARY_CSV_FIELDNAMES))

    def test_trader_name_and_id_are_separate_columns_not_combined_label(self):
        row = build_summary_csv_row(
            _summary(trader_id=9, trader_name="Sarang"), min_eligible_lifecycles=3
        )
        self.assertEqual(row["Trader Name"], "Sarang")
        self.assertEqual(row["Trader ID"], 9)
        self.assertNotIn("Trader", row)

    def test_meets_minimum_sample_included(self):
        row = build_summary_csv_row(
            _summary(eligible_lifecycle_count=1), min_eligible_lifecycles=3
        )
        self.assertEqual(row["Meets Minimum Sample"], "No (1 < 3)")

    def test_remaining_fields_match_display_row_formatting(self):
        summary = _summary(win_rate_pct="100.000000", snapshot_error_count=2)
        csv_row = build_summary_csv_row(summary, min_eligible_lifecycles=3)
        display_row = build_summary_display_row(summary, min_eligible_lifecycles=3)
        self.assertEqual(csv_row["Win Rate"], display_row["Win Rate"])
        self.assertEqual(csv_row["Data Errors"], display_row["Data Errors"])

    def test_rows_preserve_caller_order(self):
        summaries = [_summary(trader_id=3), _summary(trader_id=1), _summary(trader_id=2)]
        rows = build_summary_csv_rows(summaries, min_eligible_lifecycles=3)
        self.assertEqual([row["Trader ID"] for row in rows], [3, 1, 2])

    def test_empty_list(self):
        self.assertEqual(build_summary_csv_rows([], min_eligible_lifecycles=3), [])


class BuildLifecycleCsvRowsTests(unittest.TestCase):
    """Covers Recovery Milestone R8b's build_lifecycle_csv_rows()."""

    def test_identical_to_display_rows(self):
        results = [_lifecycle_result(trade_lifecycle_id=1), _lifecycle_result(trade_lifecycle_id=2)]
        self.assertEqual(build_lifecycle_csv_rows(results), build_lifecycle_display_rows(results))

    def test_never_includes_analytics_error_detail(self):
        result = _lifecycle_result(outcome="data_error", analytics_error_detail="boom")
        rows = build_lifecycle_csv_rows([result])
        self.assertNotIn("analytics_error_detail", rows[0])
        self.assertNotIn("Error Detail", rows[0])

    def test_order_preserved(self):
        results = [_lifecycle_result(trade_lifecycle_id=5), _lifecycle_result(trade_lifecycle_id=1)]
        rows = build_lifecycle_csv_rows(results)
        self.assertEqual([r["Lifecycle ID"] for r in rows], [5, 1])

    def test_empty_list(self):
        self.assertEqual(build_lifecycle_csv_rows([]), [])


class RowsToCsvStringTests(unittest.TestCase):
    """Covers Recovery Milestone R8b's rows_to_csv_string() - stdlib csv
    + io.StringIO only, deterministic \\r\\n output, never pandas."""

    def test_header_only_for_empty_rows(self):
        text = rows_to_csv_string([], ("A", "B"))
        self.assertEqual(text, "A,B\r\n")

    def test_exact_header_and_column_order(self):
        rows = [{"B": "2", "A": "1"}]
        text = rows_to_csv_string(rows, ("A", "B"))
        reader = csv.reader(io.StringIO(text))
        header = next(reader)
        self.assertEqual(header, ["A", "B"])

    def test_row_values_round_trip(self):
        rows = [{"A": "1", "B": "2"}, {"A": "3", "B": "4"}]
        text = rows_to_csv_string(rows, ("A", "B"))
        reader = csv.DictReader(io.StringIO(text))
        self.assertEqual(list(reader), [{"A": "1", "B": "2"}, {"A": "3", "B": "4"}])

    def test_comma_in_value_is_quoted_and_round_trips(self):
        rows = [{"A": "Smith, Jane", "B": "2"}]
        text = rows_to_csv_string(rows, ("A", "B"))
        self.assertIn('"Smith, Jane"', text)
        reader = csv.DictReader(io.StringIO(text))
        self.assertEqual(next(reader)["A"], "Smith, Jane")

    def test_quote_in_value_is_escaped_and_round_trips(self):
        rows = [{"A": 'Say "hi"', "B": "2"}]
        text = rows_to_csv_string(rows, ("A", "B"))
        reader = csv.DictReader(io.StringIO(text))
        self.assertEqual(next(reader)["A"], 'Say "hi"')

    def test_newline_in_value_is_quoted_and_round_trips(self):
        rows = [{"A": "line one\nline two", "B": "2"}]
        text = rows_to_csv_string(rows, ("A", "B"))
        reader = csv.DictReader(io.StringIO(text))
        self.assertEqual(next(reader)["A"], "line one\nline two")

    def test_line_endings_are_carriage_return_newline(self):
        rows = [{"A": "1", "B": "2"}]
        text = rows_to_csv_string(rows, ("A", "B"))
        self.assertIn("\r\n", text)
        # Every line break in the plain (non-quoted) output is \r\n.
        self.assertNotIn("\r\r\n", text)

    def test_repeated_calls_produce_identical_output(self):
        rows = [{"A": "1", "B": "2"}]
        first = rows_to_csv_string(rows, ("A", "B"))
        second = rows_to_csv_string(rows, ("A", "B"))
        self.assertEqual(first, second)

    def test_input_rows_never_mutated(self):
        rows = [{"A": "1", "B": "2"}]
        original = [dict(row) for row in rows]
        rows_to_csv_string(rows, ("A", "B"))
        self.assertEqual(rows, original)


if __name__ == "__main__":
    unittest.main()
