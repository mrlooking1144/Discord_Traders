# Current Version
v0.1.0

# Current Milestone
Milestone 2D.3 — Database Backup and Recovery
(not yet approved for implementation)

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

# Current Focus
Milestone 2D.2 — Operational Logging and Error Handling is fully implemented, tested, reviewed, committed (`a0654e51af3f82ce8826107dad15bcc0f2944b35`), and pushed to `origin/main`; local `main` and `origin/main` are synchronized at that commit. 287/287 tests passing. The current milestone is Milestone 2D.3 — Database Backup and Recovery, not yet approved for implementation. Phase 2E — Discord Ingestion remains future scope and is not yet scoped or approved for implementation. Architecture (`docs/DATABASE_DESIGN_V1.md`) and the database design remain frozen; no schema, repository, service, parser, or database-configuration changes occurred in Milestone 2D.2.

# Next Milestone
Milestone 2D.3 — Database Backup and Recovery. Not yet approved for implementation. Per project rules, implementation begins only after a separate implementation-plan review and approval step.

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
2D.3 — Database Backup and Recovery (not yet approved for implementation)

Git Synchronization:
Local main and origin/main synchronized at commit a0654e51af3f82ce8826107dad15bcc0f2944b35

Test Suite:
287/287 tests passing

Next Action:
Scope and approve the Milestone 2D.3 implementation plan, then implement, test, review, and commit per project rules
