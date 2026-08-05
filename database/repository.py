"""Data-access functions for Discord Traders.

The only module permitted to run SQL against the database, built up
incrementally table by table (Milestone 2B.5). Callers are responsible for
opening/closing the connection and for committing or rolling back the
transaction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from database.lifecycle import SignalSnapshot
from database.models import (
    Channel,
    ChannelImportOperation,
    ImportBatch,
    MessageExtraction,
    RawMessage,
    Source,
    Trader,
    TradeLifecycle,
    TradeLifecycleEvent,
    TradeSignal,
    TradeSignalEdit,
)

_REVIEW_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
_REVIEW_DATE_FORMAT = "%Y-%m-%d"


def _validate_source_name(name: str) -> None:
    """Reject empty or whitespace-only source names.

    Args:
        name: Source name to validate.

    Raises:
        ValueError: If name is empty or whitespace-only.
    """
    if not name or not name.strip():
        raise ValueError("Source name must not be empty or whitespace-only.")


def get_source_by_name(connection: sqlite3.Connection, name: str) -> Optional[Source]:
    """Look up a source by its exact name.

    Args:
        connection: An open sqlite3.Connection.
        name: Exact source name to search for.

    Returns:
        The matching Source, or None if no source with that name exists.

    Raises:
        ValueError: If name is empty or whitespace-only.
    """
    _validate_source_name(name)

    row = connection.execute(
        "SELECT id, name FROM sources WHERE name = ?", (name,)
    ).fetchone()
    if row is None:
        return None
    return Source(id=row["id"], name=row["name"])


def get_or_create_source(connection: sqlite3.Connection, name: str) -> Source:
    """Return the existing source with this name, creating it if needed.

    Args:
        connection: An open sqlite3.Connection.
        name: Exact source name to look up or create.

    Returns:
        The existing or newly created Source.

    Raises:
        ValueError: If name is empty or whitespace-only.
    """
    _validate_source_name(name)

    existing = get_source_by_name(connection, name)
    if existing is not None:
        return existing

    cursor = connection.execute("INSERT INTO sources (name) VALUES (?)", (name,))
    return Source(id=cursor.lastrowid, name=name)


def _validate_trader_name(name: str) -> None:
    """Reject empty or whitespace-only trader names.

    Args:
        name: Trader name to validate.

    Raises:
        ValueError: If name is empty or whitespace-only.
    """
    if not name or not name.strip():
        raise ValueError("Trader name must not be empty or whitespace-only.")


def _row_to_trader(row: sqlite3.Row) -> Trader:
    """Map a ``traders`` table row to a Trader model.

    Args:
        row: A row from the traders table, with all columns selected.

    Returns:
        The corresponding Trader.
    """
    return Trader(
        id=row["id"],
        source_id=row["source_id"],
        name=row["name"],
        external_trader_id=row["external_trader_id"],
        created_at=row["created_at"],
        canonical_name=row["canonical_name"],
    )


def create_trader(
    conn: sqlite3.Connection,
    source_id: int,
    name: str,
    external_trader_id: str | None = None,
) -> Trader:
    """Insert a new trader row.

    canonical_name is always computed automatically as name.strip().lower()
    and stored alongside name, so newly created rows never need a separate
    backfill. Duplicate (source_id, name) rows remain allowed, as today.

    Args:
        conn: An open sqlite3.Connection.
        source_id: FK to sources.id.
        name: Display name/handle as seen in the source.
        external_trader_id: Stable source-provided trader ID, when available.

    Returns:
        The newly created Trader, including its generated id, created_at,
        and derived canonical_name.

    Raises:
        ValueError: If name is empty or whitespace-only.
        sqlite3.IntegrityError: If source_id does not reference an existing
            source, or if external_trader_id is not None and already exists
            for this source_id.
    """
    _validate_trader_name(name)
    canonical_name = name.strip().lower()

    cursor = conn.execute(
        "INSERT INTO traders (source_id, name, external_trader_id, canonical_name) "
        "VALUES (?, ?, ?, ?)",
        (source_id, name, external_trader_id, canonical_name),
    )
    row = conn.execute(
        "SELECT id, source_id, name, external_trader_id, created_at, canonical_name "
        "FROM traders WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return _row_to_trader(row)


def get_trader_by_external_id(
    conn: sqlite3.Connection,
    source_id: int,
    external_trader_id: str,
) -> Trader | None:
    """Look up a trader by source and external trader ID.

    Args:
        conn: An open sqlite3.Connection.
        source_id: FK to sources.id.
        external_trader_id: Source-provided trader ID to search for.

    Returns:
        The matching Trader, or None if no row exists for this
        source_id/external_trader_id pair.
    """
    row = conn.execute(
        "SELECT id, source_id, name, external_trader_id, created_at, canonical_name "
        "FROM traders WHERE source_id = ? AND external_trader_id = ?",
        (source_id, external_trader_id),
    ).fetchone()
    if row is None:
        return None
    return _row_to_trader(row)


def get_traders_by_name(
    conn: sqlite3.Connection,
    source_id: int,
    name: str,
) -> list[Trader]:
    """Look up all traders matching a source and exact name.

    Duplicate trader names within a source are allowed, so this may return
    more than one Trader.

    Args:
        conn: An open sqlite3.Connection.
        source_id: FK to sources.id.
        name: Exact trader name to search for.

    Returns:
        All matching Traders, ordered by id. Empty list if none match.
    """
    rows = conn.execute(
        "SELECT id, source_id, name, external_trader_id, created_at, canonical_name "
        "FROM traders WHERE source_id = ? AND name = ? ORDER BY id",
        (source_id, name),
    ).fetchall()
    return [_row_to_trader(row) for row in rows]


def get_traders_by_canonical_name(
    conn: sqlite3.Connection,
    source_id: int,
    canonical_name: str,
) -> list[Trader]:
    """Look up all traders matching a source and exact canonical_name.

    Case-insensitive identity resolution primitive (e.g. "Matae" and
    "matae" share one canonical_name). Duplicate rows remain possible, so
    this may return more than one Trader; wiring this into actual
    find-or-create ingestion logic is out of scope here.

    Args:
        conn: An open sqlite3.Connection.
        source_id: FK to sources.id.
        canonical_name: Exact canonical_name to search for (already
            lowercased/trimmed by the caller).

    Returns:
        All matching Traders, ordered by id. Empty list if none match.
    """
    rows = conn.execute(
        "SELECT id, source_id, name, external_trader_id, created_at, canonical_name "
        "FROM traders WHERE source_id = ? AND canonical_name = ? ORDER BY id",
        (source_id, canonical_name),
    ).fetchall()
    return [_row_to_trader(row) for row in rows]


def compute_content_hash(raw_text: str) -> str:
    """Compute the content hash used for duplicate-lookup fallback.

    Public (promoted from a private helper during Recovery Milestone R9a)
    so this is the one and only content-hash implementation in the
    codebase - database.service imports and calls this exact function
    (in _duplicate_outcome() and predict_channel_import_duplicate_statuses())
    rather than maintaining a second, independently computed hash.

    Args:
        raw_text: The raw message text, exactly as received.

    Returns:
        The SHA-256 hex digest of the UTF-8 encoding of raw_text.
    """
    return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()


def _row_to_raw_message(row: sqlite3.Row) -> RawMessage:
    """Map a ``raw_messages`` table row to a RawMessage model.

    Args:
        row: A row from the raw_messages table, with all columns selected.

    Returns:
        The corresponding RawMessage, with metadata deserialized from its
        stored JSON representation (or None if the column is NULL).
    """
    metadata = json.loads(row["metadata"]) if row["metadata"] is not None else None
    return RawMessage(
        id=row["id"],
        source_id=row["source_id"],
        external_id=row["external_id"],
        raw_text=row["raw_text"],
        content_hash=row["content_hash"],
        metadata=metadata,
        received_at=row["received_at"],
        ingested_at=row["ingested_at"],
        channel_id=row["channel_id"],
        import_batch_id=row["import_batch_id"],
        sequence_in_batch=row["sequence_in_batch"],
    )


_RAW_MESSAGE_COLUMNS = (
    "id, source_id, external_id, raw_text, content_hash, metadata, "
    "received_at, ingested_at, channel_id, import_batch_id, sequence_in_batch"
)


def create_raw_message(
    conn: sqlite3.Connection,
    source_id: int,
    raw_text: str,
    external_id: str | None = None,
    metadata: dict | None = None,
    received_at: str | None = None,
    channel_id: int | None = None,
    import_batch_id: int | None = None,
    sequence_in_batch: int | None = None,
) -> RawMessage:
    """Insert a new raw message row.

    raw_text is stored exactly as supplied: no stripping, case changes,
    Unicode normalization, or line-ending changes are performed. This is
    the only way a raw message is ever written - there is no update path,
    so raw_text can never be overwritten once persisted.

    Args:
        conn: An open sqlite3.Connection.
        source_id: FK to sources.id.
        raw_text: The original message, verbatim.
        external_id: Source-provided message ID, when available.
        metadata: Opaque JSON-serializable metadata, or None.
        received_at: ISO8601 timestamp of when the source sent the message.
        channel_id: FK to channels.id, when this message is scoped to a
            specific source channel, or None.
        import_batch_id: FK to import_batches.id, when this message was
            segmented from a batch paste, or None.
        sequence_in_batch: Position of this message within its import
            batch, or None.

    Returns:
        The newly created RawMessage, including its generated id,
        computed content_hash, and DB-defaulted ingested_at.

    Raises:
        sqlite3.IntegrityError: If source_id does not reference an existing
            source; if channel_id is None and external_id is not None and
            already exists for this source_id among other channel_id-IS-NULL
            rows; or if channel_id and external_id are both not None and
            already exist together for this channel_id. The same
            external_id MAY repeat across two different non-null
            channel_ids.
        TypeError: If metadata is not JSON-serializable.
    """
    content_hash = compute_content_hash(raw_text)
    serialized_metadata = (
        json.dumps(metadata, sort_keys=True) if metadata is not None else None
    )

    cursor = conn.execute(
        "INSERT INTO raw_messages "
        "(source_id, external_id, raw_text, content_hash, metadata, received_at, "
        "channel_id, import_batch_id, sequence_in_batch) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source_id,
            external_id,
            raw_text,
            content_hash,
            serialized_metadata,
            received_at,
            channel_id,
            import_batch_id,
            sequence_in_batch,
        ),
    )
    row = conn.execute(
        f"SELECT {_RAW_MESSAGE_COLUMNS} FROM raw_messages WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return _row_to_raw_message(row)


def get_raw_message_by_external_id(
    conn: sqlite3.Connection,
    source_id: int,
    external_id: str,
) -> RawMessage | None:
    """Look up a raw message by source and external message ID.

    Args:
        conn: An open sqlite3.Connection.
        source_id: FK to sources.id.
        external_id: Source-provided message ID to search for.

    Returns:
        The matching RawMessage, or None if no row exists for this
        source_id/external_id pair.
    """
    row = conn.execute(
        f"SELECT {_RAW_MESSAGE_COLUMNS} FROM raw_messages "
        "WHERE source_id = ? AND external_id = ?",
        (source_id, external_id),
    ).fetchone()
    if row is None:
        return None
    return _row_to_raw_message(row)


def get_raw_message_by_channel_and_external_id(
    conn: sqlite3.Connection,
    channel_id: int,
    external_id: str,
) -> RawMessage | None:
    """Look up a raw message by channel and external message ID.

    This is the "idempotent ingestion using channel ID plus message ID"
    lookup: callers check this before inserting to recognize an
    already-stored message (real or synthetic ID) without relying on a
    thrown IntegrityError.

    Args:
        conn: An open sqlite3.Connection.
        channel_id: FK to channels.id.
        external_id: Source-provided (or synthetic) message ID to search
            for.

    Returns:
        The matching RawMessage, or None if no row exists for this
        channel_id/external_id pair.
    """
    row = conn.execute(
        f"SELECT {_RAW_MESSAGE_COLUMNS} FROM raw_messages "
        "WHERE channel_id = ? AND external_id = ?",
        (channel_id, external_id),
    ).fetchone()
    if row is None:
        return None
    return _row_to_raw_message(row)


def get_raw_messages_by_content_hash(
    conn: sqlite3.Connection,
    content_hash: str,
) -> list[RawMessage]:
    """Look up all raw messages matching a content hash.

    Content hashes are not unique: identical raw text may legitimately be
    inserted more than once, so this may return more than one RawMessage.

    Args:
        conn: An open sqlite3.Connection.
        content_hash: The content hash to search for.

    Returns:
        All matching RawMessages, ordered by id. Empty list if none match.
    """
    rows = conn.execute(
        f"SELECT {_RAW_MESSAGE_COLUMNS} FROM raw_messages "
        "WHERE content_hash = ? ORDER BY id",
        (content_hash,),
    ).fetchall()
    return [_row_to_raw_message(row) for row in rows]


_TRADE_SIGNAL_EDITABLE_FIELDS = frozenset(
    {
        "raw_message_id",
        "trader_id",
        "symbol",
        "action",
        "option_type",
        "price",
        "expiration",
        "position_size",
    }
)

_TRADE_SIGNAL_PROTECTED_FIELDS = frozenset({"id", "created_at", "updated_at"})


def _validate_trade_signal_required_fields(
    raw_message_id: int | None,
    trader_id: int | None,
    symbol: str | None,
    action: str | None,
) -> None:
    """Validate the trade_signals fields required by the schema.

    Args:
        raw_message_id: FK to raw_messages.id.
        trader_id: FK to traders.id.
        symbol: Ticker symbol.
        action: Free-text trade action (e.g. BTO/STC).

    Raises:
        ValueError: If raw_message_id or trader_id is None, or if symbol or
            action is None, empty, or whitespace-only.
    """
    if raw_message_id is None:
        raise ValueError("raw_message_id is required.")
    if trader_id is None:
        raise ValueError("trader_id is required.")
    if symbol is None or not symbol.strip():
        raise ValueError("symbol must not be empty or whitespace-only.")
    if action is None or not action.strip():
        raise ValueError("action must not be empty or whitespace-only.")


def _serialize_price(price: Decimal | None) -> str | None:
    """Convert a trade signal price to its exact decimal string for storage.

    Binary floating-point is never used for trade prices, per
    docs/DATABASE_DESIGN_V1.md Section 3. No arithmetic, rounding, or
    quantization is performed; the Decimal's own string representation is
    used as-is.

    Args:
        price: A Decimal price, or None.

    Returns:
        The exact string representation of price, or None.

    Raises:
        TypeError: If price is not a Decimal and not None (e.g. float, str,
            int, or any other type).
    """
    if price is None:
        return None
    if not isinstance(price, Decimal):
        raise TypeError(
            f"price must be a Decimal or None, got {type(price).__name__}."
        )
    return str(price)


def _row_to_trade_signal(row: sqlite3.Row) -> TradeSignal:
    """Map a ``trade_signals`` table row to a TradeSignal model.

    Args:
        row: A row from the trade_signals table, with all columns selected.

    Returns:
        The corresponding TradeSignal.
    """
    return TradeSignal(
        id=row["id"],
        raw_message_id=row["raw_message_id"],
        trader_id=row["trader_id"],
        symbol=row["symbol"],
        action=row["action"],
        option_type=row["option_type"],
        price=row["price"],
        expiration=row["expiration"],
        position_size=row["position_size"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        strike=row["strike"],
        expiration_raw=row["expiration_raw"],
        event_type=row["event_type"],
        qualifier=row["qualifier"],
        stated_entry_price=row["stated_entry_price"],
        stated_return_pct=row["stated_return_pct"],
        notes=row["notes"],
        extraction_id=row["extraction_id"],
        lifecycle_id=row["lifecycle_id"],
    )


_TRADE_SIGNAL_COLUMNS = (
    "id, raw_message_id, trader_id, symbol, action, option_type, price, "
    "expiration, position_size, created_at, updated_at, strike, "
    "expiration_raw, event_type, qualifier, stated_entry_price, "
    "stated_return_pct, notes, extraction_id, lifecycle_id"
)


def create_trade_signal(
    conn: sqlite3.Connection,
    raw_message_id: int,
    trader_id: int,
    symbol: str,
    action: str,
    option_type: str | None = None,
    price: Decimal | None = None,
    expiration: str | None = None,
    position_size: str | None = None,
    strike: Decimal | None = None,
    expiration_raw: str | None = None,
    event_type: str | None = None,
    qualifier: str | None = None,
    stated_entry_price: Decimal | None = None,
    stated_return_pct: Decimal | None = None,
    notes: str | None = None,
    extraction_id: int | None = None,
) -> TradeSignal:
    """Insert a new trade signal row.

    Args:
        conn: An open sqlite3.Connection.
        raw_message_id: FK to raw_messages.id.
        trader_id: FK to traders.id.
        symbol: Ticker symbol.
        action: Free-text trade action (e.g. BTO/STC, or BOUGHT/SOLD),
            stored exactly as supplied (not stripped, uppercased, or
            otherwise normalized, and never aliased to another verb).
        option_type: Free-text call/put, or None for non-option trades.
        price: A Decimal price, or None. Never a float or string.
        expiration: ISO8601 date string, or None.
        position_size: Raw wording of position size, or None.
        strike: A Decimal strike price, or None. Never a float or string.
        expiration_raw: Verbatim expiration token as it appeared in the
            message, before year resolution, or None.
        event_type: Derived lifecycle event kind, or None.
        qualifier: Raw fraction text, "ALL OUT", or a bracket annotation,
            or None.
        stated_entry_price: A Decimal entry price as stated in the
            message's own text, or None. Never a float or string.
        stated_return_pct: A Decimal return percentage as stated in the
            message's own text, or None. Never a float or string. Advisory
            only.
        notes: Free-text commentary from the message, or None.
        extraction_id: FK to message_extractions.id, or None.

    Returns:
        The newly created TradeSignal, including its generated id,
        created_at, and updated_at.

    Raises:
        ValueError: If raw_message_id or trader_id is None, or if symbol or
            action is None, empty, or whitespace-only.
        TypeError: If price, strike, stated_entry_price, or
            stated_return_pct is supplied and is not a Decimal.
        sqlite3.IntegrityError: If raw_message_id, trader_id, or
            extraction_id does not reference an existing row.
    """
    _validate_trade_signal_required_fields(raw_message_id, trader_id, symbol, action)
    price_text = _serialize_price(price)
    strike_text = _serialize_price(strike)
    stated_entry_price_text = _serialize_price(stated_entry_price)
    stated_return_pct_text = _serialize_price(stated_return_pct)

    cursor = conn.execute(
        "INSERT INTO trade_signals "
        "(raw_message_id, trader_id, symbol, action, option_type, price, "
        "expiration, position_size, strike, expiration_raw, event_type, "
        "qualifier, stated_entry_price, stated_return_pct, notes, extraction_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            raw_message_id,
            trader_id,
            symbol,
            action,
            option_type,
            price_text,
            expiration,
            position_size,
            strike_text,
            expiration_raw,
            event_type,
            qualifier,
            stated_entry_price_text,
            stated_return_pct_text,
            notes,
            extraction_id,
        ),
    )
    row = conn.execute(
        f"SELECT {_TRADE_SIGNAL_COLUMNS} FROM trade_signals WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return _row_to_trade_signal(row)


def get_trade_signal_by_id(
    conn: sqlite3.Connection,
    trade_signal_id: int,
) -> TradeSignal | None:
    """Look up a trade signal by id.

    Args:
        conn: An open sqlite3.Connection.
        trade_signal_id: Primary key to look up.

    Returns:
        The matching TradeSignal, or None if no row exists with this id.
    """
    row = conn.execute(
        f"SELECT {_TRADE_SIGNAL_COLUMNS} FROM trade_signals WHERE id = ?",
        (trade_signal_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_trade_signal(row)


def validate_trade_signal_update_fields(changed_fields: dict) -> None:
    """Validate a proposed set of trade_signals update fields.

    Single source of truth for update-field validation: both
    update_trade_signal() (below) and TradeService.update_trade_signal()
    (database/service.py) call this, so the two layers cannot diverge on
    what counts as a valid update. This function only validates - it makes
    no database calls and performs no serialization.

    Args:
        changed_fields: Proposed field/value pairs, in the same shape as
            update_trade_signal()'s **changed_fields.

    Raises:
        ValueError: If changed_fields is empty, contains an unknown field
            name, contains a protected field (id, created_at, updated_at),
            or sets a required field (raw_message_id, trader_id, symbol,
            action) to an invalid value.
        TypeError: If price is supplied and is not a Decimal or None.
    """
    if not changed_fields:
        raise ValueError("update_trade_signal requires at least one field to update.")

    unknown_fields = (
        set(changed_fields) - _TRADE_SIGNAL_EDITABLE_FIELDS - _TRADE_SIGNAL_PROTECTED_FIELDS
    )
    if unknown_fields:
        raise ValueError(f"Unknown trade_signal field(s): {sorted(unknown_fields)}")

    protected_fields = set(changed_fields) & _TRADE_SIGNAL_PROTECTED_FIELDS
    if protected_fields:
        raise ValueError(f"Cannot update protected field(s): {sorted(protected_fields)}")

    if "raw_message_id" in changed_fields and changed_fields["raw_message_id"] is None:
        raise ValueError("raw_message_id is required.")
    if "trader_id" in changed_fields and changed_fields["trader_id"] is None:
        raise ValueError("trader_id is required.")
    if "symbol" in changed_fields:
        symbol = changed_fields["symbol"]
        if symbol is None or not symbol.strip():
            raise ValueError("symbol must not be empty or whitespace-only.")
    if "action" in changed_fields:
        action = changed_fields["action"]
        if action is None or not action.strip():
            raise ValueError("action must not be empty or whitespace-only.")
    if "price" in changed_fields:
        price = changed_fields["price"]
        if price is not None and not isinstance(price, Decimal):
            raise TypeError(
                f"price must be a Decimal or None, got {type(price).__name__}."
            )


def update_trade_signal(
    conn: sqlite3.Connection,
    trade_signal_id: int,
    **changed_fields,
) -> TradeSignal | None:
    """Apply a partial update to an existing trade signal.

    Only the fields explicitly passed in changed_fields are updated;
    updated_at is always bumped to CURRENT_TIMESTAMP on a successful update.
    This function does not write to trade_signal_edits and performs no
    audit snapshot - that is out of scope for this milestone.

    Args:
        conn: An open sqlite3.Connection.
        trade_signal_id: Primary key of the trade signal to update.
        **changed_fields: One or more of raw_message_id, trader_id, symbol,
            action, option_type, price, expiration, position_size. Optional
            fields may be explicitly set to None.

    Returns:
        The updated TradeSignal, or None if no trade signal exists with
        this id.

    Raises:
        ValueError: If changed_fields is empty, contains an unknown field
            name, contains a protected field (id, created_at, updated_at),
            or sets a required field (raw_message_id, trader_id, symbol,
            action) to an invalid value.
        TypeError: If price is supplied and is not a Decimal.
    """
    validate_trade_signal_update_fields(changed_fields)

    values = dict(changed_fields)
    if "price" in values:
        values["price"] = _serialize_price(values["price"])

    set_clauses = [f"{field} = ?" for field in values]
    set_clauses.append("updated_at = CURRENT_TIMESTAMP")
    params = list(values.values()) + [trade_signal_id]

    conn.execute(
        f"UPDATE trade_signals SET {', '.join(set_clauses)} WHERE id = ?",
        params,
    )

    return get_trade_signal_by_id(conn, trade_signal_id)


def _row_to_trade_signal_edit(row: sqlite3.Row) -> TradeSignalEdit:
    """Map a ``trade_signal_edits`` table row to a TradeSignalEdit model.

    Args:
        row: A row from the trade_signal_edits table, with all columns
            selected.

    Returns:
        The corresponding TradeSignalEdit.
    """
    return TradeSignalEdit(
        id=row["id"],
        trade_signal_id=row["trade_signal_id"],
        previous_values=row["previous_values"],
        edited_at=row["edited_at"],
    )


def create_trade_signal_edit(
    conn: sqlite3.Connection,
    trade_signal_id: int,
    previous_values: dict,
) -> TradeSignalEdit:
    """Insert a full-row JSON snapshot of a trade signal's pre-edit values.

    This function only records a snapshot; it does not decide when an edit
    occurs or call update_trade_signal - that orchestration belongs to
    TradeService (Milestone 2B.6).

    Args:
        conn: An open sqlite3.Connection.
        trade_signal_id: FK to trade_signals.id.
        previous_values: The full pre-edit trade_signals row, as a dict. No
            schema is enforced on its contents.

    Returns:
        The newly created TradeSignalEdit, including its generated id and
        DB-defaulted edited_at.

    Raises:
        ValueError: If trade_signal_id is None, or if previous_values is an
            empty dict.
        TypeError: If previous_values is not a dict (including None).
        sqlite3.IntegrityError: If trade_signal_id does not reference an
            existing trade_signals row.
    """
    if trade_signal_id is None:
        raise ValueError("trade_signal_id is required.")
    if not isinstance(previous_values, dict):
        raise TypeError(
            f"previous_values must be a dict, got {type(previous_values).__name__}."
        )
    if not previous_values:
        raise ValueError("previous_values must not be an empty dict.")

    serialized_previous_values = json.dumps(previous_values, sort_keys=True)

    cursor = conn.execute(
        "INSERT INTO trade_signal_edits (trade_signal_id, previous_values) "
        "VALUES (?, ?)",
        (trade_signal_id, serialized_previous_values),
    )
    row = conn.execute(
        "SELECT id, trade_signal_id, previous_values, edited_at "
        "FROM trade_signal_edits WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return _row_to_trade_signal_edit(row)


def get_trade_signal_edits(
    conn: sqlite3.Connection,
    trade_signal_id: int,
) -> list[TradeSignalEdit]:
    """Look up the edit history for a trade signal.

    Args:
        conn: An open sqlite3.Connection.
        trade_signal_id: FK to trade_signals.id.

    Returns:
        All matching TradeSignalEdits, ordered by id ascending (i.e.
        chronologically). Empty list if none exist.
    """
    rows = conn.execute(
        "SELECT id, trade_signal_id, previous_values, edited_at "
        "FROM trade_signal_edits WHERE trade_signal_id = ? ORDER BY id",
        (trade_signal_id,),
    ).fetchall()
    return [_row_to_trade_signal_edit(row) for row in rows]


def get_trade_signals_matching(
    conn: sqlite3.Connection,
    trader_id: int,
    symbol: str,
    action: str,
    option_type: str | None,
    price: Decimal | None,
    expiration: str | None,
    window_start: str,
    window_end: str,
) -> list[TradeSignal]:
    """Look up trade signals matching an exact field set within a time range.

    Purely mechanical field- and time-range matching: this function has no
    concept of "duplicate" or "advisory window" - that meaning belongs to
    TradeService (Milestone 2B.6a). option_type, price, and expiration are
    matched with SQL's NULL-safe ``IS`` so that None on both sides counts as
    a match.

    Args:
        conn: An open sqlite3.Connection.
        trader_id: FK to traders.id.
        symbol: Ticker symbol to match exactly.
        action: Free-text trade action to match exactly.
        option_type: Free-text call/put to match exactly, or None.
        price: A Decimal price to match exactly, or None. Never a float or
            string.
        expiration: ISO8601 date string to match exactly, or None.
        window_start: Inclusive lower bound on trade_signals.created_at.
        window_end: Inclusive upper bound on trade_signals.created_at.

    Returns:
        All matching TradeSignals, ordered by id ascending. Empty list if
        none match.

    Raises:
        ValueError: If trader_id is None, or if symbol or action is None,
            empty, or whitespace-only.
        TypeError: If price is supplied and is not a Decimal.
    """
    if trader_id is None:
        raise ValueError("trader_id is required.")
    if symbol is None or not symbol.strip():
        raise ValueError("symbol must not be empty or whitespace-only.")
    if action is None or not action.strip():
        raise ValueError("action must not be empty or whitespace-only.")
    price_text = _serialize_price(price)

    rows = conn.execute(
        f"SELECT {_TRADE_SIGNAL_COLUMNS} "
        "FROM trade_signals "
        "WHERE trader_id = ? AND symbol = ? AND action = ? "
        "AND option_type IS ? AND price IS ? AND expiration IS ? "
        "AND created_at BETWEEN ? AND ? "
        "ORDER BY id",
        (
            trader_id,
            symbol,
            action,
            option_type,
            price_text,
            expiration,
            window_start,
            window_end,
        ),
    ).fetchall()
    return [_row_to_trade_signal(row) for row in rows]


def _date_range_bounds(date: str) -> tuple[str, str]:
    """Compute inclusive-start/exclusive-end created_at bounds for a date.

    Args:
        date: A calendar date string, "YYYY-MM-DD".

    Returns:
        A (start, end) pair of "YYYY-MM-DD HH:MM:SS" strings: start is
        date at 00:00:00 (inclusive), end is the following calendar day at
        00:00:00 (exclusive).

    Raises:
        ValueError: If date is not in "YYYY-MM-DD" format.
    """
    try:
        start = datetime.strptime(date, _REVIEW_DATE_FORMAT)
    except ValueError as exc:
        raise ValueError(f"date must match the format {_REVIEW_DATE_FORMAT!r}.") from exc

    end = start + timedelta(days=1)
    return start.strftime(_REVIEW_TIMESTAMP_FORMAT), end.strftime(_REVIEW_TIMESTAMP_FORMAT)


def get_trade_signals_for_review(
    conn: sqlite3.Connection,
    *,
    source_name: str | None = None,
    trader_name: str | None = None,
    symbol: str | None = None,
    date: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """List persisted trade signals for read-only review, newest first.

    Joins trade_signals with traders, raw_messages, and sources in a single
    query to provide full display context. Every active filter is applied
    in SQL before LIMIT; a blank or None filter omits its WHERE fragment
    entirely rather than matching everything via a wildcard. This function
    performs no writes and never modifies any row.

    Recovery Milestone R5: a signal whose extraction_id points at a
    superseded (non-current) message_extractions row is excluded - only
    the current derived signal set for a reprocessed message is shown by
    default. A signal with extraction_id IS NULL (every pre-R5 row, and
    every row from the unmodified legacy TradeService.ingest_message()
    path) has nothing to be superseded by and is always treated as
    current. The LEFT JOIN to message_extractions is keyed on that
    table's own primary key (message_extractions.id), so it can match at
    most one row per trade_signals.extraction_id and can never duplicate
    a trade_signals row in the result set.

    Args:
        conn: An open sqlite3.Connection.
        source_name: Exact sources.name to filter by, or None/blank to omit.
        trader_name: Exact traders.name to filter by, or None/blank to
            omit.
        symbol: Ticker symbol to filter by, matched case-insensitively
            (both sides normalized to uppercase), or None/blank to omit.
        date: Calendar date "YYYY-MM-DD" to filter trade_signals.created_at
            by - inclusive start of that day, exclusive start of the
            following day - or None/blank to omit. Never filters on
            raw_messages.received_at.
        limit: Maximum number of rows to return, applied in SQL via LIMIT.
            Defaults to 100.

    Returns:
        A list of dicts, newest first (trade_signals.id descending), each
        with keys: id, symbol, action, option_type, price, expiration,
        position_size, created_at, updated_at, source_name, trader_name,
        external_trader_id, raw_text. price is the exact stored decimal
        string, never converted to float. Empty list if nothing matches.

    Raises:
        ValueError: If date is supplied and is not in "YYYY-MM-DD" format.
    """
    where_clauses: list[str] = [
        "(trade_signals.extraction_id IS NULL OR message_extractions.is_current = 1)"
    ]
    params: list = []

    if source_name and source_name.strip():
        where_clauses.append("sources.name = ?")
        params.append(source_name)

    if trader_name and trader_name.strip():
        where_clauses.append("traders.name = ?")
        params.append(trader_name)

    if symbol and symbol.strip():
        where_clauses.append("UPPER(trade_signals.symbol) = UPPER(?)")
        params.append(symbol)

    if date and date.strip():
        range_start, range_end = _date_range_bounds(date)
        where_clauses.append("trade_signals.created_at >= ?")
        where_clauses.append("trade_signals.created_at < ?")
        params.append(range_start)
        params.append(range_end)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    rows = conn.execute(
        "SELECT "
        "trade_signals.id AS id, "
        "trade_signals.symbol AS symbol, "
        "trade_signals.action AS action, "
        "trade_signals.option_type AS option_type, "
        "trade_signals.price AS price, "
        "trade_signals.expiration AS expiration, "
        "trade_signals.position_size AS position_size, "
        "trade_signals.created_at AS created_at, "
        "trade_signals.updated_at AS updated_at, "
        "sources.name AS source_name, "
        "traders.name AS trader_name, "
        "traders.external_trader_id AS external_trader_id, "
        "raw_messages.raw_text AS raw_text "
        "FROM trade_signals "
        "JOIN traders ON trade_signals.trader_id = traders.id "
        "JOIN raw_messages ON trade_signals.raw_message_id = raw_messages.id "
        "JOIN sources ON raw_messages.source_id = sources.id "
        "LEFT JOIN message_extractions "
        "ON trade_signals.extraction_id = message_extractions.id "
        f"{where_sql} "
        "ORDER BY trade_signals.id DESC "
        "LIMIT ?",
        (*params, limit),
    ).fetchall()

    return [dict(row) for row in rows]


# Public (Recovery Milestone R9a) so database.service can import and
# reuse this exact value rather than maintaining a second, independently
# defined sentinel constant - the one and only definition of the
# "unspecified" channel's external id.
UNSPECIFIED_CHANNEL_EXTERNAL_ID = "__unspecified__"


def _row_to_channel(row: sqlite3.Row) -> Channel:
    """Map a ``channels`` table row to a Channel model.

    Args:
        row: A row from the channels table, with all columns selected.

    Returns:
        The corresponding Channel.
    """
    return Channel(
        id=row["id"],
        source_id=row["source_id"],
        external_channel_id=row["external_channel_id"],
        name=row["name"],
        created_at=row["created_at"],
    )


def create_channel(
    conn: sqlite3.Connection,
    source_id: int,
    external_channel_id: str | None = None,
    name: str | None = None,
) -> Channel:
    """Insert a new channel row.

    Args:
        conn: An open sqlite3.Connection.
        source_id: FK to sources.id.
        external_channel_id: Stable source-provided channel ID, when
            available.
        name: Display name/slug for the channel, when available.

    Returns:
        The newly created Channel, including its generated id and
        created_at.

    Raises:
        sqlite3.IntegrityError: If source_id does not reference an existing
            source, or if external_channel_id is not None and already
            exists for this source_id.
    """
    cursor = conn.execute(
        "INSERT INTO channels (source_id, external_channel_id, name) VALUES (?, ?, ?)",
        (source_id, external_channel_id, name),
    )
    row = conn.execute(
        "SELECT id, source_id, external_channel_id, name, created_at "
        "FROM channels WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return _row_to_channel(row)


def get_channel_by_external_id(
    conn: sqlite3.Connection,
    source_id: int,
    external_channel_id: str,
) -> Channel | None:
    """Look up a channel by source and external channel ID.

    Args:
        conn: An open sqlite3.Connection.
        source_id: FK to sources.id.
        external_channel_id: Source-provided channel ID to search for.

    Returns:
        The matching Channel, or None if no row exists for this
        source_id/external_channel_id pair.
    """
    row = conn.execute(
        "SELECT id, source_id, external_channel_id, name, created_at "
        "FROM channels WHERE source_id = ? AND external_channel_id = ?",
        (source_id, external_channel_id),
    ).fetchone()
    if row is None:
        return None
    return _row_to_channel(row)


def get_or_create_channel(
    conn: sqlite3.Connection,
    source_id: int,
    external_channel_id: str,
    name: str | None = None,
) -> Channel:
    """Return the existing channel with this external ID, creating it if needed.

    Args:
        conn: An open sqlite3.Connection.
        source_id: FK to sources.id.
        external_channel_id: Source-provided channel ID to look up or
            create. Must not be None - use
            get_or_create_unspecified_channel() for messages with no real
            channel identifier.
        name: Display name/slug to store if a new row is created. Ignored
            if a matching channel already exists.

    Returns:
        The existing or newly created Channel.
    """
    existing = get_channel_by_external_id(conn, source_id, external_channel_id)
    if existing is not None:
        return existing
    return create_channel(conn, source_id, external_channel_id, name)


def get_or_create_unspecified_channel(conn: sqlite3.Connection, source_id: int) -> Channel:
    """Return the sentinel "unspecified" channel for a source, creating it if needed.

    Used for raw messages with no real channel identifier (e.g. today's
    single-message manual entry), so channel-scoped queries (idempotency,
    per-channel checkpoints) always have a channel to key on.

    Args:
        conn: An open sqlite3.Connection.
        source_id: FK to sources.id.

    Returns:
        The existing or newly created sentinel Channel for this source.
    """
    return get_or_create_channel(
        conn, source_id, UNSPECIFIED_CHANNEL_EXTERNAL_ID, name="unspecified"
    )


def get_channel_by_id(conn: sqlite3.Connection, channel_id: int) -> Channel | None:
    """Look up a channel by primary key (Recovery Milestone R9a).

    One read-only primary-key lookup - never creates or modifies data.
    Mirrors get_trader_by_id()'s own by-primary-key convention exactly.

    Args:
        conn: An open sqlite3.Connection.
        channel_id: Primary key to look up.

    Returns:
        The matching Channel, or None if no row exists with this id.
    """
    row = conn.execute(
        "SELECT id, source_id, external_channel_id, name, created_at "
        "FROM channels WHERE id = ?",
        (channel_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_channel(row)


def list_channels(conn: sqlite3.Connection, source_id: int) -> list[Channel]:
    """List every channel for one source (Recovery Milestone R9a).

    Includes every channel row for source_id, including a channel with
    zero raw_messages rows (unlike get_channel_ingestion_cursors(), which
    only returns a channel that already has at least one message) and
    including the __unspecified__ sentinel channel - this function is
    deliberately repository-generic; filtering the sentinel out belongs to
    the Bulk Channel Import service layer, not here.

    Args:
        conn: An open sqlite3.Connection.
        source_id: FK to sources.id.

    Returns:
        Every Channel for source_id, ordered by display name when
        present, otherwise external_channel_id, case-insensitively, with
        id as a deterministic tie-breaker (covering the case where both
        are absent or two channels share the same case-insensitive
        display value). A null, empty, or whitespace-only name is treated
        as absent (falls back to external_channel_id, never sorted as if
        it were a blank-string name) via NULLIF(TRIM(name), '').
    """
    rows = conn.execute(
        "SELECT id, source_id, external_channel_id, name, created_at "
        "FROM channels WHERE source_id = ? "
        "ORDER BY "
        "LOWER(COALESCE(NULLIF(TRIM(name), ''), external_channel_id, '')) ASC, "
        "id ASC",
        (source_id,),
    ).fetchall()
    return [_row_to_channel(row) for row in rows]


def _row_to_import_batch(row: sqlite3.Row) -> ImportBatch:
    """Map an ``import_batches`` table row to an ImportBatch model.

    Args:
        row: A row from the import_batches table, with all columns selected.

    Returns:
        The corresponding ImportBatch.
    """
    return ImportBatch(
        id=row["id"],
        source_id=row["source_id"],
        reference_date=row["reference_date"],
        timezone=row["timezone"],
        raw_input_text=row["raw_input_text"],
        created_at=row["created_at"],
    )


def create_import_batch(
    conn: sqlite3.Connection,
    source_id: int,
    reference_date: str,
    timezone: str,
    raw_input_text: str | None = None,
) -> ImportBatch:
    """Insert a new import batch row.

    reference_date and timezone anchor the deterministic date/time
    resolution rule for every message later segmented from this batch -
    never the wall clock.

    Args:
        conn: An open sqlite3.Connection.
        source_id: FK to sources.id.
        reference_date: Calendar date ("YYYY-MM-DD") for this batch.
        timezone: Timezone name for this batch.
        raw_input_text: The complete pasted batch text, before
            segmentation, or None.

    Returns:
        The newly created ImportBatch, including its generated id and
        created_at.

    Raises:
        ValueError: If reference_date or timezone is empty or
            whitespace-only.
        sqlite3.IntegrityError: If source_id does not reference an existing
            source.
    """
    if not reference_date or not reference_date.strip():
        raise ValueError("reference_date must not be empty or whitespace-only.")
    if not timezone or not timezone.strip():
        raise ValueError("timezone must not be empty or whitespace-only.")

    cursor = conn.execute(
        "INSERT INTO import_batches (source_id, reference_date, timezone, raw_input_text) "
        "VALUES (?, ?, ?, ?)",
        (source_id, reference_date, timezone, raw_input_text),
    )
    row = conn.execute(
        "SELECT id, source_id, reference_date, timezone, raw_input_text, created_at "
        "FROM import_batches WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return _row_to_import_batch(row)


def get_import_batch_by_id(
    conn: sqlite3.Connection,
    import_batch_id: int,
) -> ImportBatch | None:
    """Look up an import batch by id.

    Args:
        conn: An open sqlite3.Connection.
        import_batch_id: Primary key to look up.

    Returns:
        The matching ImportBatch, or None if no row exists with this id.
    """
    row = conn.execute(
        "SELECT id, source_id, reference_date, timezone, raw_input_text, created_at "
        "FROM import_batches WHERE id = ?",
        (import_batch_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_import_batch(row)


_MESSAGE_EXTRACTION_PARSE_STATUSES = frozenset(
    {"parsed", "partially_parsed", "unrecognized", "failed"}
)


def _row_to_message_extraction(row: sqlite3.Row) -> MessageExtraction:
    """Map a ``message_extractions`` table row to a MessageExtraction model.

    Args:
        row: A row from the message_extractions table, with all columns
            selected.

    Returns:
        The corresponding MessageExtraction, with ambiguity_flags
        deserialized from its stored JSON representation (or None if the
        column is NULL).
    """
    ambiguity_flags = (
        json.loads(row["ambiguity_flags"]) if row["ambiguity_flags"] is not None else None
    )
    return MessageExtraction(
        id=row["id"],
        raw_message_id=row["raw_message_id"],
        parser_version=row["parser_version"],
        parse_status=row["parse_status"],
        confidence=row["confidence"],
        ambiguity_flags=ambiguity_flags,
        is_current=bool(row["is_current"]),
        superseded_at=row["superseded_at"],
        created_at=row["created_at"],
    )


def create_message_extraction(
    conn: sqlite3.Connection,
    raw_message_id: int,
    parser_version: str,
    parse_status: str,
    confidence: float | None = None,
    ambiguity_flags: list | None = None,
) -> MessageExtraction:
    """Insert a new message extraction row, marked as the current one.

    Args:
        conn: An open sqlite3.Connection.
        raw_message_id: FK to raw_messages.id.
        parser_version: Identifier of the extractor version that produced
            this attempt.
        parse_status: One of 'parsed', 'partially_parsed', 'unrecognized',
            'failed'.
        confidence: Extraction confidence in [0, 1], or None.
        ambiguity_flags: List of JSON-serializable flag strings, or None.

    Returns:
        The newly created MessageExtraction, including its generated id
        and created_at.

    Raises:
        ValueError: If parser_version is empty or whitespace-only, or if
            parse_status is not one of the recognized values.
        sqlite3.IntegrityError: If raw_message_id does not reference an
            existing row, or if raw_message_id already has a current
            (is_current=1) extraction - callers must call
            supersede_extraction() on the existing current row first.
    """
    if not parser_version or not parser_version.strip():
        raise ValueError("parser_version must not be empty or whitespace-only.")
    if parse_status not in _MESSAGE_EXTRACTION_PARSE_STATUSES:
        raise ValueError(
            f"parse_status must be one of {sorted(_MESSAGE_EXTRACTION_PARSE_STATUSES)}."
        )

    serialized_flags = (
        json.dumps(ambiguity_flags, sort_keys=True) if ambiguity_flags is not None else None
    )

    cursor = conn.execute(
        "INSERT INTO message_extractions "
        "(raw_message_id, parser_version, parse_status, confidence, ambiguity_flags, is_current) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (raw_message_id, parser_version, parse_status, confidence, serialized_flags),
    )
    row = conn.execute(
        "SELECT id, raw_message_id, parser_version, parse_status, confidence, "
        "ambiguity_flags, is_current, superseded_at, created_at "
        "FROM message_extractions WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return _row_to_message_extraction(row)


def get_current_extraction(
    conn: sqlite3.Connection,
    raw_message_id: int,
) -> MessageExtraction | None:
    """Look up the current (non-superseded) extraction for a raw message.

    Args:
        conn: An open sqlite3.Connection.
        raw_message_id: FK to raw_messages.id.

    Returns:
        The matching current MessageExtraction, or None if this raw
        message has no current extraction.
    """
    row = conn.execute(
        "SELECT id, raw_message_id, parser_version, parse_status, confidence, "
        "ambiguity_flags, is_current, superseded_at, created_at "
        "FROM message_extractions WHERE raw_message_id = ? AND is_current = 1",
        (raw_message_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_message_extraction(row)


def supersede_extraction(
    conn: sqlite3.Connection,
    extraction_id: int,
) -> MessageExtraction | None:
    """Mark an extraction row as no longer current.

    Does not insert a replacement extraction and does not touch
    raw_messages or trade_signals - orchestrating a full reprocess (marking
    the old extraction superseded, creating a new one, and re-deriving
    trade signals) is a later milestone's responsibility.

    Args:
        conn: An open sqlite3.Connection.
        extraction_id: Primary key of the extraction to supersede.

    Returns:
        The updated MessageExtraction, or None if no extraction exists
        with this id.
    """
    conn.execute(
        "UPDATE message_extractions SET is_current = 0, superseded_at = CURRENT_TIMESTAMP "
        "WHERE id = ?",
        (extraction_id,),
    )
    row = conn.execute(
        "SELECT id, raw_message_id, parser_version, parse_status, confidence, "
        "ambiguity_flags, is_current, superseded_at, created_at "
        "FROM message_extractions WHERE id = ?",
        (extraction_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_message_extraction(row)


# ---------------------------------------------------------------------------
# Recovery Milestone R5: additional raw_messages/import_batches helpers and
# checkpoint queries. No schema change - every function below reads or
# writes only existing columns/tables.
# ---------------------------------------------------------------------------


def get_trader_by_id(
    conn: sqlite3.Connection,
    trader_id: int,
) -> Trader | None:
    """Look up a trader by primary key.

    Used by reprocessing to confirm a persisted resolved_trader_id (see
    the "_r5_provenance" metadata block) still refers to an existing
    trader before reusing it, rather than blindly trusting a stale id.

    Args:
        conn: An open sqlite3.Connection.
        trader_id: Primary key to look up.

    Returns:
        The matching Trader, or None if no row exists with this id.
    """
    row = conn.execute(
        "SELECT id, source_id, name, external_trader_id, created_at, canonical_name "
        "FROM traders WHERE id = ?",
        (trader_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_trader(row)


def get_raw_message_by_id(
    conn: sqlite3.Connection,
    raw_message_id: int,
) -> RawMessage | None:
    """Look up a raw message by primary key.

    Args:
        conn: An open sqlite3.Connection.
        raw_message_id: Primary key to look up.

    Returns:
        The matching RawMessage, or None if no row exists with this id.
    """
    row = conn.execute(
        f"SELECT {_RAW_MESSAGE_COLUMNS} FROM raw_messages WHERE id = ?",
        (raw_message_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_raw_message(row)


def get_raw_message_ids_by_import_batch(
    conn: sqlite3.Connection,
    import_batch_id: int,
) -> list[int]:
    """List the ids of every raw message linked to an import batch.

    Args:
        conn: An open sqlite3.Connection.
        import_batch_id: FK to import_batches.id.

    Returns:
        Every matching raw_messages.id, ordered ascending (deterministic,
        insertion order). Empty list if none match or the import batch
        does not exist.
    """
    rows = conn.execute(
        "SELECT id FROM raw_messages WHERE import_batch_id = ? ORDER BY id",
        (import_batch_id,),
    ).fetchall()
    return [row["id"] for row in rows]


def delete_import_batch_if_empty(
    conn: sqlite3.Connection,
    import_batch_id: int,
) -> bool:
    """Delete an import_batches row only if no raw_messages reference it.

    A narrow, defensive helper for the theoretical case where every
    intended-new message in a batch is reclassified as a duplicate via a
    unique-constraint race (see TradeService.ingest_batch's narrow
    IntegrityError-to-duplicate carve-out), which would otherwise leave an
    orphaned, empty import_batches row behind. This function never deletes
    a batch that any raw_messages row still references.

    Args:
        conn: An open sqlite3.Connection.
        import_batch_id: Primary key of the import batch to conditionally
            delete.

    Returns:
        True if the row was deleted, False if it still has at least one
        linked raw_messages row (not deleted) or did not exist.
    """
    cursor = conn.execute(
        "DELETE FROM import_batches WHERE id = ? "
        "AND NOT EXISTS (SELECT 1 FROM raw_messages WHERE import_batch_id = ?)",
        (import_batch_id, import_batch_id),
    )
    return cursor.rowcount > 0


def get_channel_ingestion_cursors(conn: sqlite3.Connection) -> list[dict]:
    """List the most-recently-inserted raw message per channel.

    The "ingestion/audit cursor" half of the R5 composite channel
    checkpoint: purely insertion-order based (raw_messages.id is always
    present and monotonically increasing), so it is always defined and
    never ambiguous, unlike a timestamp-based cursor.

    Args:
        conn: An open sqlite3.Connection.

    Returns:
        One dict per channel that has at least one raw_messages row, with
        keys: channel_id, channel_external_id, channel_name,
        last_ingested_raw_message_id, last_ingested_at,
        last_import_batch_id. Empty list if no channel has any raw
        message.
    """
    rows = conn.execute(
        "SELECT "
        "c.id AS channel_id, "
        "c.external_channel_id AS channel_external_id, "
        "c.name AS channel_name, "
        "rm.id AS last_ingested_raw_message_id, "
        "rm.ingested_at AS last_ingested_at, "
        "rm.import_batch_id AS last_import_batch_id "
        "FROM channels c "
        "JOIN raw_messages rm ON rm.channel_id = c.id "
        "WHERE rm.id = (SELECT MAX(id) FROM raw_messages WHERE channel_id = c.id) "
        "ORDER BY c.id"
    ).fetchall()
    return [dict(row) for row in rows]


def get_channel_chronological_checkpoints(conn: sqlite3.Connection) -> list[dict]:
    """List the latest resolved authoritative timestamp per channel.

    The "chronological resume point" half of the R5 composite channel
    checkpoint. Only channels with at least one non-NULL
    raw_messages.received_at appear in the result - a channel where every
    message's received_at is NULL is entirely absent here, which is the
    explicit, unambiguous signal that no chronological resume point is
    available for it (the caller must not substitute insertion order).

    Ties (two messages sharing the exact same received_at value) are
    broken deterministically by the higher raw_messages.id.

    This query's correctness as a plain string MAX()/comparison depends
    on every non-NULL received_at value having already been normalized to
    the fixed-width canonical UTC representation
    ("YYYY-MM-DDTHH:MM:SS.ffffff+00:00") before being stored - see
    TradeService._to_canonical_utc_string(). This function performs no
    normalization itself; it only reads whatever was stored.

    Args:
        conn: An open sqlite3.Connection.

    Returns:
        One dict per channel with at least one non-NULL received_at, with
        keys: channel_id, latest_received_raw_message_id,
        latest_received_at, latest_received_external_id. Empty list if no
        channel has any resolved timestamp.
    """
    rows = conn.execute(
        "SELECT "
        "c.id AS channel_id, "
        "rm.id AS latest_received_raw_message_id, "
        "rm.received_at AS latest_received_at, "
        "rm.external_id AS latest_received_external_id "
        "FROM channels c "
        "JOIN raw_messages rm ON rm.channel_id = c.id "
        "WHERE rm.received_at IS NOT NULL "
        "AND rm.received_at = ("
        "    SELECT MAX(received_at) FROM raw_messages "
        "    WHERE channel_id = c.id AND received_at IS NOT NULL"
        ") "
        "AND rm.id = ("
        "    SELECT MAX(id) FROM raw_messages "
        "    WHERE channel_id = c.id AND received_at = rm.received_at"
        ") "
        "ORDER BY c.id"
    ).fetchall()
    return [dict(row) for row in rows]


def _row_to_channel_import_operation(row: sqlite3.Row) -> ChannelImportOperation:
    """Map a ``channel_import_operations`` table row to a
    ChannelImportOperation model (Recovery Milestone R9a).

    Args:
        row: A row from the channel_import_operations table, with all
            columns selected.

    Returns:
        The corresponding ChannelImportOperation.
    """
    return ChannelImportOperation(
        id=row["id"],
        channel_id=row["channel_id"],
        import_batch_id=row["import_batch_id"],
        reference_date=row["reference_date"],
        timezone=row["timezone"],
        processed_count=row["processed_count"],
        stored_count=row["stored_count"],
        duplicate_count=row["duplicate_count"],
        unrecognized_count=row["unrecognized_count"],
        failed_count=row["failed_count"],
        committed_at=row["committed_at"],
    )


def create_channel_import_operation(
    conn: sqlite3.Connection,
    *,
    channel_id: int,
    import_batch_id: int | None,
    reference_date: str,
    timezone: str,
    processed_count: int,
    stored_count: int,
    duplicate_count: int,
    unrecognized_count: int,
    failed_count: int,
) -> ChannelImportOperation:
    """Insert one successful channel_import_operations row (Recovery
    Milestone R9a).

    A plain insert-and-read-back function: inserts only the supplied
    successful-operation values, relying entirely on the
    channel_import_operations table's own CHECK constraints (see
    database/migrations/0008_channel_import_operations.sql) to enforce
    every count invariant - this function performs no validation of its
    own beyond what the database itself enforces. Never commits; the
    caller (Recovery Milestone R9b's atomic import transaction, not yet
    implemented) owns the transaction this insert participates in.

    Args:
        conn: An open sqlite3.Connection.
        channel_id: FK to channels.id.
        import_batch_id: FK to import_batches.id, or None for a
            duplicate-only operation.
        reference_date: The operation's batch-wide reference date
            ("YYYY-MM-DD").
        timezone: The operation's IANA timezone name.
        processed_count: Total messages segmented from this operation's
            batch.
        stored_count: Count of messages newly stored.
        duplicate_count: Count of messages already present.
        unrecognized_count: Subset of stored_count that was unrecognized.
        failed_count: Subset of stored_count that failed extraction.

    Returns:
        The newly created ChannelImportOperation, including its generated
        id and committed_at.

    Raises:
        sqlite3.IntegrityError: If channel_id/import_batch_id does not
            reference an existing row, or if any CHECK constraint is
            violated (e.g. processed_count < 15, a count-invariant
            mismatch, or an inconsistent stored_count/import_batch_id
            pairing).
    """
    cursor = conn.execute(
        "INSERT INTO channel_import_operations ("
        "channel_id, import_batch_id, reference_date, timezone, "
        "processed_count, stored_count, duplicate_count, "
        "unrecognized_count, failed_count"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            channel_id,
            import_batch_id,
            reference_date,
            timezone,
            processed_count,
            stored_count,
            duplicate_count,
            unrecognized_count,
            failed_count,
        ),
    )
    row = conn.execute(
        "SELECT id, channel_id, import_batch_id, reference_date, timezone, "
        "processed_count, stored_count, duplicate_count, unrecognized_count, "
        "failed_count, committed_at "
        "FROM channel_import_operations WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return _row_to_channel_import_operation(row)


def get_latest_channel_import_operation(
    conn: sqlite3.Connection, channel_id: int
) -> ChannelImportOperation | None:
    """Return one channel's most recent successful import operation
    (Recovery Milestone R9a).

    Read-only; never writes.

    Args:
        conn: An open sqlite3.Connection.
        channel_id: FK to channels.id.

    Returns:
        The ChannelImportOperation with the highest id for channel_id, or
        None if this channel has no channel_import_operations row.
    """
    row = conn.execute(
        "SELECT id, channel_id, import_batch_id, reference_date, timezone, "
        "processed_count, stored_count, duplicate_count, unrecognized_count, "
        "failed_count, committed_at "
        "FROM channel_import_operations WHERE channel_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (channel_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_channel_import_operation(row)


# ---------------------------------------------------------------------------
# Recovery Milestone R6.3: repository layer for trade_lifecycles /
# trade_lifecycle_events / trade_signals.lifecycle_id (schema added by
# R6.1, pure matching engine added by R6.2). No matching/linking
# orchestration lives here - that is TradeService's job (R6.4, not yet
# implemented). Every function below either discovers data for the R6.4
# orchestration layer to feed into database.lifecycle.build_lifecycle_sequence(),
# or persists exactly what that pure function computed. As with every
# other function in this module, callers own the transaction - nothing
# here commits or rolls back.
# ---------------------------------------------------------------------------


def _decimal_equal(stored_strike_text: str | None, target_strike: Decimal | None) -> bool:
    """Compare a stored strike string to a target Decimal, numerically.

    Never a raw string comparison: "207.50" and "207.5" are the same
    strike (_serialize_price() performs no normalization, so equal
    Decimals can be stored with different textual representations).

    Args:
        stored_strike_text: A trade_signals.strike or trade_lifecycles.strike
            value as read from the database, or None.
        target_strike: The Decimal strike being searched for, or None.

    Returns:
        True if both are None, or both are non-None and numerically
        equal; False otherwise (including exactly one side being None).
    """
    if stored_strike_text is None and target_strike is None:
        return True
    if stored_strike_text is None or target_strike is None:
        return False
    return Decimal(stored_strike_text) == target_strike


def _is_complete_lifecycle_key_shape(
    option_type: str | None, strike_text: str | None, expiration: str | None
) -> bool:
    """Return whether (option_type, strike, expiration) form a valid
    lifecycle key shape: either all three None (equity) or all three
    non-None (a complete option identity). Any other combination is an
    incomplete option identity and must never be treated as a normal
    lifecycle key - never guessed or partially matched.
    """
    all_none = option_type is None and strike_text is None and expiration is None
    all_present = option_type is not None and strike_text is not None and expiration is not None
    return all_none or all_present


def _lifecycle_key_sort_key(key: tuple) -> tuple:
    """A total, deterministic ordering for a (trader_id, symbol,
    option_type, strike, expiration) lifecycle key tuple.

    None-safe: option_type/strike/expiration may each independently be
    None (an equity key, or - for strike - simply absent), which cannot
    be compared directly against a str/Decimal in Python 3. Each field is
    instead represented as (is_present, value_or_placeholder), so two
    keys are always fully orderable regardless of which fields are None,
    and the resulting order never depends on set/dict iteration order or
    SQL row-return order. Shared by every function that must return
    lifecycle keys/violations in a normalized, repeatable order
    (get_distinct_lifecycle_keys_for_signal_ids(),
    _check_invariant_h_multiple_active_per_key()).
    """
    trader_id, symbol, option_type, strike, expiration = key
    return (
        trader_id,
        symbol,
        option_type is not None,
        option_type or "",
        strike is not None,
        strike if strike is not None else Decimal(0),
        expiration is not None,
        expiration or "",
    )


_LIFECYCLE_SIGNAL_SELECT = (
    "ts.id AS trade_signal_id, "
    "ts.raw_message_id AS raw_message_id, "
    "ts.trader_id AS trader_id, "
    "ts.symbol AS symbol, "
    "ts.option_type AS option_type, "
    "ts.strike AS strike, "
    "ts.expiration AS expiration, "
    "ts.event_type AS event_type, "
    "ts.qualifier AS qualifier, "
    "ts.action AS action, "
    "ts.price AS price, "
    "ts.stated_entry_price AS stated_entry_price, "
    "ts.stated_return_pct AS stated_return_pct, "
    "ts.notes AS notes, "
    "ts.extraction_id AS extraction_id, "
    "rm.received_at AS received_at"
)

_LIFECYCLE_SIGNAL_CURRENT_JOIN = (
    "FROM trade_signals ts "
    "JOIN raw_messages rm ON rm.id = ts.raw_message_id "
    "LEFT JOIN message_extractions me ON me.id = ts.extraction_id"
)

_LIFECYCLE_SIGNAL_CURRENT_AND_ELIGIBLE = (
    "ts.event_type IS NOT NULL "
    "AND (ts.extraction_id IS NULL OR me.is_current = 1)"
)


def _row_to_signal_snapshot(row: sqlite3.Row, ordering_key: tuple) -> SignalSnapshot:
    """Map one joined trade_signals/raw_messages row to a SignalSnapshot.

    Args:
        row: A row selected via _LIFECYCLE_SIGNAL_SELECT.
        ordering_key: The already-decided chronological ordering tuple
            for this row within its caller's result set (see
            _order_signal_rows()).

    Returns:
        The corresponding SignalSnapshot.
    """
    return SignalSnapshot(
        trade_signal_id=row["trade_signal_id"],
        raw_message_id=row["raw_message_id"],
        trader_id=row["trader_id"],
        symbol=row["symbol"],
        option_type=row["option_type"],
        strike=row["strike"],
        expiration=row["expiration"],
        event_type=row["event_type"],
        qualifier=row["qualifier"],
        action=row["action"],
        price=row["price"],
        stated_entry_price=row["stated_entry_price"],
        stated_return_pct=row["stated_return_pct"],
        notes=row["notes"],
        extraction_id=row["extraction_id"],
        ordering_key=ordering_key,
    )


def _order_signal_rows(rows: list[sqlite3.Row]) -> list[SignalSnapshot]:
    """Deterministically order a set of candidate signal rows and build
    their SignalSnapshots.

    Uses received_at-based ordering (received_at, raw_message_id,
    trade_signal_id) only when every row in the set has a resolved
    received_at; otherwise falls back to (raw_message_id, trade_signal_id)
    ordering for the entire set - never mixing the two modes within one
    result set, so a handful of unresolved timestamps can never silently
    reorder the rows that do have one.

    Args:
        rows: Candidate rows selected via _LIFECYCLE_SIGNAL_SELECT,
            already filtered to the desired key/current/eligible set but
            not yet ordered.

    Returns:
        SignalSnapshots in the chosen chronological order, each carrying
        the ordering_key actually used.
    """
    if not rows:
        return []

    all_have_received_at = all(row["received_at"] is not None for row in rows)
    if all_have_received_at:
        ordered = sorted(
            rows, key=lambda r: (r["received_at"], r["raw_message_id"], r["trade_signal_id"])
        )
        return [
            _row_to_signal_snapshot(
                row, (row["received_at"], row["raw_message_id"], row["trade_signal_id"])
            )
            for row in ordered
        ]

    ordered = sorted(rows, key=lambda r: (r["raw_message_id"], r["trade_signal_id"]))
    return [
        _row_to_signal_snapshot(row, (row["raw_message_id"], row["trade_signal_id"]))
        for row in ordered
    ]


def get_current_trade_signals_for_key(
    conn: sqlite3.Connection,
    trader_id: int,
    symbol: str,
    option_type: str | None,
    strike: Decimal | None,
    expiration: str | None,
) -> list[SignalSnapshot]:
    """List every current, lifecycle-eligible signal for one exact
    lifecycle key, in deterministic chronological order.

    "Current" is the same rule get_trade_signals_for_review() already
    applies: extraction_id IS NULL OR message_extractions.is_current = 1.
    "Eligible" additionally requires event_type IS NOT NULL - a legacy
    signal from the pre-Recovery ingest_message() path (event_type always
    NULL) can never be lifecycle-linked and is excluded here, not merely
    downstream.

    Key matching: symbol is matched case-insensitively; option_type and
    expiration are matched with SQLite's NULL-safe IS operator (so an
    equity key - option_type/strike/expiration all None - matches
    correctly, which a plain "=" comparison against NULL never would);
    strike is never compared in SQL at all - it is matched exactly via
    Decimal equality in Python, since trade_signals.strike is stored via
    _serialize_price() with no normalization, so "207.50" and "207.5" are
    numerically equal but textually different.

    Args:
        conn: An open sqlite3.Connection.
        trader_id: FK to traders.id.
        symbol: Ticker symbol, matched case-insensitively.
        option_type: 'call'/'put' to match exactly (via IS), or None for
            an equity key.
        strike: The Decimal strike to match, or None for an equity key.
        expiration: Resolved ISO8601 date to match exactly (via IS), or
            None for an equity key.

    Returns:
        SignalSnapshots in chronological order (see _order_signal_rows()),
        using received_at ordering only when every matching signal has a
        resolved received_at, otherwise falling back consistently to
        (raw_message_id, trade_signal_id) ordering for all of them. Empty
        list if nothing matches.
    """
    rows = conn.execute(
        f"SELECT {_LIFECYCLE_SIGNAL_SELECT} "
        f"{_LIFECYCLE_SIGNAL_CURRENT_JOIN} "
        "WHERE ts.trader_id = ? "
        "AND UPPER(ts.symbol) = UPPER(?) "
        "AND ts.option_type IS ? "
        "AND ts.expiration IS ? "
        f"AND {_LIFECYCLE_SIGNAL_CURRENT_AND_ELIGIBLE}",
        (trader_id, symbol, option_type, expiration),
    ).fetchall()

    matching = [row for row in rows if _decimal_equal(row["strike"], strike)]
    return _order_signal_rows(matching)


def get_distinct_lifecycle_keys_for_signal_ids(
    conn: sqlite3.Connection,
    trade_signal_ids: list[int],
) -> list[tuple]:
    """List the distinct, complete lifecycle keys among a set of signals.

    A signal whose own (option_type, strike, expiration) shape is
    incomplete (some but not all of the three populated) never
    contributes a key here - it is silently excluded, never guessed into
    a normal key. Callers are responsible for routing such a signal to
    its own unresolved singleton by other means (see
    create_lifecycle_unresolved_singleton()); this function's contract is
    "distinct valid keys only."

    Args:
        conn: An open sqlite3.Connection.
        trade_signal_ids: The trade_signals.id values to inspect.

    Returns:
        Distinct (trader_id, symbol_upper, option_type, strike, expiration)
        tuples, one per complete key shape found among trade_signal_ids -
        symbol is normalized to uppercase and strike is a Decimal (or
        None), so each tuple is directly usable as
        get_current_trade_signals_for_key()'s own arguments. Returned in
        a deterministic, normalized order (see _lifecycle_key_sort_key())
        - never raw set/dict iteration order, so calling this twice with
        trade_signal_ids in a different order, or against data inserted
        in a different order, always yields the identical list. Empty
        list if trade_signal_ids is empty or no signal has a complete key
        shape.
    """
    if not trade_signal_ids:
        return []

    placeholders = ",".join("?" * len(trade_signal_ids))
    rows = conn.execute(
        "SELECT trader_id, symbol, option_type, strike, expiration "
        f"FROM trade_signals WHERE id IN ({placeholders})",
        list(trade_signal_ids),
    ).fetchall()

    keys: set = set()
    for row in rows:
        option_type, strike_text, expiration = (
            row["option_type"], row["strike"], row["expiration"]
        )
        if not _is_complete_lifecycle_key_shape(option_type, strike_text, expiration):
            continue
        strike = Decimal(strike_text) if strike_text is not None else None
        keys.add((row["trader_id"], row["symbol"].upper(), option_type, strike, expiration))

    return sorted(keys, key=_lifecycle_key_sort_key)


def get_all_current_lifecycle_eligible_signal_ids(conn: sqlite3.Connection) -> list[int]:
    """List every current, lifecycle-eligible signal id in the database.

    The unfiltered, whole-database sibling of get_current_trade_signals_for_key():
    where that function scopes to one exact key, this scans every current
    ("current" = extraction_id IS NULL OR message_extractions.is_current = 1)
    and eligible (event_type IS NOT NULL) trade_signals row, with no key
    filter at all. Feeds get_distinct_lifecycle_keys_for_signal_ids() so a
    full-database rebuild can discover every complete lifecycle key
    represented by current signals, without any caller needing to already
    know which signal ids or keys exist.

    Args:
        conn: An open sqlite3.Connection.

    Returns:
        Every matching trade_signals.id, ordered ascending. Empty list if
        none match.
    """
    rows = conn.execute(
        "SELECT ts.id AS id "
        f"{_LIFECYCLE_SIGNAL_CURRENT_JOIN} "
        f"WHERE {_LIFECYCLE_SIGNAL_CURRENT_AND_ELIGIBLE} "
        "ORDER BY ts.id"
    ).fetchall()
    return [row["id"] for row in rows]


def get_current_incomplete_lifecycle_signal_snapshots(
    conn: sqlite3.Connection,
) -> list[SignalSnapshot]:
    """List every current, lifecycle-eligible signal whose own option
    identity is incomplete (some but not all of option_type/strike/
    expiration populated).

    Companion to get_distinct_lifecycle_keys_for_signal_ids(), which
    silently excludes exactly these same signals from its own result -
    this function is the other half: it surfaces them directly so a
    caller (R6.4) can route each one to its own unresolved singleton
    (never grouped into a normal key, never guessed).

    Args:
        conn: An open sqlite3.Connection.

    Returns:
        SignalSnapshots for every current, eligible, incomplete-key
        signal, in deterministic chronological order (see
        _order_signal_rows()) - using received_at ordering only when
        every one of them has a resolved received_at, otherwise falling
        back consistently to (raw_message_id, trade_signal_id) ordering
        for all of them. Empty list if none exist.
    """
    rows = conn.execute(
        f"SELECT {_LIFECYCLE_SIGNAL_SELECT} "
        f"{_LIFECYCLE_SIGNAL_CURRENT_JOIN} "
        f"WHERE {_LIFECYCLE_SIGNAL_CURRENT_AND_ELIGIBLE}"
    ).fetchall()
    incomplete_rows = [
        row
        for row in rows
        if not _is_complete_lifecycle_key_shape(
            row["option_type"], row["strike"], row["expiration"]
        )
    ]
    return _order_signal_rows(incomplete_rows)


def get_current_incomplete_lifecycles(conn: sqlite3.Connection) -> list[TradeLifecycle]:
    """List every current (is_current=1) lifecycle generation whose own
    (option_type, strike, expiration) shape is incomplete.

    These are always standalone incomplete-contract-identity singletons
    (see create_lifecycle_unresolved_singleton()) - never a normal,
    signal-grouped generation. Unlike get_all_current_lifecycle_keys(),
    which deduplicates by normalized key and would silently collapse two
    distinct singleton rows that happen to share an identical incomplete
    shape into one key, this returns every matching row individually, so
    a caller (R6.4) can check and, if needed, supersede each singleton on
    its own - including one whose member signal has since become
    complete-shaped and must not be left stale alongside a new normal-key
    generation for that same signal (which would otherwise violate
    Invariant A: a signal belonging to two current lifecycles at once).

    Args:
        conn: An open sqlite3.Connection.

    Returns:
        Every matching TradeLifecycle, ordered by id ascending. Empty
        list if none exist.
    """
    rows = conn.execute(
        f"SELECT {_TRADE_LIFECYCLE_COLUMNS} FROM trade_lifecycles "
        "WHERE is_current = 1 ORDER BY id"
    ).fetchall()
    lifecycles = [_row_to_trade_lifecycle(row) for row in rows]
    return [
        lifecycle
        for lifecycle in lifecycles
        if not _is_complete_lifecycle_key_shape(
            lifecycle.option_type, lifecycle.strike, lifecycle.expiration
        )
    ]


def get_current_signal_snapshot_for_raw_message(
    conn: sqlite3.Connection,
    raw_message_id: int,
) -> SignalSnapshot | None:
    """Look up the current, lifecycle-eligible signal for one raw message.

    Fails closed rather than silently picking one: queries every current,
    lifecycle-eligible signal for raw_message_id in deterministic
    trade_signal_id order and raises if more than one is found, instead
    of using fetchone() to select an arbitrary row. At most one such
    signal should ever legitimately exist (the extractor produces at most
    one candidate trade event per message, and message_extractions
    enforces at most one current extraction per raw message) - if that
    assumption is ever violated, silently returning one of them would let
    a real data-integrity problem masquerade as normal operation.

    Args:
        conn: An open sqlite3.Connection.
        raw_message_id: FK to raw_messages.id.

    Returns:
        The matching SignalSnapshot, or None if this raw message has no
        current, lifecycle-eligible signal (including if the raw message
        does not exist, has no signal at all, has only a superseded
        signal, or its signal's event_type is NULL).

    Raises:
        ValueError: If more than one current, lifecycle-eligible signal
            exists for raw_message_id - names the raw_message_id and every
            matching trade_signal_id.
    """
    rows = conn.execute(
        f"SELECT {_LIFECYCLE_SIGNAL_SELECT} "
        f"{_LIFECYCLE_SIGNAL_CURRENT_JOIN} "
        "WHERE ts.raw_message_id = ? "
        f"AND {_LIFECYCLE_SIGNAL_CURRENT_AND_ELIGIBLE} "
        "ORDER BY ts.id",
        (raw_message_id,),
    ).fetchall()

    if not rows:
        return None
    if len(rows) > 1:
        matching_trade_signal_ids = [row["trade_signal_id"] for row in rows]
        raise ValueError(
            f"raw_message_id {raw_message_id} has {len(rows)} current, "
            f"lifecycle-eligible signals {matching_trade_signal_ids} - expected at most one."
        )

    row = rows[0]
    if row["received_at"] is not None:
        ordering_key = (row["received_at"], row["raw_message_id"], row["trade_signal_id"])
    else:
        ordering_key = (row["raw_message_id"], row["trade_signal_id"])
    return _row_to_signal_snapshot(row, ordering_key)


def get_chronological_positions_for_raw_messages(
    conn: sqlite3.Connection,
    raw_message_ids: list[int],
) -> dict[int, tuple]:
    """Compute whole-set-consistent chronological positions for a batch of
    raw messages.

    Replaces any notion of an independently-decided per-row position:
    deciding one row's position in isolation from its siblings is exactly
    the mixed-mode bug this function exists to prevent. If every row in
    the requested set has a resolved received_at, every returned position
    is timestamp-based. If any row in the set lacks one, EVERY returned
    position - including for rows that do have a resolved received_at -
    falls back to (raw_message_id,) only, so a later-inserted,
    timestamp-less message can never be misread as chronologically
    earlier than an earlier, timestamped one just because "no timestamp"
    was treated as "sorts first": once any sibling lacks a timestamp,
    received_at is not consulted for anyone in the set, and insertion
    order (raw_message_id) alone governs every comparison.

    Args:
        conn: An open sqlite3.Connection.
        raw_message_ids: The raw_messages.id values to compute positions
            for. Duplicates are harmless (de-duplicated internally). Order
            does not affect the result: the returned mapping's values
            depend only on the requested set's own data, never on the
            order raw_message_ids was given in.

    Returns:
        A dict mapping each requested raw_message_id to its position
        tuple - either (received_at, raw_message_id) for every entry, or
        (raw_message_id,) for every entry - the two shapes are never
        mixed within one returned dict. Key iteration order is always
        ascending raw_message_id, regardless of the order raw_message_ids
        was given in and regardless of any duplicates in it (a duplicated
        id still produces exactly one entry). Empty dict for empty input.

    Raises:
        ValueError: If any requested raw_message_id does not exist,
            naming every missing id (sorted, for a deterministic message).
    """
    if not raw_message_ids:
        return {}

    unique_ids = list(dict.fromkeys(raw_message_ids))
    placeholders = ",".join("?" * len(unique_ids))
    # ORDER BY id makes the row-return order - and therefore this
    # function's returned dict's insertion/iteration order - always
    # ascending raw_message_id, independent of both the order
    # raw_message_ids was given in and SQLite's own unordered-by-default
    # row-return order for an IN (...) scan.
    rows = conn.execute(
        f"SELECT id, received_at FROM raw_messages WHERE id IN ({placeholders}) ORDER BY id",
        unique_ids,
    ).fetchall()

    found_ids = {row["id"] for row in rows}
    missing_ids = sorted(set(unique_ids) - found_ids)
    if missing_ids:
        raise ValueError(f"raw_message_id(s) do not exist: {missing_ids}")

    all_have_received_at = all(row["received_at"] is not None for row in rows)
    if all_have_received_at:
        return {row["id"]: (row["received_at"], row["id"]) for row in rows}
    return {row["id"]: (row["id"],) for row in rows}


_TRADE_LIFECYCLE_STATUSES = frozenset(
    {"open", "partially_closed", "closed", "orphan", "unresolved", "invalid"}
)

_TRADE_LIFECYCLE_COLUMNS = (
    "id, trader_id, symbol, option_type, strike, expiration, status, "
    "remaining_fraction, opened_by_signal_id, closed_by_signal_id, "
    "is_current, superseded_at, ambiguity_flags, created_at, updated_at"
)


def _validate_trade_lifecycle_required_fields(
    trader_id: int | None,
    symbol: str | None,
    status: str | None,
    remaining_fraction: str | None,
) -> None:
    """Validate the trade_lifecycles fields required by the schema.

    Raises:
        ValueError: If trader_id is None, symbol is None/blank, status is
            not one of the six approved values, or remaining_fraction is
            None/blank.
    """
    if trader_id is None:
        raise ValueError("trader_id is required.")
    if symbol is None or not symbol.strip():
        raise ValueError("symbol must not be empty or whitespace-only.")
    if status not in _TRADE_LIFECYCLE_STATUSES:
        raise ValueError(f"status must be one of {sorted(_TRADE_LIFECYCLE_STATUSES)}.")
    if remaining_fraction is None or not remaining_fraction.strip():
        raise ValueError("remaining_fraction must not be empty or whitespace-only.")


def _row_to_trade_lifecycle(row: sqlite3.Row) -> TradeLifecycle:
    """Map a ``trade_lifecycles`` table row to a TradeLifecycle model.

    Args:
        row: A row from the trade_lifecycles table, with all columns
            selected.

    Returns:
        The corresponding TradeLifecycle, with ambiguity_flags
        deserialized from its stored JSON representation (or None if the
        column is NULL) - matching MessageExtraction's own convention.
    """
    ambiguity_flags = (
        json.loads(row["ambiguity_flags"]) if row["ambiguity_flags"] is not None else None
    )
    return TradeLifecycle(
        id=row["id"],
        trader_id=row["trader_id"],
        symbol=row["symbol"],
        option_type=row["option_type"],
        strike=row["strike"],
        expiration=row["expiration"],
        status=row["status"],
        remaining_fraction=row["remaining_fraction"],
        opened_by_signal_id=row["opened_by_signal_id"],
        closed_by_signal_id=row["closed_by_signal_id"],
        is_current=bool(row["is_current"]),
        superseded_at=row["superseded_at"],
        ambiguity_flags=ambiguity_flags,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def create_trade_lifecycle(
    conn: sqlite3.Connection,
    trader_id: int,
    symbol: str,
    status: str,
    remaining_fraction: str,
    option_type: str | None = None,
    strike: Decimal | None = None,
    expiration: str | None = None,
    opened_by_signal_id: int | None = None,
    closed_by_signal_id: int | None = None,
    ambiguity_flags: list | None = None,
) -> TradeLifecycle:
    """Insert a new lifecycle generation row, marked as the current one.

    A generation is never edited in place once created by any repository
    function, aside from supersede_trade_lifecycle() - this mirrors
    create_message_extraction()/supersede_extraction()'s established
    contract one level up.

    Args:
        conn: An open sqlite3.Connection.
        trader_id: FK to traders.id.
        symbol: Ticker symbol, stored exactly as given (not forced
            uppercase - callers already normalize via
            get_distinct_lifecycle_keys_for_signal_ids() when relevant).
        status: One of 'open', 'partially_closed', 'closed', 'orphan',
            'unresolved', 'invalid'.
        remaining_fraction: The exact string form of a fractions.Fraction
            (e.g. "1", "1/2", "0"), never a Decimal string.
        option_type: 'call'/'put', or None for an equity key.
        strike: A Decimal strike, or None. Never a float or string.
        expiration: Resolved ISO8601 date, or None.
        opened_by_signal_id: FK to trade_signals.id, or None.
        closed_by_signal_id: FK to trade_signals.id, or None.
        ambiguity_flags: List of JSON-serializable flag strings, or None.

    Returns:
        The newly created TradeLifecycle, including its generated id,
        created_at, and updated_at.

    Raises:
        ValueError: If trader_id is None, symbol is empty/whitespace-only,
            status is not one of the six approved values, or
            remaining_fraction is empty/whitespace-only.
        TypeError: If strike is supplied and is not a Decimal.
        sqlite3.IntegrityError: If trader_id, opened_by_signal_id, or
            closed_by_signal_id does not reference an existing row.
    """
    _validate_trade_lifecycle_required_fields(trader_id, symbol, status, remaining_fraction)
    strike_text = _serialize_price(strike)
    serialized_flags = (
        json.dumps(ambiguity_flags, sort_keys=True) if ambiguity_flags is not None else None
    )

    cursor = conn.execute(
        "INSERT INTO trade_lifecycles "
        "(trader_id, symbol, option_type, strike, expiration, status, "
        "remaining_fraction, opened_by_signal_id, closed_by_signal_id, "
        "is_current, ambiguity_flags) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
        (
            trader_id,
            symbol,
            option_type,
            strike_text,
            expiration,
            status,
            remaining_fraction,
            opened_by_signal_id,
            closed_by_signal_id,
            serialized_flags,
        ),
    )
    row = conn.execute(
        f"SELECT {_TRADE_LIFECYCLE_COLUMNS} FROM trade_lifecycles WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return _row_to_trade_lifecycle(row)


def get_trade_lifecycle_by_id(
    conn: sqlite3.Connection,
    trade_lifecycle_id: int,
) -> TradeLifecycle | None:
    """Look up a lifecycle generation by primary key.

    The standard per-id getter every other entity in this module already
    has (get_trade_signal_by_id(), get_raw_message_by_id(),
    get_import_batch_by_id(), get_trader_by_id()) - trade_lifecycles was
    missing its own until R6.4 needed to resolve a bare id (e.g. from
    get_current_lifecycle_ids_for_raw_message_ids()) back into a full
    TradeLifecycle.

    Args:
        conn: An open sqlite3.Connection.
        trade_lifecycle_id: Primary key to look up.

    Returns:
        The matching TradeLifecycle (current or superseded), or None if
        no row exists with this id.
    """
    row = conn.execute(
        f"SELECT {_TRADE_LIFECYCLE_COLUMNS} FROM trade_lifecycles WHERE id = ?",
        (trade_lifecycle_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_trade_lifecycle(row)


def supersede_trade_lifecycle(
    conn: sqlite3.Connection,
    trade_lifecycle_id: int,
) -> TradeLifecycle | None:
    """Mark a lifecycle generation row as no longer current.

    Updates only is_current, superseded_at, and updated_at - status,
    remaining_fraction, membership, and every other field are left
    exactly as they were. Never deletes the row or its
    trade_lifecycle_events membership rows, which remain permanently
    queryable for audit via get_trade_lifecycle_history_rows()/
    get_trade_lifecycle_events(). Does not insert a replacement
    generation - that is TradeService's job (R6.4).

    Args:
        conn: An open sqlite3.Connection.
        trade_lifecycle_id: Primary key of the generation to supersede.

    Returns:
        The updated TradeLifecycle, or None if no row exists with this id.
    """
    conn.execute(
        "UPDATE trade_lifecycles "
        "SET is_current = 0, superseded_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
        "WHERE id = ?",
        (trade_lifecycle_id,),
    )
    row = conn.execute(
        f"SELECT {_TRADE_LIFECYCLE_COLUMNS} FROM trade_lifecycles WHERE id = ?",
        (trade_lifecycle_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_trade_lifecycle(row)


def get_current_lifecycles_for_key(
    conn: sqlite3.Connection,
    trader_id: int,
    symbol: str,
    option_type: str | None,
    strike: Decimal | None,
    expiration: str | None,
) -> list[TradeLifecycle]:
    """List every currently current (is_current=1) generation for one
    exact lifecycle key.

    May return zero, one, or several rows: at most one non-terminal
    ('open'/'partially_closed') row can validly exist per key (see
    validate_lifecycle_membership_integrity()'s invariant H), but any
    number of terminal ('closed'/'orphan'/'unresolved'/'invalid')
    generations - each a distinct past re-entry - remain simultaneously
    current until their own lineage changes.

    Key matching uses the same rules as get_current_trade_signals_for_key():
    symbol case-insensitive, option_type/expiration NULL-safe via IS,
    strike compared exactly via Decimal in Python.

    Args:
        conn: An open sqlite3.Connection.
        trader_id: FK to traders.id.
        symbol: Ticker symbol, matched case-insensitively.
        option_type: 'call'/'put' to match exactly, or None for an equity
            key.
        strike: The Decimal strike to match, or None for an equity key.
        expiration: Resolved ISO8601 date to match exactly, or None for
            an equity key.

    Returns:
        Matching TradeLifecycles, ordered by id ascending. Empty list if
        none match.
    """
    rows = conn.execute(
        f"SELECT {_TRADE_LIFECYCLE_COLUMNS} FROM trade_lifecycles "
        "WHERE trader_id = ? AND UPPER(symbol) = UPPER(?) "
        "AND option_type IS ? AND expiration IS ? AND is_current = 1 "
        "ORDER BY id",
        (trader_id, symbol, option_type, expiration),
    ).fetchall()
    matching = [row for row in rows if _decimal_equal(row["strike"], strike)]
    return [_row_to_trade_lifecycle(row) for row in matching]


def get_trade_lifecycle_history_rows(
    conn: sqlite3.Connection,
    trader_id: int,
    symbol: str,
    option_type: str | None,
    strike: Decimal | None,
    expiration: str | None,
) -> list[TradeLifecycle]:
    """List every generation - current and superseded - for one exact
    lifecycle key, newest first.

    Unlike get_current_lifecycles_for_key(), this applies no is_current
    filter at all, so a fully superseded generation remains visible here
    forever, exactly matching "auditable lifecycle history."

    Args:
        conn: An open sqlite3.Connection.
        trader_id: FK to traders.id.
        symbol: Ticker symbol, matched case-insensitively.
        option_type: 'call'/'put' to match exactly, or None for an equity
            key.
        strike: The Decimal strike to match, or None for an equity key.
        expiration: Resolved ISO8601 date to match exactly, or None for
            an equity key.

    Returns:
        Matching TradeLifecycles, ordered by id descending (newest
        first). Empty list if none match.
    """
    rows = conn.execute(
        f"SELECT {_TRADE_LIFECYCLE_COLUMNS} FROM trade_lifecycles "
        "WHERE trader_id = ? AND UPPER(symbol) = UPPER(?) "
        "AND option_type IS ? AND expiration IS ? "
        "ORDER BY id DESC",
        (trader_id, symbol, option_type, expiration),
    ).fetchall()
    matching = [row for row in rows if _decimal_equal(row["strike"], strike)]
    return [_row_to_trade_lifecycle(row) for row in matching]


def get_trade_lifecycle_lineage_raw_message_ids(
    conn: sqlite3.Connection,
    trade_lifecycle_id: int,
) -> frozenset[int]:
    """Return the fixed set of raw_message_ids that make up one
    generation's lineage.

    Derived from the generation's member signals' own raw_message_id -
    this is the persisted boundary a later rebuild uses to decide whether
    an incoming signal is a lineage-linked replacement for one of this
    generation's own members (reprocessing, or a key-changing correction)
    versus a genuinely unrelated new signal, per the approved R6 lineage-
    aware finalization design. Distinct terminal generations at the same
    key have disjoint lineage sets by construction.

    Args:
        conn: An open sqlite3.Connection.
        trade_lifecycle_id: FK to trade_lifecycles.id.

    Returns:
        The distinct raw_message_id values among this generation's
        current membership rows. Empty frozenset if the generation has no
        members or does not exist.
    """
    rows = conn.execute(
        "SELECT DISTINCT ts.raw_message_id "
        "FROM trade_lifecycle_events tle "
        "JOIN trade_signals ts ON ts.id = tle.trade_signal_id "
        "WHERE tle.trade_lifecycle_id = ?",
        (trade_lifecycle_id,),
    ).fetchall()
    return frozenset(row["raw_message_id"] for row in rows)


def get_current_lifecycle_ids_for_raw_message_ids(
    conn: sqlite3.Connection,
    raw_message_ids: list[int],
) -> list[int]:
    """List every current lifecycle generation whose immutable lineage
    includes any of the given raw_message_ids.

    The reverse direction of get_trade_lifecycle_lineage_raw_message_ids()
    (which goes one generation -> its raw_message_ids); this goes a set of
    raw_message_ids -> every current generation touched by any of them.
    This is what lets a targeted rebuild (R6.4) find the "old side" of a
    key-changing correction or reprocessing event even when the affected
    signal's current key/event_type/current-ness has already moved on -
    the lineage recorded in trade_lifecycle_events never changes once
    written, unlike the live trade_signals row it points at.

    Args:
        conn: An open sqlite3.Connection.
        raw_message_ids: The raw_messages.id values to check. Does not
            validate that these ids exist - a nonexistent or currently-
            unlinked raw_message_id simply contributes no results.

    Returns:
        Distinct current (is_current=1) trade_lifecycles.id values whose
        lineage includes at least one of raw_message_ids, ordered
        ascending. Empty list if raw_message_ids is empty or none match.
    """
    if not raw_message_ids:
        return []

    placeholders = ",".join("?" * len(raw_message_ids))
    rows = conn.execute(
        "SELECT DISTINCT tl.id "
        "FROM trade_lifecycles tl "
        "JOIN trade_lifecycle_events tle ON tle.trade_lifecycle_id = tl.id "
        "JOIN trade_signals ts ON ts.id = tle.trade_signal_id "
        f"WHERE tl.is_current = 1 AND ts.raw_message_id IN ({placeholders}) "
        "ORDER BY tl.id",
        list(raw_message_ids),
    ).fetchall()
    return [row["id"] for row in rows]


def get_recorded_shape_for_generation(
    conn: sqlite3.Connection,
    trade_lifecycle_id: int,
) -> tuple | None:
    """Return one generation's recorded shape, for rebuild idempotency
    comparison.

    The returned shape is a 1-tuple containing a single
    (status, remaining_fraction, member_signal_ids, ambiguity_flags)
    tuple - the same 4-field shape a caller (R6.4) would compute for one
    database.lifecycle.LifecycleBuild - so a caller can directly compare
    "here is what build_lifecycle_sequence() just proposed for this key"
    (a list of such 4-tuples) against "here is what is already recorded"
    to decide whether a rebuild would actually change anything.

    Args:
        conn: An open sqlite3.Connection.
        trade_lifecycle_id: FK to trade_lifecycles.id.

    Returns:
        ((status, remaining_fraction, member_signal_ids, ambiguity_flags),)
        or None if no trade_lifecycles row exists with this id.
    """
    lifecycle_row = conn.execute(
        f"SELECT {_TRADE_LIFECYCLE_COLUMNS} FROM trade_lifecycles WHERE id = ?",
        (trade_lifecycle_id,),
    ).fetchone()
    if lifecycle_row is None:
        return None
    lifecycle = _row_to_trade_lifecycle(lifecycle_row)

    member_rows = conn.execute(
        "SELECT trade_signal_id FROM trade_lifecycle_events "
        "WHERE trade_lifecycle_id = ? ORDER BY sequence_index",
        (trade_lifecycle_id,),
    ).fetchall()
    member_signal_ids = tuple(row["trade_signal_id"] for row in member_rows)
    ambiguity_flags = tuple(lifecycle.ambiguity_flags) if lifecycle.ambiguity_flags else ()

    return (
        (lifecycle.status, lifecycle.remaining_fraction, member_signal_ids, ambiguity_flags),
    )


def _row_to_trade_lifecycle_event(row: sqlite3.Row) -> TradeLifecycleEvent:
    """Map a ``trade_lifecycle_events`` table row to a TradeLifecycleEvent
    model.

    signal_snapshot is returned exactly as stored - raw JSON text, never
    decoded here. Decoding (and raising a service-layer error on
    malformed JSON) is TradeService's responsibility (R6.4), matching how
    trade_signal_edits.previous_values is likewise left encoded at this
    layer and only decoded by TradeService.list_trade_signal_audit_history().

    Args:
        row: A row from the trade_lifecycle_events table, with all
            columns selected.

    Returns:
        The corresponding TradeLifecycleEvent.
    """
    return TradeLifecycleEvent(
        id=row["id"],
        trade_lifecycle_id=row["trade_lifecycle_id"],
        trade_signal_id=row["trade_signal_id"],
        sequence_index=row["sequence_index"],
        signal_snapshot=row["signal_snapshot"],
        created_at=row["created_at"],
    )


def build_signal_snapshot_json(snapshot: SignalSnapshot) -> str:
    """Serialize a SignalSnapshot into canonical, immutable JSON text.

    The single place that ever builds trade_lifecycle_events.signal_snapshot
    text - callers never hand-construct this JSON themselves. Uses
    json.dumps(..., sort_keys=True) for deterministic output: the same
    snapshot always serializes to byte-identical text. ordering_key (a
    tuple) is serialized as a JSON array.

    Args:
        snapshot: The SignalSnapshot to serialize.

    Returns:
        The canonical JSON text, containing at minimum trade_signal_id,
        raw_message_id, trader_id, symbol, option_type, strike,
        expiration, event_type, qualifier, action, price,
        stated_entry_price, stated_return_pct, notes, extraction_id, and
        ordering_key.
    """
    payload = {
        "trade_signal_id": snapshot.trade_signal_id,
        "raw_message_id": snapshot.raw_message_id,
        "trader_id": snapshot.trader_id,
        "symbol": snapshot.symbol,
        "option_type": snapshot.option_type,
        "strike": snapshot.strike,
        "expiration": snapshot.expiration,
        "event_type": snapshot.event_type,
        "qualifier": snapshot.qualifier,
        "action": snapshot.action,
        "price": snapshot.price,
        "stated_entry_price": snapshot.stated_entry_price,
        "stated_return_pct": snapshot.stated_return_pct,
        "notes": snapshot.notes,
        "extraction_id": snapshot.extraction_id,
        "ordering_key": list(snapshot.ordering_key),
    }
    return json.dumps(payload, sort_keys=True)


def create_trade_lifecycle_event(
    conn: sqlite3.Connection,
    trade_lifecycle_id: int,
    trade_signal_id: int,
    sequence_index: int,
    signal_snapshot: str,
) -> TradeLifecycleEvent:
    """Insert one membership/audit row linking a generation to one of its
    member signals.

    signal_snapshot must already be serialized (e.g. via
    build_signal_snapshot_json()) - this function performs no
    serialization and no decoding, and never updates or deletes a row in
    this table once created, matching raw_messages.raw_text's existing
    write-once contract. This is what keeps a superseded generation's
    original membership permanently auditable even after later
    reprocessing or correction changes the live trade_signals row.

    Args:
        conn: An open sqlite3.Connection.
        trade_lifecycle_id: FK to trade_lifecycles.id.
        trade_signal_id: FK to trade_signals.id.
        sequence_index: 1-based order of this signal within its
            generation.
        signal_snapshot: Pre-serialized canonical JSON text.

    Returns:
        The newly created TradeLifecycleEvent, including its generated id
        and created_at.

    Raises:
        ValueError: If signal_snapshot is empty.
        sqlite3.IntegrityError: If trade_lifecycle_id or trade_signal_id
            does not reference an existing row, or if this exact
            (trade_lifecycle_id, trade_signal_id) pair already exists.
    """
    if not signal_snapshot:
        raise ValueError("signal_snapshot must not be empty.")

    cursor = conn.execute(
        "INSERT INTO trade_lifecycle_events "
        "(trade_lifecycle_id, trade_signal_id, sequence_index, signal_snapshot) "
        "VALUES (?, ?, ?, ?)",
        (trade_lifecycle_id, trade_signal_id, sequence_index, signal_snapshot),
    )
    row = conn.execute(
        "SELECT id, trade_lifecycle_id, trade_signal_id, sequence_index, "
        "signal_snapshot, created_at FROM trade_lifecycle_events WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return _row_to_trade_lifecycle_event(row)


def get_trade_lifecycle_events(
    conn: sqlite3.Connection,
    trade_lifecycle_id: int,
) -> list[TradeLifecycleEvent]:
    """List one generation's membership, in chronological order.

    Args:
        conn: An open sqlite3.Connection.
        trade_lifecycle_id: FK to trade_lifecycles.id.

    Returns:
        TradeLifecycleEvents ordered by sequence_index ascending. Empty
        list if the generation has no members or does not exist.
    """
    rows = conn.execute(
        "SELECT id, trade_lifecycle_id, trade_signal_id, sequence_index, "
        "signal_snapshot, created_at "
        "FROM trade_lifecycle_events WHERE trade_lifecycle_id = ? "
        "ORDER BY sequence_index",
        (trade_lifecycle_id,),
    ).fetchall()
    return [_row_to_trade_lifecycle_event(row) for row in rows]


def update_trade_signal_lifecycle_pointer(
    conn: sqlite3.Connection,
    trade_signal_id: int,
    trade_lifecycle_id: int | None,
) -> None:
    """Set (or clear, if trade_lifecycle_id is None) one signal's current
    lifecycle pointer.

    The one narrow, maintained exception to trade_signals' otherwise
    strict immutability - written only by the lifecycle engine's
    orchestration (R6.4), never by ingestion, reprocessing-of-extraction,
    or the correction workflow directly. Does not touch updated_at:
    lifecycle_id is a derived pointer maintained outside the audited
    six-field correction surface, not a correction.

    Args:
        conn: An open sqlite3.Connection.
        trade_signal_id: FK to trade_signals.id.
        trade_lifecycle_id: FK to trade_lifecycles.id, or None to clear.

    Raises:
        sqlite3.IntegrityError: If trade_lifecycle_id is not None and does
            not reference an existing row.
    """
    conn.execute(
        "UPDATE trade_signals SET lifecycle_id = ? WHERE id = ?",
        (trade_lifecycle_id, trade_signal_id),
    )


def clear_lifecycle_pointers_for_generation(
    conn: sqlite3.Connection,
    trade_lifecycle_id: int,
) -> int:
    """Clear lifecycle_id to NULL for every signal still pointing at one
    generation.

    Only clears signals whose lifecycle_id currently equals
    trade_lifecycle_id - order-independent by construction: if a
    different key's rebuild has already reassigned a formerly-departed
    member elsewhere, its lifecycle_id no longer equals
    trade_lifecycle_id, so this UPDATE's WHERE clause simply does not
    touch it, regardless of which rebuild runs first.

    Args:
        conn: An open sqlite3.Connection.
        trade_lifecycle_id: The generation whose pointers should be
            cleared.

    Returns:
        The number of trade_signals rows actually cleared.
    """
    cursor = conn.execute(
        "UPDATE trade_signals SET lifecycle_id = NULL WHERE lifecycle_id = ?",
        (trade_lifecycle_id,),
    )
    return cursor.rowcount


def _validate_lifecycle_builds_before_persisting(
    builds: list,
    snapshots_by_signal_id: dict,
) -> None:
    """Validate an entire builds list before persist_lifecycle_builds()
    performs its first database write.

    Every check below runs to completion (nothing here writes to the
    database), so a rejected call leaves zero lifecycle rows, zero
    membership rows, and no lifecycle pointer changes - even before an
    explicit rollback, since nothing was ever written in the first place.

    Args:
        builds: A list of database.lifecycle.LifecycleBuild.
        snapshots_by_signal_id: Maps every trade_signal_id appearing in
            any build's member_signal_ids to the SignalSnapshot it was
            read from.

    Raises:
        ValueError: If any build has no member signals; if any
            trade_signal_id occurs more than once across the complete
            builds list (whether duplicated within one build or split
            across two different builds); if any member trade_signal_id
            has no entry in snapshots_by_signal_id; or if any entry in
            snapshots_by_signal_id - referenced by a build's members or
            not - has a snapshot whose own trade_signal_id does not match
            the dict key it is stored under. Every message names the
            exact offending id(s).
    """
    empty_build_indexes = [i for i, build in enumerate(builds) if not build.member_signal_ids]
    if empty_build_indexes:
        raise ValueError(
            f"builds at index(es) {empty_build_indexes} contain no member signals - "
            "every LifecycleBuild must have at least one member."
        )

    seen_signal_ids: set = set()
    duplicate_signal_ids: set = set()
    for build in builds:
        for signal_id in build.member_signal_ids:
            if signal_id in seen_signal_ids:
                duplicate_signal_ids.add(signal_id)
            else:
                seen_signal_ids.add(signal_id)
    if duplicate_signal_ids:
        raise ValueError(
            f"trade_signal_id(s) {sorted(duplicate_signal_ids)} appear more than once "
            "across the builds list - a signal may belong to at most one build."
        )

    all_member_signal_ids = {
        signal_id for build in builds for signal_id in build.member_signal_ids
    }
    missing_snapshot_ids = sorted(all_member_signal_ids - set(snapshots_by_signal_id.keys()))
    if missing_snapshot_ids:
        raise ValueError(
            f"trade_signal_id(s) {missing_snapshot_ids} have no entry in "
            "snapshots_by_signal_id."
        )

    # Every entry in the mapping is validated - not only entries a
    # build's member_signal_ids happens to reference - so a stray,
    # unreferenced, mismatched entry can never slip through unnoticed.
    mismatched_mapping_keys = sorted(
        mapping_key
        for mapping_key, snapshot in snapshots_by_signal_id.items()
        if snapshot.trade_signal_id != mapping_key
    )
    if mismatched_mapping_keys:
        raise ValueError(
            f"snapshots_by_signal_id key(s) {mismatched_mapping_keys} do not match "
            "their own snapshot's trade_signal_id."
        )


def persist_lifecycle_builds(
    conn: sqlite3.Connection,
    trader_id: int,
    symbol: str,
    option_type: str | None,
    strike: Decimal | None,
    expiration: str | None,
    builds: list,
    snapshots_by_signal_id: dict,
) -> list[int]:
    """Persist a pure-engine build sequence for one lifecycle key.

    Validates the entire builds/snapshots_by_signal_id shape (see
    _validate_lifecycle_builds_before_persisting()) before performing any
    database write. Only once that validation passes: for each
    database.lifecycle.LifecycleBuild in builds (typically
    build_lifecycle_sequence()'s own return value, in order), inserts one
    fresh trade_lifecycles row, inserts its ordered trade_lifecycle_events
    membership (each carrying an immutable canonical signal_snapshot via
    build_signal_snapshot_json()), and updates each member signal's
    lifecycle_id to point at the new generation. Creates no duplicate
    membership: each build's own member_signal_ids has no repeats, and
    each freshly created trade_lifecycle_id is unique, so every
    (trade_lifecycle_id, trade_signal_id) pair inserted is unique by
    construction.

    Does not supersede any existing generation and does not commit or
    roll back - the caller (R6.4) is responsible for superseding whatever
    generation(s) this replaces, in the same transaction.

    Args:
        conn: An open sqlite3.Connection.
        trader_id, symbol, option_type, strike, expiration: The lifecycle
            key every build in `builds` belongs to.
        builds: A list of database.lifecycle.LifecycleBuild.
        snapshots_by_signal_id: Maps every trade_signal_id appearing in
            any build's member_signal_ids to the SignalSnapshot it was
            read from.

    Returns:
        The newly created trade_lifecycles.id values, one per build, in
        the same order as `builds`. Empty list if builds is empty.

    Raises:
        ValueError: See _validate_lifecycle_builds_before_persisting() -
            raised before any write, so nothing is ever partially
            persisted.
    """
    _validate_lifecycle_builds_before_persisting(builds, snapshots_by_signal_id)

    new_lifecycle_ids: list[int] = []
    for build in builds:
        lifecycle = create_trade_lifecycle(
            conn,
            trader_id=trader_id,
            symbol=symbol,
            status=build.status,
            remaining_fraction=build.remaining_fraction,
            option_type=option_type,
            strike=strike,
            expiration=expiration,
            opened_by_signal_id=build.opened_by_signal_id,
            closed_by_signal_id=build.closed_by_signal_id,
            ambiguity_flags=list(build.ambiguity_flags) or None,
        )
        for sequence_index, member_signal_id in enumerate(build.member_signal_ids, start=1):
            snapshot = snapshots_by_signal_id[member_signal_id]
            create_trade_lifecycle_event(
                conn,
                lifecycle.id,
                member_signal_id,
                sequence_index,
                build_signal_snapshot_json(snapshot),
            )
            update_trade_signal_lifecycle_pointer(conn, member_signal_id, lifecycle.id)
        new_lifecycle_ids.append(lifecycle.id)
    return new_lifecycle_ids


def create_lifecycle_unresolved_singleton(
    conn: sqlite3.Connection,
    trader_id: int,
    symbol: str,
    option_type: str | None,
    strike: Decimal | None,
    expiration: str | None,
    snapshot: SignalSnapshot,
    flag: str,
) -> int:
    """Create a standalone 'unresolved' generation for exactly one signal.

    For a signal classified unresolved outside of a normal
    build_lifecycle_sequence() replay - e.g. a genuinely new signal a
    later rebuild (R6.4) classifies out_of_order_after_terminal_lifecycle
    before it is ever added to any replay window, or a signal whose own
    key/event_type made it ineligible for a normal window in the first
    place.

    Args:
        conn: An open sqlite3.Connection.
        trader_id, symbol, option_type, strike, expiration: The lifecycle
            key this singleton is recorded under.
        snapshot: The SignalSnapshot for the one signal this singleton
            represents.
        flag: The single ambiguity flag naming why this signal is
            unresolved.

    Returns:
        The newly created trade_lifecycles.id.
    """
    lifecycle = create_trade_lifecycle(
        conn,
        trader_id=trader_id,
        symbol=symbol,
        status="unresolved",
        remaining_fraction="0",
        option_type=option_type,
        strike=strike,
        expiration=expiration,
        ambiguity_flags=[flag],
    )
    create_trade_lifecycle_event(
        conn,
        lifecycle.id,
        snapshot.trade_signal_id,
        1,
        build_signal_snapshot_json(snapshot),
    )
    update_trade_signal_lifecycle_pointer(conn, snapshot.trade_signal_id, lifecycle.id)
    return lifecycle.id


def list_current_trade_lifecycles(
    conn: sqlite3.Connection,
    *,
    trader_name: str | None = None,
    symbol: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """List current (is_current=1) lifecycle generations for read-only
    display, newest first.

    Mirrors get_trade_signals_for_review()'s filter/display pattern: a
    blank or None filter omits its WHERE fragment entirely rather than
    matching everything via a wildcard. This function performs no writes.

    Args:
        conn: An open sqlite3.Connection.
        trader_name: Exact traders.name to filter by, or None/blank to
            omit.
        symbol: Ticker symbol to filter by, matched case-insensitively,
            or None/blank to omit.
        status: Exact trade_lifecycles.status to filter by, or None/blank
            to omit.
        limit: Maximum number of rows to return, applied in SQL via
            LIMIT. Defaults to 100.

    Returns:
        A list of dicts, newest first (trade_lifecycles.id descending),
        each with keys: id, trader_id, trader_name, symbol, option_type,
        strike, expiration, status, remaining_fraction,
        opened_by_signal_id, closed_by_signal_id, ambiguity_flags (a
        decoded list or None), created_at, updated_at. Empty list if
        nothing matches.
    """
    where_clauses = ["trade_lifecycles.is_current = 1"]
    params: list = []

    if trader_name and trader_name.strip():
        where_clauses.append("traders.name = ?")
        params.append(trader_name)
    if symbol and symbol.strip():
        where_clauses.append("UPPER(trade_lifecycles.symbol) = UPPER(?)")
        params.append(symbol)
    if status and status.strip():
        where_clauses.append("trade_lifecycles.status = ?")
        params.append(status)

    where_sql = f"WHERE {' AND '.join(where_clauses)}"

    rows = conn.execute(
        "SELECT "
        "trade_lifecycles.id AS id, "
        "trade_lifecycles.trader_id AS trader_id, "
        "traders.name AS trader_name, "
        "trade_lifecycles.symbol AS symbol, "
        "trade_lifecycles.option_type AS option_type, "
        "trade_lifecycles.strike AS strike, "
        "trade_lifecycles.expiration AS expiration, "
        "trade_lifecycles.status AS status, "
        "trade_lifecycles.remaining_fraction AS remaining_fraction, "
        "trade_lifecycles.opened_by_signal_id AS opened_by_signal_id, "
        "trade_lifecycles.closed_by_signal_id AS closed_by_signal_id, "
        "trade_lifecycles.ambiguity_flags AS ambiguity_flags, "
        "trade_lifecycles.created_at AS created_at, "
        "trade_lifecycles.updated_at AS updated_at "
        "FROM trade_lifecycles "
        "JOIN traders ON trade_lifecycles.trader_id = traders.id "
        f"{where_sql} "
        "ORDER BY trade_lifecycles.id DESC "
        "LIMIT ?",
        (*params, limit),
    ).fetchall()

    results = []
    for row in rows:
        result = dict(row)
        result["ambiguity_flags"] = (
            json.loads(result["ambiguity_flags"])
            if result["ambiguity_flags"] is not None
            else None
        )
        results.append(result)
    return results


def get_all_current_trade_lifecycles(
    conn: sqlite3.Connection,
    *,
    trader_id: int | None = None,
) -> list[TradeLifecycle]:
    """List every current (is_current=1) lifecycle generation, in full,
    with no LIMIT of any kind.

    The analytics-completeness counterpart to list_current_trade_lifecycles():
    that function is a bounded, newest-first *display* reader for the
    Review UI (default LIMIT 100) and must never be repurposed to back a
    "did we see every current lifecycle" guarantee - it offers no way for
    a caller to detect truncation. This function instead follows the same
    unbounded-scan precedent already established by
    get_all_current_lifecycle_keys() (used by rebuild_all_lifecycles() for
    whole-database correctness), but returns full TradeLifecycle rows
    rather than deduplicated key tuples, since analytics needs each
    generation's own id/status/opened-closed pointers, not just its key.

    Args:
        conn: An open sqlite3.Connection.
        trader_id: FK to traders.id to filter by, or None for every
            trader.

    Returns:
        Every matching TradeLifecycle, ordered by id ascending
        (deterministic; not a display "newest first" convention). Empty
        list if none match.
    """
    if trader_id is None:
        rows = conn.execute(
            f"SELECT {_TRADE_LIFECYCLE_COLUMNS} FROM trade_lifecycles "
            "WHERE is_current = 1 ORDER BY id"
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_TRADE_LIFECYCLE_COLUMNS} FROM trade_lifecycles "
            "WHERE is_current = 1 AND trader_id = ? ORDER BY id",
            (trader_id,),
        ).fetchall()
    return [_row_to_trade_lifecycle(row) for row in rows]


def get_all_current_lifecycle_keys(conn: sqlite3.Connection) -> list[tuple]:
    """List every distinct normalized key among all current (is_current=1)
    lifecycle generations - including a key whose generation(s) no longer
    have any current eligible signal at all (e.g. every member was
    reprocessed away, corrected to a different key, or became event_type
    NULL).

    Grouped in Python, exactly like _check_invariant_h_multiple_active_per_key():
    strike must be compared via Decimal and symbol case-insensitively,
    neither of which a raw SQL DISTINCT on the stored text/columns would
    get right (two current generations logically at the same key can have
    different stored strike text, e.g. "207.50" vs "207.5", if they were
    created from signals whose own Decimal happened to print differently).

    This is what lets a full rebuild (R6.4) discover a stale persisted key
    that has zero current signals left at all - such a key would never
    appear in get_distinct_lifecycle_keys_for_signal_ids()'s signal-driven
    result, since that function only ever sees keys current signals still
    claim.

    Args:
        conn: An open sqlite3.Connection.

    Returns:
        Distinct (trader_id, symbol_upper, option_type, strike,
        expiration) tuples, one per normalized key with at least one
        current lifecycle generation, in the same deterministic,
        normalized order as get_distinct_lifecycle_keys_for_signal_ids()
        (see _lifecycle_key_sort_key()). Empty list if no current
        lifecycle generation exists.
    """
    rows = conn.execute(
        "SELECT trader_id, symbol, option_type, strike, expiration "
        "FROM trade_lifecycles WHERE is_current = 1"
    ).fetchall()

    keys: set = set()
    for row in rows:
        strike = Decimal(row["strike"]) if row["strike"] is not None else None
        keys.add(
            (row["trader_id"], row["symbol"].upper(), row["option_type"], strike,
             row["expiration"])
        )

    return sorted(keys, key=_lifecycle_key_sort_key)


def _check_invariant_h_multiple_active_per_key(conn: sqlite3.Connection) -> list[str]:
    """Invariant H: at most one current non-terminal ('open'/
    'partially_closed') lifecycle may exist per normalized key.

    Grouped in Python, not SQL: strike must be compared via Decimal, and
    symbol case-insensitively, neither of which a raw SQL GROUP BY on the
    stored text/columns would get right (SQLite's GROUP BY does treat
    NULL as equal to NULL, which is correct for the equity-key case, but
    it cannot know that stored strike text "207.50" and "207.5" are the
    same number).

    Returns:
        One human-readable violation string per key with more than one
        current non-terminal row, naming the exact violating
        trade_lifecycles.id values, in a deterministic, normalized order
        (see _lifecycle_key_sort_key()) - independent of trade_lifecycles
        row-insertion order or SQL row-return order. The key itself is
        placed before the (inherently insertion-order-correlated)
        trade_lifecycles.id list within each message, so that sorting
        these strings - whether here or by the caller re-sorting the
        full combined violations list - is driven by the key's own
        content (trader/symbol/strike/expiration), never by which row
        happened to be inserted first and therefore received a lower id.
        Empty list if the invariant holds.
    """
    rows = conn.execute(
        "SELECT id, trader_id, symbol, option_type, strike, expiration "
        "FROM trade_lifecycles WHERE is_current = 1 AND status IN ('open', 'partially_closed') "
        "ORDER BY id"
    ).fetchall()

    groups: dict = {}
    for row in rows:
        strike = Decimal(row["strike"]) if row["strike"] is not None else None
        key = (
            row["trader_id"], row["symbol"].upper(), row["option_type"], strike,
            row["expiration"],
        )
        groups.setdefault(key, []).append(row["id"])

    violations = []
    for key in sorted(groups.keys(), key=_lifecycle_key_sort_key):
        ids = groups[key]
        if len(ids) > 1:
            # The key is deliberately placed before the ids list in the
            # message text itself: sorting these strings lexicographically
            # (whether here or by a caller re-sorting a larger combined
            # list) must be driven by the key's own content, never by the
            # (insertion-order-correlated) trade_lifecycles.id values.
            violations.append(
                f"Invariant H violated: key {key!r} has {len(ids)} current "
                f"non-terminal lifecycles {sorted(ids)}."
            )
    return sorted(violations)


def validate_lifecycle_membership_integrity(conn: sqlite3.Connection) -> list[str]:
    """Check every approved lifecycle membership invariant (A-H).

    Performs no writes and raises no service-layer exception - this is a
    read-only, deterministic check. TradeService (R6.4, not yet
    implemented) is responsible for raising LifecycleIntegrityError on any
    non-empty result and rolling back its own transaction; this function
    only reports.

    Every check is scoped to is_current = 1 lifecycles (or signals
    reachable from one), so a superseded generation's own membership -
    however it looks - never triggers a violation here: audit rows tied
    to a superseded generation remain fully permitted, exactly matching
    "auditable lifecycle history."

    Invariants checked:
        A. One signal belongs to at most one current lifecycle.
        B. Every current lifecycle-event membership agrees with
           trade_signals.lifecycle_id.
        C. Every non-NULL trade_signals.lifecycle_id references an
           existing lifecycle with is_current = 1.
        D. Every non-NULL lifecycle_id has a matching
           trade_lifecycle_events row for that same lifecycle and signal.
        E/F. Every signal contained in a current lifecycle is itself
           current (extraction_id IS NULL OR message_extractions.is_current = 1)
           - equivalently, no superseded signal remains inside any
           current lifecycle. Logically equivalent (each the contrapositive
           of the other); checked with one query.
        G. Every event_type IS NULL legacy signal has lifecycle_id IS NULL.
        H. At most one current non-terminal ('open'/'partially_closed')
           lifecycle exists per normalized key.

    Args:
        conn: An open sqlite3.Connection.

    Returns:
        A list of human-readable violation descriptions, each naming
        which invariant failed and the exact violating id(s)/detail, in
        deterministic sorted order - never dependent on SQL row-return
        order, so repeated calls against unchanged state, or calls
        against equivalent data inserted in a different order, always
        return an identically-ordered list. Empty list if every
        invariant holds.
    """
    violations: list[str] = []

    rows = conn.execute(
        "SELECT tle.trade_signal_id, COUNT(DISTINCT tle.trade_lifecycle_id) AS n "
        "FROM trade_lifecycle_events tle "
        "JOIN trade_lifecycles tl ON tl.id = tle.trade_lifecycle_id "
        "WHERE tl.is_current = 1 "
        "GROUP BY tle.trade_signal_id HAVING n > 1"
    ).fetchall()
    for row in rows:
        violations.append(
            f"Invariant A violated: trade_signal_id {row['trade_signal_id']} belongs to "
            f"{row['n']} current lifecycles."
        )

    rows = conn.execute(
        "SELECT tle.trade_signal_id, tl.id AS trade_lifecycle_id, ts.lifecycle_id "
        "FROM trade_lifecycle_events tle "
        "JOIN trade_lifecycles tl ON tl.id = tle.trade_lifecycle_id "
        "JOIN trade_signals ts ON ts.id = tle.trade_signal_id "
        "WHERE tl.is_current = 1 AND ts.lifecycle_id IS NOT tl.id"
    ).fetchall()
    for row in rows:
        violations.append(
            f"Invariant B violated: trade_signal_id {row['trade_signal_id']} is a current "
            f"member of trade_lifecycle_id {row['trade_lifecycle_id']} but its own "
            f"lifecycle_id is {row['lifecycle_id']}."
        )

    rows = conn.execute(
        "SELECT ts.id, ts.lifecycle_id FROM trade_signals ts "
        "WHERE ts.lifecycle_id IS NOT NULL "
        "AND NOT EXISTS ("
        "    SELECT 1 FROM trade_lifecycles tl "
        "    WHERE tl.id = ts.lifecycle_id AND tl.is_current = 1"
        ")"
    ).fetchall()
    for row in rows:
        violations.append(
            f"Invariant C violated: trade_signal_id {row['id']} has lifecycle_id "
            f"{row['lifecycle_id']}, which is not a current (is_current=1) lifecycle."
        )

    rows = conn.execute(
        "SELECT ts.id, ts.lifecycle_id FROM trade_signals ts "
        "WHERE ts.lifecycle_id IS NOT NULL "
        "AND NOT EXISTS ("
        "    SELECT 1 FROM trade_lifecycle_events tle "
        "    WHERE tle.trade_lifecycle_id = ts.lifecycle_id AND tle.trade_signal_id = ts.id"
        ")"
    ).fetchall()
    for row in rows:
        violations.append(
            f"Invariant D violated: trade_signal_id {row['id']} has lifecycle_id "
            f"{row['lifecycle_id']} but no matching trade_lifecycle_events row exists."
        )

    rows = conn.execute(
        "SELECT tle.trade_signal_id, tl.id AS trade_lifecycle_id "
        "FROM trade_lifecycle_events tle "
        "JOIN trade_lifecycles tl ON tl.id = tle.trade_lifecycle_id "
        "JOIN trade_signals ts ON ts.id = tle.trade_signal_id "
        "LEFT JOIN message_extractions me ON me.id = ts.extraction_id "
        "WHERE tl.is_current = 1 "
        "AND NOT (ts.extraction_id IS NULL OR me.is_current = 1)"
    ).fetchall()
    for row in rows:
        violations.append(
            f"Invariant E/F violated: trade_signal_id {row['trade_signal_id']} is a member "
            f"of current trade_lifecycle_id {row['trade_lifecycle_id']} but is itself "
            "superseded (not current)."
        )

    rows = conn.execute(
        "SELECT id FROM trade_signals WHERE event_type IS NULL AND lifecycle_id IS NOT NULL"
    ).fetchall()
    for row in rows:
        violations.append(
            f"Invariant G violated: legacy trade_signal_id {row['id']} (event_type IS NULL) "
            "has a non-NULL lifecycle_id."
        )

    violations.extend(_check_invariant_h_multiple_active_per_key(conn))

    # Sorted regardless of SQL row-return order or which invariant found
    # what first, so repeated calls against the same state - and calls
    # against equivalent data built in a different insertion order -
    # always return byte-identical, identically-ordered results.
    return sorted(violations)
