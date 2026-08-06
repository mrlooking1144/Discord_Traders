"""Pure presentation helpers for the Bulk Channel Import workflow
(Recovery Milestone R9c).

Mirrors app/dashboard_formatting.py's proven precedent exactly: no
``streamlit``, ``sqlite3``, ``database.repository``, or
``database.service`` import; no calculation, parsing, channel creation,
duplicate detection, ingestion, lifecycle rebuild, or transaction logic
of any kind. Every function here takes an already-fetched
database.models dataclass instance (or a plain list of
app.discord_adapter.SegmentedMessage objects) and returns a
display-ready value or row dict - the exact same "pure formatting only"
boundary R8a/R8b's own dashboard_formatting.py already established.

Two independent checkpoint concepts (see database.models.ChannelCheckpoint's
own docstring) must never be confused in any helper below: the
CHRONOLOGICAL checkpoint (based only on a resolved Discord message
timestamp - entirely absent when unresolved) and the INGESTION checkpoint
(based only on insertion order - always populated). A resolved Discord
time is never substituted with ingestion time anywhere in this module.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.discord_adapter import SegmentedMessage
from database.models import (
    Channel,
    ChannelExternalIdAvailability,
    ChannelImportChannelSummary,
    ChannelImportDuplicatePrediction,
    ChannelImportOperation,
    LifecycleRebuildResult,
)

_MISSING_VALUE_DISPLAY = "—"  # em dash
_UNRESOLVED_TIME_DISPLAY = "Unresolved"

# The frozen, documented synthetic-id format from
# TradeService._resolve_external_id()'s own approved contract
# (database/service.py) - a literal constant here, never an import of
# that private symbol.
_SYNTHETIC_EXTERNAL_ID_PREFIX = "synthetic:"

_CREATE_MODE_PREDICTION_NOTICE = (
    "Duplicate prediction is not available until the new channel is "
    "created. These messages are provisionally shown as new, subject to "
    "the authoritative confirm-time channel collision and duplicate "
    "checks."
)

_RESUME_GUIDANCE_MESSAGE = (
    "When continuing this channel's paste, include the last imported "
    "message and preferably several messages before it. Overlapping "
    "messages are safe - duplicate detection will recognize and skip "
    "anything already stored."
)


def format_channel_option_label(channel: Channel) -> str:
    """"{name} ({external_id})" when a display name is present, else
    just "{external_id}" - used as the existing-channel selectbox's
    format_func."""
    external_id = channel.external_channel_id or _MISSING_VALUE_DISPLAY
    if channel.name and channel.name.strip():
        return f"{channel.name} ({external_id})"
    return external_id


def is_synthetic_external_id(external_id: str) -> bool:
    """True if external_id starts with the frozen synthetic-id prefix."""
    return external_id.startswith(_SYNTHETIC_EXTERNAL_ID_PREFIX)


def format_checkpoint_external_id(external_id: str | None) -> str:
    """external_id unchanged if real; prefixed with the exact required
    label "Synthetic checkpoint IDs" if it starts with the frozen
    synthetic-id prefix; the fixed em-dash placeholder if None."""
    if external_id is None:
        return _MISSING_VALUE_DISPLAY
    if is_synthetic_external_id(external_id):
        return f"Synthetic checkpoint IDs: {external_id}"
    return external_id


def format_availability_message(availability: ChannelExternalIdAvailability) -> str:
    """A one-line advisory message from a
    check_new_channel_external_id_availability() result - never raises,
    never blocks Preview Batch or Confirm; the authoritative collision
    check and rejection remain entirely R9b's own job."""
    if availability.is_available:
        return f"'{availability.external_channel_id}' is available."
    existing_channel = availability.existing_channel
    existing_id = existing_channel.id if existing_channel is not None else None
    return (
        f"'{availability.external_channel_id}' is already taken by "
        f"channel {existing_id}."
    )


def build_preview_rows(
    segmented: list[SegmentedMessage],
    predictions: list[ChannelImportDuplicatePrediction],
) -> list[dict]:
    """One display row per segmented message, in EXACTLY the caller's
    own input order - this function never sorts or reorders segmented in
    any way, including never sorting by sequence_in_batch.
    segment_discord_batch() already returns messages in paste order with
    monotonically increasing sequence_in_batch values by construction,
    so in ordinary use the result already looks sequence-ascending - but
    that is a property of the caller's own input, never a sorting
    behavior performed here.

    predictions are matched to rows by sequence_in_batch via one O(n)
    lookup dict built once, never a per-row linear scan. An empty
    predictions list (create mode - see build_create_mode_prediction_notice())
    renders "—" for both prediction columns on every row, since it means
    "not computed," never "zero duplicates found."
    """
    predictions_by_sequence = {p.sequence_in_batch: p for p in predictions}
    rows = []
    for message in segmented:
        prediction = predictions_by_sequence.get(message.sequence_in_batch)
        if prediction is None:
            predicted_display = _MISSING_VALUE_DISPLAY
            content_differs_display = _MISSING_VALUE_DISPLAY
        else:
            predicted_display = "Yes" if prediction.predicted_duplicate else "No"
            if prediction.predicted_content_differs is None:
                content_differs_display = _MISSING_VALUE_DISPLAY
            else:
                content_differs_display = (
                    "Yes" if prediction.predicted_content_differs else "No"
                )
        rows.append(
            {
                "Seq": message.sequence_in_batch,
                "Trader": message.trader_raw or _MISSING_VALUE_DISPLAY,
                "Timestamp": message.timestamp_text or _MISSING_VALUE_DISPLAY,
                "Channel Tags": (
                    ", ".join(message.channel_tags)
                    if message.channel_tags else _MISSING_VALUE_DISPLAY
                ),
                "Predicted Duplicate": predicted_display,
                "Content Differs": content_differs_display,
            }
        )
    return rows


def count_new_vs_duplicate(
    predictions: list[ChannelImportDuplicatePrediction], total_segmented: int,
) -> dict:
    """{"new": int, "predicted_duplicate": int} - EXISTING MODE ONLY.

    Must never be called for a create-mode preview: an empty predictions
    list there means "not computed" (see
    build_create_mode_prediction_notice() for create mode's own, separate
    summary), which this function cannot distinguish from "zero
    duplicates found" - calling it in that case would silently
    misrepresent an unperformed check as a completed one.
    """
    duplicate_count = sum(1 for p in predictions if p.predicted_duplicate)
    return {"new": total_segmented - duplicate_count, "predicted_duplicate": duplicate_count}


def build_create_mode_prediction_notice() -> dict:
    """The fixed create-mode duplicate-prediction summary (Recovery
    Milestone R9c) - a pure constant-returning function so the UI and its
    own tests share one source of truth for the exact wording. Never
    describes create-mode messages as "necessarily new" - only
    provisionally so, pending R9b's own authoritative confirm-time
    checks."""
    return {
        "notice": _CREATE_MODE_PREDICTION_NOTICE,
        "new_label": "Provisionally new",
        "duplicate_label": "Predicted duplicate",
        "duplicate_value": "Not available for create mode",
    }


def build_content_difference_warnings(
    segmented: list[SegmentedMessage],
    predictions: list[ChannelImportDuplicatePrediction],
) -> list[str]:
    """One short human-readable line per message where
    predicted_content_differs is True; [] otherwise (including always []
    for create mode's own empty predictions list)."""
    predictions_by_sequence = {p.sequence_in_batch: p for p in predictions}
    warnings = []
    for message in segmented:
        prediction = predictions_by_sequence.get(message.sequence_in_batch)
        if prediction is not None and prediction.predicted_content_differs:
            warnings.append(
                f"Message {message.sequence_in_batch}: predicted duplicate "
                "with content that differs from the stored message."
            )
    return warnings


def format_checkpoint_timestamp(
    latest_received_at: str | None, display_timezone: str | None,
) -> tuple[str, str | None]:
    """(primary_display, secondary_utc_display_or_None).

    latest_received_at is None (the Discord message time is unresolved) -
    returns (_UNRESOLVED_TIME_DISPLAY, _UNRESOLVED_TIME_DISPLAY) for both
    values - never substituted with ingestion time.

    display_timezone is None or blank (no timezone source available yet)
    - returns (utc_display, None): the caller renders UTC only.

    Both present and display_timezone is a valid IANA name - returns
    (local_display, utc_display), local computed by converting the
    canonical UTC latest_received_at into display_timezone.

    A malformed/unrecognized display_timezone falls back to the UTC-only
    branch - this function never raises for a display-only conversion
    failure; it only ever degrades to showing less information.

    The caller (app/streamlit_app.py) is solely responsible for choosing
    which value to pass as display_timezone (the current live form
    timezone, the frozen preview's timezone, or a completed operation's
    own recorded timezone) - this function itself is completely
    state-agnostic.
    """
    if latest_received_at is None:
        return _UNRESOLVED_TIME_DISPLAY, _UNRESOLVED_TIME_DISPLAY

    utc_dt = datetime.fromisoformat(latest_received_at)
    utc_display = utc_dt.isoformat()

    if not display_timezone or not display_timezone.strip():
        return utc_display, None

    try:
        local_dt = utc_dt.astimezone(ZoneInfo(display_timezone.strip()))
    except (ZoneInfoNotFoundError, ValueError):
        return utc_display, None

    return local_dt.isoformat(), utc_display


def build_last_operation_counts(operation: ChannelImportOperation) -> dict:
    """{"Processed", "Stored", "Duplicate", "Unrecognized", "Failed",
    "Committed At"} - a pure 1:1 field mapping of the one just-returned/
    just-fetched ChannelImportOperation row - never a cumulative or
    lifetime total across every operation for the channel."""
    return {
        "Processed": operation.processed_count,
        "Stored": operation.stored_count,
        "Duplicate": operation.duplicate_count,
        "Unrecognized": operation.unrecognized_count,
        "Failed": operation.failed_count,
        "Committed At": operation.committed_at,
    }


def build_lifecycle_rebuild_summary(result: LifecycleRebuildResult) -> dict:
    """A flat display dict of all eight LifecycleRebuildResult counters,
    unchanged pass-through - no aggregation, no recalculation."""
    return {
        "Keys Considered": result.keys_considered,
        "Keys Changed": result.keys_changed,
        "Keys Unchanged": result.keys_unchanged,
        "Lifecycles Superseded": result.lifecycles_superseded,
        "Lifecycles Created": result.lifecycles_created,
        "Lifecycle Events Created": result.lifecycle_events_created,
        "Signal Pointers Cleared": result.signal_pointers_cleared,
        "Signal Pointers Assigned": result.signal_pointers_assigned,
    }


def build_resume_panel(
    summary: ChannelImportChannelSummary, *, display_timezone: str | None,
) -> dict:
    """A flat display dict for one channel's resume panel - three
    distinct id/time concepts, never conflated (see this module's own
    top-of-file docstring):

        "Channel Name", "Channel External ID",
        "Latest resolved Discord time", "Latest resolved Discord time (UTC)",
        "Chronological message ID", "Latest ingestion checkpoint ID",
        "Last import operation", "Last operation counts"

    display_timezone is passed straight through to
    format_checkpoint_timestamp() - the caller decides its source
    (Section 16/Correction 7 of the approved R9c plan: the current live
    form timezone, the frozen preview's timezone, or a completed
    operation's own result.operation.timezone). This function itself
    never inspects any session/UI state.

    "Chronological message ID" is "Unavailable (Discord time unresolved)"
    whenever the chronological checkpoint is None - never substituted
    with the ingestion checkpoint's own id. "Latest ingestion checkpoint
    ID" is always available whenever summary.checkpoint is not None,
    labeled "Synthetic checkpoint IDs" when it is a synthetic id (via
    format_checkpoint_external_id()).
    """
    channel = summary.channel
    panel = {
        "Channel Name": channel.name or _MISSING_VALUE_DISPLAY,
        "Channel External ID": channel.external_channel_id or _MISSING_VALUE_DISPLAY,
    }

    checkpoint = summary.checkpoint
    if checkpoint is None:
        panel["Latest resolved Discord time"] = "No messages imported into this channel yet."
        panel["Latest resolved Discord time (UTC)"] = _MISSING_VALUE_DISPLAY
        panel["Chronological message ID"] = _MISSING_VALUE_DISPLAY
        panel["Latest ingestion checkpoint ID"] = _MISSING_VALUE_DISPLAY
    else:
        local_display, utc_display = format_checkpoint_timestamp(
            checkpoint.latest_received_at, display_timezone
        )
        panel["Latest resolved Discord time"] = local_display
        panel["Latest resolved Discord time (UTC)"] = (
            utc_display if utc_display is not None else _MISSING_VALUE_DISPLAY
        )
        if checkpoint.latest_received_external_id is None:
            panel["Chronological message ID"] = "Unavailable (Discord time unresolved)"
        else:
            panel["Chronological message ID"] = format_checkpoint_external_id(
                checkpoint.latest_received_external_id
            )
        panel["Latest ingestion checkpoint ID"] = format_checkpoint_external_id(
            checkpoint.last_ingested_external_id
        )

    operation = summary.latest_operation
    if operation is None:
        panel["Last import operation"] = "No prior Bulk Channel Import operation for this channel yet."
        panel["Last operation counts"] = None
    else:
        panel["Last import operation"] = operation.committed_at
        panel["Last operation counts"] = build_last_operation_counts(operation)

    return panel


def build_resume_guidance_message() -> str:
    """The fixed resume-guidance text (Recovery Milestone R9c) - a pure
    constant-returning function so the UI and its own tests share one
    source of truth for the exact wording."""
    return _RESUME_GUIDANCE_MESSAGE
