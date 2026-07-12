"""Source-related data-access functions for Discord Traders.

The only module permitted to run SQL against the ``sources`` table.
Callers are responsible for opening/closing the connection and for
committing or rolling back the transaction.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from database.models import Source


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
