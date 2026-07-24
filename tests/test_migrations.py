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
        self.assertNotIn(
            "lifecycle_id",
            trade_signal_columns,
            "lifecycle_id belongs to a later milestone (R6, alongside trade_lifecycles)",
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
        self.assertGreaterEqual(len(expected), 6)

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
