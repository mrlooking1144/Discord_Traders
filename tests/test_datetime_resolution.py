"""Tests for app/datetime_resolution.py.

Covers Recovery Milestone R4: resolve_expiration() and
resolve_footer_timestamp(). Pure unit tests - no database, no
TradeService, no UI, no app.parser or app.discord_adapter dependency.
"""

import unittest
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.datetime_resolution import (
    FLAG_DATETIME_RESOLUTION_INTERNAL_ERROR,
    FLAG_EXPIRATION_INVALID_CALENDAR_DATE,
    FLAG_EXPIRATION_INVALID_FORMAT,
    FLAG_EXPIRATION_UNSUPPORTED_FORMAT,
    FLAG_EXPLICIT_DATE_UNSUPPORTED_FORMAT,
    FLAG_FOOTER_TIMESTAMP_UNRECOGNIZED,
    FLAG_INVALID_FOOTER_TIME,
    FLAG_INVALID_REFERENCE_DATE,
    FLAG_INVALID_TIMEZONE,
    ExpirationResolution,
    TimestampResolution,
    resolve_expiration,
    resolve_footer_timestamp,
)

_NY = "America/New_York"


class _NoWallClockDate(date):
    """A date subclass whose today() raises - date/datetime are immutable
    C types, so unittest.mock.patch cannot set an attribute directly on
    them; patching the module's name binding to this subclass instead lets
    the "never reads the wall clock" tests prove non-usage by substitution
    rather than by attempting to stub the built-in classes in place."""

    @classmethod
    def today(cls):
        raise AssertionError("date.today() must never be called")


class _NoWallClockDateTime(datetime):
    """A datetime subclass whose now() raises - see _NoWallClockDate."""

    @classmethod
    def now(cls, tz=None):
        raise AssertionError("datetime.now() must never be called")


class ResolveExpirationRequiredExamplesTests(unittest.TestCase):
    """The four worked examples from the approved R4 implementation contract."""

    def test_exactly_30_days_before_does_not_roll(self):
        result = resolve_expiration("6/24", "2026-07-24")
        self.assertEqual(result.resolved_expiration, "2026-06-24")
        self.assertEqual(result.ambiguity_flags, ())

    def test_exactly_31_days_before_rolls_forward(self):
        result = resolve_expiration("6/23", "2026-07-24")
        self.assertEqual(result.resolved_expiration, "2027-06-23")
        self.assertEqual(result.ambiguity_flags, ())

    def test_year_end_rollover(self):
        result = resolve_expiration("1/05", "2026-12-31")
        self.assertEqual(result.resolved_expiration, "2027-01-05")
        self.assertEqual(result.ambiguity_flags, ())

    def test_candidate_after_reference_date_stays_in_reference_year(self):
        result = resolve_expiration("12/20", "2026-01-05")
        self.assertEqual(result.resolved_expiration, "2026-12-20")
        self.assertEqual(result.ambiguity_flags, ())


class ResolveExpirationBoundaryTests(unittest.TestCase):
    def test_same_day_expiration(self):
        result = resolve_expiration("7/24", "2026-07-24")
        self.assertEqual(result.resolved_expiration, "2026-07-24")

    def test_candidate_on_or_after_reference_date_remains_in_reference_year(self):
        result = resolve_expiration("8/15", "2026-07-24")
        self.assertEqual(result.resolved_expiration, "2026-08-15")
        self.assertEqual(result.ambiguity_flags, ())

    def test_leading_zero_and_no_leading_zero_both_supported(self):
        self.assertEqual(
            resolve_expiration("07/24", "2026-07-24").resolved_expiration,
            "2026-07-24",
        )
        self.assertEqual(
            resolve_expiration("7/24", "2026-07-24").resolved_expiration,
            "2026-07-24",
        )

    def test_leap_year_february_29_valid(self):
        result = resolve_expiration("2/29", "2028-03-01")
        self.assertEqual(result.resolved_expiration, "2028-02-29")
        self.assertEqual(result.ambiguity_flags, ())

    def test_invalid_february_29_non_leap_reference_year(self):
        result = resolve_expiration("2/29", "2026-03-01")
        self.assertIsNone(result.resolved_expiration)
        self.assertEqual(
            result.ambiguity_flags, (FLAG_EXPIRATION_INVALID_CALENDAR_DATE,)
        )

    def test_leap_day_rollover_into_non_leap_year_is_invalid(self):
        # First candidate 2028-02-29 is valid (2028 is leap) but more than
        # 30 days before reference_date, so the year rolls to 2029, which
        # is not leap - the rebuilt candidate is invalid.
        result = resolve_expiration("2/29", "2028-04-15")
        self.assertIsNone(result.resolved_expiration)
        self.assertEqual(
            result.ambiguity_flags, (FLAG_EXPIRATION_INVALID_CALENDAR_DATE,)
        )

    def test_impossible_month_day(self):
        result = resolve_expiration("13/40", "2026-07-24")
        self.assertIsNone(result.resolved_expiration)
        self.assertEqual(
            result.ambiguity_flags, (FLAG_EXPIRATION_INVALID_CALENDAR_DATE,)
        )

    def test_invalid_calendar_date_april_31(self):
        result = resolve_expiration("4/31", "2026-05-01")
        self.assertIsNone(result.resolved_expiration)
        self.assertEqual(
            result.ambiguity_flags, (FLAG_EXPIRATION_INVALID_CALENDAR_DATE,)
        )

    def test_unsupported_year_bearing_expiration_rejected(self):
        result = resolve_expiration("07/24/2026", "2026-07-24")
        self.assertIsNone(result.resolved_expiration)
        self.assertEqual(
            result.ambiguity_flags, (FLAG_EXPIRATION_UNSUPPORTED_FORMAT,)
        )

    def test_unsupported_two_digit_year_bearing_expiration_rejected(self):
        result = resolve_expiration("07/24/26", "2026-07-24")
        self.assertEqual(
            result.ambiguity_flags, (FLAG_EXPIRATION_UNSUPPORTED_FORMAT,)
        )

    def test_none_expiration_raw_is_invalid_format(self):
        result = resolve_expiration(None, "2026-07-24")
        self.assertIsNone(result.resolved_expiration)
        self.assertEqual(result.ambiguity_flags, (FLAG_EXPIRATION_INVALID_FORMAT,))
        self.assertIsNone(result.expiration_raw)

    def test_blank_expiration_raw_is_invalid_format(self):
        result = resolve_expiration("   ", "2026-07-24")
        self.assertEqual(result.ambiguity_flags, (FLAG_EXPIRATION_INVALID_FORMAT,))

    def test_garbage_expiration_raw_is_invalid_format(self):
        result = resolve_expiration("not-a-date", "2026-07-24")
        self.assertEqual(result.ambiguity_flags, (FLAG_EXPIRATION_INVALID_FORMAT,))

    def test_expiration_raw_preserved_exactly_on_failure(self):
        result = resolve_expiration("13/40", "2026-07-24")
        self.assertEqual(result.expiration_raw, "13/40")

    def test_none_reference_date(self):
        result = resolve_expiration("6/24", None)
        self.assertEqual(result.ambiguity_flags, (FLAG_INVALID_REFERENCE_DATE,))

    def test_blank_reference_date(self):
        result = resolve_expiration("6/24", "")
        self.assertEqual(result.ambiguity_flags, (FLAG_INVALID_REFERENCE_DATE,))

    def test_wrong_separator_reference_date(self):
        result = resolve_expiration("6/24", "2026/07/24")
        self.assertEqual(result.ambiguity_flags, (FLAG_INVALID_REFERENCE_DATE,))

    def test_invalid_calendar_reference_date(self):
        result = resolve_expiration("6/24", "2026-13-40")
        self.assertEqual(result.ambiguity_flags, (FLAG_INVALID_REFERENCE_DATE,))

    def test_deterministic_repeated_call_equality(self):
        first = resolve_expiration("6/23", "2026-07-24")
        second = resolve_expiration("6/23", "2026-07-24")
        self.assertEqual(first, second)

    def test_never_reads_wall_clock(self):
        with patch("app.datetime_resolution.date", _NoWallClockDate):
            result = resolve_expiration("6/24", "2026-07-24")
        self.assertEqual(result.resolved_expiration, "2026-06-24")
        self.assertEqual(result.ambiguity_flags, ())

    def test_internal_error_never_propagates_and_preserves_raw_input(self):
        with patch(
            "app.datetime_resolution._construct_calendar_date",
            side_effect=RuntimeError("unexpected internal failure"),
        ):
            result = resolve_expiration("6/24", "2026-07-24")
        self.assertIsInstance(result, ExpirationResolution)
        self.assertIsNone(result.resolved_expiration)
        self.assertEqual(
            result.ambiguity_flags, (FLAG_DATETIME_RESOLUTION_INTERNAL_ERROR,)
        )
        self.assertEqual(result.expiration_raw, "6/24")


class ResolveFooterTimestampRelativeTests(unittest.TestCase):
    def test_today_with_arabic_sad_is_am(self):
        result = resolve_footer_timestamp(
            "Today at 04:30 ص", "relative_today", "2026-07-24", _NY
        )
        self.assertEqual(
            result.resolved_timestamp,
            datetime(2026, 7, 24, 4, 30, tzinfo=ZoneInfo(_NY)),
        )
        self.assertEqual(result.ambiguity_flags, ())

    def test_today_with_arabic_meem_is_pm(self):
        result = resolve_footer_timestamp(
            "Today at 04:30 م", "relative_today", "2026-07-24", _NY
        )
        self.assertEqual(
            result.resolved_timestamp,
            datetime(2026, 7, 24, 16, 30, tzinfo=ZoneInfo(_NY)),
        )
        self.assertEqual(result.ambiguity_flags, ())

    def test_english_am_uppercase(self):
        result = resolve_footer_timestamp(
            "Today at 4:30 AM", "relative_today", "2026-07-24", _NY
        )
        self.assertEqual(result.resolved_timestamp.hour, 4)

    def test_english_am_lowercase_and_lowercase_today(self):
        result = resolve_footer_timestamp(
            "today at 4:30 am", "relative_today", "2026-07-24", _NY
        )
        self.assertEqual(result.resolved_timestamp.hour, 4)
        self.assertEqual(result.ambiguity_flags, ())

    def test_english_pm_lowercase(self):
        result = resolve_footer_timestamp(
            "Today at 4:30 pm", "relative_today", "2026-07-24", _NY
        )
        self.assertEqual(result.resolved_timestamp.hour, 16)

    def test_lowercase_yesterday(self):
        result = resolve_footer_timestamp(
            "yesterday at 10:00 AM", "relative_yesterday", "2026-07-24", _NY
        )
        self.assertEqual(result.resolved_timestamp.day, 23)

    def test_twelve_am_is_midnight(self):
        result = resolve_footer_timestamp(
            "Today at 12:00 AM", "relative_today", "2026-07-24", _NY
        )
        self.assertEqual(result.resolved_timestamp.hour, 0)

    def test_twelve_pm_is_noon(self):
        result = resolve_footer_timestamp(
            "Today at 12:00 PM", "relative_today", "2026-07-24", _NY
        )
        self.assertEqual(result.resolved_timestamp.hour, 12)

    def test_no_meridiem_is_24_hour_clock(self):
        result = resolve_footer_timestamp(
            "Today at 14:45", "relative_today", "2026-07-24", _NY
        )
        self.assertEqual(result.resolved_timestamp.hour, 14)
        self.assertEqual(result.ambiguity_flags, ())

    def test_yesterday_across_month_boundary(self):
        result = resolve_footer_timestamp(
            "Yesterday at 10:00 AM", "relative_yesterday", "2026-08-01", _NY
        )
        self.assertEqual(result.resolved_timestamp, datetime(2026, 7, 31, 10, 0, tzinfo=ZoneInfo(_NY)))

    def test_yesterday_across_year_boundary(self):
        result = resolve_footer_timestamp(
            "Yesterday at 10:00 AM", "relative_yesterday", "2026-01-01", _NY
        )
        self.assertEqual(result.resolved_timestamp, datetime(2025, 12, 31, 10, 0, tzinfo=ZoneInfo(_NY)))

    def test_invalid_hour_and_minute_no_meridiem(self):
        result = resolve_footer_timestamp(
            "Today at 25:99", "relative_today", "2026-07-24", _NY
        )
        self.assertIsNone(result.resolved_timestamp)
        self.assertEqual(result.ambiguity_flags, (FLAG_INVALID_FOOTER_TIME,))

    def test_invalid_hour_only_no_meridiem(self):
        # minute (30) is in-range on its own, isolating the no-meridiem
        # hour-range check (0-23) from the minute-range check - "25:99"
        # alone would fail on minute first and never exercise this branch.
        result = resolve_footer_timestamp(
            "Today at 25:30", "relative_today", "2026-07-24", _NY
        )
        self.assertIsNone(result.resolved_timestamp)
        self.assertEqual(result.ambiguity_flags, (FLAG_INVALID_FOOTER_TIME,))

    def test_invalid_hour_with_meridiem(self):
        result = resolve_footer_timestamp(
            "Today at 13:00 AM", "relative_today", "2026-07-24", _NY
        )
        self.assertEqual(result.ambiguity_flags, (FLAG_INVALID_FOOTER_TIME,))

    def test_kind_text_mismatch(self):
        result = resolve_footer_timestamp(
            "Yesterday at 10:00 AM", "relative_today", "2026-07-24", _NY
        )
        self.assertIsNone(result.resolved_timestamp)
        self.assertEqual(result.ambiguity_flags, (FLAG_INVALID_FOOTER_TIME,))

    def test_raw_text_preserved_exactly_on_failure(self):
        result = resolve_footer_timestamp(
            "garbage text", "relative_today", "2026-07-24", _NY
        )
        self.assertEqual(result.footer_timestamp_raw, "garbage text")
        self.assertEqual(result.footer_timestamp_kind, "relative_today")


class ResolveFooterTimestampExplicitDateTests(unittest.TestCase):
    _EXPECTED = datetime(2026, 7, 23, 23, 45, tzinfo=ZoneInfo(_NY))

    def test_form_month_day_four_digit_year_with_meridiem(self):
        result = resolve_footer_timestamp(
            "7/23/2026 11:45 PM", "explicit_date", "2026-01-01", _NY
        )
        self.assertEqual(result.resolved_timestamp, self._EXPECTED)

    def test_form_with_comma_after_date(self):
        result = resolve_footer_timestamp(
            "07/23/2026, 11:45 PM", "explicit_date", "2026-01-01", _NY
        )
        self.assertEqual(result.resolved_timestamp, self._EXPECTED)

    def test_form_two_digit_year_with_meridiem(self):
        result = resolve_footer_timestamp(
            "7/23/26 11:45 PM", "explicit_date", "2026-01-01", _NY
        )
        self.assertEqual(result.resolved_timestamp, self._EXPECTED)

    def test_form_four_digit_year_no_meridiem_24_hour(self):
        result = resolve_footer_timestamp(
            "7/23/2026 23:45", "explicit_date", "2026-01-01", _NY
        )
        self.assertEqual(result.resolved_timestamp, self._EXPECTED)

    def test_form_two_digit_year_no_meridiem_24_hour(self):
        result = resolve_footer_timestamp(
            "7/23/26 23:45", "explicit_date", "2026-01-01", _NY
        )
        self.assertEqual(result.resolved_timestamp, self._EXPECTED)

    def test_unsupported_explicit_date_format_month_name(self):
        result = resolve_footer_timestamp(
            "July 23 2026 11:45 PM", "explicit_date", "2026-01-01", _NY
        )
        self.assertIsNone(result.resolved_timestamp)
        self.assertEqual(
            result.ambiguity_flags, (FLAG_EXPLICIT_DATE_UNSUPPORTED_FORMAT,)
        )

    def test_unsupported_explicit_date_format_iso_style(self):
        result = resolve_footer_timestamp(
            "2026-07-23 11:45 PM", "explicit_date", "2026-01-01", _NY
        )
        self.assertEqual(
            result.ambiguity_flags, (FLAG_EXPLICIT_DATE_UNSUPPORTED_FORMAT,)
        )

    def test_unauthorized_two_digit_year_with_comma_and_meridiem_rejected(self):
        # Two-digit year + comma + meridiem is not one of the five
        # authorized forms (only the four-digit year has a comma form) -
        # must be rejected, not silently accepted by a lenient regex.
        raw = "7/23/26, 11:45 PM"
        result = resolve_footer_timestamp(raw, "explicit_date", "2026-01-01", _NY)
        self.assertIsNone(result.resolved_timestamp)
        self.assertEqual(
            result.ambiguity_flags, (FLAG_EXPLICIT_DATE_UNSUPPORTED_FORMAT,)
        )
        self.assertEqual(result.footer_timestamp_raw, raw)
        self.assertEqual(result.footer_timestamp_kind, "explicit_date")

    def test_unauthorized_four_digit_year_with_comma_no_meridiem_rejected(self):
        # Four-digit year + comma + no meridiem is not one of the five
        # authorized forms (the only comma form requires a meridiem).
        raw = "7/23/2026, 23:45"
        result = resolve_footer_timestamp(raw, "explicit_date", "2026-01-01", _NY)
        self.assertIsNone(result.resolved_timestamp)
        self.assertEqual(
            result.ambiguity_flags, (FLAG_EXPLICIT_DATE_UNSUPPORTED_FORMAT,)
        )
        self.assertEqual(result.footer_timestamp_raw, raw)
        self.assertEqual(result.footer_timestamp_kind, "explicit_date")

    def test_unauthorized_two_digit_year_with_comma_no_meridiem_rejected(self):
        # Two-digit year + comma + no meridiem is not one of the five
        # authorized forms at all (no two-digit-year comma form exists).
        raw = "7/23/26, 23:45"
        result = resolve_footer_timestamp(raw, "explicit_date", "2026-01-01", _NY)
        self.assertIsNone(result.resolved_timestamp)
        self.assertEqual(
            result.ambiguity_flags, (FLAG_EXPLICIT_DATE_UNSUPPORTED_FORMAT,)
        )
        self.assertEqual(result.footer_timestamp_raw, raw)
        self.assertEqual(result.footer_timestamp_kind, "explicit_date")

    def test_matched_shape_invalid_month_day(self):
        result = resolve_footer_timestamp(
            "13/40/2026 11:45 PM", "explicit_date", "2026-01-01", _NY
        )
        self.assertIsNone(result.resolved_timestamp)
        self.assertEqual(result.ambiguity_flags, (FLAG_INVALID_FOOTER_TIME,))

    def test_matched_shape_invalid_hour_for_meridiem(self):
        result = resolve_footer_timestamp(
            "7/23/2026 13:45 AM", "explicit_date", "2026-01-01", _NY
        )
        self.assertEqual(result.ambiguity_flags, (FLAG_INVALID_FOOTER_TIME,))


class ResolveFooterTimestampSharedTests(unittest.TestCase):
    def test_unrecognized_kind_string(self):
        result = resolve_footer_timestamp(
            "Today at 04:30 م", "unrecognized", "2026-07-24", _NY
        )
        self.assertIsNone(result.resolved_timestamp)
        self.assertEqual(result.ambiguity_flags, (FLAG_FOOTER_TIMESTAMP_UNRECOGNIZED,))

    def test_none_kind(self):
        result = resolve_footer_timestamp(
            "Today at 04:30 م", None, "2026-07-24", _NY
        )
        self.assertEqual(result.ambiguity_flags, (FLAG_FOOTER_TIMESTAMP_UNRECOGNIZED,))

    def test_unknown_kind_string(self):
        result = resolve_footer_timestamp(
            "Today at 04:30 م", "some_other_kind", "2026-07-24", _NY
        )
        self.assertEqual(result.ambiguity_flags, (FLAG_FOOTER_TIMESTAMP_UNRECOGNIZED,))

    def test_none_raw_text(self):
        result = resolve_footer_timestamp(None, "relative_today", "2026-07-24", _NY)
        self.assertEqual(result.ambiguity_flags, (FLAG_INVALID_FOOTER_TIME,))
        self.assertIsNone(result.footer_timestamp_raw)

    def test_blank_raw_text(self):
        result = resolve_footer_timestamp("   ", "explicit_date", "2026-07-24", _NY)
        self.assertEqual(result.ambiguity_flags, (FLAG_INVALID_FOOTER_TIME,))

    def test_none_reference_date(self):
        result = resolve_footer_timestamp(
            "Today at 04:30 م", "relative_today", None, _NY
        )
        self.assertEqual(result.ambiguity_flags, (FLAG_INVALID_REFERENCE_DATE,))

    def test_malformed_reference_date(self):
        result = resolve_footer_timestamp(
            "Today at 04:30 م", "relative_today", "24-07-2026", _NY
        )
        self.assertEqual(result.ambiguity_flags, (FLAG_INVALID_REFERENCE_DATE,))

    def test_invalid_timezone_name(self):
        result = resolve_footer_timestamp(
            "Today at 04:30 م", "relative_today", "2026-07-24", "Not/AZone"
        )
        self.assertIsNone(result.resolved_timestamp)
        self.assertEqual(result.ambiguity_flags, (FLAG_INVALID_TIMEZONE,))

    def test_none_timezone(self):
        result = resolve_footer_timestamp(
            "Today at 04:30 م", "relative_today", "2026-07-24", None
        )
        self.assertEqual(result.ambiguity_flags, (FLAG_INVALID_TIMEZONE,))

    def test_blank_timezone(self):
        result = resolve_footer_timestamp(
            "Today at 04:30 م", "relative_today", "2026-07-24", ""
        )
        self.assertEqual(result.ambiguity_flags, (FLAG_INVALID_TIMEZONE,))

    def test_valid_iana_zone_resolves(self):
        result = resolve_footer_timestamp(
            "Today at 04:30 م", "relative_today", "2026-07-24", "Asia/Riyadh"
        )
        self.assertEqual(result.resolved_timestamp.tzinfo, ZoneInfo("Asia/Riyadh"))

    def test_deterministic_repeated_call_equality(self):
        first = resolve_footer_timestamp(
            "Today at 04:30 م", "relative_today", "2026-07-24", _NY
        )
        second = resolve_footer_timestamp(
            "Today at 04:30 م", "relative_today", "2026-07-24", _NY
        )
        self.assertEqual(first, second)

    def test_never_reads_wall_clock(self):
        with patch("app.datetime_resolution.datetime", _NoWallClockDateTime), patch(
            "app.datetime_resolution.date", _NoWallClockDate
        ):
            result = resolve_footer_timestamp(
                "Today at 04:30 م", "relative_today", "2026-07-24", _NY
            )
        self.assertEqual(
            result.resolved_timestamp,
            datetime(2026, 7, 24, 16, 30, tzinfo=ZoneInfo(_NY)),
        )
        self.assertEqual(result.ambiguity_flags, ())

    def test_internal_error_never_propagates_and_preserves_raw_inputs(self):
        with patch(
            "app.datetime_resolution._construct_aware_datetime",
            side_effect=RuntimeError("unexpected internal failure"),
        ):
            result = resolve_footer_timestamp(
                "Today at 04:30 م", "relative_today", "2026-07-24", _NY
            )
        self.assertIsInstance(result, TimestampResolution)
        self.assertIsNone(result.resolved_timestamp)
        self.assertEqual(
            result.ambiguity_flags, (FLAG_DATETIME_RESOLUTION_INTERNAL_ERROR,)
        )
        self.assertEqual(result.footer_timestamp_raw, "Today at 04:30 م")
        self.assertEqual(result.footer_timestamp_kind, "relative_today")

    def test_internal_error_on_explicit_date_path_also_isolated(self):
        with patch(
            "app.datetime_resolution._construct_aware_datetime",
            side_effect=RuntimeError("unexpected internal failure"),
        ):
            result = resolve_footer_timestamp(
                "7/23/2026 11:45 PM", "explicit_date", "2026-01-01", _NY
            )
        self.assertIsNone(result.resolved_timestamp)
        self.assertEqual(
            result.ambiguity_flags, (FLAG_DATETIME_RESOLUTION_INTERNAL_ERROR,)
        )
        self.assertEqual(result.footer_timestamp_raw, "7/23/2026 11:45 PM")
        self.assertEqual(result.footer_timestamp_kind, "explicit_date")


if __name__ == "__main__":
    unittest.main()
