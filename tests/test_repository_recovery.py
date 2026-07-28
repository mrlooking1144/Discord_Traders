"""Tests for the Recovery Milestone R1 repository additions.

Covers the new channels, import_batches, and message_extractions CRUD
functions, plus the additive canonical_name/raw_messages/trade_signals
columns added alongside them. Kept separate from tests/test_repository_sources.py
(which covers the original five v0.1.0 tables) to keep this milestone's
diff self-contained.
"""

import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal

from database.config import DatabaseConfig
from database.db import get_connection, initialize_database
from database.models import Channel, ImportBatch, MessageExtraction, RawMessage, TradeSignal
from database.repository import (
    create_channel,
    create_import_batch,
    create_message_extraction,
    create_raw_message,
    create_trade_signal,
    create_trader,
    delete_import_batch_if_empty,
    get_channel_by_external_id,
    get_channel_chronological_checkpoints,
    get_channel_ingestion_cursors,
    get_current_extraction,
    get_import_batch_by_id,
    get_or_create_channel,
    get_or_create_source,
    get_or_create_unspecified_channel,
    get_raw_message_by_channel_and_external_id,
    get_raw_message_by_id,
    get_raw_message_ids_by_import_batch,
    get_trade_signal_by_id,
    get_trader_by_id,
    get_traders_by_canonical_name,
    supersede_extraction,
)


class _RecoveryRepositoryTestCase(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = path
        self.config = DatabaseConfig(db_path=path)
        initialize_database(self.config)
        self.connection = get_connection(self.config)
        self.source = get_or_create_source(self.connection, "discord")
        self.connection.commit()

    def tearDown(self):
        self.connection.close()
        os.remove(self.db_path)


class ChannelsRepositoryTests(_RecoveryRepositoryTestCase):
    def test_create_channel_with_external_id(self):
        channel = create_channel(
            self.connection, self.source.id, external_channel_id="chan-1", name="pro-alerts"
        )
        self.connection.commit()

        self.assertIsInstance(channel, Channel)
        self.assertEqual(channel.external_channel_id, "chan-1")
        self.assertEqual(channel.name, "pro-alerts")
        self.assertIsNotNone(channel.id)

    def test_get_channel_by_external_id_found_and_missing(self):
        create_channel(self.connection, self.source.id, external_channel_id="chan-2")
        self.connection.commit()

        found = get_channel_by_external_id(self.connection, self.source.id, "chan-2")
        missing = get_channel_by_external_id(self.connection, self.source.id, "chan-missing")

        self.assertIsNotNone(found)
        self.assertIsNone(missing)

    def test_get_or_create_channel_is_idempotent(self):
        first = get_or_create_channel(self.connection, self.source.id, "chan-3", name="a")
        self.connection.commit()
        second = get_or_create_channel(self.connection, self.source.id, "chan-3", name="a")
        self.connection.commit()

        self.assertEqual(first.id, second.id)

        count = self.connection.execute(
            "SELECT COUNT(*) FROM channels WHERE external_channel_id = 'chan-3'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_duplicate_external_channel_id_raises_integrity_error(self):
        create_channel(self.connection, self.source.id, external_channel_id="dup")
        self.connection.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            create_channel(self.connection, self.source.id, external_channel_id="dup")

    def test_channels_with_no_external_id_are_not_forced_unique(self):
        first = create_channel(self.connection, self.source.id, name="no-id-a")
        second = create_channel(self.connection, self.source.id, name="no-id-b")
        self.connection.commit()

        self.assertNotEqual(first.id, second.id)

    def test_get_or_create_unspecified_channel_is_stable_per_source(self):
        first = get_or_create_unspecified_channel(self.connection, self.source.id)
        self.connection.commit()
        second = get_or_create_unspecified_channel(self.connection, self.source.id)
        self.connection.commit()

        self.assertEqual(first.id, second.id)
        self.assertEqual(first.name, "unspecified")

    def test_unspecified_channel_distinct_per_source(self):
        other_source = get_or_create_source(self.connection, "telegram")
        self.connection.commit()

        discord_unspecified = get_or_create_unspecified_channel(self.connection, self.source.id)
        telegram_unspecified = get_or_create_unspecified_channel(self.connection, other_source.id)
        self.connection.commit()

        self.assertNotEqual(discord_unspecified.id, telegram_unspecified.id)


class ImportBatchesRepositoryTests(_RecoveryRepositoryTestCase):
    def test_create_import_batch(self):
        batch = create_import_batch(
            self.connection,
            self.source.id,
            reference_date="2026-07-24",
            timezone="Asia/Riyadh",
            raw_input_text="Bdorts\nAPP\n...",
        )
        self.connection.commit()

        self.assertIsInstance(batch, ImportBatch)
        self.assertEqual(batch.reference_date, "2026-07-24")
        self.assertEqual(batch.timezone, "Asia/Riyadh")
        self.assertIsNotNone(batch.id)

    def test_get_import_batch_by_id_found_and_missing(self):
        batch = create_import_batch(
            self.connection, self.source.id, reference_date="2026-07-24", timezone="UTC"
        )
        self.connection.commit()

        found = get_import_batch_by_id(self.connection, batch.id)
        missing = get_import_batch_by_id(self.connection, batch.id + 999)

        self.assertEqual(found, batch)
        self.assertIsNone(missing)

    def test_rejects_empty_reference_date(self):
        with self.assertRaises(ValueError):
            create_import_batch(self.connection, self.source.id, reference_date="", timezone="UTC")

    def test_rejects_whitespace_only_timezone(self):
        with self.assertRaises(ValueError):
            create_import_batch(
                self.connection, self.source.id, reference_date="2026-07-24", timezone="   "
            )


class MessageExtractionsRepositoryTests(_RecoveryRepositoryTestCase):
    def setUp(self):
        super().setUp()
        self.raw_message = create_raw_message(self.connection, self.source.id, "BOUGHT AVGO ...")
        self.connection.commit()

    def test_create_message_extraction_marks_current(self):
        extraction = create_message_extraction(
            self.connection,
            self.raw_message.id,
            parser_version="v1",
            parse_status="parsed",
            confidence=0.95,
            ambiguity_flags=["multiple_channel_tags"],
        )
        self.connection.commit()

        self.assertIsInstance(extraction, MessageExtraction)
        self.assertTrue(extraction.is_current)
        self.assertIsNone(extraction.superseded_at)
        self.assertEqual(extraction.ambiguity_flags, ["multiple_channel_tags"])

    def test_rejects_invalid_parse_status(self):
        with self.assertRaises(ValueError):
            create_message_extraction(
                self.connection, self.raw_message.id, parser_version="v1", parse_status="bogus"
            )

    def test_rejects_empty_parser_version(self):
        with self.assertRaises(ValueError):
            create_message_extraction(
                self.connection, self.raw_message.id, parser_version="  ", parse_status="parsed"
            )

    def test_get_current_extraction_found_and_missing(self):
        create_message_extraction(
            self.connection, self.raw_message.id, parser_version="v1", parse_status="parsed"
        )
        self.connection.commit()

        found = get_current_extraction(self.connection, self.raw_message.id)
        other_message = create_raw_message(self.connection, self.source.id, "other text")
        self.connection.commit()
        missing = get_current_extraction(self.connection, other_message.id)

        self.assertIsNotNone(found)
        self.assertIsNone(missing)

    def test_second_current_extraction_without_supersede_raises(self):
        create_message_extraction(
            self.connection, self.raw_message.id, parser_version="v1", parse_status="parsed"
        )
        self.connection.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            create_message_extraction(
                self.connection,
                self.raw_message.id,
                parser_version="v2",
                parse_status="parsed",
            )

    def test_supersede_then_create_new_current_extraction(self):
        first = create_message_extraction(
            self.connection, self.raw_message.id, parser_version="v1", parse_status="unrecognized"
        )
        self.connection.commit()

        superseded = supersede_extraction(self.connection, first.id)
        self.connection.commit()
        second = create_message_extraction(
            self.connection, self.raw_message.id, parser_version="v2", parse_status="parsed"
        )
        self.connection.commit()

        self.assertFalse(superseded.is_current)
        self.assertIsNotNone(superseded.superseded_at)
        self.assertTrue(second.is_current)

        current = get_current_extraction(self.connection, self.raw_message.id)
        self.assertEqual(current.id, second.id)

        history_count = self.connection.execute(
            "SELECT COUNT(*) FROM message_extractions WHERE raw_message_id = ?",
            (self.raw_message.id,),
        ).fetchone()[0]
        self.assertEqual(history_count, 2)

    def test_supersede_missing_extraction_returns_none(self):
        result = supersede_extraction(self.connection, 999999)
        self.connection.commit()

        self.assertIsNone(result)


class TraderCanonicalNameTests(_RecoveryRepositoryTestCase):
    def test_canonical_name_derived_automatically(self):
        trader = create_trader(self.connection, self.source.id, "  Matae  ")
        self.connection.commit()

        self.assertEqual(trader.canonical_name, "matae")
        self.assertEqual(trader.name, "  Matae  ")

    def test_case_insensitive_lookup_finds_differently_cased_names(self):
        create_trader(self.connection, self.source.id, "Matae")
        create_trader(self.connection, self.source.id, "matae")
        self.connection.commit()

        results = get_traders_by_canonical_name(self.connection, self.source.id, "matae")

        self.assertEqual(len(results), 2)
        self.assertEqual({t.name for t in results}, {"Matae", "matae"})

    def test_no_match_returns_empty_list(self):
        results = get_traders_by_canonical_name(self.connection, self.source.id, "nobody")

        self.assertEqual(results, [])


class RawMessageChannelScopingTests(_RecoveryRepositoryTestCase):
    def test_create_raw_message_with_channel_fields(self):
        channel = get_or_create_channel(self.connection, self.source.id, "chan-a")
        batch = create_import_batch(
            self.connection, self.source.id, reference_date="2026-07-24", timezone="UTC"
        )
        self.connection.commit()

        raw_message = create_raw_message(
            self.connection,
            self.source.id,
            "BOUGHT AVGO 07/24 380P $1.14 [SMALL]",
            external_id="synthetic-1",
            channel_id=channel.id,
            import_batch_id=batch.id,
            sequence_in_batch=1,
        )
        self.connection.commit()

        self.assertIsInstance(raw_message, RawMessage)
        self.assertEqual(raw_message.channel_id, channel.id)
        self.assertEqual(raw_message.import_batch_id, batch.id)
        self.assertEqual(raw_message.sequence_in_batch, 1)

    def test_channel_and_external_id_together_are_unique(self):
        channel = get_or_create_channel(self.connection, self.source.id, "chan-b")
        self.connection.commit()
        create_raw_message(
            self.connection, self.source.id, "first", channel_id=channel.id, external_id="msg-1"
        )
        self.connection.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            create_raw_message(
                self.connection,
                self.source.id,
                "second",
                channel_id=channel.id,
                external_id="msg-1",
            )

    def test_same_external_id_allowed_across_different_channels(self):
        channel_a = get_or_create_channel(self.connection, self.source.id, "chan-c")
        channel_b = get_or_create_channel(self.connection, self.source.id, "chan-d")
        self.connection.commit()

        create_raw_message(
            self.connection, self.source.id, "first", channel_id=channel_a.id, external_id="msg-x"
        )
        self.connection.commit()

        # Different channel, same external_id: this must succeed. The old
        # source-wide (source_id, external_id) uniqueness was removed from
        # the schema precisely so a message ID can validly repeat across
        # two different channels of the same source.
        second = create_raw_message(
            self.connection,
            self.source.id,
            "second",
            channel_id=channel_b.id,
            external_id="msg-x",
        )
        self.connection.commit()

        self.assertEqual(second.channel_id, channel_b.id)
        self.assertEqual(second.external_id, "msg-x")

    def test_legacy_null_channel_rows_still_enforce_source_wide_uniqueness(self):
        # Rows with no channel (e.g. today's manual-entry path, which never
        # sets channel_id) must keep the exact pre-migration behavior:
        # external_id unique per source. This is what makes
        # test_service.TradeServiceIngestMessageTests
        # .test_duplicate_external_message_id_raises_integrity_error still
        # correct after this migration.
        create_raw_message(
            self.connection, self.source.id, "first", channel_id=None, external_id="msg-legacy"
        )
        self.connection.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            create_raw_message(
                self.connection,
                self.source.id,
                "second",
                channel_id=None,
                external_id="msg-legacy",
            )

    def test_null_channel_rows_do_not_collide_with_real_channel_rows(self):
        channel = get_or_create_channel(self.connection, self.source.id, "chan-legacy-mix")
        self.connection.commit()

        create_raw_message(
            self.connection, self.source.id, "no channel", channel_id=None, external_id="msg-mix"
        )
        self.connection.commit()

        # Same external_id, but this one has a real channel: must not
        # collide with the channel_id-IS-NULL row above, since they are
        # governed by two separate partial unique indexes.
        with_channel = create_raw_message(
            self.connection,
            self.source.id,
            "with channel",
            channel_id=channel.id,
            external_id="msg-mix",
        )
        self.connection.commit()

        self.assertEqual(with_channel.channel_id, channel.id)

    def test_get_raw_message_by_channel_and_external_id(self):
        channel = get_or_create_channel(self.connection, self.source.id, "chan-e")
        self.connection.commit()
        created = create_raw_message(
            self.connection, self.source.id, "text", channel_id=channel.id, external_id="msg-y"
        )
        self.connection.commit()

        found = get_raw_message_by_channel_and_external_id(self.connection, channel.id, "msg-y")
        missing = get_raw_message_by_channel_and_external_id(self.connection, channel.id, "msg-z")

        self.assertEqual(found.id, created.id)
        self.assertIsNone(missing)

    def test_channel_fields_default_to_none(self):
        raw_message = create_raw_message(self.connection, self.source.id, "no channel here")
        self.connection.commit()

        self.assertIsNone(raw_message.channel_id)
        self.assertIsNone(raw_message.import_batch_id)
        self.assertIsNone(raw_message.sequence_in_batch)


class TradeSignalExtendedFieldsTests(_RecoveryRepositoryTestCase):
    def setUp(self):
        super().setUp()
        self.trader = create_trader(self.connection, self.source.id, "TC")
        self.raw_message = create_raw_message(self.connection, self.source.id, "BOUGHT IBM ...")
        self.connection.commit()

    def test_create_trade_signal_with_extended_fields(self):
        extraction = create_message_extraction(
            self.connection, self.raw_message.id, parser_version="v1", parse_status="parsed"
        )
        self.connection.commit()

        signal = create_trade_signal(
            self.connection,
            self.raw_message.id,
            self.trader.id,
            "IBM",
            "BOUGHT",
            option_type="call",
            price=Decimal("2.58"),
            expiration="2026-07-24",
            strike=Decimal("207.5"),
            expiration_raw="07/24",
            event_type="ENTRY",
            qualifier="[SMALL]",
            stated_entry_price=None,
            stated_return_pct=None,
            notes="HOLDING LAST RUNNER FOR GLORY",
            extraction_id=extraction.id,
        )
        self.connection.commit()

        self.assertIsInstance(signal, TradeSignal)
        self.assertEqual(signal.action, "BOUGHT")
        self.assertEqual(signal.strike, "207.5")
        self.assertEqual(signal.expiration_raw, "07/24")
        self.assertEqual(signal.event_type, "ENTRY")
        self.assertEqual(signal.qualifier, "[SMALL]")
        self.assertEqual(signal.notes, "HOLDING LAST RUNNER FOR GLORY")
        self.assertEqual(signal.extraction_id, extraction.id)

    def test_bought_and_sold_are_not_aliased(self):
        bought = create_trade_signal(
            self.connection, self.raw_message.id, self.trader.id, "IBM", "BOUGHT"
        )
        sold = create_trade_signal(
            self.connection, self.raw_message.id, self.trader.id, "IBM", "SOLD"
        )
        self.connection.commit()

        self.assertEqual(bought.action, "BOUGHT")
        self.assertEqual(sold.action, "SOLD")

    def test_extended_fields_default_to_none(self):
        signal = create_trade_signal(
            self.connection, self.raw_message.id, self.trader.id, "IBM", "BOUGHT"
        )
        self.connection.commit()

        self.assertIsNone(signal.strike)
        self.assertIsNone(signal.expiration_raw)
        self.assertIsNone(signal.event_type)
        self.assertIsNone(signal.qualifier)
        self.assertIsNone(signal.stated_entry_price)
        self.assertIsNone(signal.stated_return_pct)
        self.assertIsNone(signal.notes)
        self.assertIsNone(signal.extraction_id)

    def test_strike_must_be_decimal_not_float(self):
        with self.assertRaises(TypeError):
            create_trade_signal(
                self.connection,
                self.raw_message.id,
                self.trader.id,
                "IBM",
                "BOUGHT",
                strike=207.5,
            )

    def test_stated_return_pct_must_be_decimal_not_float(self):
        with self.assertRaises(TypeError):
            create_trade_signal(
                self.connection,
                self.raw_message.id,
                self.trader.id,
                "IBM",
                "SOLD",
                stated_return_pct=24.03,
            )

    def test_invalid_extraction_id_raises_integrity_error(self):
        with self.assertRaises(sqlite3.IntegrityError):
            create_trade_signal(
                self.connection,
                self.raw_message.id,
                self.trader.id,
                "IBM",
                "BOUGHT",
                extraction_id=999999,
            )

    def test_get_trade_signal_by_id_round_trips_extended_fields(self):
        created = create_trade_signal(
            self.connection,
            self.raw_message.id,
            self.trader.id,
            "IBM",
            "SOLD",
            strike=Decimal("207.5"),
            qualifier="1/2",
        )
        self.connection.commit()

        fetched = get_trade_signal_by_id(self.connection, created.id)

        self.assertEqual(fetched, created)
        self.assertEqual(fetched.strike, "207.5")
        self.assertEqual(fetched.qualifier, "1/2")


class GetRawMessageByIdTests(_RecoveryRepositoryTestCase):
    def test_found_and_missing(self):
        created = create_raw_message(self.connection, self.source.id, "BOUGHT AVGO ...")
        self.connection.commit()

        found = get_raw_message_by_id(self.connection, created.id)
        missing = get_raw_message_by_id(self.connection, created.id + 999999)

        self.assertEqual(found, created)
        self.assertIsNone(missing)


class GetRawMessageIdsByImportBatchTests(_RecoveryRepositoryTestCase):
    def test_returns_only_linked_ids_in_ascending_order(self):
        batch = create_import_batch(
            self.connection, self.source.id, reference_date="2026-07-24", timezone="UTC"
        )
        other_batch = create_import_batch(
            self.connection, self.source.id, reference_date="2026-07-24", timezone="UTC"
        )
        self.connection.commit()

        third = create_raw_message(
            self.connection, self.source.id, "c", import_batch_id=batch.id
        )
        first = create_raw_message(
            self.connection, self.source.id, "a", import_batch_id=batch.id
        )
        create_raw_message(
            self.connection, self.source.id, "x", import_batch_id=other_batch.id
        )
        self.connection.commit()

        ids = get_raw_message_ids_by_import_batch(self.connection, batch.id)

        self.assertEqual(ids, sorted([third.id, first.id]))

    def test_no_linked_messages_returns_empty_list(self):
        batch = create_import_batch(
            self.connection, self.source.id, reference_date="2026-07-24", timezone="UTC"
        )
        self.connection.commit()

        self.assertEqual(get_raw_message_ids_by_import_batch(self.connection, batch.id), [])

    def test_nonexistent_import_batch_returns_empty_list(self):
        self.assertEqual(
            get_raw_message_ids_by_import_batch(self.connection, 999999), []
        )


class DeleteImportBatchIfEmptyTests(_RecoveryRepositoryTestCase):
    def test_deletes_batch_with_no_linked_raw_messages(self):
        batch = create_import_batch(
            self.connection, self.source.id, reference_date="2026-07-24", timezone="UTC"
        )
        self.connection.commit()

        deleted = delete_import_batch_if_empty(self.connection, batch.id)
        self.connection.commit()

        self.assertTrue(deleted)
        self.assertIsNone(get_import_batch_by_id(self.connection, batch.id))

    def test_does_not_delete_batch_with_linked_raw_messages(self):
        batch = create_import_batch(
            self.connection, self.source.id, reference_date="2026-07-24", timezone="UTC"
        )
        self.connection.commit()
        create_raw_message(
            self.connection, self.source.id, "a", import_batch_id=batch.id
        )
        self.connection.commit()

        deleted = delete_import_batch_if_empty(self.connection, batch.id)
        self.connection.commit()

        self.assertFalse(deleted)
        self.assertIsNotNone(get_import_batch_by_id(self.connection, batch.id))

    def test_nonexistent_import_batch_returns_false(self):
        self.assertFalse(delete_import_batch_if_empty(self.connection, 999999))


class GetTraderByIdTests(_RecoveryRepositoryTestCase):
    def test_found_and_missing(self):
        created = create_trader(self.connection, self.source.id, "alice")
        self.connection.commit()

        found = get_trader_by_id(self.connection, created.id)
        missing = get_trader_by_id(self.connection, created.id + 999999)

        self.assertEqual(found, created)
        self.assertIsNone(missing)


class ChannelCheckpointQueryTests(_RecoveryRepositoryTestCase):
    def test_ingestion_cursor_reflects_highest_id_per_channel(self):
        channel = get_or_create_channel(self.connection, self.source.id, "chan-a")
        self.connection.commit()
        create_raw_message(self.connection, self.source.id, "first", channel_id=channel.id)
        second = create_raw_message(
            self.connection, self.source.id, "second", channel_id=channel.id
        )
        self.connection.commit()

        rows = get_channel_ingestion_cursors(self.connection)
        row = next(r for r in rows if r["channel_id"] == channel.id)

        self.assertEqual(row["last_ingested_raw_message_id"], second.id)
        self.assertEqual(row["channel_external_id"], "chan-a")

    def test_channel_with_no_raw_messages_is_absent(self):
        channel = get_or_create_channel(self.connection, self.source.id, "chan-empty")
        self.connection.commit()

        rows = get_channel_ingestion_cursors(self.connection)

        self.assertNotIn(channel.id, {r["channel_id"] for r in rows})

    def test_chronological_checkpoint_reflects_max_received_at(self):
        channel = get_or_create_channel(self.connection, self.source.id, "chan-b")
        self.connection.commit()
        create_raw_message(
            self.connection,
            self.source.id,
            "earlier",
            channel_id=channel.id,
            received_at="2026-07-24T10:00:00.000000+00:00",
        )
        later = create_raw_message(
            self.connection,
            self.source.id,
            "later",
            channel_id=channel.id,
            received_at="2026-07-24T20:00:00.000000+00:00",
        )
        self.connection.commit()

        rows = get_channel_chronological_checkpoints(self.connection)
        row = next(r for r in rows if r["channel_id"] == channel.id)

        self.assertEqual(row["latest_received_at"], "2026-07-24T20:00:00.000000+00:00")
        self.assertEqual(row["latest_received_raw_message_id"], later.id)

    def test_channel_with_no_resolved_timestamp_is_absent(self):
        channel = get_or_create_channel(self.connection, self.source.id, "chan-c")
        self.connection.commit()
        create_raw_message(self.connection, self.source.id, "no ts", channel_id=channel.id)
        self.connection.commit()

        rows = get_channel_chronological_checkpoints(self.connection)

        self.assertNotIn(channel.id, {r["channel_id"] for r in rows})

    def test_tie_broken_by_highest_raw_message_id(self):
        channel = get_or_create_channel(self.connection, self.source.id, "chan-d")
        self.connection.commit()
        create_raw_message(
            self.connection,
            self.source.id,
            "tie-a",
            channel_id=channel.id,
            received_at="2026-07-24T10:00:00.000000+00:00",
        )
        tie_b = create_raw_message(
            self.connection,
            self.source.id,
            "tie-b",
            channel_id=channel.id,
            received_at="2026-07-24T10:00:00.000000+00:00",
        )
        self.connection.commit()

        rows = get_channel_chronological_checkpoints(self.connection)
        row = next(r for r in rows if r["channel_id"] == channel.id)

        self.assertEqual(row["latest_received_raw_message_id"], tie_b.id)


if __name__ == "__main__":
    unittest.main()
