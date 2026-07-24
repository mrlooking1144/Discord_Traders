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

from database.models import (
    Channel,
    ImportBatch,
    MessageExtraction,
    RawMessage,
    Source,
    Trader,
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


def _compute_content_hash(raw_text: str) -> str:
    """Compute the content hash used for duplicate-lookup fallback.

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
    content_hash = _compute_content_hash(raw_text)
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
    )


_TRADE_SIGNAL_COLUMNS = (
    "id, raw_message_id, trader_id, symbol, action, option_type, price, "
    "expiration, position_size, created_at, updated_at, strike, "
    "expiration_raw, event_type, qualifier, stated_entry_price, "
    "stated_return_pct, notes, extraction_id"
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
    where_clauses: list[str] = []
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
        f"{where_sql} "
        "ORDER BY trade_signals.id DESC "
        "LIMIT ?",
        (*params, limit),
    ).fetchall()

    return [dict(row) for row in rows]


_UNSPECIFIED_CHANNEL_EXTERNAL_ID = "__unspecified__"


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
        conn, source_id, _UNSPECIFIED_CHANNEL_EXTERNAL_ID, name="unspecified"
    )


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
