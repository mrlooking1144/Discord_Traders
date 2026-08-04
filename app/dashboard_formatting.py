"""Pure presentation helpers for Recovery Milestone R8a's Trader
Performance dashboard.

Mirrors database/analytics.py's proven pure-module precedent: no
``sqlite3`` import, no import of ``database.repository``,
``database.service``, or ``database.analytics``, and no ``streamlit``
import. Every function here takes an already-fetched plain dict or list
of dicts - the exact shape
``TradeService.list_trader_performance_summaries()``/
``TradeService.list_current_trade_lifecycle_analytics()`` already
return - and returns a display-ready value or row dict. No function
here computes, re-derives, rounds, or ranks any analytics figure; every
Decimal-valued field is displayed exactly as the service returned it.
"""

from __future__ import annotations

_MISSING_VALUE_DISPLAY = "—"  # em dash


def format_optional_value(value: object) -> str:
    """Return str(value), or the fixed em-dash placeholder if value is
    None. Never used for a percentage field - see format_percent()."""
    if value is None:
        return _MISSING_VALUE_DISPLAY
    return str(value)


def format_percent(value: str | None) -> str:
    """Return f"{value}%" for an already-quantized R7 percentage
    string, or the fixed em-dash placeholder if value is None (a
    zero-denominator rate/average per R7's own contract - never
    rendered as "0%"). Never parses value as a number."""
    if value is None:
        return _MISSING_VALUE_DISPLAY
    return f"{value}%"


def format_data_error_count(count: int) -> str:
    """Return "0" when count == 0, else a visible "⚠ {count}"
    indicator. The summary table's only exposure of a data-error
    condition - never the verbatim analytics_error_detail text."""
    if count == 0:
        return "0"
    return f"⚠ {count}"


def build_trader_label(trader_id: int, trader_name: str) -> str:
    """Return "{trader_name} (ID {trader_id})" - used everywhere a
    trader is displayed or selected, so two identically-named traders
    are never confused."""
    return f"{trader_name} (ID {trader_id})"


def build_summary_display_row(summary: dict) -> dict:
    """Convert one list_trader_performance_summaries() dict into one
    display-ready row for the summary table, using only the formatting
    helpers above - no calculation, no re-derivation."""
    return {
        "Trader": build_trader_label(summary["trader_id"], summary["trader_name"]),
        "Total Lifecycles": summary["total_lifecycle_count"],
        "Open": summary["open_count"],
        "Partially Closed": summary["partially_closed_count"],
        "Closed": summary["closed_count"],
        "Orphan": summary["orphan_count"],
        "Unresolved": summary["unresolved_count"],
        "Invalid": summary["invalid_count"],
        "Data Errors": format_data_error_count(summary["snapshot_error_count"]),
        "Eligible": summary["eligible_lifecycle_count"],
        "Not Scored": summary["not_scored_count"],
        "Wins": summary["winning_count"],
        "Losses": summary["losing_count"],
        "Breakeven": summary["breakeven_count"],
        "Win Rate": format_percent(summary["win_rate_pct"]),
        "Loss Rate": format_percent(summary["loss_rate_pct"]),
        "Breakeven Rate": format_percent(summary["breakeven_rate_pct"]),
        "Avg Return": format_percent(summary["average_gross_price_return_pct"]),
        "Median Return": format_percent(summary["median_gross_price_return_pct"]),
        "Avg Winner Return": format_percent(summary["average_winner_price_return_pct"]),
        "Avg Loser Return": format_percent(summary["average_loser_price_return_pct"]),
    }


def build_summary_display_rows(summaries: list[dict]) -> list[dict]:
    """Map build_summary_display_row() over a list, preserving the
    caller's order exactly - R8a applies no ranking."""
    return [build_summary_display_row(summary) for summary in summaries]


def filter_lifecycle_results(
    results: list[dict],
    *,
    statuses: list[str] | None = None,
    outcomes: list[str] | None = None,
    symbol: str | None = None,
) -> list[dict]:
    """Pure, order-preserving filter over an already-fetched
    list_current_trade_lifecycle_analytics() result list.

    An empty or None statuses/outcomes list means "no filter on that
    dimension" (never "match nothing"). symbol is stripped and
    uppercased; blank means no filter; a non-blank value requires an
    exact match (never substring) against each row's own "symbol"
    field. Never mutates results or reorders survivors.
    """
    normalized_symbol = symbol.strip().upper() if symbol else ""

    filtered = []
    for result in results:
        if statuses and result["status"] not in statuses:
            continue
        if outcomes and result["outcome"] not in outcomes:
            continue
        if normalized_symbol and result["symbol"] != normalized_symbol:
            continue
        filtered.append(result)
    return filtered


def build_lifecycle_display_row(result: dict) -> dict:
    """Convert one lifecycle analytics dict (from
    list_current_trade_lifecycle_analytics()) into one display-ready
    drill-down row, including a short data-error indicator - never the
    verbatim analytics_error_detail text (see build_lifecycle_detail())."""
    ambiguity_flags = ", ".join(result["lifecycle_ambiguity_flags"])
    exclusion_reasons = ", ".join(result["analytics_exclusion_reasons"])
    return {
        "Lifecycle ID": result["trade_lifecycle_id"],
        "Symbol": result["symbol"],
        "Option Type": format_optional_value(result["option_type"]),
        "Strike": format_optional_value(result["strike"]),
        "Expiration": format_optional_value(result["expiration"]),
        "Status": result["status"],
        "Outcome": result["outcome"],
        "Direction": format_optional_value(result["direction"]),
        "Entry Price": format_optional_value(result["entry_price"]),
        "Terminal Exit Price": format_optional_value(result["terminal_exit_price"]),
        "Weighted Avg Exit Price": format_optional_value(
            result["weighted_average_exit_price"]
        ),
        "Return": format_percent(result["gross_price_return_pct"]),
        "Opened At": format_optional_value(result["opened_at"]),
        "Closed At": format_optional_value(result["closed_at"]),
        "Ambiguity Flags": ambiguity_flags or _MISSING_VALUE_DISPLAY,
        "Exclusion Reasons": exclusion_reasons or _MISSING_VALUE_DISPLAY,
        "Data Error": "⚠" if result["outcome"] == "data_error" else _MISSING_VALUE_DISPLAY,
    }


def build_lifecycle_display_rows(results: list[dict]) -> list[dict]:
    """Map build_lifecycle_display_row() over a list, preserving
    order."""
    return [build_lifecycle_display_row(result) for result in results]


def build_exit_leg_display_rows(exit_legs: list[dict]) -> list[dict]:
    """Convert one lifecycle's exit_legs list into display-ready rows:
    Event Type, Consumed Fraction, Exit Price, Sequence Index - in the
    order already provided (sequence_index ascending, as R7 guarantees).
    Returns [] unchanged if exit_legs is empty (an 'open' lifecycle with
    only an opener, for instance) - the caller decides how to render
    that empty case."""
    return [
        {
            "Event Type": leg["event_type"],
            "Consumed Fraction": leg["consumed_fraction"],
            "Exit Price": format_optional_value(leg["exit_price"]),
            "Sequence Index": leg["sequence_index"],
        }
        for leg in exit_legs
    ]


def build_lifecycle_detail(result: dict) -> dict:
    """Build the selected-lifecycle detail panel's content: the
    verbatim (never truncated, never re-sanitized) analytics_error_detail
    display value and the exit-leg display rows, bundled as
    {"error_detail": str, "exit_leg_rows": list[dict]} for the caller to
    render. This is the one place analytics_error_detail is ever shown -
    never in build_summary_display_row() or build_lifecycle_display_row()."""
    return {
        "error_detail": format_optional_value(result["analytics_error_detail"]),
        "exit_leg_rows": build_exit_leg_display_rows(result["exit_legs"]),
    }
