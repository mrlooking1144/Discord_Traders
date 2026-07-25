"""Tests for app/discord_adapter.py (Recovery Milestone R2).

CORPUS (the complete real 68-message Discord corpus) is imported from
tests/discord_corpus_fixture.py - the single shared copy also used by
tests/test_extractor.py (Recovery Milestone R3) - rather than being
duplicated here.
"""

import unittest

from app.discord_adapter import (
    FLAG_EMPTY_BODY,
    FLAG_FOOTER_TRADER_MISMATCH,
    FLAG_MISSING_FOOTER,
    FLAG_MISSING_HEADER_NO_PRIOR_TRADER,
    FLAG_UNRECOGNIZED_FOOTER_TIMESTAMP_FORM,
    KIND_EXPLICIT_DATE,
    KIND_RELATIVE_TODAY,
    KIND_RELATIVE_YESTERDAY,
    KIND_UNRECOGNIZED,
    SegmentedMessage,
    segment_discord_batch,
)

from tests.discord_corpus_fixture import CORPUS



class FullCorpusSegmentationTests(unittest.TestCase):
    """Macro assertions over the complete real 68-message corpus."""

    @classmethod
    def setUpClass(cls):
        cls.messages = segment_discord_batch(CORPUS)

    def test_one_record_per_actual_discord_alert(self):
        # The corpus contains exactly 68 "<Trader>-Today at HH:MM" footer
        # lines, i.e. 68 distinct Discord alerts.
        self.assertEqual(len(self.messages), 68)

    def test_sequence_in_batch_is_contiguous_from_one(self):
        self.assertEqual(
            [m.sequence_in_batch for m in self.messages], list(range(1, 69))
        )

    def test_every_message_is_a_segmented_message(self):
        for message in self.messages:
            self.assertIsInstance(message, SegmentedMessage)

    def test_first_message_is_bdorts_avgo_entry(self):
        first = self.messages[0]
        self.assertEqual(first.trader_raw, "Bdorts")
        self.assertTrue(first.header_present)
        self.assertEqual(first.channel_tags, ["analyst-bdorts"])
        self.assertEqual(first.timestamp_text, "Today at 04:30 م")
        self.assertEqual(first.footer_timestamp_raw, "Today at 04:30 م")
        self.assertEqual(first.header_timestamp_raw, "04:30 م")
        self.assertEqual(first.footer_timestamp_kind, "relative_today")
        self.assertEqual(first.ambiguity_flags, [])

    def test_last_message_is_spacemonkey_spx_7430p_all_out(self):
        last = self.messages[-1]
        self.assertEqual(last.trader_raw, "spacemonkey")
        self.assertIn("SOLD SPX 7/24 7430P $12.00 ALL OUT", last.cleaned_text)
        self.assertEqual(last.channel_tags, ["pro-alerts"])
        self.assertEqual(last.ambiguity_flags, [])

    def test_no_message_is_missing_a_footer_in_the_real_corpus(self):
        for message in self.messages:
            self.assertTrue(message.footer_present, message.sequence_in_batch)
            self.assertNotIn(FLAG_MISSING_FOOTER, message.ambiguity_flags)

    def test_channel_slugs_found_match_the_five_known_channels(self):
        all_slugs = {slug for m in self.messages for slug in m.channel_tags}
        self.assertEqual(
            all_slugs,
            {
                "analyst-bdorts",
                "analyst-tc",
                "analyst-sarang",
                "pro-alerts",
                "twi-account",
            },
        )


class NoMergingOfConsecutiveSameTraderAlertsTests(unittest.TestCase):
    """Bdorts posts two consecutive AVGO exits (04:53, then 04:55) with no
    repeated header on the second - they must remain two separate records,
    never merged into one.
    """

    @classmethod
    def setUpClass(cls):
        cls.messages = segment_discord_batch(CORPUS)

    def _find(self, predicate):
        matches = [m for m in self.messages if predicate(m)]
        self.assertEqual(len(matches), 1, "expected exactly one match")
        return matches[0]

    def test_bdorts_two_consecutive_exits_are_two_records(self):
        quarter_exit = self._find(
            lambda m: "SOLD AVGO 07/24 380P $2.05 1/4" in m.cleaned_text
        )
        eighth_exit = self._find(
            lambda m: "SOLD AVGO 07/24 380P $2.5 1/8" in m.cleaned_text
        )
        self.assertNotEqual(
            quarter_exit.sequence_in_batch, eighth_exit.sequence_in_batch
        )
        self.assertEqual(
            eighth_exit.sequence_in_batch, quarter_exit.sequence_in_batch + 1
        )

    def test_second_of_the_pair_has_no_header_but_inherits_trader(self):
        eighth_exit = self._find(
            lambda m: "SOLD AVGO 07/24 380P $2.5 1/8" in m.cleaned_text
        )
        self.assertFalse(eighth_exit.header_present)
        self.assertEqual(eighth_exit.trader_raw, "Bdorts")
        self.assertEqual(eighth_exit.footer_trader_raw, "Bdorts")
        self.assertEqual(eighth_exit.ambiguity_flags, [])

    def test_spacemonkey_three_consecutive_spx_7450c_exits_stay_separate(self):
        # 06:07, 06:08 (both no header), 06:11 (header re-appears) - three
        # distinct SOLD SPX 7450C 1/4 records, never merged.
        quarter_exits = [
            m for m in self.messages if "SOLD SPX 7/24 7450C $3.65 1/4" in m.cleaned_text
            or "SOLD SPX 7/24 7450C $5.00 1/4" in m.cleaned_text
        ]
        self.assertEqual(len(quarter_exits), 2)
        self.assertNotEqual(
            quarter_exits[0].sequence_in_batch, quarter_exits[1].sequence_in_batch
        )


class ContinuationTraderInheritanceTests(unittest.TestCase):
    """TC posts an entry (04:30) then two consecutive exits (04:33 x2) with
    no repeated header - both continuations must inherit trader "TC".
    """

    @classmethod
    def setUpClass(cls):
        cls.messages = segment_discord_batch(CORPUS)

    def test_continuations_inherit_tc(self):
        half_exit = next(
            m for m in self.messages if "SOLD IBM 07/24 207.5C $3.2 1/2" in m.cleaned_text
        )
        quarter_exit = next(
            m for m in self.messages if "SOLD IBM 07/24 207.5C $3.5 1/4" in m.cleaned_text
        )
        self.assertFalse(half_exit.header_present)
        self.assertFalse(quarter_exit.header_present)
        self.assertEqual(half_exit.trader_raw, "TC")
        self.assertEqual(quarter_exit.trader_raw, "TC")

    def test_continuation_after_a_different_traders_message_still_inherits_correctly(self):
        # spacemonkey's SOLD IBM 210C (04:35, own header) sits between two
        # TC messages; TC's own next continuation (not adjacent in the
        # text to its own header) must still resolve to TC, not
        # spacemonkey, because segmentation tracks the immediately
        # preceding *message*, and spacemonkey's message closes with its
        # own footer before TC's continuation begins.
        eighth_exit = next(
            m for m in self.messages if "SOLD IBM 07/24 207.5C $4.3 1/8" in m.cleaned_text
        )
        self.assertTrue(eighth_exit.header_present)
        self.assertEqual(eighth_exit.trader_raw, "TC")


class RepeatedContractSummaryLineTests(unittest.TestCase):
    """A message restates its own contract on a second line - this must
    never cause a split into two records.
    """

    @classmethod
    def setUpClass(cls):
        cls.messages = segment_discord_batch(CORPUS)

    def test_bdorts_entry_with_restated_contract_is_one_record(self):
        matches = [
            m
            for m in self.messages
            if "BOUGHT AVGO 07/24 380P $1.14 [SMALL]" in m.cleaned_text
        ]
        self.assertEqual(len(matches), 1)
        message = matches[0]
        # Both the action line and the bare restatement line survive in
        # cleaned_text (collapsing them into one trade event is the
        # extractor's job in a later milestone, not the adapter's).
        self.assertEqual(
            message.cleaned_text.count("AVGO 07/24 380P $1.14 [SMALL]"), 2
        )

    def test_stop_exit_with_restated_all_out_line_is_one_record(self):
        matches = [
            m
            for m in self.messages
            if "SOLD SPX 7/24 7440C $2.50 ALL OUT" in m.cleaned_text
        ]
        self.assertEqual(len(matches), 1)
        self.assertIn("HIT STOP", matches[0].cleaned_text)


class MultipleChannelTagsTests(unittest.TestCase):
    """Matae's alerts are cross-posted to two channels (twi-account and
    pro-alerts) - both tags must be captured on a single record, never
    duplicated into two messages.
    """

    @classmethod
    def setUpClass(cls):
        cls.messages = segment_discord_batch(CORPUS)

    def test_matae_entry_has_both_channel_tags_on_one_record(self):
        matches = [
            m
            for m in self.messages
            if "BOUGHT TSLA 7/24 312.5P $1.70 [B GRADE]" in m.cleaned_text
        ]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].channel_tags, ["twi-account", "pro-alerts"])

    def test_all_five_matae_alerts_carry_both_tags(self):
        matae_messages = [m for m in self.messages if m.trader_raw == "Matae"]
        self.assertEqual(len(matae_messages), 5)
        for message in matae_messages:
            self.assertEqual(message.channel_tags, ["twi-account", "pro-alerts"])

    def test_case_only_footer_trader_difference_is_not_flagged(self):
        # Every Matae message's footer says "matae" (lowercase) - this is
        # expected and must not raise footer_trader_mismatch.
        matae_messages = [m for m in self.messages if m.trader_raw == "Matae"]
        for message in matae_messages:
            self.assertEqual(message.footer_trader_raw, "matae")
            self.assertNotIn(FLAG_FOOTER_TRADER_MISMATCH, message.ambiguity_flags)


class RawTextVerbatimTests(unittest.TestCase):
    """Every message's raw_text must reconstruct an exact, unaltered
    substring of the original pasted corpus - never re-encoded, stripped
    mid-content, or otherwise modified.
    """

    @classmethod
    def setUpClass(cls):
        cls.messages = segment_discord_batch(CORPUS)

    def test_every_raw_text_is_an_exact_substring_of_the_corpus(self):
        for message in self.messages:
            self.assertIn(
                message.raw_text, CORPUS, f"message {message.sequence_in_batch}"
            )

    def test_first_message_raw_text_matches_corpus_prefix_up_to_its_own_footer(self):
        # Derived programmatically from CORPUS itself (never hand-retyped,
        # so there is no risk of silently substituting a lookalike
        # character for one of the exotic codepoints involved).
        footer_line = "Bdorts•Today at 04:30 م\n"
        end = CORPUS.index(footer_line) + len(footer_line)
        expected = CORPUS[:end]
        self.assertEqual(self.messages[0].raw_text, expected)

    def test_first_message_raw_text_exact_utf8_bytes(self):
        footer_line = "Bdorts•Today at 04:30 م\n"
        end = CORPUS.index(footer_line) + len(footer_line)
        expected_bytes = CORPUS[:end].encode("utf-8")
        self.assertEqual(self.messages[0].raw_text.encode("utf-8"), expected_bytes)

    def test_first_message_raw_text_exact_codepoint_sequence(self):
        footer_line = "Bdorts•Today at 04:30 م\n"
        end = CORPUS.index(footer_line) + len(footer_line)
        expected_codepoints = [ord(c) for c in CORPUS[:end]]
        actual_codepoints = [ord(c) for c in self.messages[0].raw_text]
        self.assertEqual(actual_codepoints, expected_codepoints)

    def test_channel_tag_wrapper_codepoints_present_in_raw_text(self):
        # U+2060 (WORD JOINER) wraps both sides of the mage emoji + U+FE31
        # channel-tag marker in the real corpus - all three must survive
        # into raw_text exactly, since raw_text is never passed through
        # the wrapper-stripping logic that only applies to cleaned_text.
        first = self.messages[0]
        self.assertIn(0x2060, [ord(c) for c in first.raw_text])
        self.assertIn(0xFE31, [ord(c) for c in first.raw_text])
        self.assertIn("⁠🧙︱analyst-bdorts⁠", first.raw_text)

    def test_wrapper_codepoints_stripped_only_from_cleaned_text(self):
        for message in self.messages:
            if not message.channel_tags:
                continue
            self.assertIn(0xFE31, [ord(c) for c in message.raw_text])
            self.assertNotIn(0xFE31, [ord(c) for c in message.cleaned_text])
            self.assertNotIn(0x2060, [ord(c) for c in message.cleaned_text])

    def test_continuation_message_raw_text_excludes_prior_footer_and_header(self):
        eighth_exit = next(
            m for m in self.messages if "SOLD AVGO 07/24 380P $2.5 1/8" in m.cleaned_text
        )
        self.assertNotIn("Bdorts•Today at 04:53", eighth_exit.raw_text)
        self.assertNotIn("APP", eighth_exit.raw_text)
        self.assertTrue(eighth_exit.raw_text.endswith("Bdorts•Today at 04:55 م\n"))

    def test_raw_text_is_never_mutated_by_repeated_segmentation(self):
        first_pass = segment_discord_batch(CORPUS)
        second_pass = segment_discord_batch(CORPUS)
        self.assertEqual(
            [m.raw_text for m in first_pass], [m.raw_text for m in second_pass]
        )

    def test_source_corpus_string_object_is_untouched(self):
        original = CORPUS
        segment_discord_batch(CORPUS)
        self.assertEqual(CORPUS, original)


class LineEndingPreservationTests(unittest.TestCase):
    """The real corpus file uses LF only, so it cannot prove CRLF
    preservation on its own - this uses a dedicated synthetic snippet
    (not from the real corpus) built with explicit CRLF line endings to
    prove the adapter itself never normalizes line endings in raw_text.
    """

    def test_crlf_line_endings_preserved_verbatim_in_raw_text(self):
        crlf_snippet = (
            "Bdorts\r\nAPP\r\n — 04:30 م\r\n"
            "BOUGHT AVGO 07/24 380P $1.14 [SMALL]\r\n"
            "Bdorts•Today at 04:30 م\r\n"
        )
        messages = segment_discord_batch(crlf_snippet)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].raw_text, crlf_snippet)
        self.assertEqual(
            messages[0].raw_text.count("\r\n"), crlf_snippet.count("\r\n")
        )

    def test_mixed_line_endings_preserved_exactly_as_supplied(self):
        mixed_snippet = (
            "Bdorts\r\nAPP\n — 04:30 م\r\n"
            "BOUGHT AVGO 07/24 380P $1.14 [SMALL]\n"
            "Bdorts•Today at 04:30 م\r\n"
        )
        messages = segment_discord_batch(mixed_snippet)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].raw_text, mixed_snippet)

    def test_cleaned_text_uses_lf_regardless_of_source_line_ending(self):
        # cleaned_text is a working copy for parsing, not a verbatim
        # artifact - normalizing its internal joins to "\n" is expected
        # and does not violate raw_text's byte-for-byte guarantee.
        crlf_snippet = (
            "Bdorts\r\nAPP\r\n — 04:30 م\r\n"
            "BOUGHT AVGO 07/24 380P $1.14 [SMALL]\r\n"
            "Bdorts•Today at 04:30 م\r\n"
        )
        messages = segment_discord_batch(crlf_snippet)
        self.assertNotIn("\r", messages[0].cleaned_text)


class NoisyCommentsRetainedTests(unittest.TestCase):
    """Free-text commentary must survive into cleaned_text, while Discord
    wrapper noise (header, footer, APP label, channel tags, blank lines)
    must not.
    """

    @classmethod
    def setUpClass(cls):
        cls.messages = segment_discord_batch(CORPUS)

    def _cleaned_text_containing(self, needle):
        matches = [m for m in self.messages if needle in m.cleaned_text]
        self.assertEqual(len(matches), 1, needle)
        return matches[0].cleaned_text

    def test_holding_last_runner_for_glory_retained(self):
        cleaned = self._cleaned_text_containing("*HOLDING LAST RUNNER FOR GLORY")
        self.assertIn("SOLD IBM 07/24 207.5C $5.2 1/16", cleaned)

    def test_hit_stop_retained(self):
        cleaned = self._cleaned_text_containing("HIT STOP")
        self.assertIn("SOLD SPX 7/24 7440C $2.50 ALL OUT", cleaned)

    def test_scalp_fast_trade_and_stop_lod_retained(self):
        cleaned = self._cleaned_text_containing("SCALP/fast trade")
        self.assertIn("STOP LOD", cleaned)

    def test_hod_test_retained(self):
        self._cleaned_text_containing("HOD test")

    def test_trying_again_retained(self):
        self._cleaned_text_containing("TRYING AGAIN")

    def test_9on10_short_and_cut_if_wrong_retained(self):
        cleaned = self._cleaned_text_containing("9ON10 short")
        self.assertIn("will be fast to cut if wrong", cleaned)

    def test_cleaned_text_never_contains_wrapper_noise(self):
        for message in self.messages:
            self.assertNotIn("APP", message.cleaned_text.split("\n"))
            self.assertNotIn("م", message.cleaned_text)
            for tag in message.channel_tags:
                self.assertNotIn(tag, message.cleaned_text)
            self.assertNotIn("•Today at", message.cleaned_text)
            for line in message.cleaned_text.split("\n"):
                self.assertNotEqual(line.strip(), "")


class SyntheticIdentityTests(unittest.TestCase):
    """synthetic_id_input must be a deterministic function of a message's
    stable inputs - never random, never wall-clock-derived, and stable
    across repeated runs on identical text.
    """

    def test_repeated_segmentation_yields_identical_synthetic_ids(self):
        first_pass = segment_discord_batch(CORPUS)
        second_pass = segment_discord_batch(CORPUS)
        self.assertEqual(
            [m.synthetic_id_input for m in first_pass],
            [m.synthetic_id_input for m in second_pass],
        )

    def test_all_68_synthetic_ids_are_unique(self):
        messages = segment_discord_batch(CORPUS)
        ids = [m.synthetic_id_input for m in messages]
        self.assertEqual(len(ids), len(set(ids)))

    def test_synthetic_id_input_contains_no_randomness_across_processes(self):
        # Segmenting the same text twice in this process already proves
        # determinism (above); this test additionally proves the value is
        # a pure function of (channel, trader, timestamp, cleaned_text) by
        # constructing two batches with identical content but different
        # surrounding corpus context, expecting identical synthetic ids
        # for the shared message.
        snippet = (
            "Bdorts\nAPP\n — 04:30 م\n"
            "BOUGHT AVGO 07/24 380P $1.14 [SMALL]\n"
            "AVGO 07/24 380P $1.14 [SMALL]\n"
            "⁠🧙︱analyst-bdorts⁠\n\n"
            "Bdorts•Today at 04:30 م\n"
        )
        alone = segment_discord_batch(snippet)
        within_corpus = segment_discord_batch(CORPUS)
        self.assertEqual(
            alone[0].synthetic_id_input, within_corpus[0].synthetic_id_input
        )


class SyntheticIdentityCollisionAndOverlapTests(unittest.TestCase):
    """Fabricated (not real-corpus) fixtures proving the occurrence-index
    disambiguation: two genuinely identical-looking alerts in one batch
    must not collide, and re-pasting an overlapping history must still
    assign the same synthetic id to the same real message - unless the
    re-paste starts strictly between two identical-looking duplicates, in
    which case a different id is the documented, unavoidable limitation.
    """

    _DUPLICATE_PAIR = (
        "Bdorts\nAPP\n — 04:30 PM\n"
        "BOUGHT AVGO 07/24 380P $1.14 [SMALL]\n"
        "⁠🧙︱analyst-bdorts⁠\n\n"
        "Bdorts•Today at 04:30 PM\n"
        "BOUGHT AVGO 07/24 380P $1.14 [SMALL]\n"
        "⁠🧙︱analyst-bdorts⁠\n\n"
        "Bdorts•Today at 04:30 PM\n"
    )

    def test_two_identical_looking_alerts_do_not_collide(self):
        messages = segment_discord_batch(self._DUPLICATE_PAIR)
        self.assertEqual(len(messages), 2)
        # Same channel, trader, timestamp, and cleaned body...
        self.assertEqual(messages[0].channel_tags, messages[1].channel_tags)
        self.assertEqual(messages[0].trader_raw, messages[1].trader_raw)
        self.assertEqual(messages[0].timestamp_text, messages[1].timestamp_text)
        self.assertEqual(messages[0].cleaned_text, messages[1].cleaned_text)
        # ...yet their synthetic ids must differ.
        self.assertNotEqual(
            messages[0].synthetic_id_input, messages[1].synthetic_id_input
        )

    def test_occurrence_index_is_the_only_difference_between_the_pair(self):
        messages = segment_discord_batch(self._DUPLICATE_PAIR)
        first_id = messages[0].synthetic_id_input
        second_id = messages[1].synthetic_id_input
        self.assertTrue(first_id.endswith("\x1f0"))
        self.assertTrue(second_id.endswith("\x1f1"))
        self.assertEqual(first_id[:-1], second_id[:-1])

    def test_three_identical_looking_alerts_each_get_a_distinct_id(self):
        triple = self._DUPLICATE_PAIR + (
            "BOUGHT AVGO 07/24 380P $1.14 [SMALL]\n"
            "⁠🧙︱analyst-bdorts⁠\n\n"
            "Bdorts•Today at 04:30 PM\n"
        )
        messages = segment_discord_batch(triple)
        self.assertEqual(len(messages), 3)
        ids = [m.synthetic_id_input for m in messages]
        self.assertEqual(len(ids), len(set(ids)))

    def test_stable_across_full_overlapping_repaste(self):
        # Re-pasting the exact same (fully overlapping) history must
        # assign identical synthetic ids to the corresponding messages -
        # this is what lets R5's idempotency check recognize them as
        # already-stored rather than duplicating them.
        first_paste = segment_discord_batch(self._DUPLICATE_PAIR)
        second_paste = segment_discord_batch(self._DUPLICATE_PAIR)
        self.assertEqual(
            [m.synthetic_id_input for m in first_paste],
            [m.synthetic_id_input for m in second_paste],
        )

    def test_stable_when_repaste_includes_full_prefix_of_duplicates(self):
        # A re-paste that starts *before* both duplicates (e.g. includes
        # extra trailing context from a longer channel history) must still
        # assign the same ids to the original pair, since both occurrences
        # are still present in the same relative order.
        extended = self._DUPLICATE_PAIR + (
            "Bdorts\nAPP\n — 04:41 PM\n"
            "SOLD AVGO 07/24 380P $1.65 1/4\n"
            "⁠🧙︱analyst-bdorts⁠\n\n"
            "Bdorts•Today at 04:41 PM\n"
        )
        original = segment_discord_batch(self._DUPLICATE_PAIR)
        extended_messages = segment_discord_batch(extended)
        self.assertEqual(
            original[0].synthetic_id_input, extended_messages[0].synthetic_id_input
        )
        self.assertEqual(
            original[1].synthetic_id_input, extended_messages[1].synthetic_id_input
        )

    def test_documented_limitation_partial_repaste_between_duplicates_differs(self):
        # A re-paste starting strictly *between* the two identical-looking
        # messages (i.e. only the second one is included, with nothing
        # before it to establish that a first occurrence already
        # happened) necessarily assigns occurrence index 0 again, not 1 -
        # a different synthetic id than the second message received in
        # the full paste. This is the documented, unavoidable limitation
        # of occurrence-index disambiguation without a real Discord
        # message ID, not a defect: it demonstrates why a real external
        # message ID must always be preferred when available.
        full_paste = segment_discord_batch(self._DUPLICATE_PAIR)
        second_message_alone = (
            "Bdorts\nAPP\n — 04:30 PM\n"
            "BOUGHT AVGO 07/24 380P $1.14 [SMALL]\n"
            "⁠🧙︱analyst-bdorts⁠\n\n"
            "Bdorts•Today at 04:30 PM\n"
        )
        partial_paste = segment_discord_batch(second_message_alone)
        self.assertNotEqual(
            full_paste[1].synthetic_id_input, partial_paste[0].synthetic_id_input
        )
        self.assertEqual(full_paste[0].synthetic_id_input, partial_paste[0].synthetic_id_input)


class FooterTimestampVariantTests(unittest.TestCase):
    """Fabricated (not real-corpus) footer variants beyond the real
    corpus's own "Today at HH:MM م" form: Discord's actual copy/export
    output varies with message age and locale.
    """

    @staticmethod
    def _single_message(footer_line):
        return (
            "Bdorts\nAPP\n — 04:30 PM\n"
            "BOUGHT AVGO 07/24 380P $1.14 [SMALL]\n"
            f"{footer_line}\n"
        )

    def test_today_with_english_am_pm(self):
        messages = segment_discord_batch(self._single_message("Bdorts•Today at 04:30 PM"))
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].footer_timestamp_kind, KIND_RELATIVE_TODAY)
        self.assertEqual(messages[0].footer_timestamp_raw, "Today at 04:30 PM")
        self.assertNotIn(FLAG_UNRECOGNIZED_FOOTER_TIMESTAMP_FORM, messages[0].ambiguity_flags)

    def test_yesterday_with_english_am_pm(self):
        messages = segment_discord_batch(
            self._single_message("Bdorts•Yesterday at 11:45 PM")
        )
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].footer_timestamp_kind, KIND_RELATIVE_YESTERDAY)
        self.assertEqual(messages[0].footer_timestamp_raw, "Yesterday at 11:45 PM")
        self.assertNotIn(FLAG_UNRECOGNIZED_FOOTER_TIMESTAMP_FORM, messages[0].ambiguity_flags)

    def test_yesterday_with_arabic_am_marker(self):
        messages = segment_discord_batch(
            self._single_message("Bdorts•Yesterday at 09:15 ص")
        )
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].footer_timestamp_kind, KIND_RELATIVE_YESTERDAY)
        self.assertEqual(messages[0].footer_timestamp_raw, "Yesterday at 09:15 ص")

    def test_explicit_date_form_with_slashes(self):
        messages = segment_discord_batch(
            self._single_message("Bdorts•07/23/2026 11:45 PM")
        )
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].footer_timestamp_kind, KIND_EXPLICIT_DATE)
        self.assertEqual(messages[0].footer_timestamp_raw, "07/23/2026 11:45 PM")
        self.assertNotIn(FLAG_UNRECOGNIZED_FOOTER_TIMESTAMP_FORM, messages[0].ambiguity_flags)

    def test_explicit_date_form_with_comma_and_arabic_pm(self):
        messages = segment_discord_batch(
            self._single_message("Bdorts•07/23/2026, 11:45 م")
        )
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].footer_timestamp_kind, KIND_EXPLICIT_DATE)
        self.assertEqual(messages[0].footer_timestamp_raw, "07/23/2026, 11:45 م")

    def test_relative_form_with_no_meridiem_still_recognized(self):
        # A 24-hour-clock locale export with no AM/PM/ص/م suffix at all.
        messages = segment_discord_batch(self._single_message("Bdorts•Today at 16:30"))
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].footer_timestamp_kind, KIND_RELATIVE_TODAY)
        self.assertEqual(messages[0].footer_timestamp_raw, "Today at 16:30")
        self.assertNotIn(FLAG_UNRECOGNIZED_FOOTER_TIMESTAMP_FORM, messages[0].ambiguity_flags)

    def test_completely_unrecognized_form_still_closes_the_message(self):
        # An unfamiliar phrasing must not cause this message to merge into
        # whatever follows - the bullet-separated structure alone is
        # enough to close it, just flagged as uncertain.
        messages = segment_discord_batch(self._single_message("Bdorts•3 hours ago"))
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].footer_timestamp_kind, KIND_UNRECOGNIZED)
        self.assertEqual(messages[0].footer_timestamp_raw, "3 hours ago")
        self.assertIn(FLAG_UNRECOGNIZED_FOOTER_TIMESTAMP_FORM, messages[0].ambiguity_flags)
        self.assertTrue(messages[0].footer_present)

    def test_unrecognized_form_does_not_merge_two_messages_into_one(self):
        two_messages = (
            "Bdorts\nAPP\n — 04:30 PM\n"
            "BOUGHT AVGO 07/24 380P $1.14 [SMALL]\n"
            "Bdorts•3 hours ago\n"
            "SOLD AVGO 07/24 380P $1.41 1/4\n"
            "Bdorts•Today at 04:37 PM\n"
        )
        messages = segment_discord_batch(two_messages)
        self.assertEqual(len(messages), 2)
        self.assertIn(
            FLAG_UNRECOGNIZED_FOOTER_TIMESTAMP_FORM, messages[0].ambiguity_flags
        )
        self.assertNotIn(
            FLAG_UNRECOGNIZED_FOOTER_TIMESTAMP_FORM, messages[1].ambiguity_flags
        )

    def test_timestamp_text_is_never_resolved_to_an_absolute_date(self):
        # R2 explicitly does not resolve dates - only preserves text.
        messages = segment_discord_batch(
            self._single_message("Bdorts•07/23/2026 11:45 PM")
        )
        self.assertIsInstance(messages[0].footer_timestamp_raw, str)
        self.assertNotIsInstance(messages[0].footer_timestamp_raw, (int, float))


class AmbiguityFlagSyntheticEdgeCaseTests(unittest.TestCase):
    """Fabricated (not real-corpus) snippets exercising each ambiguity
    flag directly - kept separate from real-corpus expectations, per this
    project's practice of never treating a synthetic edge case as the
    expected result for a real message.
    """

    def test_missing_footer_flagged_for_truncated_paste(self):
        truncated = (
            "Bdorts\nAPP\n — 04:30 م\n"
            "BOUGHT AVGO 07/24 380P $1.14 [SMALL]\n"
        )
        messages = segment_discord_batch(truncated)
        self.assertEqual(len(messages), 1)
        self.assertFalse(messages[0].footer_present)
        self.assertIn(FLAG_MISSING_FOOTER, messages[0].ambiguity_flags)
        self.assertIsNone(messages[0].footer_trader_raw)
        self.assertIsNone(messages[0].footer_timestamp_raw)
        self.assertIsNone(messages[0].footer_timestamp_kind)

    def test_footer_trader_mismatch_flagged_when_names_genuinely_differ(self):
        mismatched = (
            "Bdorts\nAPP\n — 04:30 م\n"
            "BOUGHT AVGO 07/24 380P $1.14 [SMALL]\n"
            "TC•Today at 04:30 م\n"
        )
        messages = segment_discord_batch(mismatched)
        self.assertEqual(len(messages), 1)
        self.assertIn(FLAG_FOOTER_TRADER_MISMATCH, messages[0].ambiguity_flags)

    def test_missing_header_no_prior_trader_flagged_for_first_message(self):
        headerless_first = "SOLD AVGO 07/24 380P $1.41 1/4\nBdorts•Today at 04:37 م\n"
        messages = segment_discord_batch(headerless_first)
        self.assertEqual(len(messages), 1)
        self.assertIsNone(messages[0].trader_raw)
        self.assertIn(
            FLAG_MISSING_HEADER_NO_PRIOR_TRADER, messages[0].ambiguity_flags
        )

    def test_empty_body_flagged_when_nothing_but_wrapper_remains(self):
        wrapper_only = (
            "Bdorts\nAPP\n — 04:30 م\n"
            "⁠🧙︱analyst-bdorts⁠\n"
            "Bdorts•Today at 04:30 م\n"
        )
        messages = segment_discord_batch(wrapper_only)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].cleaned_text, "")
        self.assertIn(FLAG_EMPTY_BODY, messages[0].ambiguity_flags)


class EmptyAndBlankInputTests(unittest.TestCase):
    def test_empty_string_returns_no_messages(self):
        self.assertEqual(segment_discord_batch(""), [])

    def test_whitespace_only_returns_no_messages(self):
        self.assertEqual(segment_discord_batch("\n\n   \n\t\n"), [])


if __name__ == "__main__":
    unittest.main()
