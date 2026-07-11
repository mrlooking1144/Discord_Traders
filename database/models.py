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
    """

    source_id: int
    name: str
    id: Optional[int] = None
    external_trader_id: Optional[str] = None
    created_at: Optional[str] = None


@dataclass
class RawMessage:
    """Mirrors the raw_messages table.

    Attributes:
        id: Primary key. None until the row is persisted.
        source_id: FK to sources.id. Required.
        raw_text: The original message, verbatim. Required.
        content_hash: Hash of raw_text, computed at insert. Required.
        external_id: Source-provided message ID, when available.
        metadata: Opaque JSON blob of source-specific extras.
        received_at: ISO8601 timestamp of when the source sent the message.
        ingested_at: ISO8601 timestamp. None to let the database default apply.
    """

    source_id: int
    raw_text: str
    content_hash: str
    id: Optional[int] = None
    external_id: Optional[str] = None
    metadata: Optional[str] = None
    received_at: Optional[str] = None
    ingested_at: Optional[str] = None


@dataclass
class TradeSignal:
    """Mirrors the trade_signals table.

    Attributes:
        id: Primary key. None until the row is persisted.
        raw_message_id: FK to raw_messages.id. Required.
        trader_id: FK to traders.id. Required.
        symbol: Ticker symbol. Required.
        action: Free-text trade action (e.g. BTO/STC). Required.
        option_type: Free-text call/put, or None for non-option trades.
        price: Normalized decimal string (e.g. "3.25"), or None.
        expiration: ISO8601 date string, or None.
        position_size: Raw wording of position size, or None.
        created_at: ISO8601 timestamp. None to let the database default apply.
        updated_at: ISO8601 timestamp. None to let the database default apply.
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
