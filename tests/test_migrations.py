"""Tests for the Recovery Milestone R1 migration framework.

Covers database/db.py's apply_migrations()/initialize_database() extension:
fresh-database migration application, idempotent re-application, atomic
per-migration rollback on failure, and upgrading a v0.1.0-shaped database
(the historical baseline, no migrations applied yet, with pre-existing
data) in place without data loss.
"""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from database.config import DatabaseConfig
from database.db import apply_migrations, get_connection, initialize_database

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "database" / "migrations"

# The exact historical v0.1.0 schema.sql content, frozen here rather than
# read from the live file. Real deployed v0.1.0 databases already have the
# old idx_raw_messages_source_external_id index physically created - that
# statement has since been removed from the live database/schema.sql (see
# docs/DECISIONS/0001_recovery_message_storage_and_lifecycle_schema.md), so
# reading the live file here would no longer reproduce a real upgrade
# scenario. This literal is what actually shipped in v0.1.0 and is what
# every backward-compatibility test in this module upgrades from.
_V010_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sources (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS traders (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id          INTEGER NOT NULL REFERENCES sources(id),
    name               TEXT NOT NULL,
    external_trader_id TEXT,
    created_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_traders_source_external_id
    ON traders (source_id, external_trader_id)
    WHERE external_trader_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_traders_source_name
    ON traders (source_id, name);

CREATE TABLE IF NOT EXISTS raw_messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id    INTEGER NOT NULL REFERENCES sources(id),
    external_id  TEXT,
    raw_text     TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    metadata     TEXT,
    received_at  TEXT,
    ingested_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_messages_source_external_id
    ON raw_messages (source_id, external_id)
    WHERE external_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_raw_messages_content_hash
    ON raw_messages (content_hash);

CREATE TABLE IF NOT EXISTS trade_signals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_message_id INTEGER NOT NULL REFERENCES raw_messages(id),
    trader_id      INTEGER NOT NULL REFERENCES traders(id),
    symbol         TEXT NOT NULL,
    action         TEXT NOT NULL,
    option_type    TEXT,
    price          TEXT,
    expiration     TEXT,
    position_size  TEXT,
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_trade_signals_raw_message_id
    ON trade_signals (raw_message_id);

CREATE INDEX IF NOT EXISTS idx_trade_signals_trader_id
    ON trade_signals (trader_id);

CREATE INDEX IF NOT EXISTS idx_trade_signals_symbol
    ON trade_signals (symbol);

CREATE TABLE IF NOT EXISTS trade_signal_edits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_signal_id INTEGER NOT NULL REFERENCES trade_signals(id),
    previous_values TEXT NOT NULL,
    edited_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_trade_signal_edits_trade_signal_id
    ON trade_signal_edits (trade_signal_id);
"""


def _table_names(connection):
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }


def _column_names(connection, table):
    return {
        row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _index_names(connection):
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }


def _foreign_keys(connection, table):
    """Return the exact (from_column, target_table, to_column) triples for
    every foreign key defined on `table`, via PRAGMA foreign_key_list - not
    merely the set of target table names."""
    return {
        (row[3], row[2], row[4])  # from, table, to
        for row in connection.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    }


def _column_type_and_notnull(connection, table, column):
    """Return (declared_type, notnull) for one column via PRAGMA table_info."""
    for row in connection.execute(f"PRAGMA table_info({table})").fetchall():
        if row[1] == column:
            return row[2], bool(row[3])
    raise AssertionError(f"column {column!r} not found on table {table!r}")


def _index_is_unique(connection, table, index_name):
    """Return whether `index_name` on `table` is unique, via PRAGMA index_list."""
    for row in connection.execute(f"PRAGMA index_list({table})").fetchall():
        if row[1] == index_name:
            return bool(row[2])
    raise AssertionError(f"index {index_name!r} not found on table {table!r}")


def _index_columns_in_order(connection, index_name):
    """Return the exact, ordered column names of `index_name`, via
    PRAGMA index_info (sorted by seqno, the index's own column position -
    not insertion order of the returned rows)."""
    rows = sorted(
        connection.execute(f"PRAGMA index_info({index_name})").fetchall(),
        key=lambda row: row[0],
    )
    return [row[2] for row in rows]


class FreshDatabaseMigrationTests(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = path
        self.config = DatabaseConfig(db_path=path)

    def tearDown(self):
        os.remove(self.db_path)

    def test_new_tables_created(self):
        initialize_database(self.config)

        connection = get_connection(self.config)
        try:
            tables = _table_names(connection)
        finally:
            connection.close()

        self.assertTrue(
            {
                "channels",
                "import_batches",
                "message_extractions",
                "schema_migrations",
            }.issubset(tables)
        )

    def test_new_columns_added(self):
        initialize_database(self.config)

        connection = get_connection(self.config)
        try:
            raw_message_columns = _column_names(connection, "raw_messages")
            trade_signal_columns = _column_names(connection, "trade_signals")
            trader_columns = _column_names(connection, "traders")
        finally:
            connection.close()

        self.assertTrue(
            {"channel_id", "import_batch_id", "sequence_in_batch"}.issubset(
                raw_message_columns
            )
        )
        self.assertTrue(
            {
                "strike",
                "expiration_raw",
                "event_type",
                "qualifier",
                "stated_entry_price",
                "stated_return_pct",
                "notes",
                "extraction_id",
            }.issubset(trade_signal_columns)
        )
        self.assertIn(
            "lifecycle_id",
            trade_signal_columns,
            "lifecycle_id is added by Recovery Milestone R6.1 alongside trade_lifecycles",
        )
        self.assertIn("canonical_name", trader_columns)

    def test_all_migrations_recorded_exactly_once(self):
        initialize_database(self.config)

        connection = get_connection(self.config)
        try:
            applied = {
                row[0]
                for row in connection.execute(
                    "SELECT filename FROM schema_migrations"
                ).fetchall()
            }
        finally:
            connection.close()

        expected = {p.name for p in _MIGRATIONS_DIR.glob("*.sql")}
        self.assertEqual(applied, expected)
        self.assertGreaterEqual(len(expected), 7)

    def test_reinitializing_is_idempotent(self):
        initialize_database(self.config)
        initialize_database(self.config)

        connection = get_connection(self.config)
        try:
            counts = connection.execute(
                "SELECT filename, COUNT(*) FROM schema_migrations GROUP BY filename"
            ).fetchall()
        finally:
            connection.close()

        for _, count in counts:
            self.assertEqual(count, 1)

    def test_empty_fresh_database_backfill_inserts_nothing(self):
        initialize_database(self.config)

        connection = get_connection(self.config)
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM message_extractions"
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(count, 0)

    def test_message_extraction_rejects_invalid_parse_status(self):
        initialize_database(self.config)

        connection = get_connection(self.config)
        try:
            connection.execute("INSERT INTO sources (name) VALUES ('discord')")
            source_id = connection.execute(
                "SELECT id FROM sources WHERE name = 'discord'"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO raw_messages (source_id, raw_text, content_hash) "
                "VALUES (?, 'x', 'hash-x')",
                (source_id,),
            )
            raw_message_id = connection.execute(
                "SELECT id FROM raw_messages WHERE content_hash = 'hash-x'"
            ).fetchone()[0]

            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO message_extractions "
                    "(raw_message_id, parser_version, parse_status) "
                    "VALUES (?, 'v1', 'bogus_status')",
                    (raw_message_id,),
                )
        finally:
            connection.close()

    def test_at_most_one_current_extraction_per_raw_message(self):
        initialize_database(self.config)

        connection = get_connection(self.config)
        try:
            connection.execute("INSERT INTO sources (name) VALUES ('discord')")
            source_id = connection.execute(
                "SELECT id FROM sources WHERE name = 'discord'"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO raw_messages (source_id, raw_text, content_hash) "
                "VALUES (?, 'x', 'hash-y')",
                (source_id,),
            )
            raw_message_id = connection.execute(
                "SELECT id FROM raw_messages WHERE content_hash = 'hash-y'"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO message_extractions "
                "(raw_message_id, parser_version, parse_status) "
                "VALUES (?, 'v1', 'parsed')",
                (raw_message_id,),
            )
            connection.commit()

            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO message_extractions "
                    "(raw_message_id, parser_version, parse_status) "
                    "VALUES (?, 'v2', 'parsed')",
                    (raw_message_id,),
                )
        finally:
            connection.close()


class R6LifecycleSchemaMigrationTests(unittest.TestCase):
    """Recovery Milestone R6.1: trade_lifecycles / trade_lifecycle_events /
    trade_signals.lifecycle_id.

    Schema/migration-level tests only - no matching/linking behavior exists
    yet. Verifies the migration's tables, columns, constraints, and indexes
    against a fresh database, following the same pattern as
    FreshDatabaseMigrationTests above.
    """

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = path
        self.config = DatabaseConfig(db_path=path)
        initialize_database(self.config)
        self.connection = get_connection(self.config)

    def tearDown(self):
        self.connection.close()
        os.remove(self.db_path)

    def _insert_source_trader_message_signal(self):
        conn = self.connection
        conn.execute("INSERT INTO sources (name) VALUES ('discord')")
        source_id = conn.execute(
            "SELECT id FROM sources WHERE name = 'discord'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO traders (source_id, name) VALUES (?, 'TC')", (source_id,)
        )
        trader_id = conn.execute(
            "SELECT id FROM traders WHERE name = 'TC'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO raw_messages (source_id, raw_text, content_hash) "
            "VALUES (?, 'x', 'hash-r6')",
            (source_id,),
        )
        raw_message_id = conn.execute(
            "SELECT id FROM raw_messages WHERE content_hash = 'hash-r6'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO trade_signals (raw_message_id, trader_id, symbol, action) "
            "VALUES (?, ?, 'IBM', 'BOUGHT')",
            (raw_message_id, trader_id),
        )
        signal_id = conn.execute(
            "SELECT id FROM trade_signals WHERE raw_message_id = ?", (raw_message_id,)
        ).fetchone()[0]
        conn.commit()
        return trader_id, signal_id

    def test_new_lifecycle_tables_created(self):
        tables = _table_names(self.connection)
        self.assertIn("trade_lifecycles", tables)
        self.assertIn("trade_lifecycle_events", tables)

    def test_trade_lifecycles_columns_match_approved_schema(self):
        columns = _column_names(self.connection, "trade_lifecycles")
        self.assertEqual(
            columns,
            {
                "id", "trader_id", "symbol", "option_type", "strike", "expiration",
                "status", "remaining_fraction", "opened_by_signal_id",
                "closed_by_signal_id", "is_current", "superseded_at",
                "ambiguity_flags", "created_at", "updated_at",
            },
        )

    def test_trade_lifecycle_events_columns_match_approved_schema(self):
        columns = _column_names(self.connection, "trade_lifecycle_events")
        self.assertEqual(
            columns,
            {
                "id", "trade_lifecycle_id", "trade_signal_id", "sequence_index",
                "signal_snapshot", "created_at",
            },
        )

    def test_trade_signals_gains_lifecycle_id_column(self):
        self.assertIn("lifecycle_id", _column_names(self.connection, "trade_signals"))

    def test_expected_indexes_exist(self):
        index_names = _index_names(self.connection)
        self.assertIn("idx_trade_lifecycles_key", index_names)
        self.assertIn("idx_trade_lifecycle_events_unique_membership", index_names)
        self.assertIn("idx_trade_lifecycle_events_signal_id", index_names)
        self.assertIn("idx_trade_signals_lifecycle_id", index_names)

    def test_foreign_keys_reference_expected_tables(self):
        fk_targets = {
            row[2]  # PRAGMA foreign_key_list: table
            for row in self.connection.execute(
                "PRAGMA foreign_key_list(trade_lifecycles)"
            ).fetchall()
        }
        self.assertEqual(fk_targets, {"traders", "trade_signals"})

        fk_targets = {
            row[2]
            for row in self.connection.execute(
                "PRAGMA foreign_key_list(trade_lifecycle_events)"
            ).fetchall()
        }
        self.assertEqual(fk_targets, {"trade_lifecycles", "trade_signals"})

    def test_trade_lifecycles_status_check_rejects_invalid_value(self):
        trader_id, signal_id = self._insert_source_trader_message_signal()

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO trade_lifecycles "
                "(trader_id, symbol, status, remaining_fraction) "
                "VALUES (?, 'IBM', 'bogus_status', '1')",
                (trader_id,),
            )

    def test_trade_lifecycles_status_check_accepts_every_approved_value(self):
        trader_id, signal_id = self._insert_source_trader_message_signal()

        for status in (
            "open", "partially_closed", "closed", "orphan", "unresolved", "invalid",
        ):
            self.connection.execute(
                "INSERT INTO trade_lifecycles "
                "(trader_id, symbol, status, remaining_fraction) "
                "VALUES (?, 'IBM', ?, '1')",
                (trader_id, status),
            )
        self.connection.commit()

        count = self.connection.execute(
            "SELECT COUNT(*) FROM trade_lifecycles"
        ).fetchone()[0]
        self.assertEqual(count, 6)

    def test_trade_lifecycles_is_current_check_rejects_invalid_value(self):
        trader_id, signal_id = self._insert_source_trader_message_signal()

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO trade_lifecycles "
                "(trader_id, symbol, status, remaining_fraction, is_current) "
                "VALUES (?, 'IBM', 'open', '1', 2)",
                (trader_id,),
            )

    def test_trade_lifecycles_is_current_defaults_to_one(self):
        trader_id, signal_id = self._insert_source_trader_message_signal()

        self.connection.execute(
            "INSERT INTO trade_lifecycles (trader_id, symbol, status, remaining_fraction) "
            "VALUES (?, 'IBM', 'open', '1')",
            (trader_id,),
        )
        self.connection.commit()

        is_current = self.connection.execute(
            "SELECT is_current FROM trade_lifecycles WHERE trader_id = ?", (trader_id,)
        ).fetchone()[0]
        self.assertEqual(is_current, 1)

    def test_trade_lifecycles_trader_id_foreign_key_enforced(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO trade_lifecycles "
                "(trader_id, symbol, status, remaining_fraction) "
                "VALUES (999999, 'IBM', 'open', '1')"
            )

    def test_trade_lifecycle_events_signal_snapshot_is_not_null(self):
        trader_id, signal_id = self._insert_source_trader_message_signal()
        self.connection.execute(
            "INSERT INTO trade_lifecycles (trader_id, symbol, status, remaining_fraction) "
            "VALUES (?, 'IBM', 'open', '1')",
            (trader_id,),
        )
        lifecycle_id = self.connection.execute(
            "SELECT id FROM trade_lifecycles WHERE trader_id = ?", (trader_id,)
        ).fetchone()[0]
        self.connection.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO trade_lifecycle_events "
                "(trade_lifecycle_id, trade_signal_id, sequence_index) "
                "VALUES (?, ?, 1)",
                (lifecycle_id, signal_id),
            )

    def test_trade_lifecycle_events_unique_membership_enforced(self):
        trader_id, signal_id = self._insert_source_trader_message_signal()
        self.connection.execute(
            "INSERT INTO trade_lifecycles (trader_id, symbol, status, remaining_fraction) "
            "VALUES (?, 'IBM', 'open', '1')",
            (trader_id,),
        )
        lifecycle_id = self.connection.execute(
            "SELECT id FROM trade_lifecycles WHERE trader_id = ?", (trader_id,)
        ).fetchone()[0]
        self.connection.execute(
            "INSERT INTO trade_lifecycle_events "
            "(trade_lifecycle_id, trade_signal_id, sequence_index, signal_snapshot) "
            "VALUES (?, ?, 1, '{}')",
            (lifecycle_id, signal_id),
        )
        self.connection.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO trade_lifecycle_events "
                "(trade_lifecycle_id, trade_signal_id, sequence_index, signal_snapshot) "
                "VALUES (?, ?, 2, '{}')",
                (lifecycle_id, signal_id),
            )

    def test_existing_trade_signals_row_receives_null_lifecycle_id(self):
        _trader_id, signal_id = self._insert_source_trader_message_signal()

        lifecycle_id = self.connection.execute(
            "SELECT lifecycle_id FROM trade_signals WHERE id = ?", (signal_id,)
        ).fetchone()[0]
        self.assertIsNone(lifecycle_id)

    def test_migration_performs_no_backfill(self):
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM trade_lifecycles"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM trade_lifecycle_events"
            ).fetchone()[0],
            0,
        )


class R6LifecycleSchemaExactDefinitionTests(unittest.TestCase):
    """Recovery Milestone R6.1: verifies the *exact* approved schema
    definition, via direct PRAGMA evidence, rather than only object names.

    R6LifecycleSchemaMigrationTests above already confirms the tables,
    columns, and indexes exist and that the relevant CHECK/UNIQUE/NOT NULL
    constraints are enforced at the behavioral level (a bad INSERT is
    rejected). This class instead asserts the schema's own declared shape
    directly: exact source-column -> (target table, target column) foreign
    key mappings (not merely the set of tables a foreign key reaches),
    exact declared column type and NOT NULL flag for signal_snapshot/
    sequence_index, and exact index column order plus uniqueness for every
    R6.1 index.
    """

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = path
        self.config = DatabaseConfig(db_path=path)
        initialize_database(self.config)
        self.connection = get_connection(self.config)

    def tearDown(self):
        self.connection.close()
        os.remove(self.db_path)

    def test_trade_lifecycles_foreign_keys_exact(self):
        self.assertEqual(
            _foreign_keys(self.connection, "trade_lifecycles"),
            {
                ("trader_id", "traders", "id"),
                ("opened_by_signal_id", "trade_signals", "id"),
                ("closed_by_signal_id", "trade_signals", "id"),
            },
        )

    def test_trade_lifecycle_events_foreign_keys_exact(self):
        self.assertEqual(
            _foreign_keys(self.connection, "trade_lifecycle_events"),
            {
                ("trade_lifecycle_id", "trade_lifecycles", "id"),
                ("trade_signal_id", "trade_signals", "id"),
            },
        )

    def test_trade_signals_lifecycle_id_foreign_key_exact(self):
        # trade_signals also carries pre-existing foreign keys
        # (raw_message_id, trader_id, extraction_id) from earlier
        # milestones, out of R6.1 scope - this asserts only the R6.1
        # addition maps exactly as approved, not the complete set.
        self.assertIn(
            ("lifecycle_id", "trade_lifecycles", "id"),
            _foreign_keys(self.connection, "trade_signals"),
        )

    def test_signal_snapshot_column_is_exactly_text_not_null(self):
        column_type, notnull = _column_type_and_notnull(
            self.connection, "trade_lifecycle_events", "signal_snapshot"
        )
        self.assertEqual(column_type, "TEXT")
        self.assertTrue(notnull)

    def test_sequence_index_column_is_exactly_integer_not_null(self):
        column_type, notnull = _column_type_and_notnull(
            self.connection, "trade_lifecycle_events", "sequence_index"
        )
        self.assertEqual(column_type, "INTEGER")
        self.assertTrue(notnull)

    def test_idx_trade_lifecycles_key_exact_definition(self):
        self.assertEqual(
            _index_columns_in_order(self.connection, "idx_trade_lifecycles_key"),
            ["trader_id", "symbol", "option_type", "strike", "expiration", "is_current"],
        )
        self.assertFalse(
            _index_is_unique(
                self.connection, "trade_lifecycles", "idx_trade_lifecycles_key"
            )
        )

    def test_idx_trade_lifecycle_events_unique_membership_exact_definition(self):
        self.assertEqual(
            _index_columns_in_order(
                self.connection, "idx_trade_lifecycle_events_unique_membership"
            ),
            ["trade_lifecycle_id", "trade_signal_id"],
        )
        self.assertTrue(
            _index_is_unique(
                self.connection,
                "trade_lifecycle_events",
                "idx_trade_lifecycle_events_unique_membership",
            )
        )

    def test_idx_trade_lifecycle_events_signal_id_exact_definition(self):
        self.assertEqual(
            _index_columns_in_order(
                self.connection, "idx_trade_lifecycle_events_signal_id"
            ),
            ["trade_signal_id"],
        )
        self.assertFalse(
            _index_is_unique(
                self.connection,
                "trade_lifecycle_events",
                "idx_trade_lifecycle_events_signal_id",
            )
        )

    def test_idx_trade_signals_lifecycle_id_exact_definition(self):
        self.assertEqual(
            _index_columns_in_order(self.connection, "idx_trade_signals_lifecycle_id"),
            ["lifecycle_id"],
        )
        self.assertFalse(
            _index_is_unique(
                self.connection, "trade_signals", "idx_trade_signals_lifecycle_id"
            )
        )


class ChannelImportOperationsSchemaTests(unittest.TestCase):
    """Recovery Milestone R9a: channel_import_operations table (see
    database/migrations/0008_channel_import_operations.sql). A row
    represents only a successfully completed confirmed Bulk Channel
    Import operation - every CHECK constraint below enforces that
    contract at the database level, not only in service-layer
    validation (which does not exist yet - Recovery Milestone R9b)."""

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = path
        self.config = DatabaseConfig(db_path=path)
        initialize_database(self.config)
        self.connection = get_connection(self.config)

    def tearDown(self):
        self.connection.close()
        os.remove(self.db_path)

    def _seed_channel(self):
        conn = self.connection
        conn.execute("INSERT INTO sources (name) VALUES ('discord')")
        source_id = conn.execute(
            "SELECT id FROM sources WHERE name = 'discord'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO channels (source_id, external_channel_id, name) "
            "VALUES (?, 'chan-1', 'general')",
            (source_id,),
        )
        channel_id = conn.execute(
            "SELECT id FROM channels WHERE external_channel_id = 'chan-1'"
        ).fetchone()[0]
        conn.commit()
        return channel_id

    def _seed_channel_and_batch(self):
        channel_id = self._seed_channel()
        conn = self.connection
        source_id = conn.execute(
            "SELECT source_id FROM channels WHERE id = ?", (channel_id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO import_batches (source_id, reference_date, timezone) "
            "VALUES (?, '2026-01-01', 'UTC')",
            (source_id,),
        )
        import_batch_id = conn.execute(
            "SELECT id FROM import_batches WHERE source_id = ? ORDER BY id DESC LIMIT 1",
            (source_id,),
        ).fetchone()[0]
        conn.commit()
        return channel_id, import_batch_id

    def _insert_operation(self, channel_id, import_batch_id, **overrides):
        values = {
            "channel_id": channel_id,
            "import_batch_id": import_batch_id,
            "reference_date": "2026-01-01",
            "timezone": "UTC",
            "processed_count": 15,
            "stored_count": 15,
            "duplicate_count": 0,
            "unrecognized_count": 0,
            "failed_count": 0,
        }
        values.update(overrides)
        self.connection.execute(
            "INSERT INTO channel_import_operations ("
            "channel_id, import_batch_id, reference_date, timezone, "
            "processed_count, stored_count, duplicate_count, "
            "unrecognized_count, failed_count"
            ") VALUES (:channel_id, :import_batch_id, :reference_date, :timezone, "
            ":processed_count, :stored_count, :duplicate_count, "
            ":unrecognized_count, :failed_count)",
            values,
        )

    # -- table shape -------------------------------------------------------

    def test_table_exists(self):
        self.assertIn("channel_import_operations", _table_names(self.connection))

    def test_exact_columns(self):
        self.assertEqual(
            _column_names(self.connection, "channel_import_operations"),
            {
                "id", "channel_id", "import_batch_id", "reference_date",
                "timezone", "processed_count", "stored_count",
                "duplicate_count", "unrecognized_count", "failed_count",
                "committed_at",
            },
        )

    def test_foreign_keys_exact(self):
        self.assertEqual(
            _foreign_keys(self.connection, "channel_import_operations"),
            {
                ("channel_id", "channels", "id"),
                ("import_batch_id", "import_batches", "id"),
            },
        )

    def test_index_exists_on_channel_id(self):
        self.assertIn(
            "idx_channel_import_operations_channel_id", _index_names(self.connection)
        )
        self.assertEqual(
            _index_columns_in_order(
                self.connection, "idx_channel_import_operations_channel_id"
            ),
            ["channel_id"],
        )

    def test_migration_applied_after_all_previous_migrations(self):
        # Build a temp database with only migrations 0001-0007 staged
        # (0008 excluded), confirm channel_import_operations does not
        # exist yet, then apply the real migrations directory (which
        # only has 0008 left unapplied) and confirm the real migration
        # file builds cleanly on the channels/import_batches tables
        # those earlier migrations created.
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            schema_sql = (
                Path(__file__).resolve().parent.parent / "database" / "schema.sql"
            ).read_text(encoding="utf-8")
            conn.executescript(schema_sql)
            conn.commit()

            with tempfile.TemporaryDirectory() as staged_dir:
                staged = Path(staged_dir)
                for migration_file in sorted(_MIGRATIONS_DIR.glob("*.sql")):
                    if migration_file.name == "0008_channel_import_operations.sql":
                        continue
                    (staged / migration_file.name).write_text(
                        migration_file.read_text(encoding="utf-8"), encoding="utf-8"
                    )
                apply_migrations(conn, migrations_dir=staged)
            self.assertNotIn("channel_import_operations", _table_names(conn))

            apply_migrations(conn)  # real migrations dir - only 0008 remains unapplied
            self.assertIn("channel_import_operations", _table_names(conn))
        finally:
            conn.close()
            os.remove(path)

    def test_backward_compatible_with_preexisting_channel_and_batch_data(self):
        channel_id, import_batch_id = self._seed_channel_and_batch()
        before_channel = dict(
            self.connection.execute(
                "SELECT * FROM channels WHERE id = ?", (channel_id,)
            ).fetchone()
        )

        self._insert_operation(channel_id, import_batch_id)
        self.connection.commit()

        after_channel = dict(
            self.connection.execute(
                "SELECT * FROM channels WHERE id = ?", (channel_id,)
            ).fetchone()
        )
        self.assertEqual(before_channel, after_channel)
        count = self.connection.execute(
            "SELECT COUNT(*) FROM channel_import_operations"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    # -- CHECK constraints ---------------------------------------------------

    def test_every_count_rejects_negative(self):
        channel_id, import_batch_id = self._seed_channel_and_batch()
        for field in (
            "processed_count", "stored_count", "duplicate_count",
            "unrecognized_count", "failed_count",
        ):
            with self.subTest(field=field):
                with self.assertRaises(sqlite3.IntegrityError):
                    self._insert_operation(channel_id, import_batch_id, **{field: -1})

    def test_processed_count_14_rejected(self):
        channel_id, import_batch_id = self._seed_channel_and_batch()
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_operation(
                channel_id, import_batch_id,
                processed_count=14, stored_count=14, duplicate_count=0,
            )

    def test_processed_count_15_accepted(self):
        channel_id, import_batch_id = self._seed_channel_and_batch()
        self._insert_operation(
            channel_id, import_batch_id,
            processed_count=15, stored_count=15, duplicate_count=0,
        )
        self.connection.commit()
        count = self.connection.execute(
            "SELECT COUNT(*) FROM channel_import_operations"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_stored_plus_duplicate_must_equal_processed(self):
        channel_id, import_batch_id = self._seed_channel_and_batch()
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_operation(
                channel_id, import_batch_id,
                processed_count=20, stored_count=10, duplicate_count=5,
            )

    def test_unrecognized_count_exceeding_stored_rejected(self):
        channel_id, import_batch_id = self._seed_channel_and_batch()
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_operation(
                channel_id, import_batch_id,
                processed_count=15, stored_count=10, duplicate_count=5,
                unrecognized_count=11,
            )

    def test_failed_count_exceeding_stored_rejected(self):
        channel_id, import_batch_id = self._seed_channel_and_batch()
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_operation(
                channel_id, import_batch_id,
                processed_count=15, stored_count=10, duplicate_count=5,
                failed_count=11,
            )

    def test_individually_valid_unrecognized_and_failed_sum_exceeding_stored_rejected(
        self,
    ):
        # unrecognized_count=6 <= stored_count=10 and failed_count=6 <=
        # stored_count=10 each individually satisfy their own CHECK, but
        # their sum (12) exceeds stored_count - only the combined CHECK
        # (unrecognized_count + failed_count <= stored_count) catches
        # this, proving it is not redundant with the two individual ones.
        channel_id, import_batch_id = self._seed_channel_and_batch()
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_operation(
                channel_id, import_batch_id,
                processed_count=16, stored_count=10, duplicate_count=6,
                unrecognized_count=6, failed_count=6,
            )

    def test_zero_stored_with_nonnull_import_batch_id_rejected(self):
        channel_id, import_batch_id = self._seed_channel_and_batch()
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_operation(
                channel_id, import_batch_id,
                processed_count=15, stored_count=0, duplicate_count=15,
            )

    def test_positive_stored_with_null_import_batch_id_rejected(self):
        channel_id = self._seed_channel()
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_operation(
                channel_id, None,
                processed_count=15, stored_count=15, duplicate_count=0,
            )

    def test_duplicate_only_success_with_null_import_batch_id_accepted(self):
        channel_id = self._seed_channel()
        self._insert_operation(
            channel_id, None,
            processed_count=15, stored_count=0, duplicate_count=15,
        )
        self.connection.commit()
        row = self.connection.execute(
            "SELECT stored_count, import_batch_id FROM channel_import_operations"
        ).fetchone()
        self.assertEqual(row["stored_count"], 0)
        self.assertIsNone(row["import_batch_id"])

    def test_stored_message_success_with_valid_import_batch_id_accepted(self):
        channel_id, import_batch_id = self._seed_channel_and_batch()
        self._insert_operation(
            channel_id, import_batch_id,
            processed_count=15, stored_count=15, duplicate_count=0,
        )
        self.connection.commit()
        row = self.connection.execute(
            "SELECT stored_count, import_batch_id FROM channel_import_operations"
        ).fetchone()
        self.assertEqual(row["stored_count"], 15)
        self.assertEqual(row["import_batch_id"], import_batch_id)


class MigrationAtomicityTests(unittest.TestCase):
    """Verifies apply_migrations() applies each file atomically.

    Uses a temp migrations_dir with deliberately crafted files (a valid one
    plus a broken one) rather than touching database/migrations, so this
    never depends on - or risks corrupting - the project's real migrations.
    """

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = path
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)")
        self.connection.commit()

        self._migrations_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._migrations_tmp.cleanup)
        self.migrations_dir = Path(self._migrations_tmp.name)

    def tearDown(self):
        self.connection.close()
        os.remove(self.db_path)

    def _write_migration(self, filename, sql):
        (self.migrations_dir / filename).write_text(sql, encoding="utf-8")

    def _index_names(self):
        return {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }

    def _widget_columns(self):
        return {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(widgets)").fetchall()
        }

    def test_failed_migration_is_not_recorded(self):
        self._write_migration(
            "0001_broken.sql",
            "ALTER TABLE widgets ADD COLUMN extra TEXT;\n"
            "THIS IS NOT VALID SQL;\n",
        )

        with self.assertRaises(sqlite3.Error):
            apply_migrations(self.connection, migrations_dir=self.migrations_dir)

        applied = {
            row[0]
            for row in self.connection.execute(
                "SELECT filename FROM schema_migrations"
            ).fetchall()
        }
        self.assertNotIn("0001_broken.sql", applied)

    def test_failed_migration_leaves_no_partial_ddl_or_data(self):
        self._write_migration(
            "0001_broken.sql",
            "ALTER TABLE widgets ADD COLUMN extra TEXT;\n"
            "CREATE INDEX idx_widgets_name ON widgets (name);\n"
            "INSERT INTO widgets (name) VALUES ('should not survive');\n"
            "THIS IS NOT VALID SQL;\n",
        )

        with self.assertRaises(sqlite3.Error):
            apply_migrations(self.connection, migrations_dir=self.migrations_dir)

        self.assertNotIn("extra", self._widget_columns())
        self.assertNotIn("idx_widgets_name", self._index_names())
        count = self.connection.execute("SELECT COUNT(*) FROM widgets").fetchone()[0]
        self.assertEqual(count, 0)

    def test_successful_migration_before_a_later_failure_still_persists(self):
        self._write_migration(
            "0001_good.sql", "ALTER TABLE widgets ADD COLUMN good_column TEXT;\n"
        )
        self._write_migration("0002_broken.sql", "THIS IS NOT VALID SQL;\n")

        with self.assertRaises(sqlite3.Error):
            apply_migrations(self.connection, migrations_dir=self.migrations_dir)

        self.assertIn("good_column", self._widget_columns())
        applied = {
            row[0]
            for row in self.connection.execute(
                "SELECT filename FROM schema_migrations"
            ).fetchall()
        }
        self.assertIn("0001_good.sql", applied)
        self.assertNotIn("0002_broken.sql", applied)

    def test_a_successfully_applied_migration_cannot_remain_unrecorded(self):
        # The insert into schema_migrations is part of the same explicit
        # transaction as the migration's own statements, so there is no
        # window where the DDL committed but the tracking row did not.
        self._write_migration(
            "0001_good.sql", "ALTER TABLE widgets ADD COLUMN tracked_column TEXT;\n"
        )

        apply_migrations(self.connection, migrations_dir=self.migrations_dir)

        self.assertIn("tracked_column", self._widget_columns())
        applied = {
            row[0]
            for row in self.connection.execute(
                "SELECT filename FROM schema_migrations"
            ).fetchall()
        }
        self.assertIn("0001_good.sql", applied)

    def test_retry_after_fixing_broken_migration_succeeds_cleanly(self):
        self._write_migration("0001_fixable.sql", "THIS IS NOT VALID SQL;\n")

        with self.assertRaises(sqlite3.Error):
            apply_migrations(self.connection, migrations_dir=self.migrations_dir)
        self.assertNotIn("extra", self._widget_columns())

        # Fix the file (same filename) and retry: since the failed attempt
        # was never recorded, the retry re-applies it from a clean slate
        # rather than being skipped or double-applying a partial state.
        self._write_migration(
            "0001_fixable.sql", "ALTER TABLE widgets ADD COLUMN extra TEXT;\n"
        )

        apply_migrations(self.connection, migrations_dir=self.migrations_dir)

        self.assertIn("extra", self._widget_columns())
        applied = {
            row[0]
            for row in self.connection.execute(
                "SELECT filename FROM schema_migrations"
            ).fetchall()
        }
        self.assertIn("0001_fixable.sql", applied)

    def test_connection_is_usable_after_a_rolled_back_migration(self):
        self._write_migration("0001_broken.sql", "THIS IS NOT VALID SQL;\n")

        with self.assertRaises(sqlite3.Error):
            apply_migrations(self.connection, migrations_dir=self.migrations_dir)

        # The connection itself must not be left in a broken/half-open
        # transaction state - ordinary queries must still work.
        self.connection.execute("INSERT INTO widgets (name) VALUES ('fine')")
        self.connection.commit()
        count = self.connection.execute("SELECT COUNT(*) FROM widgets").fetchone()[0]
        self.assertEqual(count, 1)


class V010BackwardCompatibilityTests(unittest.TestCase):
    """Simulates upgrading a pre-existing v0.1.0 database in place.

    Builds a database using only database/schema.sql (the frozen v0.1.0
    baseline, no migrations applied), inserts data the way v0.1.0 code would
    have, then runs the same apply_migrations() an upgrade would run, and
    verifies every pre-existing row survives untouched plus the legacy
    backfill lands correctly.
    """

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = path
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(_V010_SCHEMA_SQL)
        self.connection.commit()

    def tearDown(self):
        self.connection.close()
        os.remove(self.db_path)

    def _insert_v010_data(self):
        conn = self.connection
        conn.execute("INSERT INTO sources (name) VALUES ('discord')")
        source_id = conn.execute(
            "SELECT id FROM sources WHERE name = 'discord'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO traders (source_id, name) VALUES (?, ?)",
            (source_id, "Matae"),
        )
        trader_id = conn.execute(
            "SELECT id FROM traders WHERE name = 'Matae'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO raw_messages (source_id, raw_text, content_hash) "
            "VALUES (?, ?, ?)",
            (source_id, "BOUGHT TSLA 7/24 312.5P $1.70 [B GRADE]", "deadbeef"),
        )
        raw_message_id = conn.execute(
            "SELECT id FROM raw_messages WHERE content_hash = 'deadbeef'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO trade_signals "
            "(raw_message_id, trader_id, symbol, action, price) "
            "VALUES (?, ?, 'TSLA', 'BOUGHT', '1.70')",
            (raw_message_id, trader_id),
        )
        conn.commit()
        return source_id, trader_id, raw_message_id

    def test_baseline_has_no_new_columns_before_migration(self):
        columns = _column_names(self.connection, "raw_messages")
        self.assertNotIn("channel_id", columns)

    def test_upgrade_preserves_existing_rows_exactly(self):
        source_id, trader_id, raw_message_id = self._insert_v010_data()

        apply_migrations(self.connection)

        trader_row = self.connection.execute(
            "SELECT name, canonical_name FROM traders WHERE id = ?", (trader_id,)
        ).fetchone()
        self.assertEqual(trader_row["name"], "Matae")
        self.assertEqual(trader_row["canonical_name"], "matae")

        raw_message_row = self.connection.execute(
            "SELECT raw_text, channel_id, import_batch_id, sequence_in_batch "
            "FROM raw_messages WHERE id = ?",
            (raw_message_id,),
        ).fetchone()
        self.assertEqual(
            raw_message_row["raw_text"], "BOUGHT TSLA 7/24 312.5P $1.70 [B GRADE]"
        )
        self.assertIsNone(raw_message_row["channel_id"])
        self.assertIsNone(raw_message_row["import_batch_id"])
        self.assertIsNone(raw_message_row["sequence_in_batch"])

        trade_signal_row = self.connection.execute(
            "SELECT symbol, action, price, strike, event_type, extraction_id "
            "FROM trade_signals WHERE raw_message_id = ?",
            (raw_message_id,),
        ).fetchone()
        self.assertEqual(trade_signal_row["symbol"], "TSLA")
        self.assertEqual(trade_signal_row["action"], "BOUGHT")
        self.assertEqual(trade_signal_row["price"], "1.70")
        self.assertIsNone(trade_signal_row["strike"])
        self.assertIsNone(trade_signal_row["event_type"])
        self.assertIsNone(trade_signal_row["extraction_id"])

    def test_upgrade_leaves_existing_trade_signal_lifecycle_id_null(self):
        # Recovery Milestone R6.1: trade_signals.lifecycle_id is added by
        # migrations/0007_trade_lifecycles.sql with no backfill of any kind
        # - every pre-existing v0.1.0 trade_signals row must come through
        # the upgrade with lifecycle_id NULL, exactly like every other
        # additive column this project has ever migrated in.
        _, _, raw_message_id = self._insert_v010_data()

        apply_migrations(self.connection)

        lifecycle_id = self.connection.execute(
            "SELECT lifecycle_id FROM trade_signals WHERE raw_message_id = ?",
            (raw_message_id,),
        ).fetchone()[0]
        self.assertIsNone(lifecycle_id)

    def test_upgrade_creates_empty_lifecycle_tables(self):
        self._insert_v010_data()

        apply_migrations(self.connection)

        trade_lifecycles_count = self.connection.execute(
            "SELECT COUNT(*) FROM trade_lifecycles"
        ).fetchone()[0]
        trade_lifecycle_events_count = self.connection.execute(
            "SELECT COUNT(*) FROM trade_lifecycle_events"
        ).fetchone()[0]
        self.assertEqual(trade_lifecycles_count, 0)
        self.assertEqual(trade_lifecycle_events_count, 0)

    def test_upgrade_backfills_legacy_extraction_per_raw_message(self):
        _, _, raw_message_id = self._insert_v010_data()

        apply_migrations(self.connection)

        extraction_row = self.connection.execute(
            "SELECT parser_version, parse_status, is_current "
            "FROM message_extractions WHERE raw_message_id = ?",
            (raw_message_id,),
        ).fetchone()
        self.assertIsNotNone(extraction_row)
        self.assertEqual(extraction_row["parser_version"], "legacy")
        self.assertEqual(extraction_row["parse_status"], "parsed")
        self.assertEqual(extraction_row["is_current"], 1)

    def test_upgrade_backfills_canonical_name_for_all_existing_traders(self):
        self.connection.execute("INSERT INTO sources (name) VALUES ('discord')")
        source_id = self.connection.execute(
            "SELECT id FROM sources WHERE name = 'discord'"
        ).fetchone()[0]
        self.connection.execute(
            "INSERT INTO traders (source_id, name) VALUES (?, ?)",
            (source_id, "  Bdorts  "),
        )
        self.connection.commit()

        apply_migrations(self.connection)

        row = self.connection.execute(
            "SELECT canonical_name FROM traders WHERE name = '  Bdorts  '"
        ).fetchone()
        self.assertEqual(row["canonical_name"], "bdorts")

    def test_upgrade_is_reentrant(self):
        self._insert_v010_data()

        apply_migrations(self.connection)
        apply_migrations(self.connection)

        counts = self.connection.execute(
            "SELECT filename, COUNT(*) FROM schema_migrations GROUP BY filename"
        ).fetchall()
        for _, count in counts:
            self.assertEqual(count, 1)

        extraction_count = self.connection.execute(
            "SELECT COUNT(*) FROM message_extractions"
        ).fetchone()[0]
        self.assertEqual(extraction_count, 1)

    def test_old_index_physically_exists_before_migration(self):
        # Sanity check that this fixture really reproduces a v0.1.0
        # database: the old source-wide index must exist before we upgrade
        # it, otherwise the transition tests below would be vacuous.
        index_names = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        self.assertIn("idx_raw_messages_source_external_id", index_names)

    def test_old_index_removed_after_upgrade(self):
        apply_migrations(self.connection)

        index_names = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        self.assertNotIn("idx_raw_messages_source_external_id", index_names)
        self.assertIn("idx_raw_messages_channel_external_id", index_names)
        self.assertIn("idx_raw_messages_null_channel_source_external_id", index_names)

    def test_same_external_id_allowed_across_different_channels_after_upgrade(self):
        apply_migrations(self.connection)

        self.connection.execute("INSERT INTO sources (name) VALUES ('discord')")
        source_id = self.connection.execute(
            "SELECT id FROM sources WHERE name = 'discord'"
        ).fetchone()[0]
        for external_channel_id in ("chan-1", "chan-2"):
            self.connection.execute(
                "INSERT INTO channels (source_id, external_channel_id) VALUES (?, ?)",
                (source_id, external_channel_id),
            )
        channel_1_id, channel_2_id = (
            row[0]
            for row in self.connection.execute(
                "SELECT id FROM channels WHERE external_channel_id IN ('chan-1', 'chan-2') "
                "ORDER BY external_channel_id"
            ).fetchall()
        )
        self.connection.execute(
            "INSERT INTO raw_messages "
            "(source_id, raw_text, content_hash, channel_id, external_id) "
            "VALUES (?, 'a', 'hash-a', ?, 'msg-1')",
            (source_id, channel_1_id),
        )
        self.connection.commit()

        # Same external_id, different channel: must now succeed - this is
        # the exact bug this fix corrects.
        self.connection.execute(
            "INSERT INTO raw_messages "
            "(source_id, raw_text, content_hash, channel_id, external_id) "
            "VALUES (?, 'b', 'hash-b', ?, 'msg-1')",
            (source_id, channel_2_id),
        )
        self.connection.commit()

        count = self.connection.execute(
            "SELECT COUNT(*) FROM raw_messages WHERE external_id = 'msg-1'"
        ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_same_external_id_blocked_within_same_channel_after_upgrade(self):
        apply_migrations(self.connection)

        self.connection.execute("INSERT INTO sources (name) VALUES ('discord')")
        source_id = self.connection.execute(
            "SELECT id FROM sources WHERE name = 'discord'"
        ).fetchone()[0]
        self.connection.execute(
            "INSERT INTO channels (source_id, external_channel_id) VALUES (?, 'chan-1')",
            (source_id,),
        )
        channel_id = self.connection.execute(
            "SELECT id FROM channels WHERE external_channel_id = 'chan-1'"
        ).fetchone()[0]
        self.connection.execute(
            "INSERT INTO raw_messages "
            "(source_id, raw_text, content_hash, channel_id, external_id) "
            "VALUES (?, 'a', 'hash-a', ?, 'msg-1')",
            (source_id, channel_id),
        )
        self.connection.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO raw_messages "
                "(source_id, raw_text, content_hash, channel_id, external_id) "
                "VALUES (?, 'b', 'hash-b', ?, 'msg-1')",
                (source_id, channel_id),
            )

    def test_legacy_null_channel_rows_still_blocked_after_upgrade(self):
        apply_migrations(self.connection)

        self.connection.execute("INSERT INTO sources (name) VALUES ('discord')")
        source_id = self.connection.execute(
            "SELECT id FROM sources WHERE name = 'discord'"
        ).fetchone()[0]
        self.connection.execute(
            "INSERT INTO raw_messages (source_id, raw_text, content_hash, external_id) "
            "VALUES (?, 'a', 'hash-a', 'msg-legacy')",
            (source_id,),
        )
        self.connection.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO raw_messages "
                "(source_id, raw_text, content_hash, external_id) "
                "VALUES (?, 'b', 'hash-b', 'msg-legacy')",
                (source_id,),
            )

    def test_reinitialize_database_does_not_resurrect_old_index(self):
        # Regression guard: schema.sql is re-applied on every
        # initialize_database() call. Before this fix, its own
        # CREATE UNIQUE INDEX IF NOT EXISTS statement for the old index
        # would silently recreate it right after this migration dropped it.
        apply_migrations(self.connection)
        self.connection.close()

        config = DatabaseConfig(db_path=self.db_path)
        initialize_database(config)
        initialize_database(config)

        connection = get_connection(config)
        try:
            index_names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                ).fetchall()
            }
        finally:
            connection.close()
        self.assertNotIn("idx_raw_messages_source_external_id", index_names)


if __name__ == "__main__":
    unittest.main()
