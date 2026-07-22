"""Business-rule orchestration layer for Discord Traders.

TradeService is the only module permitted to contain business rules (e.g.
duplicate detection, edit-on-update, ingestion order); it orchestrates one
or more database/repository.py calls but never runs SQL itself. Callers are
responsible for opening/closing the connection and for committing or
rolling back the transaction, exactly as with repository.py.
"""

from __future__ import annotations

import json
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
    get_trade_signal_edits,
    get_trade_signals_for_review,
    get_trade_signals_matching,
    get_trader_by_external_id,
    update_trade_signal as _repository_update_trade_signal,
    validate_trade_signal_update_fields,
)

DUPLICATE_WINDOW_MINUTES = 5

_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# The exact, fixed set of fields a controlled correction (Milestone 2D.5)
# may change - deliberately excludes raw_message_id/trader_id (identity,
# not a "correction") and the structurally protected id/created_at/
# updated_at, even though the more permissive repository-layer editable
# set would otherwise accept raw_message_id/trader_id too.
_CORRECTION_FIELDS = frozenset(
    {"symbol", "action", "option_type", "price", "expiration", "position_size"}
)


class StaleTradeSignalError(Exception):
    """Raised when a controlled correction's expected_current_values no
    longer match the actual persisted values - another edit happened
    since the caller loaded the signal. Raised before any audit snapshot
    or update; the persisted row is left completely untouched."""


class TradeSignalNotFoundError(ValueError):
    """Raised when a controlled correction targets a trade_signal_id that
    no longer exists. A ValueError subclass so it can still be caught
    broadly as a ValueError, but is distinct from the plain ValueError
    used for no-op/shape rejections and from the legacy (non-controlled)
    update_trade_signal() path's own missing-signal ValueError, which is
    unchanged."""


class AuditHistoryError(Exception):
    """Raised when a stored trade_signal_edits.previous_values value
    cannot be decoded as JSON, or does not decode to a dict."""


def _current_correction_values(signal: TradeSignal) -> dict:
    """Build the canonical typed six-field correction snapshot from signal.

    Args:
        signal: The currently persisted TradeSignal to snapshot.

    Returns:
        A dict with exactly the _CORRECTION_FIELDS keys: symbol (str),
        action (str), option_type (str | None), price (Decimal | None -
        parsed from the stored decimal string, never a float),
        expiration (str | None), position_size (str | None).
    """
    return {
        "symbol": signal.symbol,
        "action": signal.action,
        "option_type": signal.option_type,
        "price": Decimal(signal.price) if signal.price is not None else None,
        "expiration": signal.expiration,
        "position_size": signal.position_size,
    }


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
        expected_current_values: dict | None = None,
        **changed_fields,
    ) -> TradeSignal:
        """Update a trade signal, always preserving edit history.

        Enforces docs/DATABASE_DESIGN_V1.md Section 6: before any correction
        to trade_signals, a full-row JSON snapshot of the pre-edit values is
        written to trade_signal_edits via database/repository.py.

        Two modes, selected only by whether expected_current_values is
        supplied:

        Legacy mode (expected_current_values is None) - unchanged since
        Milestone 2B.6b: changed_fields may be any non-empty, valid subset
        of the repository layer's editable fields (raw_message_id,
        trader_id, symbol, action, option_type, price, expiration,
        position_size); field validation is delegated to
        repository.validate_trade_signal_update_fields(), the single
        source of truth shared with repository.update_trade_signal(); no
        six-field shape requirement, no no-op check, no stale-conflict
        check. Execution order: validate changed_fields, fetch the
        existing row, write the edit snapshot, then apply the update.

        Controlled correction mode (expected_current_values is not None) -
        Milestone 2D.5: changed_fields must contain exactly the six
        approved correction fields (symbol, action, option_type, price,
        expiration, position_size) - never raw_message_id, trader_id, id,
        created_at, or updated_at, even though the repository layer would
        otherwise accept raw_message_id/trader_id. expected_current_values
        must contain exactly the same six keys, typed as symbol: str,
        action: str, option_type: str | None, price: Decimal | None,
        expiration: str | None, position_size: str | None - never a float,
        and never compared against an unparsed price string. Order:
        validate changed_fields' key shape, validate
        expected_current_values' key shape, run the shared repository
        field validation, fetch the existing row (raising
        TradeSignalNotFoundError if missing), build the canonical current-
        value snapshot, compare expected_current_values to it (raising
        StaleTradeSignalError on any mismatch), compare changed_fields to
        it (raising ValueError if identical - a no-op), then - only after
        every check passes - write exactly one audit snapshot and apply
        the update. No stale, missing, invalid, or no-op correction ever
        creates an audit row or changes updated_at.

        Args:
            trade_signal_id: Primary key of the trade signal to update.
            expected_current_values: None for legacy sparse-update
                behavior; the canonical typed six-field snapshot the
                caller believes is still current, to enable controlled-
                correction mode's stale-conflict and no-op protection.
            **changed_fields: The fields to change - shape and content
                requirements depend on the mode above.

        Returns:
            The updated TradeSignal.

        Raises:
            ValueError: Legacy mode - if changed_fields fails validation,
                or if no trade signal exists with trade_signal_id.
                Controlled mode - if changed_fields or
                expected_current_values does not contain exactly the six
                approved fields, if changed_fields fails the shared
                repository validation, or if changed_fields is identical
                to the current persisted values (a no-op).
            TradeSignalNotFoundError: Controlled mode only - if no trade
                signal exists with trade_signal_id. A ValueError subclass.
            StaleTradeSignalError: Controlled mode only - if
                expected_current_values no longer matches the actual
                persisted values.
            TypeError: If price is supplied and is not a Decimal.
        """
        if expected_current_values is None:
            validate_trade_signal_update_fields(changed_fields)

            existing = get_trade_signal_by_id(self.conn, trade_signal_id)
            if existing is None:
                raise ValueError(f"No trade signal exists with id {trade_signal_id}.")

            create_trade_signal_edit(self.conn, trade_signal_id, asdict(existing))

            return _repository_update_trade_signal(
                self.conn, trade_signal_id, **changed_fields
            )

        if set(changed_fields) != _CORRECTION_FIELDS:
            raise ValueError(
                "A controlled correction must supply exactly the approved "
                f"correction fields: {sorted(_CORRECTION_FIELDS)}."
            )
        if set(expected_current_values) != _CORRECTION_FIELDS:
            raise ValueError(
                "expected_current_values must supply exactly the approved "
                f"correction fields: {sorted(_CORRECTION_FIELDS)}."
            )

        validate_trade_signal_update_fields(changed_fields)

        existing = get_trade_signal_by_id(self.conn, trade_signal_id)
        if existing is None:
            raise TradeSignalNotFoundError(
                f"No trade signal exists with id {trade_signal_id}."
            )

        current_values = _current_correction_values(existing)

        if expected_current_values != current_values:
            raise StaleTradeSignalError(
                "The trade signal's current values no longer match the "
                "values expected for this correction."
            )

        if changed_fields == current_values:
            raise ValueError("A correction must change at least one field.")

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

    def list_trade_signals_for_review(
        self,
        *,
        source_name: str | None = None,
        trader_name: str | None = None,
        symbol: str | None = None,
        date: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """List persisted trade signals for read-only review, newest first.

        A thin, read-only delegation to
        database.repository.get_trade_signals_for_review(): no business
        rules are applied and no row is ever written. The only
        normalization performed here is uppercasing a non-blank symbol
        filter before delegating, so callers (the UI) do not need to
        duplicate that normalization themselves; all other arguments and
        the returned list are passed through unchanged.

        Args:
            source_name: Exact sources.name to filter by, or None/blank to
                omit.
            trader_name: Exact traders.name to filter by, or None/blank to
                omit.
            symbol: Ticker symbol to filter by; normalized to uppercase
                when non-blank, or None/blank to omit.
            date: Calendar date "YYYY-MM-DD" to filter
                trade_signals.created_at by (inclusive start of day,
                exclusive start of the following day), or None/blank to
                omit.
            limit: Maximum number of rows to return. Defaults to 100.

        Returns:
            The list of dicts returned by
            database.repository.get_trade_signals_for_review(), unchanged.

        Raises:
            ValueError: If date is supplied and is not in "YYYY-MM-DD"
                format.
        """
        normalized_symbol = symbol.upper() if symbol and symbol.strip() else symbol

        return get_trade_signals_for_review(
            self.conn,
            source_name=source_name,
            trader_name=trader_name,
            symbol=normalized_symbol,
            date=date,
            limit=limit,
        )

    def list_trade_signal_audit_history(self, trade_signal_id: int) -> list[dict]:
        """List a trade signal's audit history for read-only display.

        A thin, read-only delegation to
        database.repository.get_trade_signal_edits(): decodes each row's
        previous_values JSON (raising AuditHistoryError if it is not
        valid JSON or does not decode to a dict), and returns only the
        six previous editable-field values plus id and edited_at - never
        the raw previous_values JSON text itself. price is preserved as
        its exact stored string, never converted to float.

        Args:
            trade_signal_id: FK to trade_signals.id.

        Returns:
            A list of plain dicts, newest first, each with exactly: id,
            edited_at, symbol, action, option_type, price, expiration,
            position_size. Empty list if the signal has no edit history.

        Raises:
            AuditHistoryError: If a stored previous_values value is not
                valid JSON, or does not decode to a dict.
        """
        edits = get_trade_signal_edits(self.conn, trade_signal_id)

        history: list[dict] = []
        for edit in edits:
            try:
                previous = json.loads(edit.previous_values)
            except (json.JSONDecodeError, TypeError) as exc:
                raise AuditHistoryError(
                    f"Stored audit data for edit {edit.id} could not be decoded."
                ) from exc

            if not isinstance(previous, dict):
                raise AuditHistoryError(
                    f"Stored audit data for edit {edit.id} is not a JSON object."
                )

            history.append(
                {
                    "id": edit.id,
                    "edited_at": edit.edited_at,
                    "symbol": previous.get("symbol"),
                    "action": previous.get("action"),
                    "option_type": previous.get("option_type"),
                    "price": previous.get("price"),
                    "expiration": previous.get("expiration"),
                    "position_size": previous.get("position_size"),
                }
            )

        history.reverse()
        return history
