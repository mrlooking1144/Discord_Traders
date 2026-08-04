"""Tests for Recovery Milestone R7: the pure trader-performance analytics
engine (database/analytics.py).

Covers database/analytics.py only - no database, no repository, no
service, no UI. All fixtures are synthetic; a real-corpus acceptance pass
mirrors tests/test_lifecycle_corpus_acceptance.py's own precedent but is
out of scope for this file.
"""

import unittest
from decimal import Decimal

from database.analytics import (
    OUTCOME_BREAKEVEN,
    OUTCOME_DATA_ERROR,
    OUTCOME_LOSS,
    OUTCOME_NOT_SCORED,
    OUTCOME_WIN,
    build_data_error_result,
    compute_lifecycle_analytics,
    summarize_trader_performance,
)

_NEXT_ID = iter(range(1, 100000))


def _event(event_type, *, action=None, price="1.00", qualifier=None, trade_signal_id=None):
    """Build one decoded lifecycle-event dict with sensible defaults,
    matching the shape TradeService.list_trade_lifecycle_events() (and
    this module's own caller) already produces: id, trade_signal_id,
    sequence_index, snapshot. sequence_index is assigned by the caller
    (compute_lifecycle_analytics() never re-sorts), so this helper always
    returns events in the order given - callers pass them in the desired
    order and _sequence() (below) stamps sequence_index accordingly."""
    if trade_signal_id is None:
        trade_signal_id = next(_NEXT_ID)
    if action is None:
        action = "BTO" if event_type in ("ENTRY", "ADD", "ROLL_UP") else "STC"
    return {
        "id": trade_signal_id,
        "trade_signal_id": trade_signal_id,
        "snapshot": {
            "event_type": event_type,
            "action": action,
            "price": price,
            "qualifier": qualifier,
        },
    }


def _sequence(events):
    for index, event in enumerate(events, start=1):
        event["sequence_index"] = index
    return events


def _compute(
    events,
    *,
    status="closed",
    opened_by_signal_id=None,
    closed_by_signal_id=None,
    trade_lifecycle_id=1,
    trader_id=1,
    trader_name="TC",
    is_current=True,
    superseded_at=None,
    lifecycle_ambiguity_flags=None,
):
    events = _sequence(events)
    if opened_by_signal_id is None and events and events[0]["snapshot"]["event_type"] in (
        "ENTRY", "ROLL_UP",
    ):
        opened_by_signal_id = events[0]["trade_signal_id"]
    if closed_by_signal_id is None and events and events[-1]["snapshot"]["event_type"] == "FULL_EXIT":
        closed_by_signal_id = events[-1]["trade_signal_id"]

    return compute_lifecycle_analytics(
        trade_lifecycle_id=trade_lifecycle_id,
        trader_id=trader_id,
        trader_name=trader_name,
        is_current=is_current,
        superseded_at=superseded_at,
        status=status,
        symbol="IBM",
        option_type="call",
        strike="207.5",
        expiration="2026-07-24",
        lifecycle_ambiguity_flags=lifecycle_ambiguity_flags,
        opened_by_signal_id=opened_by_signal_id,
        closed_by_signal_id=closed_by_signal_id,
        opened_at=None,
        closed_at=None,
        events=events,
    )


class SimpleClosedLifecycleTests(unittest.TestCase):
    def test_long_win_no_partial_exits(self):
        result = _compute([
            _event("ENTRY", action="BTO", price="1.00"),
            _event("FULL_EXIT", action="STC", price="2.00", qualifier="ALL OUT"),
        ])

        self.assertEqual(result.outcome, OUTCOME_WIN)
        self.assertEqual(result.direction, "long")
        self.assertEqual(Decimal(result.gross_price_return_pct), Decimal("100.000000"))
        self.assertTrue(result.eligible_for_return_metrics)
        self.assertEqual(result.analytics_exclusion_reasons, ())

    def test_long_loss(self):
        result = _compute([
            _event("ENTRY", action="BOUGHT", price="2.00"),
            _event("FULL_EXIT", action="SOLD", price="1.00", qualifier="ALL OUT"),
        ])

        self.assertEqual(result.outcome, OUTCOME_LOSS)
        self.assertEqual(Decimal(result.gross_price_return_pct), Decimal("-50.000000"))

    def test_breakeven_uses_exact_zero_comparison(self):
        result = _compute([
            _event("ENTRY", action="BUY", price="1.00"),
            _event("FULL_EXIT", action="SELL", price="1.00", qualifier="ALL OUT"),
        ])

        self.assertEqual(result.outcome, OUTCOME_BREAKEVEN)
        self.assertEqual(Decimal(result.gross_price_return_pct), Decimal("0"))

    def test_weighted_average_exit_price_equals_terminal_when_single_exit(self):
        result = _compute([
            _event("ENTRY", action="BTO", price="1.00"),
            _event("FULL_EXIT", action="STC", price="2.00", qualifier="ALL OUT"),
        ])

        self.assertEqual(
            Decimal(result.weighted_average_exit_price), Decimal(result.terminal_exit_price)
        )

    def test_stated_return_pct_is_never_read_or_used(self):
        # compute_lifecycle_analytics() is never even given stated_return_pct
        # in this test's snapshot dicts (unlike the real 16-field snapshot
        # contract) - proving the calculation path has no dependency on it
        # at all; a computed return is still produced correctly.
        result = _compute([
            _event("ENTRY", action="BTO", price="1.00"),
            _event("FULL_EXIT", action="STC", price="2.00", qualifier="ALL OUT"),
        ])
        self.assertEqual(Decimal(result.gross_price_return_pct), Decimal("100.000000"))


class PartialExitTests(unittest.TestCase):
    def test_two_partial_exits_then_final_full_exit_is_fraction_weighted(self):
        # entry 1.00; exit half at 1.50 (contributes 1/2 * 50% = 25%);
        # exit remaining half at 2.00 (contributes 1/2 * 100% = 50%).
        # Total: 75% - never the naive "just use the final price" answer,
        # which would have wrongly reported 100%.
        result = _compute([
            _event("ENTRY", action="BTO", price="1.00"),
            _event("PARTIAL_EXIT", action="STC", price="1.50", qualifier="1/2"),
            _event("FULL_EXIT", action="STC", price="2.00", qualifier="ALL OUT"),
        ])

        self.assertEqual(result.outcome, OUTCOME_WIN)
        self.assertEqual(Decimal(result.gross_price_return_pct), Decimal("75.000000"))
        self.assertNotEqual(
            Decimal(result.gross_price_return_pct), Decimal("100.000000"),
            "must not equal the naive opener-to-terminal-price shortcut",
        )
        self.assertEqual(len(result.exit_legs), 2)
        self.assertEqual(result.exit_legs[0].consumed_fraction, "1/2")
        self.assertEqual(result.exit_legs[1].consumed_fraction, "1/2")
        self.assertEqual(
            Decimal(result.weighted_average_exit_price), Decimal("1.750000")
        )

    def test_partial_exit_missing_price_excludes_from_return_metrics(self):
        result = _compute([
            _event("ENTRY", action="BTO", price="1.00"),
            _event("PARTIAL_EXIT", action="STC", price=None, qualifier="1/2"),
            _event("FULL_EXIT", action="STC", price="2.00", qualifier="ALL OUT"),
        ])

        self.assertEqual(result.outcome, OUTCOME_NOT_SCORED)
        self.assertFalse(result.eligible_for_return_metrics)
        self.assertIn("missing_exit_price", result.analytics_exclusion_reasons)
        self.assertIsNone(result.gross_price_return_pct)


class ScaleInTests(unittest.TestCase):
    def test_add_present_excludes_from_return_metrics_but_stays_counted(self):
        result = _compute([
            _event("ENTRY", action="BTO", price="1.00"),
            _event("ADD", action="BTO", price="1.20"),
            _event("FULL_EXIT", action="STC", price="1.50", qualifier="ALL OUT"),
        ])

        self.assertEqual(result.outcome, OUTCOME_NOT_SCORED)
        self.assertFalse(result.eligible_for_return_metrics)
        self.assertIn("scale_in_cost_basis_not_modeled", result.analytics_exclusion_reasons)
        self.assertIsNone(result.gross_price_return_pct)
        self.assertTrue(result.eligible_for_status_counts)


class DirectionTests(unittest.TestCase):
    def test_bto_buy_bought_all_classify_long(self):
        for verb in ("BTO", "BUY", "BOUGHT"):
            result = _compute([
                _event("ENTRY", action=verb, price="1.00"),
                _event("FULL_EXIT", action="STC", price="2.00", qualifier="ALL OUT"),
            ])
            self.assertEqual(result.direction, "long", msg=verb)
            self.assertTrue(result.eligible_for_return_metrics, msg=verb)

    def test_sto_opener_classifies_short_and_is_not_scored(self):
        result = _compute([
            _event("ENTRY", action="STO", price="2.00"),
            _event("FULL_EXIT", action="BTC", price="1.00", qualifier="ALL OUT"),
        ])

        self.assertEqual(result.direction, "short")
        self.assertEqual(result.outcome, OUTCOME_NOT_SCORED)
        self.assertIn("short_direction_not_scored", result.analytics_exclusion_reasons)
        self.assertIsNone(result.gross_price_return_pct)


class NonClosedStatusTests(unittest.TestCase):
    def test_open_lifecycle_is_not_scored_with_no_exit_legs(self):
        result = _compute(
            [_event("ENTRY", action="BTO", price="1.00")],
            status="open",
            closed_by_signal_id=None,
        )

        self.assertEqual(result.status, "open")
        self.assertEqual(result.outcome, OUTCOME_NOT_SCORED)
        self.assertIn("status_open", result.analytics_exclusion_reasons)
        self.assertEqual(result.exit_legs, ())

    def test_orphan_with_no_verified_opener(self):
        result = _compute(
            [_event("FULL_EXIT", action="STC", price="1.00", qualifier="ALL OUT")],
            status="orphan",
            opened_by_signal_id=None,
            closed_by_signal_id=None,
        )

        self.assertIsNone(result.direction)
        self.assertEqual(result.outcome, OUTCOME_NOT_SCORED)
        self.assertIn("status_orphan", result.analytics_exclusion_reasons)
        self.assertIn("no_verified_opener", result.analytics_exclusion_reasons)

    def test_unresolved_singleton_with_no_verified_opener(self):
        result = _compute(
            [_event("ADD", action="BTO", price="1.00")],
            status="unresolved",
            opened_by_signal_id=None,
            closed_by_signal_id=None,
        )

        self.assertEqual(result.outcome, OUTCOME_NOT_SCORED)
        self.assertIn("status_unresolved", result.analytics_exclusion_reasons)

    def test_invalid_status_is_not_scored(self):
        result = _compute([
            _event("ENTRY", action="BTO", price="1.00"),
            _event("FULL_EXIT", action="STC", price="1.00", qualifier="ALL OUT"),
        ], status="invalid")

        self.assertEqual(result.outcome, OUTCOME_NOT_SCORED)
        self.assertIn("status_invalid", result.analytics_exclusion_reasons)


class EntryPriceZeroTests(unittest.TestCase):
    def test_entry_price_zero_excludes_without_raising(self):
        result = _compute([
            _event("ENTRY", action="BTO", price="0"),
            _event("FULL_EXIT", action="STC", price="1.00", qualifier="ALL OUT"),
        ])

        self.assertEqual(result.outcome, OUTCOME_NOT_SCORED)
        self.assertIn("entry_price_zero", result.analytics_exclusion_reasons)
        self.assertIsNone(result.gross_price_return_pct)


class DecimalPrecisionTests(unittest.TestCase):
    def test_repeating_fraction_rounds_deterministically_to_six_places(self):
        # A 1/3 partial exit produces a non-terminating decimal
        # intermediate; the public result must still be a clean,
        # deterministic 6-decimal-place string, never raise, never a
        # float.
        result = _compute([
            _event("ENTRY", action="BTO", price="3.00"),
            _event("PARTIAL_EXIT", action="STC", price="6.00", qualifier="1/3"),
            _event("FULL_EXIT", action="STC", price="9.00", qualifier="ALL OUT"),
        ])

        self.assertIsInstance(result.gross_price_return_pct, str)
        # Exactly 6 digits after the decimal point.
        _, _, decimals = result.gross_price_return_pct.partition(".")
        self.assertEqual(len(decimals), 6)

    def test_no_float_anywhere_in_result_fields(self):
        result = _compute([
            _event("ENTRY", action="BTO", price="1.00"),
            _event("FULL_EXIT", action="STC", price="2.00", qualifier="ALL OUT"),
        ])
        self.assertNotIsInstance(result.gross_price_return_pct, float)
        self.assertNotIsInstance(result.entry_price, float)
        self.assertNotIsInstance(result.weighted_average_exit_price, float)


class ZeroEventDataErrorResultTests(unittest.TestCase):
    def test_build_data_error_result_never_assumes_nonempty_events(self):
        result = build_data_error_result(
            trade_lifecycle_id=99,
            trader_id=1,
            trader_name="TC",
            is_current=True,
            superseded_at=None,
            status="closed",
            symbol="IBM",
            option_type="call",
            strike="207.5",
            expiration="2026-07-24",
            lifecycle_ambiguity_flags=None,
            source_event_ids=[],
            analytics_error_detail="trade_lifecycle_id 99 has no membership events.",
        )

        self.assertEqual(result.outcome, OUTCOME_DATA_ERROR)
        self.assertEqual(result.source_event_ids, ())
        self.assertIsNone(result.gross_price_return_pct)
        self.assertIsNone(result.entry_price)
        self.assertFalse(result.eligible_for_return_metrics)
        self.assertFalse(result.eligible_for_outcome_metrics)
        self.assertTrue(result.eligible_for_status_counts)
        self.assertEqual(
            result.analytics_error_detail,
            "trade_lifecycle_id 99 has no membership events.",
        )


class SummarizeTraderPerformanceTests(unittest.TestCase):
    def _make(self, outcome_status_pairs):
        """Build synthetic LifecycleAnalyticsResults directly for
        aggregation-level testing, one per (status, wants_win) pair, so
        this test class does not need to re-derive win/loss from prices -
        it exercises summarize_trader_performance()'s own reduction
        logic in isolation."""
        results = []
        next_id = 1
        for status, price_pair in outcome_status_pairs:
            entry_price, exit_price = price_pair
            events = [
                _event("ENTRY", action="BTO", price=entry_price, trade_signal_id=next_id),
                _event(
                    "FULL_EXIT", action="STC", price=exit_price, qualifier="ALL OUT",
                    trade_signal_id=next_id + 1,
                ),
            ]
            result = _compute(events, status=status, trade_lifecycle_id=next_id)
            results.append(result)
            next_id += 2
        return results

    def test_reconciliation_invariants_hold(self):
        results = self._make([
            ("closed", ("1.00", "2.00")),  # win
            ("closed", ("2.00", "1.00")),  # loss
            ("closed", ("1.00", "1.00")),  # breakeven
            ("open", ("1.00", "1.00")),
            ("partially_closed", ("1.00", "1.00")),
            ("orphan", ("1.00", "1.00")),
            ("unresolved", ("1.00", "1.00")),
            ("invalid", ("1.00", "1.00")),
        ])
        summary = summarize_trader_performance(
            trader_id=1, trader_name="TC", lifecycle_results=results
        )

        # Invariant 1: six-status partition.
        self.assertEqual(
            summary.total_lifecycle_count,
            summary.open_count + summary.partially_closed_count + summary.closed_count
            + summary.orphan_count + summary.unresolved_count + summary.invalid_count,
        )
        # Invariant 2: outcome partition.
        self.assertEqual(
            summary.total_lifecycle_count,
            summary.eligible_lifecycle_count + summary.not_scored_count
            + summary.snapshot_error_count,
        )
        self.assertEqual(summary.total_lifecycle_count, 8)
        self.assertEqual(summary.eligible_lifecycle_count, 3)
        self.assertEqual(summary.winning_count, 1)
        self.assertEqual(summary.losing_count, 1)
        self.assertEqual(summary.breakeven_count, 1)
        self.assertEqual(Decimal(summary.win_rate_pct), Decimal("33.333333"))

    def test_zero_denominator_rates_are_none_not_zero(self):
        results = self._make([("open", ("1.00", "1.00"))])
        summary = summarize_trader_performance(
            trader_id=1, trader_name="TC", lifecycle_results=results
        )

        self.assertEqual(summary.eligible_lifecycle_count, 0)
        self.assertIsNone(summary.win_rate_pct)
        self.assertIsNone(summary.loss_rate_pct)
        self.assertIsNone(summary.average_gross_price_return_pct)
        self.assertIsNone(summary.median_gross_price_return_pct)

    def test_data_error_lifecycle_counts_toward_own_status_and_snapshot_error(self):
        error_result = build_data_error_result(
            trade_lifecycle_id=42,
            trader_id=1,
            trader_name="TC",
            is_current=True,
            superseded_at=None,
            status="closed",
            symbol="IBM",
            option_type="call",
            strike="207.5",
            expiration="2026-07-24",
            lifecycle_ambiguity_flags=None,
            source_event_ids=[1, 2],
            analytics_error_detail="malformed",
        )
        clean_result = self._make([("closed", ("1.00", "2.00"))])[0]

        summary = summarize_trader_performance(
            trader_id=1, trader_name="TC", lifecycle_results=[error_result, clean_result],
        )

        self.assertEqual(summary.total_lifecycle_count, 2)
        self.assertEqual(summary.closed_count, 2, "malformed closed lifecycle still counts as closed")
        self.assertEqual(summary.snapshot_error_count, 1)
        self.assertEqual(summary.eligible_lifecycle_count, 1)
        self.assertEqual(summary.snapshot_error_lifecycle_ids, (42,))

    def test_median_even_count_uses_true_division_not_floor(self):
        # Four eligible returns: 10, 20, 30, 40 -> median = (20+30)/2 = 25,
        # never floor-divided.
        results = self._make([
            ("closed", ("1.00", "1.10")),
            ("closed", ("1.00", "1.20")),
            ("closed", ("1.00", "1.30")),
            ("closed", ("1.00", "1.40")),
        ])
        summary = summarize_trader_performance(
            trader_id=1, trader_name="TC", lifecycle_results=results
        )
        self.assertEqual(Decimal(summary.median_gross_price_return_pct), Decimal("25.000000"))

    def test_exclusion_reason_counts_aggregate_across_not_scored_lifecycles(self):
        add_result = _compute([
            _event("ENTRY", action="BTO", price="1.00"),
            _event("ADD", action="BTO", price="1.10"),
            _event("FULL_EXIT", action="STC", price="1.50", qualifier="ALL OUT"),
        ], trade_lifecycle_id=1)
        short_result = _compute([
            _event("ENTRY", action="STO", price="2.00"),
            _event("FULL_EXIT", action="BTC", price="1.00", qualifier="ALL OUT"),
        ], trade_lifecycle_id=2)

        summary = summarize_trader_performance(
            trader_id=1, trader_name="TC", lifecycle_results=[add_result, short_result],
        )

        self.assertEqual(
            summary.exclusion_reason_counts.get("scale_in_cost_basis_not_modeled"), 1
        )
        self.assertEqual(
            summary.exclusion_reason_counts.get("short_direction_not_scored"), 1
        )

    def test_id_lists_are_sorted_ascending_and_correctly_categorized(self):
        results = self._make([
            ("closed", ("1.00", "2.00")),  # id 1 -> eligible
            ("open", ("1.00", "1.00")),    # id 3 -> not_scored
        ])
        error_result = build_data_error_result(
            trade_lifecycle_id=2,
            trader_id=1,
            trader_name="TC",
            is_current=True,
            superseded_at=None,
            status="closed",
            symbol="IBM",
            option_type="call",
            strike="207.5",
            expiration="2026-07-24",
            lifecycle_ambiguity_flags=None,
            source_event_ids=[],
            analytics_error_detail="malformed",
        )
        summary = summarize_trader_performance(
            trader_id=1, trader_name="TC",
            lifecycle_results=[results[0], error_result, results[1]],
        )

        self.assertEqual(summary.all_current_lifecycle_ids, (1, 2, 3))
        self.assertEqual(summary.eligible_lifecycle_ids, (1,))
        self.assertEqual(summary.return_ineligible_lifecycle_ids, (3,))
        self.assertEqual(summary.snapshot_error_lifecycle_ids, (2,))


if __name__ == "__main__":
    unittest.main()
