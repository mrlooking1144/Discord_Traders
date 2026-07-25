"""Source-independent trade message parser for Discord Traders.

Converts raw pasted trade message text into structured trade-signal dicts
matching the fields TradeService.ingest_message() expects for its
trade_signals argument: symbol, action, option_type, price, expiration,
position_size. This module has no knowledge of any specific source (Discord,
Telegram, etc.), performs no database access, no networking, and never calls
TradeService or any UI code - it is a pure function of the message text.

V1 line grammar (one signal per non-blank line; a line that does not start
with a recognized action keyword is skipped and contributes no signal):

    ACTION SYMBOL [STRIKE][C|P|CALL|PUT] [MM/DD/YYYY] [@PRICE] [free text...]

- ACTION: one of BTO, STC, BTC, STO (buy/sell to open/close), or BUY, SELL,
  case-insensitive, normalized to upper case. The action is preserved exactly
  as entered (uppercased) - BUY is never mapped to BTO, and SELL is never
  mapped to STC.
- SYMBOL: 1-5 letters, case-insensitive, normalized to upper case.
- Option marker: an optional leading strike quantity immediately followed by
  C/P (or the words CALL/PUT). Only the call/put designation is captured as
  option_type - the frozen V1 schema has no strike column
  (docs/DATABASE_DESIGN_V1.md Section 3), so any strike number in this token
  is not stored separately.
- Expiration: a date with an explicit year, MM/DD/YYYY or MM/DD/YY (two-digit
  years are treated as 20YY), normalized to an ISO8601 date string. A bare
  date with no year at all (e.g. "7/19") is intentionally left unparsed
  rather than guessed, since inferring the year would require reading the
  wall clock - inconsistent with this project's no-hidden-clock rule (see
  TradeService.ingest_message's reference_time argument).
- Price: a token prefixed with '@' or '$', parsed as a Decimal - never a
  float, per docs/DATABASE_DESIGN_V1.md Section 3.
- Anything left on the line after the tokens above are consumed is kept
  verbatim, in order, as position_size (e.g. "half position", "10
  contracts"), per the schema's "raw wording, not normalized" note.

Everything not recognized above - a line with no matching action keyword, or
an empty/whitespace message - contributes no signal. parse_message() never
raises on malformed input; it simply returns fewer results.

Recovery Milestone R3 adds a second, independent function to this module:
extract_trade_event(). It targets a different shape of input - one Discord
alert's cleaned_text (as produced by app/discord_adapter.py's
segment_discord_batch(), which may span several lines describing a single
trade event: an action line, a duplicated bare contract-restatement line,
a bare fraction/"ALL OUT" confirmation line, a "$OLD -> $NEW (+NN%)"
stated-return line, and free-text commentary) rather than parse_message()'s
one-signal-per-independent-line shape. extract_trade_event() is still fully
source-agnostic - it has no knowledge of Discord, never imports
app/discord_adapter.py, and is a pure function of the text it is given, in
keeping with this module's source-independence tests. parse_message()
itself is completely unchanged by this addition - every existing behavior,
including its exact return-dict key set, is preserved byte-for-byte.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_ACTION_RE = re.compile(r"^(BTO|STC|BTC|STO|BUY|SELL)$", re.IGNORECASE)
_SYMBOL_RE = re.compile(r"^[A-Za-z]{1,5}$")
_OPTION_RE = re.compile(r"^\d*\.?\d*(C|P|CALL|PUT)$", re.IGNORECASE)
_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})$")
_PRICE_RE = re.compile(r"^[@$](\d+\.?\d*)$")


def parse_message(raw_text: str) -> list[dict]:
    """Parse raw trade message text into structured trade-signal dicts.

    Args:
        raw_text: The original pasted message text, verbatim.

    Returns:
        A list of dicts, one per detected signal, each with keys "symbol",
        "action", "option_type", "price", "expiration", "position_size".
        Optional fields not found on that line are None. Returns an empty
        list if no signal is detected anywhere in the message; never raises.
    """
    if not raw_text or not raw_text.strip():
        return []

    signals = []
    for line in raw_text.splitlines():
        signal = _parse_line(line)
        if signal is not None:
            signals.append(signal)
    return signals


def _parse_line(line: str) -> dict | None:
    """Parse a single line into one trade-signal dict, or None if it does
    not start with a recognized action keyword or has no symbol token.
    """
    tokens = line.strip().split()
    if not tokens:
        return None

    action_match = _ACTION_RE.match(tokens[0])
    if not action_match:
        return None
    action = tokens[0].upper()

    remaining = tokens[1:]
    if not remaining:
        return None

    symbol_token = remaining[0]
    if not _SYMBOL_RE.match(symbol_token):
        return None
    symbol = symbol_token.upper()

    option_type: str | None = None
    price: Decimal | None = None
    expiration: str | None = None
    leftover_tokens: list[str] = []

    for token in remaining[1:]:
        if option_type is None:
            option_match = _OPTION_RE.match(token)
            if option_match:
                suffix = option_match.group(1).upper()
                option_type = "put" if suffix in ("P", "PUT") else "call"
                continue

        if expiration is None:
            date_match = _DATE_RE.match(token)
            if date_match:
                normalized = _normalize_date(date_match)
                if normalized is not None:
                    expiration = normalized
                    continue

        if price is None:
            price_match = _PRICE_RE.match(token)
            if price_match:
                try:
                    price = Decimal(price_match.group(1))
                except InvalidOperation:
                    pass
                else:
                    continue

        leftover_tokens.append(token)

    position_size = " ".join(leftover_tokens).strip() or None

    return {
        "symbol": symbol,
        "action": action,
        "option_type": option_type,
        "price": price,
        "expiration": expiration,
        "position_size": position_size,
    }


def _normalize_date(match: re.Match) -> str | None:
    """Normalize a matched MM/DD/YYYY or MM/DD/YY date to ISO8601, or None
    if the month/day values are out of range.
    """
    month_s, day_s, year_s = match.groups()
    month, day = int(month_s), int(day_s)
    year = int(year_s) if len(year_s) == 4 else 2000 + int(year_s)

    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None

    return f"{year:04d}-{month:02d}-{day:02d}"


# ---------------------------------------------------------------------------
# Recovery Milestone R3: extract_trade_event()
#
# Grammar for one Discord alert's cleaned_text (see the module docstring):
#
#     ACTION SYMBOL [STRIKE][C|P|CALL|PUT] [MM/DD[/YYYY]] [@|$PRICE]
#         [ [ANNOTATION] | FRACTION | ALL OUT ]
#     <optional duplicate/confirmation lines - discarded, not double-counted>
#     <optional "$OLD -> $NEW (+NN%)" line - captured as stated entry/return>
#     <optional free-text commentary lines - captured as notes>
#
# This grammar is independent of parse_message()'s and uses its own regex
# constants throughout, so parse_message() cannot be affected by it.
# ---------------------------------------------------------------------------

PARSER_VERSION = "v2"

FLAG_EXPIRATION_YEAR_MISSING = "expiration_year_missing"
FLAG_STRIKE_MISSING = "strike_missing"
FLAG_QUALIFIER_MISSING = "qualifier_missing"
FLAG_STATED_RETURN_MISSING = "stated_return_missing"
FLAG_ACTION_QUALIFIER_CONFLICT = "action_qualifier_conflict"

PARSE_STATUS_PARSED = "parsed"
PARSE_STATUS_PARTIALLY_PARSED = "partially_parsed"
PARSE_STATUS_UNRECOGNIZED = "unrecognized"
PARSE_STATUS_FAILED = "failed"

_EXTRACTOR_OPEN_ACTIONS = frozenset({"BOUGHT", "BTO", "BUY", "STO"})
_EXTRACTOR_CLOSE_ACTIONS = frozenset({"SOLD", "STC", "BTC", "SELL"})
_EXTRACTOR_ACTION_RE = re.compile(
    r"^(BOUGHT|SOLD|BTO|STC|BTC|STO|BUY|SELL)$", re.IGNORECASE
)
_EXTRACTOR_SYMBOL_RE = re.compile(r"^[A-Za-z]{1,5}$")
_EXTRACTOR_OPTION_RE = re.compile(r"^(\d+(?:\.\d+)?)?(C|P|CALL|PUT)$", re.IGNORECASE)
_EXTRACTOR_DATE_WITH_YEAR_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})$")
_EXTRACTOR_DATE_NO_YEAR_RE = re.compile(r"^(\d{1,2})/(\d{1,2})$")
_EXTRACTOR_PRICE_RE = re.compile(r"^[@$](\d+\.\d+|\.\d+|\d+)$")
_EXTRACTOR_FRACTION_RE = re.compile(r"^(1/2|1/3|1/4|1/6|1/8|1/16)$")
_EXTRACTOR_BRACKET_RE = re.compile(r"\[([^\]]+)\]")
_EXTRACTOR_PRICE_TOKEN = r"\d+\.\d+|\.\d+|\d+"
_EXTRACTOR_PRICE_ARROW_RE = re.compile(
    rf"^\$(?P<old>{_EXTRACTOR_PRICE_TOKEN})\s*(?:→|->)\s*"
    rf"\$(?P<new>{_EXTRACTOR_PRICE_TOKEN})\s*\((?P<pct>[+-]\d+(?:\.\d+)?)%\)$"
)

_EXTRACTOR_RESULT_KEYS = (
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
)


def _empty_extractor_result(parse_status: str) -> dict:
    """Build an extract_trade_event() result with every field empty/None.

    Args:
        parse_status: One of the PARSE_STATUS_* constants.

    Returns:
        A dict with all _EXTRACTOR_RESULT_KEYS present, every trade field
        None, ambiguity_flags an empty list, and parse_status as given.
    """
    result = {key: None for key in _EXTRACTOR_RESULT_KEYS}
    result["parse_status"] = parse_status
    result["ambiguity_flags"] = []
    return result


def _extract_bracket_annotation(line: str) -> tuple[str | None, str]:
    """Pull a "[...]" annotation out of a line before whitespace-tokenizing it.

    Args:
        line: One line of cleaned alert text.

    Returns:
        A (annotation, remainder) pair: annotation is the bracketed text
        including its brackets (e.g. "[B GRADE]"), or None if no bracket is
        present; remainder is the line with that bracket (if any) removed,
        ready for whitespace tokenizing without the annotation's internal
        space splitting it into two tokens.
    """
    match = _EXTRACTOR_BRACKET_RE.search(line)
    if match is None:
        return None, line
    annotation = f"[{match.group(1)}]"
    remainder = _EXTRACTOR_BRACKET_RE.sub(" ", line, count=1)
    return annotation, remainder


def _derive_event_type(
    action: str, annotation: str | None, close_qualifier: str | None
) -> str | None:
    """Classify a single trade event's lifecycle role from its own fields.

    Classification is by opening/closing role, not by buy/sell wording -
    BTO, BUY, BOUGHT, and STO all open a position (STO opens a short);
    STC, SELL, SOLD, and BTC all close one (BTC closes a short). There is
    no separate short-entry event type, so STO is classified via the same
    annotation-based rule as the other opening verbs, and BTC via the
    same qualifier-based rule as the other closing verbs.

    An opening verb combined with a closing-only qualifier (a fraction or
    "ALL OUT") is semantically contradictory - e.g. "STO ... ALL OUT"
    cannot be an entry, since there is nothing open yet to be "all out"
    of - and no approved event type represents it, so it is never
    guessed as ENTRY, ADD, ROLL_UP, PARTIAL_EXIT, or FULL_EXIT. This
    returns None instead, and the caller (extract_trade_event()) adds
    FLAG_ACTION_QUALIFIER_CONFLICT so the alert is preserved for review
    rather than silently misclassified.

    Only ENTRY, ADD, ROLL_UP, PARTIAL_EXIT, and FULL_EXIT are ever
    returned for a non-contradictory alert - a stop-out is a FULL_EXIT
    whose "HIT STOP" reason is preserved in notes, not a separate event
    type. This classification needs no cross-message lifecycle context,
    so it is computed here rather than deferred to a later milestone.

    Args:
        action: The exact action verb, as extracted (e.g. "BOUGHT").
        annotation: The bracketed annotation text, including brackets
            (e.g. "[ADD]"), or None.
        close_qualifier: The fraction text or "ALL OUT" pulled from the
            action line, or None - tracked independently of annotation
            so a conflicting combination of the two is still detectable.

    Returns:
        One of "ENTRY", "ADD", "ROLL_UP", "PARTIAL_EXIT", "FULL_EXIT", or
        None if: action is not a recognized opening/closing verb; an
        opening verb has a conflicting close_qualifier (see
        FLAG_ACTION_QUALIFIER_CONFLICT); or a closing verb has no
        close_qualifier at all (see FLAG_QUALIFIER_MISSING).
    """
    action_upper = action.upper()
    annotation_inner = annotation[1:-1].strip().upper() if annotation else None

    if action_upper in _EXTRACTOR_OPEN_ACTIONS:
        if close_qualifier is not None:
            return None
        if annotation_inner == "ADD":
            return "ADD"
        if annotation_inner == "ROLL UP":
            return "ROLL_UP"
        return "ENTRY"

    if action_upper in _EXTRACTOR_CLOSE_ACTIONS:
        if close_qualifier == "ALL OUT":
            return "FULL_EXIT"
        if close_qualifier is not None:
            return "PARTIAL_EXIT"
        return None

    return None


def _parse_action_line(line: str) -> dict | None:
    """Parse one alert's action line into its core fields.

    Args:
        line: The action line only (already identified by the caller as
            starting with a recognized action verb).

    Returns:
        A dict with keys "action", "symbol", "strike", "option_type",
        "expiration", "expiration_raw", "price", "qualifier",
        "annotation", "close_qualifier", "position_size", and
        "ambiguity_flags" (a list), or None if the line has no token at
        all after whitespace splitting (should not happen, since the
        caller already matched the action word on this line).
        "close_qualifier" is the raw fraction/"ALL OUT" text (or None),
        tracked separately from the merged "qualifier" display field so
        callers can detect an opening verb combined with a closing-only
        qualifier even when an annotation is also present.
    """
    annotation, remainder = _extract_bracket_annotation(line)
    tokens = remainder.strip().split()
    if not tokens:
        return None

    action = tokens[0].upper()
    remaining = tokens[1:]

    symbol: str | None = None
    if remaining and _EXTRACTOR_SYMBOL_RE.match(remaining[0]):
        symbol = remaining[0].upper()
        remaining = remaining[1:]

    strike: Decimal | None = None
    option_type: str | None = None
    expiration: str | None = None
    expiration_raw: str | None = None
    price: Decimal | None = None
    qualifier: str | None = None
    leftover_tokens: list[str] = []
    ambiguity_flags: list[str] = []

    index = 0
    count = len(remaining)
    while index < count:
        token = remaining[index]

        if option_type is None:
            option_match = _EXTRACTOR_OPTION_RE.match(token)
            if option_match:
                if option_match.group(1) is not None:
                    strike = Decimal(option_match.group(1))
                option_type = "put" if option_match.group(2).upper() in ("P", "PUT") else "call"
                index += 1
                continue

        # Checked before date parsing: the required fraction set (1/2,
        # 1/3, 1/4, 1/6, 1/8, 1/16) would otherwise be misread as a bare
        # MM/DD date by _EXTRACTOR_DATE_NO_YEAR_RE (e.g. "1/2" as January
        # 2nd), since both share the same "digits/digits" shape.
        if qualifier is None:
            if _EXTRACTOR_FRACTION_RE.match(token):
                qualifier = token
                index += 1
                continue
            if (
                token.upper() == "ALL"
                and index + 1 < count
                and remaining[index + 1].upper() == "OUT"
            ):
                qualifier = "ALL OUT"
                index += 2
                continue

        if expiration_raw is None:
            year_match = _EXTRACTOR_DATE_WITH_YEAR_RE.match(token)
            if year_match:
                normalized = _normalize_date(year_match)
                if normalized is not None:
                    expiration = normalized
                    expiration_raw = token
                    index += 1
                    continue
            no_year_match = _EXTRACTOR_DATE_NO_YEAR_RE.match(token)
            if no_year_match:
                expiration_raw = token
                ambiguity_flags.append(FLAG_EXPIRATION_YEAR_MISSING)
                index += 1
                continue

        if price is None:
            price_match = _EXTRACTOR_PRICE_RE.match(token)
            if price_match:
                try:
                    price = Decimal(price_match.group(1))
                except InvalidOperation:
                    pass
                else:
                    index += 1
                    continue

        leftover_tokens.append(token)
        index += 1

    if strike is None:
        ambiguity_flags.append(FLAG_STRIKE_MISSING)
    if annotation is None and qualifier is None:
        ambiguity_flags.append(FLAG_QUALIFIER_MISSING)

    position_size = " ".join(leftover_tokens).strip() or None

    return {
        "action": action,
        "symbol": symbol,
        "strike": strike,
        "option_type": option_type,
        "expiration": expiration,
        "expiration_raw": expiration_raw,
        "price": price,
        "qualifier": annotation if annotation is not None else qualifier,
        "annotation": annotation,
        "close_qualifier": qualifier,
        "position_size": position_size,
        "ambiguity_flags": ambiguity_flags,
    }


def _is_duplicate_restatement_line(tokens: list[str], symbol: str | None) -> bool:
    """Return True if a supplementary line merely restates the same contract.

    Args:
        tokens: The whitespace-split tokens of a non-action line.
        symbol: The symbol already extracted from the action line, or None.

    Returns:
        True if this line's first token equals the extracted symbol
        (verified against the real corpus: every bare contract-restatement
        line begins with the symbol itself, regardless of what follows -
        price, ALL OUT, an annotation, and so on).
    """
    return bool(tokens) and symbol is not None and tokens[0].upper() == symbol.upper()


def _is_bare_qualifier_confirmation_line(tokens: list[str]) -> bool:
    """Return True for a short line that only re-confirms the fraction/ALL OUT.

    Matches corpus lines like "1/2 position" and a standalone "ALL OUT".

    Args:
        tokens: The whitespace-split tokens of a non-action line.

    Returns:
        True if the line is a bare fraction (optionally followed by one
        more word, e.g. "position") or exactly "ALL OUT".
    """
    if not tokens:
        return False
    if len(tokens) <= 2 and _EXTRACTOR_FRACTION_RE.match(tokens[0]):
        return True
    if len(tokens) == 2 and tokens[0].upper() == "ALL" and tokens[1].upper() == "OUT":
        return True
    return False


def extract_trade_event(cleaned_text: str) -> dict:
    """Convert one Discord alert's cleaned_text into a structured trade event.

    Intended to be called once per app/discord_adapter.SegmentedMessage's
    cleaned_text - i.e. once per real Discord alert, however many lines it
    spans - and to return exactly one trade event for it. This function
    never imports app.discord_adapter and has no Discord-specific
    knowledge; it is a pure function of the text it is given.

    Args:
        cleaned_text: One alert's cleaned body text (already stripped of
            Discord wrapper noise by the adapter) - an action line,
            optionally followed by a duplicate contract-restatement line,
            a bare fraction/"ALL OUT" confirmation line, a stated
            "$OLD -> $NEW (+NN%)" return line, and/or free-text commentary.

    Returns:
        A dict with keys "symbol", "action", "option_type", "price",
        "expiration", "position_size" (matching parse_message()'s existing
        field semantics), plus "strike", "expiration_raw", "event_type",
        "qualifier", "stated_entry_price", "stated_return_pct", "notes",
        "parse_status", and "ambiguity_flags". parse_status is
        PARSE_STATUS_PARSED only when action, symbol, and price were all
        found; PARSE_STATUS_PARTIALLY_PARSED when an action line was found
        but symbol and/or price is missing; PARSE_STATUS_UNRECOGNIZED when
        no line matches a recognized action verb at all;
        PARSE_STATUS_FAILED only if an unexpected internal error occurs.
        Missing optional metadata (expiration year, strike, qualifier, a
        stated-return line) never demotes parse_status - it is recorded in
        ambiguity_flags instead. event_type is None both for an
        unrecognized verb and for an opening verb (BTO/BUY/BOUGHT/STO)
        combined with a closing-only qualifier ("ALL OUT" or a fraction) -
        a semantically contradictory alert that is never guessed into
        ENTRY/ADD/ROLL_UP/PARTIAL_EXIT/FULL_EXIT; the latter case is
        flagged with FLAG_ACTION_QUALIFIER_CONFLICT and does not demote
        parse_status either, so the alert's action/contract/qualifier/
        notes fields remain available for manual review. Never raises.
    """
    try:
        if not cleaned_text or not cleaned_text.strip():
            return _empty_extractor_result(PARSE_STATUS_UNRECOGNIZED)

        lines = cleaned_text.split("\n")

        action_line_index: int | None = None
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            first_token = stripped.split()[0]
            if _EXTRACTOR_ACTION_RE.match(first_token):
                action_line_index = index
                break

        if action_line_index is None:
            return _empty_extractor_result(PARSE_STATUS_UNRECOGNIZED)

        parsed_action_line = _parse_action_line(lines[action_line_index].strip())
        if parsed_action_line is None:
            return _empty_extractor_result(PARSE_STATUS_UNRECOGNIZED)

        symbol = parsed_action_line["symbol"]
        action = parsed_action_line["action"]
        price = parsed_action_line["price"]
        ambiguity_flags = list(parsed_action_line["ambiguity_flags"])

        stated_entry_price: Decimal | None = None
        stated_return_pct: Decimal | None = None
        notes_lines: list[str] = []

        for line in lines[action_line_index + 1 :]:
            stripped = line.strip()
            if not stripped:
                continue

            tokens = stripped.split()
            if _is_duplicate_restatement_line(tokens, symbol):
                continue
            if _is_bare_qualifier_confirmation_line(tokens):
                continue

            arrow_match = _EXTRACTOR_PRICE_ARROW_RE.match(stripped)
            if arrow_match:
                stated_entry_price = Decimal(arrow_match.group("old"))
                stated_return_pct = Decimal(arrow_match.group("pct"))
                continue

            notes_lines.append(stripped)

        notes = "\n".join(notes_lines) or None

        close_qualifier = parsed_action_line["close_qualifier"]
        event_type = _derive_event_type(
            action, parsed_action_line["annotation"], close_qualifier
        )
        if (
            action is not None
            and action.upper() in _EXTRACTOR_OPEN_ACTIONS
            and close_qualifier is not None
        ):
            ambiguity_flags.append(FLAG_ACTION_QUALIFIER_CONFLICT)
        if event_type in ("PARTIAL_EXIT", "FULL_EXIT") and stated_entry_price is None:
            ambiguity_flags.append(FLAG_STATED_RETURN_MISSING)

        if action is None or symbol is None or price is None:
            parse_status = PARSE_STATUS_PARTIALLY_PARSED
        else:
            parse_status = PARSE_STATUS_PARSED

        return {
            "symbol": symbol,
            "action": action,
            "option_type": parsed_action_line["option_type"],
            "price": price,
            "expiration": parsed_action_line["expiration"],
            "position_size": parsed_action_line["position_size"],
            "strike": parsed_action_line["strike"],
            "expiration_raw": parsed_action_line["expiration_raw"],
            "event_type": event_type,
            "qualifier": parsed_action_line["qualifier"],
            "stated_entry_price": stated_entry_price,
            "stated_return_pct": stated_return_pct,
            "notes": notes,
            "parse_status": parse_status,
            "ambiguity_flags": ambiguity_flags,
        }
    except Exception:  # noqa: BLE001 - extract_trade_event() must never raise
        return _empty_extractor_result(PARSE_STATUS_FAILED)
