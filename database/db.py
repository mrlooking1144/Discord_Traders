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
_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


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


def _ensure_schema_migrations_table(connection: sqlite3.Connection) -> None:
    """Create the schema_migrations tracking table if it does not exist.

    Args:
        connection: An open sqlite3.Connection.
    """
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "filename TEXT NOT NULL UNIQUE, "
        "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )


def _split_sql_statements(sql_script: str) -> list[str]:
    """Split a migration file's SQL text into individual statements.

    A minimal, non-general-purpose splitter: splits on ";" and discards
    blank fragments (comment-only fragments execute harmlessly as no-ops).
    Sufficient for this project's migration files, which contain only
    simple CREATE/ALTER/DROP/INSERT/UPDATE statements with no semicolons
    inside string literals, triggers, or stored procedures.

    This exists because ``sqlite3.Connection.executescript()`` cannot be
    used for migrations that must be atomic with their tracking-row
    insert: executescript() unconditionally issues a COMMIT before running
    the script, which would commit our explicit BEGIN and defeat wrapping
    the migration's DDL and its schema_migrations row in one transaction.
    Executing each statement individually via ``connection.execute()``
    keeps everything inside our own explicit transaction instead.

    Args:
        sql_script: The full text of a migration file.

    Returns:
        The non-blank statements, in order, with surrounding whitespace
        stripped and no trailing semicolon.
    """
    statements = []
    for fragment in sql_script.split(";"):
        statement = fragment.strip()
        if statement:
            statements.append(statement)
    return statements


def apply_migrations(
    connection: sqlite3.Connection,
    migrations_dir: Path | None = None,
) -> None:
    """Apply any not-yet-applied migration scripts, atomically per file.

    Migration filenames sort in application order (e.g. "0001_...sql" before
    "0002_...sql"). Each pending file is applied inside one explicit
    transaction (``BEGIN IMMEDIATE`` ... ``COMMIT``) that covers both its
    statements and the ``INSERT`` recording it in schema_migrations: if any
    statement in the file fails, the whole transaction is rolled back, so
    the file's filename is never recorded and none of its DDL/DML persists
    - the next call to apply_migrations() will retry that same file from a
    clean slate rather than silently re-running only part of it. This is
    what lets database/schema.sql's original ``CREATE ... IF NOT EXISTS``
    baseline coexist with later ``ALTER TABLE`` migrations, which SQLite
    does not support re-running idempotently on its own.

    Args:
        connection: An open sqlite3.Connection. Must not have a transaction
            already open when this is called.
        migrations_dir: Directory containing "*.sql" migration files,
            applied in filename-sorted order. Defaults to
            database/migrations.

    Raises:
        sqlite3.Error: If a migration file's SQL fails; the failing
            migration's changes are rolled back before the exception
            propagates, and schema_migrations does not record it.
    """
    if migrations_dir is None:
        migrations_dir = _MIGRATIONS_DIR

    _ensure_schema_migrations_table(connection)
    applied = {
        row[0]
        for row in connection.execute(
            "SELECT filename FROM schema_migrations"
        ).fetchall()
    }

    for migration_file in sorted(migrations_dir.glob("*.sql")):
        if migration_file.name in applied:
            continue

        statements = _split_sql_statements(
            migration_file.read_text(encoding="utf-8")
        )

        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (filename) VALUES (?)",
                (migration_file.name,),
            )
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()


def initialize_database(config: DatabaseConfig) -> None:
    """Apply database/schema.sql and any pending migrations to config's database.

    Opens its own connection, executes the existing schema.sql (whose
    statements are all ``IF NOT EXISTS``, so applying it against an
    already-initialized database is safe and creates nothing new), then
    applies any not-yet-applied files in database/migrations (see
    apply_migrations), and always closes the connection afterward, even if
    schema or migration application fails.

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
        apply_migrations(connection)
    finally:
        connection.close()
