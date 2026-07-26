"""Deterministic date/time resolution for Discord Traders.

Recovery Milestone R4. Resolves the verbatim, unresolved values produced by
earlier Recovery milestones - app/parser.py's extract_trade_event()
expiration_raw (Milestone R3) and app/discord_adapter.py's
SegmentedMessage timestamp/footer_timestamp_kind (Milestone R2) - into
concrete calendar dates and timezone-aware datetimes, given an explicit
reference_date and timezone supplied by the caller.

This module never imports app.parser or app.discord_adapter. It accepts
app.discord_adapter's footer_timestamp_kind values ("relative_today",
"relative_yesterday", "explicit_date", "unrecognized") only by their string
contract, so this module stays independently testable and introduces no new
inter-module coupling. Both public functions are pure, deterministic, and
never raise: every input-validation branch is explicit, and one outer
safety net catches any genuinely unexpected internal error and reports it
via FLAG_DATETIME_RESOLUTION_INTERNAL_ERROR only - never conflated with an
input-defect flag. Neither function ever reads the wall clock
(datetime.now()/date.today()); every resolved value is derived solely from
the supplied reference_date, raw text, and timezone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------

FLAG_EXPIRATION_INVALID_FORMAT = "expiration_invalid_format"
FLAG_EXPIRATION_UNSUPPORTED_FORMAT = "expiration_unsupported_format"
FLAG_EXPIRATION_INVALID_CALENDAR_DATE = "expiration_invalid_calendar_date"
FLAG_INVALID_REFERENCE_DATE = "invalid_reference_date"
FLAG_INVALID_TIMEZONE = "invalid_timezone"
FLAG_FOOTER_TIMESTAMP_UNRECOGNIZED = "footer_timestamp_unrecognized"
FLAG_INVALID_FOOTER_TIME = "invalid_footer_time"
FLAG_EXPLICIT_DATE_UNSUPPORTED_FORMAT = "explicit_date_unsupported_format"
FLAG_DATETIME_RESOLUTION_INTERNAL_ERROR = "datetime_resolution_internal_error"

# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExpirationResolution:
    """Result of resolve_expiration().

    Attributes:
        expiration_raw: The original input, preserved exactly, unchanged.
        resolved_expiration: The resolved ISO8601 "YYYY-MM-DD" date string,
            or None if unresolved.
        ambiguity_flags: Zero or more FLAG_* constants from this module.
            Empty tuple only when resolved_expiration is not None.
    """

    expiration_raw: str | None
    resolved_expiration: str | None
    ambiguity_flags: tuple[str, ...]


@dataclass(frozen=True)
class TimestampResolution:
    """Result of resolve_footer_timestamp().

    Attributes:
        footer_timestamp_raw: The original raw text input, preserved
            exactly, unchanged.
        footer_timestamp_kind: The original kind input, preserved exactly,
            unchanged.
        resolved_timestamp: A timezone-aware datetime, or None if
            unresolved.
        ambiguity_flags: Zero or more FLAG_* constants from this module.
            Empty tuple only when resolved_timestamp is not None.
    """

    footer_timestamp_raw: str | None
    footer_timestamp_kind: str | None
    resolved_timestamp: datetime | None
    ambiguity_flags: tuple[str, ...]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_strict_iso_date(value: object) -> date | None:
    """Parse value as a strict "YYYY-MM-DD" calendar date, or None.

    Args:
        value: The candidate reference_date argument.

    Returns:
        The parsed date, or None if value is not a str, does not match the
        strict "YYYY-MM-DD" shape, or is not a valid calendar date.
    """
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _construct_calendar_date(year: int, month: int, day: int) -> date | None:
    """Build a calendar date, or None if it is not a valid calendar date.

    A single, mockable internal choke point for calendar-date construction,
    used by resolve_expiration()'s never-raising internal-error tests.
    """
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _construct_aware_datetime(
    year: int, month: int, day: int, hour: int, minute: int, tzinfo: ZoneInfo
) -> datetime | None:
    """Build a timezone-aware datetime, or None if it is not valid.

    A single, mockable internal choke point for datetime construction, used
    by resolve_footer_timestamp()'s never-raising internal-error tests.
    """
    try:
        return datetime(year, month, day, hour, minute, tzinfo=tzinfo)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# resolve_expiration()
# ---------------------------------------------------------------------------

_EXPIRATION_RAW_RE = re.compile(r"^(\d{1,2})/(\d{1,2})$")
_YEAR_BEARING_EXPIRATION_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$")

_EXPIRATION_ROLLOVER_THRESHOLD_DAYS = 30


def resolve_expiration(
    expiration_raw: str | None,
    reference_date: str,
) -> ExpirationResolution:
    """Resolve a bare M/D or MM/DD expiration token against a reference date.

    Rule (deterministic, documented in the R4 implementation contract):
    the first candidate is built using reference_date.year. If that
    candidate is more than 30 days before reference_date, the candidate is
    rebuilt using reference_date.year + 1; otherwise (including exactly 30
    days before, and including any candidate on or after reference_date)
    reference_date.year is kept unchanged. The wall clock is never read.

    Args:
        expiration_raw: The verbatim expiration token (e.g. "07/24" or
            "7/24"), as produced by app.parser.extract_trade_event(). Only
            bare M/D or MM/DD (no year) is supported.
        reference_date: The batch's reference date, as an ISO8601
            "YYYY-MM-DD" string. Never the wall clock.

    Returns:
        An ExpirationResolution. resolved_expiration is None whenever
        ambiguity_flags is non-empty. Never raises: an unexpected internal
        error yields ambiguity_flags == (FLAG_DATETIME_RESOLUTION_INTERNAL_ERROR,)
        exactly, never combined with or substituted by any other flag, with
        expiration_raw preserved from the original argument.
    """
    try:
        reference = _parse_strict_iso_date(reference_date)
        if reference is None:
            return ExpirationResolution(
                expiration_raw, None, (FLAG_INVALID_REFERENCE_DATE,)
            )

        if (
            not isinstance(expiration_raw, str)
            or not expiration_raw.strip()
        ):
            return ExpirationResolution(
                expiration_raw, None, (FLAG_EXPIRATION_INVALID_FORMAT,)
            )

        text = expiration_raw.strip()
        match = _EXPIRATION_RAW_RE.match(text)
        if match is None:
            if _YEAR_BEARING_EXPIRATION_RE.match(text):
                return ExpirationResolution(
                    expiration_raw, None, (FLAG_EXPIRATION_UNSUPPORTED_FORMAT,)
                )
            return ExpirationResolution(
                expiration_raw, None, (FLAG_EXPIRATION_INVALID_FORMAT,)
            )

        month, day = int(match.group(1)), int(match.group(2))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return ExpirationResolution(
                expiration_raw, None, (FLAG_EXPIRATION_INVALID_CALENDAR_DATE,)
            )

        candidate = _construct_calendar_date(reference.year, month, day)
        if candidate is None:
            return ExpirationResolution(
                expiration_raw, None, (FLAG_EXPIRATION_INVALID_CALENDAR_DATE,)
            )

        days_before = (reference - candidate).days
        resolved_year = (
            reference.year + 1
            if days_before > _EXPIRATION_ROLLOVER_THRESHOLD_DAYS
            else reference.year
        )

        if resolved_year != reference.year:
            candidate = _construct_calendar_date(resolved_year, month, day)
            if candidate is None:
                return ExpirationResolution(
                    expiration_raw,
                    None,
                    (FLAG_EXPIRATION_INVALID_CALENDAR_DATE,),
                )

        return ExpirationResolution(expiration_raw, candidate.isoformat(), ())
    except Exception:  # noqa: BLE001 - resolve_expiration() must never raise
        return ExpirationResolution(
            expiration_raw, None, (FLAG_DATETIME_RESOLUTION_INTERNAL_ERROR,)
        )


# ---------------------------------------------------------------------------
# resolve_footer_timestamp()
# ---------------------------------------------------------------------------

_KIND_RELATIVE_TODAY = "relative_today"
_KIND_RELATIVE_YESTERDAY = "relative_yesterday"
_KIND_EXPLICIT_DATE = "explicit_date"
_KNOWN_TIMESTAMP_KINDS = frozenset(
    {_KIND_RELATIVE_TODAY, _KIND_RELATIVE_YESTERDAY, _KIND_EXPLICIT_DATE}
)

_MERIDIEM_TOKEN = r"(?:AM|PM|am|pm|ص|م)"

# ص (Arabic letter SAD) = AM; م (Arabic letter MEEM) = PM. See the R4
# implementation contract §0: the source recovery plan's phrasing
# ("meridiem (`م`/`ص` treated as `pm`/`am`)") is ambiguous on the page: this
# mapping is the corrected, authoritative interpretation and the only one
# implemented here.
_MERIDIEM_MAP = {"AM": "AM", "PM": "PM", "ص": "AM", "م": "PM"}

_RELATIVE_TIMESTAMP_RE = re.compile(
    r"^(?P<relative_day>Today|Yesterday) at (?P<hour>\d{1,2}):(?P<minute>\d{2})"
    rf"(?:\s*(?P<meridiem>{_MERIDIEM_TOKEN}))?$",
    re.IGNORECASE,
)

# Explicit-date meridiem is English-only (AM/PM), per the R4 implementation
# contract's exact five enumerated forms - unlike the relative Today/
# Yesterday form, no explicit-date form authorizes ص/م.
_EXPLICIT_MERIDIEM_TOKEN = r"(?:AM|PM|am|pm)"

# Exactly five explicit-date forms are authorized by the R4 implementation
# contract - each is its own fully independent compiled regex (not three
# independently-optional components combined into one pattern), so that no
# unauthorized combination (e.g. a comma together with a two-digit year, or
# a comma together with no meridiem) can ever match. Tried in this fixed
# order; the shapes are mutually exclusive by construction (year is either
# exactly 4 or exactly 2 digits, immediately followed by the date/time
# separator), so order does not affect which one matches.
_EXPLICIT_FORM_YEAR4_RE = re.compile(  # 1. M/D/YYYY H:MM AM|PM
    r"^(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{4})\s+"
    rf"(?P<hour>\d{{1,2}}):(?P<minute>\d{{2}})\s+(?P<meridiem>{_EXPLICIT_MERIDIEM_TOKEN})$"
)
_EXPLICIT_FORM_YEAR4_COMMA_RE = re.compile(  # 2. M/D/YYYY, H:MM AM|PM
    r"^(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{4}),\s+"
    rf"(?P<hour>\d{{1,2}}):(?P<minute>\d{{2}})\s+(?P<meridiem>{_EXPLICIT_MERIDIEM_TOKEN})$"
)
_EXPLICIT_FORM_YEAR2_RE = re.compile(  # 3. M/D/YY H:MM AM|PM
    r"^(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{2})\s+"
    rf"(?P<hour>\d{{1,2}}):(?P<minute>\d{{2}})\s+(?P<meridiem>{_EXPLICIT_MERIDIEM_TOKEN})$"
)
_EXPLICIT_FORM_YEAR4_NO_MERIDIEM_RE = re.compile(  # 4. M/D/YYYY H:MM
    r"^(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{4})\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})$"
)
_EXPLICIT_FORM_YEAR2_NO_MERIDIEM_RE = re.compile(  # 5. M/D/YY H:MM
    r"^(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{2})\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})$"
)

# Exactly the five forms above, in the same numbered order as the R4
# implementation contract, and nothing else. Deliberately three unauthorized
# combinations are absent from this tuple - a two-digit year with a comma
# (with or without meridiem), and a four-digit year with a comma but no
# meridiem - so raw text in any of those shapes matches none of the five and
# correctly falls through to FLAG_EXPLICIT_DATE_UNSUPPORTED_FORMAT.
_EXPLICIT_TIMESTAMP_FORMS = (
    _EXPLICIT_FORM_YEAR4_RE,
    _EXPLICIT_FORM_YEAR4_COMMA_RE,
    _EXPLICIT_FORM_YEAR2_RE,
    _EXPLICIT_FORM_YEAR4_NO_MERIDIEM_RE,
    _EXPLICIT_FORM_YEAR2_NO_MERIDIEM_RE,
)


def _resolve_timezone(timezone: object) -> ZoneInfo | None:
    """Resolve an IANA timezone name, or None if it is invalid."""
    if not isinstance(timezone, str) or not timezone.strip():
        return None
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _normalize_meridiem(meridiem_raw: str) -> str | None:
    """Map a matched meridiem token to "AM"/"PM", or None if unrecognized."""
    return _MERIDIEM_MAP.get(meridiem_raw.upper())


def _hour_to_24h(hour: int, meridiem_raw: str | None) -> int | None:
    """Convert an hour (+ optional meridiem token) to a 24-hour-clock hour.

    Args:
        hour: The raw parsed hour digits.
        meridiem_raw: The matched meridiem token, or None if the source
            text carried no meridiem suffix (a bare 24-hour clock value).

    Returns:
        The 0-23 hour, or None if out of range for the applicable clock
        convention, or if meridiem_raw is present but unrecognized.
    """
    if meridiem_raw is None:
        return hour if 0 <= hour <= 23 else None

    meridiem = _normalize_meridiem(meridiem_raw)
    if meridiem is None or not (1 <= hour <= 12):
        return None
    if meridiem == "AM":
        return 0 if hour == 12 else hour
    return 12 if hour == 12 else hour + 12


def _parse_relative_timestamp(raw_text: str, kind: str) -> tuple[int, int] | None:
    """Parse a "Today"/"Yesterday at HH:MM [meridiem]" footer text.

    Args:
        raw_text: The stripped raw footer text.
        kind: _KIND_RELATIVE_TODAY or _KIND_RELATIVE_YESTERDAY - the
            relative-day word found in raw_text must agree with this,
            case-insensitively.

    Returns:
        A (hour24, minute) pair, or None if the text does not match, the
        relative-day word disagrees with kind, or the hour/minute/meridiem
        values are invalid.
    """
    match = _RELATIVE_TIMESTAMP_RE.match(raw_text)
    if match is None:
        return None

    expected_word = "today" if kind == _KIND_RELATIVE_TODAY else "yesterday"
    if match.group("relative_day").lower() != expected_word:
        return None

    minute = int(match.group("minute"))
    if not (0 <= minute <= 59):
        return None

    hour24 = _hour_to_24h(int(match.group("hour")), match.group("meridiem"))
    if hour24 is None:
        return None

    return hour24, minute


def _match_explicit_timestamp(raw_text: str) -> re.Match | None:
    """Match raw_text against exactly the five authorized explicit-date forms.

    Supports exactly these forms (matching the R4 implementation contract),
    with a two-digit year interpreted as 2000 + YY, matching app.parser's
    existing convention, and English-only AM/PM (no ص/م - unlike the
    relative Today/Yesterday form, no explicit-date form authorizes them):
        1. M/D/YYYY H:MM AM|PM
        2. M/D/YYYY, H:MM AM|PM
        3. M/D/YY H:MM AM|PM
        4. M/D/YYYY H:MM            (no meridiem, 24-hour clock)
        5. M/D/YY H:MM              (no meridiem, 24-hour clock)

    Each form is tried as its own fully independent compiled regex (see
    _EXPLICIT_TIMESTAMP_FORMS), not as independently-optional components of
    one combined pattern - so a two-digit year with a comma, or a
    four-digit year with a comma but no meridiem, match none of the five
    and are correctly rejected rather than silently accepted.

    Args:
        raw_text: The stripped raw footer text.

    Returns:
        The regex Match from whichever of the five forms matched, or None
        if raw_text matches none of them (the caller reports
        FLAG_EXPLICIT_DATE_UNSUPPORTED_FORMAT in that case).
    """
    for pattern in _EXPLICIT_TIMESTAMP_FORMS:
        match = pattern.match(raw_text)
        if match is not None:
            return match
    return None


def _explicit_timestamp_components(
    match: re.Match,
) -> tuple[int, int, int, int, int] | None:
    """Extract and validate an explicit-date match's numeric components.

    Args:
        match: A Match already produced by _match_explicit_timestamp().

    Returns:
        A (year, month, day, hour24, minute) tuple, or None if the matched
        year/month/day/hour/minute/meridiem values are not valid (the
        caller reports FLAG_INVALID_FOOTER_TIME in that case).
    """
    groups = match.groupdict()
    month = int(groups["month"])
    day = int(groups["day"])
    year_text = groups["year"]
    year = int(year_text) if len(year_text) == 4 else 2000 + int(year_text)
    minute = int(groups["minute"])

    if not (1 <= month <= 12 and 1 <= day <= 31 and 0 <= minute <= 59):
        return None

    # The two no-meridiem forms (4 and 5) have no "meridiem" named group at
    # all - groupdict().get() returns None for them safely, distinct from
    # a meridiem-bearing form whose group matched but was itself invalid
    # (which cannot happen here, since each form's meridiem group is
    # restricted to exactly AM|PM|am|pm by construction).
    hour24 = _hour_to_24h(int(groups["hour"]), groups.get("meridiem"))
    if hour24 is None:
        return None

    return year, month, day, hour24, minute


def resolve_footer_timestamp(
    footer_timestamp_raw: str | None,
    footer_timestamp_kind: str | None,
    reference_date: str,
    timezone: str,
) -> TimestampResolution:
    """Resolve a Discord footer timestamp into a timezone-aware datetime.

    footer_timestamp_kind is accepted by string-value contract with
    app.discord_adapter's four kind values ("relative_today",
    "relative_yesterday", "explicit_date", "unrecognized") - this module
    never imports app.discord_adapter. "Today"/"Yesterday" matching and
    English AM/PM matching are both case-insensitive; ص = AM and م = PM
    (see the module-level _MERIDIEM_MAP comment for the corrected,
    authoritative interpretation of the source plan's ambiguous phrasing).
    Without a meridiem suffix, the hour is read on a 24-hour clock. The
    wall clock is never read.

    Args:
        footer_timestamp_raw: The verbatim footer timestamp text (e.g.
            "Today at 04:30 م"), as produced by
            app.discord_adapter.SegmentedMessage.footer_timestamp_raw.
        footer_timestamp_kind: One of app.discord_adapter's four kind
            values, by string contract only.
        reference_date: The batch's reference date, as an ISO8601
            "YYYY-MM-DD" string. Used only for "Today"/"Yesterday"; an
            explicit-date footer carries its own year and does not use it.
        timezone: An IANA timezone name (e.g. "America/New_York"),
            resolved via zoneinfo.ZoneInfo. Never the wall clock's local
            timezone.

    Returns:
        A TimestampResolution. resolved_timestamp is None whenever
        ambiguity_flags is non-empty. Never raises: an unexpected internal
        error yields ambiguity_flags == (FLAG_DATETIME_RESOLUTION_INTERNAL_ERROR,)
        exactly, never combined with or substituted by any other flag, with
        footer_timestamp_raw and footer_timestamp_kind both preserved from
        the original arguments.
    """
    try:
        reference = _parse_strict_iso_date(reference_date)
        if reference is None:
            return TimestampResolution(
                footer_timestamp_raw,
                footer_timestamp_kind,
                None,
                (FLAG_INVALID_REFERENCE_DATE,),
            )

        tzinfo = _resolve_timezone(timezone)
        if tzinfo is None:
            return TimestampResolution(
                footer_timestamp_raw,
                footer_timestamp_kind,
                None,
                (FLAG_INVALID_TIMEZONE,),
            )

        if (
            not isinstance(footer_timestamp_kind, str)
            or footer_timestamp_kind not in _KNOWN_TIMESTAMP_KINDS
        ):
            return TimestampResolution(
                footer_timestamp_raw,
                footer_timestamp_kind,
                None,
                (FLAG_FOOTER_TIMESTAMP_UNRECOGNIZED,),
            )

        if (
            not isinstance(footer_timestamp_raw, str)
            or not footer_timestamp_raw.strip()
        ):
            return TimestampResolution(
                footer_timestamp_raw,
                footer_timestamp_kind,
                None,
                (FLAG_INVALID_FOOTER_TIME,),
            )

        text = footer_timestamp_raw.strip()

        if footer_timestamp_kind in (_KIND_RELATIVE_TODAY, _KIND_RELATIVE_YESTERDAY):
            parsed = _parse_relative_timestamp(text, footer_timestamp_kind)
            if parsed is None:
                return TimestampResolution(
                    footer_timestamp_raw,
                    footer_timestamp_kind,
                    None,
                    (FLAG_INVALID_FOOTER_TIME,),
                )
            hour24, minute = parsed
            target_date = (
                reference
                if footer_timestamp_kind == _KIND_RELATIVE_TODAY
                else reference - timedelta(days=1)
            )
            resolved = _construct_aware_datetime(
                target_date.year, target_date.month, target_date.day,
                hour24, minute, tzinfo,
            )
            if resolved is None:
                return TimestampResolution(
                    footer_timestamp_raw,
                    footer_timestamp_kind,
                    None,
                    (FLAG_INVALID_FOOTER_TIME,),
                )
            return TimestampResolution(
                footer_timestamp_raw, footer_timestamp_kind, resolved, ()
            )

        # footer_timestamp_kind == _KIND_EXPLICIT_DATE
        explicit_match = _match_explicit_timestamp(text)
        if explicit_match is None:
            return TimestampResolution(
                footer_timestamp_raw,
                footer_timestamp_kind,
                None,
                (FLAG_EXPLICIT_DATE_UNSUPPORTED_FORMAT,),
            )
        components = _explicit_timestamp_components(explicit_match)
        if components is None:
            return TimestampResolution(
                footer_timestamp_raw,
                footer_timestamp_kind,
                None,
                (FLAG_INVALID_FOOTER_TIME,),
            )
        year, month, day, hour24, minute = components
        resolved = _construct_aware_datetime(year, month, day, hour24, minute, tzinfo)
        if resolved is None:
            return TimestampResolution(
                footer_timestamp_raw,
                footer_timestamp_kind,
                None,
                (FLAG_INVALID_FOOTER_TIME,),
            )
        return TimestampResolution(
            footer_timestamp_raw, footer_timestamp_kind, resolved, ()
        )
    except Exception:  # noqa: BLE001 - resolve_footer_timestamp() must never raise
        return TimestampResolution(
            footer_timestamp_raw,
            footer_timestamp_kind,
            None,
            (FLAG_DATETIME_RESOLUTION_INTERNAL_ERROR,),
        )
