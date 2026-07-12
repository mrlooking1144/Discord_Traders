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
from decimal import Decimal
from typing import Optional

from database.models import RawMessage, Source, Trader, TradeSignal, TradeSignalEdit


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
    )


def create_trader(
    conn: sqlite3.Connection,
    source_id: int,
    name: str,
    external_trader_id: str | None = None,
) -> Trader:
    """Insert a new trader row.

    Args:
        conn: An open sqlite3.Connection.
        source_id: FK to sources.id.
        name: Display name/handle as seen in the source.
        external_trader_id: Stable source-provided trader ID, when available.

    Returns:
        The newly created Trader, including its generated id and created_at.

    Raises:
        ValueError: If name is empty or whitespace-only.
        sqlite3.IntegrityError: If source_id does not reference an existing
            source, or if external_trader_id is not None and already exists
            for this source_id.
    """
    _validate_trader_name(name)

    cursor = conn.execute(
        "INSERT INTO traders (source_id, name, external_trader_id) "
        "VALUES (?, ?, ?)",
        (source_id, name, external_trader_id),
    )
    row = conn.execute(
        "SELECT id, source_id, name, external_trader_id, created_at "
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
        "SELECT id, source_id, name, external_trader_id, created_at "
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
        "SELECT id, source_id, name, external_trader_id, created_at "
        "FROM traders WHERE source_id = ? AND name = ? ORDER BY id",
        (source_id, name),
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
    )


def create_raw_message(
    conn: sqlite3.Connection,
    source_id: int,
    raw_text: str,
    external_id: str | None = None,
    metadata: dict | None = None,
    received_at: str | None = None,
) -> RawMessage:
    """Insert a new raw message row.

    raw_text is stored exactly as supplied: no stripping, case changes,
    Unicode normalization, or line-ending changes are performed.

    Args:
        conn: An open sqlite3.Connection.
        source_id: FK to sources.id.
        raw_text: The original message, verbatim.
        external_id: Source-provided message ID, when available.
        metadata: Opaque JSON-serializable metadata, or None.
        received_at: ISO8601 timestamp of when the source sent the message.

    Returns:
        The newly created RawMessage, including its generated id,
        computed content_hash, and DB-defaulted ingested_at.

    Raises:
        sqlite3.IntegrityError: If source_id does not reference an existing
            source, or if external_id is not None and already exists for
            this source_id.
        TypeError: If metadata is not JSON-serializable.
    """
    content_hash = _compute_content_hash(raw_text)
    serialized_metadata = (
        json.dumps(metadata, sort_keys=True) if metadata is not None else None
    )

    cursor = conn.execute(
        "INSERT INTO raw_messages "
        "(source_id, external_id, raw_text, content_hash, metadata, received_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (source_id, external_id, raw_text, content_hash, serialized_metadata, received_at),
    )
    row = conn.execute(
        "SELECT id, source_id, external_id, raw_text, content_hash, metadata, "
        "received_at, ingested_at FROM raw_messages WHERE id = ?",
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
        "SELECT id, source_id, external_id, raw_text, content_hash, metadata, "
        "received_at, ingested_at FROM raw_messages "
        "WHERE source_id = ? AND external_id = ?",
        (source_id, external_id),
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
        "SELECT id, source_id, external_id, raw_text, content_hash, metadata, "
        "received_at, ingested_at FROM raw_messages "
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
) -> TradeSignal:
    """Insert a new trade signal row.

    Args:
        conn: An open sqlite3.Connection.
        raw_message_id: FK to raw_messages.id.
        trader_id: FK to traders.id.
        symbol: Ticker symbol.
        action: Free-text trade action (e.g. BTO/STC), stored exactly as
            supplied (not stripped, uppercased, or otherwise normalized).
        option_type: Free-text call/put, or None for non-option trades.
        price: A Decimal price, or None. Never a float or string.
        expiration: ISO8601 date string, or None.
        position_size: Raw wording of position size, or None.

    Returns:
        The newly created TradeSignal, including its generated id,
        created_at, and updated_at.

    Raises:
        ValueError: If raw_message_id or trader_id is None, or if symbol or
            action is None, empty, or whitespace-only.
        TypeError: If price is supplied and is not a Decimal.
        sqlite3.IntegrityError: If raw_message_id or trader_id does not
            reference an existing row.
    """
    _validate_trade_signal_required_fields(raw_message_id, trader_id, symbol, action)
    price_text = _serialize_price(price)

    cursor = conn.execute(
        "INSERT INTO trade_signals "
        "(raw_message_id, trader_id, symbol, action, option_type, price, "
        "expiration, position_size) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            raw_message_id,
            trader_id,
            symbol,
            action,
            option_type,
            price_text,
            expiration,
            position_size,
        ),
    )
    row = conn.execute(
        "SELECT id, raw_message_id, trader_id, symbol, action, option_type, "
        "price, expiration, position_size, created_at, updated_at "
        "FROM trade_signals WHERE id = ?",
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
        "SELECT id, raw_message_id, trader_id, symbol, action, option_type, "
        "price, expiration, position_size, created_at, updated_at "
        "FROM trade_signals WHERE id = ?",
        (trade_signal_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_trade_signal(row)


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
