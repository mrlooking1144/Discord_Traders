"""Pure-module tests for app/bulk_import_formatting.py (Recovery
Milestone R9c).

Mirrors tests/test_dashboard_formatting.py's own established convention:
no streamlit, no sqlite3, no database access of any kind - every test
constructs plain database.models dataclass instances and
app.discord_adapter.SegmentedMessage objects directly and asserts on the
formatting module's own pure return values.
"""

import unittest

from app.bulk_import_formatting import (
    build_content_difference_warnings,
    build_create_mode_prediction_notice,
    build_last_operation_counts,
    build_lifecycle_rebuild_summary,
    build_preview_rows,
    build_resume_guidance_message,
    build_resume_panel,
    count_new_vs_duplicate,
    format_availability_message,
    format_channel_option_label,
    format_checkpoint_external_id,
    format_checkpoint_timestamp,
    is_synthetic_external_id,
)
from app.discord_adapter import SegmentedMessage
from database.models import (
    Channel,
    ChannelCheckpoint,
    ChannelExternalIdAvailability,
    ChannelImportChannelSummary,
    ChannelImportDuplicatePrediction,
    ChannelImportOperation,
    LifecycleRebuildResult,
)

_SYNTHETIC_ID = "synthetic:" + "a" * 64


def _message(
    sequence_in_batch, *, trader_raw="Bdorts", timestamp_text="Today at 04:30 PM",
    channel_tags=None,
):
    return SegmentedMessage(
        sequence_in_batch=sequence_in_batch,
        trader_raw=trader_raw,
        header_present=True,
        header_timestamp_raw="04:30 PM",
        footer_present=True,
        footer_trader_raw=trader_raw,
        footer_timestamp_raw=timestamp_text,
        footer_timestamp_kind="relative_today",
        timestamp_text=timestamp_text,
        channel_tags=channel_tags or [],
        raw_text="raw",
        cleaned_text="BOUGHT AVGO 07/24 380P $1.00",
        ambiguity_flags=[],
        synthetic_id_input="input",
    )


def _prediction(sequence_in_batch, *, predicted_duplicate, predicted_content_differs=None):
    return ChannelImportDuplicatePrediction(
        sequence_in_batch=sequence_in_batch,
        external_id=f"ext-{sequence_in_batch}",
        predicted_duplicate=predicted_duplicate,
        predicted_content_differs=predicted_content_differs,
    )


def _channel(**overrides):
    values = dict(id=5, source_id=1, external_channel_id="chan-1", name="General")
    values.update(overrides)
    return Channel(**values)


def _checkpoint(**overrides):
    values = dict(
        channel_id=5, channel_external_id="chan-1", channel_name="General",
        latest_received_at="2026-01-01T12:00:00.000000+00:00",
        latest_received_raw_message_id=10, latest_received_external_id="discord-msg-1",
        last_ingested_raw_message_id=10, last_ingested_external_id="discord-msg-1",
        last_ingested_at="2026-01-01T12:00:05.000000+00:00", last_import_batch_id=1,
    )
    values.update(overrides)
    return ChannelCheckpoint(**values)


def _operation(**overrides):
    values = dict(
        id=1, channel_id=5, import_batch_id=1, reference_date="2026-01-01",
        timezone="America/New_York", processed_count=15, stored_count=15,
        duplicate_count=0, unrecognized_count=0, failed_count=0,
        committed_at="2026-01-01T12:00:05.000000+00:00",
    )
    values.update(overrides)
    return ChannelImportOperation(**values)


class FormatChannelOptionLabelTests(unittest.TestCase):
    def test_named_channel(self):
        self.assertEqual(
            format_channel_option_label(_channel(name="General", external_channel_id="chan-1")),
            "General (chan-1)",
        )

    def test_unnamed_channel(self):
        self.assertEqual(
            format_channel_option_label(_channel(name=None, external_channel_id="chan-1")),
            "chan-1",
        )

    def test_blank_name_treated_as_unnamed(self):
        self.assertEqual(
            format_channel_option_label(_channel(name="   ", external_channel_id="chan-1")),
            "chan-1",
        )


class SyntheticExternalIdTests(unittest.TestCase):
    def test_is_synthetic_external_id_true(self):
        self.assertTrue(is_synthetic_external_id(_SYNTHETIC_ID))

    def test_is_synthetic_external_id_false(self):
        self.assertFalse(is_synthetic_external_id("discord-msg-123"))

    def test_format_real_id(self):
        self.assertEqual(format_checkpoint_external_id("discord-msg-123"), "discord-msg-123")

    def test_format_synthetic_id_uses_exact_label(self):
        result = format_checkpoint_external_id(_SYNTHETIC_ID)
        self.assertTrue(result.startswith("Synthetic checkpoint IDs"))
        self.assertIn(_SYNTHETIC_ID, result)

    def test_format_none(self):
        self.assertEqual(format_checkpoint_external_id(None), "—")


class FormatAvailabilityMessageTests(unittest.TestCase):
    def test_available(self):
        availability = ChannelExternalIdAvailability(
            external_channel_id="new-chan", is_available=True, existing_channel=None
        )
        self.assertIn("available", format_availability_message(availability))

    def test_taken(self):
        existing = _channel(id=42, external_channel_id="taken")
        availability = ChannelExternalIdAvailability(
            external_channel_id="taken", is_available=False, existing_channel=existing
        )
        message = format_availability_message(availability)
        self.assertIn("already taken", message)
        self.assertIn("42", message)


class BuildPreviewRowsOrderingTests(unittest.TestCase):
    def test_never_sorts_preserves_exact_input_order(self):
        # Deliberately non-monotonic sequence_in_batch values, in this
        # exact list order - the output must preserve [3, 1, 2], never
        # re-sort to [1, 2, 3].
        segmented = [_message(3), _message(1), _message(2)]

        rows = build_preview_rows(segmented, [])

        self.assertEqual([row["Seq"] for row in rows], [3, 1, 2])

    def test_predictions_matched_by_sequence_regardless_of_order(self):
        segmented = [_message(3), _message(1), _message(2)]
        predictions = [
            _prediction(1, predicted_duplicate=True),
            _prediction(2, predicted_duplicate=False),
            _prediction(3, predicted_duplicate=True),
        ]

        rows = build_preview_rows(segmented, predictions)
        by_seq = {row["Seq"]: row for row in rows}

        self.assertEqual(by_seq[1]["Predicted Duplicate"], "Yes")
        self.assertEqual(by_seq[2]["Predicted Duplicate"], "No")
        self.assertEqual(by_seq[3]["Predicted Duplicate"], "Yes")

    def test_empty_predictions_renders_placeholder_not_no(self):
        segmented = [_message(1), _message(2)]

        rows = build_preview_rows(segmented, [])

        for row in rows:
            self.assertEqual(row["Predicted Duplicate"], "—")
            self.assertEqual(row["Content Differs"], "—")

    def test_content_differs_true_false_and_none(self):
        segmented = [_message(1), _message(2), _message(3)]
        predictions = [
            _prediction(1, predicted_duplicate=True, predicted_content_differs=True),
            _prediction(2, predicted_duplicate=True, predicted_content_differs=False),
            _prediction(3, predicted_duplicate=False, predicted_content_differs=None),
        ]

        rows = build_preview_rows(segmented, predictions)
        by_seq = {row["Seq"]: row for row in rows}

        self.assertEqual(by_seq[1]["Content Differs"], "Yes")
        self.assertEqual(by_seq[2]["Content Differs"], "No")
        self.assertEqual(by_seq[3]["Content Differs"], "—")


class CountNewVsDuplicateTests(unittest.TestCase):
    def test_all_new(self):
        predictions = [_prediction(1, predicted_duplicate=False), _prediction(2, predicted_duplicate=False)]
        self.assertEqual(
            count_new_vs_duplicate(predictions, total_segmented=2),
            {"new": 2, "predicted_duplicate": 0},
        )

    def test_all_duplicate(self):
        predictions = [_prediction(1, predicted_duplicate=True), _prediction(2, predicted_duplicate=True)]
        self.assertEqual(
            count_new_vs_duplicate(predictions, total_segmented=2),
            {"new": 0, "predicted_duplicate": 2},
        )

    def test_mixed(self):
        predictions = [
            _prediction(1, predicted_duplicate=True),
            _prediction(2, predicted_duplicate=False),
            _prediction(3, predicted_duplicate=False),
        ]
        self.assertEqual(
            count_new_vs_duplicate(predictions, total_segmented=3),
            {"new": 2, "predicted_duplicate": 1},
        )


class BuildCreateModePredictionNoticeTests(unittest.TestCase):
    def test_exact_wording(self):
        notice = build_create_mode_prediction_notice()

        self.assertEqual(
            notice["notice"],
            "Duplicate prediction is not available until the new channel "
            "is created. These messages are provisionally shown as new, "
            "subject to the authoritative confirm-time channel collision "
            "and duplicate checks.",
        )
        self.assertEqual(notice["new_label"], "Provisionally new")
        self.assertEqual(notice["duplicate_label"], "Predicted duplicate")
        self.assertEqual(notice["duplicate_value"], "Not available for create mode")

    def test_never_claims_zero_duplicates(self):
        notice = build_create_mode_prediction_notice()
        self.assertNotIn("0", notice["duplicate_value"])
        self.assertNotIn("necessarily new", notice["notice"])


class BuildContentDifferenceWarningsTests(unittest.TestCase):
    def test_zero_warnings(self):
        segmented = [_message(1)]
        predictions = [_prediction(1, predicted_duplicate=True, predicted_content_differs=False)]
        self.assertEqual(build_content_difference_warnings(segmented, predictions), [])

    def test_one_warning(self):
        segmented = [_message(1), _message(2)]
        predictions = [
            _prediction(1, predicted_duplicate=True, predicted_content_differs=True),
            _prediction(2, predicted_duplicate=True, predicted_content_differs=False),
        ]
        warnings = build_content_difference_warnings(segmented, predictions)
        self.assertEqual(len(warnings), 1)
        self.assertIn("Message 1", warnings[0])

    def test_multiple_warnings(self):
        segmented = [_message(1), _message(2)]
        predictions = [
            _prediction(1, predicted_duplicate=True, predicted_content_differs=True),
            _prediction(2, predicted_duplicate=True, predicted_content_differs=True),
        ]
        warnings = build_content_difference_warnings(segmented, predictions)
        self.assertEqual(len(warnings), 2)

    def test_empty_predictions_yields_no_warnings(self):
        segmented = [_message(1), _message(2)]
        self.assertEqual(build_content_difference_warnings(segmented, []), [])


class FormatCheckpointTimestampTests(unittest.TestCase):
    def test_unresolved_time_returns_unresolved_for_both(self):
        primary, secondary = format_checkpoint_timestamp(None, "America/New_York")
        self.assertEqual(primary, "Unresolved")
        self.assertEqual(secondary, "Unresolved")

    def test_unresolved_time_ignores_timezone_entirely(self):
        # Even a None/blank timezone must still yield "Unresolved" - the
        # None-latest_received_at branch is checked first, unconditionally.
        primary, secondary = format_checkpoint_timestamp(None, None)
        self.assertEqual(primary, "Unresolved")
        self.assertEqual(secondary, "Unresolved")

    def test_no_timezone_source_shows_utc_only(self):
        primary, secondary = format_checkpoint_timestamp(
            "2026-01-01T20:00:00.000000+00:00", None
        )
        self.assertEqual(primary, "2026-01-01T20:00:00+00:00")
        self.assertIsNone(secondary)

    def test_blank_timezone_shows_utc_only(self):
        primary, secondary = format_checkpoint_timestamp(
            "2026-01-01T20:00:00.000000+00:00", "   "
        )
        self.assertEqual(primary, "2026-01-01T20:00:00+00:00")
        self.assertIsNone(secondary)

    def test_valid_timezone_converts_and_shows_utc_secondary(self):
        # 2026-01-01T20:00:00 UTC == 2026-01-01T15:00:00 America/New_York
        # (EST, UTC-5, in January - no DST).
        primary, secondary = format_checkpoint_timestamp(
            "2026-01-01T20:00:00.000000+00:00", "America/New_York"
        )
        self.assertEqual(primary, "2026-01-01T15:00:00-05:00")
        self.assertEqual(secondary, "2026-01-01T20:00:00+00:00")

    def test_invalid_timezone_falls_back_to_utc_only_never_raises(self):
        primary, secondary = format_checkpoint_timestamp(
            "2026-01-01T20:00:00.000000+00:00", "Not/A_Real_Zone"
        )
        self.assertEqual(primary, "2026-01-01T20:00:00+00:00")
        self.assertIsNone(secondary)


class BuildLastOperationCountsTests(unittest.TestCase):
    def test_exact_field_mapping(self):
        operation = _operation(
            processed_count=20, stored_count=15, duplicate_count=5,
            unrecognized_count=2, failed_count=1, committed_at="2026-01-02T00:00:00+00:00",
        )
        self.assertEqual(
            build_last_operation_counts(operation),
            {
                "Processed": 20, "Stored": 15, "Duplicate": 5,
                "Unrecognized": 2, "Failed": 1,
                "Committed At": "2026-01-02T00:00:00+00:00",
            },
        )

    def test_never_a_cumulative_total(self):
        first = _operation(processed_count=15, stored_count=15, committed_at="2026-01-01T00:00:00+00:00")
        second = _operation(processed_count=20, stored_count=20, committed_at="2026-01-02T00:00:00+00:00")

        result = build_last_operation_counts(second)

        # Only the second operation's own values appear - never a sum
        # (15+20=35) or any other combination with the first.
        self.assertEqual(result["Processed"], 20)
        self.assertEqual(result["Stored"], 20)


class BuildLifecycleRebuildSummaryTests(unittest.TestCase):
    def test_all_eight_counters_present_and_unmodified(self):
        result = LifecycleRebuildResult(
            keys_considered=5, keys_changed=3, keys_unchanged=2,
            lifecycles_superseded=1, lifecycles_created=3,
            lifecycle_events_created=6, signal_pointers_cleared=1,
            signal_pointers_assigned=6,
        )
        summary = build_lifecycle_rebuild_summary(result)
        self.assertEqual(
            summary,
            {
                "Keys Considered": 5, "Keys Changed": 3, "Keys Unchanged": 2,
                "Lifecycles Superseded": 1, "Lifecycles Created": 3,
                "Lifecycle Events Created": 6, "Signal Pointers Cleared": 1,
                "Signal Pointers Assigned": 6,
            },
        )


class BuildResumePanelTests(unittest.TestCase):
    def _summary(self, *, checkpoint=None, latest_operation=None):
        return ChannelImportChannelSummary(
            channel=_channel(), checkpoint=checkpoint, latest_operation=latest_operation
        )

    def test_no_checkpoint_at_all(self):
        panel = build_resume_panel(self._summary(), display_timezone="UTC")
        self.assertEqual(
            panel["Latest resolved Discord time"],
            "No messages imported into this channel yet.",
        )
        self.assertEqual(panel["Chronological message ID"], "—")
        self.assertEqual(panel["Latest ingestion checkpoint ID"], "—")

    def test_checkpoint_with_no_operation(self):
        panel = build_resume_panel(
            self._summary(checkpoint=_checkpoint(), latest_operation=None),
            display_timezone="UTC",
        )
        self.assertEqual(
            panel["Last import operation"],
            "No prior Bulk Channel Import operation for this channel yet.",
        )
        self.assertIsNone(panel["Last operation counts"])

    def test_checkpoint_and_operation_both_present(self):
        panel = build_resume_panel(
            self._summary(checkpoint=_checkpoint(), latest_operation=_operation()),
            display_timezone="UTC",
        )
        self.assertIsNotNone(panel["Last operation counts"])
        self.assertEqual(panel["Last operation counts"]["Processed"], 15)

    def test_unresolved_discord_time_still_shows_ingestion_checkpoint_id_real(self):
        checkpoint = _checkpoint(
            latest_received_at=None, latest_received_raw_message_id=None,
            latest_received_external_id=None,
            last_ingested_external_id="discord-msg-999",
        )
        panel = build_resume_panel(
            self._summary(checkpoint=checkpoint), display_timezone="UTC"
        )
        self.assertEqual(panel["Latest resolved Discord time"], "Unresolved")
        self.assertEqual(panel["Chronological message ID"], "Unavailable (Discord time unresolved)")
        self.assertEqual(panel["Latest ingestion checkpoint ID"], "discord-msg-999")

    def test_unresolved_discord_time_still_shows_ingestion_checkpoint_id_synthetic(self):
        checkpoint = _checkpoint(
            latest_received_at=None, latest_received_raw_message_id=None,
            latest_received_external_id=None,
            last_ingested_external_id=_SYNTHETIC_ID,
        )
        panel = build_resume_panel(
            self._summary(checkpoint=checkpoint), display_timezone="UTC"
        )
        self.assertEqual(panel["Latest resolved Discord time"], "Unresolved")
        self.assertTrue(
            panel["Latest ingestion checkpoint ID"].startswith("Synthetic checkpoint IDs")
        )

    def test_display_timezone_drives_conversion(self):
        checkpoint = _checkpoint(latest_received_at="2026-01-01T20:00:00.000000+00:00")
        panel = build_resume_panel(
            self._summary(checkpoint=checkpoint), display_timezone="America/New_York",
        )
        self.assertEqual(panel["Latest resolved Discord time"], "2026-01-01T15:00:00-05:00")
        self.assertEqual(panel["Latest resolved Discord time (UTC)"], "2026-01-01T20:00:00+00:00")


class BuildResumeGuidanceMessageTests(unittest.TestCase):
    def test_returns_exact_fixed_string(self):
        message = build_resume_guidance_message()
        self.assertIn("include the last imported message", message)
        self.assertIn("duplicate detection will recognize and skip", message)
        # Calling twice returns the identical string - a pure constant.
        self.assertEqual(message, build_resume_guidance_message())


if __name__ == "__main__":
    unittest.main()
