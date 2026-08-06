"""Streamlit UI for Discord Traders.

Milestone 2C.2: manual raw-message entry. The user pastes raw trade message
text, the existing Milestone 2C.1 parser (app.parser.parse_message) is
invoked, and the structured result is displayed for review.

Milestone 2C.3: the reviewed parse result can be submitted to the database
through database.service.TradeService.ingest_message() - the only
persistence entry point this module calls. app/streamlit_app.py never
imports or calls database.repository directly.

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

Milestone 2D.5: a "Correct Signal" control inside Review Signals, originally
routed through database.service.TradeService.update_trade_signal()'s
controlled-correction mode (expected_current_values), plus a read-only
"Correction History" section via TradeService.list_trade_signal_audit_history().
Only the six approved fields (symbol, action, option_type, price, expiration,
position_size) are ever editable; action and option type use fixed select
controls, not free text. Client-side syntactic validation (parsing
price/expiration, requiring the confirmation checkbox) happens before the
correction service call, not before any connection is opened, since one is
already open for the review list at that point. Both the correction form and
the Correction History section share the same connection already opened for
the review list/detail within this rerun - no extra connection is opened and
the database is never initialized here.

Recovery Milestone R6.5b: the shipped "Correct Signal" save path is migrated
from TradeService.update_trade_signal()'s controlled-correction mode to the
lifecycle-safe TradeService.correct_trade_signal() - the only correction
method this module calls. correct_trade_signal() owns its correction
transaction end to end (commit on success, rollback on any failure); this
module never calls conn.commit()/conn.rollback() on the correction-save path
itself (Manual Message Entry's own Submit-workflow commit/rollback is
unrelated and unchanged). TradeService.update_trade_signal() remains
available on TradeService for backward compatibility but is not called by
this shipped UI. The six editable fields, the fixed correction/conflict/
success/failure messages, and the read-only Correction History behavior are
all unchanged by this migration. Every rejected save (validation failure,
no-op, a lifecycle-unsafe action change, a stale-value or not-found conflict,
a lifecycle-integrity or snapshot failure, or any other unexpected failure)
leaves the correction form and its entered values in place; the form is
cleared only on a successful save, an explicit Cancel, a change of the
selected signal, or a filter change that removes the signal from view. A
persisted action or option_type value outside the standard fixed choices
(e.g. the Recovery extractor's BOUGHT/SOLD actions) is appended to that
field's selectbox options and selected by default, never silently replaced.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import streamlit as st

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
)
from app.dashboard_formatting import (
    LIFECYCLE_CSV_FIELDNAMES,
    SORT_DIRECTION_CHOICES,
    SORT_METRIC_CHOICES,
    SUMMARY_CSV_FIELDNAMES,
    build_lifecycle_csv_rows,
    build_lifecycle_detail,
    build_lifecycle_display_rows,
    build_summary_csv_rows,
    build_summary_display_rows,
    build_trader_label,
    filter_lifecycle_results,
    rank_trader_summaries,
    rows_to_csv_string,
)
from app.discord_adapter import segment_discord_batch
from app.logging_config import (
    configure_console_logging,
    configure_file_logging,
    log_operation_failure,
)
from app.parser import parse_message
from database.backup import create_backup
from database.config import DatabaseConfig, resolve_database_path
from database.db import get_connection, initialize_database
from database.service import (
    AuditHistoryError,
    ChannelExternalIdCollisionError,
    LifecycleIntegrityError,
    LifecycleSnapshotError,
    LifecycleUnsafeCorrectionError,
    StaleTradeSignalError,
    TradeService,
    TradeSignalNotFoundError,
)

_SOURCE_NAME = "manual"
_REFERENCE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_NOT_FOUND_MESSAGE = "No trade signals found."
_LOAD_FAILURE_MESSAGE = "Could not load stored trade signals."
_ACTION_CHOICES = ["BTO", "STC", "BTC", "STO", "BUY", "SELL"]
_OPTION_TYPE_CHOICES = ["", "call", "put"]
_CORRECTION_VALIDATION_MESSAGE = (
    "Please enter a valid correction that changes at least one field."
)
_CORRECTION_CONFLICT_MESSAGE = (
    "This trade signal changed or is no longer available. "
    "Reload it before correcting."
)
_CORRECTION_FAILURE_MESSAGE = "Could not save the trade signal correction."
_CORRECTION_SUCCESS_MESSAGE = "Trade signal correction saved."
_AUDIT_HISTORY_FAILURE_MESSAGE = "Could not load correction history."
_DASHBOARD_EMPTY_MESSAGE = "No trader performance data found."
_DASHBOARD_LOAD_FAILURE_MESSAGE = "Could not load trader performance data."
_DASHBOARD_DRILLDOWN_LOAD_FAILURE_MESSAGE = (
    "Could not load lifecycle details for this trader."
)
_DASHBOARD_NO_MATCHING_LIFECYCLES_MESSAGE = "No lifecycles match the current filters."
_DASHBOARD_STATUS_CHOICES = [
    "open", "partially_closed", "closed", "orphan", "unresolved", "invalid",
]
_DASHBOARD_OUTCOME_CHOICES = ["win", "loss", "breakeven", "not_scored", "data_error"]
# Recovery Milestone R8b: ranking, minimum-sample threshold, and CSV
# export defaults/messages.
_DASHBOARD_DEFAULT_MIN_ELIGIBLE_LIFECYCLES = 3
_DASHBOARD_CSV_EXPORT_FAILURE_MESSAGE = "Could not generate the CSV export."
_DASHBOARD_SUMMARY_CSV_FILENAME = "trader_performance_summary.csv"

# Recovery Milestone R9c: Bulk Channel Import. Fixed source name - never
# user-editable, exactly like Manual Message Entry's own _SOURCE_NAME.
_BULK_IMPORT_SOURCE_NAME = "discord"
_BULK_IMPORT_MIN_MESSAGES = 15
_BULK_IMPORT_TOO_FEW_MESSAGES_MESSAGE = (
    "This batch has fewer than 15 messages ({count} found). Bulk Channel "
    "Import requires at least 15. Use Manual Message Entry for smaller "
    "batches."
)
_BULK_IMPORT_PREVIEW_VALIDATION_MESSAGE = (
    "Please paste at least one message and select or specify a channel "
    "before previewing this batch."
)
_BULK_IMPORT_CONFIRM_VALIDATION_MESSAGE = (
    "Please check the confirmation box before importing."
)
_BULK_IMPORT_COLLISION_MESSAGE = (
    "That external channel ID is already in use for this source."
)
_BULK_IMPORT_VALIDATION_MESSAGE = "Please correct the batch details and try again."
_BULK_IMPORT_FAILURE_MESSAGE = "Could not complete the bulk channel import."
_BULK_IMPORT_SUCCESS_MESSAGE = "Bulk channel import completed successfully."
_BULK_IMPORT_DATABASE_ACCESS_FAILURE_MESSAGE = (
    "Could not access the database for Bulk Channel Import."
)
_BULK_IMPORT_CHANNEL_LIST_FAILURE_MESSAGE = "Could not load channels for this source."
_BULK_IMPORT_AVAILABILITY_LOAD_FAILURE_MESSAGE = (
    "Could not check external channel ID availability."
)
_BULK_IMPORT_EXTERNAL_ID_VALIDATION_MESSAGE = (
    "This external channel ID is not valid for Bulk Channel Import."
)
_BULK_IMPORT_PREDICTION_LOAD_FAILURE_MESSAGE = (
    "Could not load duplicate predictions for this batch."
)
_BULK_IMPORT_SEGMENTATION_FAILURE_MESSAGE = "Could not segment this Discord batch."
_BULK_IMPORT_REFRESH_FAILURE_MESSAGE = (
    "The import succeeded, but refreshed checkpoint details could not be loaded."
)
_BULK_IMPORT_NO_CHANNELS_MESSAGE = (
    "No channels exist for this source yet. Create a new channel below, "
    "or use Manual Message Entry."
)
_BULK_IMPORT_NEW_CHANNEL_NOTICE = (
    "This will create a new channel. No prior import history exists yet."
)

_BULK_IMPORT_STATE_KEYS = (
    "bulk_import_preview",
    "bulk_import_result",
    "bulk_import_raw_text_input",
    "bulk_import_channel_mode",
    "bulk_import_existing_channel_select",
    "bulk_import_new_channel_external_id",
    "bulk_import_new_channel_name",
    "bulk_import_reference_date",
    "bulk_import_timezone",
    "bulk_import_confirm_checkbox",
)


def _reset_bulk_import_state() -> None:
    """Clear every R9c-owned session_state key, including every editable
    widget's own key - safe because the editable widgets are never
    rendered while bulk_import_result is set (the completed view's hard
    gate), so there is no live widget instance to conflict with a
    mid-render key removal. Called only by "Start Next Batch"."""
    for key in _BULK_IMPORT_STATE_KEYS:
        st.session_state.pop(key, None)


def _bulk_import_input_snapshot(
    *,
    raw_text: str,
    channel_mode: str,
    existing_channel_id: int | None,
    new_channel_external_id: str,
    new_channel_name: str,
    reference_date,
    timezone_name: str,
) -> dict:
    """Build one normalized, comparable snapshot of every Preview-Batch-
    relevant input - used identically to freeze the preview, to detect
    staleness on every later rerun, and to build the exact confirm-call
    keyword arguments, so the three can never independently drift out of
    sync with one another.

    raw_text is used exactly as typed - never stripped, since a leading/
    trailing blank line could legitimately change segmentation.
    reference_date is normalized via .isoformat() - never compared as a
    live date object. new_channel_external_id/timezone_name are
    stripped. new_channel_name is stripped, then normalized to None when
    blank (matching this codebase's own existing convention for every
    other optional text input, e.g. Correct Signal's
    position_size normalization). Mode-irrelevant fields are forced to
    None so a value left over in the "other" mode's own fields can never
    contribute to a staleness comparison or leak into the confirm call.
    """
    is_existing = channel_mode == "existing"
    return {
        "raw_text": raw_text,
        "channel_mode": channel_mode,
        "existing_channel_id": existing_channel_id if is_existing else None,
        "new_channel_external_id": (
            None if is_existing else new_channel_external_id.strip()
        ),
        "new_channel_name": (
            None if is_existing else (new_channel_name.strip() or None)
        ),
        "reference_date": reference_date.isoformat(),
        "timezone": timezone_name.strip(),
    }


def _correction_selectbox_choices(base_choices: list[str], current_value: str) -> list[str]:
    """Build one correction-form selectbox's option list: a fresh copy of
    base_choices, with current_value appended when it is not already
    present - so a persisted value outside the standard set (e.g. an
    extractor-produced "BOUGHT"/"SOLD" action) is always selectable and
    always the default, never silently replaced by index 0. Never mutates
    base_choices, and never normalizes or substitutes current_value.
    """
    choices = list(base_choices)
    if current_value not in choices:
        choices.append(current_value)
    return choices


def _render_csv_download(*, label: str, rows: list, fieldnames: tuple, file_name: str, key: str) -> None:
    """Render one Recovery Milestone R8b CSV download button for
    already-built display/export rows, or a fixed sanitized failure
    message on any unexpected serialization error - never raw exception
    text. Mirrors the log_operation_failure() + fixed-message pattern
    already used everywhere else in this module."""
    try:
        csv_text = rows_to_csv_string(rows, fieldnames)
    except Exception as exc:
        log_operation_failure(logger, "trader performance CSV export", exc)
        st.error(_DASHBOARD_CSV_EXPORT_FAILURE_MESSAGE)
    else:
        st.download_button(
            label,
            data=csv_text,
            file_name=file_name,
            mime="text/csv",
            key=key,
        )


logger = logging.getLogger("discord_traders.app")

configure_console_logging()
logger.info("Discord Traders UI started")

workflow = st.sidebar.radio(
    "Workflow",
    [
        "Manual Message Entry", "Review Signals", "Trader Performance",
        "Bulk Channel Import",
    ],
)

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

                # Filter change removed the signal under correction from
                # view entirely - exit correction mode for it.
                if (
                    "correction_signal_id" in st.session_state
                    and st.session_state["correction_signal_id"] not in signals_by_id
                ):
                    st.session_state.pop("correction_signal_id", None)
                    st.session_state.pop("correction_expected_values", None)

                selected_id = st.selectbox(
                    "Select a signal ID for details", list(signals_by_id)
                )
                selected = signals_by_id[selected_id]

                # Selecting a different signal exits correction mode for
                # whatever signal was previously being corrected.
                if (
                    "correction_signal_id" in st.session_state
                    and st.session_state["correction_signal_id"] != selected_id
                ):
                    st.session_state.pop("correction_signal_id", None)
                    st.session_state.pop("correction_expected_values", None)

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

                # Two separate, freshly-re-read session_state checks (not a
                # single cached boolean) - mirrors the existing Parse ->
                # Submit pattern, so that clicking "Correct Signal" makes
                # the form appear within this same rerun rather than
                # requiring a second interaction.
                if st.session_state.get("correction_signal_id") != selected["id"]:
                    if st.button("Correct Signal"):
                        st.session_state["correction_signal_id"] = selected["id"]
                        st.session_state["correction_expected_values"] = {
                            "symbol": selected["symbol"],
                            "action": selected["action"],
                            "option_type": selected["option_type"],
                            "price": (
                                Decimal(selected["price"])
                                if selected["price"] is not None
                                else None
                            ),
                            "expiration": selected["expiration"],
                            "position_size": selected["position_size"],
                        }

                if st.session_state.get("correction_signal_id") == selected["id"]:
                    st.subheader("Correct Signal")
                    expected_values = st.session_state["correction_expected_values"]

                    symbol_input = st.text_input(
                        "Corrected symbol", value=expected_values["symbol"]
                    )
                    current_action = expected_values["action"]
                    action_choices = _correction_selectbox_choices(
                        _ACTION_CHOICES, current_action
                    )
                    action_input = st.selectbox(
                        "Corrected action",
                        action_choices,
                        index=action_choices.index(current_action),
                    )
                    current_option_type = expected_values["option_type"] or ""
                    option_type_choices = _correction_selectbox_choices(
                        _OPTION_TYPE_CHOICES, current_option_type
                    )
                    option_type_input = st.selectbox(
                        "Corrected option type",
                        option_type_choices,
                        index=option_type_choices.index(current_option_type),
                    )
                    price_input = st.text_input(
                        "Corrected price",
                        value=(
                            str(expected_values["price"])
                            if expected_values["price"] is not None
                            else ""
                        ),
                    )
                    expiration_input = st.text_input(
                        "Corrected expiration (YYYY-MM-DD)",
                        value=expected_values["expiration"] or "",
                    )
                    position_size_input = st.text_input(
                        "Corrected position size",
                        value=expected_values["position_size"] or "",
                    )
                    confirm_correction = st.checkbox("I confirm this correction")

                    save_clicked = st.button("Save Correction")
                    cancel_clicked = st.button("Cancel")

                    if cancel_clicked:
                        st.session_state.pop("correction_signal_id", None)
                        st.session_state.pop("correction_expected_values", None)

                    if save_clicked:
                        if not confirm_correction:
                            st.error(_CORRECTION_VALIDATION_MESSAGE)
                        else:
                            normalized_symbol = symbol_input.strip().upper()
                            normalized_option_type = (
                                option_type_input.strip().lower() or None
                            )
                            normalized_position_size = (
                                position_size_input.strip() or None
                            )

                            parse_failed = not normalized_symbol

                            parsed_price = None
                            price_text = price_input.strip()
                            if price_text:
                                try:
                                    parsed_price = Decimal(price_text)
                                except InvalidOperation:
                                    parse_failed = True

                            parsed_expiration = expiration_input.strip() or None
                            if parsed_expiration is not None:
                                try:
                                    datetime.strptime(parsed_expiration, "%Y-%m-%d")
                                except ValueError:
                                    parse_failed = True

                            if parse_failed:
                                st.error(_CORRECTION_VALIDATION_MESSAGE)
                            else:
                                changed_fields = {
                                    "symbol": normalized_symbol,
                                    "action": action_input,
                                    "option_type": normalized_option_type,
                                    "price": parsed_price,
                                    "expiration": parsed_expiration,
                                    "position_size": normalized_position_size,
                                }
                                configure_file_logging()
                                try:
                                    service.correct_trade_signal(
                                        selected["id"],
                                        expected_current_values=expected_values,
                                        **changed_fields,
                                    )
                                except (
                                    StaleTradeSignalError,
                                    TradeSignalNotFoundError,
                                ) as exc:
                                    log_operation_failure(
                                        logger, "trade signal correction", exc
                                    )
                                    st.error(_CORRECTION_CONFLICT_MESSAGE)
                                except LifecycleUnsafeCorrectionError as exc:
                                    log_operation_failure(
                                        logger, "trade signal correction", exc
                                    )
                                    st.error(_CORRECTION_VALIDATION_MESSAGE)
                                except ValueError as exc:
                                    log_operation_failure(
                                        logger, "trade signal correction", exc
                                    )
                                    st.error(_CORRECTION_VALIDATION_MESSAGE)
                                except (
                                    LifecycleIntegrityError,
                                    LifecycleSnapshotError,
                                    TypeError,
                                    sqlite3.Error,
                                    OSError,
                                    RuntimeError,
                                ) as exc:
                                    log_operation_failure(
                                        logger, "trade signal correction", exc
                                    )
                                    st.error(_CORRECTION_FAILURE_MESSAGE)
                                except Exception as exc:
                                    log_operation_failure(
                                        logger, "trade signal correction", exc
                                    )
                                    st.error(_CORRECTION_FAILURE_MESSAGE)
                                else:
                                    logger.info("Trade signal correction committed")
                                    st.session_state.pop("correction_signal_id", None)
                                    st.session_state.pop(
                                        "correction_expected_values", None
                                    )
                                    st.success(_CORRECTION_SUCCESS_MESSAGE)

                st.subheader("Correction History")
                try:
                    audit_history = service.list_trade_signal_audit_history(
                        selected["id"]
                    )
                except Exception as exc:
                    log_operation_failure(logger, "correction history", exc)
                    st.error(_AUDIT_HISTORY_FAILURE_MESSAGE)
                else:
                    if not audit_history:
                        st.write("No corrections have been made to this signal.")
                    else:
                        st.dataframe(
                            [
                                {
                                    "Audit ID": entry["id"],
                                    "Edited At": entry["edited_at"],
                                    "Previous Symbol": entry["symbol"],
                                    "Previous Action": entry["action"],
                                    "Previous Option Type": entry["option_type"] or "",
                                    "Previous Price": entry["price"] or "",
                                    "Previous Expiration": entry["expiration"] or "",
                                    "Previous Position Size": entry["position_size"]
                                    or "",
                                }
                                for entry in audit_history
                            ]
                        )
        finally:
            if conn is not None:
                conn.close()

elif workflow == "Trader Performance":
    st.title("Discord Traders - Trader Performance")

    db_path = resolve_database_path()

    if not Path(db_path).exists():
        # Strictly read-only, mirroring Review Signals: never
        # initialize_database(), never create the parent directory or
        # the database file, never open a connection, when the
        # production database does not already exist.
        st.info(_DASHBOARD_EMPTY_MESSAGE)
    else:
        conn: sqlite3.Connection | None = None
        try:
            config = DatabaseConfig(db_path=db_path)
            conn = get_connection(config)
            service = TradeService(conn)
            summaries = service.list_trader_performance_summaries()
        except Exception as exc:
            log_operation_failure(logger, "trader performance summary load", exc)
            st.error(_DASHBOARD_LOAD_FAILURE_MESSAGE)
        else:
            if not summaries:
                st.info(_DASHBOARD_EMPTY_MESSAGE)
            else:
                st.subheader("Trader Summary")

                # Recovery Milestone R8b: ranking controls. Rendered
                # before the summary table/trader selector, since their
                # current values determine the ranked order both are
                # built from - trader_ids below is derived from
                # ranked_summaries, never the raw service order.
                sort_metric = st.selectbox(
                    "Rank traders by",
                    SORT_METRIC_CHOICES,
                    key="dashboard_sort_metric",
                )
                sort_direction = st.selectbox(
                    "Sort direction",
                    SORT_DIRECTION_CHOICES,
                    key="dashboard_sort_direction",
                )
                min_eligible_lifecycles = int(
                    st.number_input(
                        "Minimum eligible lifecycles",
                        min_value=0,
                        value=_DASHBOARD_DEFAULT_MIN_ELIGIBLE_LIFECYCLES,
                        step=1,
                        key="dashboard_min_eligible",
                    )
                )

                ranked_summaries = rank_trader_summaries(
                    summaries,
                    sort_metric=sort_metric,
                    descending=(sort_direction == "Descending"),
                    min_eligible_lifecycles=min_eligible_lifecycles,
                )

                st.dataframe(
                    build_summary_display_rows(
                        ranked_summaries,
                        min_eligible_lifecycles=min_eligible_lifecycles,
                    )
                )

                _render_csv_download(
                    label="Download Trader Summary CSV",
                    rows=build_summary_csv_rows(
                        ranked_summaries,
                        min_eligible_lifecycles=min_eligible_lifecycles,
                    ),
                    fieldnames=SUMMARY_CSV_FIELDNAMES,
                    file_name=_DASHBOARD_SUMMARY_CSV_FILENAME,
                    key="dashboard_summary_csv_download",
                )

                trader_ids = [summary["trader_id"] for summary in ranked_summaries]
                trader_names_by_id = {
                    summary["trader_id"]: summary["trader_name"]
                    for summary in ranked_summaries
                }

                # A previously selected trader that no longer has a
                # current lifecycle (e.g. its last one was corrected
                # away on an earlier rerun) is cleared before the
                # selectbox renders, so Streamlit falls back to a valid
                # default instead of raising for a stale session_state
                # value - the same pattern already used for
                # correction_signal_id in the Review Signals workflow.
                if (
                    "dashboard_trader_select" in st.session_state
                    and st.session_state["dashboard_trader_select"] not in trader_ids
                ):
                    st.session_state.pop("dashboard_trader_select", None)

                selected_trader_id = st.selectbox(
                    "Select a trader to drill in",
                    trader_ids,
                    format_func=lambda trader_id: build_trader_label(
                        trader_id, trader_names_by_id[trader_id]
                    ),
                    key="dashboard_trader_select",
                )

                try:
                    lifecycle_results = service.list_current_trade_lifecycle_analytics(
                        trader_id=selected_trader_id
                    )
                except Exception as exc:
                    log_operation_failure(
                        logger, "trader lifecycle drill-down load", exc
                    )
                    st.error(_DASHBOARD_DRILLDOWN_LOAD_FAILURE_MESSAGE)
                else:
                    selected_trader_label = build_trader_label(
                        selected_trader_id, trader_names_by_id[selected_trader_id]
                    )
                    st.subheader(f"{selected_trader_label} - Lifecycle Detail")

                    status_filter = st.multiselect(
                        "Status filter", _DASHBOARD_STATUS_CHOICES
                    )
                    outcome_filter = st.multiselect(
                        "Outcome filter", _DASHBOARD_OUTCOME_CHOICES
                    )
                    symbol_filter = st.text_input("Symbol filter (exact match)")

                    filtered_results = filter_lifecycle_results(
                        lifecycle_results,
                        statuses=status_filter,
                        outcomes=outcome_filter,
                        symbol=symbol_filter,
                    )

                    if not filtered_results:
                        st.info(_DASHBOARD_NO_MATCHING_LIFECYCLES_MESSAGE)
                    else:
                        st.dataframe(build_lifecycle_display_rows(filtered_results))

                        _render_csv_download(
                            label="Download Lifecycle Drill-down CSV",
                            rows=build_lifecycle_csv_rows(filtered_results),
                            fieldnames=LIFECYCLE_CSV_FIELDNAMES,
                            file_name=(
                                f"trader_lifecycle_drilldown_{selected_trader_id}.csv"
                            ),
                            key="dashboard_drilldown_csv_download",
                        )

                        lifecycle_ids = [
                            result["trade_lifecycle_id"] for result in filtered_results
                        ]
                        results_by_id = {
                            result["trade_lifecycle_id"]: result
                            for result in filtered_results
                        }

                        # A previously selected lifecycle that no longer
                        # appears under the current trader/filters (a
                        # trader change, or a filter narrowing it out)
                        # is cleared the same way as the trader
                        # selection above - one inclusion check handles
                        # both causes without distinguishing them.
                        if (
                            "dashboard_lifecycle_select" in st.session_state
                            and st.session_state["dashboard_lifecycle_select"]
                            not in lifecycle_ids
                        ):
                            st.session_state.pop("dashboard_lifecycle_select", None)

                        selected_lifecycle_id = st.selectbox(
                            "Select a lifecycle ID for detail",
                            lifecycle_ids,
                            key="dashboard_lifecycle_select",
                        )
                        selected_result = results_by_id[selected_lifecycle_id]
                        detail = build_lifecycle_detail(selected_result)

                        st.subheader(f"Lifecycle {selected_lifecycle_id} Detail")
                        st.write(f"Data Error Detail: {detail['error_detail']}")

                        if not detail["exit_leg_rows"]:
                            st.write("No exit events for this lifecycle.")
                        else:
                            st.dataframe(detail["exit_leg_rows"])
        finally:
            if conn is not None:
                conn.close()

elif workflow == "Bulk Channel Import":
    st.title("Discord Traders - Bulk Channel Import")

    if st.session_state.get("bulk_import_result") is not None:
        # ----------------------------------------------------------------
        # COMPLETED VIEW - a hard gate: the editable form and Preview
        # Batch are never rendered here. The exact frozen paste and
        # preview from bulk_import_preview are shown verbatim - never
        # re-segmented, never re-predicted. Only "Start Next Batch"
        # resets this state.
        # ----------------------------------------------------------------
        result = st.session_state["bulk_import_result"]
        preview = st.session_state.get("bulk_import_preview") or {}

        st.success(_BULK_IMPORT_SUCCESS_MESSAGE)

        st.subheader("Pasted Text (This Batch)")
        st.text_area(
            "Pasted text (this batch)",
            value=preview.get("raw_text", ""),
            height=200,
            disabled=True,
            label_visibility="collapsed",
        )

        st.subheader("Preview (As Imported)")
        preview_rows = build_preview_rows(
            preview.get("segmented", []), preview.get("predictions", [])
        )
        st.dataframe(preview_rows)

        st.subheader("Last operation counts")
        st.write(build_last_operation_counts(result.operation))

        st.subheader("Lifecycle Rebuild Result")
        st.write(build_lifecycle_rebuild_summary(result.lifecycle_result))

        st.subheader("Updated Channel Checkpoint")
        conn: sqlite3.Connection | None = None
        try:
            config = DatabaseConfig(db_path=resolve_database_path())
            conn = get_connection(config)
            service = TradeService(conn)
            updated_summary = service.get_bulk_import_channel_summary(
                source_name=_BULK_IMPORT_SOURCE_NAME, channel_id=result.channel.id,
            )
        except Exception as exc:
            log_operation_failure(logger, "bulk import updated summary readback", exc)
            st.warning(_BULK_IMPORT_REFRESH_FAILURE_MESSAGE)
        else:
            if updated_summary is not None:
                # Post-success: the checkpoint conversion source is this
                # new operation's own recorded timezone - never the
                # pre-import form/preview timezone, and never any older
                # latest_operation.timezone that might predate it.
                panel = build_resume_panel(
                    updated_summary, display_timezone=result.operation.timezone,
                )
                st.write(panel)
            else:
                # A None summary after a successful, committed import
                # means the refreshed checkpoint could not be resolved -
                # the same fixed warning as an outright readback
                # exception; the successful result above remains fully
                # visible either way.
                st.warning(_BULK_IMPORT_REFRESH_FAILURE_MESSAGE)
        finally:
            if conn is not None:
                conn.close()

        if st.button("Start Next Batch", key="bulk_import_start_next_batch_button"):
            _reset_bulk_import_state()
            st.rerun()

    else:
        # ----------------------------------------------------------------
        # EDITING / PREVIEWED - the editable form. Every widget below uses
        # a stable, fixed key.
        # ----------------------------------------------------------------
        st.subheader("Paste Batch")
        raw_text = st.text_area(
            "Paste Discord channel history (at least 15 messages)",
            height=250, key="bulk_import_raw_text_input",
        )

        st.subheader("Channel")
        channel_mode_choice = st.radio(
            "Channel", ["Use an existing channel", "Create a new channel"],
            key="bulk_import_channel_mode",
        )
        channel_mode = (
            "existing" if channel_mode_choice == "Use an existing channel" else "create"
        )

        st.subheader("Batch Details")
        reference_date_value = st.date_input(
            "Reference date", key="bulk_import_reference_date"
        )
        timezone_input = st.text_input(
            "Timezone (IANA name, e.g. America/New_York)", key="bulk_import_timezone"
        )

        existing_channel_id: int | None = None
        new_channel_external_id_input = ""
        new_channel_name_input = ""
        new_channel_external_id_valid = True

        conn = None
        service = None
        try:
            config = DatabaseConfig(db_path=resolve_database_path())
            initialize_database(config)
            conn = get_connection(config)
            service = TradeService(conn)
        except Exception as exc:
            log_operation_failure(logger, "bulk channel import database access", exc)
            st.error(_BULK_IMPORT_DATABASE_ACCESS_FAILURE_MESSAGE)

        # Every editing/advisory/Preview Batch operation that uses conn
        # is inside this try/finally, so conn is always closed even if
        # channel-list loading, channel-label formatting, segmentation,
        # or duplicate prediction raises unexpectedly - never left open
        # on an uncaught error. The connection is deliberately not held
        # open across the later, pure preview rendering or the Confirm
        # transaction (which opens its own connection).
        try:
            if service is not None:
                if channel_mode == "existing":
                    # Correction 1: channel existence is unknown when
                    # loading fails - a separate loaded flag keeps the
                    # (factually correct) "no channels exist yet"
                    # message from ever appearing after a failed load,
                    # and keeps the selectbox itself from rendering at
                    # all when loading failed.
                    channel_list_loaded = False
                    channel_summaries = []
                    try:
                        channel_summaries = service.list_bulk_import_channels(
                            source_name=_BULK_IMPORT_SOURCE_NAME
                        )
                    except Exception as exc:
                        log_operation_failure(logger, "bulk import channel list", exc)
                        st.error(_BULK_IMPORT_CHANNEL_LIST_FAILURE_MESSAGE)
                    else:
                        channel_list_loaded = True

                    summaries_by_id = {s.channel.id: s for s in channel_summaries}
                    options = list(summaries_by_id)

                    if channel_list_loaded and not options:
                        st.info(_BULK_IMPORT_NO_CHANNELS_MESSAGE)

                    if channel_list_loaded:
                        # Stale-selection guard: a previously selected
                        # channel that no longer appears is cleared
                        # before the selectbox renders - the same
                        # pattern already used for
                        # dashboard_trader_select/correction_signal_id
                        # elsewhere in this file.
                        if (
                            "bulk_import_existing_channel_select" in st.session_state
                            and st.session_state["bulk_import_existing_channel_select"]
                            not in options
                        ):
                            st.session_state.pop(
                                "bulk_import_existing_channel_select", None
                            )

                        existing_channel_id = st.selectbox(
                            "Select a channel", options, index=None,
                            placeholder="Select a channel...",
                            format_func=lambda cid: format_channel_option_label(
                                summaries_by_id[cid].channel
                            ),
                            key="bulk_import_existing_channel_select",
                        )

                        if existing_channel_id is not None:
                            selected_summary = summaries_by_id[existing_channel_id]
                            # Pre-confirm resume-panel display source: the
                            # current LIVE normalized batch timezone from
                            # the form - never latest_operation.timezone.
                            live_timezone = (
                                st.session_state.get("bulk_import_timezone") or ""
                            ).strip()
                            panel = build_resume_panel(
                                selected_summary, display_timezone=live_timezone or None,
                            )
                            st.write(panel)
                            if selected_summary.checkpoint is not None:
                                st.info(build_resume_guidance_message())
                else:
                    new_channel_external_id_input = st.text_input(
                        "New channel external ID (stable, required)",
                        key="bulk_import_new_channel_external_id",
                    )
                    new_channel_name_input = st.text_input(
                        "New channel display name (optional)",
                        key="bulk_import_new_channel_name",
                    )
                    st.info(_BULK_IMPORT_NEW_CHANNEL_NOTICE)

                    if new_channel_external_id_input.strip():
                        try:
                            availability = service.check_new_channel_external_id_availability(
                                source_name=_BULK_IMPORT_SOURCE_NAME,
                                external_channel_id=new_channel_external_id_input.strip(),
                            )
                        except ValueError as exc:
                            # Correction 5: an invalid candidate (blank,
                            # or the reserved __unspecified__ sentinel)
                            # is never silently swallowed - the existing
                            # R9a method's own ValueError is the
                            # validation result; no parallel sentinel
                            # string comparison is implemented here.
                            log_operation_failure(
                                logger, "bulk import external id validation", exc
                            )
                            st.error(_BULK_IMPORT_EXTERNAL_ID_VALIDATION_MESSAGE)
                            new_channel_external_id_valid = False
                        except Exception as exc:
                            log_operation_failure(
                                logger, "bulk import availability check", exc
                            )
                            st.error(_BULK_IMPORT_AVAILABILITY_LOAD_FAILURE_MESSAGE)
                        else:
                            message = format_availability_message(availability)
                            if availability.is_available:
                                st.success(message)
                            else:
                                # A strong warning - never blocks Preview
                                # Batch or Confirm; the authoritative
                                # collision rejection remains entirely
                                # R9b's own job at Confirm time.
                                st.error(message)

            preview_clicked = st.button(
                "Preview Batch", key="bulk_import_preview_button",
                disabled=(service is None),
            )

            if preview_clicked and service is not None:
                # A stale approval can never survive any new Preview
                # Batch attempt, success or failure.
                st.session_state.pop("bulk_import_confirm_checkbox", None)

                channel_ready = (
                    (channel_mode == "existing" and existing_channel_id is not None)
                    or (
                        channel_mode == "create"
                        and new_channel_external_id_input.strip()
                        and new_channel_external_id_valid
                    )
                )
                if not raw_text.strip() or not channel_ready:
                    st.error(_BULK_IMPORT_PREVIEW_VALIDATION_MESSAGE)
                else:
                    # Correction 3: segmentation is never called without
                    # an exception boundary - a failure here shows only
                    # the fixed, sanitized message, never creates or
                    # replaces bulk_import_preview (leaving any still-
                    # valid prior preview exactly as it was), and never
                    # reaches duplicate prediction or import.
                    segmented = None
                    try:
                        segmented = segment_discord_batch(raw_text)
                    except Exception as exc:
                        log_operation_failure(
                            logger, "bulk import batch segmentation", exc
                        )
                        st.error(_BULK_IMPORT_SEGMENTATION_FAILURE_MESSAGE)

                    if segmented is not None:
                        if len(segmented) < _BULK_IMPORT_MIN_MESSAGES:
                            st.error(
                                _BULK_IMPORT_TOO_FEW_MESSAGES_MESSAGE.format(
                                    count=len(segmented)
                                )
                            )
                        else:
                            predictions = None
                            if channel_mode == "existing":
                                try:
                                    predictions = service.predict_channel_import_duplicate_statuses(
                                        channel_id=existing_channel_id,
                                        segmented_messages=segmented,
                                    )
                                except Exception as exc:
                                    log_operation_failure(
                                        logger, "bulk import duplicate prediction", exc
                                    )
                                    st.error(_BULK_IMPORT_PREDICTION_LOAD_FAILURE_MESSAGE)
                            else:
                                # Create mode: no channel_id exists yet to
                                # check against - never fabricated
                                # prediction objects.
                                predictions = []

                            if predictions is not None:
                                snapshot = _bulk_import_input_snapshot(
                                    raw_text=raw_text, channel_mode=channel_mode,
                                    existing_channel_id=existing_channel_id,
                                    new_channel_external_id=new_channel_external_id_input,
                                    new_channel_name=new_channel_name_input,
                                    reference_date=reference_date_value,
                                    timezone_name=timezone_input,
                                )
                                st.session_state["bulk_import_preview"] = {
                                    **snapshot,
                                    "segmented": segmented,
                                    "predictions": predictions,
                                }
        finally:
            if conn is not None:
                conn.close()

        # ----------------------------------------------------------------
        # PREVIEW SECTION - rendered only when a preview exists and is not
        # stale relative to the current live form inputs.
        # ----------------------------------------------------------------
        preview = st.session_state.get("bulk_import_preview")
        if preview is not None:
            live_snapshot = _bulk_import_input_snapshot(
                raw_text=raw_text, channel_mode=channel_mode,
                existing_channel_id=existing_channel_id,
                new_channel_external_id=new_channel_external_id_input,
                new_channel_name=new_channel_name_input,
                reference_date=reference_date_value, timezone_name=timezone_input,
            )
            frozen_snapshot = {key: preview[key] for key in live_snapshot}

            if live_snapshot != frozen_snapshot:
                # Staleness (including a channel-mode switch, since
                # channel_mode is itself one of the snapshot fields) -
                # drop the preview and any lingering approval.
                st.session_state.pop("bulk_import_preview", None)
                st.session_state.pop("bulk_import_confirm_checkbox", None)
            else:
                st.subheader("Preview")
                st.write(f"{len(preview['segmented'])} messages segmented.")

                preview_rows = build_preview_rows(
                    preview["segmented"], preview["predictions"]
                )
                st.dataframe(preview_rows)

                if preview["channel_mode"] == "existing":
                    counts = count_new_vs_duplicate(
                        preview["predictions"], len(preview["segmented"])
                    )
                    st.write(
                        f"New: {counts['new']} | "
                        f"Predicted duplicate: {counts['predicted_duplicate']}"
                    )
                    for warning in build_content_difference_warnings(
                        preview["segmented"], preview["predictions"]
                    ):
                        st.warning(warning)
                else:
                    notice = build_create_mode_prediction_notice()
                    st.info(notice["notice"])
                    st.write(f"{notice['new_label']}: {len(preview['segmented'])}")
                    st.write(f"{notice['duplicate_label']}: {notice['duplicate_value']}")

                st.checkbox(
                    "I have reviewed this preview and want to import it",
                    key="bulk_import_confirm_checkbox",
                )
                confirm_clicked = st.button(
                    "Confirm Import", key="bulk_import_confirm_button"
                )

                if confirm_clicked:
                    if not st.session_state.get("bulk_import_confirm_checkbox"):
                        st.error(_BULK_IMPORT_CONFIRM_VALIDATION_MESSAGE)
                    else:
                        configure_file_logging()
                        confirm_conn: sqlite3.Connection | None = None
                        try:
                            confirm_config = DatabaseConfig(
                                db_path=resolve_database_path()
                            )
                            initialize_database(confirm_config)
                            confirm_conn = get_connection(confirm_config)
                            confirm_service = TradeService(confirm_conn)
                            # Every argument comes from the frozen preview
                            # snapshot - never a live widget read here.
                            # import_channel_batch_with_lifecycle_rebuild()
                            # owns its entire transaction end to end; this
                            # branch never calls conn.commit()/
                            # conn.rollback() around it.
                            result = confirm_service.import_channel_batch_with_lifecycle_rebuild(
                                source_name=_BULK_IMPORT_SOURCE_NAME,
                                channel_mode=preview["channel_mode"],
                                existing_channel_id=preview["existing_channel_id"],
                                new_channel_external_id=preview["new_channel_external_id"],
                                new_channel_name=preview["new_channel_name"],
                                reference_date=preview["reference_date"],
                                timezone=preview["timezone"],
                                raw_batch_text=preview["raw_text"],
                                segmented_messages=preview["segmented"],
                            )
                        except ChannelExternalIdCollisionError as exc:
                            log_operation_failure(logger, "bulk channel import", exc)
                            st.error(_BULK_IMPORT_COLLISION_MESSAGE)
                        except ValueError as exc:
                            log_operation_failure(logger, "bulk channel import", exc)
                            st.error(_BULK_IMPORT_VALIDATION_MESSAGE)
                        except (
                            LifecycleIntegrityError,
                            LifecycleSnapshotError,
                            RuntimeError,
                            sqlite3.Error,
                            OSError,
                        ) as exc:
                            log_operation_failure(logger, "bulk channel import", exc)
                            st.error(_BULK_IMPORT_FAILURE_MESSAGE)
                        except Exception as exc:
                            log_operation_failure(logger, "bulk channel import", exc)
                            st.error(_BULK_IMPORT_FAILURE_MESSAGE)
                        else:
                            st.session_state["bulk_import_result"] = result
                            # bulk_import_confirm_checkbox is deliberately
                            # NOT popped here: it was already rendered
                            # earlier in this same script pass, and
                            # popping a widget's own key for a widget
                            # already instantiated in the current run is
                            # unsafe (it can leave Streamlit's own
                            # widget-state bookkeeping inconsistent). This
                            # is safe to skip: the checkbox is never
                            # rendered again until "Start Next Batch"
                            # resets the whole workflow (which itself
                            # clears this same key, in a later, separate
                            # script run - see _reset_bulk_import_state()),
                            # so its stale True value can never reach the
                            # user again regardless.
                            st.rerun()
                        finally:
                            if confirm_conn is not None:
                                confirm_conn.close()
