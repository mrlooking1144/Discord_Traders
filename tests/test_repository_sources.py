"""Tests for repository access.

Covers Milestone 2B.5a (sources), 2B.5b (traders), 2B.5c (raw_messages),
2B.5d (trade_signals), 2B.5e (trade_signal_edits), and the
get_trade_signals_matching lookup added in 2B.6a.
"""

import hashlib
import inspect
import json
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal

from database.config import DatabaseConfig
from database.db import get_connection, initialize_database
from database.models import RawMessage, Source, Trader, TradeSignal, TradeSignalEdit
from database import repository
from database.repository import (
    create_raw_message,
    create_trade_signal,
    create_trade_signal_edit,
    create_trader,
    get_or_create_source,
    get_raw_message_by_external_id,
    get_raw_messages_by_content_hash,
    get_source_by_name,
    get_trade_signal_by_id,
    get_trade_signal_edits,
    get_trade_signals_matching,
    get_trader_by_external_id,
    get_traders_by_name,
    update_trade_signal,
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
            any("raw_message" in name.lower() and "update" in name.lower() for name in public_names),
            "No raw-message update function should exist; raw_text is write-once.",
        )

        source = inspect.getsource(repository)
        self.assertNotIn("UPDATE raw_messages", source)


class TradeSignalsRepositoryTests(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = path
        self.config = DatabaseConfig(db_path=path)
        initialize_database(self.config)
        self.connection = get_connection(self.config)

        source_id = get_or_create_source(self.connection, "discord").id
        trader = create_trader(self.connection, source_id, "alice")
        raw_message = create_raw_message(self.connection, source_id, "BTO SPY 500c")
        self.connection.commit()

        self.source_id = source_id
        self.trader_id = trader.id
        self.raw_message_id = raw_message.id

    def tearDown(self):
        self.connection.close()
        os.remove(self.db_path)

    def test_create_trade_signal_required_only(self):
        signal = create_trade_signal(
            self.connection, self.raw_message_id, self.trader_id, "SPY", "BTO"
        )
        self.connection.commit()

        self.assertIsInstance(signal, TradeSignal)
        self.assertEqual(signal.raw_message_id, self.raw_message_id)
        self.assertEqual(signal.trader_id, self.trader_id)
        self.assertEqual(signal.symbol, "SPY")
        self.assertEqual(signal.action, "BTO")
        self.assertIsNone(signal.option_type)
        self.assertIsNone(signal.price)
        self.assertIsNone(signal.expiration)
        self.assertIsNone(signal.position_size)

    def test_create_trade_signal_full_fields(self):
        signal = create_trade_signal(
            self.connection,
            self.raw_message_id,
            self.trader_id,
            "SPY",
            "BTO",
            option_type="call",
            price=Decimal("3.25"),
            expiration="2026-12-18",
            position_size="half position",
        )
        self.connection.commit()

        self.assertEqual(signal.option_type, "call")
        self.assertEqual(signal.price, "3.25")
        self.assertEqual(signal.expiration, "2026-12-18")
        self.assertEqual(signal.position_size, "half position")

    def test_create_trade_signal_returns_generated_id_created_at_updated_at(self):
        signal = create_trade_signal(
            self.connection, self.raw_message_id, self.trader_id, "SPY", "BTO"
        )
        self.connection.commit()

        self.assertIsNotNone(signal.id)
        self.assertIsNotNone(signal.created_at)
        self.assertIsNotNone(signal.updated_at)

    def test_required_fields_round_trip(self):
        created = create_trade_signal(
            self.connection, self.raw_message_id, self.trader_id, "SPY", "BTO"
        )
        self.connection.commit()

        found = get_trade_signal_by_id(self.connection, created.id)

        self.assertEqual(found.raw_message_id, self.raw_message_id)
        self.assertEqual(found.trader_id, self.trader_id)
        self.assertEqual(found.symbol, "SPY")
        self.assertEqual(found.action, "BTO")

    def test_optional_none_fields_round_trip(self):
        created = create_trade_signal(
            self.connection, self.raw_message_id, self.trader_id, "SPY", "BTO"
        )
        self.connection.commit()

        found = get_trade_signal_by_id(self.connection, created.id)

        self.assertIsNone(found.option_type)
        self.assertIsNone(found.price)
        self.assertIsNone(found.expiration)
        self.assertIsNone(found.position_size)

    def test_full_optional_fields_round_trip(self):
        created = create_trade_signal(
            self.connection,
            self.raw_message_id,
            self.trader_id,
            "SPY",
            "BTO",
            option_type="put",
            price=Decimal("0.85"),
            expiration="2026-01-16",
            position_size="10 contracts",
        )
        self.connection.commit()

        found = get_trade_signal_by_id(self.connection, created.id)

        self.assertEqual(found.option_type, "put")
        self.assertEqual(found.price, "0.85")
        self.assertEqual(found.expiration, "2026-01-16")
        self.assertEqual(found.position_size, "10 contracts")

    def test_decimal_price_stored_and_returned_exactly(self):
        signal = create_trade_signal(
            self.connection,
            self.raw_message_id,
            self.trader_id,
            "SPY",
            "BTO",
            price=Decimal("3.25"),
        )
        self.connection.commit()

        self.assertEqual(signal.price, "3.25")
        self.assertIsInstance(signal.price, str)

    def test_float_price_rejected(self):
        with self.assertRaises(TypeError):
            create_trade_signal(
                self.connection,
                self.raw_message_id,
                self.trader_id,
                "SPY",
                "BTO",
                price=3.25,
            )

    def test_string_price_rejected(self):
        with self.assertRaises(TypeError):
            create_trade_signal(
                self.connection,
                self.raw_message_id,
                self.trader_id,
                "SPY",
                "BTO",
                price="3.25",
            )

    def test_int_price_rejected(self):
        with self.assertRaises(TypeError):
            create_trade_signal(
                self.connection,
                self.raw_message_id,
                self.trader_id,
                "SPY",
                "BTO",
                price=3,
            )

    def test_missing_raw_message_id_rejected(self):
        with self.assertRaises(ValueError):
            create_trade_signal(self.connection, None, self.trader_id, "SPY", "BTO")

    def test_missing_trader_id_rejected(self):
        with self.assertRaises(ValueError):
            create_trade_signal(
                self.connection, self.raw_message_id, None, "SPY", "BTO"
            )

    def test_empty_symbol_rejected(self):
        with self.assertRaises(ValueError):
            create_trade_signal(
                self.connection, self.raw_message_id, self.trader_id, "", "BTO"
            )

    def test_whitespace_only_symbol_rejected(self):
        with self.assertRaises(ValueError):
            create_trade_signal(
                self.connection, self.raw_message_id, self.trader_id, "   ", "BTO"
            )

    def test_empty_action_rejected(self):
        with self.assertRaises(ValueError):
            create_trade_signal(
                self.connection, self.raw_message_id, self.trader_id, "SPY", ""
            )

    def test_whitespace_only_action_rejected(self):
        with self.assertRaises(ValueError):
            create_trade_signal(
                self.connection, self.raw_message_id, self.trader_id, "SPY", "   "
            )

    def test_foreign_key_failure_propagates(self):
        with self.assertRaises(sqlite3.IntegrityError):
            create_trade_signal(self.connection, 999999, self.trader_id, "SPY", "BTO")

    def test_lookup_by_existing_id_returns_row(self):
        created = create_trade_signal(
            self.connection, self.raw_message_id, self.trader_id, "SPY", "BTO"
        )
        self.connection.commit()

        found = get_trade_signal_by_id(self.connection, created.id)

        self.assertEqual(found, created)

    def test_lookup_by_missing_id_returns_none(self):
        result = get_trade_signal_by_id(self.connection, 999999)

        self.assertIsNone(result)

    def test_partial_update_changes_only_supplied_fields(self):
        created = create_trade_signal(
            self.connection,
            self.raw_message_id,
            self.trader_id,
            "SPY",
            "BTO",
            option_type="call",
            price=Decimal("3.25"),
        )
        self.connection.commit()

        updated = update_trade_signal(self.connection, created.id, symbol="QQQ")
        self.connection.commit()

        self.assertEqual(updated.symbol, "QQQ")
        self.assertEqual(updated.action, "BTO")
        self.assertEqual(updated.option_type, "call")
        self.assertEqual(updated.price, "3.25")

    def test_update_changes_updated_at(self):
        created = create_trade_signal(
            self.connection, self.raw_message_id, self.trader_id, "SPY", "BTO"
        )
        self.connection.commit()

        # Deterministically age the row instead of sleeping across a
        # CURRENT_TIMESTAMP tick.
        self.connection.execute(
            "UPDATE trade_signals SET updated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00", created.id),
        )
        self.connection.commit()

        updated = update_trade_signal(self.connection, created.id, symbol="QQQ")
        self.connection.commit()

        self.assertNotEqual(updated.updated_at, "2000-01-01T00:00:00")

    def test_optional_fields_can_be_updated_to_none(self):
        created = create_trade_signal(
            self.connection,
            self.raw_message_id,
            self.trader_id,
            "SPY",
            "BTO",
            option_type="call",
            price=Decimal("3.25"),
        )
        self.connection.commit()

        updated = update_trade_signal(
            self.connection, created.id, option_type=None, price=None
        )
        self.connection.commit()

        self.assertIsNone(updated.option_type)
        self.assertIsNone(updated.price)

    def test_update_rejects_none_raw_message_id(self):
        created = create_trade_signal(
            self.connection, self.raw_message_id, self.trader_id, "SPY", "BTO"
        )
        self.connection.commit()

        with self.assertRaises(ValueError):
            update_trade_signal(self.connection, created.id, raw_message_id=None)

    def test_update_rejects_none_trader_id(self):
        created = create_trade_signal(
            self.connection, self.raw_message_id, self.trader_id, "SPY", "BTO"
        )
        self.connection.commit()

        with self.assertRaises(ValueError):
            update_trade_signal(self.connection, created.id, trader_id=None)

    def test_update_rejects_whitespace_only_symbol(self):
        created = create_trade_signal(
            self.connection, self.raw_message_id, self.trader_id, "SPY", "BTO"
        )
        self.connection.commit()

        with self.assertRaises(ValueError):
            update_trade_signal(self.connection, created.id, symbol="   ")

    def test_update_rejects_whitespace_only_action(self):
        created = create_trade_signal(
            self.connection, self.raw_message_id, self.trader_id, "SPY", "BTO"
        )
        self.connection.commit()

        with self.assertRaises(ValueError):
            update_trade_signal(self.connection, created.id, action="   ")

    def test_update_rejects_float_price(self):
        created = create_trade_signal(
            self.connection, self.raw_message_id, self.trader_id, "SPY", "BTO"
        )
        self.connection.commit()

        with self.assertRaises(TypeError):
            update_trade_signal(self.connection, created.id, price=3.25)

    def test_update_rejects_string_price(self):
        created = create_trade_signal(
            self.connection, self.raw_message_id, self.trader_id, "SPY", "BTO"
        )
        self.connection.commit()

        with self.assertRaises(TypeError):
            update_trade_signal(self.connection, created.id, price="3.25")

    def test_unknown_update_field_rejected(self):
        created = create_trade_signal(
            self.connection, self.raw_message_id, self.trader_id, "SPY", "BTO"
        )
        self.connection.commit()

        with self.assertRaises(ValueError):
            update_trade_signal(self.connection, created.id, not_a_real_field="x")

    def test_protected_fields_cannot_be_updated(self):
        created = create_trade_signal(
            self.connection, self.raw_message_id, self.trader_id, "SPY", "BTO"
        )
        self.connection.commit()

        with self.assertRaises(ValueError):
            update_trade_signal(self.connection, created.id, id=999)
        with self.assertRaises(ValueError):
            update_trade_signal(
                self.connection, created.id, created_at="2000-01-01T00:00:00"
            )
        with self.assertRaises(ValueError):
            update_trade_signal(
                self.connection, created.id, updated_at="2000-01-01T00:00:00"
            )

    def test_empty_update_rejected(self):
        created = create_trade_signal(
            self.connection, self.raw_message_id, self.trader_id, "SPY", "BTO"
        )
        self.connection.commit()

        with self.assertRaises(ValueError):
            update_trade_signal(self.connection, created.id)

    def test_missing_row_update_returns_none(self):
        result = update_trade_signal(self.connection, 999999, symbol="QQQ")

        self.assertIsNone(result)

    def test_create_trade_signal_not_committed_automatically(self):
        other_connection = get_connection(self.config)
        try:
            created = create_trade_signal(
                self.connection, self.raw_message_id, self.trader_id, "SPY", "BTO"
            )

            row = other_connection.execute(
                "SELECT * FROM trade_signals WHERE id = ?", (created.id,)
            ).fetchone()
            self.assertIsNone(row)

            self.connection.commit()

            row = other_connection.execute(
                "SELECT * FROM trade_signals WHERE id = ?", (created.id,)
            ).fetchone()
            self.assertIsNotNone(row)
        finally:
            other_connection.close()

    def test_update_trade_signal_not_committed_automatically(self):
        created = create_trade_signal(
            self.connection, self.raw_message_id, self.trader_id, "SPY", "BTO"
        )
        self.connection.commit()

        other_connection = get_connection(self.config)
        try:
            update_trade_signal(self.connection, created.id, symbol="QQQ")

            row = other_connection.execute(
                "SELECT symbol FROM trade_signals WHERE id = ?", (created.id,)
            ).fetchone()
            self.assertEqual(row["symbol"], "SPY")

            self.connection.commit()

            row = other_connection.execute(
                "SELECT symbol FROM trade_signals WHERE id = ?", (created.id,)
            ).fetchone()
            self.assertEqual(row["symbol"], "QQQ")
        finally:
            other_connection.close()

    def test_no_trade_signal_edits_row_created(self):
        created = create_trade_signal(
            self.connection, self.raw_message_id, self.trader_id, "SPY", "BTO"
        )
        self.connection.commit()

        update_trade_signal(self.connection, created.id, symbol="QQQ")
        self.connection.commit()

        count = self.connection.execute(
            "SELECT COUNT(*) FROM trade_signal_edits"
        ).fetchone()[0]
        self.assertEqual(count, 0)

        update_source = inspect.getsource(repository.update_trade_signal)
        self.assertNotIn("INSERT INTO trade_signal_edits", update_source)
        self.assertNotIn("create_trade_signal_edit", update_source)


class TradeSignalEditsRepositoryTests(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = path
        self.config = DatabaseConfig(db_path=path)
        initialize_database(self.config)
        self.connection = get_connection(self.config)

        source_id = get_or_create_source(self.connection, "discord").id
        trader = create_trader(self.connection, source_id, "alice")
        raw_message = create_raw_message(self.connection, source_id, "BTO SPY 500c")
        signal = create_trade_signal(
            self.connection, raw_message.id, trader.id, "SPY", "BTO"
        )
        self.connection.commit()

        self.source_id = source_id
        self.trader_id = trader.id
        self.raw_message_id = raw_message.id
        self.trade_signal_id = signal.id

    def tearDown(self):
        self.connection.close()
        os.remove(self.db_path)

    def test_create_trade_signal_edit_from_dict(self):
        edit = create_trade_signal_edit(
            self.connection, self.trade_signal_id, {"symbol": "SPY", "action": "BTO"}
        )
        self.connection.commit()

        self.assertIsInstance(edit, TradeSignalEdit)
        self.assertEqual(edit.trade_signal_id, self.trade_signal_id)
        self.assertEqual(edit.previous_values, '{"action": "BTO", "symbol": "SPY"}')

    def test_create_trade_signal_edit_returns_generated_id_and_edited_at(self):
        edit = create_trade_signal_edit(
            self.connection, self.trade_signal_id, {"symbol": "SPY"}
        )
        self.connection.commit()

        self.assertIsNotNone(edit.id)
        self.assertIsNotNone(edit.edited_at)

    def test_serialization_is_deterministic_with_sorted_keys(self):
        edit_a = create_trade_signal_edit(
            self.connection,
            self.trade_signal_id,
            {"symbol": "SPY", "action": "BTO", "price": "3.25"},
        )
        edit_b = create_trade_signal_edit(
            self.connection,
            self.trade_signal_id,
            {"price": "3.25", "action": "BTO", "symbol": "SPY"},
        )
        self.connection.commit()

        self.assertEqual(edit_a.previous_values, edit_b.previous_values)
        self.assertEqual(
            edit_a.previous_values,
            '{"action": "BTO", "price": "3.25", "symbol": "SPY"}',
        )

    def test_nested_dicts_and_lists_round_trip(self):
        previous_values = {
            "symbol": "SPY",
            "legs": [{"strike": 500, "type": "call"}, {"strike": 505, "type": "call"}],
            "meta": {"source": "discord", "tags": ["swing", "options"]},
        }

        edit = create_trade_signal_edit(
            self.connection, self.trade_signal_id, previous_values
        )
        self.connection.commit()

        found = get_trade_signal_edits(self.connection, self.trade_signal_id)[0]

        self.assertEqual(json.loads(found.previous_values), previous_values)
        self.assertEqual(found.id, edit.id)

    def test_empty_dict_raises_value_error(self):
        with self.assertRaises(ValueError):
            create_trade_signal_edit(self.connection, self.trade_signal_id, {})

    def test_none_previous_values_rejected(self):
        with self.assertRaises(TypeError):
            create_trade_signal_edit(self.connection, self.trade_signal_id, None)

    def test_string_previous_values_rejected(self):
        with self.assertRaises(TypeError):
            create_trade_signal_edit(
                self.connection, self.trade_signal_id, '{"symbol": "SPY"}'
            )

    def test_list_previous_values_rejected(self):
        with self.assertRaises(TypeError):
            create_trade_signal_edit(self.connection, self.trade_signal_id, ["SPY"])

    def test_int_previous_values_rejected(self):
        with self.assertRaises(TypeError):
            create_trade_signal_edit(self.connection, self.trade_signal_id, 123)

    def test_missing_trade_signal_id_rejected(self):
        with self.assertRaises(ValueError):
            create_trade_signal_edit(self.connection, None, {"symbol": "SPY"})

    def test_foreign_key_failure_propagates(self):
        with self.assertRaises(sqlite3.IntegrityError):
            create_trade_signal_edit(self.connection, 999999, {"symbol": "SPY"})

    def test_multiple_edits_ordered_by_id_ascending(self):
        first = create_trade_signal_edit(
            self.connection, self.trade_signal_id, {"symbol": "SPY"}
        )
        second = create_trade_signal_edit(
            self.connection, self.trade_signal_id, {"symbol": "QQQ"}
        )
        third = create_trade_signal_edit(
            self.connection, self.trade_signal_id, {"symbol": "IWM"}
        )
        self.connection.commit()

        history = get_trade_signal_edits(self.connection, self.trade_signal_id)

        self.assertEqual([edit.id for edit in history], [first.id, second.id, third.id])

    def test_results_scoped_to_requested_trade_signal_id(self):
        other_raw_message = create_raw_message(
            self.connection, self.source_id, "STC SPY 500c"
        )
        other_signal = create_trade_signal(
            self.connection, other_raw_message.id, self.trader_id, "SPY", "STC"
        )
        self.connection.commit()

        create_trade_signal_edit(
            self.connection, self.trade_signal_id, {"symbol": "SPY"}
        )
        create_trade_signal_edit(
            self.connection, other_signal.id, {"symbol": "OTHER"}
        )
        self.connection.commit()

        history = get_trade_signal_edits(self.connection, self.trade_signal_id)

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].trade_signal_id, self.trade_signal_id)

    def test_no_edits_returns_empty_list(self):
        history = get_trade_signal_edits(self.connection, self.trade_signal_id)

        self.assertEqual(history, [])

    def test_identical_snapshots_insertable_more_than_once(self):
        first = create_trade_signal_edit(
            self.connection, self.trade_signal_id, {"symbol": "SPY"}
        )
        second = create_trade_signal_edit(
            self.connection, self.trade_signal_id, {"symbol": "SPY"}
        )
        self.connection.commit()

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(first.previous_values, second.previous_values)

        history = get_trade_signal_edits(self.connection, self.trade_signal_id)
        self.assertEqual(len(history), 2)

    def test_create_trade_signal_edit_not_committed_automatically(self):
        other_connection = get_connection(self.config)
        try:
            created = create_trade_signal_edit(
                self.connection, self.trade_signal_id, {"symbol": "SPY"}
            )

            row = other_connection.execute(
                "SELECT * FROM trade_signal_edits WHERE id = ?", (created.id,)
            ).fetchone()
            self.assertIsNone(row)

            self.connection.commit()

            row = other_connection.execute(
                "SELECT * FROM trade_signal_edits WHERE id = ?", (created.id,)
            ).fetchone()
            self.assertIsNotNone(row)
        finally:
            other_connection.close()

    def test_no_trade_signal_edit_update_or_delete_function_exists(self):
        public_names = [
            name
            for name in dir(repository)
            if not name.startswith("_") and callable(getattr(repository, name))
        ]
        self.assertFalse(
            any(
                "trade_signal_edit" in name.lower()
                and ("update" in name.lower() or "delete" in name.lower())
                for name in public_names
            ),
            "No update or delete function should exist for trade_signal_edits; "
            "it is append-only.",
        )

        source = inspect.getsource(repository)
        self.assertNotIn("UPDATE trade_signal_edits", source)
        self.assertNotIn("DELETE FROM trade_signal_edits", source)

    def test_update_trade_signal_still_does_not_create_history(self):
        update_trade_signal(self.connection, self.trade_signal_id, symbol="QQQ")
        self.connection.commit()

        history = get_trade_signal_edits(self.connection, self.trade_signal_id)

        self.assertEqual(history, [])


class TradeSignalsMatchingRepositoryTests(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = path
        self.config = DatabaseConfig(db_path=path)
        initialize_database(self.config)
        self.connection = get_connection(self.config)

        source_id = get_or_create_source(self.connection, "discord").id
        trader = create_trader(self.connection, source_id, "alice")
        raw_message = create_raw_message(self.connection, source_id, "BTO SPY 500c")
        self.connection.commit()

        self.source_id = source_id
        self.trader_id = trader.id
        self.raw_message_id = raw_message.id

    def tearDown(self):
        self.connection.close()
        os.remove(self.db_path)

    def _create_signal_at(self, created_at, **kwargs):
        fields = {
            "raw_message_id": self.raw_message_id,
            "trader_id": self.trader_id,
            "symbol": "SPY",
            "action": "BTO",
            "option_type": "call",
            "price": Decimal("3.25"),
            "expiration": "2026-12-18",
        }
        fields.update(kwargs)
        signal = create_trade_signal(self.connection, **fields)
        self.connection.execute(
            "UPDATE trade_signals SET created_at = ? WHERE id = ?",
            (created_at, signal.id),
        )
        self.connection.commit()
        return signal

    def test_match_found_within_window(self):
        self._create_signal_at("2026-07-12 12:03:00")

        matches = get_trade_signals_matching(
            self.connection,
            self.trader_id,
            "SPY",
            "BTO",
            "call",
            Decimal("3.25"),
            "2026-12-18",
            "2026-07-12 12:00:00",
            "2026-07-12 12:05:00",
        )

        self.assertEqual(len(matches), 1)

    def test_no_match_trader_id_differs(self):
        other_trader = create_trader(self.connection, self.source_id, "bob")
        self.connection.commit()
        self._create_signal_at("2026-07-12 12:03:00", trader_id=other_trader.id)

        matches = get_trade_signals_matching(
            self.connection,
            self.trader_id,
            "SPY",
            "BTO",
            "call",
            Decimal("3.25"),
            "2026-12-18",
            "2026-07-12 12:00:00",
            "2026-07-12 12:05:00",
        )

        self.assertEqual(matches, [])

    def test_no_match_symbol_differs(self):
        self._create_signal_at("2026-07-12 12:03:00", symbol="QQQ")

        matches = get_trade_signals_matching(
            self.connection,
            self.trader_id,
            "SPY",
            "BTO",
            "call",
            Decimal("3.25"),
            "2026-12-18",
            "2026-07-12 12:00:00",
            "2026-07-12 12:05:00",
        )

        self.assertEqual(matches, [])

    def test_no_match_action_differs(self):
        self._create_signal_at("2026-07-12 12:03:00", action="STC")

        matches = get_trade_signals_matching(
            self.connection,
            self.trader_id,
            "SPY",
            "BTO",
            "call",
            Decimal("3.25"),
            "2026-12-18",
            "2026-07-12 12:00:00",
            "2026-07-12 12:05:00",
        )

        self.assertEqual(matches, [])

    def test_no_match_option_type_differs(self):
        self._create_signal_at("2026-07-12 12:03:00", option_type="put")

        matches = get_trade_signals_matching(
            self.connection,
            self.trader_id,
            "SPY",
            "BTO",
            "call",
            Decimal("3.25"),
            "2026-12-18",
            "2026-07-12 12:00:00",
            "2026-07-12 12:05:00",
        )

        self.assertEqual(matches, [])

    def test_no_match_price_differs(self):
        self._create_signal_at("2026-07-12 12:03:00", price=Decimal("4.00"))

        matches = get_trade_signals_matching(
            self.connection,
            self.trader_id,
            "SPY",
            "BTO",
            "call",
            Decimal("3.25"),
            "2026-12-18",
            "2026-07-12 12:00:00",
            "2026-07-12 12:05:00",
        )

        self.assertEqual(matches, [])

    def test_no_match_expiration_differs(self):
        self._create_signal_at("2026-07-12 12:03:00", expiration="2026-11-20")

        matches = get_trade_signals_matching(
            self.connection,
            self.trader_id,
            "SPY",
            "BTO",
            "call",
            Decimal("3.25"),
            "2026-12-18",
            "2026-07-12 12:00:00",
            "2026-07-12 12:05:00",
        )

        self.assertEqual(matches, [])

    def test_no_match_outside_window(self):
        self._create_signal_at("2026-07-12 11:54:00")

        matches = get_trade_signals_matching(
            self.connection,
            self.trader_id,
            "SPY",
            "BTO",
            "call",
            Decimal("3.25"),
            "2026-12-18",
            "2026-07-12 12:00:00",
            "2026-07-12 12:05:00",
        )

        self.assertEqual(matches, [])

    def test_window_start_boundary_is_inclusive(self):
        self._create_signal_at("2026-07-12 12:00:00")

        matches = get_trade_signals_matching(
            self.connection,
            self.trader_id,
            "SPY",
            "BTO",
            "call",
            Decimal("3.25"),
            "2026-12-18",
            "2026-07-12 12:00:00",
            "2026-07-12 12:05:00",
        )

        self.assertEqual(len(matches), 1)

    def test_window_end_boundary_is_inclusive(self):
        self._create_signal_at("2026-07-12 12:05:00")

        matches = get_trade_signals_matching(
            self.connection,
            self.trader_id,
            "SPY",
            "BTO",
            "call",
            Decimal("3.25"),
            "2026-12-18",
            "2026-07-12 12:00:00",
            "2026-07-12 12:05:00",
        )

        self.assertEqual(len(matches), 1)

    def test_none_option_type_price_expiration_matches(self):
        self._create_signal_at(
            "2026-07-12 12:03:00", option_type=None, price=None, expiration=None
        )

        matches = get_trade_signals_matching(
            self.connection,
            self.trader_id,
            "SPY",
            "BTO",
            None,
            None,
            None,
            "2026-07-12 12:00:00",
            "2026-07-12 12:05:00",
        )

        self.assertEqual(len(matches), 1)

    def test_empty_list_when_no_matches_exist(self):
        matches = get_trade_signals_matching(
            self.connection,
            self.trader_id,
            "SPY",
            "BTO",
            "call",
            Decimal("3.25"),
            "2026-12-18",
            "2026-07-12 12:00:00",
            "2026-07-12 12:05:00",
        )

        self.assertEqual(matches, [])

    def test_multiple_matches_ordered_by_id_ascending(self):
        first = self._create_signal_at("2026-07-12 12:01:00")
        second = self._create_signal_at("2026-07-12 12:02:00")
        third = self._create_signal_at("2026-07-12 12:03:00")

        matches = get_trade_signals_matching(
            self.connection,
            self.trader_id,
            "SPY",
            "BTO",
            "call",
            Decimal("3.25"),
            "2026-12-18",
            "2026-07-12 12:00:00",
            "2026-07-12 12:05:00",
        )

        self.assertEqual(
            [match.id for match in matches], [first.id, second.id, third.id]
        )

    def test_missing_trader_id_rejected(self):
        with self.assertRaises(ValueError):
            get_trade_signals_matching(
                self.connection,
                None,
                "SPY",
                "BTO",
                "call",
                Decimal("3.25"),
                "2026-12-18",
                "2026-07-12 12:00:00",
                "2026-07-12 12:05:00",
            )

    def test_whitespace_only_symbol_rejected(self):
        with self.assertRaises(ValueError):
            get_trade_signals_matching(
                self.connection,
                self.trader_id,
                "   ",
                "BTO",
                "call",
                Decimal("3.25"),
                "2026-12-18",
                "2026-07-12 12:00:00",
                "2026-07-12 12:05:00",
            )

    def test_whitespace_only_action_rejected(self):
        with self.assertRaises(ValueError):
            get_trade_signals_matching(
                self.connection,
                self.trader_id,
                "SPY",
                "   ",
                "call",
                Decimal("3.25"),
                "2026-12-18",
                "2026-07-12 12:00:00",
                "2026-07-12 12:05:00",
            )

    def test_invalid_price_type_rejected(self):
        with self.assertRaises(TypeError):
            get_trade_signals_matching(
                self.connection,
                self.trader_id,
                "SPY",
                "BTO",
                "call",
                3.25,
                "2026-12-18",
                "2026-07-12 12:00:00",
                "2026-07-12 12:05:00",
            )


if __name__ == "__main__":
    unittest.main()
