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

Milestone 2D.3: a minimal "Create Backup" control invoking
database.backup.create_backup(). Restore is CLI-only (see
database/backup.py) and is never exposed here.

Milestone 2D.4: a sidebar-selected "Review Signals" workflow, strictly
read-only, showing persisted trade signals via
database.service.TradeService.list_trade_signals_for_review(). Navigation
uses st.sidebar.radio() with a plain if/elif, not st.tabs(), because
Streamlit executes the body of every tab on every rerun regardless of
which tab is visually selected - only the selected workflow's code runs
here. The Review workflow never calls initialize_database() and never
opens a connection when the configured database file does not already
exist, so merely viewing the review screen can never create the database.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from app.logging_config import (
    configure_console_logging,
    configure_file_logging,
    log_operation_failure,
)
from app.parser import parse_message
from database.backup import create_backup
from database.config import DatabaseConfig, resolve_database_path
from database.db import get_connection, initialize_database
from database.service import TradeService

_SOURCE_NAME = "manual"
_REFERENCE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_NOT_FOUND_MESSAGE = "No trade signals found."
_LOAD_FAILURE_MESSAGE = "Could not load stored trade signals."

logger = logging.getLogger("discord_traders.app")

configure_console_logging()
logger.info("Discord Traders UI started")

workflow = st.sidebar.radio("Workflow", ["Manual Message Entry", "Review Signals"])

if workflow == "Manual Message Entry":
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

    if st.button("Create Backup"):
        configure_file_logging()
        try:
            create_backup(resolve_database_path())
        except Exception as exc:
            log_operation_failure(logger, "database backup", exc)
            st.error("Could not create a database backup.")
        else:
            logger.info("Database backup created")
            st.success("Database backup created successfully.")

elif workflow == "Review Signals":
    st.title("Discord Traders - Review Stored Signals")

    source_filter = st.text_input("Source (exact match)")
    trader_filter = st.text_input("Trader (exact match)")
    symbol_filter = st.text_input("Symbol")

    use_date_filter = st.checkbox("Filter by date")
    date_filter: str | None = None
    if use_date_filter:
        date_filter = st.date_input("Date").strftime("%Y-%m-%d")

    db_path = resolve_database_path()

    if not Path(db_path).exists():
        # Strictly read-only: never initialize_database(), never create the
        # parent directory or the database file, never open a connection,
        # when the production database does not already exist.
        st.info(_NOT_FOUND_MESSAGE)
    else:
        conn: sqlite3.Connection | None = None
        try:
            config = DatabaseConfig(db_path=db_path)
            conn = get_connection(config)
            service = TradeService(conn)
            signals = service.list_trade_signals_for_review(
                source_name=source_filter,
                trader_name=trader_filter,
                symbol=symbol_filter,
                date=date_filter,
            )
        except Exception as exc:
            log_operation_failure(logger, "stored-signal review", exc)
            st.error(_LOAD_FAILURE_MESSAGE)
        else:
            if not signals:
                st.info(_NOT_FOUND_MESSAGE)
            else:
                st.dataframe(
                    [
                        {
                            "ID": signal["id"],
                            "Source": signal["source_name"],
                            "Trader": signal["trader_name"],
                            "Symbol": signal["symbol"],
                            "Action": signal["action"],
                            "Option Type": signal["option_type"] or "",
                            "Price": signal["price"] or "",
                            "Expiration": signal["expiration"] or "",
                            "Created At": signal["created_at"],
                        }
                        for signal in signals
                    ]
                )

                signals_by_id = {signal["id"]: signal for signal in signals}
                selected_id = st.selectbox(
                    "Select a signal ID for details", list(signals_by_id)
                )
                selected = signals_by_id[selected_id]

                st.subheader(f"Signal {selected['id']} details")
                st.write(f"Source: {selected['source_name']}")
                st.write(f"Trader: {selected['trader_name']}")
                if selected["external_trader_id"]:
                    st.write(f"External trader ID: {selected['external_trader_id']}")
                st.write(f"Symbol: {selected['symbol']}")
                st.write(f"Action: {selected['action']}")
                st.write(f"Option type: {selected['option_type'] or '—'}")
                st.write(f"Price: {selected['price'] or '—'}")
                st.write(f"Expiration: {selected['expiration'] or '—'}")
                st.write(f"Position size: {selected['position_size'] or '—'}")
                st.write(f"Created at: {selected['created_at']}")
                st.write(f"Updated at: {selected['updated_at']}")
                st.text_area(
                    "Raw message", value=selected["raw_text"], height=200, disabled=True
                )
        finally:
            if conn is not None:
                conn.close()
