"""Tests for repository access (Milestones 2B.5a sources, 2B.5b traders)."""

import os
import sqlite3
import tempfile
import unittest

from database.config import DatabaseConfig
from database.db import get_connection, initialize_database
from database.models import Source, Trader
from database.repository import (
    create_trader,
    get_or_create_source,
    get_source_by_name,
    get_trader_by_external_id,
    get_traders_by_name,
)


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


class TradersRepositoryTests(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = path
        self.config = DatabaseConfig(db_path=path)
        initialize_database(self.config)
        self.connection = get_connection(self.config)

        self.source_id = get_or_create_source(self.connection, "discord").id
        self.connection.commit()

    def tearDown(self):
        self.connection.close()
        os.remove(self.db_path)

    def test_create_trader_with_external_id(self):
        trader = create_trader(
            self.connection, self.source_id, "alice", external_trader_id="ext-1"
        )
        self.connection.commit()

        self.assertEqual(trader.source_id, self.source_id)
        self.assertEqual(trader.name, "alice")
        self.assertEqual(trader.external_trader_id, "ext-1")

    def test_create_trader_without_external_id(self):
        trader = create_trader(self.connection, self.source_id, "bob")
        self.connection.commit()

        self.assertIsNone(trader.external_trader_id)

    def test_create_trader_returns_generated_id_and_created_at(self):
        trader = create_trader(self.connection, self.source_id, "carol")
        self.connection.commit()

        self.assertIsInstance(trader, Trader)
        self.assertIsNotNone(trader.id)
        self.assertIsNotNone(trader.created_at)

    def test_get_trader_by_external_id_returns_correct_trader(self):
        created = create_trader(
            self.connection, self.source_id, "dave", external_trader_id="ext-2"
        )
        self.connection.commit()

        found = get_trader_by_external_id(self.connection, self.source_id, "ext-2")

        self.assertEqual(found, created)

    def test_get_trader_by_external_id_scoped_to_source(self):
        other_source_id = get_or_create_source(self.connection, "telegram").id
        create_trader(
            self.connection, other_source_id, "erin", external_trader_id="ext-3"
        )
        self.connection.commit()

        result = get_trader_by_external_id(self.connection, self.source_id, "ext-3")

        self.assertIsNone(result)

    def test_get_trader_by_missing_external_id_returns_none(self):
        result = get_trader_by_external_id(self.connection, self.source_id, "nope")

        self.assertIsNone(result)

    def test_get_traders_by_name_returns_matches(self):
        created = create_trader(self.connection, self.source_id, "frank")
        self.connection.commit()

        results = get_traders_by_name(self.connection, self.source_id, "frank")

        self.assertEqual(results, [created])

    def test_get_traders_by_name_allows_duplicates(self):
        first = create_trader(self.connection, self.source_id, "grace")
        second = create_trader(self.connection, self.source_id, "grace")
        self.connection.commit()

        results = get_traders_by_name(self.connection, self.source_id, "grace")

        self.assertEqual([t.id for t in results], [first.id, second.id])

    def test_get_traders_by_name_scoped_to_source(self):
        other_source_id = get_or_create_source(self.connection, "telegram").id
        create_trader(self.connection, other_source_id, "heidi")
        self.connection.commit()

        results = get_traders_by_name(self.connection, self.source_id, "heidi")

        self.assertEqual(results, [])

    def test_get_traders_by_missing_name_returns_empty_list(self):
        results = get_traders_by_name(self.connection, self.source_id, "nobody")

        self.assertEqual(results, [])

    def test_duplicate_external_id_within_source_raises_integrity_error(self):
        create_trader(
            self.connection, self.source_id, "ivan", external_trader_id="ext-4"
        )
        self.connection.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            create_trader(
                self.connection, self.source_id, "ivan2", external_trader_id="ext-4"
            )

    def test_same_external_id_allowed_across_sources(self):
        other_source_id = get_or_create_source(self.connection, "telegram").id
        self.connection.commit()

        first = create_trader(
            self.connection, self.source_id, "judy", external_trader_id="ext-5"
        )
        second = create_trader(
            self.connection, other_source_id, "judy", external_trader_id="ext-5"
        )
        self.connection.commit()

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(first.external_trader_id, second.external_trader_id)

    def test_multiple_traders_with_null_external_id_allowed(self):
        first = create_trader(self.connection, self.source_id, "kim")
        second = create_trader(self.connection, self.source_id, "kim2")
        self.connection.commit()

        self.assertIsNone(first.external_trader_id)
        self.assertIsNone(second.external_trader_id)
        self.assertNotEqual(first.id, second.id)

    def test_create_trader_rejects_empty_name(self):
        with self.assertRaises(ValueError):
            create_trader(self.connection, self.source_id, "")

    def test_create_trader_rejects_whitespace_only_name(self):
        with self.assertRaises(ValueError):
            create_trader(self.connection, self.source_id, "   ")

    def test_create_trader_not_committed_automatically(self):
        other_connection = get_connection(self.config)
        try:
            create_trader(self.connection, self.source_id, "liam")

            row = other_connection.execute(
                "SELECT * FROM traders WHERE name = ?", ("liam",)
            ).fetchone()
            self.assertIsNone(row)

            self.connection.commit()

            row = other_connection.execute(
                "SELECT * FROM traders WHERE name = ?", ("liam",)
            ).fetchone()
            self.assertIsNotNone(row)
        finally:
            other_connection.close()


if __name__ == "__main__":
    unittest.main()
