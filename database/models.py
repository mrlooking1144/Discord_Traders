"""Version 1 data models for Discord Traders.

Plain dataclasses mirroring the five tables in database/schema.sql. These
are the typed boundary between the database layer and the rest of the
application. They contain no database access, validation, or business logic.
"""

from dataclasses import dataclass
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
        event_type: Derived lifecycle event kind (e.g. ENTRY, ADD, ROLL_UP,
            PARTIAL_EXIT, FULL_EXIT, STOP_EXIT), or None.
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
