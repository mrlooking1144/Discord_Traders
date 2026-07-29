"""Pure lifecycle-matching engine for Recovery Milestone R6.

This module contains no database access whatsoever - no ``sqlite3``
import, no import of ``database.repository`` or ``database.service`` - and
performs only the deterministic state-machine calculation approved during
R6 planning review: given one lifecycle key's already-discovered,
already-ordered current signal snapshots, replay them and produce the
resulting sequence of lifecycle generations.

Scope boundary (approved R6 design, "Clarify pure-function boundaries"):
repository/service code (Recovery Milestones R6.3/R6.4, not yet
implemented as of this milestone) is responsible for -

- discovering which ``trade_signals`` rows are current (the same
  ``extraction_id IS NULL OR message_extractions.is_current = 1`` rule
  ``get_trade_signals_for_review()`` already applies) and eligible
  (``event_type IS NOT NULL``) for lifecycle linking at all - a signal
  with an incomplete key (some but not all of option_type/strike/
  expiration populated) or a missing ``event_type`` is filtered out and
  routed to its own unresolved singleton *before* it ever reaches this
  module, flagged ``lifecycle_key_incomplete``/``lifecycle_event_type_missing``
  by that layer, not this one;
- filtering the eligible signals to one lifecycle key;
- resolving their chronological order, including the fallback to
  insertion order (flagged ``lifecycle_order_unresolved_timestamp`` by
  that layer) when any signal in the window has no resolved timestamp;
- scoping *which* signals belong in a given rebuild window at all (a
  terminal generation's fixed lineage, the current non-terminal tail, or
  a brand-new key) - this module has no concept of "generations already
  persisted," "lineage," or "terminal anchors" whatsoever; it only ever
  sees one already-assembled window and replays it;
- persisting whatever this module computes, including the defensive,
  cross-generation ``ambiguous_active_lifecycle_for_key`` check (which
  cannot arise *within* one single replay of this module - it only
  arises, if ever, from an inconsistency across multiple already-persisted
  ``trade_lifecycles`` rows for the same key, which this module never
  sees at all).

This module assumes, as a precondition it does not re-validate, that
every ``SignalSnapshot`` passed to ``build_lifecycle_sequence()`` belongs
to the same lifecycle key and is already in the exact order it should be
replayed in.

Recovery Milestone R6.2 adds this module and its tests only. No
repository, service, or UI code reads or calls anything here yet -
nothing in this codebase constructs a ``trade_lifecycles``/
``trade_lifecycle_events`` row as of R6.2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Optional

# ---------------------------------------------------------------------------
# Approved vocabulary (Recovery Milestones R3/R5). Restated here rather
# than imported from app.parser, so this module has zero dependencies
# beyond the standard library - the strictest possible reading of "no
# database access."
# ---------------------------------------------------------------------------

EVENT_TYPE_ENTRY = "ENTRY"
EVENT_TYPE_ADD = "ADD"
EVENT_TYPE_ROLL_UP = "ROLL_UP"
EVENT_TYPE_PARTIAL_EXIT = "PARTIAL_EXIT"
EVENT_TYPE_FULL_EXIT = "FULL_EXIT"

_OPENING_EVENT_TYPES = frozenset({EVENT_TYPE_ENTRY, EVENT_TYPE_ROLL_UP})
_EXIT_EVENT_TYPES = frozenset({EVENT_TYPE_PARTIAL_EXIT, EVENT_TYPE_FULL_EXIT})
_APPROVED_EVENT_TYPES = frozenset(
    {
        EVENT_TYPE_ENTRY,
        EVENT_TYPE_ADD,
        EVENT_TYPE_ROLL_UP,
        EVENT_TYPE_PARTIAL_EXIT,
        EVENT_TYPE_FULL_EXIT,
    }
)

STATUS_OPEN = "open"
STATUS_PARTIALLY_CLOSED = "partially_closed"
STATUS_CLOSED = "closed"
STATUS_ORPHAN = "orphan"
STATUS_UNRESOLVED = "unresolved"
STATUS_INVALID = "invalid"

# The exact six fraction tokens app/parser.py's _EXTRACTOR_FRACTION_RE
# (r"^(1/2|1/3|1/4|1/6|1/8|1/16)$") can ever produce - matched by exact
# string equality, never a general "parse any N/M text" fraction parser.
# This is deliberate: e.g. "1/5" is a real, well-formed fraction but is
# explicitly *not* an approved token (see tests.test_extractor's own
# rejection-of-1/5 coverage) - a general parser would silently accept it,
# which is exactly the guessing this module must never do.
_APPROVED_FRACTION_TOKENS = {
    "1/2": Fraction(1, 2),
    "1/3": Fraction(1, 3),
    "1/4": Fraction(1, 4),
    "1/6": Fraction(1, 6),
    "1/8": Fraction(1, 8),
    "1/16": Fraction(1, 16),
}

FLAG_ENTRY_AGAINST_OPEN_POSITION_SAME_KEY = "entry_against_open_position_same_key"
FLAG_ORPHAN_INTERRUPTED_BY_NEW_ENTRY = "orphan_interrupted_by_new_entry"
FLAG_AMBIGUOUS_ADD_NO_OPEN_POSITION = "ambiguous_add_no_open_position"
FLAG_SCALE_IN_COST_BASIS_NOT_MODELED = "scale_in_cost_basis_not_modeled"
FLAG_PARTIAL_EXIT_FRACTION_MISSING = "partial_exit_fraction_missing"
FLAG_PARTIAL_EXIT_FRACTION_UNRECOGNIZED = "partial_exit_fraction_unrecognized"
FLAG_PARTIAL_EXIT_FRACTION_NON_POSITIVE = "partial_exit_fraction_non_positive"
FLAG_PARTIAL_EXIT_FRACTION_EXCEEDS_ONE = "partial_exit_fraction_exceeds_one"
FLAG_FRACTION_EXCEEDS_REMAINING = "fraction_exceeds_remaining"
FLAG_LIFECYCLE_EVENT_TYPE_UNRECOGNIZED = "lifecycle_event_type_unrecognized"
FLAG_INCOMPLETE_CONTRACT_IDENTITY = "incomplete_contract_identity"


@dataclass(frozen=True)
class SignalSnapshot:
    """One current ``trade_signals`` row's field values, immutable, as
    assembled and passed into the pure lifecycle engine by repository/
    service code (Recovery Milestones R6.3/R6.4).

    This is deliberately the same shape later persisted (as canonical
    JSON) into ``trade_lifecycle_events.signal_snapshot`` when a build is
    persisted - one field list serves both the pure engine's own input
    and the immutable audit record, so they can never independently drift
    apart.

    Attributes:
        trade_signal_id: The trade_signals.id this snapshot was read from.
        raw_message_id: FK to raw_messages.id.
        trader_id: FK to traders.id.
        symbol: Ticker symbol.
        option_type: 'call'/'put', or None for an equity signal.
        strike: Decimal string, or None.
        expiration: Resolved ISO8601 date string, or None.
        event_type: Expected to be one of ENTRY/ADD/ROLL_UP/PARTIAL_EXIT/
            FULL_EXIT. Any other value (including None - though a None
            event_type should already have been filtered out by the
            caller before this snapshot is ever built, per the module
            scope boundary above) is classified as its own unresolved
            singleton rather than guessed or silently dropped.
        qualifier: Raw fraction text, "ALL OUT", a bracket annotation, or
            None.
        action: The raw action verb, exactly as stored.
        price: Decimal string, or None.
        stated_entry_price: Decimal string, or None.
        stated_return_pct: Decimal string, or None.
        notes: Free-text commentary, or None.
        extraction_id: FK to message_extractions.id, or None.
        ordering_key: An opaque, already-comparable tuple the caller used
            to sort this snapshot into its position within
            ordered_snapshots (e.g. (received_at, raw_message_id,
            trade_signal_id), or an insertion-order-only fallback tuple).
            This module never inspects, re-derives, or re-sorts by this
            value - it exists so callers/tests can confirm a snapshot's
            original ordering context, and so it can be serialized
            unchanged into signal_snapshot's persisted audit JSON.
    """

    trade_signal_id: int
    raw_message_id: int
    trader_id: int
    symbol: str
    option_type: Optional[str]
    strike: Optional[str]
    expiration: Optional[str]
    event_type: Optional[str]
    qualifier: Optional[str]
    action: str
    price: Optional[str]
    stated_entry_price: Optional[str]
    stated_return_pct: Optional[str]
    notes: Optional[str]
    extraction_id: Optional[int]
    ordering_key: tuple


@dataclass(frozen=True)
class LifecycleBuild:
    """One resulting lifecycle generation from replaying a window of
    SignalSnapshots.

    Attributes:
        status: Exactly one of 'open', 'partially_closed', 'closed',
            'orphan', 'unresolved', 'invalid'.
        remaining_fraction: The exact string form of a fractions.Fraction
            (e.g. "1", "1/2", "5/6", "0"), never a Decimal string -
            several approved fraction tokens (1/3, 1/6) do not terminate
            in base 10.
        opened_by_signal_id: The trade_signal_id that opened this
            generation (an ENTRY or ROLL_UP - never an ADD, which never
            opens a generation on its own; an ADD with no active
            generation becomes its own unresolved singleton instead), or
            None for 'orphan'/'unresolved' (no verified opener).
        closed_by_signal_id: The trade_signal_id whose processing brought
            remaining_fraction to zero (or below, for 'invalid') -
            applies uniformly whether the generation's final status is
            'closed', 'invalid', or an exhausted 'orphan'. None while a
            generation is still open/partially_closed/still-accumulating,
            and always None for 'unresolved'.
        member_signal_ids: Every member trade_signal_id, in the exact
            order they were folded into this generation. Exactly one
            element for 'unresolved'.
        ambiguity_flags: Every flag raised while building this specific
            generation, in the order first encountered (never
            duplicated). Empty tuple (never None) if none.
    """

    status: str
    remaining_fraction: str
    opened_by_signal_id: Optional[int]
    closed_by_signal_id: Optional[int]
    member_signal_ids: tuple[int, ...]
    ambiguity_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExitFractionResolution:
    """Result of parse_exit_fraction().

    Attributes:
        is_full_exit: True if this exit closes whatever remains,
            regardless of the exact qualifier text or notes content -
            authoritative from event_type alone.
        fraction: The exact Fraction this exit consumes, or None when
            is_full_exit is True (not applicable) or when the fraction
            could not be resolved at all.
        ambiguity_flags: Empty tuple when resolution succeeded (either
            is_full_exit, or a recognized fraction token); otherwise
            exactly one FLAG_PARTIAL_EXIT_* constant naming why.
    """

    is_full_exit: bool
    fraction: Optional[Fraction]
    ambiguity_flags: tuple[str, ...] = ()


def parse_exit_fraction(event_type: str, qualifier: Optional[str]) -> ExitFractionResolution:
    """Deterministically interpret one exit signal's quantity. Never raises.

    Args:
        event_type: Expected to be PARTIAL_EXIT or FULL_EXIT - callers
            (build_lifecycle_sequence, and repository/service code) only
            ever invoke this for an exit event_type.
        qualifier: The signal's raw qualifier text, or None.

    Returns:
        An ExitFractionResolution. FULL_EXIT always resolves to
        is_full_exit=True regardless of qualifier's exact text or any
        stop-out reason recorded elsewhere (e.g. in notes) - qualifier is
        never re-inspected once event_type says FULL_EXIT. A recognized
        PARTIAL_EXIT fraction token (exactly one of 1/2, 1/3, 1/4, 1/6,
        1/8, 1/16) resolves to its exact Fraction. Everything else -
        missing/blank qualifier, an unrecognized token (including a
        mathematically valid but unapproved fraction such as "1/5", a
        percentage, or a word such as "HALF" - none of which
        app/parser.py's current grammar can actually produce; these
        branches exist defensively for a hypothetical future
        parser_version, not the real corpus), a resolved value <= 0, or a
        resolved value > 1 (the latter two unreachable via the six
        approved tokens today, kept as defensive coverage against
        _APPROVED_FRACTION_TOKENS ever being extended incorrectly) -
        resolves to fraction=None with the specific flag naming why. An
        exit fraction is never guessed.
    """
    if event_type == EVENT_TYPE_FULL_EXIT:
        return ExitFractionResolution(is_full_exit=True, fraction=None)

    if qualifier is None or not qualifier.strip():
        return ExitFractionResolution(
            is_full_exit=False,
            fraction=None,
            ambiguity_flags=(FLAG_PARTIAL_EXIT_FRACTION_MISSING,),
        )

    fraction = _APPROVED_FRACTION_TOKENS.get(qualifier)
    if fraction is None:
        return ExitFractionResolution(
            is_full_exit=False,
            fraction=None,
            ambiguity_flags=(FLAG_PARTIAL_EXIT_FRACTION_UNRECOGNIZED,),
        )
    if fraction <= 0:
        return ExitFractionResolution(
            is_full_exit=False,
            fraction=None,
            ambiguity_flags=(FLAG_PARTIAL_EXIT_FRACTION_NON_POSITIVE,),
        )
    if fraction > 1:
        return ExitFractionResolution(
            is_full_exit=False,
            fraction=None,
            ambiguity_flags=(FLAG_PARTIAL_EXIT_FRACTION_EXCEEDS_ONE,),
        )
    return ExitFractionResolution(is_full_exit=False, fraction=fraction)


@dataclass
class _GenerationInProgress:
    """Mutable, module-private accumulator for one in-progress generation
    during a single replay. Never exposed outside this module - always
    finalized into a frozen LifecycleBuild via finalize() before being
    returned."""

    status: str
    remaining_fraction: Fraction
    opened_by_signal_id: Optional[int]
    closed_by_signal_id: Optional[int] = None
    member_signal_ids: list = field(default_factory=list)
    ambiguity_flags: list = field(default_factory=list)

    def add_flag_once(self, flag: str) -> None:
        if flag not in self.ambiguity_flags:
            self.ambiguity_flags.append(flag)

    def finalize(self) -> LifecycleBuild:
        return LifecycleBuild(
            status=self.status,
            remaining_fraction=str(self.remaining_fraction),
            opened_by_signal_id=self.opened_by_signal_id,
            closed_by_signal_id=self.closed_by_signal_id,
            member_signal_ids=tuple(self.member_signal_ids),
            ambiguity_flags=tuple(self.ambiguity_flags),
        )


def _unresolved_singleton(signal_id: int, flags) -> LifecycleBuild:
    return LifecycleBuild(
        status=STATUS_UNRESOLVED,
        remaining_fraction="0",
        opened_by_signal_id=None,
        closed_by_signal_id=None,
        member_signal_ids=(signal_id,),
        ambiguity_flags=tuple(flags),
    )


def build_lifecycle_sequence(ordered_snapshots) -> list:
    """Replay one lifecycle key's already-ordered current signal
    snapshots and return the resulting sequence of lifecycle generations.

    Args:
        ordered_snapshots: A list of SignalSnapshot, all belonging to the
            same lifecycle key (trader_id, symbol, option_type, strike,
            expiration - not re-verified here; the caller guarantees
            this) and already in the exact chronological order they
            should be replayed in (also not re-derived or re-sorted here
            - see the module docstring's scope boundary).

    Returns:
        A list of LifecycleBuild, ordered by the chronological position
        (within ordered_snapshots) of each generation's first member
        signal - never by the order in which a generation happened to be
        finalized/appended internally. A generation that starts earliest
        (e.g. an orphan begun by an early unmatched exit) is always
        returned before one that starts later, even if the earlier one
        finishes accumulating and is finalized after the later one
        already closed - finalization time never determines result
        order. Empty list for an empty input. Every signal in
        ordered_snapshots belongs to exactly one returned LifecycleBuild,
        and each build's own member_signal_ids remain in the exact
        chronological order they were folded in.

        A trailing element may be a still-open ('open'/'partially_closed')
        generation, or a separate still-accumulating ('orphan' with
        remaining_fraction != "0") generation, if the window ends
        mid-position - but never both at once. A verified ENTRY or
        ROLL_UP establishes a new chronological trade boundary: if a
        non-exhausted orphan is in progress when one arrives, that orphan
        is immediately finalized - its status and remaining_fraction are
        retained exactly as accumulated, it is flagged
        orphan_interrupted_by_new_entry, and it can never accept another
        signal afterward - before the opening signal starts its own,
        completely separate verified generation. The opening signal is
        never merged into the orphan. Every subsequent exit attaches only
        to the new verified generation while it remains open/
        partially_closed; once that generation becomes terminal, a later
        unmatched exit always starts a brand-new orphan - the interrupted
        orphan is never reopened or extended. An ADD arriving while an
        orphan is in progress does not interrupt or finalize it either -
        only a verified opening signal (ENTRY/ROLL_UP) does, since an ADD
        is not itself a verified opening entry (see EVENT_TYPE_ADD
        handling below). A caller extending this window in a later
        rebuild is responsible for including a trailing generation's own
        prior members again, so it can be correctly reconstructed from a
        fresh replay rather than patched in place.

    Never raises: every signal is classified into exactly one outcome -
    attach to an in-progress generation, open/extend/close one, or become
    its own unresolved singleton - there is no input shape this function
    does not have a defined, deterministic answer for.
    """
    results: list = []
    active: Optional[_GenerationInProgress] = None
    active_orphan: Optional[_GenerationInProgress] = None

    # Maps each input signal to its position in ordered_snapshots, so the
    # final result order can be determined by chronological input
    # position rather than by whichever order generations happened to be
    # finalized/appended during replay (a generation started early - e.g.
    # an orphan begun by an early unmatched exit - can finish
    # accumulating, and therefore be appended, after a later-starting
    # generation already closed).
    signal_index = {
        snapshot.trade_signal_id: position
        for position, snapshot in enumerate(ordered_snapshots)
    }

    for snapshot in ordered_snapshots:
        event_type = snapshot.event_type

        if event_type not in _APPROVED_EVENT_TYPES:
            # Includes event_type is None. A None event_type should
            # already have been filtered out by the caller before this
            # snapshot was ever built (see the module docstring's scope
            # boundary) - this branch exists so the function still never
            # raises and never guesses if that precondition is ever
            # violated, or a future parser_version emits an unrecognized
            # value.
            results.append(
                _unresolved_singleton(
                    snapshot.trade_signal_id, (FLAG_LIFECYCLE_EVENT_TYPE_UNRECOGNIZED,)
                )
            )
            continue

        if event_type in _OPENING_EVENT_TYPES:
            if active is not None:
                # An opening signal (ENTRY or ROLL_UP - treated
                # identically; ROLL_UP has no approved "attach"
                # semantics, unlike ADD) arriving while a position is
                # already active at this key. Never silently opens a
                # second concurrent active position - becomes its own
                # unresolved singleton; the active position is left
                # completely untouched.
                results.append(
                    _unresolved_singleton(
                        snapshot.trade_signal_id,
                        (FLAG_ENTRY_AGAINST_OPEN_POSITION_SAME_KEY,),
                    )
                )
                continue
            if active_orphan is not None:
                # A verified ENTRY/ROLL_UP establishes a new
                # chronological trade boundary: a non-exhausted orphan in
                # progress at this exact key is immediately finalized -
                # status stays 'orphan', remaining_fraction is retained
                # exactly as accumulated, and it can never accept another
                # signal afterward (active_orphan is cleared below, so
                # any later unmatched exit builds a brand-new orphan
                # instead of extending this one). The opening signal is
                # never merged into it - it always starts its own,
                # completely separate verified generation.
                active_orphan.add_flag_once(FLAG_ORPHAN_INTERRUPTED_BY_NEW_ENTRY)
                results.append(active_orphan.finalize())
                active_orphan = None
            active = _GenerationInProgress(
                status=STATUS_OPEN,
                remaining_fraction=Fraction(1),
                opened_by_signal_id=snapshot.trade_signal_id,
                member_signal_ids=[snapshot.trade_signal_id],
            )
            continue

        if event_type == EVENT_TYPE_ADD:
            if active is None:
                # An ADD with no verified active position never opens a
                # normal lifecycle - it becomes its own unresolved
                # singleton, is never open/closed, and is excluded from
                # R7's confirmed performance calculations by the same
                # status != 'unresolved' filter every unresolved row is
                # already excluded by. It never attaches to, finalizes,
                # or otherwise repairs an in-progress orphan either - an
                # ADD is not a verified opening entry (unlike ENTRY/
                # ROLL_UP, which do interrupt and finalize a non-exhausted
                # orphan - see the opening-event branch above), and an
                # orphan has no verified entry to scale into regardless.
                results.append(
                    _unresolved_singleton(
                        snapshot.trade_signal_id,
                        (FLAG_AMBIGUOUS_ADD_NO_OPEN_POSITION,),
                    )
                )
                continue
            active.member_signal_ids.append(snapshot.trade_signal_id)
            active.add_flag_once(FLAG_SCALE_IN_COST_BASIS_NOT_MODELED)
            continue

        # event_type is PARTIAL_EXIT or FULL_EXIT.
        resolution = parse_exit_fraction(event_type, snapshot.qualifier)
        if not resolution.is_full_exit and resolution.fraction is None:
            # An exit with no usable fraction is never guessed, and it
            # never touches whatever active/active_orphan generation
            # might otherwise have received it - both are left exactly
            # as they were.
            results.append(
                _unresolved_singleton(snapshot.trade_signal_id, resolution.ambiguity_flags)
            )
            continue

        if active is not None:
            target = active
        else:
            if active_orphan is None:
                active_orphan = _GenerationInProgress(
                    status=STATUS_ORPHAN,
                    remaining_fraction=Fraction(1),
                    opened_by_signal_id=None,
                )
            target = active_orphan

        target.member_signal_ids.append(snapshot.trade_signal_id)

        if resolution.is_full_exit:
            target.remaining_fraction = Fraction(0)
        else:
            target.remaining_fraction -= resolution.fraction

        if target.remaining_fraction < 0:
            # Applies uniformly whether target is the active position or
            # an in-progress orphan: a fraction inconsistency is a data-
            # quality problem regardless of whether there is a verified
            # entry, and takes priority over the softer 'orphan'
            # classification.
            target.status = STATUS_INVALID
            target.remaining_fraction = Fraction(0)
            target.closed_by_signal_id = snapshot.trade_signal_id
            target.add_flag_once(FLAG_FRACTION_EXCEEDS_REMAINING)
            results.append(target.finalize())
            if target is active:
                active = None
            else:
                active_orphan = None
            continue

        if target.remaining_fraction == 0:
            target.closed_by_signal_id = snapshot.trade_signal_id
            if target is active:
                target.status = STATUS_CLOSED
                results.append(target.finalize())
                active = None
            else:
                # status stays STATUS_ORPHAN - orphan is a permanent
                # data-quality classification, never promoted to
                # 'closed' even once fully exhausted. This generation is
                # now terminal: a later, genuinely new unmatched exit at
                # this key starts a fresh orphan-of-one, never
                # re-attaching here.
                results.append(target.finalize())
                active_orphan = None
            continue

        if target is active:
            target.status = STATUS_PARTIALLY_CLOSED
        # else: orphan keeps accumulating (remaining_fraction > 0),
        # not yet finalized/appended to results.

    if active is not None:
        results.append(active.finalize())
    if active_orphan is not None:
        results.append(active_orphan.finalize())

    # A build's first member is always its chronologically earliest
    # (members are only ever appended in the order encountered), so
    # sorting by that single lookup is sufficient - no need to scan every
    # member for a minimum. Stable and collision-free: every signal
    # belongs to exactly one build, so no two builds can share a first-
    # member index.
    results.sort(key=lambda build: signal_index[build.member_signal_ids[0]])

    return results
