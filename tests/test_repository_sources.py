"""Tests for source-related repository access (Milestone 2B.5a)."""

import os
import tempfile
import unittest

from database.config import DatabaseConfig
from database.db import get_connection, initialize_database
from database.models import Source
from database.repository import get_or_create_source, get_source_by_name


class SourcesRepositoryTests(unittest.TestCase):
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

    def test_create_new_source(self):
        source = get_or_create_source(self.connection, "discord")
        self.connection.commit()

        self.assertIsInstance(source, Source)
        self.assertEqual(source.name, "discord")
        self.assertIsNotNone(source.id)

    def test_get_existing_source_by_name(self):
        created = get_or_create_source(self.connection, "telegram")
        self.connection.commit()

        found = get_source_by_name(self.connection, "telegram")

        self.assertEqual(found, created)

    def test_get_missing_source_returns_none(self):
        result = get_source_by_name(self.connection, "nonexistent")

        self.assertIsNone(result)

    def test_get_source_by_name_rejects_empty_name(self):
        with self.assertRaises(ValueError):
            get_source_by_name(self.connection, "")

    def test_get_source_by_name_rejects_whitespace_only_name(self):
        with self.assertRaises(ValueError):
            get_source_by_name(self.connection, "   ")

    def test_get_or_create_is_idempotent(self):
        first = get_or_create_source(self.connection, "csv")
        self.connection.commit()
        second = get_or_create_source(self.connection, "csv")
        self.connection.commit()

        self.assertEqual(first.id, second.id)

        count = self.connection.execute(
            "SELECT COUNT(*) FROM sources WHERE name = ?", ("csv",)
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_rejects_empty_name(self):
        with self.assertRaises(ValueError):
            get_or_create_source(self.connection, "")

    def test_rejects_whitespace_only_name(self):
        with self.assertRaises(ValueError):
            get_or_create_source(self.connection, "   ")

    def test_invalid_input_creates_no_row(self):
        with self.assertRaises(ValueError):
            get_or_create_source(self.connection, "")
        with self.assertRaises(ValueError):
            get_or_create_source(self.connection, "   ")

        count = self.connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        self.assertEqual(count, 0)

    def test_valid_name_is_preserved_exactly(self):
        source = get_or_create_source(self.connection, "  discord  ")
        self.connection.commit()

        self.assertEqual(source.name, "  discord  ")

    def test_no_autocommit_before_caller_commits(self):
        other_connection = get_connection(self.config)
        try:
            get_or_create_source(self.connection, "email")

            row = other_connection.execute(
                "SELECT * FROM sources WHERE name = ?", ("email",)
            ).fetchone()
            self.assertIsNone(row)

            self.connection.commit()

            row = other_connection.execute(
                "SELECT * FROM sources WHERE name = ?", ("email",)
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["name"], "email")
        finally:
            other_connection.close()


if __name__ == "__main__":
    unittest.main()
