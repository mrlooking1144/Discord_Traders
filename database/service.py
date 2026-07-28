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
from database.models import (
    BatchIngestResult,
    ChannelCheckpoint,
    MessageIngestOutcome,
    ReprocessBatchResult,
    ReprocessOutcome,
    TradeSignal,
)
from database.repository import (
    create_import_batch,
    create_message_extraction,
    create_raw_message,
    create_trade_signal,
    create_trade_signal_edit,
    create_trader,
    delete_import_batch_if_empty,
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
    get_trade_signal_edits,
    get_trade_signals_for_review,
    get_trade_signals_matching,
    get_trader_by_external_id,
    get_trader_by_id,
    get_traders_by_canonical_name,
    supersede_extraction,
    update_trade_signal as _repository_update_trade_signal,
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
