"""Tests for the Recovery Milestone R1 repository additions.

Covers the new channels, import_batches, and message_extractions CRUD
functions, plus the additive canonical_name/raw_messages/trade_signals
columns added alongside them. Kept separate from tests/test_repository_sources.py
(which covers the original five v0.1.0 tables) to keep this milestone's
diff self-contained.
"""

import json
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal

from database.config import DatabaseConfig
from database.db import get_connection, initialize_database
from database.lifecycle import LifecycleBuild, build_lifecycle_sequence
from database.models import (
    Channel,
    ChannelImportOperation,
    ImportBatch,
    MessageExtraction,
    RawMessage,
    TradeLifecycle,
    TradeSignal,
)
from database.repository import (
    build_signal_snapshot_json,
    clear_lifecycle_pointers_for_generation,
    create_channel,
    create_channel_import_operation,
    create_import_batch,
    create_lifecycle_unresolved_singleton,
    create_message_extraction,
    create_raw_message,
    create_trade_lifecycle,
    create_trade_lifecycle_event,
    create_trade_signal,
    create_trader,
    delete_import_batch_if_empty,
    get_all_current_lifecycle_eligible_signal_ids,
    get_all_current_lifecycle_keys,
    get_all_current_trade_lifecycles,
    get_channel_by_external_id,
    get_channel_by_id,
    get_channel_chronological_checkpoints,
    get_channel_ingestion_cursors,
    get_chronological_positions_for_raw_messages,
    get_current_extraction,
    get_current_incomplete_lifecycle_signal_snapshots,
    get_current_incomplete_lifecycles,
    get_current_lifecycle_ids_for_raw_message_ids,
    get_current_lifecycles_for_key,
    get_current_signal_snapshot_for_raw_message,
    get_current_trade_signals_for_key,
    get_distinct_lifecycle_keys_for_signal_ids,
    get_import_batch_by_id,
    get_latest_channel_import_operation,
    get_or_create_channel,
    get_or_create_source,
    get_or_create_unspecified_channel,
    get_raw_message_by_channel_and_external_id,
    get_raw_message_by_id,
    get_raw_message_ids_by_import_batch,
    get_recorded_shape_for_generation,
    get_trade_lifecycle_by_id,
    get_trade_lifecycle_events,
    get_trade_lifecycle_history_rows,
    get_trade_lifecycle_lineage_raw_message_ids,
    get_trade_signal_by_id,
    get_trader_by_id,
    get_traders_by_canonical_name,
    list_channels,
    list_current_trade_lifecycles,
    persist_lifecycle_builds,
    supersede_extraction,
    supersede_trade_lifecycle,
    update_trade_signal,
    update_trade_signal_lifecycle_pointer,
    validate_lifecycle_membership_integrity,
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

    # -- Recovery Milestone R9a: get_channel_by_id() / list_channels() -----

    def test_get_channel_by_id_returns_existing_channel(self):
        created = create_channel(
            self.connection, self.source.id, external_channel_id="chan-9", name="pro-alerts"
        )
        self.connection.commit()

        found = get_channel_by_id(self.connection, created.id)

        self.assertIsNotNone(found)
        self.assertEqual(found.id, created.id)
        self.assertEqual(found.external_channel_id, "chan-9")
        self.assertEqual(found.name, "pro-alerts")

    def test_get_channel_by_id_missing_returns_none(self):
        self.assertIsNone(get_channel_by_id(self.connection, 999999))

    def test_list_channels_empty_for_source_with_no_channels(self):
        self.assertEqual(list_channels(self.connection, self.source.id), [])

    def test_list_channels_includes_channel_with_zero_messages(self):
        # No raw_messages row is ever created for this channel - unlike
        # get_channel_ingestion_cursors(), list_channels() must still
        # return it.
        create_channel(self.connection, self.source.id, external_channel_id="empty-chan")
        self.connection.commit()

        channels = list_channels(self.connection, self.source.id)

        self.assertEqual(
            {c.external_channel_id for c in channels}, {"empty-chan"}
        )
        self.assertEqual(
            get_channel_ingestion_cursors(self.connection), [],
            "sanity check: the ingestion-cursor query has no row for a "
            "message-less channel, confirming list_channels() is the one "
            "actually exercised by this test, not accidentally reusing "
            "that query",
        )

    def test_list_channels_is_source_scoped(self):
        other_source = get_or_create_source(self.connection, "telegram")
        create_channel(self.connection, self.source.id, external_channel_id="discord-chan")
        create_channel(self.connection, other_source.id, external_channel_id="telegram-chan")
        self.connection.commit()

        channels = list_channels(self.connection, self.source.id)

        self.assertEqual({c.external_channel_id for c in channels}, {"discord-chan"})

    def test_list_channels_deterministic_ordering(self):
        create_channel(self.connection, self.source.id, external_channel_id="z-id", name="Bravo")
        create_channel(self.connection, self.source.id, external_channel_id="a-id", name="alpha")
        create_channel(self.connection, self.source.id, external_channel_id="m-id")
        self.connection.commit()

        channels = list_channels(self.connection, self.source.id)

        # "alpha"/"Bravo" ordered case-insensitively by name; "m-id" (no
        # name) ordered by its own external_channel_id, which sorts after
        # both names alphabetically here.
        self.assertEqual(
            [c.external_channel_id for c in channels], ["a-id", "z-id", "m-id"]
        )

    def test_list_channels_whitespace_only_name_falls_back_to_external_id(self):
        # A whitespace-only name is treated as absent - ordered by its
        # own external_channel_id, exactly like a NULL name, never
        # sorted as if "   " were a real (blank-looking) display value.
        create_channel(self.connection, self.source.id, external_channel_id="z-id", name="Bravo")
        create_channel(
            self.connection, self.source.id, external_channel_id="a-id", name="   "
        )
        self.connection.commit()

        channels = list_channels(self.connection, self.source.id)

        self.assertEqual(
            [c.external_channel_id for c in channels], ["a-id", "z-id"]
        )

    def test_list_channels_includes_unspecified_sentinel(self):
        # list_channels() is deliberately repository-generic - excluding
        # the sentinel is the Bulk Channel Import service layer's own
        # responsibility, not this function's.
        get_or_create_unspecified_channel(self.connection, self.source.id)
        self.connection.commit()

        channels = list_channels(self.connection, self.source.id)

        self.assertIn("__unspecified__", {c.external_channel_id for c in channels})

    def test_list_channels_tie_broken_by_id_when_display_values_match(self):
        first = create_channel(
            self.connection, self.source.id, external_channel_id="dup-a", name="same"
        )
        second = create_channel(
            self.connection, self.source.id, external_channel_id="dup-b", name="same"
        )
        self.connection.commit()

        channels = list_channels(self.connection, self.source.id)

        self.assertEqual([c.id for c in channels], sorted([first.id, second.id]))


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


class ChannelImportOperationsRepositoryTests(_RecoveryRepositoryTestCase):
    """Recovery Milestone R9a: create_channel_import_operation() /
    get_latest_channel_import_operation()."""

    def setUp(self):
        super().setUp()
        self.channel = create_channel(
            self.connection, self.source.id, external_channel_id="ops-chan"
        )
        self.connection.commit()

    def _create_operation(self, **overrides):
        values = dict(
            channel_id=self.channel.id,
            import_batch_id=None,
            reference_date="2026-01-01",
            timezone="UTC",
            processed_count=15,
            stored_count=0,
            duplicate_count=15,
            unrecognized_count=0,
            failed_count=0,
        )
        values.update(overrides)
        return create_channel_import_operation(self.connection, **values)

    def test_insert_and_read_back(self):
        operation = self._create_operation()
        self.connection.commit()

        self.assertIsInstance(operation, ChannelImportOperation)
        self.assertIsNotNone(operation.id)
        self.assertEqual(operation.channel_id, self.channel.id)
        self.assertIsNone(operation.import_batch_id)
        self.assertEqual(operation.processed_count, 15)
        self.assertEqual(operation.duplicate_count, 15)
        self.assertIsNotNone(operation.committed_at)

    def test_duplicate_only_operation_has_null_import_batch_id(self):
        operation = self._create_operation(
            import_batch_id=None, stored_count=0, duplicate_count=15
        )
        self.connection.commit()

        self.assertIsNone(operation.import_batch_id)

    def test_stored_operation_requires_import_batch_id(self):
        import_batch = create_import_batch(
            self.connection, self.source.id, "2026-01-01", "UTC", "raw text"
        )
        self.connection.commit()

        operation = self._create_operation(
            import_batch_id=import_batch.id,
            stored_count=15,
            duplicate_count=0,
        )
        self.connection.commit()

        self.assertEqual(operation.import_batch_id, import_batch.id)

    def test_database_constraint_violation_propagates_as_integrity_error(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self._create_operation(processed_count=14, stored_count=0, duplicate_count=14)

    def test_create_channel_import_operation_does_not_commit(self):
        self._create_operation()
        self.connection.rollback()

        count = self.connection.execute(
            "SELECT COUNT(*) FROM channel_import_operations"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_get_latest_channel_import_operation_returns_highest_id(self):
        first = self._create_operation()
        self.connection.commit()
        second = self._create_operation()
        self.connection.commit()

        latest = get_latest_channel_import_operation(self.connection, self.channel.id)

        self.assertEqual(latest.id, second.id)
        self.assertNotEqual(latest.id, first.id)

    def test_get_latest_channel_import_operation_no_operations_returns_none(self):
        self.assertIsNone(
            get_latest_channel_import_operation(self.connection, self.channel.id)
        )

    def test_get_latest_channel_import_operation_is_channel_scoped(self):
        other_channel = create_channel(
            self.connection, self.source.id, external_channel_id="other-ops-chan"
        )
        self.connection.commit()
        self._create_operation()
        self.connection.commit()

        self.assertIsNone(
            get_latest_channel_import_operation(self.connection, other_channel.id)
        )

    def test_get_latest_channel_import_operation_does_not_write(self):
        self._create_operation()
        self.connection.commit()

        changes_before = self.connection.total_changes
        get_latest_channel_import_operation(self.connection, self.channel.id)

        self.assertEqual(self.connection.total_changes, changes_before)


# ---------------------------------------------------------------------------
# Recovery Milestone R6.3: trade_lifecycles / trade_lifecycle_events /
# trade_signals.lifecycle_id repository layer. No matching/linking
# orchestration is exercised here - only repository-level discovery,
# reads, persistence, and the membership-integrity validation query.
# database/lifecycle.py (the pure engine, R6.2) is used only to produce
# realistic LifecycleBuild fixtures for the persistence tests; its own
# behavior is not re-tested here (see tests/test_lifecycle.py).
# ---------------------------------------------------------------------------


class _LifecycleRepositoryTestCase(_RecoveryRepositoryTestCase):
    def setUp(self):
        super().setUp()
        self.trader = create_trader(self.connection, self.source.id, "TC")
        self.connection.commit()

    def _make_signal(
        self,
        symbol="IBM",
        option_type="call",
        strike=None,
        expiration="2026-07-24",
        event_type="ENTRY",
        qualifier=None,
        action="BOUGHT",
        received_at=None,
        extraction_id=None,
        price=None,
        raw_text="x",
    ):
        raw_message = create_raw_message(
            self.connection, self.source.id, raw_text, received_at=received_at
        )
        signal = create_trade_signal(
            self.connection,
            raw_message.id,
            self.trader.id,
            symbol,
            action,
            option_type=option_type,
            strike=strike,
            expiration=expiration,
            event_type=event_type,
            qualifier=qualifier,
            extraction_id=extraction_id,
            price=price,
        )
        self.connection.commit()
        return signal, raw_message


class LifecycleKeyMatchingTests(_LifecycleRepositoryTestCase):
    def test_exact_key_match_returns_only_matching_signals(self):
        matching, _ = self._make_signal(strike=Decimal("207.5"))
        self._make_signal(symbol="AVGO", strike=Decimal("380"), option_type="put")

        results = get_current_trade_signals_for_key(
            self.connection, self.trader.id, "IBM", "call", Decimal("207.5"), "2026-07-24"
        )

        self.assertEqual([s.trade_signal_id for s in results], [matching.id])

    def test_equity_key_with_null_option_fields(self):
        signal, _ = self._make_signal(
            symbol="AAPL", option_type=None, strike=None, expiration=None
        )

        results = get_current_trade_signals_for_key(
            self.connection, self.trader.id, "AAPL", None, None, None
        )

        self.assertEqual([s.trade_signal_id for s in results], [signal.id])

    def test_symbol_case_insensitive_matching(self):
        signal, _ = self._make_signal(symbol="ibm", strike=Decimal("207.5"))

        results = get_current_trade_signals_for_key(
            self.connection, self.trader.id, "IBM", "call", Decimal("207.5"), "2026-07-24"
        )

        self.assertEqual([s.trade_signal_id for s in results], [signal.id])

    def test_decimal_equivalent_strikes_match(self):
        signal, _ = self._make_signal(strike=Decimal("207.50"))

        results = get_current_trade_signals_for_key(
            self.connection, self.trader.id, "IBM", "call", Decimal("207.5"), "2026-07-24"
        )

        self.assertEqual([s.trade_signal_id for s in results], [signal.id])

    def test_current_extraction_included(self):
        raw_message = create_raw_message(self.connection, self.source.id, "x")
        self.connection.commit()
        extraction = create_message_extraction(
            self.connection, raw_message.id, parser_version="v2", parse_status="parsed"
        )
        self.connection.commit()
        signal = create_trade_signal(
            self.connection,
            raw_message.id,
            self.trader.id,
            "IBM",
            "BOUGHT",
            option_type="call",
            strike=Decimal("207.5"),
            expiration="2026-07-24",
            event_type="ENTRY",
            extraction_id=extraction.id,
        )
        self.connection.commit()

        results = get_current_trade_signals_for_key(
            self.connection, self.trader.id, "IBM", "call", Decimal("207.5"), "2026-07-24"
        )

        self.assertEqual([s.trade_signal_id for s in results], [signal.id])

    def test_superseded_extraction_excluded(self):
        raw_message = create_raw_message(self.connection, self.source.id, "x")
        self.connection.commit()
        extraction = create_message_extraction(
            self.connection, raw_message.id, parser_version="v2", parse_status="parsed"
        )
        self.connection.commit()
        create_trade_signal(
            self.connection,
            raw_message.id,
            self.trader.id,
            "IBM",
            "BOUGHT",
            option_type="call",
            strike=Decimal("207.5"),
            expiration="2026-07-24",
            event_type="ENTRY",
            extraction_id=extraction.id,
        )
        self.connection.commit()
        supersede_extraction(self.connection, extraction.id)
        self.connection.commit()

        results = get_current_trade_signals_for_key(
            self.connection, self.trader.id, "IBM", "call", Decimal("207.5"), "2026-07-24"
        )

        self.assertEqual(results, [])

    def test_legacy_null_extraction_id_treated_as_current(self):
        signal, _ = self._make_signal(strike=Decimal("207.5"), extraction_id=None)

        results = get_current_trade_signals_for_key(
            self.connection, self.trader.id, "IBM", "call", Decimal("207.5"), "2026-07-24"
        )

        self.assertEqual([s.trade_signal_id for s in results], [signal.id])

    def test_event_type_null_signal_excluded(self):
        self._make_signal(strike=Decimal("207.5"), event_type=None)

        results = get_current_trade_signals_for_key(
            self.connection, self.trader.id, "IBM", "call", Decimal("207.5"), "2026-07-24"
        )

        self.assertEqual(results, [])

    def test_deterministic_ordering_with_timestamps(self):
        second, _ = self._make_signal(
            strike=Decimal("207.5"),
            event_type="PARTIAL_EXIT",
            qualifier="1/2",
            action="SOLD",
            received_at="2026-07-24T05:00:00.000000+00:00",
        )
        first, _ = self._make_signal(
            strike=Decimal("207.5"), received_at="2026-07-24T04:00:00.000000+00:00"
        )

        results = get_current_trade_signals_for_key(
            self.connection, self.trader.id, "IBM", "call", Decimal("207.5"), "2026-07-24"
        )

        self.assertEqual([s.trade_signal_id for s in results], [first.id, second.id])
        self.assertEqual(
            results[0].ordering_key,
            ("2026-07-24T04:00:00.000000+00:00", results[0].raw_message_id, first.id),
        )

    def test_deterministic_fallback_ordering_when_any_timestamp_missing(self):
        # s1 has a LATER received_at than s3, but s2 (inserted between
        # them) has no received_at at all - forcing the whole set to fall
        # back to (raw_message_id, trade_signal_id) insertion-order
        # ordering, which does NOT match what a received_at-based sort
        # would have given (s3 before s1) - proving the fallback truly
        # overrides per-row timestamps once any sibling in the set lacks
        # one.
        s1, _ = self._make_signal(
            strike=Decimal("207.5"), received_at="2026-07-24T10:00:00.000000+00:00"
        )
        s2, _ = self._make_signal(
            strike=Decimal("207.5"),
            event_type="PARTIAL_EXIT",
            qualifier="1/2",
            action="SOLD",
            received_at=None,
        )
        s3, _ = self._make_signal(
            strike=Decimal("207.5"),
            event_type="FULL_EXIT",
            qualifier="ALL OUT",
            action="SOLD",
            received_at="2026-07-24T05:00:00.000000+00:00",
        )

        results = get_current_trade_signals_for_key(
            self.connection, self.trader.id, "IBM", "call", Decimal("207.5"), "2026-07-24"
        )

        self.assertEqual([s.trade_signal_id for s in results], [s1.id, s2.id, s3.id])
        for snapshot in results:
            self.assertEqual(len(snapshot.ordering_key), 2)

    def test_no_match_returns_empty_list(self):
        self.assertEqual(
            get_current_trade_signals_for_key(
                self.connection, self.trader.id, "NOPE", None, None, None
            ),
            [],
        )


class DistinctLifecycleKeysTests(_LifecycleRepositoryTestCase):
    def test_returns_distinct_keys_for_given_signal_ids(self):
        a, _ = self._make_signal(symbol="IBM", strike=Decimal("207.5"))
        b, _ = self._make_signal(
            symbol="IBM",
            strike=Decimal("207.5"),
            event_type="PARTIAL_EXIT",
            qualifier="1/2",
            action="SOLD",
        )
        c, _ = self._make_signal(symbol="AVGO", option_type="put", strike=Decimal("380"))

        keys = get_distinct_lifecycle_keys_for_signal_ids(self.connection, [a.id, b.id, c.id])

        self.assertEqual(
            set(keys),
            {
                (self.trader.id, "IBM", "call", Decimal("207.5"), "2026-07-24"),
                (self.trader.id, "AVGO", "put", Decimal("380"), "2026-07-24"),
            },
        )

    def test_incomplete_option_identity_excluded(self):
        complete, _ = self._make_signal(strike=Decimal("207.5"))
        raw_message = create_raw_message(self.connection, self.source.id, "y")
        self.connection.commit()
        incomplete = create_trade_signal(
            self.connection,
            raw_message.id,
            self.trader.id,
            "IBM",
            "BOUGHT",
            option_type="call",
            strike=None,  # missing strike - option_type/expiration present
            expiration="2026-07-24",
            event_type="ENTRY",
        )
        self.connection.commit()

        keys = get_distinct_lifecycle_keys_for_signal_ids(
            self.connection, [complete.id, incomplete.id]
        )

        self.assertEqual(
            keys, [(self.trader.id, "IBM", "call", Decimal("207.5"), "2026-07-24")]
        )

    def test_equity_signals_included(self):
        signal, _ = self._make_signal(
            symbol="AAPL", option_type=None, strike=None, expiration=None
        )

        keys = get_distinct_lifecycle_keys_for_signal_ids(self.connection, [signal.id])

        self.assertEqual(keys, [(self.trader.id, "AAPL", None, None, None)])

    def test_empty_signal_ids_returns_empty_list(self):
        self.assertEqual(get_distinct_lifecycle_keys_for_signal_ids(self.connection, []), [])


class CurrentSignalSnapshotAndPositionTests(_LifecycleRepositoryTestCase):
    def test_get_current_signal_snapshot_for_raw_message_found(self):
        signal, raw_message = self._make_signal(strike=Decimal("207.5"))

        snapshot = get_current_signal_snapshot_for_raw_message(self.connection, raw_message.id)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.trade_signal_id, signal.id)
        self.assertEqual(snapshot.raw_message_id, raw_message.id)

    def test_get_current_signal_snapshot_for_raw_message_missing(self):
        raw_message = create_raw_message(self.connection, self.source.id, "no signal")
        self.connection.commit()

        self.assertIsNone(
            get_current_signal_snapshot_for_raw_message(self.connection, raw_message.id)
        )

    def test_get_current_signal_snapshot_excludes_superseded(self):
        raw_message = create_raw_message(self.connection, self.source.id, "x")
        self.connection.commit()
        extraction = create_message_extraction(
            self.connection, raw_message.id, parser_version="v2", parse_status="parsed"
        )
        self.connection.commit()
        create_trade_signal(
            self.connection,
            raw_message.id,
            self.trader.id,
            "IBM",
            "BOUGHT",
            option_type="call",
            strike=Decimal("207.5"),
            expiration="2026-07-24",
            event_type="ENTRY",
            extraction_id=extraction.id,
        )
        self.connection.commit()
        supersede_extraction(self.connection, extraction.id)
        self.connection.commit()

        self.assertIsNone(
            get_current_signal_snapshot_for_raw_message(self.connection, raw_message.id)
        )

    def test_multiple_current_signals_for_one_raw_message_raises(self):
        # Should never legitimately happen (see the function's own
        # docstring), but must fail closed rather than silently picking
        # an arbitrary one via fetchone().
        raw_message = create_raw_message(self.connection, self.source.id, "dup")
        self.connection.commit()
        first = create_trade_signal(
            self.connection,
            raw_message.id,
            self.trader.id,
            "IBM",
            "BOUGHT",
            option_type="call",
            strike=Decimal("207.5"),
            expiration="2026-07-24",
            event_type="ENTRY",
        )
        second = create_trade_signal(
            self.connection,
            raw_message.id,
            self.trader.id,
            "IBM",
            "BOUGHT",
            option_type="call",
            strike=Decimal("207.5"),
            expiration="2026-07-24",
            event_type="ENTRY",
        )
        self.connection.commit()

        with self.assertRaises(ValueError) as ctx:
            get_current_signal_snapshot_for_raw_message(self.connection, raw_message.id)

        message = str(ctx.exception)
        self.assertIn(str(raw_message.id), message)
        self.assertIn(str(first.id), message)
        self.assertIn(str(second.id), message)


class ChronologicalPositionsForRawMessagesTests(_LifecycleRepositoryTestCase):
    def test_all_timestamped_set_uses_timestamp_ordering(self):
        _, raw_a = self._make_signal(
            strike=Decimal("207.5"), received_at="2026-07-24T04:00:00.000000+00:00"
        )
        _, raw_b = self._make_signal(
            strike=Decimal("207.5"),
            event_type="PARTIAL_EXIT",
            qualifier="1/2",
            action="SOLD",
            received_at="2026-07-24T05:00:00.000000+00:00",
        )

        positions = get_chronological_positions_for_raw_messages(
            self.connection, [raw_a.id, raw_b.id]
        )

        self.assertEqual(
            positions[raw_a.id], ("2026-07-24T04:00:00.000000+00:00", raw_a.id)
        )
        self.assertEqual(
            positions[raw_b.id], ("2026-07-24T05:00:00.000000+00:00", raw_b.id)
        )
        self.assertLess(positions[raw_a.id], positions[raw_b.id])

    def test_mixed_set_uses_raw_message_id_ordering_for_every_member(self):
        _, raw_a = self._make_signal(
            strike=Decimal("207.5"), received_at="2026-07-24T04:00:00.000000+00:00"
        )
        _, raw_b = self._make_signal(
            strike=Decimal("207.5"),
            event_type="PARTIAL_EXIT",
            qualifier="1/2",
            action="SOLD",
            received_at=None,
        )

        positions = get_chronological_positions_for_raw_messages(
            self.connection, [raw_a.id, raw_b.id]
        )

        # Both fall back to (raw_message_id,) - never a mix of a
        # timestamp-based tuple for raw_a and a different-shaped
        # fallback tuple for raw_b.
        self.assertEqual(positions[raw_a.id], (raw_a.id,))
        self.assertEqual(positions[raw_b.id], (raw_b.id,))

    def test_later_missing_timestamp_message_not_falsely_sorted_before_earlier_timestamped_one(
        self,
    ):
        # raw_a: earliest insertion, WITH a received_at.
        # raw_b: later insertion, WITHOUT a received_at.
        # raw_c: even later insertion, WITH a received_at that - if it
        # were actually used - would sort raw_c BEFORE raw_a. Because
        # raw_b lacks a timestamp, the whole set must fall back to
        # insertion order for all three, so raw_c must NOT be sorted
        # before raw_a despite its earlier-looking timestamp, and raw_b
        # must NOT be sorted before raw_a just because it has no
        # timestamp at all.
        _, raw_a = self._make_signal(
            strike=Decimal("207.5"), received_at="2026-07-24T10:00:00.000000+00:00"
        )
        _, raw_b = self._make_signal(
            strike=Decimal("207.5"),
            event_type="PARTIAL_EXIT",
            qualifier="1/2",
            action="SOLD",
            received_at=None,
        )
        _, raw_c = self._make_signal(
            strike=Decimal("207.5"),
            event_type="FULL_EXIT",
            qualifier="ALL OUT",
            action="SOLD",
            received_at="2026-07-24T05:00:00.000000+00:00",
        )

        positions = get_chronological_positions_for_raw_messages(
            self.connection, [raw_a.id, raw_b.id, raw_c.id]
        )

        ordered_ids = sorted([raw_a.id, raw_b.id, raw_c.id], key=lambda i: positions[i])
        self.assertEqual(ordered_ids, [raw_a.id, raw_b.id, raw_c.id])

    def test_reversed_input_id_order_produces_the_same_mapping(self):
        _, raw_a = self._make_signal(
            strike=Decimal("207.5"), received_at="2026-07-24T04:00:00.000000+00:00"
        )
        _, raw_b = self._make_signal(
            strike=Decimal("207.5"),
            event_type="PARTIAL_EXIT",
            qualifier="1/2",
            action="SOLD",
            received_at="2026-07-24T05:00:00.000000+00:00",
        )

        forward = get_chronological_positions_for_raw_messages(
            self.connection, [raw_a.id, raw_b.id]
        )
        reversed_order = get_chronological_positions_for_raw_messages(
            self.connection, [raw_b.id, raw_a.id]
        )

        # Equal as mappings...
        self.assertEqual(forward, reversed_order)
        # ...and identical in key iteration order too - the input
        # argument's order must never leak into the returned dict's
        # insertion/iteration order.
        self.assertEqual(list(forward.keys()), list(reversed_order.keys()))
        self.assertEqual(list(forward.keys()), sorted([raw_a.id, raw_b.id]))

    def test_key_iteration_order_is_always_ascending_raw_message_id(self):
        # Three signals, deliberately requested in a scrambled order that
        # matches neither ascending nor descending id order, to prove the
        # returned dict's key order is canonically ascending regardless
        # of the caller's input order.
        _, raw_a = self._make_signal(
            strike=Decimal("207.5"), received_at="2026-07-24T04:00:00.000000+00:00"
        )
        _, raw_b = self._make_signal(
            strike=Decimal("207.5"),
            event_type="PARTIAL_EXIT",
            qualifier="1/2",
            action="SOLD",
            received_at="2026-07-24T05:00:00.000000+00:00",
        )
        _, raw_c = self._make_signal(
            strike=Decimal("207.5"),
            event_type="FULL_EXIT",
            qualifier="ALL OUT",
            action="SOLD",
            received_at="2026-07-24T06:00:00.000000+00:00",
        )
        unique_requested_ids = [raw_b.id, raw_c.id, raw_a.id]

        positions = get_chronological_positions_for_raw_messages(
            self.connection, unique_requested_ids
        )

        self.assertEqual(list(positions.keys()), sorted(unique_requested_ids))

    def test_duplicate_input_ids_produce_one_canonical_key_entry(self):
        _, raw_a = self._make_signal(
            strike=Decimal("207.5"), received_at="2026-07-24T04:00:00.000000+00:00"
        )
        _, raw_b = self._make_signal(
            strike=Decimal("207.5"),
            event_type="PARTIAL_EXIT",
            qualifier="1/2",
            action="SOLD",
            received_at="2026-07-24T05:00:00.000000+00:00",
        )

        positions = get_chronological_positions_for_raw_messages(
            self.connection, [raw_a.id, raw_b.id, raw_a.id, raw_b.id, raw_a.id]
        )

        self.assertEqual(list(positions.keys()), sorted([raw_a.id, raw_b.id]))
        self.assertEqual(len(positions), 2)

    def test_missing_raw_message_ids_raise_value_error_listing_them(self):
        _, raw_a = self._make_signal(strike=Decimal("207.5"))
        self.connection.commit()

        with self.assertRaises(ValueError) as ctx:
            get_chronological_positions_for_raw_messages(
                self.connection, [raw_a.id, 999999, 888888]
            )

        message = str(ctx.exception)
        self.assertIn("999999", message)
        self.assertIn("888888", message)

    def test_empty_input_returns_empty_dict(self):
        self.assertEqual(
            get_chronological_positions_for_raw_messages(self.connection, []), {}
        )


class LifecycleGenerationCrudTests(_LifecycleRepositoryTestCase):
    def test_create_trade_lifecycle_defaults_and_fields(self):
        lifecycle = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open", remaining_fraction="1"
        )
        self.connection.commit()

        self.assertIsInstance(lifecycle, TradeLifecycle)
        self.assertTrue(lifecycle.is_current)
        self.assertIsNone(lifecycle.superseded_at)
        self.assertIsNotNone(lifecycle.id)

    def test_create_trade_lifecycle_rejects_invalid_status(self):
        with self.assertRaises(ValueError):
            create_trade_lifecycle(
                self.connection, self.trader.id, "IBM", status="bogus", remaining_fraction="1"
            )

    def test_create_trade_lifecycle_stores_ambiguity_flags(self):
        lifecycle = create_trade_lifecycle(
            self.connection,
            self.trader.id,
            "IBM",
            status="unresolved",
            remaining_fraction="0",
            ambiguity_flags=["ambiguous_add_no_open_position"],
        )
        self.connection.commit()

        self.assertEqual(lifecycle.ambiguity_flags, ["ambiguous_add_no_open_position"])

    def test_supersede_trade_lifecycle_only_touches_bookkeeping_fields(self):
        lifecycle = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open", remaining_fraction="1"
        )
        self.connection.commit()

        superseded = supersede_trade_lifecycle(self.connection, lifecycle.id)
        self.connection.commit()

        self.assertFalse(superseded.is_current)
        self.assertIsNotNone(superseded.superseded_at)
        self.assertEqual(superseded.status, "open")
        self.assertEqual(superseded.remaining_fraction, "1")

    def test_supersede_missing_lifecycle_returns_none(self):
        self.assertIsNone(supersede_trade_lifecycle(self.connection, 999999))

    def test_create_trade_lifecycle_event_and_get_ordering(self):
        lifecycle = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="closed", remaining_fraction="0"
        )
        entry, entry_raw = self._make_signal(strike=Decimal("207.5"))
        exit_, exit_raw = self._make_signal(
            strike=Decimal("207.5"), event_type="FULL_EXIT", qualifier="ALL OUT", action="SOLD"
        )
        self.connection.commit()

        entry_snapshot = get_current_signal_snapshot_for_raw_message(
            self.connection, entry_raw.id
        )
        exit_snapshot = get_current_signal_snapshot_for_raw_message(self.connection, exit_raw.id)

        create_trade_lifecycle_event(
            self.connection, lifecycle.id, entry.id, 1, build_signal_snapshot_json(entry_snapshot)
        )
        create_trade_lifecycle_event(
            self.connection, lifecycle.id, exit_.id, 2, build_signal_snapshot_json(exit_snapshot)
        )
        self.connection.commit()

        events = get_trade_lifecycle_events(self.connection, lifecycle.id)

        self.assertEqual([e.trade_signal_id for e in events], [entry.id, exit_.id])
        self.assertEqual([e.sequence_index for e in events], [1, 2])

    def test_create_trade_lifecycle_event_rejects_empty_snapshot(self):
        lifecycle = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open", remaining_fraction="1"
        )
        signal, _ = self._make_signal(strike=Decimal("207.5"))
        self.connection.commit()

        with self.assertRaises(ValueError):
            create_trade_lifecycle_event(self.connection, lifecycle.id, signal.id, 1, "")

    def test_duplicate_membership_raises_integrity_error(self):
        lifecycle = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open", remaining_fraction="1"
        )
        signal, _ = self._make_signal(strike=Decimal("207.5"))
        self.connection.commit()
        create_trade_lifecycle_event(self.connection, lifecycle.id, signal.id, 1, "{}")
        self.connection.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            create_trade_lifecycle_event(self.connection, lifecycle.id, signal.id, 2, "{}")

    def test_signal_snapshot_is_canonical_and_deterministic(self):
        _, raw_message = self._make_signal(strike=Decimal("207.5"), qualifier="[SMALL]")
        snapshot = get_current_signal_snapshot_for_raw_message(self.connection, raw_message.id)

        json_a = build_signal_snapshot_json(snapshot)
        json_b = build_signal_snapshot_json(snapshot)

        self.assertEqual(json_a, json_b)
        payload = json.loads(json_a)
        for field in (
            "trade_signal_id", "raw_message_id", "trader_id", "symbol", "option_type",
            "strike", "expiration", "event_type", "qualifier", "action", "price",
            "stated_entry_price", "stated_return_pct", "notes", "extraction_id",
            "ordering_key",
        ):
            self.assertIn(field, payload)

    def test_signal_snapshot_immutable_after_later_correction(self):
        signal, raw_message = self._make_signal(strike=Decimal("207.5"), price=Decimal("1.00"))
        snapshot = get_current_signal_snapshot_for_raw_message(self.connection, raw_message.id)
        lifecycle = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open", remaining_fraction="1"
        )
        create_trade_lifecycle_event(
            self.connection, lifecycle.id, signal.id, 1, build_signal_snapshot_json(snapshot)
        )
        self.connection.commit()

        # A later correction to the live signal's price must never alter
        # the already-stored snapshot - this is exactly the bug the
        # immutable-snapshot mechanism exists to prevent.
        update_trade_signal(self.connection, signal.id, price=Decimal("9.99"))
        self.connection.commit()

        stored_event = get_trade_lifecycle_events(self.connection, lifecycle.id)[0]
        stored_payload = json.loads(stored_event.signal_snapshot)
        self.assertEqual(stored_payload["price"], "1.00")

        live_signal = get_trade_signal_by_id(self.connection, signal.id)
        self.assertEqual(live_signal.price, "9.99")


class LifecyclePointerTests(_LifecycleRepositoryTestCase):
    def test_update_trade_signal_lifecycle_pointer_sets_and_clears(self):
        signal, _ = self._make_signal(strike=Decimal("207.5"))
        lifecycle = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open", remaining_fraction="1"
        )
        self.connection.commit()

        update_trade_signal_lifecycle_pointer(self.connection, signal.id, lifecycle.id)
        self.connection.commit()
        self.assertEqual(
            get_trade_signal_by_id(self.connection, signal.id).lifecycle_id, lifecycle.id
        )

        update_trade_signal_lifecycle_pointer(self.connection, signal.id, None)
        self.connection.commit()
        self.assertIsNone(get_trade_signal_by_id(self.connection, signal.id).lifecycle_id)

    def test_clear_lifecycle_pointers_for_generation_only_clears_matching(self):
        signal_a, _ = self._make_signal(strike=Decimal("207.5"))
        signal_b, _ = self._make_signal(
            strike=Decimal("207.5"),
            event_type="PARTIAL_EXIT",
            qualifier="1/2",
            action="SOLD",
        )
        lifecycle_1 = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open", remaining_fraction="1"
        )
        lifecycle_2 = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open", remaining_fraction="1"
        )
        self.connection.commit()
        update_trade_signal_lifecycle_pointer(self.connection, signal_a.id, lifecycle_1.id)
        update_trade_signal_lifecycle_pointer(self.connection, signal_b.id, lifecycle_2.id)
        self.connection.commit()

        cleared_count = clear_lifecycle_pointers_for_generation(self.connection, lifecycle_1.id)
        self.connection.commit()

        self.assertEqual(cleared_count, 1)
        self.assertIsNone(get_trade_signal_by_id(self.connection, signal_a.id).lifecycle_id)
        self.assertEqual(
            get_trade_signal_by_id(self.connection, signal_b.id).lifecycle_id, lifecycle_2.id
        )

    def test_clear_lifecycle_pointers_for_nonexistent_generation_returns_zero(self):
        self.assertEqual(clear_lifecycle_pointers_for_generation(self.connection, 999999), 0)


class LifecycleGenerationReadTests(_LifecycleRepositoryTestCase):
    def test_get_current_lifecycles_for_key_excludes_superseded(self):
        current = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open", remaining_fraction="1"
        )
        superseded_source = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="closed", remaining_fraction="0"
        )
        self.connection.commit()
        supersede_trade_lifecycle(self.connection, superseded_source.id)
        self.connection.commit()

        results = get_current_lifecycles_for_key(
            self.connection, self.trader.id, "IBM", None, None, None
        )

        ids = {r.id for r in results}
        self.assertIn(current.id, ids)
        self.assertNotIn(superseded_source.id, ids)

    def test_lineage_lookup_across_multiple_current_terminal_generations_same_key(self):
        # spacemonkey SPX 7430P shape: two independent closed generations
        # at the identical key, both still current, with disjoint
        # lineage sets.
        entry_1, raw_1 = self._make_signal(
            symbol="SPX", option_type="put", strike=Decimal("7430")
        )
        exit_1, raw_2 = self._make_signal(
            symbol="SPX",
            option_type="put",
            strike=Decimal("7430"),
            event_type="FULL_EXIT",
            qualifier="ALL OUT",
            action="SOLD",
        )
        entry_2, raw_3 = self._make_signal(
            symbol="SPX", option_type="put", strike=Decimal("7430")
        )
        exit_2, raw_4 = self._make_signal(
            symbol="SPX",
            option_type="put",
            strike=Decimal("7430"),
            event_type="FULL_EXIT",
            qualifier="ALL OUT",
            action="SOLD",
        )

        generation_1 = create_trade_lifecycle(
            self.connection,
            self.trader.id,
            "SPX",
            status="closed",
            remaining_fraction="0",
            option_type="put",
            strike=Decimal("7430"),
            expiration="2026-07-24",
            opened_by_signal_id=entry_1.id,
            closed_by_signal_id=exit_1.id,
        )
        generation_2 = create_trade_lifecycle(
            self.connection,
            self.trader.id,
            "SPX",
            status="closed",
            remaining_fraction="0",
            option_type="put",
            strike=Decimal("7430"),
            expiration="2026-07-24",
            opened_by_signal_id=entry_2.id,
            closed_by_signal_id=exit_2.id,
        )
        self.connection.commit()

        for lifecycle_id, entry, exit_, raw_entry, raw_exit in (
            (generation_1.id, entry_1, exit_1, raw_1, raw_2),
            (generation_2.id, entry_2, exit_2, raw_3, raw_4),
        ):
            entry_snapshot = get_current_signal_snapshot_for_raw_message(
                self.connection, raw_entry.id
            )
            exit_snapshot = get_current_signal_snapshot_for_raw_message(
                self.connection, raw_exit.id
            )
            create_trade_lifecycle_event(
                self.connection, lifecycle_id, entry.id, 1,
                build_signal_snapshot_json(entry_snapshot),
            )
            create_trade_lifecycle_event(
                self.connection, lifecycle_id, exit_.id, 2,
                build_signal_snapshot_json(exit_snapshot),
            )
        self.connection.commit()

        lineage_1 = get_trade_lifecycle_lineage_raw_message_ids(self.connection, generation_1.id)
        lineage_2 = get_trade_lifecycle_lineage_raw_message_ids(self.connection, generation_2.id)

        self.assertEqual(lineage_1, frozenset({raw_1.id, raw_2.id}))
        self.assertEqual(lineage_2, frozenset({raw_3.id, raw_4.id}))
        self.assertEqual(lineage_1 & lineage_2, frozenset())

        current_generations = get_current_lifecycles_for_key(
            self.connection, self.trader.id, "SPX", "put", Decimal("7430"), "2026-07-24"
        )
        self.assertEqual(
            {g.id for g in current_generations}, {generation_1.id, generation_2.id}
        )

    def test_get_recorded_shape_for_generation(self):
        lifecycle = create_trade_lifecycle(
            self.connection,
            self.trader.id,
            "IBM",
            status="closed",
            remaining_fraction="0",
            ambiguity_flags=["fraction_exceeds_remaining"],
        )
        entry, entry_raw = self._make_signal(strike=Decimal("207.5"))
        self.connection.commit()
        entry_snapshot = get_current_signal_snapshot_for_raw_message(
            self.connection, entry_raw.id
        )
        create_trade_lifecycle_event(
            self.connection, lifecycle.id, entry.id, 1, build_signal_snapshot_json(entry_snapshot)
        )
        self.connection.commit()

        shape = get_recorded_shape_for_generation(self.connection, lifecycle.id)

        self.assertEqual(
            shape,
            (("closed", "0", (entry.id,), ("fraction_exceeds_remaining",)),),
        )

    def test_get_recorded_shape_for_missing_generation_returns_none(self):
        self.assertIsNone(get_recorded_shape_for_generation(self.connection, 999999))

    def test_list_current_trade_lifecycles_filters_by_trader(self):
        create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open", remaining_fraction="1"
        )
        other_trader = create_trader(self.connection, self.source.id, "Sarang")
        self.connection.commit()
        create_trade_lifecycle(
            self.connection, other_trader.id, "AVGO", status="closed", remaining_fraction="0"
        )
        self.connection.commit()

        results = list_current_trade_lifecycles(self.connection, trader_name="TC")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["symbol"], "IBM")
        self.assertEqual(results[0]["trader_name"], "TC")

    def test_list_current_trade_lifecycles_excludes_superseded(self):
        lifecycle = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="closed", remaining_fraction="0"
        )
        self.connection.commit()
        supersede_trade_lifecycle(self.connection, lifecycle.id)
        self.connection.commit()

        self.assertEqual(list_current_trade_lifecycles(self.connection), [])

    def test_get_trade_lifecycle_history_rows_retains_superseded(self):
        first = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="orphan", remaining_fraction="1/2"
        )
        self.connection.commit()
        supersede_trade_lifecycle(self.connection, first.id)
        self.connection.commit()
        second = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="orphan", remaining_fraction="0"
        )
        self.connection.commit()

        history = get_trade_lifecycle_history_rows(
            self.connection, self.trader.id, "IBM", None, None, None
        )

        self.assertEqual([h.id for h in history], [second.id, first.id])
        self.assertFalse(history[1].is_current)


class PersistLifecycleBuildsTests(_LifecycleRepositoryTestCase):
    def test_persist_lifecycle_builds_inserts_rows_membership_and_pointers(self):
        entry, entry_raw = self._make_signal(strike=Decimal("207.5"))
        exit_, exit_raw = self._make_signal(
            strike=Decimal("207.5"), event_type="FULL_EXIT", qualifier="ALL OUT", action="SOLD"
        )
        self.connection.commit()

        ordered_snapshots = [
            get_current_signal_snapshot_for_raw_message(self.connection, entry_raw.id),
            get_current_signal_snapshot_for_raw_message(self.connection, exit_raw.id),
        ]
        builds = build_lifecycle_sequence(ordered_snapshots)
        snapshots_by_id = {s.trade_signal_id: s for s in ordered_snapshots}

        new_ids = persist_lifecycle_builds(
            self.connection,
            self.trader.id,
            "IBM",
            "call",
            Decimal("207.5"),
            "2026-07-24",
            builds,
            snapshots_by_id,
        )
        self.connection.commit()

        self.assertEqual(len(new_ids), 1)
        events = get_trade_lifecycle_events(self.connection, new_ids[0])
        self.assertEqual([e.trade_signal_id for e in events], [entry.id, exit_.id])
        self.assertEqual(
            get_trade_signal_by_id(self.connection, entry.id).lifecycle_id, new_ids[0]
        )
        self.assertEqual(
            get_trade_signal_by_id(self.connection, exit_.id).lifecycle_id, new_ids[0]
        )

        lifecycle = get_current_lifecycles_for_key(
            self.connection, self.trader.id, "IBM", "call", Decimal("207.5"), "2026-07-24"
        )[0]
        self.assertEqual(lifecycle.status, "closed")
        self.assertEqual(lifecycle.remaining_fraction, "0")

    def test_persist_lifecycle_builds_creates_no_duplicate_membership(self):
        entry, entry_raw = self._make_signal(strike=Decimal("207.5"))
        self.connection.commit()
        entry_snapshot = get_current_signal_snapshot_for_raw_message(
            self.connection, entry_raw.id
        )
        builds = build_lifecycle_sequence([entry_snapshot])
        snapshots_by_id = {entry_snapshot.trade_signal_id: entry_snapshot}

        new_ids = persist_lifecycle_builds(
            self.connection,
            self.trader.id,
            "IBM",
            "call",
            Decimal("207.5"),
            "2026-07-24",
            builds,
            snapshots_by_id,
        )
        self.connection.commit()

        count = self.connection.execute(
            "SELECT COUNT(*) FROM trade_lifecycle_events WHERE trade_lifecycle_id = ?",
            (new_ids[0],),
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_persist_lifecycle_builds_preserves_member_order(self):
        entry, entry_raw = self._make_signal(strike=Decimal("3"), symbol="MU")
        add, add_raw = self._make_signal(strike=Decimal("3"), symbol="MU", event_type="ADD")
        exit_, exit_raw = self._make_signal(
            strike=Decimal("3"),
            symbol="MU",
            event_type="PARTIAL_EXIT",
            qualifier="1/2",
            action="SOLD",
        )
        self.connection.commit()

        ordered_snapshots = [
            get_current_signal_snapshot_for_raw_message(self.connection, r.id)
            for r in (entry_raw, add_raw, exit_raw)
        ]
        builds = build_lifecycle_sequence(ordered_snapshots)
        snapshots_by_id = {s.trade_signal_id: s for s in ordered_snapshots}

        new_ids = persist_lifecycle_builds(
            self.connection,
            self.trader.id,
            "MU",
            "call",
            Decimal("3"),
            "2026-07-24",
            builds,
            snapshots_by_id,
        )
        self.connection.commit()

        events = get_trade_lifecycle_events(self.connection, new_ids[0])
        self.assertEqual([e.trade_signal_id for e in events], [entry.id, add.id, exit_.id])

    def test_persist_lifecycle_builds_empty_list_creates_nothing(self):
        new_ids = persist_lifecycle_builds(
            self.connection, self.trader.id, "IBM", "call", Decimal("207.5"), "2026-07-24", [], {}
        )
        self.connection.commit()

        self.assertEqual(new_ids, [])
        count = self.connection.execute("SELECT COUNT(*) FROM trade_lifecycles").fetchone()[0]
        self.assertEqual(count, 0)


class PersistLifecycleBuildsValidationTests(_LifecycleRepositoryTestCase):
    """Confirms persist_lifecycle_builds() validates the complete
    builds/snapshots_by_signal_id shape before performing any write -
    every rejection here must leave zero lifecycle rows, zero membership
    rows, and no lifecycle pointer changes, checked directly (no explicit
    rollback), proving nothing was ever written in the first place."""

    def _assert_nothing_persisted(self, signal_id):
        lifecycle_count = self.connection.execute(
            "SELECT COUNT(*) FROM trade_lifecycles"
        ).fetchone()[0]
        membership_count = self.connection.execute(
            "SELECT COUNT(*) FROM trade_lifecycle_events"
        ).fetchone()[0]
        self.assertEqual(lifecycle_count, 0)
        self.assertEqual(membership_count, 0)
        self.assertIsNone(get_trade_signal_by_id(self.connection, signal_id).lifecycle_id)

    def test_rejects_build_with_no_member_signals(self):
        empty_build = LifecycleBuild(
            status="unresolved",
            remaining_fraction="0",
            opened_by_signal_id=None,
            closed_by_signal_id=None,
            member_signal_ids=(),
        )

        with self.assertRaises(ValueError):
            persist_lifecycle_builds(
                self.connection,
                self.trader.id,
                "IBM",
                "call",
                Decimal("207.5"),
                "2026-07-24",
                [empty_build],
                {},
            )

        lifecycle_count = self.connection.execute(
            "SELECT COUNT(*) FROM trade_lifecycles"
        ).fetchone()[0]
        self.assertEqual(lifecycle_count, 0)

    def test_rejects_duplicate_signal_within_one_build(self):
        entry, entry_raw = self._make_signal(strike=Decimal("207.5"))
        self.connection.commit()
        snapshot = get_current_signal_snapshot_for_raw_message(self.connection, entry_raw.id)
        bad_build = LifecycleBuild(
            status="open",
            remaining_fraction="1",
            opened_by_signal_id=entry.id,
            closed_by_signal_id=None,
            member_signal_ids=(entry.id, entry.id),
        )

        with self.assertRaises(ValueError) as ctx:
            persist_lifecycle_builds(
                self.connection,
                self.trader.id,
                "IBM",
                "call",
                Decimal("207.5"),
                "2026-07-24",
                [bad_build],
                {entry.id: snapshot},
            )

        self.assertIn(str(entry.id), str(ctx.exception))
        self._assert_nothing_persisted(entry.id)

    def test_rejects_duplicate_signal_across_two_builds(self):
        entry, entry_raw = self._make_signal(strike=Decimal("207.5"))
        self.connection.commit()
        snapshot = get_current_signal_snapshot_for_raw_message(self.connection, entry_raw.id)
        build_1 = LifecycleBuild(
            status="open",
            remaining_fraction="1",
            opened_by_signal_id=entry.id,
            closed_by_signal_id=None,
            member_signal_ids=(entry.id,),
        )
        build_2 = LifecycleBuild(
            status="unresolved",
            remaining_fraction="0",
            opened_by_signal_id=None,
            closed_by_signal_id=None,
            member_signal_ids=(entry.id,),
        )

        with self.assertRaises(ValueError) as ctx:
            persist_lifecycle_builds(
                self.connection,
                self.trader.id,
                "IBM",
                "call",
                Decimal("207.5"),
                "2026-07-24",
                [build_1, build_2],
                {entry.id: snapshot},
            )

        self.assertIn(str(entry.id), str(ctx.exception))
        self._assert_nothing_persisted(entry.id)

    def test_rejects_missing_snapshot(self):
        entry, _ = self._make_signal(strike=Decimal("207.5"))
        self.connection.commit()
        build = LifecycleBuild(
            status="open",
            remaining_fraction="1",
            opened_by_signal_id=entry.id,
            closed_by_signal_id=None,
            member_signal_ids=(entry.id,),
        )

        with self.assertRaises(ValueError) as ctx:
            persist_lifecycle_builds(
                self.connection,
                self.trader.id,
                "IBM",
                "call",
                Decimal("207.5"),
                "2026-07-24",
                [build],
                {},  # no entry for entry.id at all
            )

        self.assertIn(str(entry.id), str(ctx.exception))
        self._assert_nothing_persisted(entry.id)

    def test_rejects_mismatched_snapshot_key(self):
        entry, _ = self._make_signal(strike=Decimal("207.5"))
        other, other_raw = self._make_signal(
            strike=Decimal("207.5"),
            event_type="PARTIAL_EXIT",
            qualifier="1/2",
            action="SOLD",
        )
        self.connection.commit()
        other_snapshot = get_current_signal_snapshot_for_raw_message(
            self.connection, other_raw.id
        )
        build = LifecycleBuild(
            status="open",
            remaining_fraction="1",
            opened_by_signal_id=entry.id,
            closed_by_signal_id=None,
            member_signal_ids=(entry.id,),
        )

        with self.assertRaises(ValueError) as ctx:
            persist_lifecycle_builds(
                self.connection,
                self.trader.id,
                "IBM",
                "call",
                Decimal("207.5"),
                "2026-07-24",
                [build],
                {entry.id: other_snapshot},  # key entry.id maps to a mismatched snapshot
            )

        self.assertIn(str(entry.id), str(ctx.exception))
        self._assert_nothing_persisted(entry.id)

    def test_rejects_mismatched_snapshot_entry_even_when_unreferenced_by_any_build(self):
        # `entry` and its snapshot are entirely valid and correctly
        # mapped, and `entry` IS the one and only member the (otherwise
        # perfectly valid) build references. The problem is a SECOND,
        # unrelated mapping entry in snapshots_by_signal_id whose key
        # does not match its own snapshot's trade_signal_id - and which
        # no build references at all. This must still be rejected: every
        # mapping entry is validated, not only ones a build happens to
        # use.
        entry, entry_raw = self._make_signal(strike=Decimal("207.5"))
        other, other_raw = self._make_signal(
            strike=Decimal("207.5"),
            event_type="PARTIAL_EXIT",
            qualifier="1/2",
            action="SOLD",
        )
        self.connection.commit()
        entry_snapshot = get_current_signal_snapshot_for_raw_message(
            self.connection, entry_raw.id
        )
        other_snapshot = get_current_signal_snapshot_for_raw_message(
            self.connection, other_raw.id
        )
        bogus_key = other.id + 999999

        build = LifecycleBuild(
            status="open",
            remaining_fraction="1",
            opened_by_signal_id=entry.id,
            closed_by_signal_id=None,
            member_signal_ids=(entry.id,),
        )
        snapshots_by_signal_id = {
            entry.id: entry_snapshot,  # correct, and referenced by `build`
            bogus_key: other_snapshot,  # wrong key, and never referenced at all
        }

        with self.assertRaises(ValueError) as ctx:
            persist_lifecycle_builds(
                self.connection,
                self.trader.id,
                "IBM",
                "call",
                Decimal("207.5"),
                "2026-07-24",
                [build],
                snapshots_by_signal_id,
            )

        self.assertIn(str(bogus_key), str(ctx.exception))

        lifecycle_count = self.connection.execute(
            "SELECT COUNT(*) FROM trade_lifecycles"
        ).fetchone()[0]
        membership_count = self.connection.execute(
            "SELECT COUNT(*) FROM trade_lifecycle_events"
        ).fetchone()[0]
        self.assertEqual(lifecycle_count, 0)
        self.assertEqual(membership_count, 0)
        self.assertIsNone(get_trade_signal_by_id(self.connection, entry.id).lifecycle_id)
        self.assertIsNone(get_trade_signal_by_id(self.connection, other.id).lifecycle_id)


class CreateLifecycleUnresolvedSingletonTests(_LifecycleRepositoryTestCase):
    def test_creates_unresolved_generation_with_pointer_set(self):
        signal, raw_message = self._make_signal(strike=Decimal("207.5"), event_type="ADD")
        self.connection.commit()
        snapshot = get_current_signal_snapshot_for_raw_message(self.connection, raw_message.id)

        lifecycle_id = create_lifecycle_unresolved_singleton(
            self.connection,
            self.trader.id,
            "IBM",
            "call",
            Decimal("207.5"),
            "2026-07-24",
            snapshot,
            "ambiguous_add_no_open_position",
        )
        self.connection.commit()

        shape = get_recorded_shape_for_generation(self.connection, lifecycle_id)
        self.assertEqual(
            shape,
            (("unresolved", "0", (signal.id,), ("ambiguous_add_no_open_position",)),),
        )
        self.assertEqual(
            get_trade_signal_by_id(self.connection, signal.id).lifecycle_id, lifecycle_id
        )


class LifecycleMembershipIntegrityTests(_LifecycleRepositoryTestCase):
    def test_valid_state_has_no_violations(self):
        self.assertEqual(validate_lifecycle_membership_integrity(self.connection), [])

    def test_invariant_a_detects_signal_in_two_current_lifecycles(self):
        signal, _ = self._make_signal(strike=Decimal("207.5"))
        lifecycle_1 = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open", remaining_fraction="1"
        )
        lifecycle_2 = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open", remaining_fraction="1"
        )
        self.connection.commit()
        self.connection.execute(
            "INSERT INTO trade_lifecycle_events "
            "(trade_lifecycle_id, trade_signal_id, sequence_index, signal_snapshot) "
            "VALUES (?, ?, 1, '{}')",
            (lifecycle_1.id, signal.id),
        )
        self.connection.execute(
            "INSERT INTO trade_lifecycle_events "
            "(trade_lifecycle_id, trade_signal_id, sequence_index, signal_snapshot) "
            "VALUES (?, ?, 1, '{}')",
            (lifecycle_2.id, signal.id),
        )
        self.connection.commit()

        violations = validate_lifecycle_membership_integrity(self.connection)

        self.assertTrue(any("Invariant A" in v for v in violations))

    def test_invariant_b_detects_pointer_disagreement(self):
        signal, _ = self._make_signal(strike=Decimal("207.5"))
        lifecycle = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open", remaining_fraction="1"
        )
        self.connection.commit()
        self.connection.execute(
            "INSERT INTO trade_lifecycle_events "
            "(trade_lifecycle_id, trade_signal_id, sequence_index, signal_snapshot) "
            "VALUES (?, ?, 1, '{}')",
            (lifecycle.id, signal.id),
        )
        # signal.lifecycle_id is deliberately left NULL, despite membership.
        self.connection.commit()

        violations = validate_lifecycle_membership_integrity(self.connection)

        self.assertTrue(any("Invariant B" in v for v in violations))

    def test_invariant_c_detects_pointer_to_noncurrent_lifecycle(self):
        signal, _ = self._make_signal(strike=Decimal("207.5"))
        lifecycle = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="closed", remaining_fraction="0"
        )
        self.connection.commit()
        update_trade_signal_lifecycle_pointer(self.connection, signal.id, lifecycle.id)
        self.connection.commit()
        # Deliberately supersede without clearing the pointer (unlike the
        # correct create_lifecycle_unresolved_singleton()/rebuild path).
        supersede_trade_lifecycle(self.connection, lifecycle.id)
        self.connection.commit()

        violations = validate_lifecycle_membership_integrity(self.connection)

        self.assertTrue(any("Invariant C" in v for v in violations))

    def test_invariant_d_detects_pointer_with_no_membership_row(self):
        signal, _ = self._make_signal(strike=Decimal("207.5"))
        lifecycle = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open", remaining_fraction="1"
        )
        self.connection.commit()
        update_trade_signal_lifecycle_pointer(self.connection, signal.id, lifecycle.id)
        self.connection.commit()
        # No trade_lifecycle_events row inserted at all.

        violations = validate_lifecycle_membership_integrity(self.connection)

        self.assertTrue(any("Invariant D" in v for v in violations))

    def test_invariant_e_f_detects_superseded_signal_in_current_lifecycle(self):
        raw_message = create_raw_message(self.connection, self.source.id, "x")
        self.connection.commit()
        extraction = create_message_extraction(
            self.connection, raw_message.id, parser_version="v2", parse_status="parsed"
        )
        self.connection.commit()
        signal = create_trade_signal(
            self.connection,
            raw_message.id,
            self.trader.id,
            "IBM",
            "BOUGHT",
            option_type="call",
            strike=Decimal("207.5"),
            expiration="2026-07-24",
            event_type="ENTRY",
            extraction_id=extraction.id,
        )
        self.connection.commit()
        lifecycle = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open", remaining_fraction="1"
        )
        self.connection.commit()
        self.connection.execute(
            "INSERT INTO trade_lifecycle_events "
            "(trade_lifecycle_id, trade_signal_id, sequence_index, signal_snapshot) "
            "VALUES (?, ?, 1, '{}')",
            (lifecycle.id, signal.id),
        )
        update_trade_signal_lifecycle_pointer(self.connection, signal.id, lifecycle.id)
        self.connection.commit()

        supersede_extraction(self.connection, extraction.id)
        self.connection.commit()

        violations = validate_lifecycle_membership_integrity(self.connection)

        self.assertTrue(any("Invariant E/F" in v for v in violations))

    def test_invariant_g_detects_legacy_signal_with_lifecycle_id(self):
        raw_message = create_raw_message(self.connection, self.source.id, "legacy")
        self.connection.commit()
        signal = create_trade_signal(
            self.connection, raw_message.id, self.trader.id, "SPY", "BTO"
        )  # event_type left None, matching every pre-Recovery ingest_message() signal
        self.connection.commit()
        lifecycle = create_trade_lifecycle(
            self.connection, self.trader.id, "SPY", status="open", remaining_fraction="1"
        )
        self.connection.commit()
        update_trade_signal_lifecycle_pointer(self.connection, signal.id, lifecycle.id)
        self.connection.commit()

        violations = validate_lifecycle_membership_integrity(self.connection)

        self.assertTrue(any("Invariant G" in v for v in violations))

    def test_invariant_h_detects_two_non_terminal_lifecycles_same_key(self):
        create_trade_lifecycle(
            self.connection,
            self.trader.id,
            "IBM",
            status="open",
            remaining_fraction="1",
            option_type="call",
            strike=Decimal("207.50"),
            expiration="2026-07-24",
        )
        create_trade_lifecycle(
            self.connection,
            self.trader.id,
            "IBM",
            status="partially_closed",
            remaining_fraction="1/2",
            option_type="call",
            strike=Decimal("207.5"),  # Decimal-equivalent to "207.50" above
            expiration="2026-07-24",
        )
        self.connection.commit()

        violations = validate_lifecycle_membership_integrity(self.connection)

        self.assertTrue(any("Invariant H" in v for v in violations))

    def test_two_terminal_lifecycles_same_key_do_not_violate_invariant_h(self):
        # Terminal generations (e.g. two independent closed re-entries)
        # may coexist at the same key - only non-terminal ones may not.
        create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="closed", remaining_fraction="0"
        )
        create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="orphan", remaining_fraction="0"
        )
        self.connection.commit()

        violations = validate_lifecycle_membership_integrity(self.connection)

        self.assertEqual([v for v in violations if "Invariant H" in v], [])

    def test_no_violation_for_properly_superseded_generation_with_retained_audit_rows(self):
        signal, _ = self._make_signal(strike=Decimal("207.5"))
        lifecycle = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open", remaining_fraction="1"
        )
        self.connection.commit()
        self.connection.execute(
            "INSERT INTO trade_lifecycle_events "
            "(trade_lifecycle_id, trade_signal_id, sequence_index, signal_snapshot) "
            "VALUES (?, ?, 1, '{}')",
            (lifecycle.id, signal.id),
        )
        update_trade_signal_lifecycle_pointer(self.connection, signal.id, lifecycle.id)
        self.connection.commit()

        # Properly superseded: pointer is also cleared, unlike the
        # deliberately-corrupted invariant-C fixture above.
        supersede_trade_lifecycle(self.connection, lifecycle.id)
        clear_lifecycle_pointers_for_generation(self.connection, lifecycle.id)
        self.connection.commit()

        self.assertEqual(validate_lifecycle_membership_integrity(self.connection), [])

        # The old membership row remains permanently queryable for audit.
        events = get_trade_lifecycle_events(self.connection, lifecycle.id)
        self.assertEqual(len(events), 1)


class DeterministicRepositoryOutputTests(_LifecycleRepositoryTestCase):
    """Confirms get_distinct_lifecycle_keys_for_signal_ids() and
    validate_lifecycle_membership_integrity() (including its private
    invariant-H helper) return a normalized, repeatable order - never raw
    set/dict iteration order or unordered SQL row-return order."""

    def test_distinct_lifecycle_keys_returned_in_deterministic_normalized_order(self):
        # Inserted deliberately out of alphabetical order: SPX, then
        # AVGO, then IBM.
        spx, _ = self._make_signal(symbol="SPX", option_type="put", strike=Decimal("7430"))
        avgo, _ = self._make_signal(symbol="AVGO", option_type="put", strike=Decimal("380"))
        ibm, _ = self._make_signal(symbol="IBM", strike=Decimal("207.5"))

        keys_forward = get_distinct_lifecycle_keys_for_signal_ids(
            self.connection, [spx.id, avgo.id, ibm.id]
        )
        keys_reversed_input = get_distinct_lifecycle_keys_for_signal_ids(
            self.connection, [ibm.id, avgo.id, spx.id]
        )

        # Same result regardless of the order signal ids were passed in.
        self.assertEqual(keys_forward, keys_reversed_input)
        # Normalized order, not insertion order (SPX was inserted first).
        symbols_in_order = [key[1] for key in keys_forward]
        self.assertEqual(symbols_in_order, sorted(symbols_in_order))

    def test_integrity_violations_deterministic_across_repeated_calls_and_insertion_orders(
        self,
    ):
        # Two separate, isolated Invariant G violations (a legacy,
        # event_type-NULL signal with a non-NULL lifecycle_id, and a
        # matching membership row so Invariant D does not also fire),
        # created in a deliberately scrambled (non-alphabetical, "ZZZ"
        # before "AAA") order.
        raw_z = create_raw_message(self.connection, self.source.id, "legacy-z")
        self.connection.commit()
        signal_z = create_trade_signal(
            self.connection, raw_z.id, self.trader.id, "ZZZ", "BTO"
        )
        self.connection.commit()
        lifecycle_z = create_trade_lifecycle(
            self.connection, self.trader.id, "ZZZ", status="open", remaining_fraction="1"
        )
        self.connection.commit()
        create_trade_lifecycle_event(self.connection, lifecycle_z.id, signal_z.id, 1, "{}")
        update_trade_signal_lifecycle_pointer(self.connection, signal_z.id, lifecycle_z.id)
        self.connection.commit()

        raw_a = create_raw_message(self.connection, self.source.id, "legacy-a")
        self.connection.commit()
        signal_a = create_trade_signal(
            self.connection, raw_a.id, self.trader.id, "AAA", "BTO"
        )
        self.connection.commit()
        lifecycle_a = create_trade_lifecycle(
            self.connection, self.trader.id, "AAA", status="open", remaining_fraction="1"
        )
        self.connection.commit()
        create_trade_lifecycle_event(self.connection, lifecycle_a.id, signal_a.id, 1, "{}")
        update_trade_signal_lifecycle_pointer(self.connection, signal_a.id, lifecycle_a.id)
        self.connection.commit()

        first_call = validate_lifecycle_membership_integrity(self.connection)
        second_call = validate_lifecycle_membership_integrity(self.connection)

        g_violations = [v for v in first_call if "Invariant G" in v]
        self.assertEqual(len(g_violations), 2)
        self.assertEqual(first_call, second_call)
        self.assertEqual(first_call, sorted(first_call))

    def test_invariant_h_violations_deterministic_regardless_of_insertion_order(self):
        # Two different violating keys (each two current non-terminal
        # lifecycles), created in a deliberately scrambled order.
        create_trade_lifecycle(
            self.connection, self.trader.id, "ZZZ", status="open", remaining_fraction="1"
        )
        create_trade_lifecycle(
            self.connection,
            self.trader.id,
            "ZZZ",
            status="partially_closed",
            remaining_fraction="1/2",
        )
        create_trade_lifecycle(
            self.connection, self.trader.id, "AAA", status="open", remaining_fraction="1"
        )
        create_trade_lifecycle(
            self.connection,
            self.trader.id,
            "AAA",
            status="partially_closed",
            remaining_fraction="1/2",
        )
        self.connection.commit()

        first_call = validate_lifecycle_membership_integrity(self.connection)
        second_call = validate_lifecycle_membership_integrity(self.connection)

        h_violations = [v for v in first_call if "Invariant H" in v]
        self.assertEqual(len(h_violations), 2)
        self.assertEqual(first_call, second_call)
        self.assertEqual(first_call, sorted(first_call))


class DeterministicOutputAcrossIsolatedInsertionOrderTests(unittest.TestCase):
    """Strengthens DeterministicRepositoryOutputTests above.

    Those tests all call the same database state repeatedly (or reorder
    only the caller's input-id argument), which cannot actually prove
    insertion-order independence - a single database's autoincrement ids
    are fixed the moment its rows are created, so "call twice against the
    same state" never exercises a genuinely different insertion order.
    These tests instead build two separate, isolated temporary databases,
    populate each with the same logical content in opposite insertion
    orders, and confirm both
    get_distinct_lifecycle_keys_for_signal_ids() and
    validate_lifecycle_membership_integrity() are driven by canonical key
    content, never by which database happened to insert which row first.
    """

    def _new_database(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        config = DatabaseConfig(db_path=path)
        initialize_database(config)
        connection = get_connection(config)
        source = get_or_create_source(connection, "discord")
        connection.commit()
        trader = create_trader(connection, source.id, "TC")
        connection.commit()
        # addCleanup runs registered callbacks in LIFO order, so
        # os.remove is registered first - it must run second, after the
        # connection is closed, or Windows refuses to delete the
        # still-open file.
        self.addCleanup(os.remove, path)
        self.addCleanup(connection.close)
        return connection, source, trader

    def _make_signal(
        self,
        connection,
        source,
        trader,
        symbol,
        option_type,
        strike,
        expiration="2026-07-24",
        event_type="ENTRY",
        qualifier=None,
        action="BOUGHT",
    ):
        raw_message = create_raw_message(connection, source.id, "x")
        signal = create_trade_signal(
            connection,
            raw_message.id,
            trader.id,
            symbol,
            action,
            option_type=option_type,
            strike=strike,
            expiration=expiration,
            event_type=event_type,
            qualifier=qualifier,
        )
        connection.commit()
        return signal, raw_message

    def test_distinct_lifecycle_keys_identical_across_opposite_insertion_orders(self):
        conn1, source1, trader1 = self._new_database()
        conn2, source2, trader2 = self._new_database()

        # Same three logical keys, inserted in opposite order across the
        # two isolated databases.
        specs = [
            ("SPX", "put", Decimal("7430")),
            ("AVGO", "put", Decimal("380")),
            ("IBM", "call", Decimal("207.5")),
        ]

        db1_signal_ids = []
        for symbol, option_type, strike in specs:
            signal, _ = self._make_signal(
                conn1, source1, trader1, symbol, option_type, strike
            )
            db1_signal_ids.append(signal.id)

        db2_signal_ids = []
        for symbol, option_type, strike in reversed(specs):
            signal, _ = self._make_signal(
                conn2, source2, trader2, symbol, option_type, strike
            )
            db2_signal_ids.append(signal.id)

        keys_db1 = get_distinct_lifecycle_keys_for_signal_ids(conn1, db1_signal_ids)
        keys_db2 = get_distinct_lifecycle_keys_for_signal_ids(conn2, db2_signal_ids)

        # Drop trader_id (index 0) before comparing - the two databases
        # are independent autoincrement sequences, so only the
        # symbol/option_type/strike/expiration shape is meaningfully
        # comparable across them.
        normalized_db1 = [key[1:] for key in keys_db1]
        normalized_db2 = [key[1:] for key in keys_db2]

        self.assertEqual(normalized_db1, normalized_db2)
        self.assertEqual(normalized_db1, sorted(normalized_db1))

    def test_integrity_violation_order_reflects_key_content_not_insertion_order(self):
        conn1, source1, trader1 = self._new_database()
        conn2, source2, trader2 = self._new_database()

        # Each database gets two Invariant-H-violating keys ("AAA" and
        # "ZZZ", each with two current non-terminal lifecycles), created
        # in opposite insertion order across the two databases.
        def _make_violating_pair(connection, trader, first_symbol, second_symbol):
            create_trade_lifecycle(
                connection, trader.id, first_symbol, status="open", remaining_fraction="1"
            )
            create_trade_lifecycle(
                connection,
                trader.id,
                first_symbol,
                status="partially_closed",
                remaining_fraction="1/2",
            )
            create_trade_lifecycle(
                connection, trader.id, second_symbol, status="open", remaining_fraction="1"
            )
            create_trade_lifecycle(
                connection,
                trader.id,
                second_symbol,
                status="partially_closed",
                remaining_fraction="1/2",
            )
            connection.commit()

        _make_violating_pair(conn1, trader1, "ZZZ", "AAA")
        _make_violating_pair(conn2, trader2, "AAA", "ZZZ")

        violations1 = validate_lifecycle_membership_integrity(conn1)
        violations2 = validate_lifecycle_membership_integrity(conn2)

        h_violations1 = [v for v in violations1 if "Invariant H" in v]
        h_violations2 = [v for v in violations2 if "Invariant H" in v]

        self.assertEqual(len(h_violations1), 2)
        self.assertEqual(len(h_violations2), 2)

        # Regardless of which database inserted "AAA" or "ZZZ" first,
        # the violation ordering must be driven by key content: "AAA"
        # always sorts before "ZZZ".
        self.assertIn("'AAA'", h_violations1[0])
        self.assertIn("'ZZZ'", h_violations1[1])
        self.assertIn("'AAA'", h_violations2[0])
        self.assertIn("'ZZZ'", h_violations2[1])


class R64RepositorySupportTests(_LifecycleRepositoryTestCase):
    """Focused tests for the five narrowly-scoped repository helpers added
    to support Recovery Milestone R6.4's TradeService orchestration:
    get_all_current_lifecycle_eligible_signal_ids(),
    get_current_incomplete_lifecycle_signal_snapshots(),
    get_trade_lifecycle_by_id(),
    get_current_lifecycle_ids_for_raw_message_ids(), and
    get_all_current_lifecycle_keys(). No R6.3 behavior is changed by this
    class - it covers only the new additions."""

    # -- get_all_current_lifecycle_eligible_signal_ids -----------------

    def test_all_eligible_signal_ids_empty_when_nothing_exists(self):
        self.assertEqual(get_all_current_lifecycle_eligible_signal_ids(self.connection), [])

    def test_all_eligible_signal_ids_includes_eligible_excludes_legacy(self):
        eligible, _ = self._make_signal(strike=Decimal("207.5"))
        raw = create_raw_message(self.connection, self.source.id, "legacy")
        self.connection.commit()
        legacy = create_trade_signal(self.connection, raw.id, self.trader.id, "IBM", "BTO")
        self.connection.commit()

        ids = get_all_current_lifecycle_eligible_signal_ids(self.connection)

        self.assertEqual(ids, [eligible.id])
        self.assertNotIn(legacy.id, ids)

    def test_all_eligible_signal_ids_excludes_superseded_extraction(self):
        raw_message = create_raw_message(self.connection, self.source.id, "x")
        self.connection.commit()
        extraction = create_message_extraction(
            self.connection, raw_message.id, parser_version="v2", parse_status="parsed"
        )
        self.connection.commit()
        signal = create_trade_signal(
            self.connection, raw_message.id, self.trader.id, "IBM", "BOUGHT",
            option_type="call", strike=Decimal("207.5"), expiration="2026-07-24",
            event_type="ENTRY", extraction_id=extraction.id,
        )
        self.connection.commit()
        supersede_extraction(self.connection, extraction.id)
        self.connection.commit()

        self.assertNotIn(signal.id, get_all_current_lifecycle_eligible_signal_ids(self.connection))

    def test_all_eligible_signal_ids_ascending_order(self):
        first, _ = self._make_signal(strike=Decimal("207.5"))
        second, _ = self._make_signal(symbol="AVGO", strike=Decimal("380"), option_type="put")

        ids = get_all_current_lifecycle_eligible_signal_ids(self.connection)

        self.assertEqual(ids, sorted([first.id, second.id]))

    # -- get_current_incomplete_lifecycle_signal_snapshots --------------

    def test_incomplete_signal_snapshots_empty_when_none_incomplete(self):
        self._make_signal(strike=Decimal("207.5"))
        self.assertEqual(get_current_incomplete_lifecycle_signal_snapshots(self.connection), [])

    def test_incomplete_signal_snapshots_finds_partial_option_identity(self):
        incomplete, _ = self._make_signal(option_type="call", strike=None, expiration=None)
        self._make_signal(symbol="AVGO", strike=Decimal("380"), option_type="put")

        snapshots = get_current_incomplete_lifecycle_signal_snapshots(self.connection)

        self.assertEqual([s.trade_signal_id for s in snapshots], [incomplete.id])

    def test_incomplete_signal_snapshots_excludes_legacy_signal(self):
        raw = create_raw_message(self.connection, self.source.id, "legacy")
        self.connection.commit()
        create_trade_signal(
            self.connection, raw.id, self.trader.id, "IBM", "BTO", option_type="call"
        )
        self.connection.commit()

        self.assertEqual(get_current_incomplete_lifecycle_signal_snapshots(self.connection), [])

    # -- get_current_incomplete_lifecycles --------------------------------

    def test_current_incomplete_lifecycles_empty_when_none_exist(self):
        self.assertEqual(get_current_incomplete_lifecycles(self.connection), [])

    def test_current_incomplete_lifecycles_excludes_complete_shape(self):
        create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open", remaining_fraction="1",
            option_type="call", strike=Decimal("207.5"), expiration="2026-07-24",
        )
        self.connection.commit()

        self.assertEqual(get_current_incomplete_lifecycles(self.connection), [])

    def test_current_incomplete_lifecycles_finds_incomplete_shape(self):
        incomplete = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="unresolved",
            remaining_fraction="0", option_type="call",
        )
        self.connection.commit()

        found = get_current_incomplete_lifecycles(self.connection)

        self.assertEqual([lc.id for lc in found], [incomplete.id])

    def test_current_incomplete_lifecycles_excludes_superseded(self):
        incomplete = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="unresolved",
            remaining_fraction="0", option_type="call",
        )
        self.connection.commit()
        supersede_trade_lifecycle(self.connection, incomplete.id)
        self.connection.commit()

        self.assertEqual(get_current_incomplete_lifecycles(self.connection), [])

    def test_current_incomplete_lifecycles_does_not_dedupe_identical_shapes(self):
        first = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="unresolved",
            remaining_fraction="0", option_type="call",
        )
        second = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="unresolved",
            remaining_fraction="0", option_type="call",
        )
        self.connection.commit()

        found_ids = {lc.id for lc in get_current_incomplete_lifecycles(self.connection)}

        self.assertEqual(found_ids, {first.id, second.id})

    # -- get_trade_lifecycle_by_id ---------------------------------------

    def test_get_trade_lifecycle_by_id_found(self):
        lifecycle = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open", remaining_fraction="1"
        )
        self.connection.commit()

        self.assertEqual(get_trade_lifecycle_by_id(self.connection, lifecycle.id), lifecycle)

    def test_get_trade_lifecycle_by_id_missing_returns_none(self):
        self.assertIsNone(get_trade_lifecycle_by_id(self.connection, 999999))

    # -- get_current_lifecycle_ids_for_raw_message_ids -------------------

    def test_current_lifecycle_ids_for_raw_message_ids_empty_input(self):
        self.assertEqual(get_current_lifecycle_ids_for_raw_message_ids(self.connection, []), [])

    def test_current_lifecycle_ids_for_raw_message_ids_finds_lineage(self):
        signal, raw = self._make_signal(strike=Decimal("207.5"))
        self.connection.commit()
        lifecycle = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open", remaining_fraction="1",
            option_type="call", strike=Decimal("207.5"), expiration="2026-07-24",
        )
        self.connection.commit()
        create_trade_lifecycle_event(self.connection, lifecycle.id, signal.id, 1, "{}")
        self.connection.commit()

        ids = get_current_lifecycle_ids_for_raw_message_ids(self.connection, [raw.id])

        self.assertEqual(ids, [lifecycle.id])

    def test_current_lifecycle_ids_for_raw_message_ids_excludes_superseded(self):
        signal, raw = self._make_signal(strike=Decimal("207.5"))
        self.connection.commit()
        lifecycle = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open", remaining_fraction="1"
        )
        self.connection.commit()
        create_trade_lifecycle_event(self.connection, lifecycle.id, signal.id, 1, "{}")
        supersede_trade_lifecycle(self.connection, lifecycle.id)
        self.connection.commit()

        ids = get_current_lifecycle_ids_for_raw_message_ids(self.connection, [raw.id])

        self.assertEqual(ids, [])

    def test_current_lifecycle_ids_for_raw_message_ids_unrelated_id_contributes_nothing(self):
        self._make_signal(strike=Decimal("207.5"))
        unrelated_raw = create_raw_message(self.connection, self.source.id, "y")
        self.connection.commit()

        ids = get_current_lifecycle_ids_for_raw_message_ids(self.connection, [unrelated_raw.id])

        self.assertEqual(ids, [])

    # -- get_all_current_lifecycle_keys -----------------------------------

    def test_all_current_lifecycle_keys_empty_when_none_exist(self):
        self.assertEqual(get_all_current_lifecycle_keys(self.connection), [])

    def test_all_current_lifecycle_keys_includes_stale_key_with_no_current_signal(self):
        create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="closed", remaining_fraction="0",
            option_type="call", strike=Decimal("207.5"), expiration="2026-07-24",
        )
        self.connection.commit()

        keys = get_all_current_lifecycle_keys(self.connection)

        self.assertEqual(keys, [(self.trader.id, "IBM", "call", Decimal("207.5"), "2026-07-24")])

    def test_all_current_lifecycle_keys_excludes_superseded(self):
        lifecycle = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open", remaining_fraction="1"
        )
        self.connection.commit()
        supersede_trade_lifecycle(self.connection, lifecycle.id)
        self.connection.commit()

        self.assertEqual(get_all_current_lifecycle_keys(self.connection), [])

    def test_all_current_lifecycle_keys_groups_decimal_equivalent_strikes(self):
        create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="closed", remaining_fraction="0",
            option_type="call", strike=Decimal("207.50"), expiration="2026-07-24",
        )
        create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="orphan", remaining_fraction="0",
            option_type="call", strike=Decimal("207.5"), expiration="2026-07-24",
        )
        self.connection.commit()

        self.assertEqual(len(get_all_current_lifecycle_keys(self.connection)), 1)

    def test_all_current_lifecycle_keys_deterministic_normalized_order(self):
        create_trade_lifecycle(
            self.connection, self.trader.id, "SPX", status="open", remaining_fraction="1",
            option_type="put", strike=Decimal("7430"), expiration="2026-07-24",
        )
        create_trade_lifecycle(
            self.connection, self.trader.id, "AVGO", status="open", remaining_fraction="1",
            option_type="put", strike=Decimal("380"), expiration="2026-07-24",
        )
        self.connection.commit()

        symbols = [key[1] for key in get_all_current_lifecycle_keys(self.connection)]

        self.assertEqual(symbols, sorted(symbols))


class RepositoryTransactionDisciplineTests(_LifecycleRepositoryTestCase):
    """Confirms the new R6.3 write functions never commit or roll back -
    the caller owns the transaction, exactly like every other function in
    this module."""

    def test_create_trade_lifecycle_does_not_commit(self):
        create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open", remaining_fraction="1"
        )
        self.connection.rollback()

        count = self.connection.execute("SELECT COUNT(*) FROM trade_lifecycles").fetchone()[0]
        self.assertEqual(count, 0)

    def test_supersede_trade_lifecycle_does_not_commit(self):
        lifecycle = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open", remaining_fraction="1"
        )
        self.connection.commit()

        supersede_trade_lifecycle(self.connection, lifecycle.id)
        self.connection.rollback()

        still_current = self.connection.execute(
            "SELECT is_current FROM trade_lifecycles WHERE id = ?", (lifecycle.id,)
        ).fetchone()[0]
        self.assertEqual(still_current, 1)

    def test_persist_lifecycle_builds_does_not_commit(self):
        entry, entry_raw = self._make_signal(strike=Decimal("207.5"))
        self.connection.commit()
        snapshot = get_current_signal_snapshot_for_raw_message(self.connection, entry_raw.id)
        builds = build_lifecycle_sequence([snapshot])

        persist_lifecycle_builds(
            self.connection,
            self.trader.id,
            "IBM",
            "call",
            Decimal("207.5"),
            "2026-07-24",
            builds,
            {snapshot.trade_signal_id: snapshot},
        )
        self.connection.rollback()

        count = self.connection.execute("SELECT COUNT(*) FROM trade_lifecycles").fetchone()[0]
        self.assertEqual(count, 0)
        self.assertIsNone(get_trade_signal_by_id(self.connection, entry.id).lifecycle_id)

    def test_clear_lifecycle_pointers_for_generation_does_not_commit(self):
        signal, _ = self._make_signal(strike=Decimal("207.5"))
        lifecycle = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open", remaining_fraction="1"
        )
        self.connection.commit()
        update_trade_signal_lifecycle_pointer(self.connection, signal.id, lifecycle.id)
        self.connection.commit()

        clear_lifecycle_pointers_for_generation(self.connection, lifecycle.id)
        self.connection.rollback()

        self.assertEqual(
            get_trade_signal_by_id(self.connection, signal.id).lifecycle_id, lifecycle.id
        )


class DatabaseLifecyclePurityUnaffectedTests(unittest.TestCase):
    """R6.3 imports database.lifecycle (SignalSnapshot) into
    database/repository.py, a one-way dependency (repository -> lifecycle)
    the approved design explicitly allows. This sanity-checks, from the
    repository test file's own perspective, that database/lifecycle.py
    itself was not modified to accommodate that import - the full purity
    contract is exercised by tests.test_lifecycle.PureModuleBoundaryTests."""

    def test_lifecycle_module_still_has_no_database_imports(self):
        import database.lifecycle as lifecycle_module

        with open(lifecycle_module.__file__, "r", encoding="utf-8") as f:
            lines = f.readlines()
        import_lines = [
            line
            for line in lines
            if line.strip().startswith("import ") or line.strip().startswith("from ")
        ]
        for line in import_lines:
            self.assertNotIn("sqlite3", line)
            self.assertNotIn("database.repository", line)
            self.assertNotIn("database.service", line)


class GetAllCurrentTradeLifecyclesTests(_LifecycleRepositoryTestCase):
    """Covers Recovery Milestone R7's
    database.repository.get_all_current_trade_lifecycles() - the
    unbounded (no LIMIT) analytics-completeness reader, distinct from the
    pre-existing, deliberately bounded (default LIMIT 100) display reader
    list_current_trade_lifecycles()."""

    def test_returns_empty_list_when_no_current_lifecycle_exists(self):
        self.assertEqual(get_all_current_trade_lifecycles(self.connection), [])

    def test_more_than_one_hundred_current_lifecycles_are_not_truncated(self):
        # 101 rows - one more than list_current_trade_lifecycles()'s own
        # default LIMIT 100 - proving this function has no such bound.
        created_ids = []
        for _ in range(101):
            lifecycle = create_trade_lifecycle(
                self.connection, self.trader.id, "IBM", status="open",
                remaining_fraction="1",
            )
            created_ids.append(lifecycle.id)
        self.connection.commit()

        total_in_db = self.connection.execute(
            "SELECT COUNT(*) FROM trade_lifecycles WHERE is_current = 1"
        ).fetchone()[0]
        self.assertEqual(total_in_db, 101)

        results = get_all_current_trade_lifecycles(self.connection)

        # No truncation: every one of the 101 inserted ids is present.
        self.assertEqual(len(results), 101)
        # No double-counting: every id is distinct.
        result_ids = [r.id for r in results]
        self.assertEqual(len(result_ids), len(set(result_ids)))
        self.assertEqual(set(result_ids), set(created_ids))
        # Deterministic ascending order, not a display "newest first"
        # convention.
        self.assertEqual(result_ids, sorted(result_ids))

    def test_excludes_superseded_lifecycles(self):
        lifecycle = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="closed",
            remaining_fraction="0",
        )
        self.connection.commit()
        supersede_trade_lifecycle(self.connection, lifecycle.id)
        self.connection.commit()

        self.assertEqual(get_all_current_trade_lifecycles(self.connection), [])

    def test_filters_by_trader_id(self):
        own_lifecycle = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open",
            remaining_fraction="1",
        )
        other_trader = create_trader(self.connection, self.source.id, "Sarang")
        self.connection.commit()
        create_trade_lifecycle(
            self.connection, other_trader.id, "AVGO", status="closed",
            remaining_fraction="0",
        )
        self.connection.commit()

        results = get_all_current_trade_lifecycles(
            self.connection, trader_id=self.trader.id
        )

        self.assertEqual([r.id for r in results], [own_lifecycle.id])

    def test_filter_by_nonexistent_trader_id_returns_empty_list(self):
        create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open",
            remaining_fraction="1",
        )
        self.connection.commit()

        self.assertEqual(
            get_all_current_trade_lifecycles(self.connection, trader_id=999999), []
        )

    def test_several_trader_ids_sharing_the_same_name_are_never_merged(self):
        duplicate_named_trader = create_trader(self.connection, self.source.id, "TC")
        self.connection.commit()
        self.assertNotEqual(self.trader.id, duplicate_named_trader.id)
        self.assertEqual(self.trader.name, duplicate_named_trader.name)

        lifecycle_one = create_trade_lifecycle(
            self.connection, self.trader.id, "IBM", status="open",
            remaining_fraction="1",
        )
        lifecycle_two = create_trade_lifecycle(
            self.connection, duplicate_named_trader.id, "IBM", status="open",
            remaining_fraction="1",
        )
        self.connection.commit()

        results = get_all_current_trade_lifecycles(self.connection)

        self.assertEqual(len(results), 2)
        self.assertEqual(
            {r.trader_id for r in results}, {self.trader.id, duplicate_named_trader.id}
        )
        self.assertEqual(
            {r.id for r in results}, {lifecycle_one.id, lifecycle_two.id}
        )


if __name__ == "__main__":
    unittest.main()
