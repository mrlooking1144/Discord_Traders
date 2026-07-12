"""Tests for repository access.

Covers Milestone 2B.5a (sources), 2B.5b (traders), and 2B.5c
(raw_messages).
"""

import hashlib
import inspect
import os
import sqlite3
import tempfile
import unittest

from database.config import DatabaseConfig
from database.db import get_connection, initialize_database
from database.models import RawMessage, Source, Trader
from database import repository
from database.repository import (
    create_raw_message,
    create_trader,
    get_or_create_source,
    get_raw_message_by_external_id,
    get_raw_messages_by_content_hash,
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


class RawMessagesRepositoryTests(unittest.TestCase):
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

    def test_create_raw_message_with_required_fields_only(self):
        message = create_raw_message(self.connection, self.source_id, "hello world")
        self.connection.commit()

        self.assertIsInstance(message, RawMessage)
        self.assertEqual(message.source_id, self.source_id)
        self.assertEqual(message.raw_text, "hello world")
        self.assertIsNone(message.external_id)
        self.assertIsNone(message.metadata)
        self.assertIsNone(message.received_at)

    def test_create_raw_message_with_all_optional_fields(self):
        message = create_raw_message(
            self.connection,
            self.source_id,
            "hello world",
            external_id="ext-1",
            metadata={"channel_id": "123", "guild_id": "456"},
            received_at="2026-07-01T00:00:00",
        )
        self.connection.commit()

        self.assertEqual(message.external_id, "ext-1")
        self.assertEqual(message.metadata, {"channel_id": "123", "guild_id": "456"})
        self.assertEqual(message.received_at, "2026-07-01T00:00:00")

    def test_create_raw_message_returns_generated_id_and_ingested_at(self):
        message = create_raw_message(self.connection, self.source_id, "hello world")
        self.connection.commit()

        self.assertIsNotNone(message.id)
        self.assertIsNotNone(message.ingested_at)

    def test_multiline_raw_text_preserved_exactly(self):
        raw_text = "line one\nline two\r\nline three\n\ntrailing blank line\n"
        message = create_raw_message(self.connection, self.source_id, raw_text)
        self.connection.commit()

        self.assertEqual(message.raw_text, raw_text)

        row = self.connection.execute(
            "SELECT raw_text FROM raw_messages WHERE id = ?", (message.id,)
        ).fetchone()
        self.assertEqual(row["raw_text"], raw_text)

    def test_non_ascii_raw_text_preserved_exactly(self):
        raw_text = "Bought $NVDA calls \U0001F680 café éèê résumé"
        message = create_raw_message(self.connection, self.source_id, raw_text)
        self.connection.commit()

        self.assertEqual(message.raw_text, raw_text)

    def test_content_hash_matches_sha256_of_utf8_text(self):
        raw_text = "STO $SPY 500p 12/20 café"
        message = create_raw_message(self.connection, self.source_id, raw_text)
        self.connection.commit()

        expected_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        self.assertEqual(message.content_hash, expected_hash)

    def test_metadata_dict_round_trip(self):
        metadata = {"channel_id": "123", "message_link": "https://example.test/1"}
        created = create_raw_message(
            self.connection, self.source_id, "hello", metadata=metadata
        )
        self.connection.commit()

        found = get_raw_messages_by_content_hash(
            self.connection, created.content_hash
        )[0]

        self.assertEqual(found.metadata, metadata)

    def test_metadata_none_round_trip(self):
        created = create_raw_message(self.connection, self.source_id, "hello")
        self.connection.commit()

        found = get_raw_messages_by_content_hash(
            self.connection, created.content_hash
        )[0]

        self.assertIsNone(found.metadata)

    def test_get_raw_message_by_external_id_returns_correct_message(self):
        created = create_raw_message(
            self.connection, self.source_id, "hello", external_id="ext-2"
        )
        self.connection.commit()

        found = get_raw_message_by_external_id(self.connection, self.source_id, "ext-2")

        self.assertEqual(found, created)

    def test_get_raw_message_by_external_id_scoped_to_source(self):
        other_source_id = get_or_create_source(self.connection, "telegram").id
        create_raw_message(
            self.connection, other_source_id, "hello", external_id="ext-3"
        )
        self.connection.commit()

        result = get_raw_message_by_external_id(self.connection, self.source_id, "ext-3")

        self.assertIsNone(result)

    def test_get_raw_message_by_missing_external_id_returns_none(self):
        result = get_raw_message_by_external_id(self.connection, self.source_id, "nope")

        self.assertIsNone(result)

    def test_duplicate_external_id_within_source_raises_integrity_error(self):
        create_raw_message(
            self.connection, self.source_id, "hello", external_id="ext-4"
        )
        self.connection.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            create_raw_message(
                self.connection, self.source_id, "goodbye", external_id="ext-4"
            )

    def test_same_external_id_allowed_across_sources(self):
        other_source_id = get_or_create_source(self.connection, "telegram").id
        self.connection.commit()

        first = create_raw_message(
            self.connection, self.source_id, "hello", external_id="ext-5"
        )
        second = create_raw_message(
            self.connection, other_source_id, "hello", external_id="ext-5"
        )
        self.connection.commit()

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(first.external_id, second.external_id)

    def test_multiple_messages_with_null_external_id_allowed(self):
        first = create_raw_message(self.connection, self.source_id, "hello")
        second = create_raw_message(self.connection, self.source_id, "hello again")
        self.connection.commit()

        self.assertIsNone(first.external_id)
        self.assertIsNone(second.external_id)
        self.assertNotEqual(first.id, second.id)

    def test_hash_lookup_returns_all_matching_messages(self):
        raw_text = "duplicate text"
        first = create_raw_message(
            self.connection, self.source_id, raw_text, external_id="dup-1"
        )
        second = create_raw_message(
            self.connection, self.source_id, raw_text, external_id="dup-2"
        )
        self.connection.commit()

        results = get_raw_messages_by_content_hash(self.connection, first.content_hash)

        self.assertEqual([m.id for m in results], [first.id, second.id])

    def test_hash_lookup_missing_hash_returns_empty_list(self):
        results = get_raw_messages_by_content_hash(self.connection, "no-such-hash")

        self.assertEqual(results, [])

    def test_hash_lookup_ordered_by_id_ascending(self):
        raw_text = "ordered duplicate text"
        third = create_raw_message(
            self.connection, self.source_id, raw_text, external_id="ord-3"
        )
        first_id = third.id
        second = create_raw_message(
            self.connection, self.source_id, raw_text, external_id="ord-4"
        )
        self.connection.commit()

        results = get_raw_messages_by_content_hash(self.connection, third.content_hash)

        self.assertEqual([m.id for m in results], sorted(m.id for m in results))
        self.assertEqual([m.id for m in results], [first_id, second.id])

    def test_identical_raw_text_with_different_external_ids_insertable(self):
        raw_text = "same text, different message"
        first = create_raw_message(
            self.connection, self.source_id, raw_text, external_id="same-1"
        )
        second = create_raw_message(
            self.connection, self.source_id, raw_text, external_id="same-2"
        )
        self.connection.commit()

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(first.content_hash, second.content_hash)

    def test_create_raw_message_not_committed_automatically(self):
        other_connection = get_connection(self.config)
        try:
            create_raw_message(
                self.connection, self.source_id, "hello", external_id="ext-6"
            )

            row = other_connection.execute(
                "SELECT * FROM raw_messages WHERE external_id = ?", ("ext-6",)
            ).fetchone()
            self.assertIsNone(row)

            self.connection.commit()

            row = other_connection.execute(
                "SELECT * FROM raw_messages WHERE external_id = ?", ("ext-6",)
            ).fetchone()
            self.assertIsNotNone(row)
        finally:
            other_connection.close()

    def test_no_raw_message_update_function_exists(self):
        public_names = [
            name
            for name in dir(repository)
            if not name.startswith("_") and callable(getattr(repository, name))
        ]
        self.assertFalse(
            any("update" in name.lower() for name in public_names),
            "No raw-message (or other) update function should exist yet.",
        )

        source = inspect.getsource(repository)
        self.assertNotIn("UPDATE raw_messages", source)


if __name__ == "__main__":
    unittest.main()
