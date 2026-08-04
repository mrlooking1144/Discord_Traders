"""Business-rule orchestration layer for Discord Traders.

TradeService is the only module permitted to contain business rules (e.g.
duplicate detection, edit-on-update, ingestion order); it orchestrates one
or more database/repository.py calls but never runs SQL itself. Callers are
responsible for opening/closing the connection and for committing or
rolling back the transaction, exactly as with repository.py.

Recovery Milestone R5 adds channel-scoped, idempotent ingestion
(ingest_channel_message/ingest_batch), reprocessing/supersession
(reprocess_raw_message/reprocess_import_batch), and read-only per-channel
checkpoints (get_channel_checkpoints). These five new public methods each
own and fully control their own transaction via the private
_r5_write_transaction() context manager - a deliberate, explicit
divergence from every pre-R5 method's caller-owned commit/rollback
convention. TradeService.ingest_message() and every other pre-R5 method
are completely unmodified: no R5 behavior is added to the legacy path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.datetime_resolution import resolve_expiration, resolve_footer_timestamp
from app.discord_adapter import segment_discord_batch
from app.parser import PARSER_VERSION, extract_trade_event
from database.analytics import (
    build_data_error_result,
    compute_lifecycle_analytics,
    summarize_trader_performance,
)
from database.lifecycle import (
    FLAG_INCOMPLETE_CONTRACT_IDENTITY,
    STATUS_UNRESOLVED,
    build_lifecycle_sequence,
)
from database.models import (
    BatchIngestResult,
    ChannelCheckpoint,
    LifecycleRebuildResult,
    MessageIngestOutcome,
    ReprocessBatchResult,
    ReprocessOutcome,
    TradeSignal,
    TradeSignalCorrectionResult,
)
from database.repository import (
    clear_lifecycle_pointers_for_generation,
    create_import_batch,
    create_lifecycle_unresolved_singleton,
    create_message_extraction,
    create_raw_message,
    create_trade_signal,
    create_trade_signal_edit,
    create_trader,
    delete_import_batch_if_empty,
    get_all_current_lifecycle_eligible_signal_ids,
    get_all_current_lifecycle_keys,
    get_all_current_trade_lifecycles,
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
    get_or_create_channel,
    get_or_create_source,
    get_or_create_unspecified_channel,
    get_raw_message_by_channel_and_external_id,
    get_raw_message_by_id,
    get_raw_message_ids_by_import_batch,
    get_recorded_shape_for_generation,
    get_trade_lifecycle_by_id,
    get_trade_lifecycle_events,
    get_trade_lifecycle_lineage_raw_message_ids,
    get_trade_signal_by_id,
    get_trade_signal_edits,
    get_trade_signals_for_review,
    get_trade_signals_matching,
    get_trader_by_external_id,
    get_trader_by_id,
    get_traders_by_canonical_name,
    persist_lifecycle_builds,
    supersede_extraction,
    supersede_trade_lifecycle,
    update_trade_signal as _repository_update_trade_signal,
    validate_lifecycle_membership_integrity,
    validate_trade_signal_update_fields,
)

logger = logging.getLogger(__name__)

DUPLICATE_WINDOW_MINUTES = 5

# ---------------------------------------------------------------------------
# Recovery Milestone R5 module-level constants, exceptions, and pure
# helpers. Kept separate from the pre-R5 constants/classes above and below
# so the legacy contract (DUPLICATE_WINDOW_MINUTES, StaleTradeSignalError,
# TradeSignalNotFoundError, AuditHistoryError, _CORRECTION_FIELDS) is
# visibly untouched.
# ---------------------------------------------------------------------------

_SYNTHETIC_ID_PREFIX = "synthetic:"
_R5_PROVENANCE_KEY = "_r5_provenance"

# Approved Product Owner decision (Recovery Milestone R5 implementation
# authorization): "synthetic:" + the full lowercase hexadecimal SHA-256
# digest of synthetic_id_input, encoded as UTF-8. No truncation. Treated as
# a durable identifier format - see _resolve_external_id() below.

AMBIGUITY_FLAG_TRADER_IDENTITY_MISSING = "trader_identity_missing"
AMBIGUITY_FLAG_TRADER_IDENTITY_AMBIGUOUS = "trader_identity_ambiguous"
AMBIGUITY_FLAG_INVALID_NATIVE_TIMESTAMP = "invalid_native_timestamp"

_STRICT_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ReprocessingNotSupportedError(ValueError):
    """Raised when reprocess_raw_message()/reprocess_import_batch() targets
    a raw message with no R5 provenance recorded (e.g. a legacy row, or one
    ingested through the unmodified TradeService.ingest_message() path) -
    there is nothing to deterministically reconstruct the original
    extractor input from. A ValueError subclass, matching this project's
    existing convention for other structured, catchable failure modes
    (StaleTradeSignalError is the one exception; it is intentionally a
    plain Exception, not a ValueError, per its own pre-R5 docstring)."""


@dataclass(frozen=True)
class _TraderIdentityClassification:
    """Result of read-only trader-identity classification (§ Recovery
    Milestone R5). Never itself performs a write - see
    TradeService._classify_trader_identity()/_create_classified_trader().

    Attributes:
        status: One of "resolved", "needs_creation", "missing", or
            "ambiguous".
        trader_id: The existing trader's id, only when status ==
            "resolved".
        creation_name: The name to use if a new trader is created, only
            when status == "needs_creation".
        creation_external_trader_id: The external_trader_id to use if a
            new trader is created, only when status == "needs_creation"
            and one was supplied.
        ambiguity_flags: Zero or more AMBIGUITY_FLAG_* constants - always
            empty for "resolved"/"needs_creation", always non-empty for
            "missing"/"ambiguous".
    """

    status: str
    trader_id: int | None
    creation_name: str | None
    creation_external_trader_id: str | None
    ambiguity_flags: list


def _validate_strict_iso_date(value: object) -> bool:
    """Return True if value is a valid strict "YYYY-MM-DD" calendar date."""
    if not isinstance(value, str) or not _STRICT_ISO_DATE_RE.match(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _validate_iana_timezone(value: object) -> bool:
    """Return True if value is a valid IANA timezone name."""
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _resolve_external_id(
    external_id: str | None,
    synthetic_id_input: str | None,
) -> str:
    """Resolve the real-or-synthetic external_id for one message.

    Approved synthetic-ID contract (Product Owner-authorized for Recovery
    Milestone R5 implementation): "synthetic:" prefix + the full lowercase
    hexadecimal SHA-256 digest of synthetic_id_input, encoded as UTF-8. No
    truncation. This is a durable identifier format - changing any part of
    it later would orphan every previously-synthesized id.

    Args:
        external_id: A real, source-provided message id, or None.
        synthetic_id_input: app.discord_adapter.SegmentedMessage's
            deterministic identity string, or None.

    Returns:
        external_id unchanged, or the synthetic id computed from
        synthetic_id_input.

    Raises:
        ValueError: If both arguments are None, if both are supplied, or if
            either supplied argument is empty or whitespace-only.
    """
    if external_id is not None and synthetic_id_input is not None:
        raise ValueError("Supply external_id or synthetic_id_input, not both.")
    if external_id is not None:
        if not external_id.strip():
            raise ValueError("external_id must not be empty or whitespace-only.")
        return external_id
    if synthetic_id_input is not None:
        if not synthetic_id_input.strip():
            raise ValueError(
                "synthetic_id_input must not be empty or whitespace-only."
            )
        return _SYNTHETIC_ID_PREFIX + hashlib.sha256(
            synthetic_id_input.encode("utf-8")
        ).hexdigest()
    raise ValueError("external_id or synthetic_id_input is required.")


def _resolve_native_timestamp(
    native_received_at: str | None,
) -> tuple[datetime | None, list]:
    """Validate and parse a caller-supplied native source timestamp.

    Accepted format: any string parseable by datetime.fromisoformat() that
    yields a timezone-aware datetime (non-None tzinfo). A naive timestamp
    (no UTC offset) is never guessed into a timezone - it is rejected,
    exactly like an unparseable string.

    Args:
        native_received_at: The caller-supplied ISO8601 string, or None.

    Returns:
        (parsed_datetime, ambiguity_flags). parsed_datetime is None
        whenever native_received_at was None (no flag - nothing was
        attempted) or invalid (flagged
        AMBIGUITY_FLAG_INVALID_NATIVE_TIMESTAMP).
    """
    if native_received_at is None:
        return None, []
    try:
        parsed = datetime.fromisoformat(native_received_at)
    except (TypeError, ValueError):
        return None, [AMBIGUITY_FLAG_INVALID_NATIVE_TIMESTAMP]
    if parsed.tzinfo is None:
        return None, [AMBIGUITY_FLAG_INVALID_NATIVE_TIMESTAMP]
    return parsed, []


def _to_canonical_utc_string(dt: datetime) -> str:
    """Normalize a timezone-aware datetime to this project's fixed-width
    canonical UTC representation for raw_messages.received_at:
    "YYYY-MM-DDTHH:MM:SS.ffffff+00:00". Required so that a plain SQL
    MAX(received_at) string comparison is guaranteed to match true
    chronological order regardless of the timestamp's original UTC
    offset - see database.repository.get_channel_chronological_checkpoints().
    """
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _resolve_authoritative_timestamp(
    native_received_at: str | None,
    footer_timestamp_raw: str | None,
    footer_timestamp_kind: str | None,
    reference_date: str,
    timezone_name: str,
) -> tuple[str | None, list]:
    """Resolve one message's authoritative received_at value.

    Precedence: (1) a valid native_received_at: (2) otherwise a
    successfully resolved footer timestamp: (3) otherwise None, with
    whichever ambiguity flags fired preserved. resolve_footer_timestamp()
    is only ever called when footer_timestamp_raw is not None - calling it
    with no footer text supplied at all is not an ambiguity, just an
    absent input, and must not be flagged as one.

    Returns:
        (canonical_utc_string_or_None, ambiguity_flags).
    """
    flags: list = []
    resolved_native, native_flags = _resolve_native_timestamp(native_received_at)
    flags.extend(native_flags)
    if resolved_native is not None:
        return _to_canonical_utc_string(resolved_native), flags

    if footer_timestamp_raw is not None:
        footer_result = resolve_footer_timestamp(
            footer_timestamp_raw, footer_timestamp_kind, reference_date, timezone_name
        )
        flags.extend(footer_result.ambiguity_flags)
        if footer_result.resolved_timestamp is not None:
            return _to_canonical_utc_string(footer_result.resolved_timestamp), flags
        return None, flags

    return None, flags


def _validate_batch_inputs(
    source_name: str,
    reference_date: str,
    timezone_name: str,
    raw_batch_text: str,
) -> None:
    """Validate ingest_batch()'s batch-wide anchors before any DB write.

    Raises:
        ValueError: If any input fails validation.
    """
    if not source_name or not source_name.strip():
        raise ValueError("source_name must not be empty or whitespace-only.")
    if not _validate_strict_iso_date(reference_date):
        raise ValueError('reference_date must be a valid "YYYY-MM-DD" date.')
    if not _validate_iana_timezone(timezone_name):
        raise ValueError("timezone must be a valid IANA timezone name.")
    if not raw_batch_text or not raw_batch_text.strip():
        raise ValueError("raw_batch_text must not be empty or whitespace-only.")

_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# The exact, fixed set of fields a controlled correction (Milestone 2D.5)
# may change - deliberately excludes raw_message_id/trader_id (identity,
# not a "correction") and the structurally protected id/created_at/
# updated_at, even though the more permissive repository-layer editable
# set would otherwise accept raw_message_id/trader_id too.
_CORRECTION_FIELDS = frozenset(
    {"symbol", "action", "option_type", "price", "expiration", "position_size"}
)


class StaleTradeSignalError(Exception):
    """Raised when a controlled correction's expected_current_values no
    longer match the actual persisted values - another edit happened
    since the caller loaded the signal. Raised before any audit snapshot
    or update; the persisted row is left completely untouched."""


class TradeSignalNotFoundError(ValueError):
    """Raised when a controlled correction targets a trade_signal_id that
    no longer exists. A ValueError subclass so it can still be caught
    broadly as a ValueError, but is distinct from the plain ValueError
    used for no-op/shape rejections and from the legacy (non-controlled)
    update_trade_signal() path's own missing-signal ValueError, which is
    unchanged."""


class AuditHistoryError(Exception):
    """Raised when a stored trade_signal_edits.previous_values value
    cannot be decoded as JSON, or does not decode to a dict."""


# ---------------------------------------------------------------------------
# Recovery Milestone R6.4: lifecycle rebuild orchestration exceptions and
# pure helpers. Kept separate from the R5 section above; the actual
# TradeService methods (rebuild_all_lifecycles,
# rebuild_lifecycles_for_raw_message_ids, and their private helpers) are
# defined at the end of the TradeService class below.
# ---------------------------------------------------------------------------


class LifecycleIntegrityError(Exception):
    """Raised when validate_lifecycle_membership_integrity() reports one or
    more violations after a lifecycle rebuild's writes, but before commit.

    The entire rebuild transaction is rolled back by the caller (the
    _r5_write_transaction() context manager the rebuild methods reuse) once
    this propagates - no newly created lifecycle, superseded generation, or
    pointer change from this call is ever left in place.
    """

    def __init__(self, violations: list[str]) -> None:
        self.violations = list(violations)
        message = "Lifecycle membership integrity violated:\n" + "\n".join(self.violations)
        super().__init__(message)


class LifecycleSnapshotError(Exception):
    """Raised when a persisted trade_lifecycle_events.signal_snapshot
    cannot be safely decoded or validated - during a lifecycle rebuild, or
    during a read-only lifecycle-event query
    (TradeService.list_trade_lifecycle_events(), Recovery Milestone R6.7).

    Malformed or contradictory snapshot evidence is never silently
    reconstructed or replaced. During a rebuild, this always aborts the
    entire rebuild call (rolled back by the caller) before any write
    occurs for the key being compared. During a read-only query, no write
    was ever attempted, so there is nothing to roll back - the caller
    simply receives this exception instead of a result.
    """

    def __init__(
        self, trade_lifecycle_id: int, trade_lifecycle_event_id: int, reason: str
    ) -> None:
        self.trade_lifecycle_id = trade_lifecycle_id
        self.trade_lifecycle_event_id = trade_lifecycle_event_id
        self.reason = reason
        message = (
            f"Invalid signal_snapshot for trade_lifecycle_event_id "
            f"{trade_lifecycle_event_id} (trade_lifecycle_id {trade_lifecycle_id}): {reason}"
        )
        super().__init__(message)


class TradeLifecycleNotFoundError(ValueError):
    """Raised by TradeService.get_trade_lifecycle_analytics() (Recovery
    Milestone R7) when trade_lifecycle_id references no trade_lifecycles
    row at all, current or superseded. A ValueError subclass, matching
    this project's existing not-found exception convention
    (TradeSignalNotFoundError)."""


class LifecycleAnalyticsError(Exception):
    """Raised by TradeService.get_trade_lifecycle_analytics() (Recovery
    Milestone R7) when a trade_lifecycles row exists but has zero
    trade_lifecycle_events membership rows.

    Distinct from LifecycleSnapshotError: that exception covers a
    membership row whose own signal_snapshot content is malformed; this
    one covers the case where no membership row exists to validate at
    all - a schema-unconstrained data-integrity condition that must never
    be silently treated as "zero events, therefore nothing to report."
    Never raised by the tolerant list_current_trade_lifecycle_analytics()/
    list_trader_performance_summaries() read paths, which instead surface
    this same condition as a per-lifecycle 'data_error' analytics result.
    """


_REQUIRED_SNAPSHOT_FIELDS = frozenset(
    {
        "trade_signal_id", "raw_message_id", "trader_id", "symbol", "option_type",
        "strike", "expiration", "event_type", "qualifier", "action", "price",
        "stated_entry_price", "stated_return_pct", "notes", "extraction_id",
        "ordering_key",
    }
)


def _is_complete_key_shape(
    option_type: str | None, strike: object, expiration: str | None
) -> bool:
    """Return whether (option_type, strike, expiration) form a valid
    lifecycle key shape: either all three None (equity) or all three
    non-None (a complete option identity). A tiny, purely structural
    mirror of database.repository._is_complete_lifecycle_key_shape() -
    duplicated rather than imported, since that helper is module-private
    to repository.py and this check is a plain structural classification
    (documented in the R6 ADR/plan), not a business rule this module
    should ever reinterpret or extend.
    """
    all_none = option_type is None and strike is None and expiration is None
    all_present = option_type is not None and strike is not None and expiration is not None
    return all_none or all_present


def _normalize_key_from_lifecycle(lifecycle) -> tuple:
    """Build a normalized (trader_id, symbol_upper, option_type, strike,
    expiration) key tuple from a database.models.TradeLifecycle."""
    strike = Decimal(lifecycle.strike) if lifecycle.strike is not None else None
    return (
        lifecycle.trader_id, lifecycle.symbol.upper(), lifecycle.option_type, strike,
        lifecycle.expiration,
    )


def _normalize_key_from_snapshot(snapshot) -> tuple:
    """Build a normalized (trader_id, symbol_upper, option_type, strike,
    expiration) key tuple from a database.lifecycle.SignalSnapshot."""
    strike = Decimal(snapshot.strike) if snapshot.strike is not None else None
    return (
        snapshot.trader_id, snapshot.symbol.upper(), snapshot.option_type, strike,
        snapshot.expiration,
    )


def _key_sort_key(key: tuple) -> tuple:
    """A total, deterministic ordering for a lifecycle key tuple - the
    same None-safe shape as
    database.repository._lifecycle_key_sort_key(), duplicated for the same
    reason as _is_complete_key_shape() above (that helper is module-private
    to repository.py)."""
    trader_id, symbol, option_type, strike, expiration = key
    return (
        trader_id,
        symbol,
        option_type is not None,
        option_type or "",
        strike is not None,
        strike if strike is not None else Decimal(0),
        expiration is not None,
        expiration or "",
    )


def _validate_and_decode_snapshot(trade_lifecycle_id: int, event) -> dict:
    """Decode and structurally validate one persisted
    trade_lifecycle_events.signal_snapshot value.

    Never checks the decoded values against the live trade_signals row -
    a snapshot is an immutable historical record and is expected to differ
    from the current row after any later correction. Only the snapshot's
    own internal shape/self-consistency is checked.

    Args:
        trade_lifecycle_id: The generation this event belongs to (for the
            raised error only).
        event: A database.models.TradeLifecycleEvent, as returned by
            database.repository.get_trade_lifecycle_events().

    Returns:
        The decoded snapshot dict.

    Raises:
        LifecycleSnapshotError: If signal_snapshot is not valid JSON, does
            not decode to a JSON object, is missing or has a non-integer
            (bool explicitly rejected, even though bool is a Python int
            subclass) trade_signal_id, has a trade_signal_id that does
            not match the event's own trade_signal_id column, is missing
            or has a non-integer raw_message_id, is missing any other
            required snapshot field, or has an ordering_key that is not
            one of the two canonical shapes: [raw_message_id,
            trade_signal_id] (both plain integers, never bool) or
            [received_at, raw_message_id, trade_signal_id] (received_at a
            non-empty string; the trailing two elements plain integers,
            never bool) - in either shape, ordering_key's raw_message_id
            must equal the snapshot's own raw_message_id and
            ordering_key's trade_signal_id must equal the snapshot's own
            trade_signal_id (and therefore the event's own trade_signal_id
            column, already checked above). An empty array, wrong length,
            wrong element types, or a mismatched id is never accepted.
    """
    try:
        decoded = json.loads(event.signal_snapshot)
    except (json.JSONDecodeError, TypeError) as exc:
        raise LifecycleSnapshotError(
            trade_lifecycle_id, event.id, f"signal_snapshot is not valid JSON: {exc}"
        ) from exc

    if not isinstance(decoded, dict):
        raise LifecycleSnapshotError(
            trade_lifecycle_id, event.id, "decoded signal_snapshot is not a JSON object."
        )

    decoded_trade_signal_id = decoded.get("trade_signal_id")
    if not isinstance(decoded_trade_signal_id, int) or isinstance(decoded_trade_signal_id, bool):
        # Explicit type check before the equality comparison below: bool
        # is a Python int subclass, so e.g. True == 1 - without this
        # check, a decoded trade_signal_id of JSON true would silently
        # pass the equality check whenever event.trade_signal_id == 1.
        raise LifecycleSnapshotError(
            trade_lifecycle_id, event.id,
            f"trade_signal_id is missing or not an integer: {decoded_trade_signal_id!r}.",
        )

    if decoded_trade_signal_id != event.trade_signal_id:
        raise LifecycleSnapshotError(
            trade_lifecycle_id, event.id,
            f"decoded trade_signal_id {decoded_trade_signal_id!r} does not match "
            f"this event's own trade_signal_id {event.trade_signal_id}.",
        )

    raw_message_id = decoded.get("raw_message_id")
    if not isinstance(raw_message_id, int) or isinstance(raw_message_id, bool):
        raise LifecycleSnapshotError(
            trade_lifecycle_id, event.id,
            f"raw_message_id is missing or not an integer: {raw_message_id!r}.",
        )

    ordering_key = decoded.get("ordering_key")
    if not isinstance(ordering_key, list):
        raise LifecycleSnapshotError(
            trade_lifecycle_id, event.id,
            f"ordering_key is missing or not a JSON array: {ordering_key!r}.",
        )

    def _is_plain_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    if len(ordering_key) == 2:
        key_raw_message_id, key_trade_signal_id = ordering_key
        if not _is_plain_int(key_raw_message_id) or not _is_plain_int(key_trade_signal_id):
            raise LifecycleSnapshotError(
                trade_lifecycle_id, event.id,
                f"ordering_key must be [raw_message_id, trade_signal_id] with both "
                f"elements plain integers: {ordering_key!r}.",
            )
    elif len(ordering_key) == 3:
        received_at, key_raw_message_id, key_trade_signal_id = ordering_key
        if not isinstance(received_at, str) or not received_at.strip():
            raise LifecycleSnapshotError(
                trade_lifecycle_id, event.id,
                f"ordering_key's received_at must be a non-empty string: {ordering_key!r}.",
            )
        if not _is_plain_int(key_raw_message_id) or not _is_plain_int(key_trade_signal_id):
            raise LifecycleSnapshotError(
                trade_lifecycle_id, event.id,
                f"ordering_key must be [received_at, raw_message_id, trade_signal_id] "
                f"with the trailing two elements plain integers: {ordering_key!r}.",
            )
    else:
        raise LifecycleSnapshotError(
            trade_lifecycle_id, event.id,
            f"ordering_key must have exactly 2 or 3 elements: {ordering_key!r}.",
        )

    if key_raw_message_id != raw_message_id:
        raise LifecycleSnapshotError(
            trade_lifecycle_id, event.id,
            f"ordering_key raw_message_id {key_raw_message_id!r} does not match "
            f"the snapshot's own raw_message_id {raw_message_id!r}.",
        )
    if key_trade_signal_id != decoded_trade_signal_id:
        raise LifecycleSnapshotError(
            trade_lifecycle_id, event.id,
            f"ordering_key trade_signal_id {key_trade_signal_id!r} does not match "
            f"the snapshot's own trade_signal_id {decoded_trade_signal_id!r}.",
        )

    missing_fields = sorted(_REQUIRED_SNAPSHOT_FIELDS - set(decoded.keys()))
    if missing_fields:
        raise LifecycleSnapshotError(
            trade_lifecycle_id, event.id,
            f"missing required snapshot field(s): {missing_fields}.",
        )

    return decoded


def _lifecycle_analytics_result_to_dict(result) -> dict:
    """Convert one database.analytics.LifecycleAnalyticsResult (a frozen,
    internal dataclass) into a plain dict for TradeService's public
    boundary - matching list_trade_lifecycle_events()'s existing
    plain-dict convention (Recovery Milestone R7 planning: "keep the
    public TradeService boundary as plain dictionaries for consistency
    with R6.7"). Every tuple-typed field is converted to a list, since a
    tuple is not the expected shape for a public, JSON-friendly result;
    dataclasses.asdict() already recurses into the nested ExitLeg
    dataclasses within exit_legs, producing a tuple of dicts, so only the
    outer container needs converting.
    """
    result_dict = asdict(result)
    result_dict["exit_legs"] = list(result_dict["exit_legs"])
    result_dict["lifecycle_ambiguity_flags"] = list(result_dict["lifecycle_ambiguity_flags"])
    result_dict["analytics_exclusion_reasons"] = list(
        result_dict["analytics_exclusion_reasons"]
    )
    result_dict["source_event_ids"] = list(result_dict["source_event_ids"])
    return result_dict


def _trader_performance_summary_to_dict(summary) -> dict:
    """Convert one database.analytics.TraderPerformanceSummary (a frozen,
    internal dataclass) into a plain dict for TradeService's public
    boundary, converting every tuple-typed id-list field to a list."""
    summary_dict = asdict(summary)
    summary_dict["all_current_lifecycle_ids"] = list(summary_dict["all_current_lifecycle_ids"])
    summary_dict["eligible_lifecycle_ids"] = list(summary_dict["eligible_lifecycle_ids"])
    summary_dict["return_ineligible_lifecycle_ids"] = list(
        summary_dict["return_ineligible_lifecycle_ids"]
    )
    summary_dict["snapshot_error_lifecycle_ids"] = list(
        summary_dict["snapshot_error_lifecycle_ids"]
    )
    return summary_dict


class _RebuildCounters:
    """Mutable accumulator for one rebuild_all_lifecycles() or
    rebuild_lifecycles_for_raw_message_ids() call - converted to an
    immutable LifecycleRebuildResult via to_result() once the call
    finishes. Never exposed outside this module."""

    def __init__(self) -> None:
        self.keys_considered = 0
        self.keys_changed = 0
        self.keys_unchanged = 0
        self.lifecycles_superseded = 0
        self.lifecycles_created = 0
        self.lifecycle_events_created = 0
        self.signal_pointers_cleared = 0
        self.signal_pointers_assigned = 0

    def to_result(self) -> LifecycleRebuildResult:
        return LifecycleRebuildResult(
            keys_considered=self.keys_considered,
            keys_changed=self.keys_changed,
            keys_unchanged=self.keys_unchanged,
            lifecycles_superseded=self.lifecycles_superseded,
            lifecycles_created=self.lifecycles_created,
            lifecycle_events_created=self.lifecycle_events_created,
            signal_pointers_cleared=self.signal_pointers_cleared,
            signal_pointers_assigned=self.signal_pointers_assigned,
        )


# ---------------------------------------------------------------------------
# Recovery Milestone R6.5a: lifecycle-safe trade-signal correction service
# contract. Kept separate from the pre-existing R5/R6.4 sections above; the
# actual TradeService methods (correct_trade_signal and its private
# no-commit helper) are defined at the end of the TradeService class below.
# The legacy/controlled TradeService.update_trade_signal() contract is
# entirely unmodified - this is a new, additional public method, not a
# replacement.
# ---------------------------------------------------------------------------


class LifecycleUnsafeCorrectionError(ValueError):
    """Raised by TradeService.correct_trade_signal() when a requested
    correction would change `action` on a lifecycle-managed trade signal
    (event_type IS NOT NULL).

    Rejected before any audit row, signal update, lifecycle mutation, or
    commit - the entire correction is refused outright, never partially
    applied. A ValueError subclass so it can still be caught broadly as a
    ValueError, matching this module's existing convention for other
    structured, catchable correction-rejection modes
    (TradeSignalNotFoundError).
    """


def _current_correction_values(signal: TradeSignal) -> dict:
    """Build the canonical typed six-field correction snapshot from signal.

    Args:
        signal: The currently persisted TradeSignal to snapshot.

    Returns:
        A dict with exactly the _CORRECTION_FIELDS keys: symbol (str),
        action (str), option_type (str | None), price (Decimal | None -
        parsed from the stored decimal string, never a float),
        expiration (str | None), position_size (str | None).
    """
    return {
        "symbol": signal.symbol,
        "action": signal.action,
        "option_type": signal.option_type,
        "price": Decimal(signal.price) if signal.price is not None else None,
        "expiration": signal.expiration,
        "position_size": signal.position_size,
    }


class TradeService:
    """Orchestrates business rules on top of database/repository.py.

    Attributes:
        conn: An open sqlite3.Connection, owned and committed/rolled back
            by the caller.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Initialize the service with a caller-owned connection.

        Args:
            conn: An open sqlite3.Connection.
        """
        self.conn = conn

    def check_duplicate_signal(
        self,
        trader_id: int,
        symbol: str,
        action: str,
        option_type: str | None,
        price: Decimal | None,
        expiration: str | None,
        reference_time: str,
    ) -> str | None:
        """Check for a possible duplicate trade signal within a short window.

        Soft, advisory check per docs/DATABASE_DESIGN_V1.md Section 5: looks
        for existing trade_signals matching trader_id, symbol, action,
        option_type, price, and expiration exactly, with created_at within
        DUPLICATE_WINDOW_MINUTES minutes before (inclusive) reference_time.
        Nothing is ever blocked - this only returns a warning the caller can
        act on.

        Args:
            trader_id: FK to traders.id.
            symbol: Ticker symbol to match exactly.
            action: Free-text trade action to match exactly.
            option_type: Free-text call/put to match exactly, or None.
            price: A Decimal price to match exactly, or None. Never a float
                or string.
            expiration: ISO8601 date string to match exactly, or None.
            reference_time: The timestamp to check against, in the same
                format as trade_signals.created_at
                ("YYYY-MM-DD HH:MM:SS"), so callers and tests can supply a
                deterministic value instead of wall-clock now().

        Returns:
            A warning string if at least one matching trade signal exists
            within the window, otherwise None.

        Raises:
            ValueError: If reference_time is None, empty, or not in the
                expected format; or if trader_id is None, or symbol or
                action is None, empty, or whitespace-only.
            TypeError: If price is supplied and is not a Decimal.
        """
        if not reference_time or not reference_time.strip():
            raise ValueError("reference_time is required.")
        try:
            reference_dt = datetime.strptime(reference_time, _TIMESTAMP_FORMAT)
        except ValueError as exc:
            raise ValueError(
                f"reference_time must match the format {_TIMESTAMP_FORMAT!r}."
            ) from exc

        window_start_dt = reference_dt - timedelta(minutes=DUPLICATE_WINDOW_MINUTES)
        window_start = window_start_dt.strftime(_TIMESTAMP_FORMAT)
        window_end = reference_dt.strftime(_TIMESTAMP_FORMAT)

        matches = get_trade_signals_matching(
            self.conn,
            trader_id,
            symbol,
            action,
            option_type,
            price,
            expiration,
            window_start,
            window_end,
        )

        if not matches:
            return None

        return (
            f"Possible duplicate: {len(matches)} matching trade signal(s) "
            f"found for trader {trader_id} ({symbol} {action}) within the "
            f"last {DUPLICATE_WINDOW_MINUTES} minutes."
        )

    def update_trade_signal(
        self,
        trade_signal_id: int,
        expected_current_values: dict | None = None,
        **changed_fields,
    ) -> TradeSignal:
        """Update a trade signal, always preserving edit history.

        Enforces docs/DATABASE_DESIGN_V1.md Section 6: before any correction
        to trade_signals, a full-row JSON snapshot of the pre-edit values is
        written to trade_signal_edits via database/repository.py.

        Two modes, selected only by whether expected_current_values is
        supplied:

        Legacy mode (expected_current_values is None) - unchanged since
        Milestone 2B.6b: changed_fields may be any non-empty, valid subset
        of the repository layer's editable fields (raw_message_id,
        trader_id, symbol, action, option_type, price, expiration,
        position_size); field validation is delegated to
        repository.validate_trade_signal_update_fields(), the single
        source of truth shared with repository.update_trade_signal(); no
        six-field shape requirement, no no-op check, no stale-conflict
        check. Execution order: validate changed_fields, fetch the
        existing row, write the edit snapshot, then apply the update.

        Controlled correction mode (expected_current_values is not None) -
        Milestone 2D.5: changed_fields must contain exactly the six
        approved correction fields (symbol, action, option_type, price,
        expiration, position_size) - never raw_message_id, trader_id, id,
        created_at, or updated_at, even though the repository layer would
        otherwise accept raw_message_id/trader_id. expected_current_values
        must contain exactly the same six keys, typed as symbol: str,
        action: str, option_type: str | None, price: Decimal | None,
        expiration: str | None, position_size: str | None - never a float,
        and never compared against an unparsed price string. Order:
        validate changed_fields' key shape, validate
        expected_current_values' key shape, run the shared repository
        field validation, fetch the existing row (raising
        TradeSignalNotFoundError if missing), build the canonical current-
        value snapshot, compare expected_current_values to it (raising
        StaleTradeSignalError on any mismatch), compare changed_fields to
        it (raising ValueError if identical - a no-op), then - only after
        every check passes - write exactly one audit snapshot and apply
        the update. No stale, missing, invalid, or no-op correction ever
        creates an audit row or changes updated_at.

        Args:
            trade_signal_id: Primary key of the trade signal to update.
            expected_current_values: None for legacy sparse-update
                behavior; the canonical typed six-field snapshot the
                caller believes is still current, to enable controlled-
                correction mode's stale-conflict and no-op protection.
            **changed_fields: The fields to change - shape and content
                requirements depend on the mode above.

        Returns:
            The updated TradeSignal.

        Raises:
            ValueError: Legacy mode - if changed_fields fails validation,
                or if no trade signal exists with trade_signal_id.
                Controlled mode - if changed_fields or
                expected_current_values does not contain exactly the six
                approved fields, if changed_fields fails the shared
                repository validation, or if changed_fields is identical
                to the current persisted values (a no-op).
            TradeSignalNotFoundError: Controlled mode only - if no trade
                signal exists with trade_signal_id. A ValueError subclass.
            StaleTradeSignalError: Controlled mode only - if
                expected_current_values no longer matches the actual
                persisted values.
            TypeError: If price is supplied and is not a Decimal.
        """
        if expected_current_values is None:
            validate_trade_signal_update_fields(changed_fields)

            existing = get_trade_signal_by_id(self.conn, trade_signal_id)
            if existing is None:
                raise ValueError(f"No trade signal exists with id {trade_signal_id}.")

            create_trade_signal_edit(self.conn, trade_signal_id, asdict(existing))

            return _repository_update_trade_signal(
                self.conn, trade_signal_id, **changed_fields
            )

        if set(changed_fields) != _CORRECTION_FIELDS:
            raise ValueError(
                "A controlled correction must supply exactly the approved "
                f"correction fields: {sorted(_CORRECTION_FIELDS)}."
            )
        if set(expected_current_values) != _CORRECTION_FIELDS:
            raise ValueError(
                "expected_current_values must supply exactly the approved "
                f"correction fields: {sorted(_CORRECTION_FIELDS)}."
            )

        validate_trade_signal_update_fields(changed_fields)

        existing = get_trade_signal_by_id(self.conn, trade_signal_id)
        if existing is None:
            raise TradeSignalNotFoundError(
                f"No trade signal exists with id {trade_signal_id}."
            )

        current_values = _current_correction_values(existing)

        if expected_current_values != current_values:
            raise StaleTradeSignalError(
                "The trade signal's current values no longer match the "
                "values expected for this correction."
            )

        if changed_fields == current_values:
            raise ValueError("A correction must change at least one field.")

        create_trade_signal_edit(self.conn, trade_signal_id, asdict(existing))

        return _repository_update_trade_signal(
            self.conn, trade_signal_id, **changed_fields
        )

    def ingest_message(
        self,
        source_name: str,
        trader_name: str,
        raw_text: str,
        reference_time: str,
        external_trader_id: str | None = None,
        external_message_id: str | None = None,
        metadata: dict | None = None,
        received_at: str | None = None,
        trade_signals: list[dict] | None = None,
    ) -> dict:
        """Ingest a new message and its parsed trade signals.

        The single public entry point for persisting a new message: resolves
        the source and trader, inserts the raw message, then inserts each
        parsed trade signal, checking each for a possible duplicate via
        check_duplicate_signal() first. Persistence follows this strictly
        linear order: source, then trader, then raw message, then (for each
        parsed signal) duplicate check followed by insert.

        Trader identity (per docs/DATABASE_DESIGN_V1.md Section 3): if
        external_trader_id is given, an existing trader is reused when one
        already exists for (source_id, external_trader_id); otherwise a new
        trader is created. If external_trader_id is not given, a new trader
        row is always created - display names are never used to match an
        existing trader, since they are not a unique identity.

        Duplicate raw messages (source_id, external_id) are not pre-checked:
        the database's own UNIQUE constraint enforces this, and
        sqlite3.IntegrityError propagates naturally on collision. Editing an
        already-ingested Discord message is out of scope for this method -
        raw_messages.raw_text is write-once, and only new-message ingestion
        is handled here.

        As with the other TradeService methods, this never commits or rolls
        back the connection; the caller owns the transaction.

        Args:
            source_name: Source type name (e.g. 'discord'); looked up or
                created.
            trader_name: Display name/handle as seen in the source.
            raw_text: The original message, verbatim.
            reference_time: The timestamp to check each parsed signal's
                duplicate window against, in the same format as
                trade_signals.created_at ("YYYY-MM-DD HH:MM:SS"). Always
                supplied by the caller; never read from wall-clock now().
            external_trader_id: Stable source-provided trader ID, when
                available.
            external_message_id: Source-provided message ID, when available.
            metadata: Opaque JSON-serializable metadata, or None.
            received_at: ISO8601 timestamp of when the source sent the
                message.
            trade_signals: Zero or more dicts of parsed trade signal fields
                (symbol, action, option_type, price, expiration,
                position_size). raw_message_id and trader_id are filled in
                by this method, not the caller.

        Returns:
            A dict with keys "source" (Source), "trader" (Trader),
            "raw_message" (RawMessage), "trade_signals" (list[TradeSignal],
            in the same order as the trade_signals argument), and
            "duplicate_warnings" (list[str | None], one entry per created
            trade signal, from check_duplicate_signal()).

        Raises:
            ValueError: If source_name or trader_name is empty or
                whitespace-only; if reference_time is missing or malformed;
                or if any parsed signal is missing a required field
                (symbol, action).
            TypeError: If price on any parsed signal is supplied and is not
                a Decimal.
            sqlite3.IntegrityError: If external_message_id collides with an
                already-ingested message for this source, or
                external_trader_id collides in a race.
        """
        source = get_or_create_source(self.conn, source_name)

        if external_trader_id is not None:
            trader = get_trader_by_external_id(
                self.conn, source.id, external_trader_id
            )
            if trader is None:
                trader = create_trader(
                    self.conn, source.id, trader_name, external_trader_id
                )
        else:
            trader = create_trader(self.conn, source.id, trader_name, None)

        raw_message = create_raw_message(
            self.conn,
            source.id,
            raw_text,
            external_message_id,
            metadata,
            received_at,
        )

        created_signals: list[TradeSignal] = []
        duplicate_warnings: list[str | None] = []

        for signal_fields in trade_signals or []:
            symbol = signal_fields.get("symbol")
            action = signal_fields.get("action")
            option_type = signal_fields.get("option_type")
            price = signal_fields.get("price")
            expiration = signal_fields.get("expiration")
            position_size = signal_fields.get("position_size")

            warning = self.check_duplicate_signal(
                trader.id,
                symbol,
                action,
                option_type,
                price,
                expiration,
                reference_time,
            )
            signal = create_trade_signal(
                self.conn,
                raw_message.id,
                trader.id,
                symbol,
                action,
                option_type,
                price,
                expiration,
                position_size,
            )
            created_signals.append(signal)
            duplicate_warnings.append(warning)

        return {
            "source": source,
            "trader": trader,
            "raw_message": raw_message,
            "trade_signals": created_signals,
            "duplicate_warnings": duplicate_warnings,
        }

    def list_trade_signals_for_review(
        self,
        *,
        source_name: str | None = None,
        trader_name: str | None = None,
        symbol: str | None = None,
        date: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """List persisted trade signals for read-only review, newest first.

        A thin, read-only delegation to
        database.repository.get_trade_signals_for_review(): no business
        rules are applied and no row is ever written. The only
        normalization performed here is uppercasing a non-blank symbol
        filter before delegating, so callers (the UI) do not need to
        duplicate that normalization themselves; all other arguments and
        the returned list are passed through unchanged.

        Args:
            source_name: Exact sources.name to filter by, or None/blank to
                omit.
            trader_name: Exact traders.name to filter by, or None/blank to
                omit.
            symbol: Ticker symbol to filter by; normalized to uppercase
                when non-blank, or None/blank to omit.
            date: Calendar date "YYYY-MM-DD" to filter
                trade_signals.created_at by (inclusive start of day,
                exclusive start of the following day), or None/blank to
                omit.
            limit: Maximum number of rows to return. Defaults to 100.

        Returns:
            The list of dicts returned by
            database.repository.get_trade_signals_for_review(), unchanged.

        Raises:
            ValueError: If date is supplied and is not in "YYYY-MM-DD"
                format.
        """
        normalized_symbol = symbol.upper() if symbol and symbol.strip() else symbol

        return get_trade_signals_for_review(
            self.conn,
            source_name=source_name,
            trader_name=trader_name,
            symbol=normalized_symbol,
            date=date,
            limit=limit,
        )

    def list_trade_signal_audit_history(self, trade_signal_id: int) -> list[dict]:
        """List a trade signal's audit history for read-only display.

        A thin, read-only delegation to
        database.repository.get_trade_signal_edits(): decodes each row's
        previous_values JSON (raising AuditHistoryError if it is not
        valid JSON or does not decode to a dict), and returns only the
        six previous editable-field values plus id and edited_at - never
        the raw previous_values JSON text itself. price is preserved as
        its exact stored string, never converted to float.

        Args:
            trade_signal_id: FK to trade_signals.id.

        Returns:
            A list of plain dicts, newest first, each with exactly: id,
            edited_at, symbol, action, option_type, price, expiration,
            position_size. Empty list if the signal has no edit history.

        Raises:
            AuditHistoryError: If a stored previous_values value is not
                valid JSON, or does not decode to a dict.
        """
        edits = get_trade_signal_edits(self.conn, trade_signal_id)

        history: list[dict] = []
        for edit in edits:
            try:
                previous = json.loads(edit.previous_values)
            except (json.JSONDecodeError, TypeError) as exc:
                raise AuditHistoryError(
                    f"Stored audit data for edit {edit.id} could not be decoded."
                ) from exc

            if not isinstance(previous, dict):
                raise AuditHistoryError(
                    f"Stored audit data for edit {edit.id} is not a JSON object."
                )

            history.append(
                {
                    "id": edit.id,
                    "edited_at": edit.edited_at,
                    "symbol": previous.get("symbol"),
                    "action": previous.get("action"),
                    "option_type": previous.get("option_type"),
                    "price": previous.get("price"),
                    "expiration": previous.get("expiration"),
                    "position_size": previous.get("position_size"),
                }
            )

        history.reverse()
        return history

    def list_trade_lifecycle_events(
        self,
        trade_lifecycle_id: int,
    ) -> list[dict]:
        """List one lifecycle generation's membership, in chronological
        order, with each member's immutable signal_snapshot validated and
        decoded (Recovery Milestone R6.7).

        A thin, read-only delegation to
        database.repository.get_trade_lifecycle_events(): performs no
        write, opens no transaction, and calls neither commit nor
        rollback. Repository-provided event order (sequence_index
        ascending) is preserved unchanged. Each event's signal_snapshot is
        validated and decoded via the existing, unmodified
        _validate_and_decode_snapshot() - the same private validator
        rebuild_all_lifecycles()/rebuild_lifecycles_for_raw_message_ids()
        already use internally - so a malformed snapshot is rejected
        identically whether encountered during a rebuild or during this
        read. No second decoding or validation implementation exists.

        Args:
            trade_lifecycle_id: FK to trade_lifecycles.id.

        Returns:
            A list of plain dicts, ordered by sequence_index ascending,
            each with exactly: id, trade_lifecycle_id, trade_signal_id,
            sequence_index, created_at, snapshot (the decoded
            signal_snapshot dict - never the raw JSON text). Empty list
            if the generation has no members or does not exist.

        Raises:
            LifecycleSnapshotError: If any member's signal_snapshot is not
                valid JSON, does not decode to a JSON object, is missing a
                required field, or has a trade_signal_id/ordering_key that
                does not match its own event row - propagated unchanged
                from _validate_and_decode_snapshot(), never caught,
                wrapped, suppressed, or reinterpreted here.
        """
        events = get_trade_lifecycle_events(self.conn, trade_lifecycle_id)
        return [
            {
                "id": event.id,
                "trade_lifecycle_id": event.trade_lifecycle_id,
                "trade_signal_id": event.trade_signal_id,
                "sequence_index": event.sequence_index,
                "created_at": event.created_at,
                "snapshot": _validate_and_decode_snapshot(trade_lifecycle_id, event),
            }
            for event in events
        ]

    # -----------------------------------------------------------------
    # Recovery Milestone R7: trader-performance analytics.
    #
    # Three public read methods, all read-only (no write, no transaction,
    # no commit/rollback - the same caller-owned-connection convention as
    # list_trade_lifecycle_events() above). All calculation logic lives in
    # the pure database.analytics module; these methods only fetch,
    # validate/decode, and translate frozen analytics dataclasses into
    # plain dicts at the public boundary (matching R6.7's own
    # plain-dict convention).
    # -----------------------------------------------------------------

    def _resolve_event_timestamp(
        self, signal_id: int | None, events_by_signal_id: dict
    ) -> str | None:
        """Resolve the canonical UTC timestamp for one member signal's raw
        message, or None if signal_id is None, the signal is not among
        events_by_signal_id, or its raw message has no resolved
        received_at.

        Reads database.repository.get_raw_message_by_id() live rather
        than only trusting the frozen snapshot's own ordering_key: the
        snapshot's raw_message_id link is immutable (frozen at build
        time), but raw_messages.received_at is itself never updated once
        written (no update_raw_message() function exists anywhere in
        database/repository.py) - so reading it live via that immutable
        id is safe and does not violate the snapshot trust boundary, and
        is more precise than inferring anything from ordering_key's own
        2- vs 3-element shape (which describes a whole matching window,
        not this one specific message).
        """
        if signal_id is None:
            return None
        event = events_by_signal_id.get(signal_id)
        if event is None:
            return None
        raw_message_id = event["snapshot"]["raw_message_id"]
        raw_message = get_raw_message_by_id(self.conn, raw_message_id)
        if raw_message is None:
            return None
        return raw_message.received_at

    def _build_lifecycle_analytics(self, lifecycle, events: list):
        """Decode every member event's signal_snapshot and compute the
        complete analytics result for one lifecycle generation.

        Args:
            lifecycle: A database.models.TradeLifecycle, already fetched
                by the caller. events must not be empty - the caller
                (get_trade_lifecycle_analytics() or
                _lifecycle_analytics_or_error()) is responsible for the
                zero-event check before calling this method.
            events: Every database.models.TradeLifecycleEvent for this
                generation (database.repository.get_trade_lifecycle_events()'s
                own return value), already ordered by sequence_index.

        Returns:
            A database.analytics.LifecycleAnalyticsResult.

        Raises:
            LifecycleSnapshotError: Propagated unchanged from
                _validate_and_decode_snapshot() for any malformed or
                contradictory snapshot evidence - never caught here.
        """
        trader = get_trader_by_id(self.conn, lifecycle.trader_id)
        trader_name = trader.name if trader is not None else None

        decoded_events = [
            {
                "id": event.id,
                "trade_signal_id": event.trade_signal_id,
                "sequence_index": event.sequence_index,
                "snapshot": _validate_and_decode_snapshot(lifecycle.id, event),
            }
            for event in events
        ]

        events_by_signal_id = {e["trade_signal_id"]: e for e in decoded_events}
        opened_at = self._resolve_event_timestamp(
            lifecycle.opened_by_signal_id, events_by_signal_id
        )
        closed_at = self._resolve_event_timestamp(
            lifecycle.closed_by_signal_id, events_by_signal_id
        )

        return compute_lifecycle_analytics(
            trade_lifecycle_id=lifecycle.id,
            trader_id=lifecycle.trader_id,
            trader_name=trader_name,
            is_current=lifecycle.is_current,
            superseded_at=lifecycle.superseded_at,
            status=lifecycle.status,
            symbol=lifecycle.symbol,
            option_type=lifecycle.option_type,
            strike=lifecycle.strike,
            expiration=lifecycle.expiration,
            lifecycle_ambiguity_flags=lifecycle.ambiguity_flags,
            opened_by_signal_id=lifecycle.opened_by_signal_id,
            closed_by_signal_id=lifecycle.closed_by_signal_id,
            opened_at=opened_at,
            closed_at=closed_at,
            events=decoded_events,
        )

    def _lifecycle_analytics_or_error(self, lifecycle):
        """The tolerant counterpart to _build_lifecycle_analytics(): never
        raises. A zero-event generation or a LifecycleSnapshotError is
        converted into a 'data_error' database.analytics.LifecycleAnalyticsResult
        instead of propagating, so one corrupted lifecycle can never abort
        an entire current-lifecycle list or trader-summary call. The
        failure is never hidden - it is preserved verbatim in the
        returned result's analytics_error_detail and counted by the
        caller.

        The trader lookup is performed only inside each error branch,
        not unconditionally up front - on the normal (non-error) path,
        _build_lifecycle_analytics() already performs its own single
        trader lookup, so resolving trader_name here too would be a
        redundant second query for every successfully computed
        lifecycle."""
        events = get_trade_lifecycle_events(self.conn, lifecycle.id)

        if not events:
            trader = get_trader_by_id(self.conn, lifecycle.trader_id)
            return build_data_error_result(
                trade_lifecycle_id=lifecycle.id,
                trader_id=lifecycle.trader_id,
                trader_name=trader.name if trader is not None else None,
                is_current=lifecycle.is_current,
                superseded_at=lifecycle.superseded_at,
                status=lifecycle.status,
                symbol=lifecycle.symbol,
                option_type=lifecycle.option_type,
                strike=lifecycle.strike,
                expiration=lifecycle.expiration,
                lifecycle_ambiguity_flags=lifecycle.ambiguity_flags,
                source_event_ids=[],
                analytics_error_detail=(
                    f"trade_lifecycle_id {lifecycle.id} has no membership events."
                ),
            )

        try:
            return self._build_lifecycle_analytics(lifecycle, events)
        except LifecycleSnapshotError as exc:
            trader = get_trader_by_id(self.conn, lifecycle.trader_id)
            return build_data_error_result(
                trade_lifecycle_id=lifecycle.id,
                trader_id=lifecycle.trader_id,
                trader_name=trader.name if trader is not None else None,
                is_current=lifecycle.is_current,
                superseded_at=lifecycle.superseded_at,
                status=lifecycle.status,
                symbol=lifecycle.symbol,
                option_type=lifecycle.option_type,
                strike=lifecycle.strike,
                expiration=lifecycle.expiration,
                lifecycle_ambiguity_flags=lifecycle.ambiguity_flags,
                source_event_ids=[event.id for event in events],
                analytics_error_detail=str(exc),
            )

    def get_trade_lifecycle_analytics(self, trade_lifecycle_id: int) -> dict:
        """Compute one lifecycle generation's complete R7 analytics
        result - strict, and accepts any id, current or superseded.

        A thin orchestration over database.analytics.compute_lifecycle_analytics():
        fetches the lifecycle and its membership, decodes every snapshot,
        resolves opened_at/closed_at from the immutable raw_messages
        evidence behind the opener/closer signal, and returns the result
        as a plain dict (matching list_trade_lifecycle_events()'s
        existing plain-dict convention). Performs no write, opens no
        transaction, calls neither commit nor rollback.

        This is the strict counterpart to list_current_trade_lifecycle_analytics():
        a caller asking about one specific, named lifecycle receives the
        unfiltered truth or an exception, never a silently substituted
        'data_error' placeholder.

        Args:
            trade_lifecycle_id: FK to trade_lifecycles.id. May reference a
                current or superseded generation - the result always
                exposes is_current/superseded_at so a superseded
                generation's analytics can never be mistaken for current
                performance.

        Returns:
            A plain dict mirroring database.analytics.LifecycleAnalyticsResult
            field-for-field (exit_legs, lifecycle_ambiguity_flags,
            analytics_exclusion_reasons, and source_event_ids as lists,
            not tuples).

        Raises:
            TradeLifecycleNotFoundError: If trade_lifecycle_id references
                no trade_lifecycles row at all.
            LifecycleAnalyticsError: If the row exists but has zero
                trade_lifecycle_events membership rows.
            LifecycleSnapshotError: Propagated unchanged from
                _validate_and_decode_snapshot() for any malformed or
                contradictory snapshot evidence.
        """
        lifecycle = get_trade_lifecycle_by_id(self.conn, trade_lifecycle_id)
        if lifecycle is None:
            raise TradeLifecycleNotFoundError(
                f"No trade_lifecycles row exists with id {trade_lifecycle_id}."
            )

        events = get_trade_lifecycle_events(self.conn, trade_lifecycle_id)
        if not events:
            raise LifecycleAnalyticsError(
                f"trade_lifecycle_id {trade_lifecycle_id} has no membership events."
            )

        result = self._build_lifecycle_analytics(lifecycle, events)
        return _lifecycle_analytics_result_to_dict(result)

    def list_current_trade_lifecycle_analytics(
        self,
        *,
        trader_id: int | None = None,
    ) -> list[dict]:
        """List every current lifecycle generation's R7 analytics result -
        tolerant per lifecycle, with no truncating limit.

        Built on database.repository.get_all_current_trade_lifecycles()
        (Recovery Milestone R7 - no LIMIT of any kind, unlike the
        pre-existing, display-oriented list_current_trade_lifecycles()),
        so this method can never silently omit a current lifecycle. A
        malformed signal_snapshot, or a lifecycle with zero membership
        events, never aborts the call and is never silently dropped - it
        produces an explicit 'data_error' result in its place (see
        _lifecycle_analytics_or_error()). Performs no write, opens no
        transaction, calls neither commit nor rollback.

        Args:
            trader_id: FK to traders.id to scope to one trader, or None
                for every trader with at least one current lifecycle.
                trader_id is the sole authoritative selector - there is
                no trader_name filter on this method, since the
                repository permits duplicate trader names and this
                contract must never silently merge or arbitrarily pick
                among same-named traders.

        Returns:
            A list of plain dicts (see get_trade_lifecycle_analytics()'s
            return shape), one per current lifecycle, ordered by
            trade_lifecycle_id ascending - deterministic, never a
            "newest first" display convention. Empty list if no current
            lifecycle matches.
        """
        lifecycles = get_all_current_trade_lifecycles(self.conn, trader_id=trader_id)
        results = [self._lifecycle_analytics_or_error(lifecycle) for lifecycle in lifecycles]
        return [_lifecycle_analytics_result_to_dict(result) for result in results]

    def list_trader_performance_summaries(
        self,
        *,
        trader_id: int | None = None,
    ) -> list[dict]:
        """List one performance summary per trader, aggregated strictly
        from current-lifecycle analytics results.

        Shares its underlying per-lifecycle computation with
        list_current_trade_lifecycle_analytics(): both methods delegate
        every lifecycle's fetch/decode/compute work to the same private
        _lifecycle_analytics_or_error() helper, so no signal_snapshot is
        ever read or decoded twice for the same call, and the two public
        methods can never disagree with each other about one lifecycle's
        own result - this method does not call
        list_current_trade_lifecycle_analytics() and then re-derive
        anything from its dict output; it computes the same underlying
        LifecycleAnalyticsResult objects once and reduces over them
        directly via database.analytics.summarize_trader_performance().

        Never ranks or sorts by any performance metric - trader_id
        ascending only, so any ranking/comparison-display decision
        remains entirely with R8.

        Args:
            trader_id: FK to traders.id to scope to one trader, or None
                for every trader with at least one current lifecycle.
                The sole authoritative selector - no trader_name filter.

        Returns:
            A list of plain dicts mirroring
            database.analytics.TraderPerformanceSummary field-for-field
            (every *_lifecycle_ids field as a list, ascending), ordered
            by trader_id ascending. Empty list if no trader has any
            current lifecycle.
        """
        lifecycles = get_all_current_trade_lifecycles(self.conn, trader_id=trader_id)
        results = [self._lifecycle_analytics_or_error(lifecycle) for lifecycle in lifecycles]

        grouped: dict[int, list] = {}
        for result in results:
            grouped.setdefault(result.trader_id, []).append(result)

        summaries = []
        for grouped_trader_id in sorted(grouped):
            group = grouped[grouped_trader_id]
            summaries.append(
                summarize_trader_performance(
                    trader_id=grouped_trader_id,
                    trader_name=group[0].trader_name,
                    lifecycle_results=group,
                )
            )
        return [_trader_performance_summary_to_dict(summary) for summary in summaries]

    # -----------------------------------------------------------------
    # Recovery Milestone R5
    #
    # Everything below this point is new. Every pre-R5 method above
    # (check_duplicate_signal, update_trade_signal, ingest_message,
    # list_trade_signals_for_review, list_trade_signal_audit_history) is
    # completely unmodified and keeps its existing caller-owned
    # commit/rollback convention. The five public methods below each own
    # and fully control their own transaction via _r5_write_transaction()
    # - a deliberate, explicit divergence from that pre-R5 convention.
    # -----------------------------------------------------------------

    def _cleanup_failed_r5_transaction(self) -> None:
        """Best-effort cleanup after a failed R5 write transaction (BEGIN,
        the transaction body, or COMMIT raised). Never itself raises: any
        cleanup failure is only logged, so the caller's bare `raise` always
        re-propagates the original BEGIN/body/COMMIT exception, never a
        cleanup exception.

        Tries self.conn.rollback() first. If that raises, falls back to a
        raw SQL "ROLLBACK". If the fallback also raises, or the connection
        still reports an open transaction even after the fallback
        succeeds, the connection is closed outright - so later code can
        never reuse a connection left in an unknown transactional state.
        """
        if not self.conn.in_transaction:
            return

        try:
            self.conn.rollback()
            return
        except BaseException:
            logger.error(
                "rollback() failed while cleaning up a failed R5 write "
                "transaction; attempting a fallback SQL ROLLBACK.",
                exc_info=True,
            )

        try:
            self.conn.execute("ROLLBACK")
        except BaseException:
            logger.critical(
                "Fallback SQL ROLLBACK also failed while cleaning up a "
                "failed R5 write transaction; closing the connection to "
                "prevent reuse with an unknown open transaction.",
                exc_info=True,
            )
            self.conn.close()
            return

        if self.conn.in_transaction:
            logger.critical(
                "Fallback SQL ROLLBACK completed but the connection still "
                "reports an open transaction; closing the connection to "
                "prevent reuse with an unknown open transaction."
            )
            self.conn.close()

    @contextmanager
    def _r5_write_transaction(self):
        """Context manager owning one complete, explicit R5 write transaction.

        Required behavior (all satisfied below):
          1. Verifies self.conn.in_transaction is False before changing
             any connection configuration - raises RuntimeError if this
             connection already has unrelated pending work, so an R5
             method can never silently sweep a caller's unfinished
             operation into its own commit/rollback.
          2-3. Saves the connection's current isolation_level, then sets
             it to None (true manual/autocommit mode) so this method can
             issue its own explicit BEGIN/COMMIT/ROLLBACK without
             conflicting with sqlite3's own implicit-transaction
             management (which only applies in non-None isolation_level
             modes).
          4. Issues BEGIN IMMEDIATE, reserving the write lock immediately
             rather than deferring it to the first actual write - this is
             what makes ingest_batch()'s duplicate preflight and its
             conditional inserts atomic together.
          5. Yields control to the method body.
          6. On normal completion (no exception), issues COMMIT.
          7. If BEGIN, the body, or COMMIT raises: delegates cleanup to
             _cleanup_failed_r5_transaction() (rollback(), falling back to
             a raw SQL ROLLBACK, falling back to closing the connection -
             see that method), then re-raises the *original* exception
             unmodified. A cleanup failure is never allowed to replace or
             mask it.
          8. Always attempts to restore the original isolation_level in a
             finally block - covering a BEGIN failure, a body failure, a
             COMMIT failure, and even a total rollback-cleanup failure
             (where the connection was closed and the restoration attempt
             itself is expected to fail; that failure is logged, never
             allowed to replace the primary exception).

        Raises:
            RuntimeError: If self.conn.in_transaction is already True.
            Exception: Whatever the transaction body (or BEGIN/COMMIT)
                raised, unmodified - even when cleanup required closing
                the connection.
        """
        if self.conn.in_transaction:
            raise RuntimeError(
                "Cannot start an R5-owned transaction: this connection "
                "already has unrelated pending work (self.conn.in_transaction "
                "is True). Commit or roll back the caller's existing work "
                "before calling this method."
            )

        saved_isolation_level = self.conn.isolation_level
        self.conn.isolation_level = None
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except BaseException:
            self._cleanup_failed_r5_transaction()
            raise
        finally:
            try:
                self.conn.isolation_level = saved_isolation_level
            except BaseException:
                # A secondary cleanup failure (e.g. the connection was
                # closed above) - logged, never allowed to replace
                # whatever exception is actually propagating.
                logger.error(
                    "Failed to restore the original isolation_level after "
                    "an R5 write transaction.",
                    exc_info=True,
                )

    def _classify_trader_identity(
        self,
        source_id: int,
        external_trader_id: str | None,
        trader_raw: str | None,
    ) -> _TraderIdentityClassification:
        """Read-only trader-identity classification. Never writes.

        Resolution order:
          1. When external_trader_id is supplied, resolve by
             (source_id, external_trader_id).
          2. Otherwise normalize trader_raw to a canonical_name and match
             case-insensitively.
          3. Zero canonical matches: "needs_creation".
          4. Exactly one canonical match: "resolved".
          5. More than one canonical match: "ambiguous" - never silently
             selected the first.
          6. Missing/blank trader_raw (and no external_trader_id):
             "missing".

        Channel does not participate in trader identity anywhere in this
        method - traders are scoped only to source_id, matching
        get_traders_by_canonical_name()'s own signature and the R1 ADR's
        canonical_name design.
        """
        if external_trader_id is not None:
            trader = get_trader_by_external_id(self.conn, source_id, external_trader_id)
            if trader is not None:
                return _TraderIdentityClassification("resolved", trader.id, None, None, [])
            return _TraderIdentityClassification(
                "needs_creation",
                None,
                trader_raw or external_trader_id,
                external_trader_id,
                [],
            )

        if not trader_raw or not trader_raw.strip():
            return _TraderIdentityClassification(
                "missing", None, None, None, [AMBIGUITY_FLAG_TRADER_IDENTITY_MISSING]
            )

        canonical_name = trader_raw.strip().lower()
        matches = get_traders_by_canonical_name(self.conn, source_id, canonical_name)
        if len(matches) == 0:
            return _TraderIdentityClassification("needs_creation", None, trader_raw, None, [])
        if len(matches) == 1:
            return _TraderIdentityClassification("resolved", matches[0].id, None, None, [])
        return _TraderIdentityClassification(
            "ambiguous", None, None, None, [AMBIGUITY_FLAG_TRADER_IDENTITY_AMBIGUOUS]
        )

    def _create_classified_trader(
        self,
        source_id: int,
        classification: _TraderIdentityClassification,
    ) -> int:
        """Create a trader from a "needs_creation" classification.

        Writes. Must be called only after duplicate detection has
        confirmed a new raw message will actually be inserted (ingestion),
        or during reprocessing (which always operates on an
        already-stored message, so there is no "duplicate" concern to
        guard against there) - never for a message that turns out to be a
        duplicate during ingestion, so a duplicate message never creates
        an orphan trader row.
        """
        trader = create_trader(
            self.conn,
            source_id,
            classification.creation_name,
            classification.creation_external_trader_id,
        )
        return trader.id

    @staticmethod
    def _validate_metadata_extra(metadata_extra: dict | None) -> None:
        """Validate caller-supplied metadata_extra before any classification,
        duplicate lookup, or database write - including when the message
        will turn out to be a duplicate. Called by both
        ingest_channel_message() (before its transaction opens) and
        _ingest_channel_message_no_commit() (the entry point ingest_batch()
        calls directly), so neither call path can skip this check.

        Raises:
            ValueError: If metadata_extra is neither None nor a dict, or if
                it defines the reserved "_r5_provenance" key.
        """
        if metadata_extra is None:
            return
        if not isinstance(metadata_extra, dict):
            raise ValueError("metadata_extra must be a dict or None.")
        if _R5_PROVENANCE_KEY in metadata_extra:
            raise ValueError(
                f'metadata_extra must not define the reserved "{_R5_PROVENANCE_KEY}" key.'
            )

    @staticmethod
    def _duplicate_outcome(
        sequence_in_batch: int | None,
        channel_id: int,
        existing_raw_message,
        raw_text: str,
        resolved_external_id: str,
    ) -> MessageIngestOutcome:
        """Build a "duplicate" MessageIngestOutcome referencing an
        already-stored raw message. No write of any kind occurs - the
        original raw_text is never overwritten."""
        incoming_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        content_differs = existing_raw_message.content_hash != incoming_hash
        return MessageIngestOutcome(
            sequence_in_batch=sequence_in_batch,
            outcome="duplicate",
            channel_id=channel_id,
            raw_message_id=existing_raw_message.id,
            external_id=resolved_external_id,
            parse_status=None,
            trade_signal_ids=[],
            ambiguity_flags=[],
            content_differs=content_differs,
        )

    def ingest_channel_message(
        self,
        *,
        source_name: str,
        channel_external_id: str | None,
        channel_name: str | None = None,
        trader_raw: str | None,
        external_trader_id: str | None = None,
        raw_text: str,
        cleaned_text: str,
        external_id: str | None = None,
        synthetic_id_input: str | None = None,
        native_received_at: str | None = None,
        footer_timestamp_raw: str | None = None,
        footer_timestamp_kind: str | None = None,
        reference_date: str,
        timezone: str,
        header_timestamp_raw: str | None = None,
        channel_tags: list | None = None,
        adapter_ambiguity_flags: list | None = None,
        metadata_extra: dict | None = None,
    ) -> MessageIngestOutcome:
        """Ingest one metadata-rich, channel-scoped message.

        Public entry point: accepts only user/source-facing metadata -
        never import_batch_id, sequence_in_batch, or any internal
        source_id/channel_id. Those are resolved internally (source/
        channel find-or-create) and passed to the private no-commit helper
        as resolved ids, with import_batch_id/sequence_in_batch always
        None here (batch linkage is supplied only by ingest_batch(),
        never by an external caller of this method).

        Owns one complete R5 write transaction (see _r5_write_transaction):
        commits before returning on success, rolls back and re-raises on
        any exception.

        Args:
            source_name: Source type name (e.g. 'discord'); looked up or
                created.
            channel_external_id: Source-provided channel id, or None to
                use the per-source sentinel "unspecified" channel.
            channel_name: Display name/slug to store if a new channel row
                is created. Ignored if a matching channel already exists.
            trader_raw: The trader/author display name, exactly as it
                appeared in the source, or None/blank if unknown.
            external_trader_id: Stable source-provided trader id, when
                available.
            raw_text: The original message, verbatim. Required,
                non-blank.
            cleaned_text: The extractor's input (already stripped of
                source-specific wrapper noise by an adapter). May be
                blank - extract_trade_event("") yields an "unrecognized"
                extraction, not an error.
            external_id: A real, source-provided message id, or None.
            synthetic_id_input: A deterministic identity string (e.g.
                app.discord_adapter.SegmentedMessage.synthetic_id_input),
                or None. Exactly one of external_id/synthetic_id_input
                must be supplied.
            native_received_at: An ISO8601, timezone-aware timestamp
                string supplied directly by the caller, or None. Takes
                precedence over footer_timestamp_raw when valid.
            footer_timestamp_raw: Verbatim footer timestamp text, or None.
            footer_timestamp_kind: One of app.discord_adapter's footer
                timestamp kind values, or None.
            reference_date: Strict "YYYY-MM-DD" date anchoring expiration/
                footer-timestamp resolution. Required, never the wall
                clock.
            timezone: A valid IANA timezone name. Required.
            header_timestamp_raw: Verbatim header timestamp text, or None
                - stored only as provenance.
            channel_tags: Every channel slug found on this message
                (including secondary cross-post tags), or None.
            adapter_ambiguity_flags: Segmentation-time ambiguity flags
                from the adapter, or None.
            metadata_extra: Additional caller-supplied metadata to merge
                into raw_messages.metadata alongside the reserved R5
                provenance block. Must not define the reserved
                "_r5_provenance" key.

        Returns:
            A MessageIngestOutcome with outcome "stored" or "duplicate".

        Raises:
            ValueError: For any invalid/missing required input, or for a
                metadata_extra collision with the reserved provenance key.
            RuntimeError: If self.conn already has unrelated pending work.
            sqlite3.Error: On an unexpected database failure (the
                transaction is rolled back before this propagates).
        """
        if not source_name or not source_name.strip():
            raise ValueError("source_name must not be empty or whitespace-only.")
        if not raw_text or not raw_text.strip():
            raise ValueError("raw_text must not be empty or whitespace-only.")
        if not _validate_strict_iso_date(reference_date):
            raise ValueError('reference_date must be a valid "YYYY-MM-DD" date.')
        if not _validate_iana_timezone(timezone):
            raise ValueError("timezone must be a valid IANA timezone name.")
        self._validate_metadata_extra(metadata_extra)
        resolved_external_id = _resolve_external_id(external_id, synthetic_id_input)

        with self._r5_write_transaction():
            source = get_or_create_source(self.conn, source_name)
            channel = (
                get_or_create_channel(self.conn, source.id, channel_external_id, channel_name)
                if channel_external_id is not None
                else get_or_create_unspecified_channel(self.conn, source.id)
            )
            return self._ingest_channel_message_no_commit(
                source_id=source.id,
                channel_id=channel.id,
                source_name=source_name,
                trader_raw=trader_raw,
                external_trader_id=external_trader_id,
                raw_text=raw_text,
                cleaned_text=cleaned_text,
                resolved_external_id=resolved_external_id,
                synthetic_id_input=synthetic_id_input,
                native_received_at=native_received_at,
                footer_timestamp_raw=footer_timestamp_raw,
                footer_timestamp_kind=footer_timestamp_kind,
                reference_date=reference_date,
                timezone=timezone,
                import_batch_id=None,
                sequence_in_batch=None,
                header_timestamp_raw=header_timestamp_raw,
                channel_tags=channel_tags,
                adapter_ambiguity_flags=adapter_ambiguity_flags,
                metadata_extra=metadata_extra,
            )

    def _ingest_channel_message_no_commit(
        self,
        *,
        source_id: int,
        channel_id: int,
        source_name: str,
        trader_raw: str | None,
        external_trader_id: str | None,
        raw_text: str,
        cleaned_text: str,
        resolved_external_id: str,
        synthetic_id_input: str | None = None,
        native_received_at: str | None,
        footer_timestamp_raw: str | None,
        footer_timestamp_kind: str | None,
        reference_date: str,
        timezone: str,
        import_batch_id: int | None,
        sequence_in_batch: int | None,
        header_timestamp_raw: str | None,
        channel_tags: list | None,
        adapter_ambiguity_flags: list | None,
        metadata_extra: dict | None,
    ) -> MessageIngestOutcome:
        """Private. Performs the full R5 ingestion sequence for one
        message using already-resolved internal ids. Never begins,
        commits, or rolls back a transaction, and never modifies
        isolation_level - the caller (the public wrapper above, or
        ingest_batch()) fully owns that.

        Note: synthetic_id_input is accepted here (beyond the minimal set
        named in the approved private-helper contract) solely so it can
        be recorded in the reserved "_r5_provenance" metadata block for
        audit/debugging purposes - it never affects batch-linkage
        exposure, which remains exactly import_batch_id/sequence_in_batch
        as approved.
        """
        # --- metadata_extra validation: first, before any classification,
        # duplicate lookup, or write - even when the message will turn out
        # to be a duplicate, invalid metadata is never silently ignored. ---
        self._validate_metadata_extra(metadata_extra)

        # --- internal batch-linkage validation (never externally supplied) ---
        if (import_batch_id is None) != (sequence_in_batch is None):
            raise ValueError(
                "import_batch_id and sequence_in_batch must either both be "
                "None or both be non-None."
            )
        if import_batch_id is not None:
            batch = get_import_batch_by_id(self.conn, import_batch_id)
            if batch is None:
                raise ValueError(f"import_batch_id {import_batch_id} does not exist.")
            if batch.source_id != source_id:
                raise ValueError(
                    f"import_batch_id {import_batch_id} does not belong to "
                    f"source_id {source_id}."
                )
            if not isinstance(sequence_in_batch, int) or isinstance(
                sequence_in_batch, bool
            ) or sequence_in_batch <= 0:
                raise ValueError("sequence_in_batch must be a positive integer.")

        # --- pure extraction/resolution - no write yet ---
        extraction_result = extract_trade_event(cleaned_text)

        expiration = None
        expiration_ambiguity_flags: list = []
        if extraction_result["expiration_raw"] is not None:
            expiration_resolution = resolve_expiration(
                extraction_result["expiration_raw"], reference_date
            )
            expiration = expiration_resolution.resolved_expiration
            expiration_ambiguity_flags = list(expiration_resolution.ambiguity_flags)

        received_at_value, timestamp_ambiguity_flags = _resolve_authoritative_timestamp(
            native_received_at, footer_timestamp_raw, footer_timestamp_kind,
            reference_date, timezone,
        )

        classification = self._classify_trader_identity(
            source_id, external_trader_id, trader_raw
        )

        merged_ambiguity_flags = (
            list(adapter_ambiguity_flags or [])
            + list(extraction_result["ambiguity_flags"])
            + expiration_ambiguity_flags
            + timestamp_ambiguity_flags
            + list(classification.ambiguity_flags)
        )

        # --- idempotency check ---
        existing = get_raw_message_by_channel_and_external_id(
            self.conn, channel_id, resolved_external_id
        )
        if existing is not None:
            return self._duplicate_outcome(
                sequence_in_batch, channel_id, existing, raw_text, resolved_external_id
            )

        # --- message-level SAVEPOINT: wraps deferred trader creation and
        # the raw_message insert attempt, so a confirmed-race duplicate
        # (see the sqlite3.IntegrityError handling below) can discard any
        # provisional trader (or other write made in this block) without
        # touching writes made before this point - an import_batches row
        # or previously processed messages within the same batch
        # transaction. Uniquely named per call; always released, on every
        # path, by the finally block. ---
        savepoint_name = f"r5_ingest_{uuid.uuid4().hex}"
        self.conn.execute(f"SAVEPOINT {savepoint_name}")
        try:
            # --- deferred trader creation - never for a duplicate message ---
            if classification.status == "needs_creation":
                trader_id = self._create_classified_trader(source_id, classification)
            elif classification.status == "resolved":
                trader_id = classification.trader_id
            else:
                trader_id = None

            # --- reserved R5 provenance metadata ---
            r5_provenance = {
                "cleaned_text": cleaned_text,
                "reference_date": reference_date,
                "timezone": timezone,
                "footer_timestamp_raw": footer_timestamp_raw,
                "footer_timestamp_kind": footer_timestamp_kind,
                "trader_raw": trader_raw,
                "external_trader_id": external_trader_id,
                "resolved_trader_id": trader_id,
                "channel_tags": list(channel_tags or []),
                "adapter_ambiguity_flags": list(adapter_ambiguity_flags or []),
            }
            if synthetic_id_input is not None:
                r5_provenance["synthetic_id_input"] = synthetic_id_input
            if native_received_at is not None:
                r5_provenance["native_received_at"] = native_received_at
            if header_timestamp_raw is not None:
                r5_provenance["header_timestamp_raw"] = header_timestamp_raw

            full_metadata = {**(metadata_extra or {}), _R5_PROVENANCE_KEY: r5_provenance}

            # --- insert raw_message (narrow unique-constraint race carve-out) ---
            try:
                raw_message = create_raw_message(
                    self.conn,
                    source_id,
                    raw_text,
                    external_id=resolved_external_id,
                    metadata=full_metadata,
                    received_at=received_at_value,
                    channel_id=channel_id,
                    import_batch_id=import_batch_id,
                    sequence_in_batch=sequence_in_batch,
                )
            except sqlite3.IntegrityError:
                raced_existing = get_raw_message_by_channel_and_external_id(
                    self.conn, channel_id, resolved_external_id
                )
                self.conn.execute(f"ROLLBACK TO {savepoint_name}")
                if raced_existing is not None:
                    return self._duplicate_outcome(
                        sequence_in_batch, channel_id, raced_existing, raw_text,
                        resolved_external_id,
                    )
                raise
        finally:
            self.conn.execute(f"RELEASE {savepoint_name}")

        extraction = create_message_extraction(
            self.conn,
            raw_message.id,
            PARSER_VERSION,
            extraction_result["parse_status"],
            ambiguity_flags=merged_ambiguity_flags or None,
        )

        trade_signal_ids: list = []
        action = extraction_result["action"]
        symbol = extraction_result["symbol"]
        if trader_id is not None and action is not None and symbol is not None:
            signal = create_trade_signal(
                self.conn,
                raw_message.id,
                trader_id,
                symbol,
                action,
                option_type=extraction_result["option_type"],
                price=extraction_result["price"],
                expiration=expiration,
                position_size=extraction_result["position_size"],
                strike=extraction_result["strike"],
                expiration_raw=extraction_result["expiration_raw"],
                event_type=extraction_result["event_type"],
                qualifier=extraction_result["qualifier"],
                stated_entry_price=extraction_result["stated_entry_price"],
                stated_return_pct=extraction_result["stated_return_pct"],
                notes=extraction_result["notes"],
                extraction_id=extraction.id,
            )
            trade_signal_ids.append(signal.id)

        return MessageIngestOutcome(
            sequence_in_batch=sequence_in_batch,
            outcome="stored",
            channel_id=channel_id,
            raw_message_id=raw_message.id,
            external_id=resolved_external_id,
            parse_status=extraction_result["parse_status"],
            trade_signal_ids=trade_signal_ids,
            ambiguity_flags=merged_ambiguity_flags,
            content_differs=None,
        )

    def ingest_batch(
        self,
        *,
        source_name: str,
        reference_date: str,
        timezone: str,
        raw_batch_text: str,
        channel_external_id: str | None = None,
        channel_name: str | None = None,
    ) -> BatchIngestResult:
        """Ingest one pasted Discord channel-history batch.

        Validates and segments (both pure, no database interaction)
        before ever opening a transaction. Owns one complete R5 write
        transaction covering source/channel resolution, the duplicate
        preflight, and every insert - see _r5_write_transaction(). A
        fully duplicate batch (every segmented message already present)
        creates no import_batches row and writes nothing. A batch where
        every intended-new message is reclassified as a duplicate via the
        narrow unique-constraint race carve-out has its otherwise-orphaned
        import_batches row removed before returning, so the result
        remains import_batch_id=None in that case too.

        Args:
            source_name: Source type name (e.g. 'discord'); looked up or
                created.
            reference_date: Strict "YYYY-MM-DD" date anchoring every
                message segmented from this batch. Required.
            timezone: A valid IANA timezone name, shared by every message
                in this batch. Required.
            raw_batch_text: The complete pasted channel-history block,
                exactly as supplied. Required, non-blank, and must
                segment into at least one message.
            channel_external_id: Source-provided channel id, or None to
                use the per-source sentinel "unspecified" channel.
            channel_name: Display name/slug to store if a new channel row
                is created.

        Returns:
            A BatchIngestResult summarizing every segmented message's
            outcome.

        Raises:
            ValueError: If any batch-wide anchor is invalid, or if
                raw_batch_text segments into zero messages. Zero writes
                occur in either case.
            RuntimeError: If self.conn already has unrelated pending work.
            sqlite3.Error: On an unexpected database failure (the whole
                transaction is rolled back before this propagates).
        """
        _validate_batch_inputs(source_name, reference_date, timezone, raw_batch_text)
        segmented_messages = segment_discord_batch(raw_batch_text)
        if not segmented_messages:
            raise ValueError("raw_batch_text produced no segmentable messages.")

        with self._r5_write_transaction():
            source = get_or_create_source(self.conn, source_name)
            channel = (
                get_or_create_channel(self.conn, source.id, channel_external_id, channel_name)
                if channel_external_id is not None
                else get_or_create_unspecified_channel(self.conn, source.id)
            )

            preflight = []
            for message in segmented_messages:
                resolved_id = _resolve_external_id(None, message.synthetic_id_input)
                existing = get_raw_message_by_channel_and_external_id(
                    self.conn, channel.id, resolved_id
                )
                preflight.append((message, resolved_id, existing))

            if all(existing is not None for _, _, existing in preflight):
                outcomes = [
                    self._duplicate_outcome(
                        message.sequence_in_batch, channel.id, existing,
                        message.raw_text, resolved_id,
                    )
                    for message, resolved_id, existing in preflight
                ]
                return BatchIngestResult(
                    import_batch_id=None,
                    channel_id=channel.id,
                    total_segmented=len(segmented_messages),
                    stored_count=0,
                    duplicate_count=len(segmented_messages),
                    unrecognized_count=0,
                    failed_count=0,
                    messages=outcomes,
                )

            import_batch = create_import_batch(
                self.conn, source.id, reference_date, timezone, raw_batch_text
            )

            outcomes = []
            for message, resolved_id, existing in preflight:
                if existing is not None:
                    outcomes.append(self._duplicate_outcome(
                        message.sequence_in_batch, channel.id, existing,
                        message.raw_text, resolved_id,
                    ))
                    continue
                outcomes.append(self._ingest_channel_message_no_commit(
                    source_id=source.id,
                    channel_id=channel.id,
                    source_name=source_name,
                    trader_raw=message.trader_raw,
                    external_trader_id=None,
                    raw_text=message.raw_text,
                    cleaned_text=message.cleaned_text,
                    resolved_external_id=resolved_id,
                    synthetic_id_input=message.synthetic_id_input,
                    native_received_at=None,
                    footer_timestamp_raw=message.footer_timestamp_raw,
                    footer_timestamp_kind=message.footer_timestamp_kind,
                    reference_date=reference_date,
                    timezone=timezone,
                    import_batch_id=import_batch.id,
                    sequence_in_batch=message.sequence_in_batch,
                    header_timestamp_raw=message.header_timestamp_raw,
                    channel_tags=message.channel_tags,
                    adapter_ambiguity_flags=message.ambiguity_flags,
                    metadata_extra=None,
                ))

            stored_count = sum(1 for outcome in outcomes if outcome.outcome == "stored")
            if stored_count == 0:
                # Every intended-new message was reclassified as a
                # duplicate via the narrow unique-constraint race
                # carve-out above - remove the now-orphaned, empty
                # import_batches row so the result matches the "fully
                # duplicate batch" contract exactly.
                delete_import_batch_if_empty(self.conn, import_batch.id)
                final_import_batch_id = None
            else:
                final_import_batch_id = import_batch.id

            return BatchIngestResult(
                import_batch_id=final_import_batch_id,
                channel_id=channel.id,
                total_segmented=len(segmented_messages),
                stored_count=stored_count,
                duplicate_count=sum(1 for o in outcomes if o.outcome == "duplicate"),
                unrecognized_count=sum(
                    1 for o in outcomes if o.parse_status == "unrecognized"
                ),
                failed_count=sum(1 for o in outcomes if o.parse_status == "failed"),
                messages=outcomes,
            )

    def reprocess_raw_message(self, raw_message_id: int) -> ReprocessOutcome:
        """Reprocess one raw message using its persisted R5 provenance.

        Owns one complete R5 write transaction. Never reruns segmentation
        and never touches raw_messages.raw_text or its metadata - both
        remain immutable. The prior current extraction (if any) is
        superseded, never deleted; old trade_signals rows tied to it are
        left exactly as they were.

        Args:
            raw_message_id: FK to raw_messages.id.

        Returns:
            A ReprocessOutcome describing the new current extraction and
            any newly-created trade_signals rows.

        Raises:
            ValueError: If raw_message_id does not exist.
            ReprocessingNotSupportedError: If the raw message has no R5
                provenance recorded (e.g. a legacy row, or one ingested
                via the unmodified TradeService.ingest_message() path). A
                ValueError subclass.
            RuntimeError: If self.conn already has unrelated pending work.
            sqlite3.Error: On an unexpected database failure (the
                transaction is rolled back before this propagates).
        """
        with self._r5_write_transaction():
            return self._reprocess_raw_message_no_commit(raw_message_id)

    def _reprocess_raw_message_no_commit(self, raw_message_id: int) -> ReprocessOutcome:
        """Private. Never begins, commits, or rolls back a transaction, and
        never modifies isolation_level - used by both public reprocessing
        methods."""
        raw_message = get_raw_message_by_id(self.conn, raw_message_id)
        if raw_message is None:
            raise ValueError(f"raw_message_id {raw_message_id} does not exist.")

        provenance = (raw_message.metadata or {}).get(_R5_PROVENANCE_KEY)
        if provenance is None:
            raise ReprocessingNotSupportedError(
                f"raw_message {raw_message_id} has no R5 provenance recorded "
                "and cannot be reprocessed."
            )

        cleaned_text = provenance["cleaned_text"]
        reference_date = provenance["reference_date"]
        timezone_name = provenance["timezone"]

        extraction_result = extract_trade_event(cleaned_text)

        expiration = None
        expiration_ambiguity_flags: list = []
        if extraction_result["expiration_raw"] is not None:
            expiration_resolution = resolve_expiration(
                extraction_result["expiration_raw"], reference_date
            )
            expiration = expiration_resolution.resolved_expiration
            expiration_ambiguity_flags = list(expiration_resolution.ambiguity_flags)

        # received_at is write-once, like raw_text - never recomputed or
        # rewritten here. Resolution is re-run only to recompute ambiguity
        # flags for the NEW extraction's own audit record.
        _, timestamp_ambiguity_flags = _resolve_authoritative_timestamp(
            provenance.get("native_received_at"),
            provenance.get("footer_timestamp_raw"),
            provenance.get("footer_timestamp_kind"),
            reference_date,
            timezone_name,
        )

        resolved_trader_id = provenance.get("resolved_trader_id")
        trader_still_exists = (
            resolved_trader_id is not None
            and get_trader_by_id(self.conn, resolved_trader_id) is not None
        )
        if trader_still_exists:
            trader_id = resolved_trader_id
            trader_ambiguity_flags: list = []
        else:
            # resolved_trader_id was never established, or no longer
            # exists - never blindly reuse a stale id. Reclassify from
            # persisted external_trader_id/trader_raw, never silently
            # selecting among multiple canonical matches even now.
            classification = self._classify_trader_identity(
                raw_message.source_id,
                provenance.get("external_trader_id"),
                provenance.get("trader_raw"),
            )
            if classification.status == "resolved":
                trader_id = classification.trader_id
            elif classification.status == "needs_creation":
                trader_id = self._create_classified_trader(
                    raw_message.source_id, classification
                )
            else:
                trader_id = None
            trader_ambiguity_flags = list(classification.ambiguity_flags)

        merged_ambiguity_flags = (
            list(provenance.get("adapter_ambiguity_flags") or [])
            + list(extraction_result["ambiguity_flags"])
            + expiration_ambiguity_flags
            + timestamp_ambiguity_flags
            + trader_ambiguity_flags
        )

        previous = get_current_extraction(self.conn, raw_message_id)
        if previous is not None:
            supersede_extraction(self.conn, previous.id)

        new_extraction = create_message_extraction(
            self.conn,
            raw_message_id,
            PARSER_VERSION,
            extraction_result["parse_status"],
            ambiguity_flags=merged_ambiguity_flags or None,
        )

        new_signal_ids: list = []
        action = extraction_result["action"]
        symbol = extraction_result["symbol"]
        if trader_id is not None and action is not None and symbol is not None:
            signal = create_trade_signal(
                self.conn,
                raw_message_id,
                trader_id,
                symbol,
                action,
                option_type=extraction_result["option_type"],
                price=extraction_result["price"],
                expiration=expiration,
                position_size=extraction_result["position_size"],
                strike=extraction_result["strike"],
                expiration_raw=extraction_result["expiration_raw"],
                event_type=extraction_result["event_type"],
                qualifier=extraction_result["qualifier"],
                stated_entry_price=extraction_result["stated_entry_price"],
                stated_return_pct=extraction_result["stated_return_pct"],
                notes=extraction_result["notes"],
                extraction_id=new_extraction.id,
            )
            new_signal_ids.append(signal.id)

        return ReprocessOutcome(
            raw_message_id=raw_message_id,
            previous_extraction_id=previous.id if previous is not None else None,
            new_extraction_id=new_extraction.id,
            parse_status=extraction_result["parse_status"],
            new_trade_signal_ids=new_signal_ids,
            ambiguity_flags=merged_ambiguity_flags,
        )

    def reprocess_import_batch(self, import_batch_id: int) -> ReprocessBatchResult:
        """Reprocess every raw message linked to one import batch.

        Owns exactly one transaction for the entire call - all-or-nothing.
        A normal per-message business result (any parse_status, or an
        empty new signal set) is never a failure and never aborts the
        loop; only an unexpected exception aborts the whole call, rolling
        back every supersession and insert made during it.

        Args:
            import_batch_id: FK to import_batches.id.

        Returns:
            A ReprocessBatchResult with one ReprocessOutcome per linked
            raw message, in raw_messages.id order.

        Raises:
            ValueError: If import_batch_id does not exist, or if it has
                zero linked raw messages.
            ReprocessingNotSupportedError: If any linked raw message has
                no R5 provenance recorded. A ValueError subclass.
            RuntimeError: If self.conn already has unrelated pending work.
            sqlite3.Error: On an unexpected database failure (the entire
                transaction is rolled back before this propagates).
        """
        with self._r5_write_transaction():
            batch = get_import_batch_by_id(self.conn, import_batch_id)
            if batch is None:
                raise ValueError(f"import_batch_id {import_batch_id} does not exist.")

            raw_message_ids = get_raw_message_ids_by_import_batch(self.conn, import_batch_id)
            if not raw_message_ids:
                raise ValueError(
                    f"import_batch_id {import_batch_id} contains no raw messages "
                    "eligible for reprocessing."
                )

            outcomes = [
                self._reprocess_raw_message_no_commit(raw_message_id)
                for raw_message_id in raw_message_ids
            ]
            return ReprocessBatchResult(import_batch_id=import_batch_id, outcomes=outcomes)

    def get_channel_checkpoints(self) -> list[ChannelCheckpoint]:
        """List the composite resume/audit checkpoint for every channel.

        Thin, read-only, no business rules, no transaction ownership
        (nothing is written) - matches list_trade_signals_for_review()'s
        existing delegation pattern.

        Returns:
            One ChannelCheckpoint per channel with at least one
            raw_messages row, ordered by channel_id. A channel with no
            resolved timestamp anywhere among its messages gets
            latest_received_at=None (and its two sibling fields None) -
            never substituted with insertion order.
        """
        ingestion_rows = {
            row["channel_id"]: row for row in get_channel_ingestion_cursors(self.conn)
        }
        chronological_rows = {
            row["channel_id"]: row
            for row in get_channel_chronological_checkpoints(self.conn)
        }

        checkpoints = []
        for channel_id in sorted(ingestion_rows):
            ingestion = ingestion_rows[channel_id]
            chronological = chronological_rows.get(channel_id)
            checkpoints.append(
                ChannelCheckpoint(
                    channel_id=channel_id,
                    channel_external_id=ingestion["channel_external_id"],
                    channel_name=ingestion["channel_name"],
                    latest_received_at=(
                        chronological["latest_received_at"] if chronological else None
                    ),
                    latest_received_raw_message_id=(
                        chronological["latest_received_raw_message_id"]
                        if chronological else None
                    ),
                    latest_received_external_id=(
                        chronological["latest_received_external_id"]
                        if chronological else None
                    ),
                    last_ingested_raw_message_id=ingestion["last_ingested_raw_message_id"],
                    last_ingested_at=ingestion["last_ingested_at"],
                    last_import_batch_id=ingestion["last_import_batch_id"],
                )
            )
        return checkpoints

    # -----------------------------------------------------------------
    # Recovery Milestone R6.4
    #
    # Lifecycle rebuild orchestration on top of database/lifecycle.py's
    # pure matching engine (R6.2) and database/repository.py's lifecycle
    # persistence layer (R6.3). Both public methods below own one complete
    # transaction via the existing _r5_write_transaction() context manager
    # - reused exactly as-is, despite its "r5" name, per this project's
    # existing transaction convention. Neither method touches
    # database/lifecycle.py's state-machine rules directly - both only
    # discover/compare/persist what that pure module computes.
    # -----------------------------------------------------------------

    def _supersede_one(self, trade_lifecycle_id: int, counters: _RebuildCounters) -> None:
        """Clear pointers from, then supersede, one lifecycle generation.
        Shared by the normal per-key rebuild path and the incomplete-
        identity singleton path."""
        cleared = clear_lifecycle_pointers_for_generation(self.conn, trade_lifecycle_id)
        counters.signal_pointers_cleared += cleared
        supersede_trade_lifecycle(self.conn, trade_lifecycle_id)
        counters.lifecycles_superseded += 1

    def _rebuild_one_key(self, key: tuple, counters: _RebuildCounters) -> None:
        """Rebuild every current lifecycle generation at one normal,
        complete lifecycle key - the shared algorithm behind both
        rebuild_all_lifecycles() and rebuild_lifecycles_for_raw_message_ids().

        Never touches an incomplete-identity signal or singleton - those
        are handled entirely by _process_incomplete_signal().
        """
        trader_id, symbol, option_type, strike, expiration = key

        existing_lifecycles = get_current_lifecycles_for_key(
            self.conn, trader_id, symbol, option_type, strike, expiration
        )

        first_member_raw_message_id: dict[int, int] = {}
        for lifecycle in existing_lifecycles:
            events = get_trade_lifecycle_events(self.conn, lifecycle.id)
            if not events:
                raise LifecycleIntegrityError(
                    [
                        f"Lifecycle {lifecycle.id} (current, key {key!r}) has no "
                        "membership events."
                    ]
                )
            first_raw_message_id = None
            for event in events:
                decoded = _validate_and_decode_snapshot(lifecycle.id, event)
                if first_raw_message_id is None:
                    first_raw_message_id = decoded["raw_message_id"]
            first_member_raw_message_id[lifecycle.id] = first_raw_message_id

        if existing_lifecycles:
            positions = get_chronological_positions_for_raw_messages(
                self.conn, list(first_member_raw_message_id.values())
            )
            ordered_existing_ids = sorted(
                first_member_raw_message_id.keys(),
                key=lambda lifecycle_id: positions[first_member_raw_message_id[lifecycle_id]],
            )
        else:
            ordered_existing_ids = []

        existing_shapes = [
            get_recorded_shape_for_generation(self.conn, lifecycle_id)[0]
            for lifecycle_id in ordered_existing_ids
        ]

        proposed_snapshots = get_current_trade_signals_for_key(
            self.conn, trader_id, symbol, option_type, strike, expiration
        )
        proposed_builds = (
            build_lifecycle_sequence(proposed_snapshots) if proposed_snapshots else []
        )
        proposed_shapes = [
            (build.status, build.remaining_fraction, tuple(build.member_signal_ids),
             tuple(build.ambiguity_flags))
            for build in proposed_builds
        ]

        counters.keys_considered += 1

        if proposed_shapes == existing_shapes:
            counters.keys_unchanged += 1
            return

        counters.keys_changed += 1

        for lifecycle_id in ordered_existing_ids:
            self._supersede_one(lifecycle_id, counters)

        snapshots_by_signal_id = {s.trade_signal_id: s for s in proposed_snapshots}
        new_ids = persist_lifecycle_builds(
            self.conn, trader_id, symbol, option_type, strike, expiration,
            proposed_builds, snapshots_by_signal_id,
        )
        counters.lifecycles_created += len(new_ids)
        member_count = sum(len(build.member_signal_ids) for build in proposed_builds)
        counters.lifecycle_events_created += member_count
        counters.signal_pointers_assigned += member_count

    def _is_unchanged_incomplete_singleton(
        self,
        old_lifecycle,
        events: list,
        decoded_events: list[dict],
        current_snapshot,
        raw_message_id: int,
    ) -> bool:
        """Return whether a persisted incomplete-identity singleton is
        provably identical to current_snapshot, using only recorded/
        persisted evidence (old_lifecycle's own columns and its already
        decoded-and-validated event snapshot(s)) - never by reconstructing
        historical evidence from current_snapshot itself, beyond the one
        legitimate comparison of current_snapshot's own identity/id fields
        against what was recorded.

        Args:
            old_lifecycle: The single stale incomplete TradeLifecycle
                candidate.
            events: Every TradeLifecycleEvent for old_lifecycle.id (already
                fetched by the caller), in sequence_index order.
            decoded_events: The result of calling
                _validate_and_decode_snapshot() on every element of events,
                in the same order - already validated by the caller before
                this method is ever called, so a malformed snapshot always
                raises LifecycleSnapshotError before reaching here.
            current_snapshot: The current SignalSnapshot for
                raw_message_id.
            raw_message_id: The raw_messages.id being processed.

        Returns:
            True only when every one of the following holds: old_lifecycle
            has exactly one recorded event, at sequence_index 1; its
            status is STATUS_UNRESOLVED with remaining_fraction == "0" and
            ambiguity_flags == [FLAG_INCOMPLETE_CONTRACT_IDENTITY] exactly;
            that one event's trade_signal_id matches
            current_snapshot.trade_signal_id; its validated snapshot's own
            recorded raw_message_id matches raw_message_id; and
            old_lifecycle's own normalized identity (trader_id,
            case-insensitive symbol, option_type, Decimal-equivalent
            strike, expiration) matches current_snapshot's. False
            otherwise - including zero or more-than-one recorded events,
            which are never treated as unchanged.
        """
        if old_lifecycle.status != STATUS_UNRESOLVED:
            return False
        if old_lifecycle.remaining_fraction != "0":
            return False
        if (old_lifecycle.ambiguity_flags or []) != [FLAG_INCOMPLETE_CONTRACT_IDENTITY]:
            return False
        if len(events) != 1:
            return False

        event = events[0]
        if event.sequence_index != 1:
            return False
        if event.trade_signal_id != current_snapshot.trade_signal_id:
            return False

        decoded = decoded_events[0]
        if decoded["raw_message_id"] != raw_message_id:
            return False

        old_key = _normalize_key_from_lifecycle(old_lifecycle)
        new_key = _normalize_key_from_snapshot(current_snapshot)
        return old_key == new_key

    def _process_incomplete_signal(
        self, raw_message_id: int, counters: _RebuildCounters
    ) -> None:
        """Handle one raw_message_id's incomplete-contract-identity
        singleton, independently of every normal lifecycle key.

        Never groups an incomplete signal into a normal key and never
        guesses its missing option component. Idempotent: an existing,
        exactly-matching current singleton is left completely untouched -
        proven only from recorded evidence (old_lifecycle's own columns
        plus its validated, decoded event snapshot(s)), never by
        reconstructing that history from the current live trade_signals
        row. Self-heals a stale singleton left over from before the
        signal either became complete-shaped (its own new key is handled
        separately by _rebuild_one_key()) or departed (no current signal
        at all remains for this raw_message_id).

        Every candidate stale singleton's membership is read and every one
        of its persisted signal_snapshot values is decoded/validated via
        _validate_and_decode_snapshot() before it is ever considered
        idempotent or superseded - a malformed or zero-event generation
        always aborts this entire rebuild call (propagating
        LifecycleIntegrityError/LifecycleSnapshotError, rolled back by the
        caller) rather than being silently superseded or trusted.
        """
        current_snapshot = get_current_signal_snapshot_for_raw_message(
            self.conn, raw_message_id
        )
        old_ids = get_current_lifecycle_ids_for_raw_message_ids(self.conn, [raw_message_id])

        stale_incomplete: list[tuple[int, object, list, list]] = []
        for old_id in old_ids:
            old_lifecycle = get_trade_lifecycle_by_id(self.conn, old_id)
            if old_lifecycle is None or _is_complete_key_shape(
                old_lifecycle.option_type, old_lifecycle.strike, old_lifecycle.expiration
            ):
                continue

            events = get_trade_lifecycle_events(self.conn, old_id)
            if not events:
                raise LifecycleIntegrityError(
                    [
                        f"Lifecycle {old_id} (current, incomplete-identity "
                        f"singleton for raw_message_id {raw_message_id}) has no "
                        "membership events."
                    ]
                )
            decoded_events = [
                _validate_and_decode_snapshot(old_id, event) for event in events
            ]
            stale_incomplete.append((old_id, old_lifecycle, events, decoded_events))

        is_currently_incomplete = current_snapshot is not None and not _is_complete_key_shape(
            current_snapshot.option_type, current_snapshot.strike, current_snapshot.expiration
        )

        if is_currently_incomplete:
            is_idempotent = False
            if len(stale_incomplete) == 1:
                old_id, old_lifecycle, events, decoded_events = stale_incomplete[0]
                is_idempotent = self._is_unchanged_incomplete_singleton(
                    old_lifecycle, events, decoded_events, current_snapshot, raw_message_id,
                )

            counters.keys_considered += 1
            if is_idempotent:
                counters.keys_unchanged += 1
                return

            counters.keys_changed += 1
            for old_id, _, _, _ in stale_incomplete:
                self._supersede_one(old_id, counters)

            strike = (
                Decimal(current_snapshot.strike) if current_snapshot.strike is not None else None
            )
            create_lifecycle_unresolved_singleton(
                self.conn, current_snapshot.trader_id, current_snapshot.symbol,
                current_snapshot.option_type, strike, current_snapshot.expiration,
                current_snapshot, FLAG_INCOMPLETE_CONTRACT_IDENTITY,
            )
            counters.lifecycles_created += 1
            counters.lifecycle_events_created += 1
            counters.signal_pointers_assigned += 1
            return

        # Not currently incomplete: the signal either became complete
        # (its own new key is rebuilt separately by _rebuild_one_key()) or
        # departed entirely. Either way, any stale incomplete singleton
        # left over from before must be superseded, with no replacement
        # created here.
        if not stale_incomplete:
            return
        counters.keys_considered += 1
        counters.keys_changed += 1
        for old_id, _, _, _ in stale_incomplete:
            self._supersede_one(old_id, counters)

    def rebuild_all_lifecycles(self) -> LifecycleRebuildResult:
        """Rebuild every lifecycle generation in the database from scratch.

        Owns one complete transaction (see _r5_write_transaction()):
        every discovery, supersession, persistence, pointer update, and
        the final integrity validation belong to this single transaction.
        Commits once on success; any exception rolls back the entire call,
        leaving every lifecycle row, membership row, and pointer exactly
        as it was before the call.

        Returns:
            A LifecycleRebuildResult summarizing every change made.

        Raises:
            LifecycleIntegrityError: If validate_lifecycle_membership_integrity()
                reports any violation after this call's writes. Rolled back
                before propagating.
            LifecycleSnapshotError: If any existing generation's persisted
                signal_snapshot cannot be safely decoded/validated. Rolled
                back before propagating.
            RuntimeError: If self.conn already has unrelated pending work.
            sqlite3.Error: On an unexpected database failure. Rolled back
                before propagating.
        """
        with self._r5_write_transaction():
            return self._rebuild_all_lifecycles_no_commit()

    def _rebuild_all_lifecycles_no_commit(self) -> LifecycleRebuildResult:
        """Private. Never begins, commits, or rolls back a transaction."""
        counters = _RebuildCounters()

        # Incomplete-identity singletons are processed first, so a signal
        # that has become complete-shaped since its singleton was created
        # is already freed (superseded, pointer cleared) by the time the
        # normal per-key pass below considers its new key - never leaving
        # the same signal a member of two current lifecycles at once.
        incomplete_snapshots = get_current_incomplete_lifecycle_signal_snapshots(self.conn)
        stale_singletons = get_current_incomplete_lifecycles(self.conn)
        incomplete_raw_message_ids = {s.raw_message_id for s in incomplete_snapshots}
        for singleton in stale_singletons:
            incomplete_raw_message_ids.update(
                get_trade_lifecycle_lineage_raw_message_ids(self.conn, singleton.id)
            )
        for raw_message_id in sorted(incomplete_raw_message_ids):
            self._process_incomplete_signal(raw_message_id, counters)

        all_signal_ids = get_all_current_lifecycle_eligible_signal_ids(self.conn)
        signal_driven_keys = set(
            get_distinct_lifecycle_keys_for_signal_ids(self.conn, all_signal_ids)
        )
        persisted_keys = {
            key for key in get_all_current_lifecycle_keys(self.conn)
            if _is_complete_key_shape(key[2], key[3], key[4])
        }
        all_keys = signal_driven_keys | persisted_keys

        for key in sorted(all_keys, key=_key_sort_key):
            self._rebuild_one_key(key, counters)

        violations = validate_lifecycle_membership_integrity(self.conn)
        if violations:
            raise LifecycleIntegrityError(violations)

        return counters.to_result()

    def rebuild_lifecycles_for_raw_message_ids(
        self, raw_message_ids: list[int]
    ) -> LifecycleRebuildResult:
        """Rebuild only the lifecycle keys affected by specific raw messages.

        Discovers the union of every "old" lifecycle key whose immutable
        lineage includes any of raw_message_ids, and every "new" complete
        lifecycle key belonging to the current lifecycle-eligible signal
        for each of raw_message_ids - so both a correction/reprocessing
        event's old and new side are always rebuilt, each exactly once,
        even when a key change means the old and new keys differ entirely.

        An empty raw_message_ids performs no writes at all (this method
        returns before ever opening a transaction) and returns a
        zero-valued result.

        Owns one complete transaction for a non-empty call (see
        _r5_write_transaction()): every discovery, supersession,
        persistence, pointer update, and the final integrity validation
        belong to this single transaction. Commits once on success; any
        exception rolls back the entire call.

        Args:
            raw_message_ids: The raw_messages.id values to rebuild the
                affected lifecycle keys for. Duplicates are deduplicated
                deterministically; order does not affect the result.

        Returns:
            A LifecycleRebuildResult summarizing every change made (all
            zero if raw_message_ids is empty).

        Raises:
            ValueError: If any raw_message_id does not exist - names every
                missing id, sorted. Raised before any write.
            LifecycleIntegrityError: If validate_lifecycle_membership_integrity()
                reports any violation after this call's writes. Rolled back
                before propagating.
            LifecycleSnapshotError: If any existing generation's persisted
                signal_snapshot cannot be safely decoded/validated. Rolled
                back before propagating.
            RuntimeError: If self.conn already has unrelated pending work.
            sqlite3.Error: On an unexpected database failure. Rolled back
                before propagating.
        """
        if not raw_message_ids:
            return _RebuildCounters().to_result()

        with self._r5_write_transaction():
            return self._rebuild_lifecycles_for_raw_message_ids_no_commit(raw_message_ids)

    def _rebuild_lifecycles_for_raw_message_ids_no_commit(
        self, raw_message_ids: list[int]
    ) -> LifecycleRebuildResult:
        """Private. Never begins, commits, or rolls back a transaction."""
        counters = _RebuildCounters()

        unique_ids = sorted(set(raw_message_ids))
        # Fails closed with a ValueError naming every missing id, sorted -
        # before any write. The returned positions mapping itself is not
        # needed here; this call's only purpose at this point is the
        # existence check it already performs.
        get_chronological_positions_for_raw_messages(self.conn, unique_ids)

        old_lifecycle_ids = get_current_lifecycle_ids_for_raw_message_ids(
            self.conn, unique_ids
        )
        old_keys: set = set()
        old_incomplete_lifecycle_ids: set = set()
        for old_id in old_lifecycle_ids:
            lifecycle = get_trade_lifecycle_by_id(self.conn, old_id)
            if _is_complete_key_shape(
                lifecycle.option_type, lifecycle.strike, lifecycle.expiration
            ):
                old_keys.add(_normalize_key_from_lifecycle(lifecycle))
            else:
                old_incomplete_lifecycle_ids.add(old_id)

        new_keys: set = set()
        new_incomplete_raw_message_ids: set = set()
        for raw_message_id in unique_ids:
            current_snapshot = get_current_signal_snapshot_for_raw_message(
                self.conn, raw_message_id
            )
            if current_snapshot is None:
                continue
            if _is_complete_key_shape(
                current_snapshot.option_type, current_snapshot.strike,
                current_snapshot.expiration,
            ):
                new_keys.add(_normalize_key_from_snapshot(current_snapshot))
            else:
                new_incomplete_raw_message_ids.add(raw_message_id)

        # Incomplete-identity singletons are processed first - see
        # _rebuild_all_lifecycles_no_commit()'s equivalent comment for why.
        incomplete_raw_message_ids = set(new_incomplete_raw_message_ids)
        for old_id in old_incomplete_lifecycle_ids:
            incomplete_raw_message_ids.update(
                get_trade_lifecycle_lineage_raw_message_ids(self.conn, old_id)
            )
        for raw_message_id in sorted(incomplete_raw_message_ids):
            self._process_incomplete_signal(raw_message_id, counters)

        all_keys = old_keys | new_keys
        for key in sorted(all_keys, key=_key_sort_key):
            self._rebuild_one_key(key, counters)

        violations = validate_lifecycle_membership_integrity(self.conn)
        if violations:
            raise LifecycleIntegrityError(violations)

        return counters.to_result()

    def correct_trade_signal(
        self,
        trade_signal_id: int,
        expected_current_values: dict,
        **changed_fields,
    ) -> TradeSignalCorrectionResult:
        """Lifecycle-safe controlled correction (Recovery Milestone R6.5a).

        The lifecycle-aware counterpart to the pre-existing controlled-
        correction mode of update_trade_signal() (Milestone 2D.5): that
        method's legacy and controlled-correction contracts are completely
        unmodified by this method's addition. This method additionally
        integrates the correction with the R6 lifecycle system so a
        correction can never leave lifecycle events, memberships,
        pointers, audit records, or snapshots inconsistent.

        Owns one complete transaction (see _r5_write_transaction()):
        validating the correction, verifying expected_current_values,
        writing the correction audit record, updating the trade signal,
        rebuilding affected lifecycles when required, and the final
        lifecycle-membership integrity validation all belong to this one
        transaction. Commits exactly once on success; any exception rolls
        back the entire operation.

        changed_fields must contain exactly the six approved correction
        fields (symbol, action, option_type, price, expiration,
        position_size) - never raw_message_id, trader_id, strike,
        event_type, lifecycle_id, or any other field.
        expected_current_values must contain exactly the same six keys,
        typed as symbol: str, action: str, option_type: str | None,
        price: Decimal | None, expiration: str | None,
        position_size: str | None - never a float, and never compared
        against an unparsed price string.

        Rebuild-decision rules:
          - If the current signal is lifecycle-managed (event_type IS NOT
            NULL) and symbol, option_type, or expiration would effectively
            change, a targeted lifecycle rebuild is performed for the
            signal's raw_message_id after the update.
          - If the current signal is a legacy signal (event_type IS NULL)
            that unexpectedly carries stale lifecycle state (a non-NULL
            lifecycle_id pointer, or existing lifecycle-event lineage for
            its raw_message_id), a targeted lifecycle rebuild is performed
            for its raw_message_id regardless of which fields changed, so
            the stale membership can be safely removed.
          - Otherwise no rebuild is performed, but the final lifecycle-
            membership integrity validation still runs before commit.

        Action safety: if action would effectively change and the current
        signal is lifecycle-managed (event_type IS NOT NULL), the entire
        correction is rejected with LifecycleUnsafeCorrectionError before
        any audit row, signal update, or lifecycle mutation - never
        partially applied. A legacy signal (event_type IS NULL) may still
        receive an action correction under the existing controlled-
        correction rules. Supplying the same action as already stored is
        treated as a no-op contribution, not an unsafe action change.

        Args:
            trade_signal_id: Primary key of the trade signal to correct.
            expected_current_values: The canonical typed six-field
                snapshot the caller believes is still current.
            **changed_fields: The six approved correction fields' proposed
                new values.

        Returns:
            A TradeSignalCorrectionResult with the authoritative, reloaded
            corrected trade signal and the lifecycle rebuild outcome (a
            zero-valued LifecycleRebuildResult when no rebuild occurred).

        Raises:
            ValueError: If changed_fields or expected_current_values does
                not contain exactly the six approved fields, if
                changed_fields fails the shared repository validation, or
                if changed_fields is identical to the current persisted
                values (a no-op).
            TradeSignalNotFoundError: If no trade signal exists with
                trade_signal_id. A ValueError subclass.
            StaleTradeSignalError: If expected_current_values no longer
                matches the actual persisted values.
            LifecycleUnsafeCorrectionError: If action would effectively
                change on a lifecycle-managed signal. A ValueError
                subclass.
            LifecycleIntegrityError: If validate_lifecycle_membership_integrity()
                reports any violation after this call's writes. Rolled back
                before propagating.
            LifecycleSnapshotError: If any existing generation's persisted
                signal_snapshot cannot be safely decoded/validated during a
                required rebuild. Rolled back before propagating.
            RuntimeError: If self.conn already has unrelated pending work.
            TypeError: If price is supplied and is not a Decimal.
            sqlite3.Error: On an unexpected database failure. Rolled back
                before propagating.
        """
        if set(changed_fields) != _CORRECTION_FIELDS:
            raise ValueError(
                "A lifecycle-safe correction must supply exactly the "
                f"approved correction fields: {sorted(_CORRECTION_FIELDS)}."
            )
        if set(expected_current_values) != _CORRECTION_FIELDS:
            raise ValueError(
                "expected_current_values must supply exactly the approved "
                f"correction fields: {sorted(_CORRECTION_FIELDS)}."
            )

        validate_trade_signal_update_fields(changed_fields)

        with self._r5_write_transaction():
            return self._correct_trade_signal_no_commit(
                trade_signal_id, expected_current_values, changed_fields
            )

    def _correct_trade_signal_no_commit(
        self,
        trade_signal_id: int,
        expected_current_values: dict,
        changed_fields: dict,
    ) -> TradeSignalCorrectionResult:
        """Private. Never begins, commits, or rolls back a transaction."""
        existing = get_trade_signal_by_id(self.conn, trade_signal_id)
        if existing is None:
            raise TradeSignalNotFoundError(
                f"No trade signal exists with id {trade_signal_id}."
            )

        current_values = _current_correction_values(existing)

        if expected_current_values != current_values:
            raise StaleTradeSignalError(
                "The trade signal's current values no longer match the "
                "values expected for this correction."
            )

        if changed_fields == current_values:
            raise ValueError("A correction must change at least one field.")

        changed_field_names = {
            field for field, value in changed_fields.items()
            if value != current_values[field]
        }

        if "action" in changed_field_names and existing.event_type is not None:
            raise LifecycleUnsafeCorrectionError(
                "Cannot change action on a lifecycle-managed trade signal "
                f"(trade_signal_id {trade_signal_id}, event_type "
                f"{existing.event_type!r}). Correct the underlying message "
                "and reprocess instead."
            )

        create_trade_signal_edit(self.conn, trade_signal_id, asdict(existing))

        _repository_update_trade_signal(self.conn, trade_signal_id, **changed_fields)

        key_fields_changed = bool(
            changed_field_names & {"symbol", "option_type", "expiration"}
        )

        if existing.event_type is not None:
            needs_rebuild = key_fields_changed
        else:
            needs_rebuild = existing.lifecycle_id is not None or bool(
                get_current_lifecycle_ids_for_raw_message_ids(
                    self.conn, [existing.raw_message_id]
                )
            )

        if needs_rebuild:
            rebuild_result = self._rebuild_lifecycles_for_raw_message_ids_no_commit(
                [existing.raw_message_id]
            )
        else:
            violations = validate_lifecycle_membership_integrity(self.conn)
            if violations:
                raise LifecycleIntegrityError(violations)
            rebuild_result = _RebuildCounters().to_result()

        corrected_signal = get_trade_signal_by_id(self.conn, trade_signal_id)

        return TradeSignalCorrectionResult(
            trade_signal=corrected_signal,
            lifecycle_rebuild_performed=needs_rebuild,
            lifecycle_rebuild_result=rebuild_result,
        )
