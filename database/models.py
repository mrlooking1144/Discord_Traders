"""Version 1 data models for Discord Traders.

Plain dataclasses mirroring the five tables in database/schema.sql. These
are the typed boundary between the database layer and the rest of the
application. They contain no database access, validation, or business logic.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Source:
    """Mirrors the sources table.

    Attributes:
        id: Primary key. None until the row is persisted.
        name: Source type name (e.g. 'discord'). Required, unique.
    """

    name: str
    id: Optional[int] = None


@dataclass
class Trader:
    """Mirrors the traders table.

    Attributes:
        id: Primary key. None until the row is persisted.
        source_id: FK to sources.id. Required.
        name: Display name/handle as seen in the source. Required.
        external_trader_id: Stable source-provided trader ID, when available.
        created_at: ISO8601 timestamp. None to let the database default apply.
        canonical_name: Lowercased/trimmed name, used for case-insensitive
            identity resolution (e.g. "Matae" and "matae" are the same
            trader). None only for rows persisted before this column
            existed and not yet backfilled.
    """

    source_id: int
    name: str
    id: Optional[int] = None
    external_trader_id: Optional[str] = None
    created_at: Optional[str] = None
    canonical_name: Optional[str] = None


@dataclass
class RawMessage:
    """Mirrors the raw_messages table.

    Attributes:
        id: Primary key. None until the row is persisted.
        source_id: FK to sources.id. Required.
        raw_text: The original message, verbatim. Required. Write-once:
            never updated once persisted.
        content_hash: Hash of raw_text, computed at insert. Required.
        external_id: Source-provided message ID, when available.
        metadata: Opaque JSON blob of source-specific extras.
        received_at: ISO8601 timestamp of when the source sent the message.
        ingested_at: ISO8601 timestamp. None to let the database default apply.
        channel_id: FK to channels.id, when this message is scoped to a
            specific source channel. None for sources/entries with no
            channel concept.
        import_batch_id: FK to import_batches.id, when this message was
            segmented from a batch paste. None for single-message entry.
        sequence_in_batch: Position of this message within its import
            batch, used for checkpoint ordering. None outside batch import.
    """

    source_id: int
    raw_text: str
    content_hash: str
    id: Optional[int] = None
    external_id: Optional[str] = None
    metadata: Optional[str] = None
    received_at: Optional[str] = None
    ingested_at: Optional[str] = None
    channel_id: Optional[int] = None
    import_batch_id: Optional[int] = None
    sequence_in_batch: Optional[int] = None


@dataclass
class TradeSignal:
    """Mirrors the trade_signals table.

    Attributes:
        id: Primary key. None until the row is persisted.
        raw_message_id: FK to raw_messages.id. Required.
        trader_id: FK to traders.id. Required.
        symbol: Ticker symbol. Required.
        action: Free-text trade action (e.g. BTO/STC, or BOUGHT/SOLD),
            stored exactly as supplied and never aliased. Required.
        option_type: Free-text call/put, or None for non-option trades.
        price: Normalized decimal string (e.g. "3.25"), or None.
        expiration: ISO8601 date string, or None.
        position_size: Raw wording of position size, or None.
        created_at: ISO8601 timestamp. None to let the database default apply.
        updated_at: ISO8601 timestamp. None to let the database default apply.
        strike: Normalized decimal string (e.g. "207.5"), or None.
        expiration_raw: Verbatim expiration token as it appeared in the
            message (e.g. "07/24"), before year resolution, or None.
        event_type: Derived lifecycle event kind - exactly one of ENTRY,
            ADD, ROLL_UP, PARTIAL_EXIT, FULL_EXIT (per the Recovery
            Milestone R3 extractor grammar; a stop-out is a FULL_EXIT
            whose reason is preserved verbatim in notes, never a separate
            event type) - or None.
        qualifier: Raw fraction text (e.g. "1/2"), "ALL OUT", or a bracket
            annotation (e.g. "[SMALL]"), or None.
        stated_entry_price: Normalized decimal string of the entry price as
            stated in the message's own "$OLD -> $NEW" line, or None.
        stated_return_pct: Normalized decimal string of the return
            percentage as stated in the message, or None. Advisory only -
            execution prices are the source of truth for computed returns.
        notes: Free-text commentary from the message (e.g. "HIT STOP"),
            or None.
        extraction_id: FK to message_extractions.id identifying which parse
            attempt produced this row, or None for rows persisted before
            this column existed.
        lifecycle_id: FK to trade_lifecycles.id identifying this signal's
            current lifecycle generation (Recovery Milestone R6), or None
            if this signal belongs to no lifecycle - always None for a
            legacy signal with event_type None, and None until the R6
            lifecycle engine (not yet implemented as of R6.1) links it.
            The one narrow, maintained exception to this model's
            otherwise immutable fields - never written by ingestion,
            reprocessing-of-extraction, or the correction workflow
            directly.
    """

    raw_message_id: int
    trader_id: int
    symbol: str
    action: str
    id: Optional[int] = None
    option_type: Optional[str] = None
    price: Optional[str] = None
    expiration: Optional[str] = None
    position_size: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    strike: Optional[str] = None
    expiration_raw: Optional[str] = None
    event_type: Optional[str] = None
    qualifier: Optional[str] = None
    stated_entry_price: Optional[str] = None
    stated_return_pct: Optional[str] = None
    notes: Optional[str] = None
    extraction_id: Optional[int] = None
    lifecycle_id: Optional[int] = None


@dataclass
class TradeSignalEdit:
    """Mirrors the trade_signal_edits table.

    Attributes:
        id: Primary key. None until the row is persisted.
        trade_signal_id: FK to trade_signals.id. Required.
        previous_values: Full-row JSON snapshot before the edit. Required.
        edited_at: ISO8601 timestamp. None to let the database default apply.
    """

    trade_signal_id: int
    previous_values: str
    id: Optional[int] = None
    edited_at: Optional[str] = None


@dataclass
class Channel:
    """Mirrors the channels table.

    Attributes:
        id: Primary key. None until the row is persisted.
        source_id: FK to sources.id. Required.
        external_channel_id: Stable source-provided channel ID, when
            available. A per-source sentinel value is used for messages
            with no real channel identifier.
        name: Display name/slug for the channel, when available.
        created_at: ISO8601 timestamp. None to let the database default apply.
    """

    source_id: int
    id: Optional[int] = None
    external_channel_id: Optional[str] = None
    name: Optional[str] = None
    created_at: Optional[str] = None


@dataclass
class ImportBatch:
    """Mirrors the import_batches table.

    Attributes:
        id: Primary key. None until the row is persisted.
        source_id: FK to sources.id. Required.
        reference_date: Calendar date ("YYYY-MM-DD") used to resolve
            year-less expirations and "Today at HH:MM" timestamps for every
            message segmented from this batch. Required, never the wall
            clock.
        timezone: Timezone name used alongside reference_date. Required.
        raw_input_text: The complete pasted batch text, before
            segmentation, or None.
        created_at: ISO8601 timestamp. None to let the database default apply.
    """

    source_id: int
    reference_date: str
    timezone: str
    id: Optional[int] = None
    raw_input_text: Optional[str] = None
    created_at: Optional[str] = None


@dataclass
class MessageExtraction:
    """Mirrors the message_extractions table.

    One row per parse attempt against a raw message. Reprocessing inserts a
    new row and marks the prior current row superseded; raw_messages itself
    is never modified.

    Attributes:
        id: Primary key. None until the row is persisted.
        raw_message_id: FK to raw_messages.id. Required.
        parser_version: Identifier of the extractor version that produced
            this attempt. Required.
        parse_status: One of 'parsed', 'partially_parsed', 'unrecognized',
            'failed'. Required.
        confidence: Extraction confidence in [0, 1], or None.
        ambiguity_flags: List of flag strings (e.g.
            "ambiguous_add_no_open_position"), or None.
        is_current: True if this is the active (non-superseded) extraction
            for its raw message. At most one row per raw_message_id may be
            current, enforced by a partial unique index.
        superseded_at: ISO8601 timestamp of when this row was superseded by
            a later extraction, or None if still current.
        created_at: ISO8601 timestamp. None to let the database default apply.
    """

    raw_message_id: int
    parser_version: str
    parse_status: str
    id: Optional[int] = None
    confidence: Optional[float] = None
    ambiguity_flags: Optional[list] = None
    is_current: bool = True
    superseded_at: Optional[str] = None
    created_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Recovery Milestone R5: result models.
#
# These are plain, frozen result dataclasses returned by
# database.service.TradeService's new R5 orchestration methods
# (ingest_channel_message, ingest_batch, reprocess_raw_message,
# reprocess_import_batch, get_channel_checkpoints). Unlike the models
# above, they do not mirror a database table row-for-row - they are
# purpose-built return shapes for one call's outcome. They contain no
# database access, validation, or business logic. Unexpected exceptions
# are never represented by any of these dataclasses - they always
# propagate as raised Python exceptions instead.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MessageIngestOutcome:
    """One message's ingestion result, from ingest_channel_message() or one
    message within ingest_batch().

    Attributes:
        sequence_in_batch: 1-indexed position within its batch, or None for
            a message ingested via a direct (non-batch)
            ingest_channel_message() call.
        outcome: Exactly "stored" or "duplicate". Never represents an
            unexpected exception - those propagate as raised Python
            exceptions and are never captured as a result value.
        channel_id: FK to channels.id. Always populated.
        raw_message_id: The new row's id (outcome="stored") or the
            pre-existing row's id (outcome="duplicate"). Always populated.
        external_id: The resolved (real or synthetic) external_id used for
            this message. Always populated.
        parse_status: One of 'parsed'/'partially_parsed'/'unrecognized'/
            'failed' when outcome == "stored"; None when outcome ==
            "duplicate", since no extraction is attempted for a recognized
            duplicate.
        trade_signal_ids: ids of every trade_signals row created for this
            message. Empty (never None) when the signal-creation gate did
            not pass, or when outcome == "duplicate".
        ambiguity_flags: Merged adapter/extractor/resolver/trader-identity
            flags. Empty (never None) when outcome == "duplicate".
        content_differs: True/False when outcome == "duplicate" (whether
            the newly-supplied raw_text's content hash differs from the
            existing row's stored content_hash); None when outcome ==
            "stored" (not applicable).
    """

    sequence_in_batch: Optional[int]
    outcome: str
    channel_id: int
    raw_message_id: int
    external_id: str
    parse_status: Optional[str]
    trade_signal_ids: list[int] = field(default_factory=list)
    ambiguity_flags: list[str] = field(default_factory=list)
    content_differs: Optional[bool] = None


@dataclass(frozen=True)
class BatchIngestResult:
    """Result of one TradeService.ingest_batch() call.

    Attributes:
        import_batch_id: The newly-created import_batches.id, or None when
            every segmented message was already present (a fully
            duplicate/no-op batch), or when every intended new message was
            reclassified as a duplicate via the narrow unique-constraint
            race carve-out, leaving zero stored messages.
        channel_id: FK to channels.id. Always populated.
        total_segmented: Count segment_discord_batch() produced from the
            pasted batch text.
        stored_count: Count of messages with outcome == "stored"
            (regardless of parse_status - unrecognized/failed extractions
            still count as "stored", since the raw message and extraction
            persist either way).
        duplicate_count: Count of messages with outcome == "duplicate".
        unrecognized_count: Subset of stored_count where
            parse_status == "unrecognized".
        failed_count: Subset of stored_count where parse_status ==
            "failed" (extract_trade_event()'s own internal-error status,
            not a Python exception).
        messages: One MessageIngestOutcome per segmented message, in
            sequence_in_batch order, including duplicates.
    """

    import_batch_id: Optional[int]
    channel_id: int
    total_segmented: int
    stored_count: int
    duplicate_count: int
    unrecognized_count: int
    failed_count: int
    messages: list[MessageIngestOutcome] = field(default_factory=list)


@dataclass(frozen=True)
class ReprocessOutcome:
    """Result of reprocessing one raw message.

    Attributes:
        raw_message_id: The reprocessed row's id.
        previous_extraction_id: The prior current extraction's id, or None
            if this raw message had no current extraction before this call
            (e.g. its first-ever extraction attempt).
        new_extraction_id: The newly-created, now-current extraction's id.
        parse_status: The new extraction's parse_status.
        new_trade_signal_ids: ids of every new trade_signals row created,
            linked to new_extraction_id. Empty (never None) if the
            signal-creation gate did not pass.
        ambiguity_flags: The new extraction's merged ambiguity flags.
    """

    raw_message_id: int
    previous_extraction_id: Optional[int]
    new_extraction_id: int
    parse_status: str
    new_trade_signal_ids: list[int] = field(default_factory=list)
    ambiguity_flags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReprocessBatchResult:
    """Result of TradeService.reprocess_import_batch().

    Attributes:
        import_batch_id: The import_batches.id that was reprocessed.
            Deliberately not channel_id - reprocessing scope is
            batch-level, and this field name reflects exactly what was
            requested and processed.
        outcomes: One ReprocessOutcome per raw message linked to this
            import_batch_id, in raw_messages.id order.
    """

    import_batch_id: int
    outcomes: list[ReprocessOutcome] = field(default_factory=list)


@dataclass(frozen=True)
class ChannelCheckpoint:
    """One channel's composite resume/audit checkpoint.

    Attributes:
        channel_id: FK to channels.id.
        channel_external_id: The channel's external_channel_id, or None.
        channel_name: The channel's display name, or None.
        latest_received_at: Canonical UTC ISO8601 string
            ("YYYY-MM-DDTHH:MM:SS.ffffff+00:00"), the maximum non-NULL
            raw_messages.received_at for this channel; None when no
            message in this channel has any resolved timestamp at all -
            this explicitly signals "chronological resume information
            unavailable" and must never be substituted with insertion
            order.
        latest_received_raw_message_id: The specific raw_messages.id
            latest_received_at came from, or None exactly when
            latest_received_at is None.
        latest_received_external_id: That same message's external_id, or
            None exactly when latest_received_at is None.
        last_ingested_raw_message_id: MAX(raw_messages.id) for this
            channel - always populated (every channel returned here has
            at least one raw_messages row).
        last_ingested_at: That same row's ingested_at.
        last_import_batch_id: That same row's import_batch_id, or None for
            a channel whose most recently inserted message came from
            single-message ingestion outside any batch.
    """

    channel_id: int
    channel_external_id: Optional[str]
    channel_name: Optional[str]
    latest_received_at: Optional[str]
    latest_received_raw_message_id: Optional[int]
    latest_received_external_id: Optional[str]
    last_ingested_raw_message_id: int
    last_ingested_at: str
    last_import_batch_id: Optional[int]


# ---------------------------------------------------------------------------
# Recovery Milestone R6.1: trade_lifecycles / trade_lifecycle_events models.
#
# These mirror the two new tables added by
# database/migrations/0007_trade_lifecycles.sql field-for-field, following
# the same convention as every table-mirroring model above (Source,
# Trader, RawMessage, TradeSignal, TradeSignalEdit, Channel, ImportBatch,
# MessageExtraction): plain data shape only, no database access, no
# validation, no business logic. R6.1 adds schema, migration, and these
# models only - no lifecycle matching/linking behavior exists yet (that is
# Recovery Milestones R6.2-R6.4); nothing in this codebase constructs a
# TradeLifecycle or TradeLifecycleEvent as of R6.1.
#
# Unlike the R5 result dataclasses above (MessageIngestOutcome and
# following), which are frozen, purpose-built call-result shapes with no
# database row of their own, TradeLifecycle and TradeLifecycleEvent each
# mirror one real table row - so they are declared frozen dataclasses here
# for the same reason the R5 result models are frozen (a persisted
# lifecycle generation, once created, is never mutated in place except via
# the is_current/superseded_at bookkeeping the lifecycle engine will apply
# by constructing a new model instance from a fresh row read, not by
# mutating an existing one), while still mirroring a table row like the
# earlier, non-frozen models above.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TradeLifecycle:
    """Mirrors the trade_lifecycles table.

    One row per lifecycle *generation* - the persisted outcome of
    replaying one (trader_id, symbol, option_type, strike, expiration)
    key's current signal history through the lifecycle-matching engine
    (not yet implemented as of Recovery Milestone R6.1). A generation is
    never edited in place once created, aside from is_current/
    superseded_at - reprocessing or a key-changing correction supersedes
    the old row and inserts a fresh one, mirroring MessageExtraction's
    is_current/superseded_at contract exactly. Unlike MessageExtraction's
    strict one-current-per-raw-message invariant, a single lifecycle key
    may legitimately have multiple simultaneously current rows over time
    (each a distinct re-entry).

    Attributes:
        id: Primary key. None until the row is persisted.
        trader_id: FK to traders.id. Required.
        symbol: Ticker symbol. Required.
        option_type: Free-text call/put, or None for an equity key.
        strike: Normalized decimal string (e.g. "207.5"), or None for an
            equity key.
        expiration: Resolved ISO8601 date string, or None for an equity
            key.
        status: Exactly one of 'open', 'partially_closed', 'closed',
            'orphan', 'unresolved', 'invalid'. Required. A stop-out is
            represented as 'closed' plus the closing signal's own notes -
            there is no separate 'stopped' status.
        remaining_fraction: The exact string form of a fractions.Fraction
            (e.g. "1", "1/2", "5/6", "0"), never a Decimal string - several
            approved fraction tokens (1/3, 1/6) do not terminate in base
            10, and exact rational arithmetic avoids any rounding-residue
            risk when checking whether an exit exactly zeroes the
            remaining position. Required.
        opened_by_signal_id: FK to trade_signals.id for the signal that
            opened this generation, or None (an 'orphan'/'unresolved'
            generation may have no verified opening signal).
        closed_by_signal_id: FK to trade_signals.id for the signal that
            brought remaining_fraction to zero, or None (an 'open'/
            'partially_closed'/'unresolved' generation has none yet).
        is_current: True if this is the active (non-superseded)
            generation. Unlike MessageExtraction, this is not a
            per-key-exclusive flag - see the class docstring above.
        superseded_at: ISO8601 timestamp of when this row was superseded
            by a rebuild, or None if still current.
        ambiguity_flags: List of flag strings (e.g.
            "ambiguous_add_no_open_position"), or None.
        created_at: ISO8601 timestamp. None to let the database default
            apply.
        updated_at: ISO8601 timestamp. None to let the database default
            apply.
    """

    trader_id: int
    symbol: str
    status: str
    remaining_fraction: str
    id: Optional[int] = None
    option_type: Optional[str] = None
    strike: Optional[str] = None
    expiration: Optional[str] = None
    opened_by_signal_id: Optional[int] = None
    closed_by_signal_id: Optional[int] = None
    is_current: bool = True
    superseded_at: Optional[str] = None
    ambiguity_flags: Optional[list] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass(frozen=True)
class TradeLifecycleEvent:
    """Mirrors the trade_lifecycle_events table.

    The membership/audit table linking one TradeLifecycle generation to
    the trade_signals rows that made it up, in chronological order
    (sequence_index). No repository function ever updates or deletes a
    row in this table once created, matching raw_messages.raw_text's
    existing write-once contract - this is what keeps a superseded
    generation's original membership auditable even after later
    reprocessing or correction.

    Attributes:
        id: Primary key. None until the row is persisted.
        trade_lifecycle_id: FK to trade_lifecycles.id. Required.
        trade_signal_id: FK to trade_signals.id. Required.
        sequence_index: 1-based order of this signal within its
            generation. Required.
        signal_snapshot: Immutable canonical JSON captured at the moment
            this row is created - never updated or deleted afterward.
            Required (NOT NULL at the schema level). Captures, at
            minimum, this signal's trade_signal_id, raw_message_id,
            trader_id, symbol, option_type, strike, expiration,
            event_type, qualifier, action, price, stated_entry_price,
            stated_return_pct, notes, extraction_id, and the exact
            ordering key used to place it within this generation's
            replay - so a later correction to the live trade_signals row
            (e.g. its price, per the existing 2D.5 correction workflow)
            can never silently alter what this generation is recorded as
            having been built from.
        created_at: ISO8601 timestamp. None to let the database default
            apply.
    """

    trade_lifecycle_id: int
    trade_signal_id: int
    sequence_index: int
    signal_snapshot: str
    id: Optional[int] = None
    created_at: Optional[str] = None
