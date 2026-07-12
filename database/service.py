"""Business-rule orchestration layer for Discord Traders.

TradeService is the only module permitted to contain business rules (e.g.
duplicate detection, edit-on-update, ingestion order); it orchestrates one
or more database/repository.py calls but never runs SQL itself. Callers are
responsible for opening/closing the connection and for committing or
rolling back the transaction, exactly as with repository.py.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal

from database.repository import get_trade_signals_matching

DUPLICATE_WINDOW_MINUTES = 5

_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


class TradeService:
    """Orchestrates business rules on top of database/repository.py.

    Attributes:
        conn: An open sqlite3.Connection, owned and committed/rolled back
            by the caller.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Initialize the service with a caller-owned connection.

        Args:
            conn: An open sqlite3.Connection.
        """
        self.conn = conn

    def check_duplicate_signal(
        self,
        trader_id: int,
        symbol: str,
        action: str,
        option_type: str | None,
        price: Decimal | None,
        expiration: str | None,
        reference_time: str,
    ) -> str | None:
        """Check for a possible duplicate trade signal within a short window.

        Soft, advisory check per docs/DATABASE_DESIGN_V1.md Section 5: looks
        for existing trade_signals matching trader_id, symbol, action,
        option_type, price, and expiration exactly, with created_at within
        DUPLICATE_WINDOW_MINUTES minutes before (inclusive) reference_time.
        Nothing is ever blocked - this only returns a warning the caller can
        act on.

        Args:
            trader_id: FK to traders.id.
            symbol: Ticker symbol to match exactly.
            action: Free-text trade action to match exactly.
            option_type: Free-text call/put to match exactly, or None.
            price: A Decimal price to match exactly, or None. Never a float
                or string.
            expiration: ISO8601 date string to match exactly, or None.
            reference_time: The timestamp to check against, in the same
                format as trade_signals.created_at
                ("YYYY-MM-DD HH:MM:SS"), so callers and tests can supply a
                deterministic value instead of wall-clock now().

        Returns:
            A warning string if at least one matching trade signal exists
            within the window, otherwise None.

        Raises:
            ValueError: If reference_time is None, empty, or not in the
                expected format; or if trader_id is None, or symbol or
                action is None, empty, or whitespace-only.
            TypeError: If price is supplied and is not a Decimal.
        """
        if not reference_time or not reference_time.strip():
            raise ValueError("reference_time is required.")
        try:
            reference_dt = datetime.strptime(reference_time, _TIMESTAMP_FORMAT)
        except ValueError as exc:
            raise ValueError(
                f"reference_time must match the format {_TIMESTAMP_FORMAT!r}."
            ) from exc

        window_start_dt = reference_dt - timedelta(minutes=DUPLICATE_WINDOW_MINUTES)
        window_start = window_start_dt.strftime(_TIMESTAMP_FORMAT)
        window_end = reference_dt.strftime(_TIMESTAMP_FORMAT)

        matches = get_trade_signals_matching(
            self.conn,
            trader_id,
            symbol,
            action,
            option_type,
            price,
            expiration,
            window_start,
            window_end,
        )

        if not matches:
            return None

        return (
            f"Possible duplicate: {len(matches)} matching trade signal(s) "
            f"found for trader {trader_id} ({symbol} {action}) within the "
            f"last {DUPLICATE_WINDOW_MINUTES} minutes."
        )
