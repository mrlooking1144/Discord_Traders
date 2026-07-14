"""Tests for database/db.py.

Covers Milestone 2D.1: the parent-directory creation initialize_database()
performs immediately before opening its connection. Uses only
tempfile.TemporaryDirectory - no real LOCALAPPDATA or home directory is
ever touched. Kept separate from tests/test_config.py, which covers
path-resolution logic only.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from database.config import DatabaseConfig
from database.db import initialize_database


class InitializeDatabaseParentDirectoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)

    def test_creates_missing_single_level_parent(self):
        db_path = self.tmp_dir / "missing_dir" / "discord_traders.db"
        self.assertFalse(db_path.parent.exists())

        initialize_database(DatabaseConfig(db_path=str(db_path)))

        self.assertTrue(db_path.parent.is_dir())
        self.assertTrue(db_path.exists())

    def test_creates_missing_nested_parents(self):
        db_path = self.tmp_dir / "a" / "b" / "c" / "discord_traders.db"
        self.assertFalse(db_path.parent.exists())

        initialize_database(DatabaseConfig(db_path=str(db_path)))

        self.assertTrue(db_path.parent.is_dir())
        self.assertTrue(db_path.exists())

    def test_existing_parent_does_not_fail(self):
        db_path = self.tmp_dir / "discord_traders.db"
        self.assertTrue(db_path.parent.exists())

        initialize_database(DatabaseConfig(db_path=str(db_path)))

        self.assertTrue(db_path.exists())

    def test_schema_initialization_still_succeeds_after_parent_creation(self):
        db_path = self.tmp_dir / "nested" / "discord_traders.db"

        initialize_database(DatabaseConfig(db_path=str(db_path)))

        connection = sqlite3.connect(str(db_path))
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        finally:
            connection.close()

        self.assertTrue(
            {
                "sources",
                "traders",
                "raw_messages",
                "trade_signals",
                "trade_signal_edits",
            }.issubset(tables)
        )


if __name__ == "__main__":
    unittest.main()
