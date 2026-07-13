# Current Version
v0.1.0

# Current Milestone
Milestone 2C.4

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

# Current Focus
Milestone 2B is complete: schema (2B.1), config (2B.2), db connection layer (2B.3), models (2B.4), repository.py (2B.5a-e), service.py (2B.6a-c), and integration/transaction tests (2B.7) are all implemented, tested, reviewed, and committed. Milestone 2C.1 (Parser Module), 2C.2 (Manual Message Entry UI), and 2C.3 (TradeService Integration) are now also implemented, tested, reviewed, and committed. Next up is Milestone 2C.4 (Integration Tests), to be planned and approved separately.

# Next Milestone
Milestone 2C.4

# Known Issues
- Date/Time extraction not implemented
- Manual entry UI persistence (Milestone 2C.3) has been validated only against mocked service/database doubles; end-to-end coverage against a real SQLite database is deferred to Milestone 2C.4

# Documentation

Architecture:
FROZEN

Implementation Roadmap:
FROZEN

Current Phase:
Implementation

Current Milestone:
2C.3 (Complete) — Milestone 2C.4 is next

Next Action:
Plan and approve Milestone 2C.4 (Integration Tests)
