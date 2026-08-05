"""Pure presentation helpers for the Trader Performance dashboard
(Recovery Milestone R8a's core dashboard, extended by Recovery
Milestone R8b's ranking/minimum-sample/CSV export additions).

Mirrors database/analytics.py's proven pure-module precedent: no
``sqlite3`` import, no import of ``database.repository``,
``database.service``, or ``database.analytics``, and no ``streamlit``
import. Every function here takes an already-fetched plain dict or list
of dicts - the exact shape
``TradeService.list_trader_performance_summaries()``/
``TradeService.list_current_trade_lifecycle_analytics()`` already
return - and returns a display-ready value or row dict. No function
here computes, re-derives, or rounds any analytics figure; every
Decimal-valued field is displayed exactly as the service returned it.

Recovery Milestone R8b adds trader ranking (``rank_trader_summaries()``),
a minimum-eligible-lifecycle threshold indicator
(``meets_minimum_sample()``/``format_minimum_sample()``), and
deterministic CSV export (``build_summary_csv_rows()``,
``build_lifecycle_csv_rows()``, ``rows_to_csv_string()``). Ranking
compares the raw service dict's own Decimal/int/str values directly -
never a float, never a re-derivation of an R7 analytics value - and CSV
serialization uses only the Python standard library (``csv`` +
``io.StringIO``), never pandas. This module still performs no ranking
"by way of" any dataframe/UI widget's own sort behavior - every ordering
a caller sees (on screen or in an export) is produced here, in plain
Python, before any Streamlit rendering happens.
"""

from __future__ import annotations

import csv
import functools
import io
from decimal import Decimal

_MISSING_VALUE_DISPLAY = "—"  # em dash

# Recovery Milestone R8b: approved sortable-metric vocabulary, in the
# exact order the "Rank traders by" control presents them. Each metric
# maps to (raw TraderPerformanceSummary field name, comparison type) -
# "decimal" for a percentage/return field (compared as Decimal, never
# float), "int" for a lifecycle-count/trader_id field, "str_ci" for a
# case-insensitive trader-name comparison.
_SORT_METRIC_FIELDS: dict[str, tuple[str, str]] = {
    "Average Return": ("average_gross_price_return_pct", "decimal"),
    "Median Return": ("median_gross_price_return_pct", "decimal"),
    "Win Rate": ("win_rate_pct", "decimal"),
    "Loss Rate": ("loss_rate_pct", "decimal"),
    "Avg Winner Return": ("average_winner_price_return_pct", "decimal"),
    "Avg Loser Return": ("average_loser_price_return_pct", "decimal"),
    "Eligible Lifecycles": ("eligible_lifecycle_count", "int"),
    "Total Lifecycles": ("total_lifecycle_count", "int"),
    "Trader Name": ("trader_name", "str_ci"),
    "Trader ID": ("trader_id", "int"),
}

# Public, ordered choice tuples for the "Rank traders by"/"Sort
# direction" controls - a plain tuple (not the dict above) so the UI
# layer never needs to know about the internal field/comparison-type
# mapping.
SORT_METRIC_CHOICES: tuple[str, ...] = tuple(_SORT_METRIC_FIELDS.keys())
SORT_DIRECTION_CHOICES: tuple[str, ...] = ("Descending", "Ascending")

# Fixed, documented CSV column orders (Recovery Milestone R8b). The
# summary CSV splits the on-screen combined "Trader" label back into
# separate "Trader Name"/"Trader ID" columns for spreadsheet
# usability - the one deliberate divergence from the on-screen table,
# which keeps the combined disambiguated label everywhere else.
SUMMARY_CSV_FIELDNAMES: tuple[str, ...] = (
    "Trader Name", "Trader ID", "Meets Minimum Sample", "Total Lifecycles",
    "Open", "Partially Closed", "Closed", "Orphan", "Unresolved", "Invalid",
    "Data Errors", "Eligible", "Not Scored", "Wins", "Losses", "Breakeven",
    "Win Rate", "Loss Rate", "Breakeven Rate", "Avg Return", "Median Return",
    "Avg Winner Return", "Avg Loser Return",
)

LIFECYCLE_CSV_FIELDNAMES: tuple[str, ...] = (
    "Lifecycle ID", "Symbol", "Option Type", "Strike", "Expiration",
    "Status", "Outcome", "Direction", "Entry Price", "Terminal Exit Price",
    "Weighted Avg Exit Price", "Return", "Opened At", "Closed At",
    "Ambiguity Flags", "Exclusion Reasons", "Data Error",
)


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


def meets_minimum_sample(summary: dict, min_eligible_lifecycles: int) -> bool:
    """Return True if summary's own eligible_lifecycle_count (never
    recalculated here - read verbatim from the R7 service result) meets
    or exceeds min_eligible_lifecycles, the current user-selected
    threshold (Recovery Milestone R8b)."""
    return summary["eligible_lifecycle_count"] >= min_eligible_lifecycles


def format_minimum_sample(summary: dict, min_eligible_lifecycles: int) -> str:
    """Return "Yes" if the trader meets the minimum eligible-lifecycle
    threshold, else a clear "No (n < threshold)" indicator that always
    shows the real counts - never a bare "No" that hides them."""
    if meets_minimum_sample(summary, min_eligible_lifecycles):
        return "Yes"
    return f"No ({summary['eligible_lifecycle_count']} < {min_eligible_lifecycles})"


def build_summary_display_row(summary: dict, *, min_eligible_lifecycles: int) -> dict:
    """Convert one list_trader_performance_summaries() dict into one
    display-ready row for the summary table, using only the formatting
    helpers above - no calculation, no re-derivation.
    min_eligible_lifecycles is the current user-selected threshold
    (Recovery Milestone R8b), used only to build the "Meets Minimum
    Sample" column - eligible_lifecycle_count itself is never
    recalculated."""
    return {
        "Trader": build_trader_label(summary["trader_id"], summary["trader_name"]),
        "Meets Minimum Sample": format_minimum_sample(summary, min_eligible_lifecycles),
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


def build_summary_display_rows(
    summaries: list[dict], *, min_eligible_lifecycles: int
) -> list[dict]:
    """Map build_summary_display_row() over a list, preserving the
    caller's order exactly - this module never reorders; ranking
    (Recovery Milestone R8b's rank_trader_summaries()) is always applied
    by the caller before this function is called."""
    return [
        build_summary_display_row(summary, min_eligible_lifecycles=min_eligible_lifecycles)
        for summary in summaries
    ]


def _sort_key_value(summary: dict, field: str, value_type: str):
    """Return summary[field] coerced to a directly comparable value for
    ranking: Decimal for a percentage/return field (never float), int
    for a count/id field, or a lowercased str for a case-insensitive
    name comparison. Returns None unchanged (R7's own zero-denominator
    contract) so the caller can apply the always-sorts-last rule."""
    raw = summary[field]
    if raw is None:
        return None
    if value_type == "decimal":
        return Decimal(raw)
    if value_type == "int":
        return int(raw)
    if value_type == "str_ci":
        return str(raw).lower()
    raise ValueError(f"Unknown sort value type: {value_type!r}")  # pragma: no cover


def _compare_ranked_summaries(
    left: dict,
    right: dict,
    *,
    field: str,
    value_type: str,
    descending: bool,
    min_eligible_lifecycles: int,
) -> int:
    """Three-way comparator used by rank_trader_summaries(): a trader
    meeting min_eligible_lifecycles always sorts before one that does
    not; within each of those two tiers, the selected metric sorts in
    the chosen direction with a None value always last regardless of
    direction; any remaining tie (including a tie between two None
    values) breaks by trader_id ascending, unconditionally of
    descending."""
    left_qualifies = meets_minimum_sample(left, min_eligible_lifecycles)
    right_qualifies = meets_minimum_sample(right, min_eligible_lifecycles)
    if left_qualifies != right_qualifies:
        return -1 if left_qualifies else 1

    left_value = _sort_key_value(left, field, value_type)
    right_value = _sort_key_value(right, field, value_type)

    if (left_value is None) != (right_value is None):
        return -1 if right_value is None else 1

    if left_value is not None and left_value != right_value:
        if left_value < right_value:
            return 1 if descending else -1
        return -1 if descending else 1

    left_id = left["trader_id"]
    right_id = right["trader_id"]
    if left_id == right_id:
        return 0
    return -1 if left_id < right_id else 1


def rank_trader_summaries(
    summaries: list[dict],
    *,
    sort_metric: str,
    descending: bool,
    min_eligible_lifecycles: int,
) -> list[dict]:
    """Rank list_trader_performance_summaries() dicts for display/export
    (Recovery Milestone R8b).

    Sorts on the raw service dict (never the display-formatted row)
    using Decimal for percentage fields and int for count/id fields -
    never a float, and never a string/lexicographic comparison of a
    numeric field; trader_name compares case-insensitively. A None
    metric value (R7's own zero-denominator contract) always sorts
    last, in either direction. A trader below min_eligible_lifecycles
    (per meets_minimum_sample()) always sorts after every trader that
    meets it, then by the same metric/direction/tie-break rules within
    that tier. The final tie-break is always trader_id ascending,
    unconditionally of descending. Never mutates summaries; always
    returns a new list.

    Raises ValueError if sort_metric is not one of SORT_METRIC_CHOICES.
    """
    if sort_metric not in _SORT_METRIC_FIELDS:
        raise ValueError(f"Unknown sort metric: {sort_metric!r}")
    field, value_type = _SORT_METRIC_FIELDS[sort_metric]
    comparator = functools.partial(
        _compare_ranked_summaries,
        field=field,
        value_type=value_type,
        descending=descending,
        min_eligible_lifecycles=min_eligible_lifecycles,
    )
    return sorted(summaries, key=functools.cmp_to_key(comparator))


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


def build_summary_csv_row(summary: dict, *, min_eligible_lifecycles: int) -> dict:
    """Convert one ranked summary dict into one CSV-ready row matching
    SUMMARY_CSV_FIELDNAMES exactly (Recovery Milestone R8b): the
    combined on-screen "Trader" label is split back into separate
    "Trader Name"/"Trader ID" columns for spreadsheet usability; every
    other value is identical to build_summary_display_row()'s own
    formatting - the export is always what is on screen, never
    recalculated here."""
    display_row = build_summary_display_row(
        summary, min_eligible_lifecycles=min_eligible_lifecycles
    )
    csv_row = dict(display_row)
    del csv_row["Trader"]
    csv_row["Trader Name"] = summary["trader_name"]
    csv_row["Trader ID"] = summary["trader_id"]
    return csv_row


def build_summary_csv_rows(
    summaries: list[dict], *, min_eligible_lifecycles: int
) -> list[dict]:
    """Map build_summary_csv_row() over a list, preserving the caller's
    (already-ranked) order exactly."""
    return [
        build_summary_csv_row(summary, min_eligible_lifecycles=min_eligible_lifecycles)
        for summary in summaries
    ]


def build_lifecycle_csv_rows(results: list[dict]) -> list[dict]:
    """Convert filtered lifecycle analytics dicts into CSV-ready rows
    (Recovery Milestone R8b) - identical to
    build_lifecycle_display_rows()'s own drill-down table rows, so the
    export is always exactly what is on screen, in the same order.
    Never includes analytics_error_detail - that field is never part of
    the approved lifecycle display-row contract (see
    build_lifecycle_display_row())."""
    return build_lifecycle_display_rows(results)


def rows_to_csv_string(rows: list[dict], fieldnames: tuple[str, ...]) -> str:
    """Serialize display/CSV-ready rows into one deterministic CSV
    string (Recovery Milestone R8b), using only the Python standard
    library (csv + io.StringIO) - never pandas. Always writes the header
    row, even for an empty rows list. Uses the csv module's default
    dialect (comma-delimited, "\\r\\n" line endings, standard quoting) -
    never hand-rolled string joining. Repeated calls with the same input
    always produce byte-identical output."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(fieldnames))
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()
