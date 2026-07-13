"""UI/smoke tests for app/app.py.

Covers Milestone 2C.2: manual raw-message entry, invoking the Milestone 2C.1
parser, and displaying either the parsed structured result or a "nothing
found" message. Uses Streamlit's AppTest harness to drive the app without a
running server. No TradeService, repository, or database involvement.
"""

import json
import unittest

from streamlit.testing.v1 import AppTest


class ManualMessageEntryTests(unittest.TestCase):
    def test_successful_parse_displays_structured_result(self):
        at = AppTest.from_file("app/app.py")
        at.run()

        at.text_area[0].input("BTO SPY 450C 7/19/2025 @3.25 10 contracts").run()
        at.button[0].click().run()

        self.assertEqual(len(at.success), 1)
        self.assertIn("1 trade signal", at.success[0].value)
        self.assertEqual(len(at.json), 1)
        displayed = json.loads(at.json[0].value)
        self.assertEqual(displayed["symbol"], "SPY")
        self.assertEqual(displayed["action"], "BTO")
        self.assertEqual(displayed["option_type"], "call")
        self.assertEqual(displayed["expiration"], "2025-07-19")
        self.assertEqual(displayed["position_size"], "10 contracts")
        self.assertIn("3.25", displayed["price"])
        self.assertEqual(len(at.warning), 0)

    def test_multiple_signals_each_displayed(self):
        at = AppTest.from_file("app/app.py")
        at.run()

        at.text_area[0].input("BTO SPY 450C 7/19/2025 @3.25\nSTC AAPL 190P 12/15/2025 @1.10").run()
        at.button[0].click().run()

        self.assertEqual(len(at.success), 1)
        self.assertIn("2 trade signal", at.success[0].value)
        self.assertEqual(len(at.json), 2)

    def test_no_signal_displays_nothing_found_message(self):
        at = AppTest.from_file("app/app.py")
        at.run()

        at.text_area[0].input("just some random text").run()
        at.button[0].click().run()

        self.assertEqual(len(at.warning), 1)
        self.assertIn("No trade signals found", at.warning[0].value)
        self.assertEqual(len(at.success), 0)
        self.assertEqual(len(at.json), 0)

    def test_empty_input_displays_nothing_found_message(self):
        at = AppTest.from_file("app/app.py")
        at.run()

        at.button[0].click().run()

        self.assertEqual(len(at.warning), 1)
        self.assertIn("No trade signals found", at.warning[0].value)

    def test_no_run_before_button_click_shows_no_result(self):
        at = AppTest.from_file("app/app.py")
        at.run()

        self.assertEqual(len(at.success), 0)
        self.assertEqual(len(at.warning), 0)
        self.assertEqual(len(at.json), 0)


if __name__ == "__main__":
    unittest.main()
