"""Pure trader-performance analytics engine for Recovery Milestone R7.

This module contains no database access whatsoever - no ``sqlite3``
import, no import of ``database.repository`` or ``database.service`` -
mirroring ``database/lifecycle.py``'s proven pure-module precedent.
Every function here takes already-fetched, already-validated data (a
lifecycle's own column values, its decoded ``signal_snapshot`` events, and
any live-but-immutable timestamp already resolved by the caller) and
returns deterministic, database-independent results.

Scope boundary (approved R7 planning review, three rounds - see the R7
handoff for the full record): this module computes per-lifecycle and
per-trader *price-return* analytics only. It has no concept of dollar
P&L, contract multipliers, or position quantity - none of that evidence
exists anywhere in the committed schema. It never reads
``trade_signals.stated_return_pct`` for any calculation; that field is
audit-only. It never guesses a missing price, a missing quantity, or a
trade's direction - a lifecycle whose evidence does not unambiguously
support a computation is classified ``not_scored`` (valid but
ineligible) or ``data_error`` (structurally broken), never assigned a
fabricated value.

Trade-direction evidence (approved R7 planning review): ``app/parser.py``
classifies ``BTO``/``BUY``/``BOUGHT`` and ``STO`` identically as
*opening* actions (``STO`` opens a short - there is no separate
short-entry ``event_type``), and ``STC``/``SELL``/``SOLD``/``BTC``
identically as *closing* actions. ``event_type`` alone therefore cannot
distinguish a long position from a short one; only the opener's own raw
``action`` text can. The real 68-message corpus (Recovery Milestone R6.6)
contains no ``STO``/``BTC`` occurrences, but the grammar explicitly
supports them, so a short direction is never assumed absent - it is
detected from the opener's ``action`` and, when found, is classified
``not_scored`` (``short_direction_not_scored``), never scored under the
long-position return formula below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, DivisionByZero, InvalidOperation, ROUND_HALF_EVEN, localcontext
from fractions import Fraction
from typing import Optional

# ---------------------------------------------------------------------------
# Approved vocabulary, duplicated (not imported) across the pure-module
# boundary - the same established pattern as database/service.py
# duplicating database/repository.py's private
# _is_complete_lifecycle_key_shape()/_lifecycle_key_sort_key() rather than
# importing a module-private name across files.
# ---------------------------------------------------------------------------

_LONG_OPEN_ACTIONS = frozenset({"BTO", "BUY", "BOUGHT"})
_SHORT_OPEN_ACTIONS = frozenset({"STO"})

_OPENING_EVENT_TYPES = frozenset({"ENTRY", "ROLL_UP"})
_EXIT_EVENT_TYPES = frozenset({"PARTIAL_EXIT", "FULL_EXIT"})

_APPROVED_FRACTION_TOKENS = {
    "1/2": Fraction(1, 2),
    "1/3": Fraction(1, 3),
    "1/4": Fraction(1, 4),
    "1/6": Fraction(1, 6),
    "1/8": Fraction(1, 8),
    "1/16": Fraction(1, 16),
}

STATUS_CLOSED = "closed"

OUTCOME_WIN = "win"
OUTCOME_LOSS = "loss"
OUTCOME_BREAKEVEN = "breakeven"
OUTCOME_NOT_SCORED = "not_scored"
OUTCOME_DATA_ERROR = "data_error"

_CALC_PRECISION = 50
_OUTPUT_QUANTIZE = Decimal("0.000001")


def _quantize_pct(value: Decimal) -> Decimal:
    """Round a computed percentage to exactly 6 decimal places using
    ROUND_HALF_EVEN, for public serialization only. Never used before
    classification (win/loss/breakeven/entry_price_zero) - those always
    compare the unquantized, full-precision value."""
    with localcontext() as ctx:
        ctx.prec = _CALC_PRECISION
        ctx.rounding = ROUND_HALF_EVEN
        return value.quantize(_OUTPUT_QUANTIZE, rounding=ROUND_HALF_EVEN)


def _parse_decimal(value: object) -> Optional[Decimal]:
    """Parse a stored decimal string into a Decimal, or None if value is
    None or not a valid decimal string. Never accepts a float."""
    if value is None:
        return None
    if isinstance(value, float):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _resolve_direction(action: Optional[str]) -> Optional[str]:
    """Deterministically classify an opener's raw action verb as
    'long', 'short', or None (unrecognized/absent) - never guessed.

    See the module docstring for the committed evidence (app/parser.py)
    supporting this classification.
    """
    if action is None:
        return None
    action_upper = action.upper()
    if action_upper in _LONG_OPEN_ACTIONS:
        return "long"
    if action_upper in _SHORT_OPEN_ACTIONS:
        return "short"
    return None


@dataclass(frozen=True)
class ExitLeg:
    """One exit event's contribution to a lifecycle's exit history.

    Attributes:
        trade_lifecycle_event_id: The membership row this leg was built
            from.
        trade_signal_id: The signal that produced this exit.
        sequence_index: This event's 1-based position within its
            generation.
        event_type: 'PARTIAL_EXIT' or 'FULL_EXIT'.
        consumed_fraction: The exact fractions.Fraction string this exit
            consumed of the *original* position (never Decimal - several
            approved tokens, e.g. 1/3 and 1/6, are non-terminating in
            base 10).
        exit_price: The exact stored decimal string this exit's own
            signal snapshot carries, or None if unstated.
    """

    trade_lifecycle_event_id: int
    trade_signal_id: int
    sequence_index: int
    event_type: str
    consumed_fraction: str
    exit_price: Optional[str]


@dataclass(frozen=True)
class LifecycleAnalyticsResult:
    """One lifecycle generation's complete R7 analytics result.

    Every Decimal-valued field is a plain string (never a Python float),
    matching this project's existing price/strike serialization
    convention. See the R7 handoff for the full field-by-field
    justification.
    """

    trade_lifecycle_id: int
    trader_id: int
    trader_name: str
    is_current: bool
    superseded_at: Optional[str]
    status: str
    outcome: str
    direction: Optional[str]
    symbol: str
    option_type: Optional[str]
    strike: Optional[str]
    expiration: Optional[str]
    opened_at: Optional[str]
    closed_at: Optional[str]
    entry_price: Optional[str]
    terminal_exit_price: Optional[str]
    weighted_average_exit_price: Optional[str]
    exit_legs: tuple[ExitLeg, ...]
    gross_price_return_pct: Optional[str]
    eligible_for_status_counts: bool
    eligible_for_outcome_metrics: bool
    eligible_for_return_metrics: bool
    lifecycle_ambiguity_flags: tuple[str, ...]
    analytics_exclusion_reasons: tuple[str, ...]
    analytics_error_detail: Optional[str]
    source_event_ids: tuple[int, ...]


def build_data_error_result(
    *,
    trade_lifecycle_id: int,
    trader_id: int,
    trader_name: str,
    is_current: bool,
    superseded_at: Optional[str],
    status: str,
    symbol: str,
    option_type: Optional[str],
    strike: Optional[str],
    expiration: Optional[str],
    lifecycle_ambiguity_flags: Optional[list],
    source_event_ids: list,
    analytics_error_detail: str,
) -> LifecycleAnalyticsResult:
    """Build a 'data_error' result for a lifecycle whose evidence is
    structurally broken (zero membership events, or a signal_snapshot
    that failed validation/decoding) - used only by the tolerant service
    read paths. No return or outcome metric is ever computed or
    fabricated for a data_error result; source_event_ids reflects
    whatever event ids were actually found (possibly empty), never
    assumed non-empty.
    """
    return LifecycleAnalyticsResult(
        trade_lifecycle_id=trade_lifecycle_id,
        trader_id=trader_id,
        trader_name=trader_name,
        is_current=is_current,
        superseded_at=superseded_at,
        status=status,
        outcome=OUTCOME_DATA_ERROR,
        direction=None,
        symbol=symbol,
        option_type=option_type,
        strike=strike,
        expiration=expiration,
        opened_at=None,
        closed_at=None,
        entry_price=None,
        terminal_exit_price=None,
        weighted_average_exit_price=None,
        exit_legs=(),
        gross_price_return_pct=None,
        eligible_for_status_counts=True,
        eligible_for_outcome_metrics=False,
        eligible_for_return_metrics=False,
        lifecycle_ambiguity_flags=tuple(lifecycle_ambiguity_flags or ()),
        analytics_exclusion_reasons=(),
        analytics_error_detail=analytics_error_detail,
        source_event_ids=tuple(source_event_ids),
    )


def _build_exit_legs(events: list[dict]) -> tuple[tuple[ExitLeg, ...], bool, bool]:
    """Replay one generation's ordered, already-validated member events
    and reconstruct its exit history.

    Args:
        events: Decoded lifecycle-event dicts (as returned by
            TradeService.list_trade_lifecycle_events()/an equivalent
            already-validated read), in sequence_index ascending order -
            not re-sorted here.

    Returns:
        (exit_legs, has_add, has_unresolvable_fraction). exit_legs is
        empty if the generation has no PARTIAL_EXIT/FULL_EXIT member (an
        'open' generation with only an opener, for instance).
        has_unresolvable_fraction is always False for any lifecycle that
        actually reached a normal 'closed'/'partially_closed'/'orphan'
        status - the pure matching engine (database/lifecycle.py) already
        routes an unresolvable exit fraction to its own standalone
        'unresolved' singleton before it can ever become a member here -
        this flag exists only as a defensive, never-guess safeguard
        against that precondition ever being violated.
    """
    legs: list[ExitLeg] = []
    remaining = Fraction(1)
    has_add = False
    has_unresolvable_fraction = False

    for event in events:
        snapshot = event["snapshot"]
        event_type = snapshot.get("event_type")

        if event_type in _OPENING_EVENT_TYPES:
            remaining = Fraction(1)
            continue

        if event_type == "ADD":
            has_add = True
            continue

        if event_type not in _EXIT_EVENT_TYPES:
            # Unrecognized event_type on a member of a real generation
            # should not occur (the engine routes it to its own
            # unresolved singleton before this point) - never guessed.
            has_unresolvable_fraction = True
            continue

        if event_type == "FULL_EXIT":
            consumed = remaining
        else:
            token = snapshot.get("qualifier")
            fraction = _APPROVED_FRACTION_TOKENS.get(token)
            if fraction is None:
                has_unresolvable_fraction = True
                continue
            consumed = fraction

        legs.append(
            ExitLeg(
                trade_lifecycle_event_id=event["id"],
                trade_signal_id=event["trade_signal_id"],
                sequence_index=event["sequence_index"],
                event_type=event_type,
                consumed_fraction=str(consumed),
                exit_price=snapshot.get("price"),
            )
        )
        remaining -= consumed

    return tuple(legs), has_add, has_unresolvable_fraction


def compute_lifecycle_analytics(
    *,
    trade_lifecycle_id: int,
    trader_id: int,
    trader_name: str,
    is_current: bool,
    superseded_at: Optional[str],
    status: str,
    symbol: str,
    option_type: Optional[str],
    strike: Optional[str],
    expiration: Optional[str],
    lifecycle_ambiguity_flags: Optional[list],
    opened_by_signal_id: Optional[int],
    closed_by_signal_id: Optional[int],
    opened_at: Optional[str],
    closed_at: Optional[str],
    events: list[dict],
) -> LifecycleAnalyticsResult:
    """Compute one current-or-historical lifecycle generation's complete
    R7 analytics result from already-fetched, already-validated evidence.

    Args:
        events: Every decoded lifecycle-event dict for this generation
            (as TradeService.list_trade_lifecycle_events() already
            produces), in sequence_index ascending order. Must be
            non-empty and already free of LifecycleSnapshotError - the
            caller (TradeService) is responsible for fetching and
            validating these before calling this function; a zero-event
            or malformed-snapshot generation is never passed here - see
            build_data_error_result() for that case instead.
        opened_at, closed_at: Already-resolved canonical UTC timestamps
            (or None), sourced by the caller from raw_messages.received_at
            for opened_by_signal_id/closed_by_signal_id - this module
            never resolves a timestamp itself.

    Returns:
        The complete LifecycleAnalyticsResult. Never raises - every input
        shape this function can receive (given the precondition that
        events is non-empty and pre-validated) has a defined,
        deterministic classification.
    """
    source_event_ids = tuple(event["id"] for event in events)

    opener_event = next(
        (e for e in events if e["trade_signal_id"] == opened_by_signal_id),
        None,
    ) if opened_by_signal_id is not None else None
    closer_event = next(
        (e for e in events if e["trade_signal_id"] == closed_by_signal_id),
        None,
    ) if closed_by_signal_id is not None else None

    opener_action = opener_event["snapshot"].get("action") if opener_event else None
    direction = _resolve_direction(opener_action)

    entry_price = opener_event["snapshot"].get("price") if opener_event else None
    terminal_exit_price = closer_event["snapshot"].get("price") if closer_event else None

    exit_legs, has_add, has_unresolvable_fraction = _build_exit_legs(events)

    reasons: list[str] = []
    if status != STATUS_CLOSED:
        reasons.append(f"status_{status}")
    if opener_event is None:
        reasons.append("no_verified_opener")
    if direction == "short":
        reasons.append("short_direction_not_scored")
    elif direction is None and opener_event is not None:
        reasons.append("direction_undetermined")
    if has_add:
        reasons.append("scale_in_cost_basis_not_modeled")
    if has_unresolvable_fraction:
        reasons.append("unresolvable_exit_fraction")

    entry_decimal = _parse_decimal(entry_price)
    if entry_price is not None and entry_decimal is None:
        reasons.append("unparseable_entry_price")
    elif entry_price is None:
        reasons.append("missing_entry_price")

    if not exit_legs and status == STATUS_CLOSED:
        reasons.append("missing_exit_price")
    missing_exit_price = any(leg.exit_price is None for leg in exit_legs)
    if missing_exit_price:
        reasons.append("missing_exit_price")

    entry_price_zero = entry_decimal is not None and entry_decimal == Decimal("0")
    if entry_price_zero:
        reasons.append("entry_price_zero")

    eligible_for_return_metrics = (
        status == STATUS_CLOSED
        and direction == "long"
        and not has_add
        and not has_unresolvable_fraction
        and entry_decimal is not None
        and not entry_price_zero
        and bool(exit_legs)
        and not missing_exit_price
    )
    eligible_for_outcome_metrics = eligible_for_return_metrics

    gross_price_return_pct: Optional[str] = None
    weighted_average_exit_price: Optional[str] = None
    outcome = OUTCOME_NOT_SCORED

    if eligible_for_return_metrics:
        with localcontext() as ctx:
            ctx.prec = _CALC_PRECISION
            ctx.rounding = ROUND_HALF_EVEN

            weighted_sum_price = Decimal("0")
            weighted_sum_fraction = Decimal("0")
            return_accum = Decimal("0")
            for leg in exit_legs:
                leg_fraction = Fraction(leg.consumed_fraction)
                leg_fraction_decimal = Decimal(leg_fraction.numerator) / Decimal(
                    leg_fraction.denominator
                )
                leg_price = Decimal(leg.exit_price)
                weighted_sum_price += leg_price * leg_fraction_decimal
                weighted_sum_fraction += leg_fraction_decimal
                return_accum += leg_fraction_decimal * (
                    (leg_price - entry_decimal) / entry_decimal
                )

            try:
                weighted_avg = weighted_sum_price / weighted_sum_fraction
            except DivisionByZero:
                weighted_avg = None

            gross_return_pct_value = return_accum * Decimal(100)

            if weighted_avg is not None:
                weighted_average_exit_price = str(_quantize_pct(weighted_avg))
            gross_price_return_pct = str(_quantize_pct(gross_return_pct_value))

            if gross_return_pct_value > 0:
                outcome = OUTCOME_WIN
            elif gross_return_pct_value < 0:
                outcome = OUTCOME_LOSS
            else:
                outcome = OUTCOME_BREAKEVEN

    return LifecycleAnalyticsResult(
        trade_lifecycle_id=trade_lifecycle_id,
        trader_id=trader_id,
        trader_name=trader_name,
        is_current=is_current,
        superseded_at=superseded_at,
        status=status,
        outcome=outcome,
        direction=direction,
        symbol=symbol,
        option_type=option_type,
        strike=strike,
        expiration=expiration,
        opened_at=opened_at,
        closed_at=closed_at,
        entry_price=entry_price,
        terminal_exit_price=terminal_exit_price,
        weighted_average_exit_price=weighted_average_exit_price,
        exit_legs=exit_legs,
        gross_price_return_pct=gross_price_return_pct,
        eligible_for_status_counts=True,
        eligible_for_outcome_metrics=eligible_for_outcome_metrics,
        eligible_for_return_metrics=eligible_for_return_metrics,
        lifecycle_ambiguity_flags=tuple(lifecycle_ambiguity_flags or ()),
        analytics_exclusion_reasons=tuple(reasons),
        analytics_error_detail=None,
        source_event_ids=source_event_ids,
    )


_ALL_STATUSES = ("open", "partially_closed", "closed", "orphan", "unresolved", "invalid")


@dataclass(frozen=True)
class TraderPerformanceSummary:
    """One trader's complete R7 performance summary, reduced entirely
    from already-computed LifecycleAnalyticsResults for that trader's
    current lifecycles - never independently re-reads or re-validates
    any snapshot itself.
    """

    trader_id: int
    trader_name: str
    total_lifecycle_count: int
    open_count: int
    partially_closed_count: int
    closed_count: int
    orphan_count: int
    unresolved_count: int
    invalid_count: int
    snapshot_error_count: int
    eligible_lifecycle_count: int
    not_scored_count: int
    winning_count: int
    losing_count: int
    breakeven_count: int
    win_rate_pct: Optional[str]
    loss_rate_pct: Optional[str]
    breakeven_rate_pct: Optional[str]
    average_gross_price_return_pct: Optional[str]
    median_gross_price_return_pct: Optional[str]
    average_winner_price_return_pct: Optional[str]
    average_loser_price_return_pct: Optional[str]
    all_current_lifecycle_ids: tuple[int, ...]
    eligible_lifecycle_ids: tuple[int, ...]
    return_ineligible_lifecycle_ids: tuple[int, ...]
    snapshot_error_lifecycle_ids: tuple[int, ...]
    exclusion_reason_counts: dict


def _decimal_average(values: list[Decimal]) -> Optional[Decimal]:
    if not values:
        return None
    with localcontext() as ctx:
        ctx.prec = _CALC_PRECISION
        ctx.rounding = ROUND_HALF_EVEN
        return sum(values, Decimal("0")) / Decimal(len(values))


def _decimal_median(values: list[Decimal]) -> Optional[Decimal]:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    with localcontext() as ctx:
        ctx.prec = _CALC_PRECISION
        ctx.rounding = ROUND_HALF_EVEN
        if n % 2 == 1:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / Decimal(2)


def summarize_trader_performance(
    *,
    trader_id: int,
    trader_name: str,
    lifecycle_results: list[LifecycleAnalyticsResult],
) -> TraderPerformanceSummary:
    """Reduce one trader's current-lifecycle analytics results into a
    single performance summary.

    Every count/rate/average here is derived solely from
    lifecycle_results - no snapshot is read or decoded again. Rates and
    averages are None (never 0) when their denominator is zero.
    """
    status_counts = {status: 0 for status in _ALL_STATUSES}
    snapshot_error_count = 0
    eligible_ids: list[int] = []
    not_scored_ids: list[int] = []
    data_error_ids: list[int] = []
    winners: list[Decimal] = []
    losers: list[Decimal] = []
    eligible_returns: list[Decimal] = []
    exclusion_reason_counts: dict = {}

    for result in lifecycle_results:
        if result.status in status_counts:
            status_counts[result.status] += 1

        if result.outcome == OUTCOME_DATA_ERROR:
            snapshot_error_count += 1
            data_error_ids.append(result.trade_lifecycle_id)
            continue

        if result.outcome == OUTCOME_NOT_SCORED:
            not_scored_ids.append(result.trade_lifecycle_id)
            for reason in result.analytics_exclusion_reasons:
                exclusion_reason_counts[reason] = exclusion_reason_counts.get(reason, 0) + 1
            continue

        # win / loss / breakeven
        eligible_ids.append(result.trade_lifecycle_id)
        with localcontext() as ctx:
            ctx.prec = _CALC_PRECISION
            ctx.rounding = ROUND_HALF_EVEN
            return_value = Decimal(result.gross_price_return_pct)
        eligible_returns.append(return_value)
        if result.outcome == OUTCOME_WIN:
            winners.append(return_value)
        elif result.outcome == OUTCOME_LOSS:
            losers.append(return_value)

    eligible_lifecycle_count = len(eligible_ids)
    winning_count = len(winners)
    losing_count = len(losers)
    breakeven_count = eligible_lifecycle_count - winning_count - losing_count

    def _rate(count: int) -> Optional[str]:
        if eligible_lifecycle_count == 0:
            return None
        with localcontext() as ctx:
            ctx.prec = _CALC_PRECISION
            ctx.rounding = ROUND_HALF_EVEN
            value = Decimal(count) / Decimal(eligible_lifecycle_count) * Decimal(100)
        return str(_quantize_pct(value))

    def _avg(values: list[Decimal]) -> Optional[str]:
        avg = _decimal_average(values)
        return None if avg is None else str(_quantize_pct(avg))

    def _med(values: list[Decimal]) -> Optional[str]:
        med = _decimal_median(values)
        return None if med is None else str(_quantize_pct(med))

    return TraderPerformanceSummary(
        trader_id=trader_id,
        trader_name=trader_name,
        total_lifecycle_count=len(lifecycle_results),
        open_count=status_counts["open"],
        partially_closed_count=status_counts["partially_closed"],
        closed_count=status_counts["closed"],
        orphan_count=status_counts["orphan"],
        unresolved_count=status_counts["unresolved"],
        invalid_count=status_counts["invalid"],
        snapshot_error_count=snapshot_error_count,
        eligible_lifecycle_count=eligible_lifecycle_count,
        not_scored_count=len(not_scored_ids),
        winning_count=winning_count,
        losing_count=losing_count,
        breakeven_count=breakeven_count,
        win_rate_pct=_rate(winning_count),
        loss_rate_pct=_rate(losing_count),
        breakeven_rate_pct=_rate(breakeven_count),
        average_gross_price_return_pct=_avg(eligible_returns),
        median_gross_price_return_pct=_med(eligible_returns),
        average_winner_price_return_pct=_avg(winners),
        average_loser_price_return_pct=_avg(losers),
        all_current_lifecycle_ids=tuple(
            sorted(r.trade_lifecycle_id for r in lifecycle_results)
        ),
        eligible_lifecycle_ids=tuple(sorted(eligible_ids)),
        return_ineligible_lifecycle_ids=tuple(sorted(not_scored_ids)),
        snapshot_error_lifecycle_ids=tuple(sorted(data_error_ids)),
        exclusion_reason_counts=exclusion_reason_counts,
    )
