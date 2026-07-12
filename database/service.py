"""Business-rule orchestration layer for Discord Traders.

TradeService is the only module permitted to contain business rules (e.g.
duplicate detection, edit-on-update, ingestion order); it orchestrates one
or more database/repository.py calls but never runs SQL itself. Callers are
responsible for opening/closing the connection and for committing or
rolling back the transaction, exactly as with repository.py.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal

from database.models import TradeSignal
from database.repository import (
    create_raw_message,
    create_trade_signal,
    create_trade_signal_edit,
    create_trader,
    get_or_create_source,
    get_trade_signal_by_id,
    get_trade_signals_matching,
    get_trader_by_external_id,
    update_trade_signal as _repository_update_trade_signal,
    validate_trade_signal_update_fields,
)

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

    def update_trade_signal(
        self,
        trade_signal_id: int,
        **changed_fields,
    ) -> TradeSignal:
        """Update a trade signal, always preserving edit history.

        Enforces docs/DATABASE_DESIGN_V1.md Section 6: before any correction
        to trade_signals, a full-row JSON snapshot of the pre-edit values is
        written to trade_signal_edits via database/repository.py. Field
        validation is delegated to
        repository.validate_trade_signal_update_fields(), the single source
        of truth shared with repository.update_trade_signal(), so this
        method cannot diverge from the repository layer on what counts as a
        valid update.

        Execution order: validate changed_fields, fetch the existing row,
        write the edit snapshot, then apply the update - in that order, so
        no edit row is ever written for an update that will not happen.

        Args:
            trade_signal_id: Primary key of the trade signal to update.
            **changed_fields: One or more of raw_message_id, trader_id,
                symbol, action, option_type, price, expiration,
                position_size. Optional fields may be explicitly set to
                None.

        Returns:
            The updated TradeSignal.

        Raises:
            ValueError: If changed_fields fails validation (see
                repository.validate_trade_signal_update_fields), or if no
                trade signal exists with trade_signal_id.
            TypeError: If price is supplied and is not a Decimal.
        """
        validate_trade_signal_update_fields(changed_fields)

        existing = get_trade_signal_by_id(self.conn, trade_signal_id)
        if existing is None:
            raise ValueError(f"No trade signal exists with id {trade_signal_id}.")

        create_trade_signal_edit(self.conn, trade_signal_id, asdict(existing))

        return _repository_update_trade_signal(
            self.conn, trade_signal_id, **changed_fields
        )

    def ingest_message(
        self,
        source_name: str,
        trader_name: str,
        raw_text: str,
        reference_time: str,
        external_trader_id: str | None = None,
        external_message_id: str | None = None,
        metadata: dict | None = None,
        received_at: str | None = None,
        trade_signals: list[dict] | None = None,
    ) -> dict:
        """Ingest a new message and its parsed trade signals.

        The single public entry point for persisting a new message: resolves
        the source and trader, inserts the raw message, then inserts each
        parsed trade signal, checking each for a possible duplicate via
        check_duplicate_signal() first. Persistence follows this strictly
        linear order: source, then trader, then raw message, then (for each
        parsed signal) duplicate check followed by insert.

        Trader identity (per docs/DATABASE_DESIGN_V1.md Section 3): if
        external_trader_id is given, an existing trader is reused when one
        already exists for (source_id, external_trader_id); otherwise a new
        trader is created. If external_trader_id is not given, a new trader
        row is always created - display names are never used to match an
        existing trader, since they are not a unique identity.

        Duplicate raw messages (source_id, external_id) are not pre-checked:
        the database's own UNIQUE constraint enforces this, and
        sqlite3.IntegrityError propagates naturally on collision. Editing an
        already-ingested Discord message is out of scope for this method -
        raw_messages.raw_text is write-once, and only new-message ingestion
        is handled here.

        As with the other TradeService methods, this never commits or rolls
        back the connection; the caller owns the transaction.

        Args:
            source_name: Source type name (e.g. 'discord'); looked up or
                created.
            trader_name: Display name/handle as seen in the source.
            raw_text: The original message, verbatim.
            reference_time: The timestamp to check each parsed signal's
                duplicate window against, in the same format as
                trade_signals.created_at ("YYYY-MM-DD HH:MM:SS"). Always
                supplied by the caller; never read from wall-clock now().
            external_trader_id: Stable source-provided trader ID, when
                available.
            external_message_id: Source-provided message ID, when available.
            metadata: Opaque JSON-serializable metadata, or None.
            received_at: ISO8601 timestamp of when the source sent the
                message.
            trade_signals: Zero or more dicts of parsed trade signal fields
                (symbol, action, option_type, price, expiration,
                position_size). raw_message_id and trader_id are filled in
                by this method, not the caller.

        Returns:
            A dict with keys "source" (Source), "trader" (Trader),
            "raw_message" (RawMessage), "trade_signals" (list[TradeSignal],
            in the same order as the trade_signals argument), and
            "duplicate_warnings" (list[str | None], one entry per created
            trade signal, from check_duplicate_signal()).

        Raises:
            ValueError: If source_name or trader_name is empty or
                whitespace-only; if reference_time is missing or malformed;
                or if any parsed signal is missing a required field
                (symbol, action).
            TypeError: If price on any parsed signal is supplied and is not
                a Decimal.
            sqlite3.IntegrityError: If external_message_id collides with an
                already-ingested message for this source, or
                external_trader_id collides in a race.
        """
        source = get_or_create_source(self.conn, source_name)

        if external_trader_id is not None:
            trader = get_trader_by_external_id(
                self.conn, source.id, external_trader_id
            )
            if trader is None:
                trader = create_trader(
                    self.conn, source.id, trader_name, external_trader_id
                )
        else:
            trader = create_trader(self.conn, source.id, trader_name, None)

        raw_message = create_raw_message(
            self.conn,
            source.id,
            raw_text,
            external_message_id,
            metadata,
            received_at,
        )

        created_signals: list[TradeSignal] = []
        duplicate_warnings: list[str | None] = []

        for signal_fields in trade_signals or []:
            symbol = signal_fields.get("symbol")
            action = signal_fields.get("action")
            option_type = signal_fields.get("option_type")
            price = signal_fields.get("price")
            expiration = signal_fields.get("expiration")
            position_size = signal_fields.get("position_size")

            warning = self.check_duplicate_signal(
                trader.id,
                symbol,
                action,
                option_type,
                price,
                expiration,
                reference_time,
            )
            signal = create_trade_signal(
                self.conn,
                raw_message.id,
                trader.id,
                symbol,
                action,
                option_type,
                price,
                expiration,
                position_size,
            )
            created_signals.append(signal)
            duplicate_warnings.append(warning)

        return {
            "source": source,
            "trader": trader,
            "raw_message": raw_message,
            "trade_signals": created_signals,
            "duplicate_warnings": duplicate_warnings,
        }
