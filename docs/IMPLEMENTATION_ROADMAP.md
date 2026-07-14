# Implementation Roadmap — Version 1

Status: **Active** — this document is the implementation contract for Version 1,
starting with Milestone 2B.

## 1. Architecture Status

- The architecture (`docs/DATABASE_DESIGN_V1.md`) is **frozen**.
- Future architectural changes require a **demonstrated implementation issue**
  (something concrete encountered while building or testing V1), not a hypothetical
  concern.
- Feature requests alone do **not** justify redesign. A feature request is
  evaluated for a V2/later milestone; it does not reopen the frozen schema or
  layering decisions.

## 2. Development Rules

Every iteration of work follows this loop, with no steps skipped or reordered:

```
Architecture
    ↓
Implement ONE milestone only
    ↓
Run tests
    ↓
Wait for review
    ↓
Git commit
    ↓
Next milestone
```

- Never implement multiple milestones in one iteration.
- Never start the next milestone before the current one is reviewed and committed.
- If a milestone turns out to be larger than expected once work begins, stop and
  split it rather than absorbing the extra scope silently.

## 3. Milestone Roadmap

Milestone 2B ("database implementation") is broken into the smallest practical
steps below, organized around the approved module architecture — a single
`repository.py` data-access module and a single `service.py` module exposing
`TradeService` — rather than one file per table.

### Milestone 2B.1 — schema.sql

- **Objective**: Create the SQLite schema (all 5 tables, indexes, and
  constraints) exactly as specified in `docs/DATABASE_DESIGN_V1.md`, with
  nothing added or reinterpreted.
- **Files affected**: `database/schema.sql` (new).
- **Expected deliverables**: A single SQL file containing `CREATE TABLE` /
  `CREATE INDEX` statements for `sources`, `traders`, `raw_messages`,
  `trade_signals`, `trade_signal_edits`.
- **Tests**: A test that applies the schema to a fresh database and asserts all
  5 tables and their indexes/constraints exist as designed.
- **Acceptance criteria**: Schema matches `DATABASE_DESIGN_V1.md` column-for-
  column; applying it twice does not error or duplicate schema.

### Milestone 2B.2 — config.py

- **Objective**: Introduce a minimal config module for the database file path
  (and any other V1-required settings), avoiding hardcoded paths scattered
  across modules.
- **Files affected**: `database/config.py` (new).
- **Expected deliverables**: A single source of truth for the DB path,
  overridable for tests (e.g. in-memory or temp-file DB).
- **Tests**: A test confirming the config value can be read and overridden.
- **Acceptance criteria**: No other module hardcodes a DB path.

### Milestone 2B.3 — db.py

- **Objective**: Provide the connection layer — opening a SQLite connection
  against the path from `config.py` and applying `schema.sql` if not already
  present (idempotent init).
- **Files affected**: `database/db.py` (new).
- **Expected deliverables**: A function to obtain a connection and ensure the
  schema is applied.
- **Tests**: A test that initializes a fresh database twice without error or
  duplication, using the overridable config from 2B.2.
- **Acceptance criteria**: All database access in later milestones goes through
  this module — no ad hoc `sqlite3.connect` calls elsewhere.

### Milestone 2B.4 — models.py

- **Objective**: Define plain Python data structures (e.g. dataclasses)
  mirroring the 5 tables, with type hints, used as the boundary between the
  database layer and the rest of the app.
- **Files affected**: `database/models.py` (new).
- **Expected deliverables**: `Source`, `Trader`, `RawMessage`, `TradeSignal`,
  `TradeSignalEdit` dataclasses matching schema fields and nullability.
- **Tests**: Construction tests confirming required vs. optional fields match
  the schema's NOT NULL constraints.
- **Acceptance criteria**: Models contain no database or business logic — data
  shape only.

### Milestone 2B.5 — repository.py (implemented incrementally)

A single repository module, built up in small reviewable slices rather than as
one large file. Each slice is its own milestone, its own commit.

- **2B.5a — sources access**
  - **Objective**: Insert-if-not-exists and lookup-by-name functions for
    `sources` within `repository.py`.
  - **Files affected**: `database/repository.py` (new).
  - **Tests**: Insert, duplicate-name rejection (UNIQUE), lookup.
  - **Acceptance criteria**: No direct SQL for `sources` outside this module.

- **2B.5b — traders access**
  - **Objective**: Insert and lookup functions for `traders`, including the
    partial-unique identity constraint on `(source_id, external_trader_id)`.
  - **Files affected**: `database/repository.py` (extend).
  - **Tests**: Insert with/without `external_trader_id`, duplicate external-ID
    rejection within the same source, same external ID allowed across two
    sources.
  - **Acceptance criteria**: Matches Sections 3/7 of `DATABASE_DESIGN_V1.md`.

- **2B.5c — raw_messages access**
  - **Objective**: Insert-only function for `raw_messages`, including
    `content_hash` computation and duplicate lookup by `(source_id,
    external_id)` and by `content_hash` fallback.
  - **Files affected**: `database/repository.py` (extend).
  - **Tests**: Insert, duplicate `external_id` rejection, hash-based duplicate
    lookup, no update function exists (immutability).
  - **Acceptance criteria**: `raw_text` is never updated by any code path.

- **2B.5d — trade_signals access**
  - **Objective**: Insert and update functions for `trade_signals`, enforcing
    required fields (`raw_message_id`, `trader_id`, `symbol`, `action`) and
    bumping `updated_at` on update.
  - **Files affected**: `database/repository.py` (extend).
  - **Tests**: Insert with required-only fields, insert with full fields,
    update changes `updated_at`, missing-required-field rejection.
  - **Acceptance criteria**: `price` is only ever handled as `Decimal` in
    application code, never binary float, per design doc Section 3.

- **2B.5e — trade_signal_edits access**
  - **Objective**: Insert function writing a full-row JSON snapshot into
    `trade_signal_edits`, and a lookup function for a signal's edit history.
  - **Files affected**: `database/repository.py` (extend).
  - **Tests**: Insert produces correct pre-edit snapshot; history is ordered.
  - **Acceptance criteria**: This module only records edits — it does not
    decide when an edit occurs (that belongs to `TradeService` in 2B.6).

### Milestone 2B.6 — service.py (implemented incrementally)

A single service module exposing `TradeService`, built up in small reviewable
slices. This is the only layer that orchestrates multiple repository calls and
contains business rules (duplicate detection, edit-on-update, ingestion order).

- **2B.6a — TradeService scaffold + duplicate detection**
  - **Objective**: Create `TradeService` and implement the soft, advisory
    duplicate check described in Section 5 of the design doc
    (`trader_id + symbol + action + option_type + price + expiration` within a
    short ingestion window) as a method on the service.
  - **Files affected**: `database/service.py` (new).
  - **Tests**: Match found within window, no match outside window, no match on
    differing fields.
  - **Acceptance criteria**: The check never raises or blocks; it returns a
    warning the caller can act on.

- **2B.6b — edit-on-update rule**
  - **Objective**: Ensure `TradeService` never updates a `trade_signals` row
    without first writing a `trade_signal_edits` snapshot via
    `repository.py`.
  - **Files affected**: `database/service.py` (extend).
  - **Tests**: Update produces exactly one edit row with correct pre-edit
    snapshot; multiple edits produce an ordered history.
  - **Acceptance criteria**: No code path in `TradeService` can update a signal
    without a corresponding edit record.

- **2B.6c — TradeService.ingest_message(...)**
  - **Objective**: Implement the single public ingestion entry point: given a
    source, raw message text/metadata, and zero or more parsed trade signals,
    persist everything in the correct order (`sources` → `traders` →
    `raw_messages` → `trade_signals`) via `repository.py`, surfacing duplicate
    warnings from 2B.6a.
  - **Files affected**: `database/service.py` (extend).
  - **Tests**: End-to-end test from raw input to all rows persisted correctly
    across tables; verify FK relationships are correct.
  - **Acceptance criteria**: `TradeService.ingest_message(...)` is the only
    entry point the UI layer will call to persist data — no other code reaches
    into `repository.py` directly for a full ingestion.

### Milestone 2B.7 — Integration and transaction tests

- **Objective**: End-to-end tests exercising `TradeService` against a real
  (temp-file) SQLite database, confirming transactional integrity (e.g. a
  failure partway through `ingest_message` does not leave partial rows).
- **Files affected**: `tests/` only — no new production module.
- **Expected deliverables**: A test suite covering the full ingestion path,
  duplicate warnings, and edit history, run against `db.py` + `schema.sql`
  as they will actually be used.
- **Tests**: Full ingest happy path; partial-failure rollback; duplicate
  warning surfaced but not blocking; edit history correct after multiple
  corrections.
- **Acceptance criteria**: All tests pass against a real SQLite file, not just
  in-memory mocks.

Milestone 2B is complete when 2B.1–2B.7 (including all lettered sub-steps) are
implemented, tested, reviewed, and committed individually. Wiring this into the
actual Streamlit UI (`app/app.py`) is explicitly **out of scope** for 2B and
will be its own future milestone.

### Milestone 2C.1 — Parser Module

- **Objective**: Implement a source-independent parser module that converts
  raw pasted trade message text into structured trade signal data. The parser
  must contain no source-specific assumptions (e.g. no Discord-specific
  logic) and no networking, so that future sources (Discord, Telegram, Slack,
  Email, etc.) can reuse it without modification.
- **Files affected**: Parser module (new).
- **Expected deliverables**: A parser function/module that accepts raw
  message text and returns structured trade signal data matching the fields
  `TradeService.ingest_message()` expects.
- **Tests**: Well-formed messages, malformed/unparseable messages, and
  messages with missing optional fields.
- **Acceptance criteria**: The parser contains no source-specific logic and
  no networking code; the parser has no dependency on `app/app.py` or any
  database module.

### Milestone 2C.2 — Manual Message Entry UI

- **Objective**: Add a manual message entry flow to the Streamlit UI allowing
  the user to paste raw trade message text, invoke the Milestone 2C.1 parser,
  and review the resulting structured trade data before it is persisted. No
  persistence occurs in this milestone.
- **Files affected**: `app/app.py` (extend).
- **Expected deliverables**: A UI input for pasting raw message text, a UI
  action that invokes the parser, and a display of the parsed structured
  trade data (or parse errors) to the user.
- **Tests**: Coverage of successful-parse display and parse-error display for
  pasted input.
- **Acceptance criteria**: No calls to `TradeService` or `repository.py` from
  this milestone; existing UI behavior outside manual entry is unchanged.

### Milestone 2C.3 — TradeService Integration

- **Objective**: Wire the manual entry UI's parsed structured trade data to
  `TradeService.ingest_message()`, so a user-submitted, parsed message is
  persisted through the existing service layer only.
- **Files affected**: `app/app.py` (extend).
- **Expected deliverables**: A submit action that passes the parser's
  structured output, together with the required source and message metadata,
  to `TradeService.ingest_message()`, surfacing any duplicate warnings
  returned to the user.
- **Tests**: A submitted message results in `TradeService.ingest_message()`
  being called with correctly structured arguments; duplicate warning
  surfaced but not blocking.
- **Acceptance criteria**: `TradeService.ingest_message()` remains the only
  persistence entry point; `app/app.py` makes no direct calls to
  `repository.py`.

### Milestone 2C.4 — Integration Tests

- **Objective**: End-to-end tests exercising the full manual-entry path —
  pasted raw text through the parser, through the UI wiring, into
  `TradeService.ingest_message()`, and persisted to a real (temp-file)
  SQLite database.
- **Files affected**: `tests/` only — no new production module.
- **Expected deliverables**: A test suite covering the full manual-entry
  ingestion path, malformed-input handling, and duplicate-warning behavior,
  run against `db.py` + `schema.sql` as they will actually be used.
- **Tests**: Full manual-entry happy path; malformed/unparseable input
  handled without persisting partial data; duplicate warning surfaced but not
  blocking.
- **Acceptance criteria**: All tests pass against a real SQLite file, not
  just in-memory mocks or parser-only unit tests.

## Phase 2D — Manual Version 1 Operational Readiness

Purpose:

Prepare the existing manual-entry application for controlled practical user
testing and the v0.1 release without changing the frozen architecture or
database design.

### Milestone 2D.1 — Runtime Configuration and Application Data Locations

Scope:

- Replace the working-directory-relative database path in `app/app.py` with
  an explicitly resolved Windows application-data path.
- Default production database location:
  `%LOCALAPPDATA%\DiscordTraders\discord_traders.db`
- Support an optional `DISCORD_TRADERS_DB_PATH` environment-variable
  override for development and controlled testing.
- Preserve `DatabaseConfig.db_path` as a required field with no hidden
  dataclass default.
- Create required parent directories safely.
- Keep development, test, and production database locations isolated.
- Keep `TradeService.ingest_message()` as the only UI persistence entry
  point.
- Use only Python standard-library path and environment handling unless a
  separate dependency is later approved.

Expected files when implemented:

- `database/config.py`
- `app/app.py`
- `tests/test_config.py`
- `tests/test_app.py` only if required

Explicit exclusions:

- No logging implementation.
- No database backup.
- No stored-signal review UI.
- No correction workflow.
- No packaging.
- No Discord connection or authentication.
- No parser, schema, repository, model, or service redesign.

### Milestone 2D.2 — Operational Logging and Error Handling

Scope summary:

- Add persistent application logging.
- Record startup, configuration, database, ingestion, and unexpected
  application failures.
- Present safe user-facing errors while retaining diagnostic detail in
  logs.
- Prevent secrets or sensitive credentials from being written to logs.

### Milestone 2D.3 — Database Backup and Recovery

Scope summary:

- Add controlled timestamped backups of the production SQLite database.
- Validate backup integrity.
- Define safe restore behavior.
- Prevent partial or destructive restore operations.

### Milestone 2D.4 — Stored-Signal Review UI

Scope summary:

- Display persisted trade signals.
- Support practical filtering by source, trader, symbol, and date.
- Display the original raw message and persisted parsed fields.
- Keep read operations behind approved application/service boundaries.

### Milestone 2D.5 — Correction and Audit Workflow

Scope summary:

- Allow controlled corrections to persisted trade signals.
- Route corrections through `TradeService.update_trade_signal()`.
- Preserve `trade_signal_edits` audit history.
- Display correction history.
- Split this milestone into lettered sub-milestones during its detailed
  planning review if required.

### Milestone 2D.6 — Windows Startup and Packaging

Scope summary:

- Provide a reliable Windows startup method for a non-technical user.
- Define dependency installation and packaging.
- Allow the application to start without VS Code or Claude.
- Evaluate packaging dependencies separately before approval.

### Milestone 2D.7 — Release and User-Acceptance Tests

Scope summary:

- Test startup on a clean environment.
- Test restart and persistent data.
- Test review and correction workflows.
- Test backup and recovery.
- Prepare the v0.1 release checklist and controlled user-acceptance
  procedure.

## Phase 2E — Discord Ingestion

Phase 2E is not yet scoped or approved for implementation. It will later
cover:

- Discord authentication
- Server and channel configuration
- Event-driven or background ingestion
- Source and trader mapping
- External Discord message-ID deduplication
- Operational resilience
- Discord integration tests
- v0.2 release validation

Detailed Phase 2E milestones are intentionally not defined yet.

## 4. Git Commit Policy

- Every approved milestone (including each lettered sub-step, e.g. 2B.5a,
  2B.5b) ends with **exactly one** Git commit.
- No milestone spans multiple commits, and no commit spans multiple
  milestones.
- A commit is only made after: implementation is complete, tests pass, and the
  milestone has been reviewed and approved.

## 5. Handoff Policy

After every completed milestone:

1. `docs/PROJECT_STATUS.md` is updated to reflect the new current milestone,
   completed items, and next milestone.
2. A handoff document is saved to `docs/HANDOFFS/` (e.g.
   `docs/HANDOFFS/2B.5a_repository_sources.md`) summarizing:
   - What was implemented
   - Tests completed
   - Known limitations
   - Next milestone

## 6. Scope Control

Not allowed during implementation, without a separate, explicit approval step:

- Architecture redesign or schema changes
- Adding new dependencies without approval
- Changing the approved schema (`docs/DATABASE_DESIGN_V1.md`)
- Splitting `repository.py` or `service.py` into per-table/per-concern modules
- Feature creep beyond the milestone's stated objective
- Parser redesign
- Streamlit/UI redesign
- Combining or reordering milestones
- Skipping tests or review before committing

## 7. Implementation Order

```
Milestone 2B.1  — schema.sql
Milestone 2B.2  — config.py
Milestone 2B.3  — db.py
Milestone 2B.4  — models.py
Milestone 2B.5  — repository.py (implemented incrementally)
  2B.5a — sources access
  2B.5b — traders access
  2B.5c — raw_messages access
  2B.5d — trade_signals access
  2B.5e — trade_signal_edits access
Milestone 2B.6  — service.py (implemented incrementally)
  2B.6a — TradeService scaffold + duplicate detection
  2B.6b — edit-on-update rule
  2B.6c — TradeService.ingest_message(...)
Milestone 2B.7  — Integration and transaction tests
Milestone 2C.1  — Parser Module
Milestone 2C.2  — Manual Message Entry UI
Milestone 2C.3  — TradeService Integration
Milestone 2C.4  — Integration Tests
Milestone 2D.1  — Runtime Configuration and Application Data Locations
Milestone 2D.2  — Operational Logging and Error Handling
Milestone 2D.3  — Database Backup and Recovery
Milestone 2D.4  — Stored-Signal Review UI
Milestone 2D.5  — Correction and Audit Workflow
Milestone 2D.6  — Windows Startup and Packaging
Milestone 2D.7  — Release and User-Acceptance Tests
```

Milestone 2B is complete once 2B.7 is committed. The next milestone (2C or
later, e.g. UI wiring) will be scoped and approved separately at that point.
