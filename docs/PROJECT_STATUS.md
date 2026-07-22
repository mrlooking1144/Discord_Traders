# Current Version
v0.1.0

# Current Milestone
Milestone 2D.5 — Correction and Audit Workflow
(implementation committed locally, not yet pushed; documentation pending review and commit)

# Completed
- Project structure
- VS Code environment
- GitHub repository
- Streamlit UI prototype
- Discord parser prototype
- Editable trade table
- Validation warning
- Milestone 2A: Version 1 database design (see docs/DATABASE_DESIGN_V1.md)
- Milestone 2B.1: database/schema.sql implemented and validated (see docs/HANDOFFS/2B.1_schema.md)
- Milestone 2B.2: database/config.py implemented and validated (see docs/HANDOFFS/2B.2_config.md)
- Milestone 2B.3: database/db.py implemented and validated (see docs/HANDOFFS/2B.3_db.md; commit ae3ebf07d0b2784c2fd849b7174be210e51bfc76)
- Milestone 2B.4: database/models.py implemented and validated (see docs/HANDOFFS/2B.4_models.md; commit 740e2556b6143d7e0fd983df5f325c516a7b506d)
- Milestone 2B.5a: database/repository.py sources access implemented and validated (see docs/HANDOFFS/2B.5a_sources_repository.md; commit 9afe549)
- Milestone 2B.5b: database/repository.py traders access implemented and validated (see docs/HANDOFFS/2B.5b_traders_repository.md; commit d03eae5)
  - `create_trader()`, `get_trader_by_external_id()`, `get_traders_by_name()`
  - 27/27 tests passing
- Milestone 2B.5c: database/repository.py raw_messages access implemented and validated (see docs/HANDOFFS/2B.5c_messages_repository.md; commit 9aa33e4)
  - `create_raw_message()`, `get_raw_message_by_external_id()`, `get_raw_messages_by_content_hash()`
  - 47/47 tests passing
- Milestone 2B.5d: database/repository.py trade_signals access implemented and validated (see docs/HANDOFFS/2B.5d_trades_repository.md; commit 04e72b6)
  - `create_trade_signal()`, `get_trade_signal_by_id()`, `update_trade_signal()`
  - 82/82 tests passing
- Milestone 2B.5e: database/repository.py trade_signal_edits access implemented and validated (see docs/HANDOFFS/2B.5e_trade_signal_edits_repository.md; commit df0b36f055be1f6199d53e2e98ceafba5634f554)
  - `create_trade_signal_edit()`, `get_trade_signal_edits()`
  - 100/100 tests passing
- Milestone 2B.6a: database/service.py TradeService scaffold + duplicate detection implemented and validated (see docs/HANDOFFS/2B.6a_trade_service_duplicate_detection.md; commit 799506bee7c3e320fb1d25044cbf2382637f5511)
  - `TradeService.check_duplicate_signal()`; repository addition: `get_trade_signals_matching()`
  - 133/133 tests passing
- Milestone 2B.6b: database/service.py edit-on-update rule implemented and validated (see docs/HANDOFFS/2B.6b_trade_service_edit_on_update.md; commit 1ae5dad52b7821ddf5fb92fcd327e2557969658e)
  - `TradeService.update_trade_signal()`; repository addition: `validate_trade_signal_update_fields()` shared validation helper
  - 141/141 tests passing
- Milestone 2B.6c: database/service.py ingest_message entry point implemented and validated (see docs/HANDOFFS/2B.6c_trade_service_ingest_message.md; commit bb75326c53e09851465747a8bac58c506822456f)
  - `TradeService.ingest_message()`; no repository changes
  - 158/158 tests passing
- Milestone 2B.7: Integration and transaction tests implemented and validated (see docs/HANDOFFS/2B.7_integration_transaction_tests.md; commit 44ffe33486ca7c71f27895a47fce547ba8b4fb80)
  - `tests/test_integration_transactions.py`; no production code changes
  - 163/163 tests passing
- Milestone 2C.1: app/parser.py implemented and validated (see docs/HANDOFFS/2C.1_parser_module.md; commit 3a2408ede8170155aced6d7b5a150f6a1804c62e)
  - `parse_message()`; source-independent, pure function, no database/UI/TradeService dependency
  - 191/191 tests passing
- Milestone 2C.2: app/app.py manual message entry UI implemented and validated (see docs/HANDOFFS/2C.2_manual_message_entry_ui.md; commit aa9917e3a102449697a76a68038324afa0320d19)
  - Manual raw-message text entry, invokes `app.parser.parse_message()`, displays structured result or "nothing found" message
  - `requirements.txt` added, pinning `streamlit==1.59.2`
  - No database, repository, TradeService, or persistence work included
  - 196/196 tests passing
- Milestone 2C.3: TradeService Integration — COMPLETE (see docs/HANDOFFS/2C.3_trade_service_integration.md; commit dfdc8b5ca98564ff398de177cec3a78a585bb380 — "Implement TradeService UI integration")
  - `app/app.py` wires the manual entry UI's parsed structured trade data to `TradeService.ingest_message()` via a fresh database connection created on each Submit click
  - Required "Trader display name" and "Stable external trader ID" UI fields; source name fixed to `"manual"`
  - Two-step Parse -> Review -> Submit workflow preserved, with `st.session_state`-based stale-preview protection
  - No repository, schema, service, or parser changes
  - 217/217 tests passing
- Milestone 2C.4: Integration Tests implemented and validated (see docs/HANDOFFS/2C.4_integration_tests.md; commit 6d0bfd2db5f4dbed4605676390716b9af81f0795 — "Add manual entry integration tests")
  - `tests/test_manual_entry_integration.py`; no production code changes
  - 8 new real-SQLite integration tests exercising the complete manual-entry path (Parse -> review -> Submit -> `TradeService` -> `repository.py` -> real temporary SQLite database), each against a unique temporary database with real schema initialization
  - Covers: full persisted-record and foreign-key verification across `sources`/`traders`/`raw_messages`/`trade_signals`; stable external trader identity reuse; advisory (non-blocking) duplicate warning with both signals persisted; malformed input creating no database rows; commit durability through a fresh connection; rollback removing all partial writes after a controlled mid-transaction failure; and protection against creating or modifying the repository-root `discord_traders.db`
  - 225/225 tests passing
  - Implementation commit `6d0bfd2` pushed to `origin/main`; documentation committed via commit `3be0c78` ("Update documentation for Milestone 2C.4"), also pushed to `origin/main` — Milestone 2C.4 is fully complete
- Phase 2D roadmap scoping approved: `docs/IMPLEMENTATION_ROADMAP.md` extended with Phase 2D — Manual Version 1 Operational Readiness (Milestones 2D.1 through 2D.7) and a Phase 2E — Discord Ingestion future-scope heading, plus an updated Implementation Order listing 2D.1-2D.7 (commit `1ed88d3628823ce231f76d9ad9e4f5a46b953a64`, pushed to `origin/main`; local `main` and `origin/main` synchronized at that commit)
  - Documentation only — no production code, test, schema, or architecture changes
  - 225/225 tests still passing
- Milestone 2D.1: Runtime Configuration and Application Data Locations implemented, tested, reviewed, committed, and pushed (see docs/HANDOFFS/2D.1_runtime_configuration_and_application_data_locations.md; commit 648ff121fc1f91a044f963f155fa0ea97e3211d6 — "Implement runtime database path configuration")
  - `database/config.py`: new `resolve_database_path()` — resolves a non-empty `DISCORD_TRADERS_DB_PATH` override (normalized via `Path(value).expanduser().resolve()`), else `%LOCALAPPDATA%\DiscordTraders\discord_traders.db`, else a `Path.home()\AppData\Local\DiscordTraders\discord_traders.db` fallback; `DatabaseConfig.db_path` remains required with no default
  - `database/db.py`: `initialize_database()` now creates required parent directories via `Path(config.db_path).parent.mkdir(parents=True, exist_ok=True)` immediately before opening its connection; directory-creation failures propagate normally
  - `app/app.py`: hardcoded `_DB_PATH` removed; `DatabaseConfig(db_path=resolve_database_path())` is the new call site; `TradeService.ingest_message()` remains the sole UI persistence entry point
  - No schema, model, parser, repository, or service redesign; no logging, backup, recovery, review UI, correction workflow, packaging, or Discord work included
  - 242/242 tests passing (225 pre-existing + 17 new across `tests/test_config.py`, `tests/test_db.py`, `tests/test_app.py`)
  - Implementation commit `648ff12` pushed to `origin/main`; local `main` and `origin/main` synchronized at that commit
- Milestone 2D.2: Operational Logging and Error Handling implemented, tested, reviewed, committed, and pushed (see docs/HANDOFFS/2D.2_operational_logging_and_error_handling.md; commit a0654e51af3f82ce8826107dad15bcc0f2944b35 — "Implement operational logging and error handling")
  - `app/logging_config.py` (new): standard-library-only logging, a `discord_traders` logger tree, independently idempotent `configure_console_logging()`/`configure_file_logging()` (each identified by its own private marker attribute), `resolve_log_path()` (fully independent of `database.config.resolve_database_path()`/`DISCORD_TRADERS_DB_PATH`), `sanitized_traceback()`, and `log_operation_failure()`
  - Console logging configured at application startup; file logging configured lazily at the start of the Submit workflow via a `RotatingFileHandler(maxBytes=1_000_000, backupCount=3, encoding="utf-8", mode="a", delay=True)`; file-logging failures never block submission and console logging remains available as fallback
  - `app/app.py`: broadened Submit exception boundary (`ValueError`, `TypeError`, `sqlite3.Error`, `OSError`, plus a final `except Exception` safety net), both logged at ERROR with sanitized diagnostics, no CRITICAL introduced; a new parser-failure guard distinguishes an unexpected exception (`"Could not parse this message."`) from the unchanged valid empty-result message (`"No trade signals found in this message."`); the unchanged `"Could not save the message to the database."` message covers all Submit failures; duplicate advisories remain a normal result, logged only at WARNING; rollback, unconditional connection cleanup, preserved review state, and `TradeService.ingest_message()` as the sole persistence entry point are all unchanged
  - Sanitized diagnostics exclude exception message text, absolute source paths, raw trade text, trader names, external trader IDs, parsed signal values, database/log paths, environment values, and SQL values
  - No schema, model, parser, repository, service, database-config, or database-initialization redesign; no backup, recovery, review UI, correction workflow, packaging, Discord, telemetry, or third-party dependency work included
  - 287/287 tests passing (242 pre-existing + 45 new across `tests/test_logging_config.py` and `tests/test_app.py`); tests use temporary log paths exclusively and do not write to the real LOCALAPPDATA log
  - Implementation commit `a0654e5` pushed to `origin/main`; local `main` and `origin/main` synchronized at that commit
- Milestone 2D.3: Database Backup and Recovery implemented, tested, reviewed, committed, and pushed (see docs/HANDOFFS/2D.3_database_backup_and_recovery.md; commit eb41fff5da5d80818b1b462685d4f5549eac1d58 — "Implement database backup and recovery")
  - `database/backup.py` (new): `create_backup()` using `sqlite3.Connection.backup()` for a consistent, standalone backup regardless of the source's rollback-journal or WAL journal mode; writes to a temporary file, verifies with `PRAGMA integrity_check`, then publishes via atomic rename; `resolve_backup_dir()` (override `DISCORD_TRADERS_BACKUP_PATH`, else `%LOCALAPPDATA%\DiscordTraders\backups`, else `Path.home()` fallback); UTC timestamped filenames (`discord_traders_YYYYMMDD_HHMMSS_ffffff.db`) with collision refusal; retention keeps the latest 10 verified backups (oldest deleted first, only after a successful verified backup, non-fatal deletion failures); `list_backups()`
  - `database/backup.py` restore (CLI-only, `python -m database.backup restore <path>`; never called from `app/app.py`): candidate validation (SQLite header, read-only open, integrity check, required-table/required-column schema compatibility) before any modification; a best-effort exclusive-access probe; a pre-restore safety backup (excluded from retention until post-restore validation succeeds); quarantine of any existing `-wal`/`-shm`/`-journal` sidecars, with a hard abort (production database and all sidecars left untouched) if a sidecar cannot be safely quarantined; atomic main-file replacement; post-restore integrity/schema validation with full recovery (original database and its quarantined sidecars restored) if validation fails
  - `app/app.py`: one new unconditional "Create Backup" button calling `database.backup.create_backup()`, with the same try/except/log/fixed-message pattern as the existing Submit workflow; no restore control; `TradeService.ingest_message()` remains the sole UI persistence entry point
  - No schema, repository, service, parser, review UI, correction workflow, packaging, or Discord redesign; `database/config.py`, `database/repository.py`, `database/service.py`, `database/schema.sql`, and `app/parser.py` are unchanged
  - 328/328 tests passing (287 pre-existing + 41 new across `tests/test_backup.py` (37) and `tests/test_app.py` (4), plus a 2-line assertion update in `tests/test_manual_entry_integration.py` for the always-rendered "Create Backup" button)
  - Implementation commit `eb41fff5da5d80818b1b462685d4f5549eac1d58` pushed to `origin/main`; documentation committed via commit `0c5257b244947b81be2f33294b9a0749cfae79a4` ("Update documentation for Milestone 2D.3"), also pushed to `origin/main` — Milestone 2D.3 is fully complete
- Milestone 2D.4: Stored-Signal Review UI implemented, tested, reviewed, committed, and pushed (see docs/HANDOFFS/2D.4_stored_signal_review_ui.md; commit 9aba2a2dce4e79384c52dd76db96600dd055d09c — "Implement stored-signal review UI")
  - `app/app.py`: a new `st.sidebar.radio("Workflow", ["Manual Message Entry", "Review Signals"])` control, with the entire script restructured into a plain `if`/`elif` on its value - not `st.tabs()`, since Streamlit executes every tab's body on every rerun regardless of visual selection - so only the selected workflow's code executes on a given rerun; the existing Parse/Submit/Create Backup logic moved unchanged in behavior into the `Manual Message Entry` branch
  - `app/app.py` `Review Signals` branch (new, strictly read-only): checks `Path(resolve_database_path()).exists()` before any database access - never calls `initialize_database()`, never opens a connection, never creates the directory or file, when the database does not yet exist - showing the fixed message `"No trade signals found."` in that case; otherwise opens a connection, delegates to `TradeService.list_trade_signals_for_review()` with source/trader/symbol text-input filters and an optional checkbox-gated date filter, and renders a read-only summary table (`st.dataframe`) plus a signal-ID `st.selectbox`-driven detail view (never depending on `st.dataframe`'s row-selection API) showing the raw message text only in the detail view; query/connection failures show the fixed message `"Could not load stored trade signals."`, logged via the existing sanitized `log_operation_failure()`
  - `database/repository.py` (additive): new `get_trade_signals_for_review()` joining `trade_signals`/`traders`/`raw_messages`/`sources`, `ORDER BY trade_signals.id DESC`, default `LIMIT 100`, with every active filter applied in SQL before `LIMIT` - exact `source_name`/`trader_name` match, case-insensitive exact `symbol` match, and a `trade_signals.created_at` date-range filter (inclusive day start, exclusive start of the following day); blank/`None` filters omit their `WHERE` fragment entirely; no existing repository function changed
  - `database/service.py` (additive): new `TradeService.list_trade_signals_for_review()`, a thin read-only delegation that only uppercases a non-blank `symbol` filter before calling the repository function and returns its result unchanged; no business rules, no writes; `TradeService` remains the application's sole persistence/service boundary and `app/app.py` still executes no raw SQL
  - Duplicate-warning status intentionally omitted from the review display - it is never persisted in the schema (only produced transiently at ingestion time), so it cannot be shown as stored data
  - No schema, parser, database-config, database-connection, backup, repository redesign, or service redesign; `database/schema.sql`, `database/config.py`, `database/db.py`, `database/backup.py`, and `app/parser.py` are all unchanged
  - 380/380 tests passing (328 pre-existing + 52 new: 21 in `tests/test_repository_sources.py`, 7 in `tests/test_service.py`, 20 in `tests/test_app.py` (16 covering navigation/empty/failure/display + 4 covering the date filter), and 4 real-SQLite integration tests in the new `tests/test_review_integration.py`)
  - Implementation commit `9aba2a2dce4e79384c52dd76db96600dd055d09c` pushed to `origin/main`; documentation committed via commit `ce1e6c527ae3e33e5bd3b915c8e4dc49ecc77328` ("Update documentation for Milestone 2D.4"), also pushed to `origin/main` — Milestone 2D.4 is fully complete
- Milestone 2D.5: Correction and Audit Workflow — implementation committed locally (see docs/HANDOFFS/2D.5_correction_and_audit_workflow.md; commit 644fb7a792e437c35d28e281fa9e5ffff9d1bee3 — "Implement correction and audit workflow"). **Implementation only; documentation for this milestone is not yet committed, and the implementation commit has not yet been pushed to `origin/main`. Milestone 2D.5 is not considered fully complete until the documentation commit is reviewed, approved, and committed.**
  - `database/service.py`: `TradeService.update_trade_signal()` extended with an optional keyword-only `expected_current_values: dict | None = None`, fully backward-compatible - when omitted, behavior is byte-for-byte the pre-existing Milestone 2B.6b sparse-update logic (every existing 2B.6b test passes unmodified); when supplied, the call is treated as a controlled correction requiring `changed_fields` and `expected_current_values` to each contain exactly the six approved fields (`symbol`, `action`, `option_type`, `price`, `expiration`, `position_size`), rejecting `raw_message_id`, `trader_id`, `id`, `created_at`, `updated_at`, and any missing/extra field
  - Controlled-correction check order: field-shape validation, then the shared repository field validation, then fetch the current signal (raising the new `TradeSignalNotFoundError`, a `ValueError` subclass, if missing - the legacy path's own missing-signal `ValueError` is unchanged), then a typed stale-value comparison (`Decimal`/`None`, never float, never a Decimal-vs-string mismatch) against a canonical current-value snapshot - raising the new `StaleTradeSignalError` on any mismatch - **before** a no-op comparison (raising a plain `ValueError` if the six proposed values are identical to the current ones) - both checks strictly **before** the existing full-row `trade_signal_edits` snapshot and the update are applied; no audit row is written and `updated_at` is not changed for any invalid, stale, missing, or no-op correction
  - `database/service.py` (additive): new `TradeService.list_trade_signal_audit_history()` - decodes and validates each `trade_signal_edits.previous_values` JSON value inside `TradeService` (raising the new `AuditHistoryError` for malformed or non-dict JSON), returning newest-first plain dicts with `id`, `edited_at`, and the six previous editable values (price preserved as its exact stored string); the raw JSON text itself is never returned or logged, and `app/app.py` never decodes audit JSON itself
  - `app/app.py`: a "Correct Signal" entry point inside the existing `Review Signals` branch, prefilling a correction form (symbol/price/expiration/position-size text inputs; action and option-type fixed `st.selectbox` controls - `BTO`/`STC`/`BTC`/`STO`/`BUY`/`SELL` and blank/`call`/`put` respectively) from the currently displayed values; symbol and action normalized to uppercase, option type to lowercase or `None`, price parsed as `Decimal`, expiration validated as `YYYY-MM-DD` - all client-side, before the correction service call (not before any connection is opened, since the branch already has one open for the review list/audit history at that point); a required confirmation checkbox gates Save; Save/Cancel and a read-only, newest-first "Correction History" section all share the one connection already opened for the review list within that rerun - no extra connection is opened and the database is never initialized here, preserving the Milestone 2D.4 no-implicit-creation guarantee exactly
  - Exact fixed messages: validation/no-op - `"Please enter a valid correction that changes at least one field."`; conflict (stale or not-found) - `"This trade signal changed or is no longer available. Reload it before correcting."`; persistence failure - `"Could not save the trade signal correction."`; success - `"Trade signal correction saved."`; audit-history failure - `"Could not load correction history."`
  - Session-state rules: entering correction mode captures the signal ID and canonical typed six-field snapshot; changing the selected signal, changing filters so the signal disappears, a stale/not-found conflict, or Cancel all clear correction state; a validation failure keeps the form open; a successful save clears correction state before the next rerun; the raw message stays `disabled=True`; no delete control exists anywhere in this workflow
  - Logging reuses the existing sanitized `log_operation_failure()` contract exactly - no raw message/field values, trader/source names, audit JSON, paths, or exception text in any log record or user-facing message; no `CRITICAL` logging introduced
  - No schema, repository, model, parser, database-config, or database-connection changes; `database/repository.py`, `database/schema.sql`, `database/models.py`, `database/config.py`, `database/db.py`, `database/backup.py`, `app/parser.py`, and `tests/test_repository_sources.py` are all unchanged
  - 433/433 tests passing (380 pre-existing + 53 new: 19 in `tests/test_service.py`, 22 in `tests/test_app.py`, and 12 real-SQLite integration tests in the new `tests/test_correction_integration.py`); all UI/integration tests isolate real file logging (patched or redirected) and the real `%LOCALAPPDATA%\DiscordTraders` log directory was confirmed absent both before and after the full suite run
  - Implementation commit `644fb7a792e437c35d28e281fa9e5ffff9d1bee3` committed locally; **not yet pushed to `origin/main`**; local `main` is one commit ahead of `origin/main`

# Current Focus
Milestone 2D.5 — Correction and Audit Workflow is implemented, tested, and reviewed, with its implementation committed locally as `644fb7a792e437c35d28e281fa9e5ffff9d1bee3` ("Implement correction and audit workflow"). This commit has **not** been pushed to `origin/main`; local `main` is one commit ahead of `origin/main` (`ce1e6c527ae3e33e5bd3b915c8e4dc49ecc77328`). Documentation for Milestone 2D.5 (this update and its handoff document) is currently uncommitted and awaiting ChatGPT review before it is committed and pushed. 433/433 tests passing. **Milestone 2D.6 must not begin until the Milestone 2D.5 documentation commit is reviewed, approved, committed, and pushed.** Phase 2E — Discord Ingestion remains future scope and is not yet scoped or approved for implementation. Architecture (`docs/DATABASE_DESIGN_V1.md`) and the database design remain frozen; no schema, repository, model, parser, or database-configuration redesign occurred in Milestone 2D.5.

# Next Milestone
Milestone 2D.6 — Windows Startup and Packaging. Must not begin until Milestone 2D.5's documentation commit is reviewed, approved, committed, and pushed, and a separate implementation-plan review and approval step for 2D.6 has taken place, per project rules.

# Known Issues
- Date/Time extraction not implemented

# Documentation

Architecture:
FROZEN

Database Design:
FROZEN (docs/DATABASE_DESIGN_V1.md unchanged)

Implementation Roadmap:
Extended through Milestone 2D.7 (Phase 2D approved; commit 1ed88d3628823ce231f76d9ad9e4f5a46b953a64); Phase 2E (Discord Ingestion) recorded as future scope only, not yet scoped or approved for implementation

Current Phase:
Phase 2D — Manual Version 1 Operational Readiness

Current Milestone:
2D.5 — Correction and Audit Workflow (implementation committed locally as 644fb7a792e437c35d28e281fa9e5ffff9d1bee3; documentation pending review and commit; not yet pushed)

Git Synchronization:
Local main is one commit ahead of origin/main. origin/main is at ce1e6c527ae3e33e5bd3b915c8e4dc49ecc77328 (last-pushed commit, the Milestone 2D.4 documentation commit). Local main additionally includes commit 644fb7a792e437c35d28e281fa9e5ffff9d1bee3 ("Implement correction and audit workflow", Milestone 2D.5 implementation), not yet pushed.

Test Suite:
433/433 tests passing

Next Action:
ChatGPT review of the Milestone 2D.5 documentation diff (this file and docs/HANDOFFS/2D.5_correction_and_audit_workflow.md). On approval, commit the documentation separately from the implementation commit, then push both to origin/main. Milestone 2D.6 — Windows Startup and Packaging must not begin until that review, commit, and push are complete.
