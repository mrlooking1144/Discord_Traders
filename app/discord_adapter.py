"""Discord-specific segmentation and noise-stripping adapter.

Recovery Milestone R2. Turns a pasted Discord channel-history block (many
alerts, copy-pasted as one blob of text) into a list of individual raw
messages, each preserved verbatim, plus adapter metadata (trader/author,
timestamp text, channel tags, sequence in batch, and the deterministic
inputs a later milestone hashes into a synthetic message ID when no real
Discord message ID is available).

This module is intentionally the *only* place that knows about Discord's
specific export quirks (the "APP" bot label, the em-dash timestamp line,
the bullet-separated "Trader-<relative or explicit timestamp>" footer, the
Arabic PM/AM markers "م"/"ص", zero-width-joiner-wrapped channel tags).
app/parser.py remains source-agnostic and only ever receives this module's
cleaned_text output - never raw Discord wrapper text - per its own
source-independence tests.

No date/year resolution, no ingestion, no lifecycle linking, no database
access happens here. Segmentation is driven entirely by structural line
patterns (footer lines close a message; a three-line header block opens
one), never by guessing based on message content. Timestamp *text* is
preserved verbatim; it is never resolved to an absolute datetime here -
that is a later milestone's job, once a reference date/timezone is
available.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A Discord "compact" export collapses the username/avatar header for
# consecutive messages from the same author, but still describes each
# individual message with its own footer: "<Trader>•<remainder>". The
# bullet is Discord's own separator and never appears in trade-alert
# content, so its presence is a safe, unambiguous segmentation boundary
# regardless of which specific timestamp phrasing follows it - this is
# what lets an unrecognized timestamp form still close a message (flagged
# ambiguous) rather than silently merging into the next one.
_FOOTER_LINE_RE = re.compile(r"^(?P<trader>\S.*?)•(?P<remainder>.+)$")

# Known footer timestamp phrasings. Meridiem is optional (some locales/
# copy exports show a 24-hour clock with no AM/PM/ص/م suffix at all).
_MERIDIEM_TOKEN = r"(?:AM|PM|am|pm|ص|م)"

_RELATIVE_DAY_RE = re.compile(
    rf"^(?P<relative_day>Today|Yesterday) at (?P<hour>\d{{1,2}}):(?P<minute>\d{{2}})"
    rf"(?:\s*(?P<meridiem>{_MERIDIEM_TOKEN}))?$"
)

# Discord's copy/export falls back to an explicit date once a message is
# old enough that "Today"/"Yesterday" no longer applies, e.g.
# "07/23/2026 11:45 PM" or "07/23/2026, 11:45 PM".
_EXPLICIT_DATE_RE = re.compile(
    rf"^(?P<month>\d{{1,2}})/(?P<day>\d{{1,2}})/(?P<year>\d{{2,4}}),?\s+"
    rf"(?P<hour>\d{{1,2}}):(?P<minute>\d{{2}})"
    rf"(?:\s*(?P<meridiem>{_MERIDIEM_TOKEN}))?$"
)

KIND_RELATIVE_TODAY = "relative_today"
KIND_RELATIVE_YESTERDAY = "relative_yesterday"
KIND_EXPLICIT_DATE = "explicit_date"
KIND_UNRECOGNIZED = "unrecognized"


def _classify_footer_timestamp(remainder: str) -> str:
    """Classify a footer's timestamp phrasing, without resolving it.

    Args:
        remainder: The footer text after the trader name and bullet (e.g.
            "Today at 04:30 م", "Yesterday at 11:45 PM",
            "07/23/2026 11:45 PM").

    Returns:
        One of KIND_RELATIVE_TODAY, KIND_RELATIVE_YESTERDAY,
        KIND_EXPLICIT_DATE, or KIND_UNRECOGNIZED. Never raises - an
        unrecognized phrasing is a normal, expected outcome that callers
        surface as an ambiguity flag rather than a failure.
    """
    match = _RELATIVE_DAY_RE.match(remainder)
    if match is not None:
        return (
            KIND_RELATIVE_TODAY
            if match.group("relative_day") == "Today"
            else KIND_RELATIVE_YESTERDAY
        )
    if _EXPLICIT_DATE_RE.match(remainder) is not None:
        return KIND_EXPLICIT_DATE
    return KIND_UNRECOGNIZED


# A full header block is exactly three lines: the trader name, the literal
# "APP" bot label, and an em-dash-prefixed time (e.g. " — 04:30 م",
# where م is the Arabic letter meem used here as a PM marker).
_APP_LABEL = "APP"
_HEADER_TIMESTAMP_RE = re.compile(r"^—\s*(?P<rest>.+)$")

# A channel-tag line is an emoji (any non-whitespace run) followed by
# U+FE31 (PRESENTATION FORM FOR VERTICAL EM DASH) and a slug, optionally
# wrapped in invisible characters (U+2060 WORD JOINER, U+200B ZERO WIDTH
# SPACE, U+FE0F VARIATION SELECTOR-16). This character combination is
# unique to channel-tag lines in Discord exports - it never appears in
# trade-alert content - so it is a safe, unambiguous detector. This
# pattern is only ever applied when building cleaned_text; raw_text is
# never passed through it, so these wrapper characters are never stripped
# from the preserved original.
_CHANNEL_TAG_RE = re.compile(
    r"^[⁠​️]*\S+?︱(?P<slug>[\w-]+)[⁠​️]*$"
)

FLAG_MISSING_HEADER_NO_PRIOR_TRADER = "missing_header_no_prior_trader"
FLAG_FOOTER_TRADER_MISMATCH = "footer_trader_mismatch"
FLAG_MISSING_FOOTER = "missing_footer"
FLAG_EMPTY_BODY = "empty_body"
FLAG_UNRECOGNIZED_FOOTER_TIMESTAMP_FORM = "unrecognized_footer_timestamp_form"

_SYNTHETIC_ID_FIELD_SEPARATOR = "\x1f"


@dataclass(frozen=True)
class SegmentedMessage:
    """One segmented Discord alert, extracted from a pasted batch.

    Attributes:
        sequence_in_batch: 1-indexed position of this message among all
            messages segmented from the same batch, in paste order.
        trader_raw: The trader/author name for this message, exactly as it
            appeared in its own header line, or inherited from the nearest
            preceding message when this one has no header of its own
            (Discord collapses repeated headers for consecutive
            same-author messages). None only if the very first message in
            the batch has no header and there is nothing to inherit.
        header_present: True if this message had its own
            "<Trader>/APP/-time" header block.
        header_timestamp_raw: The header's own timestamp text, exactly as
            it appeared (e.g. "04:30 م"), or None if header_present is
            False. Preserved verbatim, never resolved to a date.
        footer_present: True if this message had a recognizable
            "<Trader>•<remainder>" footer line. False only for a
            trailing, truncated paste with no footer at all.
        footer_trader_raw: The trader name exactly as it appeared in this
            message's own footer line, or None if footer_present is False.
            May differ from trader_raw only in case (e.g. "Matae" header,
            "matae" footer) - that is expected, not an error.
        footer_timestamp_raw: The footer's timestamp text exactly as it
            appeared after the bullet (e.g. "Today at 04:30 م",
            "Yesterday at 11:45 PM", "07/23/2026 11:45 PM"), or None if
            footer_present is False. Preserved verbatim; never resolved to
            an absolute date/time here.
        footer_timestamp_kind: One of KIND_RELATIVE_TODAY,
            KIND_RELATIVE_YESTERDAY, KIND_EXPLICIT_DATE, or
            KIND_UNRECOGNIZED, or None if footer_present is False. An
            unrecognized phrasing does not prevent segmentation (the
            footer's bullet structure alone is enough to close the
            message) - it only raises FLAG_UNRECOGNIZED_FOOTER_TIMESTAMP_FORM.
        timestamp_text: This message's own timestamp text, preferring
            footer_timestamp_raw (more reliable per-message) and falling
            back to header_timestamp_raw when no footer was found. None if
            neither is present.
        channel_tags: Channel slugs found in this message, in the order
            they appeared (e.g. ["analyst-tc"], or ["twi-account",
            "pro-alerts"] for a message cross-posted to two channels).
            Never deduplicated - repeated tags are preserved as found.
        raw_text: The exact original text of this message, reconstructed
            by joining the original lines verbatim (including their
            original line terminators, and every invisible character such
            as U+2060/U+200B/U+FE0F) - never stripped, normalized, or
            otherwise altered, aside from excluding purely blank lines at
            the very start/end of the message's span (inter-message paste
            whitespace, not message content). Wrapper characters are
            stripped only when building cleaned_text, never here.
        cleaned_text: A working copy for parsing: raw_text with the header
            block (if present), channel-tag lines, the footer line, and
            blank lines removed. Every other line - action lines, restated
            contract summaries, position-size lines, price-arrow lines,
            and free-text commentary (e.g. "HIT STOP") - is preserved
            verbatim and in order. Never written back to storage.
        ambiguity_flags: Zero or more of the FLAG_* constants in this
            module, recording segmentation-time uncertainty for human
            review rather than silently guessing.
        synthetic_id_input: A deterministic string combining this
            message's stable identity inputs (primary channel tag, trader,
            timestamp text, cleaned body, and an occurrence index - see
            below) - never wall-clock time or randomness. A later
            milestone hashes this into a synthetic external_id when no
            real Discord message ID is available.

            The occurrence index disambiguates two otherwise-identical
            messages (same channel/trader/timestamp/body) appearing in
            one batch by counting, in paste order, how many times that
            exact combination has already been seen - the first is index
            0, the second index 1, and so on. This is stable across a
            later paste of the same or an overlapping history *provided
            the overlap includes every earlier occurrence of that same
            combination*: since Discord history order never changes, the
            Nth occurrence remains the Nth occurrence in any paste that
            starts at or before it. It is an unavoidable limitation - not
            a bug - that a partial re-paste starting strictly *between*
            two identical-looking messages will assign the second one a
            different occurrence index (0 instead of 1), and therefore a
            different synthetic id, than it received in the original
            full paste. This is exactly why a real Discord message ID,
            when available, must always be preferred over this fallback;
            resolving it is left entirely to a later ingestion milestone.
    """

    sequence_in_batch: int
    trader_raw: str | None
    header_present: bool
    header_timestamp_raw: str | None
    footer_present: bool
    footer_trader_raw: str | None
    footer_timestamp_raw: str | None
    footer_timestamp_kind: str | None
    timestamp_text: str | None
    channel_tags: list[str]
    raw_text: str
    cleaned_text: str
    ambiguity_flags: list[str]
    synthetic_id_input: str


def _is_blank(line: str) -> bool:
    """Return True if line is empty or contains only whitespace."""
    return line.strip() == ""


def _match_header(span_lines: list[str]) -> re.Match | None:
    """Check whether span_lines opens with a full "<Trader>/APP/-time" block.

    Args:
        span_lines: The trimmed lines of one message's span.

    Returns:
        The _HEADER_TIMESTAMP_RE match for the third line if a full header
        block is present, or None if not (too few lines, second line isn't
        "APP", or the third line doesn't match the header timestamp
        pattern).
    """
    if len(span_lines) < 3:
        return None
    if span_lines[1].strip() != _APP_LABEL:
        return None
    return _HEADER_TIMESTAMP_RE.match(span_lines[2].strip())


def _build_synthetic_id_input(
    channel_tags: list[str],
    trader_raw: str | None,
    timestamp_text: str | None,
    cleaned_text: str,
    occurrence_index: int,
) -> str:
    """Deterministically combine a message's stable identity inputs.

    Args:
        channel_tags: This message's channel slugs, in order.
        trader_raw: This message's resolved trader name, or None.
        timestamp_text: This message's timestamp text, or None.
        cleaned_text: This message's cleaned body text.
        occurrence_index: 0-based count of how many earlier messages in
            this same batch shared this exact (channel, trader, timestamp,
            cleaned_text) combination - see SegmentedMessage.synthetic_id_input.

    Returns:
        A single string combining the inputs with a field separator
        unlikely to appear in real text. Deterministic: identical inputs
        always produce an identical result, with no wall-clock or random
        component.
    """
    primary_channel = channel_tags[0] if channel_tags else ""
    return _SYNTHETIC_ID_FIELD_SEPARATOR.join(
        (
            primary_channel,
            trader_raw or "",
            timestamp_text or "",
            cleaned_text,
            str(occurrence_index),
        )
    )


def segment_discord_batch(raw_batch_text: str) -> list[SegmentedMessage]:
    """Segment a pasted Discord channel-history block into raw messages.

    Segmentation is driven by footer lines ("<Trader>•<remainder>"), which
    close exactly one message each - never by blank lines, since alerts
    contain internal blank lines, and never by whether the remainder's
    timestamp phrasing is one this module recognizes (an unrecognized
    phrasing still closes the message; it only raises
    FLAG_UNRECOGNIZED_FOOTER_TIMESTAMP_FORM). A trailing chunk of
    non-blank text with no footer at all (a truncated paste) still becomes
    one final message, flagged FLAG_MISSING_FOOTER rather than being
    dropped.

    Args:
        raw_batch_text: The complete pasted block, exactly as the user
            supplied it.

    Returns:
        One SegmentedMessage per detected alert, in paste order. Returns
        an empty list for blank/whitespace-only input.
    """
    lines = raw_batch_text.splitlines(keepends=True)

    footer_indices = [
        i for i, line in enumerate(lines) if _FOOTER_LINE_RE.match(line.strip())
    ]

    spans: list[tuple[int, int | None]] = []
    cursor = 0
    for footer_index in footer_indices:
        spans.append((cursor, footer_index))
        cursor = footer_index + 1
    if cursor < len(lines) and any(not _is_blank(line) for line in lines[cursor:]):
        spans.append((cursor, None))

    messages: list[SegmentedMessage] = []
    previous_trader: str | None = None
    sequence = 0
    identity_occurrence_counts: dict[tuple, int] = {}

    for span_start, footer_index in spans:
        span_end = footer_index if footer_index is not None else len(lines) - 1

        start, end = span_start, span_end
        while start <= end and _is_blank(lines[start]):
            start += 1
        while end >= start and _is_blank(lines[end]):
            end -= 1
        if start > end:
            continue

        span_lines = lines[start : end + 1]
        ambiguity_flags: list[str] = []

        header_match = _match_header(span_lines)
        if header_match is not None:
            header_present = True
            trader_raw = span_lines[0].strip()
            header_timestamp_raw = header_match.group("rest")
            body_start = 3
        else:
            header_present = False
            trader_raw = previous_trader
            header_timestamp_raw = None
            body_start = 0
            if trader_raw is None:
                ambiguity_flags.append(FLAG_MISSING_HEADER_NO_PRIOR_TRADER)

        footer_present = footer_index is not None
        footer_trader_raw: str | None = None
        footer_timestamp_raw: str | None = None
        footer_timestamp_kind: str | None = None
        if footer_present:
            footer_match = _FOOTER_LINE_RE.match(span_lines[-1].strip())
            footer_trader_raw = footer_match.group("trader")
            footer_timestamp_raw = footer_match.group("remainder")
            footer_timestamp_kind = _classify_footer_timestamp(footer_timestamp_raw)
            if footer_timestamp_kind == KIND_UNRECOGNIZED:
                ambiguity_flags.append(FLAG_UNRECOGNIZED_FOOTER_TIMESTAMP_FORM)
            body_end = len(span_lines) - 1
        else:
            body_end = len(span_lines)
            ambiguity_flags.append(FLAG_MISSING_FOOTER)

        if (
            footer_trader_raw is not None
            and trader_raw is not None
            and footer_trader_raw.strip().lower() != trader_raw.strip().lower()
        ):
            ambiguity_flags.append(FLAG_FOOTER_TRADER_MISMATCH)

        body_lines = span_lines[body_start:body_end]
        channel_tags = [
            match.group("slug")
            for line in body_lines
            if (match := _CHANNEL_TAG_RE.match(line.strip()))
        ]
        cleaned_lines = [
            line.rstrip("\r\n")
            for line in body_lines
            if not _is_blank(line) and not _CHANNEL_TAG_RE.match(line.strip())
        ]
        cleaned_text = "\n".join(cleaned_lines)
        if not cleaned_text.strip():
            ambiguity_flags.append(FLAG_EMPTY_BODY)

        timestamp_text = footer_timestamp_raw or header_timestamp_raw
        raw_text = "".join(span_lines)

        identity_key = (
            channel_tags[0] if channel_tags else "",
            trader_raw,
            timestamp_text,
            cleaned_text,
        )
        occurrence_index = identity_occurrence_counts.get(identity_key, 0)
        identity_occurrence_counts[identity_key] = occurrence_index + 1

        sequence += 1
        messages.append(
            SegmentedMessage(
                sequence_in_batch=sequence,
                trader_raw=trader_raw,
                header_present=header_present,
                header_timestamp_raw=header_timestamp_raw,
                footer_present=footer_present,
                footer_trader_raw=footer_trader_raw,
                footer_timestamp_raw=footer_timestamp_raw,
                footer_timestamp_kind=footer_timestamp_kind,
                timestamp_text=timestamp_text,
                channel_tags=channel_tags,
                raw_text=raw_text,
                cleaned_text=cleaned_text,
                ambiguity_flags=ambiguity_flags,
                synthetic_id_input=_build_synthetic_id_input(
                    channel_tags, trader_raw, timestamp_text, cleaned_text, occurrence_index
                ),
            )
        )
        previous_trader = trader_raw

    return messages
