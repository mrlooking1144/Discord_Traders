"""Streamlit UI for Discord Traders.

Milestone 2C.2: manual raw-message entry. The user pastes raw trade message
text, the existing Milestone 2C.1 parser (app.parser.parse_message) is
invoked, and the structured result is displayed for review.

Milestone 2C.3: the reviewed parse result can be submitted to the database
through database.service.TradeService.ingest_message() - the only
persistence entry point this module calls. app/app.py never imports or
calls database.repository directly.

Milestone 2D.2: operational logging (app.logging_config) and a safe UI
error boundary. Console logging is configured at startup; file logging is
configured lazily at the start of the Submit workflow. Log messages are
fixed and generic, or counts-only - never raw message text, trader
identifiers, parsed signal values, database/log paths, or exception
message text (see app/logging_config.py for the sanitization contract).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

import streamlit as st

from app.logging_config import (
    configure_console_logging,
    configure_file_logging,
    log_operation_failure,
)
from app.parser import parse_message
from database.config import DatabaseConfig, resolve_database_path
from database.db import get_connection, initialize_database
from database.service import TradeService

_SOURCE_NAME = "manual"
_REFERENCE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

logger = logging.getLogger("discord_traders.app")

configure_console_logging()
logger.info("Discord Traders UI started")

st.title("Discord Traders - Manual Message Entry")

raw_text = st.text_area("Paste raw trade message text", height=200)

# Stale-preview protection: if the textarea no longer matches the text that
# produced the stored preview, drop the preview immediately so a later
# Submit click can never persist signals parsed from older text.
if (
    "parsed_raw_text" in st.session_state
    and st.session_state["parsed_raw_text"] != raw_text
):
    st.session_state.pop("parsed_signals", None)
    st.session_state.pop("parsed_raw_text", None)

if st.button("Parse Message"):
    try:
        signals = parse_message(raw_text)
    except Exception as exc:
        # An unexpected parser failure clears any previously stored valid
        # preview, distinct from the valid "nothing found" empty result.
        st.session_state.pop("parsed_signals", None)
        st.session_state.pop("parsed_raw_text", None)
        log_operation_failure(logger, "message parsing", exc)
        st.error("Could not parse this message.")
    else:
        if not signals:
            # A failed/empty parse clears any previously stored valid preview.
            st.session_state.pop("parsed_signals", None)
            st.session_state.pop("parsed_raw_text", None)
            st.warning("No trade signals found in this message.")
        else:
            st.session_state["parsed_signals"] = signals
            st.session_state["parsed_raw_text"] = raw_text
            st.success(f"Found {len(signals)} trade signal(s).")
            for signal in signals:
                st.json(signal)

if "parsed_signals" in st.session_state:
    trader_name = st.text_input("Trader display name")
    external_trader_id = st.text_input("Stable external trader ID")

    if st.button("Submit to Database"):
        configure_file_logging()
        if not trader_name.strip():
            st.error("Trader display name is required.")
        elif not external_trader_id.strip():
            st.error("Stable external trader ID is required.")
        else:
            conn: sqlite3.Connection | None = None
            try:
                config = DatabaseConfig(db_path=resolve_database_path())
                initialize_database(config)
                logger.info("Database initialization completed")
                conn = get_connection(config)
                logger.info("Database connection opened")

                service = TradeService(conn)
                reference_time = datetime.now(timezone.utc).strftime(
                    _REFERENCE_TIME_FORMAT
                )
                result = service.ingest_message(
                    source_name=_SOURCE_NAME,
                    trader_name=trader_name.strip(),
                    raw_text=st.session_state["parsed_raw_text"],
                    reference_time=reference_time,
                    external_trader_id=external_trader_id.strip(),
                    external_message_id=None,
                    metadata=None,
                    received_at=None,
                    trade_signals=st.session_state["parsed_signals"],
                )
                conn.commit()
                logger.info("Database transaction committed")
            except (ValueError, TypeError, sqlite3.Error, OSError) as exc:
                if conn is not None:
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        pass
                log_operation_failure(logger, "message submission", exc)
                st.error("Could not save the message to the database.")
            except Exception as exc:
                if conn is not None:
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        pass
                log_operation_failure(logger, "message submission", exc)
                st.error("Could not save the message to the database.")
            else:
                logger.info(
                    "Message ingestion completed with %d signal(s)",
                    len(result["trade_signals"]),
                )
                st.success(
                    f"Saved {len(result['trade_signals'])} trade signal(s) "
                    "to the database."
                )
                duplicate_count = sum(
                    1 for warning in result["duplicate_warnings"] if warning
                )
                if duplicate_count:
                    logger.warning(
                        "Duplicate advisory returned for %d signal(s)",
                        duplicate_count,
                    )
                for warning in result["duplicate_warnings"]:
                    if warning:
                        st.warning(warning)
            finally:
                if conn is not None:
                    conn.close()
