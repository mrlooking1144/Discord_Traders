"""Tests for Recovery Milestone R6.2: the pure lifecycle-matching engine.

Covers database/lifecycle.py only - no database, no repository, no
service, no UI. All fixtures are synthetic; the real 68-message corpus
acceptance pass is a later milestone (R6.6), not this one.
"""

import unittest
from fractions import Fraction
from unittest.mock import patch

from database.lifecycle import (
    EVENT_TYPE_ADD,
    EVENT_TYPE_ENTRY,
    EVENT_TYPE_FULL_EXIT,
    EVENT_TYPE_PARTIAL_EXIT,
    EVENT_TYPE_ROLL_UP,
    FLAG_AMBIGUOUS_ADD_NO_OPEN_POSITION,
    FLAG_ENTRY_AGAINST_OPEN_POSITION_SAME_KEY,
    FLAG_FRACTION_EXCEEDS_REMAINING,
    FLAG_INCOMPLETE_CONTRACT_IDENTITY,
    FLAG_LIFECYCLE_EVENT_TYPE_UNRECOGNIZED,
    FLAG_ORPHAN_INTERRUPTED_BY_NEW_ENTRY,
    FLAG_PARTIAL_EXIT_FRACTION_EXCEEDS_ONE,
    FLAG_PARTIAL_EXIT_FRACTION_MISSING,
    FLAG_PARTIAL_EXIT_FRACTION_NON_POSITIVE,
    FLAG_PARTIAL_EXIT_FRACTION_UNRECOGNIZED,
    FLAG_SCALE_IN_COST_BASIS_NOT_MODELED,
    STATUS_CLOSED,
    STATUS_INVALID,
    STATUS_OPEN,
    STATUS_ORPHAN,
    STATUS_PARTIALLY_CLOSED,
    STATUS_UNRESOLVED,
    ExitFractionResolution,
    LifecycleBuild,
    SignalSnapshot,
    _APPROVED_EVENT_TYPES,
    _APPROVED_FRACTION_TOKENS,
    build_lifecycle_sequence,
    parse_exit_fraction,
)

_NEXT_ID = iter(range(1, 100000))


def _snapshot(event_type, qualifier=None, trade_signal_id=None, **overrides):
    """Build one SignalSnapshot with sensible defaults for a single
    synthetic key (trader 1, IBM, call, strike 207.5, expiration
    2026-07-24), overriding only the fields a given test cares about."""
    if trade_signal_id is None:
        trade_signal_id = next(_NEXT_ID)
    fields = dict(
        trade_signal_id=trade_signal_id,
        raw_message_id=trade_signal_id,
        trader_id=1,
        symbol="IBM",
        option_type="call",
        strike="207.5",
        expiration="2026-07-24",
        event_type=event_type,
        qualifier=qualifier,
        action="BOUGHT" if event_type in (EVENT_TYPE_ENTRY, EVENT_TYPE_ADD, EVENT_TYPE_ROLL_UP) else "SOLD",
        price="1.00",
        stated_entry_price=None,
        stated_return_pct=None,
        notes=None,
        extraction_id=None,
        ordering_key=(trade_signal_id,),
    )
    fields.update(overrides)
    return SignalSnapshot(**fields)


class ParseExitFractionTests(unittest.TestCase):
    def test_full_exit_with_all_out_qualifier(self):
        result = parse_exit_fraction(EVENT_TYPE_FULL_EXIT, "ALL OUT")
        self.assertTrue(result.is_full_exit)
        self.assertIsNone(result.fraction)
        self.assertEqual(result.ambiguity_flags, ())

    def test_full_exit_regardless_of_qualifier_text(self):
        # event_type alone is authoritative - qualifier's exact text is
        # never inspected once event_type says FULL_EXIT.
        for qualifier in (None, "", "1/2", "garbage", "ALL OUT [stop]"):
            with self.subTest(qualifier=qualifier):
                result = parse_exit_fraction(EVENT_TYPE_FULL_EXIT, qualifier)
                self.assertTrue(result.is_full_exit)
                self.assertIsNone(result.fraction)
                self.assertEqual(result.ambiguity_flags, ())

    def test_every_approved_fraction_token_resolves_exactly(self):
        expected = {
            "1/2": Fraction(1, 2),
            "1/3": Fraction(1, 3),
            "1/4": Fraction(1, 4),
            "1/6": Fraction(1, 6),
            "1/8": Fraction(1, 8),
            "1/16": Fraction(1, 16),
        }
        for token, fraction in expected.items():
            with self.subTest(token=token):
                result = parse_exit_fraction(EVENT_TYPE_PARTIAL_EXIT, token)
                self.assertFalse(result.is_full_exit)
                self.assertEqual(result.fraction, fraction)
                self.assertEqual(result.ambiguity_flags, ())

    def test_partial_exit_missing_qualifier(self):
        for qualifier in (None, "", "   "):
            with self.subTest(qualifier=qualifier):
                result = parse_exit_fraction(EVENT_TYPE_PARTIAL_EXIT, qualifier)
                self.assertIsNone(result.fraction)
                self.assertEqual(result.ambiguity_flags, (FLAG_PARTIAL_EXIT_FRACTION_MISSING,))

    def test_partial_exit_rejects_unapproved_fraction_1_5(self):
        # 1/5 is a well-formed, mathematically valid fraction but is not
        # one of the six tokens app/parser.py's grammar ever produces -
        # explicitly rejected, matching tests.test_extractor's own
        # rejection-of-1/5 coverage. Never guessed as 0.2.
        result = parse_exit_fraction(EVENT_TYPE_PARTIAL_EXIT, "1/5")
        self.assertIsNone(result.fraction)
        self.assertEqual(result.ambiguity_flags, (FLAG_PARTIAL_EXIT_FRACTION_UNRECOGNIZED,))

    def test_partial_exit_rejects_percentage_and_textual_forms(self):
        # Neither is currently producible by app/parser.py's grammar -
        # defensive coverage only, never guessed.
        for qualifier in ("50%", "HALF", "half"):
            with self.subTest(qualifier=qualifier):
                result = parse_exit_fraction(EVENT_TYPE_PARTIAL_EXIT, qualifier)
                self.assertIsNone(result.fraction)
                self.assertEqual(
                    result.ambiguity_flags, (FLAG_PARTIAL_EXIT_FRACTION_UNRECOGNIZED,)
                )

    def test_partial_exit_rejects_all_out_text_as_a_fraction(self):
        # "ALL OUT" is only ever meaningful paired with FULL_EXIT; as a
        # PARTIAL_EXIT qualifier it is simply an unrecognized token.
        result = parse_exit_fraction(EVENT_TYPE_PARTIAL_EXIT, "ALL OUT")
        self.assertIsNone(result.fraction)
        self.assertEqual(result.ambiguity_flags, (FLAG_PARTIAL_EXIT_FRACTION_UNRECOGNIZED,))

    def test_defensive_non_positive_fraction_branch(self):
        # Unreachable via the six real approved tokens today (all are
        # strictly between 0 and 1) - proven here only by monkeypatching
        # the token table, confirming the guard exists and fires
        # correctly if that table were ever extended incorrectly.
        with patch.dict(
            "database.lifecycle._APPROVED_FRACTION_TOKENS",
            {"0/2": Fraction(0)},
        ):
            result = parse_exit_fraction(EVENT_TYPE_PARTIAL_EXIT, "0/2")
        self.assertIsNone(result.fraction)
        self.assertEqual(result.ambiguity_flags, (FLAG_PARTIAL_EXIT_FRACTION_NON_POSITIVE,))

    def test_defensive_exceeds_one_fraction_branch(self):
        with patch.dict(
            "database.lifecycle._APPROVED_FRACTION_TOKENS",
            {"3/2": Fraction(3, 2)},
        ):
            result = parse_exit_fraction(EVENT_TYPE_PARTIAL_EXIT, "3/2")
        self.assertIsNone(result.fraction)
        self.assertEqual(result.ambiguity_flags, (FLAG_PARTIAL_EXIT_FRACTION_EXCEEDS_ONE,))

    def test_never_raises_on_unexpected_event_type(self):
        try:
            parse_exit_fraction("SOMETHING_ELSE", "1/2")
        except Exception as exc:  # pragma: no cover - failure path only
            self.fail(f"parse_exit_fraction raised unexpectedly: {exc!r}")

    def test_result_is_frozen(self):
        result = ExitFractionResolution(is_full_exit=True, fraction=None)
        with self.assertRaises(Exception):
            result.is_full_exit = False


class BuildLifecycleSequenceEmptyAndEntryTests(unittest.TestCase):
    def test_empty_input_returns_empty_list(self):
        self.assertEqual(build_lifecycle_sequence([]), [])

    def test_single_entry_is_trailing_open(self):
        entry = _snapshot(EVENT_TYPE_ENTRY)
        results = build_lifecycle_sequence([entry])

        self.assertEqual(len(results), 1)
        build = results[0]
        self.assertEqual(build.status, STATUS_OPEN)
        self.assertEqual(build.remaining_fraction, "1")
        self.assertEqual(build.opened_by_signal_id, entry.trade_signal_id)
        self.assertIsNone(build.closed_by_signal_id)
        self.assertEqual(build.member_signal_ids, (entry.trade_signal_id,))
        self.assertEqual(build.ambiguity_flags, ())

    def test_roll_up_opens_a_fresh_lifecycle_identically_to_entry(self):
        roll_up = _snapshot(EVENT_TYPE_ROLL_UP)
        results = build_lifecycle_sequence([roll_up])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, STATUS_OPEN)
        self.assertEqual(results[0].opened_by_signal_id, roll_up.trade_signal_id)


class BuildLifecycleSequenceExitTests(unittest.TestCase):
    def test_full_exit_closes_a_fresh_entry(self):
        entry = _snapshot(EVENT_TYPE_ENTRY)
        exit_ = _snapshot(EVENT_TYPE_FULL_EXIT, "ALL OUT")
        results = build_lifecycle_sequence([entry, exit_])

        self.assertEqual(len(results), 1)
        build = results[0]
        self.assertEqual(build.status, STATUS_CLOSED)
        self.assertEqual(build.remaining_fraction, "0")
        self.assertEqual(build.opened_by_signal_id, entry.trade_signal_id)
        self.assertEqual(build.closed_by_signal_id, exit_.trade_signal_id)
        self.assertEqual(
            build.member_signal_ids, (entry.trade_signal_id, exit_.trade_signal_id)
        )

    def test_partial_exit_leaves_a_trailing_partially_closed_generation(self):
        entry = _snapshot(EVENT_TYPE_ENTRY)
        exit_ = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/2")
        results = build_lifecycle_sequence([entry, exit_])

        self.assertEqual(len(results), 1)
        build = results[0]
        self.assertEqual(build.status, STATUS_PARTIALLY_CLOSED)
        self.assertEqual(build.remaining_fraction, "1/2")
        self.assertIsNone(build.closed_by_signal_id)

    def test_partial_exits_summing_exactly_to_one_close_via_fraction(self):
        entry = _snapshot(EVENT_TYPE_ENTRY)
        exit1 = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/2")
        exit2 = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/2")
        results = build_lifecycle_sequence([entry, exit1, exit2])

        self.assertEqual(len(results), 1)
        build = results[0]
        self.assertEqual(build.status, STATUS_CLOSED)
        self.assertEqual(build.remaining_fraction, "0")
        self.assertEqual(build.closed_by_signal_id, exit2.trade_signal_id)

    def test_full_exit_closes_remaining_fraction_not_a_fresh_100_percent(self):
        entry = _snapshot(EVENT_TYPE_ENTRY)
        partial = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/4")
        all_out = _snapshot(EVENT_TYPE_FULL_EXIT, "ALL OUT")
        results = build_lifecycle_sequence([entry, partial, all_out])

        self.assertEqual(len(results), 1)
        build = results[0]
        self.assertEqual(build.status, STATUS_CLOSED)
        self.assertEqual(build.remaining_fraction, "0")
        self.assertEqual(build.closed_by_signal_id, all_out.trade_signal_id)

    def test_fraction_exceeding_remainder_becomes_invalid_and_terminates(self):
        # entry -> 1/3 (remaining 2/3) -> 1/3 (remaining 1/3) -> 1/2
        # (would leave -1/6): every token is a real approved token, so
        # this reaches the fraction_exceeds_remaining path without any
        # synthetic/unreachable input.
        entry = _snapshot(EVENT_TYPE_ENTRY)
        e1 = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/3")
        e2 = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/3")
        e3 = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/2")
        results = build_lifecycle_sequence([entry, e1, e2, e3])

        self.assertEqual(len(results), 1)
        build = results[0]
        self.assertEqual(build.status, STATUS_INVALID)
        self.assertEqual(build.remaining_fraction, "0")
        self.assertEqual(build.closed_by_signal_id, e3.trade_signal_id)
        self.assertIn(FLAG_FRACTION_EXCEEDS_REMAINING, build.ambiguity_flags)

    def test_a_signal_after_invalid_starts_completely_fresh(self):
        entry = _snapshot(EVENT_TYPE_ENTRY)
        e1 = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/3")
        e2 = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/3")
        e3 = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/2")  # drives it invalid
        later_entry = _snapshot(EVENT_TYPE_ENTRY)
        results = build_lifecycle_sequence([entry, e1, e2, e3, later_entry])

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].status, STATUS_INVALID)
        self.assertEqual(results[1].status, STATUS_OPEN)
        self.assertEqual(results[1].opened_by_signal_id, later_entry.trade_signal_id)

    def test_unusable_fraction_becomes_its_own_unresolved_singleton(self):
        entry = _snapshot(EVENT_TYPE_ENTRY)
        bad_exit = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/5")
        results = build_lifecycle_sequence([entry, bad_exit])

        self.assertEqual(len(results), 2)
        unresolved = next(b for b in results if b.status == STATUS_UNRESOLVED)
        self.assertEqual(unresolved.member_signal_ids, (bad_exit.trade_signal_id,))
        self.assertEqual(
            unresolved.ambiguity_flags, (FLAG_PARTIAL_EXIT_FRACTION_UNRECOGNIZED,)
        )
        # The active position itself is untouched - still trailing open.
        active = next(b for b in results if b.status == STATUS_OPEN)
        self.assertEqual(active.remaining_fraction, "1")
        self.assertEqual(active.member_signal_ids, (entry.trade_signal_id,))


class BuildLifecycleSequenceAddTests(unittest.TestCase):
    def test_add_with_no_active_position_becomes_unresolved_singleton(self):
        add = _snapshot(EVENT_TYPE_ADD)
        results = build_lifecycle_sequence([add])

        self.assertEqual(len(results), 1)
        build = results[0]
        self.assertEqual(build.status, STATUS_UNRESOLVED)
        self.assertEqual(build.member_signal_ids, (add.trade_signal_id,))
        self.assertEqual(build.ambiguity_flags, (FLAG_AMBIGUOUS_ADD_NO_OPEN_POSITION,))

    def test_add_never_opens_a_normal_open_or_closed_lifecycle(self):
        # The TC QQQ 685P corpus shape: an ADD with no matching prior
        # entry, followed by a full exit. Must NOT become one closed
        # lifecycle - the ADD is its own unresolved singleton and the
        # exit becomes its own separate orphan.
        add = _snapshot(EVENT_TYPE_ADD)
        exit_ = _snapshot(EVENT_TYPE_FULL_EXIT, "ALL OUT")
        results = build_lifecycle_sequence([add, exit_])

        self.assertEqual(len(results), 2)
        statuses = {b.status for b in results}
        self.assertEqual(statuses, {STATUS_UNRESOLVED, STATUS_ORPHAN})
        unresolved = next(b for b in results if b.status == STATUS_UNRESOLVED)
        self.assertEqual(unresolved.member_signal_ids, (add.trade_signal_id,))
        orphan = next(b for b in results if b.status == STATUS_ORPHAN)
        self.assertEqual(orphan.member_signal_ids, (exit_.trade_signal_id,))
        self.assertEqual(orphan.remaining_fraction, "0")

    def test_add_attaches_to_an_active_position_and_flags_it_once(self):
        entry = _snapshot(EVENT_TYPE_ENTRY)
        add1 = _snapshot(EVENT_TYPE_ADD)
        add2 = _snapshot(EVENT_TYPE_ADD)
        results = build_lifecycle_sequence([entry, add1, add2])

        self.assertEqual(len(results), 1)
        build = results[0]
        self.assertEqual(build.status, STATUS_OPEN)
        self.assertEqual(build.remaining_fraction, "1")
        self.assertEqual(
            build.member_signal_ids,
            (entry.trade_signal_id, add1.trade_signal_id, add2.trade_signal_id),
        )
        self.assertEqual(build.ambiguity_flags, (FLAG_SCALE_IN_COST_BASIS_NOT_MODELED,))

    def test_add_does_not_attach_to_an_in_progress_orphan(self):
        unmatched_exit = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/2")
        add = _snapshot(EVENT_TYPE_ADD)
        results = build_lifecycle_sequence([unmatched_exit, add])

        self.assertEqual(len(results), 2)
        orphan = next(b for b in results if b.status == STATUS_ORPHAN)
        self.assertEqual(orphan.member_signal_ids, (unmatched_exit.trade_signal_id,))
        unresolved = next(b for b in results if b.status == STATUS_UNRESOLVED)
        self.assertEqual(unresolved.member_signal_ids, (add.trade_signal_id,))

    def test_add_does_not_finalize_or_repair_an_in_progress_orphan(self):
        # An ADD is not a verified opening entry (unlike ENTRY/ROLL_UP),
        # so unlike those, it must not interrupt/finalize an in-progress
        # orphan - the orphan is left completely untouched: still
        # in-progress, unflagged, remaining_fraction unchanged.
        unmatched_exit = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/2")
        add = _snapshot(EVENT_TYPE_ADD)
        results = build_lifecycle_sequence([unmatched_exit, add])

        orphan = next(b for b in results if b.status == STATUS_ORPHAN)
        self.assertEqual(orphan.remaining_fraction, "1/2")
        self.assertEqual(orphan.ambiguity_flags, ())
        self.assertNotIn(FLAG_ORPHAN_INTERRUPTED_BY_NEW_ENTRY, orphan.ambiguity_flags)


class BuildLifecycleSequenceOrphanTests(unittest.TestCase):
    def test_three_unmatched_exits_summing_to_one_become_a_single_orphan(self):
        # Mirrors the real corpus's spacemonkey IBM 210C case: SOLD 1/2,
        # SOLD 1/4, ALL OUT (closing the remaining 1/4) - no BOUGHT
        # anywhere for this key.
        e1 = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/2")
        e2 = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/4")
        e3 = _snapshot(EVENT_TYPE_FULL_EXIT, "ALL OUT")
        results = build_lifecycle_sequence([e1, e2, e3])

        self.assertEqual(len(results), 1)
        build = results[0]
        self.assertEqual(build.status, STATUS_ORPHAN)
        self.assertIsNone(build.opened_by_signal_id)
        self.assertEqual(build.remaining_fraction, "0")
        self.assertEqual(build.closed_by_signal_id, e3.trade_signal_id)
        self.assertEqual(
            build.member_signal_ids,
            (e1.trade_signal_id, e2.trade_signal_id, e3.trade_signal_id),
        )

    def test_orphan_status_never_becomes_closed_once_exhausted(self):
        e1 = _snapshot(EVENT_TYPE_FULL_EXIT, "ALL OUT")
        results = build_lifecycle_sequence([e1])
        self.assertEqual(results[0].status, STATUS_ORPHAN)
        self.assertNotEqual(results[0].status, STATUS_CLOSED)

    def test_trailing_unexhausted_orphan_is_still_reported(self):
        e1 = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/2")
        results = build_lifecycle_sequence([e1])

        self.assertEqual(len(results), 1)
        build = results[0]
        self.assertEqual(build.status, STATUS_ORPHAN)
        self.assertEqual(build.remaining_fraction, "1/2")
        self.assertIsNone(build.closed_by_signal_id)

    def test_exhausted_orphan_never_absorbs_a_later_unrelated_exit(self):
        e1 = _snapshot(EVENT_TYPE_FULL_EXIT, "ALL OUT")  # exhausts immediately
        e2 = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/2")  # a later, separate orphan
        results = build_lifecycle_sequence([e1, e2])

        self.assertEqual(len(results), 2)
        first_orphan, second_orphan = results
        self.assertEqual(first_orphan.status, STATUS_ORPHAN)
        self.assertEqual(first_orphan.member_signal_ids, (e1.trade_signal_id,))
        self.assertEqual(second_orphan.status, STATUS_ORPHAN)
        self.assertEqual(second_orphan.member_signal_ids, (e2.trade_signal_id,))
        self.assertEqual(
            set(first_orphan.member_signal_ids) & set(second_orphan.member_signal_ids),
            set(),
        )

    def test_entry_interrupts_and_finalizes_a_non_exhausted_orphan(self):
        # Corrected boundary rule: a verified ENTRY/ROLL_UP never lets an
        # in-progress orphan keep accumulating alongside it - it
        # immediately finalizes the orphan (flagged, unchanged
        # remaining_fraction) before opening its own separate position.
        # See BoundaryOrphanInterruptedByNewEntryTests below for the full
        # required scenario coverage.
        unmatched_exit = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/2")
        entry = _snapshot(EVENT_TYPE_ENTRY)
        results = build_lifecycle_sequence([unmatched_exit, entry])

        self.assertEqual(len(results), 2)
        orphan = next(b for b in results if b.status == STATUS_ORPHAN)
        open_build = next(b for b in results if b.status == STATUS_OPEN)
        self.assertEqual(orphan.member_signal_ids, (unmatched_exit.trade_signal_id,))
        self.assertEqual(orphan.remaining_fraction, "1/2")
        self.assertEqual(orphan.ambiguity_flags, (FLAG_ORPHAN_INTERRUPTED_BY_NEW_ENTRY,))
        self.assertEqual(open_build.opened_by_signal_id, entry.trade_signal_id)
        self.assertEqual(open_build.member_signal_ids, (entry.trade_signal_id,))
        self.assertEqual(open_build.remaining_fraction, "1")


class BoundaryOrphanInterruptedByNewEntryTests(unittest.TestCase):
    """Dedicated coverage for the corrected orphan-interruption boundary
    rule: a verified ENTRY or ROLL_UP establishes a new chronological
    trade boundary and always finalizes a non-exhausted orphan at the
    same key, rather than letting it keep accumulating alongside a fresh
    verified position."""

    def test_partial_exit_creates_orphan_with_remaining_one_half(self):
        e1 = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/2")
        results = build_lifecycle_sequence([e1])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, STATUS_ORPHAN)
        self.assertEqual(results[0].remaining_fraction, "1/2")
        self.assertEqual(results[0].ambiguity_flags, ())

    def test_later_entry_finalizes_that_orphan_with_the_interrupted_flag(self):
        e1 = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/2")
        entry = _snapshot(EVENT_TYPE_ENTRY)
        results = build_lifecycle_sequence([e1, entry])

        orphan = next(b for b in results if b.status == STATUS_ORPHAN)
        self.assertEqual(orphan.ambiguity_flags, (FLAG_ORPHAN_INTERRUPTED_BY_NEW_ENTRY,))

    def test_entry_opens_a_separate_verified_lifecycle_not_merged_into_orphan(self):
        e1 = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/2")
        entry = _snapshot(EVENT_TYPE_ENTRY)
        results = build_lifecycle_sequence([e1, entry])

        self.assertEqual(len(results), 2)
        open_build = next(b for b in results if b.status == STATUS_OPEN)
        self.assertEqual(open_build.opened_by_signal_id, entry.trade_signal_id)
        self.assertEqual(open_build.member_signal_ids, (entry.trade_signal_id,))
        self.assertEqual(open_build.remaining_fraction, "1")
        orphan = next(b for b in results if b.status == STATUS_ORPHAN)
        self.assertNotIn(entry.trade_signal_id, orphan.member_signal_ids)

    def test_subsequent_partial_exit_affects_only_the_verified_lifecycle(self):
        e1 = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/2")
        entry = _snapshot(EVENT_TYPE_ENTRY)
        e2 = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/4")
        results = build_lifecycle_sequence([e1, entry, e2])

        self.assertEqual(len(results), 2)
        orphan = next(b for b in results if b.status == STATUS_ORPHAN)
        verified = next(b for b in results if b.status == STATUS_PARTIALLY_CLOSED)
        # The interrupted orphan is completely unaffected by the later exit.
        self.assertEqual(orphan.remaining_fraction, "1/2")
        self.assertEqual(orphan.member_signal_ids, (e1.trade_signal_id,))
        self.assertNotIn(e2.trade_signal_id, orphan.member_signal_ids)
        # Only the verified lifecycle absorbed it.
        self.assertEqual(verified.remaining_fraction, "3/4")
        self.assertEqual(
            verified.member_signal_ids, (entry.trade_signal_id, e2.trade_signal_id)
        )

    def test_subsequent_full_exit_closes_only_the_verified_lifecycle(self):
        e1 = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/2")
        entry = _snapshot(EVENT_TYPE_ENTRY)
        e2 = _snapshot(EVENT_TYPE_FULL_EXIT, "ALL OUT")
        results = build_lifecycle_sequence([e1, entry, e2])

        self.assertEqual(len(results), 2)
        orphan = next(b for b in results if b.status == STATUS_ORPHAN)
        closed = next(b for b in results if b.status == STATUS_CLOSED)
        self.assertEqual(orphan.remaining_fraction, "1/2")
        self.assertEqual(orphan.ambiguity_flags, (FLAG_ORPHAN_INTERRUPTED_BY_NEW_ENTRY,))
        self.assertEqual(closed.opened_by_signal_id, entry.trade_signal_id)
        self.assertEqual(closed.closed_by_signal_id, e2.trade_signal_id)
        self.assertEqual(closed.remaining_fraction, "0")

    def test_interrupted_orphan_membership_and_fraction_never_change(self):
        e1 = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/2")
        entry = _snapshot(EVENT_TYPE_ENTRY)
        e2 = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/4")
        e3 = _snapshot(EVENT_TYPE_FULL_EXIT, "ALL OUT")
        results = build_lifecycle_sequence([e1, entry, e2, e3])

        orphan = next(b for b in results if b.status == STATUS_ORPHAN)
        self.assertEqual(orphan.member_signal_ids, (e1.trade_signal_id,))
        self.assertEqual(orphan.remaining_fraction, "1/2")
        self.assertIsNone(orphan.closed_by_signal_id)

    def test_unmatched_exit_after_verified_lifecycle_closes_starts_a_fresh_orphan(self):
        # After the verified lifecycle (opened by `entry`) becomes
        # terminal, a later unmatched exit must never reopen or extend
        # the interrupted orphan - it starts an entirely new one.
        e1 = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/2")  # interrupted orphan
        entry = _snapshot(EVENT_TYPE_ENTRY)
        closing_exit = _snapshot(EVENT_TYPE_FULL_EXIT, "ALL OUT")  # closes entry
        later_unmatched_exit = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/3")  # fresh orphan
        results = build_lifecycle_sequence([e1, entry, closing_exit, later_unmatched_exit])

        self.assertEqual(len(results), 3)
        interrupted_orphan, closed, fresh_orphan = results
        self.assertEqual(interrupted_orphan.status, STATUS_ORPHAN)
        self.assertEqual(interrupted_orphan.member_signal_ids, (e1.trade_signal_id,))
        self.assertEqual(
            interrupted_orphan.ambiguity_flags, (FLAG_ORPHAN_INTERRUPTED_BY_NEW_ENTRY,)
        )
        self.assertEqual(closed.status, STATUS_CLOSED)
        self.assertEqual(fresh_orphan.status, STATUS_ORPHAN)
        self.assertEqual(fresh_orphan.member_signal_ids, (later_unmatched_exit.trade_signal_id,))
        self.assertEqual(fresh_orphan.remaining_fraction, "2/3")
        self.assertEqual(fresh_orphan.ambiguity_flags, ())
        # The fresh orphan is a genuinely distinct object from the
        # interrupted one - disjoint membership, independently unflagged.
        self.assertEqual(
            set(interrupted_orphan.member_signal_ids) & set(fresh_orphan.member_signal_ids),
            set(),
        )

    def test_roll_up_produces_the_same_interruption_boundary_as_entry(self):
        e1 = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/2")
        roll_up = _snapshot(EVENT_TYPE_ROLL_UP)
        results = build_lifecycle_sequence([e1, roll_up])

        self.assertEqual(len(results), 2)
        orphan = next(b for b in results if b.status == STATUS_ORPHAN)
        open_build = next(b for b in results if b.status == STATUS_OPEN)
        self.assertEqual(orphan.remaining_fraction, "1/2")
        self.assertEqual(orphan.ambiguity_flags, (FLAG_ORPHAN_INTERRUPTED_BY_NEW_ENTRY,))
        self.assertEqual(open_build.opened_by_signal_id, roll_up.trade_signal_id)

    def test_add_without_entry_neither_interrupts_nor_repairs_an_orphan(self):
        e1 = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/2")
        add = _snapshot(EVENT_TYPE_ADD)
        results = build_lifecycle_sequence([e1, add])

        self.assertEqual(len(results), 2)
        orphan = next(b for b in results if b.status == STATUS_ORPHAN)
        unresolved = next(b for b in results if b.status == STATUS_UNRESOLVED)
        # The orphan remains in progress, completely unflagged - only a
        # verified ENTRY/ROLL_UP interrupts it, never an ADD.
        self.assertEqual(orphan.remaining_fraction, "1/2")
        self.assertEqual(orphan.ambiguity_flags, ())
        self.assertEqual(unresolved.ambiguity_flags, (FLAG_AMBIGUOUS_ADD_NO_OPEN_POSITION,))

    def test_real_corpus_synthetic_examples_remain_unchanged(self):
        # Corpus shapes with no orphan-then-entry interruption anywhere
        # in them must produce byte-for-byte the same result as before
        # this correction: spacemonkey IBM 210C (pure orphan, no entry
        # ever appears) and spacemonkey SPX 7430P (stop then re-entry,
        # no orphan ever appears).
        ibm_210c = [
            _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/2"),
            _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/4"),
            _snapshot(EVENT_TYPE_FULL_EXIT, "ALL OUT"),
        ]
        ibm_results = build_lifecycle_sequence(ibm_210c)
        self.assertEqual(len(ibm_results), 1)
        self.assertEqual(ibm_results[0].status, STATUS_ORPHAN)
        self.assertEqual(ibm_results[0].remaining_fraction, "0")
        self.assertEqual(ibm_results[0].ambiguity_flags, ())

        spx_7430p = [
            _snapshot(EVENT_TYPE_ENTRY),
            _snapshot(EVENT_TYPE_FULL_EXIT, "ALL OUT", notes="HIT STOP"),
            _snapshot(EVENT_TYPE_ROLL_UP),
            _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/3"),
            _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/3"),
            _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/6"),
            _snapshot(EVENT_TYPE_FULL_EXIT, "ALL OUT"),
        ]
        spx_results = build_lifecycle_sequence(spx_7430p)
        self.assertEqual(len(spx_results), 2)
        self.assertTrue(all(b.status == STATUS_CLOSED for b in spx_results))
        self.assertEqual(
            set(spx_results[0].member_signal_ids) & set(spx_results[1].member_signal_ids),
            set(),
        )


class OutputOrderingInvariantTests(unittest.TestCase):
    """Recovery Milestone R6.2 output-order correction: LifecycleBuild
    results must be ordered by the chronological input position of each
    generation's first member signal - never by the order in which a
    generation happened to be finalized/appended internally."""

    def test_orphan_before_interleaved_add_singleton_despite_later_finalization(self):
        # PARTIAL_EXIT starts a non-exhausted orphan; ADD (no active
        # lifecycle) becomes its own unresolved singleton without
        # interrupting that orphan; a later FULL_EXIT completes the
        # original orphan. The orphan is finalized LAST (at the
        # FULL_EXIT) but must be returned FIRST, since its first member
        # (the initial PARTIAL_EXIT) chronologically precedes the ADD.
        initial_partial_exit = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/2")
        add = _snapshot(EVENT_TYPE_ADD)
        completing_full_exit = _snapshot(EVENT_TYPE_FULL_EXIT, "ALL OUT")
        results = build_lifecycle_sequence(
            [initial_partial_exit, add, completing_full_exit]
        )

        self.assertEqual(len(results), 2)
        orphan, unresolved = results
        self.assertEqual(orphan.status, STATUS_ORPHAN)
        self.assertEqual(
            orphan.member_signal_ids,
            (initial_partial_exit.trade_signal_id, completing_full_exit.trade_signal_id),
        )
        self.assertEqual(orphan.remaining_fraction, "0")
        self.assertEqual(unresolved.status, STATUS_UNRESOLVED)
        self.assertEqual(unresolved.member_signal_ids, (add.trade_signal_id,))
        self.assertEqual(
            unresolved.ambiguity_flags, (FLAG_AMBIGUOUS_ADD_NO_OPEN_POSITION,)
        )

    def test_verified_lifecycle_before_interleaved_second_entry_singleton(self):
        # ENTRY opens a lifecycle; a second ENTRY becomes its own
        # unresolved singleton (entry_against_open_position_same_key); a
        # later FULL_EXIT closes the original lifecycle. The verified
        # lifecycle's own closing exit is processed (and its build
        # finalized) after the second entry's singleton is already
        # finalized, but the verified lifecycle must still be returned
        # first, since its first member (the first ENTRY) chronologically
        # precedes the second ENTRY.
        first_entry = _snapshot(EVENT_TYPE_ENTRY)
        second_entry = _snapshot(EVENT_TYPE_ENTRY)
        closing_exit = _snapshot(EVENT_TYPE_FULL_EXIT, "ALL OUT")
        results = build_lifecycle_sequence([first_entry, second_entry, closing_exit])

        self.assertEqual(len(results), 2)
        closed, unresolved = results
        self.assertEqual(closed.status, STATUS_CLOSED)
        self.assertEqual(closed.opened_by_signal_id, first_entry.trade_signal_id)
        self.assertEqual(closed.closed_by_signal_id, closing_exit.trade_signal_id)
        self.assertEqual(
            closed.member_signal_ids,
            (first_entry.trade_signal_id, closing_exit.trade_signal_id),
        )
        self.assertEqual(unresolved.status, STATUS_UNRESOLVED)
        self.assertEqual(unresolved.member_signal_ids, (second_entry.trade_signal_id,))
        self.assertEqual(
            unresolved.ambiguity_flags, (FLAG_ENTRY_AGAINST_OPEN_POSITION_SAME_KEY,)
        )

    def test_general_ordering_and_coverage_invariant_over_mixed_generations(self):
        # A richer mixed sequence deliberately constructed so that
        # finalization order and chronological input order diverge in
        # more than one place, asserting the general invariants directly
        # rather than any one specific expected shape.
        entry1 = _snapshot(EVENT_TYPE_ENTRY)
        add1 = _snapshot(EVENT_TYPE_ADD)
        exit_close1 = _snapshot(EVENT_TYPE_FULL_EXIT, "ALL OUT")
        unmatched_orphan_start = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/4")
        add_no_active = _snapshot(EVENT_TYPE_ADD)
        entry2 = _snapshot(EVENT_TYPE_ROLL_UP)
        bad_fraction_exit = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/5")
        exit_close2 = _snapshot(EVENT_TYPE_FULL_EXIT, "ALL OUT")
        unmatched_final_orphan = _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/6")

        ordered_snapshots = [
            entry1, add1, exit_close1,
            unmatched_orphan_start, add_no_active, entry2,
            bad_fraction_exit, exit_close2,
            unmatched_final_orphan,
        ]
        results = build_lifecycle_sequence(ordered_snapshots)

        input_index = {
            snapshot.trade_signal_id: position
            for position, snapshot in enumerate(ordered_snapshots)
        }

        # Results are ordered by the minimum (== first, since members are
        # only ever appended in chronological order) input index of their
        # own member_signal_ids.
        first_member_indices = [
            input_index[build.member_signal_ids[0]] for build in results
        ]
        self.assertEqual(first_member_indices, sorted(first_member_indices))

        # No signal appears in more than one LifecycleBuild.
        seen_signal_ids = []
        for build in results:
            seen_signal_ids.extend(build.member_signal_ids)
        self.assertEqual(len(seen_signal_ids), len(set(seen_signal_ids)))

        # No input signal is missing.
        self.assertEqual(set(seen_signal_ids), set(input_index.keys()))

        # member_signal_ids within each build follow input order.
        for build in results:
            member_indices = [input_index[sid] for sid in build.member_signal_ids]
            self.assertEqual(member_indices, sorted(member_indices))

        # Confirms this fixture actually exercises a real divergence
        # between finalization order and input order (i.e. this test
        # would have failed before the ordering correction): the orphan
        # begun at unmatched_orphan_start only finalizes when entry2
        # later interrupts it, which happens after add_no_active was
        # already finalized as its own unresolved singleton - yet the
        # orphan's first member precedes add_no_active in input order and
        # must still be returned first.
        orphan_build = next(
            b for b in results
            if unmatched_orphan_start.trade_signal_id in b.member_signal_ids
        )
        add_build = next(
            b for b in results if add_no_active.trade_signal_id in b.member_signal_ids
        )
        self.assertLess(results.index(orphan_build), results.index(add_build))


class BuildLifecycleSequenceMultipleActiveGuardTests(unittest.TestCase):
    def test_second_entry_against_open_position_becomes_unresolved(self):
        entry1 = _snapshot(EVENT_TYPE_ENTRY)
        entry2 = _snapshot(EVENT_TYPE_ENTRY)
        results = build_lifecycle_sequence([entry1, entry2])

        self.assertEqual(len(results), 2)
        unresolved = next(b for b in results if b.status == STATUS_UNRESOLVED)
        self.assertEqual(unresolved.member_signal_ids, (entry2.trade_signal_id,))
        self.assertEqual(
            unresolved.ambiguity_flags, (FLAG_ENTRY_AGAINST_OPEN_POSITION_SAME_KEY,)
        )
        # The original open position is completely untouched.
        open_build = next(b for b in results if b.status == STATUS_OPEN)
        self.assertEqual(open_build.opened_by_signal_id, entry1.trade_signal_id)
        self.assertEqual(open_build.member_signal_ids, (entry1.trade_signal_id,))

    def test_roll_up_against_open_position_is_treated_identically_to_entry(self):
        entry = _snapshot(EVENT_TYPE_ENTRY)
        roll_up = _snapshot(EVENT_TYPE_ROLL_UP)
        results = build_lifecycle_sequence([entry, roll_up])

        unresolved = next(b for b in results if b.status == STATUS_UNRESOLVED)
        self.assertEqual(unresolved.member_signal_ids, (roll_up.trade_signal_id,))
        self.assertEqual(
            unresolved.ambiguity_flags, (FLAG_ENTRY_AGAINST_OPEN_POSITION_SAME_KEY,)
        )


class BuildLifecycleSequenceReEntryTests(unittest.TestCase):
    def test_stop_then_re_entry_produces_two_independent_closed_lifecycles(self):
        # Mirrors the real corpus's spacemonkey SPX 7430P case: a
        # stopped-out closed lifecycle, then a later re-entry at the
        # exact same key, never reopening the first.
        entry1 = _snapshot(EVENT_TYPE_ENTRY)
        stop_out = _snapshot(EVENT_TYPE_FULL_EXIT, "ALL OUT", notes="HIT STOP")
        entry2 = _snapshot(EVENT_TYPE_ROLL_UP)
        exit2 = _snapshot(EVENT_TYPE_FULL_EXIT, "ALL OUT")
        results = build_lifecycle_sequence([entry1, stop_out, entry2, exit2])

        self.assertEqual(len(results), 2)
        first, second = results
        self.assertEqual(first.status, STATUS_CLOSED)
        self.assertEqual(first.opened_by_signal_id, entry1.trade_signal_id)
        self.assertEqual(first.closed_by_signal_id, stop_out.trade_signal_id)
        self.assertEqual(second.status, STATUS_CLOSED)
        self.assertEqual(second.opened_by_signal_id, entry2.trade_signal_id)
        self.assertEqual(second.closed_by_signal_id, exit2.trade_signal_id)
        self.assertEqual(
            set(first.member_signal_ids) & set(second.member_signal_ids), set()
        )

    def test_stop_out_reason_lives_only_in_notes_not_in_status(self):
        # A stop-out is a FULL_EXIT whose reason is preserved verbatim in
        # notes - never a separate status/event type. This module has no
        # 'stopped' status at all.
        entry = _snapshot(EVENT_TYPE_ENTRY)
        stop_out = _snapshot(EVENT_TYPE_FULL_EXIT, "ALL OUT", notes="HIT STOP")
        results = build_lifecycle_sequence([entry, stop_out])

        self.assertEqual(results[0].status, STATUS_CLOSED)
        self.assertNotIn("stopped", results[0].status)


class BuildLifecycleSequenceUnrecognizedEventTypeTests(unittest.TestCase):
    def test_none_event_type_becomes_unresolved_singleton(self):
        snapshot = _snapshot(None)
        results = build_lifecycle_sequence([snapshot])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, STATUS_UNRESOLVED)
        self.assertEqual(
            results[0].ambiguity_flags, (FLAG_LIFECYCLE_EVENT_TYPE_UNRECOGNIZED,)
        )

    def test_unexpected_event_type_string_becomes_unresolved_singleton(self):
        snapshot = _snapshot("SOMETHING_ELSE")
        results = build_lifecycle_sequence([snapshot])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, STATUS_UNRESOLVED)
        self.assertEqual(
            results[0].ambiguity_flags, (FLAG_LIFECYCLE_EVENT_TYPE_UNRECOGNIZED,)
        )

    def test_never_raises_on_a_large_mixed_synthetic_sequence(self):
        snapshots = [
            _snapshot(EVENT_TYPE_ENTRY),
            _snapshot(EVENT_TYPE_ADD),
            _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/2"),
            _snapshot(None),
            _snapshot(EVENT_TYPE_PARTIAL_EXIT, "bogus"),
            _snapshot(EVENT_TYPE_FULL_EXIT, "ALL OUT"),
            _snapshot(EVENT_TYPE_ROLL_UP),
            _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/3"),
            _snapshot(EVENT_TYPE_ADD),
            _snapshot(EVENT_TYPE_FULL_EXIT, "ALL OUT"),
            _snapshot(EVENT_TYPE_PARTIAL_EXIT, "1/2"),  # trailing unmatched -> orphan
        ]
        try:
            results = build_lifecycle_sequence(snapshots)
        except Exception as exc:  # pragma: no cover - failure path only
            self.fail(f"build_lifecycle_sequence raised unexpectedly: {exc!r}")
        self.assertGreater(len(results), 0)
        for build in results:
            self.assertIn(
                build.status,
                {
                    STATUS_OPEN, STATUS_PARTIALLY_CLOSED, STATUS_CLOSED,
                    STATUS_ORPHAN, STATUS_UNRESOLVED, STATUS_INVALID,
                },
            )


class LifecycleBuildAndSnapshotModelTests(unittest.TestCase):
    def test_lifecycle_build_is_frozen(self):
        build = LifecycleBuild(
            status=STATUS_OPEN,
            remaining_fraction="1",
            opened_by_signal_id=1,
            closed_by_signal_id=None,
            member_signal_ids=(1,),
        )
        with self.assertRaises(Exception):
            build.status = STATUS_CLOSED

    def test_signal_snapshot_is_frozen(self):
        snapshot = _snapshot(EVENT_TYPE_ENTRY)
        with self.assertRaises(Exception):
            snapshot.symbol = "AAPL"

    def test_ambiguity_flags_default_to_empty_tuple_not_none(self):
        build = LifecycleBuild(
            status=STATUS_OPEN,
            remaining_fraction="1",
            opened_by_signal_id=1,
            closed_by_signal_id=None,
            member_signal_ids=(1,),
        )
        self.assertEqual(build.ambiguity_flags, ())
        self.assertIsNotNone(build.ambiguity_flags)


class ModuleVocabularyConsistencyTests(unittest.TestCase):
    def test_approved_event_types_match_r3_grammar_exactly(self):
        self.assertEqual(
            _APPROVED_EVENT_TYPES,
            {
                EVENT_TYPE_ENTRY, EVENT_TYPE_ADD, EVENT_TYPE_ROLL_UP,
                EVENT_TYPE_PARTIAL_EXIT, EVENT_TYPE_FULL_EXIT,
            },
        )

    def test_approved_fraction_tokens_match_extractor_grammar_exactly(self):
        self.assertEqual(
            set(_APPROVED_FRACTION_TOKENS.keys()),
            {"1/2", "1/3", "1/4", "1/6", "1/8", "1/16"},
        )

    def test_incomplete_contract_identity_flag_exported_with_approved_value(self):
        # R6.4 compatibility correction: repository/service code (R6.3/
        # R6.4) routes a signal with an incomplete option identity
        # (some but not all of option_type/strike/expiration populated)
        # to its own unresolved singleton before it ever reaches
        # build_lifecycle_sequence(). This flag names that outcome - it
        # is never raised by build_lifecycle_sequence() itself.
        self.assertEqual(FLAG_INCOMPLETE_CONTRACT_IDENTITY, "incomplete_contract_identity")


class PureModuleBoundaryTests(unittest.TestCase):
    """Recovery Milestone R6.2's core architectural requirement:
    database/lifecycle.py has no database access whatsoever. Mirrors
    tests.test_parser.ParseMessageSourceIndependenceTests' existing
    technique for app/parser.py's own source-independence guarantee."""

    def _module_lines(self):
        import database.lifecycle as lifecycle_module

        with open(lifecycle_module.__file__, "r", encoding="utf-8") as f:
            return f.readlines()

    def test_module_has_no_database_or_sqlite_imports(self):
        import_lines = [
            line
            for line in self._module_lines()
            if line.strip().startswith("import ") or line.strip().startswith("from ")
        ]
        self.assertTrue(import_lines, "expected at least the stdlib imports")
        for line in import_lines:
            self.assertNotIn("sqlite3", line)
            self.assertNotIn("database.repository", line)
            self.assertNotIn("database.service", line)
            self.assertNotIn("database.db", line)
            self.assertNotIn("app.streamlit_app", line)
            self.assertNotIn("streamlit", line)
            self.assertNotIn("requests", line)
            self.assertNotIn("socket", line)

    def test_module_docstring_declares_no_database_access(self):
        import database.lifecycle as lifecycle_module

        self.assertIn("no database access", lifecycle_module.__doc__.lower())


if __name__ == "__main__":
    unittest.main()
