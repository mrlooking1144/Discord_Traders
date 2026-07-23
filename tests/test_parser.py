"""Tests for app/parser.py.

Covers Milestone 2C.1: the source-independent parse_message() function.
Pure unit tests - no database, no TradeService, no UI.
"""

import unittest
from decimal import Decimal

from app.parser import parse_message


class ParseMessageWellFormedTests(unittest.TestCase):
    def test_full_call_signal(self):
        result = parse_message("BTO SPY 450C 7/19/2025 @3.25 10 contracts")
        self.assertEqual(
            result,
            [
                {
                    "symbol": "SPY",
                    "action": "BTO",
                    "option_type": "call",
                    "price": Decimal("3.25"),
                    "expiration": "2025-07-19",
                    "position_size": "10 contracts",
                }
            ],
        )

    def test_full_put_signal(self):
        result = parse_message("STC AAPL 190P 12/15/2025 @1.10 half position")
        self.assertEqual(
            result,
            [
                {
                    "symbol": "AAPL",
                    "action": "STC",
                    "option_type": "put",
                    "price": Decimal("1.10"),
                    "expiration": "2025-12-15",
                    "position_size": "half position",
                }
            ],
        )

    def test_price_is_decimal_not_float(self):
        result = parse_message("BTO SPY 450C 7/19/2025 @3.25")
        price = result[0]["price"]
        self.assertIsInstance(price, Decimal)
        self.assertNotIsInstance(price, float)

    def test_two_digit_year_treated_as_20yy(self):
        result = parse_message("BTO TSLA 250C 1/5/26 @5.00")
        self.assertEqual(result[0]["expiration"], "2026-01-05")

    def test_plain_equity_trade_no_option(self):
        result = parse_message("BTO TSLA @250.50")
        self.assertEqual(
            result,
            [
                {
                    "symbol": "TSLA",
                    "action": "BTO",
                    "option_type": None,
                    "price": Decimal("250.50"),
                    "expiration": None,
                    "position_size": None,
                }
            ],
        )

    def test_call_put_word_form(self):
        call_result = parse_message("BTO SPY CALL 7/19/2025 @3.25")
        self.assertEqual(call_result[0]["option_type"], "call")

        put_result = parse_message("STC SPY PUT 7/19/2025 @1.10")
        self.assertEqual(put_result[0]["option_type"], "put")

    def test_dollar_sign_price_prefix(self):
        result = parse_message("BTO SPY $3.25")
        self.assertEqual(result[0]["price"], Decimal("3.25"))

    def test_action_and_symbol_case_insensitive(self):
        result = parse_message("bto spy 450c 7/19/2025 @3.25")
        self.assertEqual(result[0]["action"], "BTO")
        self.assertEqual(result[0]["symbol"], "SPY")
        self.assertEqual(result[0]["option_type"], "call")

    def test_btc_and_sto_actions_recognized(self):
        btc_result = parse_message("BTC SPY 450C @3.25")
        self.assertEqual(btc_result[0]["action"], "BTC")

        sto_result = parse_message("STO SPY 450P @1.10")
        self.assertEqual(sto_result[0]["action"], "STO")

    def test_buy_action_recognized_and_not_mapped_to_bto(self):
        result = parse_message("BUY SPY @625.50")
        self.assertEqual(
            result,
            [
                {
                    "symbol": "SPY",
                    "action": "BUY",
                    "option_type": None,
                    "price": Decimal("625.50"),
                    "expiration": None,
                    "position_size": None,
                }
            ],
        )
        self.assertNotEqual(result[0]["action"], "BTO")

    def test_sell_action_recognized_and_not_mapped_to_stc(self):
        result = parse_message("SELL NVDA @182.30")
        self.assertEqual(
            result,
            [
                {
                    "symbol": "NVDA",
                    "action": "SELL",
                    "option_type": None,
                    "price": Decimal("182.30"),
                    "expiration": None,
                    "position_size": None,
                }
            ],
        )
        self.assertNotEqual(result[0]["action"], "STC")

    def test_buy_sell_case_insensitive(self):
        buy_result = parse_message("buy spy @625.50")
        self.assertEqual(buy_result[0]["action"], "BUY")

        sell_result = parse_message("sell nvda @182.30")
        self.assertEqual(sell_result[0]["action"], "SELL")


class ParseMessageMissingOptionalFieldsTests(unittest.TestCase):
    def test_missing_price(self):
        result = parse_message("BTO SPY 450C 7/19/2025")
        self.assertIsNone(result[0]["price"])
        self.assertEqual(result[0]["expiration"], "2025-07-19")

    def test_missing_expiration_year_left_unparsed(self):
        result = parse_message("BTO SPY 450C 7/19 @3.25")
        signal = result[0]
        self.assertIsNone(signal["expiration"])
        self.assertEqual(signal["price"], Decimal("3.25"))
        self.assertEqual(signal["position_size"], "7/19")

    def test_missing_option_type(self):
        result = parse_message("BTO SPY 7/19/2025 @3.25")
        self.assertIsNone(result[0]["option_type"])

    def test_missing_position_size_is_none_not_empty_string(self):
        result = parse_message("BTO SPY 450C 7/19/2025 @3.25")
        self.assertIsNone(result[0]["position_size"])

    def test_all_keys_present_even_when_all_optional_fields_absent(self):
        result = parse_message("BTO SPY")
        self.assertEqual(
            set(result[0].keys()),
            {"symbol", "action", "option_type", "price", "expiration", "position_size"},
        )


class ParseMessageMalformedInputTests(unittest.TestCase):
    def test_empty_string_returns_empty_list(self):
        self.assertEqual(parse_message(""), [])

    def test_whitespace_only_returns_empty_list(self):
        self.assertEqual(parse_message("   \n\t  "), [])

    def test_no_recognized_action_returns_empty_list(self):
        self.assertEqual(parse_message("just chatting, no trade here"), [])

    def test_action_with_no_symbol_skipped(self):
        self.assertEqual(parse_message("BTO"), [])

    def test_invalid_symbol_token_skips_line(self):
        self.assertEqual(parse_message("BTO SPY123 something"), [])

    def test_out_of_range_date_left_unparsed(self):
        result = parse_message("BTO SPY 450C 13/45/2025 @3.25")
        signal = result[0]
        self.assertIsNone(signal["expiration"])
        self.assertIn("13/45/2025", signal["position_size"])

    def test_does_not_raise_on_arbitrary_garbage(self):
        try:
            result = parse_message("!!! $$$ @@@ ///// 12345")
        except Exception as exc:  # noqa: BLE001 - explicitly asserting no raise
            self.fail(f"parse_message() raised unexpectedly: {exc!r}")
        self.assertEqual(result, [])


class ParseMessageMultiSignalTests(unittest.TestCase):
    def test_multiple_signals_in_order_with_commentary_skipped(self):
        raw_text = (
            "BTO SPY 450C 7/19/2025 @3.25\n"
            "STC AAPL 190P 12/15/2025 @1.10 half position\n"
            "random commentary line\n"
            "BTO TSLA @250.50"
        )
        result = parse_message(raw_text)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["symbol"], "SPY")
        self.assertEqual(result[1]["symbol"], "AAPL")
        self.assertEqual(result[2]["symbol"], "TSLA")

    def test_blank_lines_between_signals_ignored(self):
        raw_text = "BTO SPY 450C @3.25\n\n\nSTC AAPL 190P @1.10"
        result = parse_message(raw_text)
        self.assertEqual(len(result), 2)


class ParseMessageSourceIndependenceTests(unittest.TestCase):
    def _module_lines(self):
        import app.parser as parser_module

        with open(parser_module.__file__, "r", encoding="utf-8") as f:
            return f.readlines()

    def test_module_has_no_import_statements_beyond_stdlib(self):
        import_lines = [
            line
            for line in self._module_lines()
            if line.startswith("import ") or line.startswith("from ")
        ]
        # Only stdlib imports (re, decimal, __future__) are allowed - no
        # database, service, UI, or networking modules.
        for line in import_lines:
            self.assertNotIn("database", line)
            self.assertNotIn("app.streamlit_app", line)
            self.assertNotIn("streamlit", line)
            self.assertNotIn("requests", line)
            self.assertNotIn("socket", line)
            self.assertNotIn("urllib", line)
            self.assertNotIn("discord", line)

    def test_module_never_calls_trade_service(self):
        contents = "".join(self._module_lines())
        self.assertNotIn("TradeService(", contents)


if __name__ == "__main__":
    unittest.main()
