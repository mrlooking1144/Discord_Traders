"""Data-access functions for Discord Traders.

The only module permitted to run SQL against the database, built up
incrementally table by table (Milestone 2B.5). Callers are responsible for
opening/closing the connection and for committing or rolling back the
transaction.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from database.models import Source, Trader


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
