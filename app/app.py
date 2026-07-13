"""Streamlit UI for Discord Traders.

Milestone 2C.2: manual raw-message entry. The user pastes raw trade message
text, the existing Milestone 2C.1 parser (app.parser.parse_message) is
invoked, and the structured result is displayed for review.

Milestone 2C.3: the reviewed parse result can be submitted to the database
through database.service.TradeService.ingest_message() - the only
persistence entry point this module calls. app/app.py never imports or
calls database.repository directly.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import streamlit as st

from app.parser import parse_message
from database.config import DatabaseConfig
from database.db import get_connection, initialize_database
from database.service import TradeService

_DB_PATH = "discord_traders.db"
_SOURCE_NAME = "manual"
_REFERENCE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

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
    signals = parse_message(raw_text)

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
        if not trader_name.strip():
            st.error("Trader display name is required.")
        elif not external_trader_id.strip():
            st.error("Stable external trader ID is required.")
        else:
            conn: sqlite3.Connection | None = None
            try:
                config = DatabaseConfig(db_path=_DB_PATH)
                initialize_database(config)
                conn = get_connection(config)

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
            except (ValueError, TypeError, sqlite3.Error):
                if conn is not None:
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        pass
                st.error("Could not save the message to the database.")
            else:
                st.success(
                    f"Saved {len(result['trade_signals'])} trade signal(s) "
                    "to the database."
                )
                for warning in result["duplicate_warnings"]:
                    if warning:
                        st.warning(warning)
            finally:
                if conn is not None:
                    conn.close()
