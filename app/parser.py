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
