"""Tests for app/parser.py's extract_trade_event() (Recovery Milestone R3).

Covers the extractor grammar extension: converting one Discord alert's
cleaned_text (as produced by app/discord_adapter.segment_discord_batch())
into a structured trade-event dict. tests/test_parser.py (parse_message())
is left completely untouched - this file only imports extract_trade_event
and the new PARSE_STATUS_*/FLAG_* constants from app.parser.

app/parser.py itself never imports app.discord_adapter - only this test
file combines the two, exactly as intended by the R3 scope. The real
68-message corpus is imported from tests/discord_corpus_fixture.py, the
single shared copy also used by tests/test_discord_adapter.py, so it is
never independently duplicated.
"""

import unittest
from decimal import Decimal

from app.discord_adapter import segment_discord_batch
from app.parser import (
    FLAG_ACTION_QUALIFIER_CONFLICT,
    FLAG_EXPIRATION_YEAR_MISSING,
    FLAG_QUALIFIER_MISSING,
    FLAG_STATED_RETURN_MISSING,
    FLAG_STRIKE_MISSING,
    PARSE_STATUS_FAILED,
    PARSE_STATUS_PARSED,
    PARSE_STATUS_PARTIALLY_PARSED,
    PARSE_STATUS_UNRECOGNIZED,
    PARSER_VERSION,
    extract_trade_event,
)
from tests.discord_corpus_fixture import CORPUS

_RESULT_KEYS = {
    "symbol",
    "action",
    "option_type",
    "price",
    "expiration",
    "position_size",
    "strike",
    "expiration_raw",
    "event_type",
    "qualifier",
    "stated_entry_price",
    "stated_return_pct",
    "notes",
    "parse_status",
    "ambiguity_flags",
}


class ExtractTradeEventBoughtSoldTests(unittest.TestCase):
    def test_bought_alert_extracted(self):
        result = extract_trade_event("BOUGHT AVGO 07/24 380P $1.14 [SMALL]")
        self.assertEqual(result["action"], "BOUGHT")
        self.assertEqual(result["symbol"], "AVGO")
        self.assertEqual(result["parse_status"], PARSE_STATUS_PARSED)

    def test_sold_alert_extracted(self):
        result = extract_trade_event("SOLD IBM 07/24 207.5C $3.2 1/2")
        self.assertEqual(result["action"], "SOLD")
        self.assertEqual(result["symbol"], "IBM")
        self.assertEqual(result["parse_status"], PARSE_STATUS_PARSED)

    def test_bought_and_sold_never_aliased_to_bto_stc(self):
        bought = extract_trade_event("BOUGHT SPY 450C $3.25")
        sold = extract_trade_event("SOLD SPY 450C $3.25 ALL OUT")
        self.assertEqual(bought["action"], "BOUGHT")
        self.assertEqual(sold["action"], "SOLD")
        self.assertNotIn(bought["action"], ("BTO",))
        self.assertNotIn(sold["action"], ("STC", "BTC", "STO"))

    def test_legacy_verbs_still_recognized_and_not_aliased(self):
        for verb in ("BTO", "STC", "BTC", "STO", "BUY", "SELL"):
            result = extract_trade_event(f"{verb} SPY 450C $3.25")
            self.assertEqual(result["action"], verb)


class ExtractTradeEventOpenCloseLifecycleTests(unittest.TestCase):
    """STO opens a short and BTC closes a short - neither is aliased to the
    long-position BTO/STC wording, but each must still be classified by its
    opening/closing role, not by whether its verb superficially reads as a
    "sell" or a "buy". An opening verb (BTO/BUY/BOUGHT/STO) combined with a
    closing-only qualifier ("ALL OUT" or a fraction) is contradictory - it
    must never be guessed into ENTRY/ADD/ROLL_UP/PARTIAL_EXIT/FULL_EXIT, and
    is instead preserved for review via event_type=None plus
    FLAG_ACTION_QUALIFIER_CONFLICT.
    """

    def test_sto_all_out_is_preserved_as_ambiguous_not_an_exit(self):
        # Before the fix, STO fell into the same bucket as SOLD/STC/BTC and
        # "ALL OUT" would have produced FULL_EXIT - silently misreporting a
        # new short position as a full exit. After the fix it must not be
        # silently guessed into ENTRY either.
        result = extract_trade_event("STO SPY 450C $3.25 ALL OUT")
        self.assertIsNone(result["event_type"])
        self.assertIn(FLAG_ACTION_QUALIFIER_CONFLICT, result["ambiguity_flags"])

    def test_sto_fraction_is_preserved_as_ambiguous_not_an_exit(self):
        result = extract_trade_event("STO SPY 450C $3.25 1/2")
        self.assertIsNone(result["event_type"])
        self.assertIn(FLAG_ACTION_QUALIFIER_CONFLICT, result["ambiguity_flags"])

    def test_bto_all_out_is_preserved_as_ambiguous(self):
        # The same contradiction applies to every opening verb, not just
        # STO - BTO/BUY/BOUGHT + ALL OUT is equally nonsensical.
        result = extract_trade_event("BTO SPY 450C $3.25 ALL OUT")
        self.assertIsNone(result["event_type"])
        self.assertIn(FLAG_ACTION_QUALIFIER_CONFLICT, result["ambiguity_flags"])

    def test_bought_all_out_is_preserved_as_ambiguous(self):
        result = extract_trade_event("BOUGHT SPY 450C $3.25 ALL OUT")
        self.assertIsNone(result["event_type"])
        self.assertIn(FLAG_ACTION_QUALIFIER_CONFLICT, result["ambiguity_flags"])

    def test_buy_fraction_is_preserved_as_ambiguous(self):
        result = extract_trade_event("BUY SPY 450C $3.25 1/4")
        self.assertIsNone(result["event_type"])
        self.assertIn(FLAG_ACTION_QUALIFIER_CONFLICT, result["ambiguity_flags"])

    def test_contradictory_qualifier_and_other_fields_are_not_discarded(self):
        result = extract_trade_event(
            "STO SPY 450C $3.25 ALL OUT\nHIT STOP"
        )
        self.assertEqual(result["action"], "STO")
        self.assertEqual(result["symbol"], "SPY")
        self.assertEqual(result["strike"], Decimal("450"))
        self.assertEqual(result["option_type"], "call")
        self.assertEqual(result["price"], Decimal("3.25"))
        self.assertEqual(result["qualifier"], "ALL OUT")
        self.assertEqual(result["notes"], "HIT STOP")
        self.assertIsNone(result["event_type"])

    def test_contradictory_alert_still_reaches_parsed_status(self):
        # A conflicting qualifier is not a parse failure - action, symbol,
        # and price were all found, so parse_status follows the normal
        # R3 contract and is not demoted just because event_type is None.
        result = extract_trade_event("STO SPY 450C $3.25 ALL OUT")
        self.assertEqual(result["parse_status"], PARSE_STATUS_PARSED)
        self.assertNotIn(FLAG_QUALIFIER_MISSING, result["ambiguity_flags"])

    def test_sto_with_no_annotation_is_entry(self):
        result = extract_trade_event("STO SPY 450C $3.25")
        self.assertEqual(result["event_type"], "ENTRY")
        self.assertNotIn(FLAG_ACTION_QUALIFIER_CONFLICT, result["ambiguity_flags"])

    def test_sto_with_add_annotation_is_add(self):
        result = extract_trade_event("STO SPY 450C $3.25 [ADD]")
        self.assertEqual(result["event_type"], "ADD")
        self.assertNotIn(FLAG_ACTION_QUALIFIER_CONFLICT, result["ambiguity_flags"])

    def test_sto_with_roll_up_annotation_is_roll_up(self):
        result = extract_trade_event("STO SPY 450C $3.25 [ROLL UP]")
        self.assertEqual(result["event_type"], "ROLL_UP")
        self.assertNotIn(FLAG_ACTION_QUALIFIER_CONFLICT, result["ambiguity_flags"])

    def test_btc_with_all_out_is_full_exit(self):
        result = extract_trade_event("BTC SPY 450C $3.25 ALL OUT")
        self.assertEqual(result["event_type"], "FULL_EXIT")
        self.assertNotIn(FLAG_ACTION_QUALIFIER_CONFLICT, result["ambiguity_flags"])

    def test_btc_with_fraction_is_partial_exit(self):
        result = extract_trade_event("BTC SPY 450C $3.25 1/2")
        self.assertEqual(result["event_type"], "PARTIAL_EXIT")
        self.assertNotIn(FLAG_ACTION_QUALIFIER_CONFLICT, result["ambiguity_flags"])

    def test_btc_is_never_treated_as_an_entry(self):
        result = extract_trade_event("BTC SPY 450C $3.25 ALL OUT")
        self.assertNotEqual(result["event_type"], "ENTRY")
        self.assertNotEqual(result["event_type"], "ADD")
        self.assertNotEqual(result["event_type"], "ROLL_UP")

    def test_stc_sold_exit_behavior_unaffected_by_sto_fix(self):
        stc_full = extract_trade_event("STC SPY 450C $3.25 ALL OUT")
        stc_partial = extract_trade_event("STC SPY 450C $3.25 1/4")
        sold_full = extract_trade_event("SOLD SPY 450C $3.25 ALL OUT")
        self.assertEqual(stc_full["event_type"], "FULL_EXIT")
        self.assertEqual(stc_partial["event_type"], "PARTIAL_EXIT")
        self.assertEqual(sold_full["event_type"], "FULL_EXIT")

    def test_bto_buy_bought_entry_behavior_unaffected_by_sto_fix(self):
        bto = extract_trade_event("BTO SPY 450C $3.25")
        buy = extract_trade_event("BUY SPY 450C $3.25 [ADD]")
        bought = extract_trade_event("BOUGHT SPY 450C $3.25 [ROLL UP]")
        self.assertEqual(bto["event_type"], "ENTRY")
        self.assertEqual(buy["event_type"], "ADD")
        self.assertEqual(bought["event_type"], "ROLL_UP")


class ExtractTradeEventContractTests(unittest.TestCase):
    def test_call_contract_letter_form(self):
        result = extract_trade_event("BOUGHT SPY 450C $3.25")
        self.assertEqual(result["option_type"], "call")
        self.assertEqual(result["strike"], Decimal("450"))

    def test_put_contract_letter_form(self):
        result = extract_trade_event("BOUGHT SPY 450P $3.25")
        self.assertEqual(result["option_type"], "put")
        self.assertEqual(result["strike"], Decimal("450"))

    def test_call_put_word_form(self):
        call_result = extract_trade_event("BOUGHT SPY CALL $3.25")
        self.assertEqual(call_result["option_type"], "call")
        put_result = extract_trade_event("SOLD SPY PUT $3.25 ALL OUT")
        self.assertEqual(put_result["option_type"], "put")

    def test_decimal_strike_captured_not_discarded(self):
        result = extract_trade_event("BOUGHT IBM 07/24 207.5C $2.58 [SMALL]")
        self.assertEqual(result["strike"], Decimal("207.5"))
        self.assertIsInstance(result["strike"], Decimal)


class ExtractTradeEventPriceTests(unittest.TestCase):
    def test_price_dollar_no_leading_digit(self):
        result = extract_trade_event("SOLD NVDA 07/24 207.5C $.91 ALL OUT")
        self.assertEqual(result["price"], Decimal(".91"))

    def test_price_dollar_no_leading_digit_short(self):
        result = extract_trade_event("SOLD AVGO 07/24 377.5P $.40 ALL OUT")
        self.assertEqual(result["price"], Decimal(".40"))

    def test_price_whole_dollar_no_decimal(self):
        result = extract_trade_event("BOUGHT MU 07/24 950C $3 [SMALL]")
        self.assertEqual(result["price"], Decimal("3"))

    def test_price_leading_zero(self):
        result = extract_trade_event("BOUGHT NVDA 7/24 210C $0.68 [SMALL]")
        self.assertEqual(result["price"], Decimal("0.68"))

    def test_price_is_decimal_never_float(self):
        result = extract_trade_event("BOUGHT SPY 450C $3.25")
        self.assertIsInstance(result["price"], Decimal)
        self.assertNotIsInstance(result["price"], float)

    def test_price_never_demotes_status_when_present(self):
        result = extract_trade_event("BOUGHT SPY 450C $.01")
        self.assertEqual(result["parse_status"], PARSE_STATUS_PARSED)


class ExtractTradeEventPositionFractionTests(unittest.TestCase):
    def test_all_required_fractions_recognized(self):
        for fraction in ("1/2", "1/4", "1/6", "1/8", "1/16", "1/3"):
            result = extract_trade_event(f"SOLD SPY 450C $3.25 {fraction}")
            self.assertEqual(result["qualifier"], fraction, fraction)
            self.assertEqual(result["event_type"], "PARTIAL_EXIT", fraction)

    def test_unsupported_fraction_not_treated_as_qualifier(self):
        # 1/5 is not in the required set - must not be silently accepted as
        # a position fraction.
        result = extract_trade_event("SOLD SPY 450C $3.25 1/5")
        self.assertNotEqual(result["qualifier"], "1/5")
        self.assertIn(FLAG_QUALIFIER_MISSING, result["ambiguity_flags"])


class ExtractTradeEventAllOutTests(unittest.TestCase):
    def test_all_out_recognized_as_qualifier(self):
        result = extract_trade_event("SOLD SPY 450C $3.25 ALL OUT")
        self.assertEqual(result["qualifier"], "ALL OUT")
        self.assertEqual(result["event_type"], "FULL_EXIT")

    def test_all_out_with_hit_stop_is_full_exit_not_a_separate_type(self):
        result = extract_trade_event(
            "SOLD SPX 7/24 7440C $2.50 ALL OUT\n"
            "SPX 7/24 7440C $2.50 ALL OUT\n"
            "$6.10 → $2.50 (-59%)\n"
            "HIT STOP"
        )
        self.assertEqual(result["event_type"], "FULL_EXIT")
        self.assertEqual(result["notes"], "HIT STOP")


class ExtractTradeEventAnnotationTests(unittest.TestCase):
    def test_small_annotation_is_entry(self):
        result = extract_trade_event("BOUGHT AVGO 07/24 380P $1.14 [SMALL]")
        self.assertEqual(result["qualifier"], "[SMALL]")
        self.assertEqual(result["event_type"], "ENTRY")

    def test_add_annotation_is_add_event_type(self):
        result = extract_trade_event("BOUGHT QQQ 07/24 685P $2.1 [ADD]")
        self.assertEqual(result["qualifier"], "[ADD]")
        self.assertEqual(result["event_type"], "ADD")

    def test_roll_up_annotation_survives_internal_space(self):
        result = extract_trade_event("BOUGHT QQQ 07/24 690C $1.28 [ROLL UP]")
        self.assertEqual(result["qualifier"], "[ROLL UP]")
        self.assertEqual(result["event_type"], "ROLL_UP")

    def test_b_grade_annotation_survives_internal_space_and_is_entry(self):
        result = extract_trade_event("BOUGHT TSLA 7/24 312.5P $1.70 [B GRADE]")
        self.assertEqual(result["qualifier"], "[B GRADE]")
        self.assertEqual(result["event_type"], "ENTRY")


class ExtractTradeEventRepeatedLineTests(unittest.TestCase):
    def test_repeated_contract_summary_line_not_double_extracted(self):
        result = extract_trade_event(
            "BOUGHT AVGO 07/24 380P $1.14 [SMALL]\nAVGO 07/24 380P $1.14 [SMALL]"
        )
        self.assertEqual(result["symbol"], "AVGO")
        self.assertEqual(result["price"], Decimal("1.14"))
        # A single dict is always returned regardless of how many lines
        # restate the same contract - there is no "list of extra events".
        self.assertIsInstance(result, dict)

    def test_bare_fraction_confirmation_line_not_double_extracted(self):
        result = extract_trade_event(
            "SOLD IBM 07/24 207.5C $3.2 1/2\nIBM 07/24 207.5C $3.2\n1/2 position"
        )
        self.assertEqual(result["qualifier"], "1/2")

    def test_bare_all_out_confirmation_line_not_double_extracted(self):
        result = extract_trade_event(
            "SOLD NVDA 07/24 207.5C $.91 ALL OUT\nNVDA 07/24 207.5C $.91\nALL OUT"
        )
        self.assertEqual(result["qualifier"], "ALL OUT")
        self.assertIsNone(result["notes"])


class ExtractTradeEventNotesTests(unittest.TestCase):
    def test_hit_stop_preserved_in_notes(self):
        result = extract_trade_event("SOLD SPY 450C $3.25 ALL OUT\nHIT STOP")
        self.assertEqual(result["notes"], "HIT STOP")

    def test_stop_lod_preserved_in_notes(self):
        result = extract_trade_event(
            "BOUGHT TSLA 7/24 310C $2.38 [B GRADE]\nSCALP/fast trade\nSTOP LOD"
        )
        self.assertEqual(result["notes"], "SCALP/fast trade\nSTOP LOD")

    def test_trying_again_preserved_in_notes(self):
        result = extract_trade_event(
            "BOUGHT SPX 7/24 7430P $3.35 [ROLL UP]\nTRYING AGAIN"
        )
        self.assertEqual(result["notes"], "TRYING AGAIN")

    def test_hod_test_preserved_in_notes(self):
        result = extract_trade_event(
            "SOLD SPX 7/24 7470C $4.40 1/2\nHOD test"
        )
        self.assertEqual(result["notes"], "HOD test")

    def test_multiple_notes_lines_joined_in_order(self):
        result = extract_trade_event(
            "BOUGHT SPX 7/24 7430P $4.00 [ROLL UP]\n"
            "9ON10 short\n"
            "will be fast to cut if wrong"
        )
        self.assertEqual(result["notes"], "9ON10 short\nwill be fast to cut if wrong")

    def test_no_notes_lines_yields_none(self):
        result = extract_trade_event("BOUGHT SPY 450C $3.25")
        self.assertIsNone(result["notes"])


class ExtractTradeEventStatedReturnTests(unittest.TestCase):
    def test_stated_entry_and_return_captured(self):
        result = extract_trade_event(
            "SOLD IBM 07/24 207.5C $3.2 1/2\n"
            "IBM 07/24 207.5C $3.2\n"
            "1/2 position\n"
            "$2.58 → $3.2 (+24%)"
        )
        self.assertEqual(result["stated_entry_price"], Decimal("2.58"))
        self.assertEqual(result["stated_return_pct"], Decimal("24"))

    def test_negative_stated_return_captured(self):
        result = extract_trade_event(
            "SOLD QQQ 07/24 685P $1.5 ALL OUT\n$2.10 → $1.5 (-29%)"
        )
        self.assertEqual(result["stated_return_pct"], Decimal("-29"))

    def test_missing_stated_return_flagged_for_exit_without_forcing_partial_status(self):
        result = extract_trade_event("SOLD NVDA 07/24 207.5C $.91 ALL OUT")
        self.assertIsNone(result["stated_entry_price"])
        self.assertIn(FLAG_STATED_RETURN_MISSING, result["ambiguity_flags"])
        self.assertEqual(result["parse_status"], PARSE_STATUS_PARSED)

    def test_entry_messages_never_flagged_for_missing_stated_return(self):
        # Entries structurally never carry a "$OLD -> $NEW" line - this is
        # not ambiguous, so no flag should fire for a BOUGHT message.
        result = extract_trade_event("BOUGHT SPY 450C $3.25 [SMALL]")
        self.assertNotIn(FLAG_STATED_RETURN_MISSING, result["ambiguity_flags"])


class ExtractTradeEventParseStatusTests(unittest.TestCase):
    def test_fully_populated_message_is_parsed(self):
        result = extract_trade_event("BOUGHT SPY 450C $3.25")
        self.assertEqual(result["parse_status"], PARSE_STATUS_PARSED)

    def test_missing_price_is_partially_parsed(self):
        result = extract_trade_event("BOUGHT SPY 450C")
        self.assertEqual(result["parse_status"], PARSE_STATUS_PARTIALLY_PARSED)
        self.assertEqual(result["symbol"], "SPY")

    def test_missing_symbol_is_partially_parsed(self):
        result = extract_trade_event("BOUGHT 450C $3.25")
        self.assertEqual(result["parse_status"], PARSE_STATUS_PARTIALLY_PARSED)
        self.assertIsNone(result["symbol"])

    def test_no_action_line_at_all_is_unrecognized(self):
        result = extract_trade_event("just some chatter, nothing tradeable here")
        self.assertEqual(result["parse_status"], PARSE_STATUS_UNRECOGNIZED)
        self.assertIsNone(result["symbol"])
        self.assertIsNone(result["action"])

    def test_empty_string_is_unrecognized(self):
        result = extract_trade_event("")
        self.assertEqual(result["parse_status"], PARSE_STATUS_UNRECOGNIZED)

    def test_whitespace_only_is_unrecognized(self):
        result = extract_trade_event("   \n\t  ")
        self.assertEqual(result["parse_status"], PARSE_STATUS_UNRECOGNIZED)

    def test_missing_optional_metadata_never_demotes_parsed_status(self):
        # No strike, no qualifier, no expiration at all - all optional -
        # action/symbol/price are the only things that matter for status.
        result = extract_trade_event("BOUGHT SPY $625.50")
        self.assertEqual(result["parse_status"], PARSE_STATUS_PARSED)
        self.assertIn(FLAG_STRIKE_MISSING, result["ambiguity_flags"])
        self.assertIn(FLAG_QUALIFIER_MISSING, result["ambiguity_flags"])


class ExtractTradeEventExpirationTests(unittest.TestCase):
    def test_bare_date_no_year_preserved_raw_not_resolved(self):
        result = extract_trade_event("BOUGHT AVGO 07/24 380P $1.14 [SMALL]")
        self.assertIsNone(result["expiration"])
        self.assertEqual(result["expiration_raw"], "07/24")
        self.assertIn(FLAG_EXPIRATION_YEAR_MISSING, result["ambiguity_flags"])

    def test_date_with_explicit_year_resolved_and_not_flagged(self):
        result = extract_trade_event("BOUGHT SPY 450C 7/19/2025 @3.25")
        self.assertEqual(result["expiration"], "2025-07-19")
        self.assertEqual(result["expiration_raw"], "7/19/2025")
        self.assertNotIn(FLAG_EXPIRATION_YEAR_MISSING, result["ambiguity_flags"])

    def test_no_date_token_at_all_is_not_flagged(self):
        result = extract_trade_event("BOUGHT SPY 450C $3.25")
        self.assertIsNone(result["expiration_raw"])
        self.assertNotIn(FLAG_EXPIRATION_YEAR_MISSING, result["ambiguity_flags"])


class ExtractTradeEventNeverRaisesTests(unittest.TestCase):
    def test_does_not_raise_on_arbitrary_garbage(self):
        try:
            result = extract_trade_event("!!! $$$ @@@ ///// 12345")
        except Exception as exc:  # noqa: BLE001 - explicitly asserting no raise
            self.fail(f"extract_trade_event() raised unexpectedly: {exc!r}")
        self.assertEqual(result["parse_status"], PARSE_STATUS_UNRECOGNIZED)

    def test_none_input_does_not_raise(self):
        try:
            result = extract_trade_event(None)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            self.fail(f"extract_trade_event() raised unexpectedly: {exc!r}")
        self.assertEqual(result["parse_status"], PARSE_STATUS_UNRECOGNIZED)

    def test_result_always_has_the_full_key_set(self):
        for text in ("", "garbage", "BOUGHT SPY 450C $3.25"):
            result = extract_trade_event(text)
            self.assertEqual(set(result.keys()), _RESULT_KEYS, text)

    def test_failed_status_constant_exists_and_is_distinct(self):
        self.assertEqual(
            len({PARSE_STATUS_PARSED, PARSE_STATUS_PARTIALLY_PARSED,
                 PARSE_STATUS_UNRECOGNIZED, PARSE_STATUS_FAILED}),
            4,
        )


class ExtractorDoesNotImportDiscordAdapterTests(unittest.TestCase):
    def test_parser_module_has_no_discord_adapter_import(self):
        # Checks import lines specifically, not the whole file, matching
        # tests/test_parser.py's ParseMessageSourceIndependenceTests
        # convention - mentioning "discord_adapter" descriptively in a
        # docstring/comment is fine; importing it is not.
        import app.parser as parser_module

        with open(parser_module.__file__, encoding="utf-8") as f:
            lines = f.readlines()
        import_lines = [
            line for line in lines if line.startswith("import ") or line.startswith("from ")
        ]
        for line in import_lines:
            self.assertNotIn("discord_adapter", line)
            self.assertNotIn("discord", line)

    def test_parser_version_constant_exists(self):
        self.assertIsInstance(PARSER_VERSION, str)
        self.assertTrue(PARSER_VERSION)


class FullCorpusExtractionAcceptanceTests(unittest.TestCase):
    """Runs every one of the 68 real corpus messages through the full
    adapter -> extractor pipeline and checks the expected structured
    fields for each. Expected values were derived by hand from the real
    corpus text before running the code, then cross-checked against it.
    """

    # (action, symbol, strike, option_type, price, qualifier, event_type,
    #  notes, stated_entry_price, stated_return_pct)
    _EXPECTED = [
        ("BOUGHT", "AVGO", "380", "put", "1.14", "[SMALL]", "ENTRY", None, None, None),
        ("BOUGHT", "IBM", "207.5", "call", "2.58", "[SMALL]", "ENTRY", None, None, None),
        ("SOLD", "IBM", "207.5", "call", "3.2", "1/2", "PARTIAL_EXIT", None, "2.58", "24"),
        ("SOLD", "IBM", "207.5", "call", "3.5", "1/4", "PARTIAL_EXIT", None, "2.58", "36"),
        ("SOLD", "IBM", "210", "call", "2.30", "1/2", "PARTIAL_EXIT", None, "1.84", "25"),
        ("SOLD", "AVGO", "380", "put", "1.41", "1/4", "PARTIAL_EXIT", None, "1.14", "24"),
        ("SOLD", "IBM", "210", "call", "2.68", "1/4", "PARTIAL_EXIT", None, "1.84", "46"),
        ("SOLD", "IBM", "207.5", "call", "4.3", "1/8", "PARTIAL_EXIT", None, "2.58", "67"),
        ("SOLD", "AVGO", "380", "put", "1.65", "1/4", "PARTIAL_EXIT", None, "1.14", "45"),
        ("BOUGHT", "NVDA", "207.5", "call", "1.17", "[SMALL]", "ENTRY", None, None, None),
        ("SOLD", "NVDA", "207.5", "call", "1.7", "1/2", "PARTIAL_EXIT", None, "1.17", "45"),
        ("BOUGHT", "TSLA", "312.5", "put", "1.70", "[B GRADE]", "ENTRY", None, None, None),
        ("SOLD", "NVDA", "207.5", "call", ".91", "ALL OUT", "FULL_EXIT", None, None, None),
        ("SOLD", "AVGO", "380", "put", "2.05", "1/4", "PARTIAL_EXIT", None, "1.14", "80"),
        ("SOLD", "AVGO", "380", "put", "2.5", "1/8", "PARTIAL_EXIT", None, "1.14", "119"),
        ("BOUGHT", "QQQ", "685", "put", "2.1", "[ADD]", "ADD", None, None, None),
        ("SOLD", "QQQ", "685", "put", "1.5", "ALL OUT", "FULL_EXIT", None, "2.10", "-29"),
        ("BOUGHT", "QQQ", "690", "call", "1.28", "[ROLL UP]", "ROLL_UP", None, None, None),
        ("SOLD", "TSLA", "312.5", "put", "2.41", "1/2", "PARTIAL_EXIT", None, "1.70", "42"),
        ("BOUGHT", "QQQ", "687", "call", "1.7", "[SMALL]", "ENTRY", None, None, None),
        ("SOLD", "QQQ", "687", "call", "2.3", "1/2", "PARTIAL_EXIT", None, "1.70", "35"),
        ("BOUGHT", "SPX", "7440", "call", "6.10", "[SMALL]", "ENTRY", None, None, None),
        ("SOLD", "QQQ", "687", "call", "2.5", "1/4", "PARTIAL_EXIT", None, "1.70", "47"),
        ("SOLD", "QQQ", "687", "call", "1.58", "ALL OUT", "FULL_EXIT", None, "1.70", "-7"),
        ("SOLD", "TSLA", "312.5", "put", "3.10", "1/6", "PARTIAL_EXIT", None, "1.70", "82"),
        ("SOLD", "IBM", "207.5", "call", "5.2", "1/16", "PARTIAL_EXIT",
         "*HOLDING LAST RUNNER FOR GLORY", "2.58", "102"),
        ("SOLD", "TSLA", "312.5", "put", "4.66", "1/6", "PARTIAL_EXIT", None, "1.70", "174"),
        ("SOLD", "SPX", "7440", "call", "2.50", "ALL OUT", "FULL_EXIT", "HIT STOP", "6.10", "-59"),
        ("SOLD", "TSLA", "312.5", "put", "4.83", "ALL OUT", "FULL_EXIT", None, "1.70", "184"),
        ("BOUGHT", "AVGO", "377.5", "put", "0.95", "[ROLL UP]", "ROLL_UP", None, None, None),
        ("BOUGHT", "TSLA", "310", "call", "2.38", "[B GRADE]", "ENTRY",
         "SCALP/fast trade\nSTOP LOD", None, None),
        ("SOLD", "TSLA", "310", "call", "3.00", "1/2", "PARTIAL_EXIT", None, "2.38", "26"),
        ("SOLD", "TSLA", "310", "call", "3.40", "1/4", "PARTIAL_EXIT", None, "2.38", "43"),
        ("SOLD", "AVGO", "377.5", "put", ".40", "ALL OUT", "FULL_EXIT", None, None, None),
        ("SOLD", "IBM", "210", "call", "3.60", "ALL OUT", "FULL_EXIT", None, "1.84", "96"),
        ("SOLD", "TSLA", "310", "call", "2.40", "ALL OUT", "FULL_EXIT", None, "2.38", "1"),
        ("BOUGHT", "SPX", "7450", "call", "2.80", "[SMALL]", "ENTRY", None, None, None),
        ("SOLD", "SPX", "7450", "call", "3.65", "1/4", "PARTIAL_EXIT", None, "2.80", "30"),
        ("SOLD", "SPX", "7450", "call", "5.00", "1/4", "PARTIAL_EXIT", None, "2.80", "79"),
        ("SOLD", "SPX", "7450", "call", "8.00", "1/4", "PARTIAL_EXIT", None, "2.80", "186"),
        ("SOLD", "QQQ", "690", "call", "1.60", "1/4", "PARTIAL_EXIT", None, "1.28", "25"),
        ("SOLD", "QQQ", "690", "call", "1.83", "1/4", "PARTIAL_EXIT", None, "1.28", "43"),
        ("SOLD", "QQQ", "690", "call", "2.25", "1/6", "PARTIAL_EXIT", None, "1.28", "76"),
        ("SOLD", "SPX", "7450", "call", "15.00", "ALL OUT", "FULL_EXIT", None, "2.80", "436"),
        ("SOLD", "IBM", "207.5", "call", "7.3", "ALL OUT", "FULL_EXIT", None, "2.58", "183"),
        ("BOUGHT", "SPX", "7470", "call", "3.35", "[ROLL UP]", "ROLL_UP", None, None, None),
        ("BOUGHT", "JPM", "352.5", "call", "0.92", "[ROLL UP]", "ROLL_UP", None, None, None),
        ("SOLD", "SPX", "7470", "call", "4.40", "1/2", "PARTIAL_EXIT", "HOD test", "3.35", "31"),
        ("SOLD", "SPX", "7470", "call", "5.00", "1/4", "PARTIAL_EXIT", None, "3.35", "49"),
        ("BOUGHT", "MU", "950", "call", "3", "[SMALL]", "ENTRY", None, None, None),
        ("SOLD", "MU", "950", "call", "3.7", "1/2", "PARTIAL_EXIT", None, "3.00", "23"),
        ("SOLD", "JPM", "352.5", "call", "1.13", "1/4", "PARTIAL_EXIT", None, "0.92", "23"),
        ("SOLD", "JPM", "352.5", "call", "1.31", "1/16", "PARTIAL_EXIT", None, "0.92", "42"),
        ("SOLD", "MU", "950", "call", "6", "1/4", "PARTIAL_EXIT", None, "3.00", "100"),
        ("SOLD", "QQQ", "690", "call", "2.84", "1/6", "PARTIAL_EXIT", None, "1.28", "122"),
        ("SOLD", "SPX", "7470", "call", "3.25", "ALL OUT", "FULL_EXIT", None, "3.35", "-3"),
        ("BOUGHT", "MU", "955", "call", "3.3", "[ROLL UP]", "ROLL_UP", None, None, None),
        ("BOUGHT", "NVDA", "210", "call", "0.68", "[SMALL]", "ENTRY", None, None, None),
        ("SOLD", "NVDA", "210", "call", "0.83", "1/2", "PARTIAL_EXIT", None, "0.68", "22"),
        ("SOLD", "NVDA", "210", "call", "0.96", "1/4", "PARTIAL_EXIT", None, "0.68", "41"),
        ("SOLD", "NVDA", "210", "call", "0.71", "ALL OUT", "FULL_EXIT", None, "0.68", "4"),
        ("BOUGHT", "SPX", "7430", "put", "4.00", "[ROLL UP]", "ROLL_UP",
         "9ON10 short\nwill be fast to cut if wrong", None, None),
        ("SOLD", "SPX", "7430", "put", "3.65", "ALL OUT", "FULL_EXIT", None, "4.00", "-9"),
        ("BOUGHT", "SPX", "7430", "put", "3.35", "[ROLL UP]", "ROLL_UP", "TRYING AGAIN", None, None),
        ("SOLD", "SPX", "7430", "put", "4.40", "1/3", "PARTIAL_EXIT", None, "3.35", "31"),
        ("SOLD", "SPX", "7430", "put", "5.00", "1/3", "PARTIAL_EXIT", None, "3.35", "49"),
        ("SOLD", "SPX", "7430", "put", "6.00", "1/6", "PARTIAL_EXIT", None, "3.35", "79"),
        ("SOLD", "SPX", "7430", "put", "12.00", "ALL OUT", "FULL_EXIT", None, "3.35", "258"),
    ]

    @classmethod
    def setUpClass(cls):
        cls.messages = segment_discord_batch(CORPUS)
        cls.results = [extract_trade_event(m.cleaned_text) for m in cls.messages]

    def test_corpus_has_68_messages(self):
        self.assertEqual(len(self.messages), 68)

    def test_every_message_produces_exactly_one_result(self):
        self.assertEqual(len(self.results), 68)
        for result in self.results:
            self.assertIsInstance(result, dict)

    def test_every_result_is_parsed(self):
        statuses = {r["parse_status"] for r in self.results}
        self.assertEqual(statuses, {PARSE_STATUS_PARSED})

    def test_expected_table_matches_result_count(self):
        self.assertEqual(len(self._EXPECTED), 68)

    def test_every_field_matches_expected_for_every_message(self):
        for index, (expected, result) in enumerate(zip(self._EXPECTED, self.results), 1):
            (
                action, symbol, strike, option_type, price, qualifier,
                event_type, notes, stated_entry, stated_pct,
            ) = expected
            self.assertEqual(result["action"], action, f"message {index} action")
            self.assertEqual(result["symbol"], symbol, f"message {index} symbol")
            self.assertEqual(result["strike"], Decimal(strike), f"message {index} strike")
            self.assertEqual(result["option_type"], option_type, f"message {index} option_type")
            self.assertEqual(result["price"], Decimal(price), f"message {index} price")
            self.assertEqual(result["qualifier"], qualifier, f"message {index} qualifier")
            self.assertEqual(result["event_type"], event_type, f"message {index} event_type")
            self.assertEqual(result["notes"], notes, f"message {index} notes")
            expected_stated_entry = Decimal(stated_entry) if stated_entry is not None else None
            expected_stated_pct = Decimal(stated_pct) if stated_pct is not None else None
            self.assertEqual(
                result["stated_entry_price"], expected_stated_entry, f"message {index} stated_entry_price"
            )
            self.assertEqual(
                result["stated_return_pct"], expected_stated_pct, f"message {index} stated_return_pct"
            )

    def test_every_message_expiration_raw_present_but_unresolved(self):
        # No message in this corpus carries a year - expiration stays None
        # and expiration_raw holds the bare token, on every one of the 68.
        for index, result in enumerate(self.results, 1):
            self.assertIsNone(result["expiration"], f"message {index}")
            self.assertIsNotNone(result["expiration_raw"], f"message {index}")
            self.assertIn(FLAG_EXPIRATION_YEAR_MISSING, result["ambiguity_flags"], f"message {index}")

    def test_exactly_two_messages_flagged_for_missing_stated_return(self):
        # Only the TC NVDA 207.5C ALL OUT ($.91, message 13) and the
        # Bdorts AVGO 377.5P ALL OUT ($.40, message 34) lack a price-arrow
        # line in the real corpus.
        flagged_indices = [
            i for i, r in enumerate(self.results, 1)
            if FLAG_STATED_RETURN_MISSING in r["ambiguity_flags"]
        ]
        self.assertEqual(flagged_indices, [13, 34])

    def test_event_type_distribution(self):
        from collections import Counter

        counts = Counter(r["event_type"] for r in self.results)
        self.assertEqual(counts["ENTRY"], 10)
        self.assertEqual(counts["ADD"], 1)
        self.assertEqual(counts["ROLL_UP"], 7)
        self.assertEqual(counts["PARTIAL_EXIT"], 36)
        self.assertEqual(counts["FULL_EXIT"], 14)
        self.assertNotIn("STOP_EXIT", counts)
        self.assertEqual(sum(counts.values()), 68)

    def test_no_qualifier_missing_flags_in_real_corpus(self):
        # Every real alert has either a bracket annotation or a
        # fraction/ALL OUT - this flag should never fire here.
        for index, result in enumerate(self.results, 1):
            self.assertNotIn(FLAG_QUALIFIER_MISSING, result["ambiguity_flags"], f"message {index}")

    def test_no_strike_missing_flags_in_real_corpus(self):
        for index, result in enumerate(self.results, 1):
            self.assertNotIn(FLAG_STRIKE_MISSING, result["ambiguity_flags"], f"message {index}")


if __name__ == "__main__":
    unittest.main()
