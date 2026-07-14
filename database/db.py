"""SQLite connection and initialization layer for Discord Traders.

This is the only module that opens SQLite connections or applies
database/schema.sql. It contains no business logic, repository queries,
or application-specific workflows.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from database.config import DatabaseConfig

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def get_connection(config: DatabaseConfig) -> sqlite3.Connection:
    """Open a SQLite connection configured per the given DatabaseConfig.

    The connection is opened against ``config.db_path`` with a busy timeout
    of ``config.busy_timeout_seconds``, has foreign-key enforcement enabled
    (``PRAGMA foreign_keys = ON``), and uses ``sqlite3.Row`` as its row
    factory.

    Args:
        config: Database configuration providing the file path and busy
            timeout to use.

    Returns:
        An open ``sqlite3.Connection`` ready for use.
    """
    connection = sqlite3.connect(config.db_path, timeout=config.busy_timeout_seconds)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(config: DatabaseConfig) -> None:
    """Apply database/schema.sql to the database described by config.

    Opens its own connection, executes the existing schema.sql (whose
    statements are all ``IF NOT EXISTS``, so applying it against an
    already-initialized database is safe and creates nothing new), and
    always closes the connection afterward, even if schema application
    fails.

    Args:
        config: Database configuration providing the file path and busy
            timeout to use.
    """
    Path(config.db_path).parent.mkdir(parents=True, exist_ok=True)
    connection = get_connection(config)
    try:
        schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
        connection.executescript(schema_sql)
        connection.commit()
    finally:
        connection.close()
