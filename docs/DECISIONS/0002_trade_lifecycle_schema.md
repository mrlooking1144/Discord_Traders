# Decision Title

Add a lifecycle-linking persistence layer (`trade_lifecycles`, `trade_lifecycle_events`, `trade_signals.lifecycle_id`) on top of the Recovery schema, to support Recovery Milestone R6.

# Status

Accepted (R6.1 scope only — schema, migration, and model foundation; no matching/linking behavior yet).

# Context

Recovery Milestones R1–R5 gave the pipeline idempotent, channel-scoped, reprocessable ingestion of noisy Discord trade alerts (`docs/DECISIONS/0001_recovery_message_storage_and_lifecycle_schema.md`; `docs/HANDOFFS/R5_ingestion_reprocessing_and_checkpoints.txt`), but every `trade_signals` row remains independent — there is no linkage from an entry to its later scale-ins, partial exits, or final exit, and no way to answer "is this position still open" or "what happened to this contract over time." `docs/DECISIONS/0001_...md` explicitly deferred this: *"`lifecycle_id` is deliberately deferred to the milestone that introduces `trade_lifecycles` (R6) rather than added as a dangling forward reference now."* This ADR is that milestone's schema foundation.

R6's full design (matching/linking rules, the lineage-aware rebuild algorithm, correction-workflow integration, membership-integrity validation) was developed across several planning rounds and passed ChatGPT quality review before any implementation began, per this project's `docs/REVIEW_RULES.md`. This ADR records only the schema/model portion approved for R6.1; the matching/linking behavior itself is out of scope for this ADR's authorized implementation and lands in R6.2–R6.4.

# Decision

Extend the schema **additively only**, exactly as `docs/DECISIONS/0001_...md` did for R1: no existing table is dropped, no existing column is removed or retyped, no existing row is touched.

## New tables

- **`trade_lifecycles`** — one row per lifecycle *generation*. A "generation" is the persisted, immutable-once-created outcome of replaying one `(trader_id, symbol, option_type, strike, expiration)` key's current signal history through the (not-yet-implemented) deterministic matching engine. `status` is constrained to exactly six values: `open`, `partially_closed`, `closed`, `orphan`, `unresolved`, `invalid` — no other value is valid, and no seventh status (e.g. a dedicated "stopped out" state) is introduced; a stop-out remains representable as `status='closed'` plus the closing signal's own free-text `notes`, per the R3 extractor grammar's existing decision that a stop-out is a `FULL_EXIT`, never a separate event type. `remaining_fraction` is stored as the exact string form of a rational (`fractions.Fraction`), not a `Decimal` string, because several corpus fractions (`1/3`, `1/6`) are non-terminating in base 10 and repeated `Decimal` arithmetic risks a rounding residue that never exactly reaches `"0"`. `opened_by_signal_id`/`closed_by_signal_id` are nullable FKs into `trade_signals` (an `orphan`/`unresolved` generation may have neither). No denormalized `entry_price` column is added — a verified entry price is read via `opened_by_signal_id`, avoiding a second source of truth.
- **`trade_lifecycle_events`** — the membership/audit table linking a `trade_lifecycles` generation to the `trade_signals` rows that made it up, in chronological order (`sequence_index`), **plus an immutable `signal_snapshot` column** (canonical JSON, `NOT NULL`) captured at the moment the membership row is created. This is required because the existing 2D.5 correction workflow (`TradeService.update_trade_signal()`) edits several `trade_signals` fields in place; without a frozen snapshot, joining an old (superseded) lifecycle generation to *live* `trade_signals` rows later would silently show post-correction values instead of the values actually in effect when that generation was built, corrupting historical audit evidence. The snapshot captures, at minimum: `trade_signal_id`, `raw_message_id`, `trader_id`, `symbol`, `option_type`, `strike`, `expiration`, `event_type`, `qualifier`, `action`, `price`, `stated_entry_price`, `stated_return_pct`, `notes`, `extraction_id`, and the exact ordering key used to place this signal within its generation's replay. Snapshots are write-once: no repository function updates or deletes a `trade_lifecycle_events` row, matching `raw_messages.raw_text`'s existing write-once contract.

## Extended existing table

- **`trade_signals`** gains a nullable `lifecycle_id INTEGER REFERENCES trade_lifecycles(id)`. This is a **maintained pointer**, not raw audit data — the one narrow, explicitly-scoped exception to `trade_signals`' otherwise strict immutability (distinct from, and narrower than, the six fields the 2D.5 correction workflow may already touch). It is written only by the (not-yet-implemented) lifecycle engine, never by ingestion, reprocessing-of-extraction, or the correction workflow directly. It always points at the *current* generation a signal belongs to, or `NULL` if the signal belongs to none (including every legacy `ingest_message()` signal, whose `event_type IS NULL` makes it permanently ineligible for lifecycle linking).

## Supersede-not-delete generations

`trade_lifecycles.is_current`/`superseded_at` mirror `message_extractions`' already-proven contract exactly (`database/migrations/0002_message_extractions.sql`): a generation is never edited in place once created (aside from these two bookkeeping fields); when the signals it was built from change (via reprocessing or a key-changing correction), the old generation row is superseded (`is_current = 0`, `superseded_at` stamped) and, if the rebuilt result is non-empty, a fresh generation row is inserted as current. Unlike `message_extractions` (a strict 1-raw-message : at-most-1-current invariant, enforced by a partial unique index), a single lifecycle key legitimately has **multiple simultaneously current rows over time** — every past terminal generation for that key (each a distinct re-entry) remains `is_current = 1` until *its own* lineage changes, alongside at most one current non-terminal (`open`/`partially_closed`) row. No database-level uniqueness constraint expresses this multi-row invariant; it is enforced by the engine's construction and independently checked by pre-commit validation queries (an R6.4 concern, not implemented in R6.1).

## Auditability

Every `trade_lifecycles` row, current or superseded, remains permanently queryable — nothing is ever deleted. Every `trade_lifecycle_events` row, including those belonging to a since-superseded generation, remains permanently queryable with its frozen `signal_snapshot`, independent of whatever the live `trade_signals` row it references has since become. This gives R6 (and later, R7) two independent audit views: "what does this signal currently look like" (join to live `trade_signals`) and "what did this generation actually look like when it was built" (the frozen snapshot) — the second is not derivable from the first once a correction has occurred.

## No automatic data backfill

Unlike R1's `message_extractions` backfill (necessary because pre-existing `raw_messages` rows had no extraction row at all), this migration performs **no backfill of any kind**. Every pre-existing `trade_signals` row simply gets `lifecycle_id = NULL` by default via the column's own default — correct and final for legacy (`event_type IS NULL`) rows, and merely *pending* for any R5-era signal with a real `event_type` that has not yet been linked. Linking pre-existing data is a deliberate, separate, explicitly-authorized operational step (`TradeService.rebuild_all_lifecycles()`, an R6.4 deliverable) run manually after the matching engine exists — never automatically from inside this or any migration, matching every existing migration's schema-only, side-effect-free scope.

## R6.1 scope boundary

This ADR and its accompanying migration authorize **schema, migration, and model changes only**. No matching/linking logic, no repository query behavior beyond what the schema itself enforces (constraints/indexes), no `TradeService` orchestration, no UI change, and no real-corpus acceptance testing are authorized by this ADR — those remain R6.2 through R6.7, each requiring its own review before implementation, per `docs/REVIEW_RULES.md`. In particular:

- **R7 (trader-performance analytics)** and **R8 (UI — batch import, checkpoint view, lifecycle review, trader-performance dashboard)** remain entirely out of scope. This ADR creates the tables R7/R8 will eventually read from, but defines no aggregation, no return calculation, and no display of any kind.
- Every architecture decision reached during R6's planning review is treated as binding for later R6 slices, in particular: lineage-based (not timestamp-based) boundaries for rebuilding a terminal generation; an `ADD` with no verified active lifecycle becomes its own `unresolved` singleton, never a fabricated open/closed lifecycle; a correction that would change `action` on a lifecycle-managed (`event_type IS NOT NULL`) signal is rejected outright by the correction API, never partially applied; `database/lifecycle.py` (a later slice) remains a pure module with no database access, receiving only immutable ordered signal snapshots from repository/service code.

# Alternatives Considered

- **Do nothing / keep deriving "is this position open" from raw `trade_signals` queries ad hoc**: rejected — every consumer (a future review UI, R7's analytics) would have to reimplement the same non-trivial fraction/re-entry/orphan logic independently, and inconsistently.
- **Add `trade_signals.lifecycle_id` without the `trade_lifecycle_events` membership table, relying solely on the pointer column for lifecycle history**: rejected — a pointer column only ever tells you a signal's *current* generation; once a generation is superseded, nothing would show what it used to consist of. The membership table is the direct, one-level-up analogue of `trade_signal_edits`, and is required for "auditable lifecycle history" to mean anything once reprocessing/correction start superseding generations.
- **Store `trade_lifecycle_events` without an immutable snapshot, joining historical views to live `trade_signals` instead**: rejected — the 2D.5 correction workflow can and does edit `trade_signals` fields in place; a historical join would then silently show corrected values as if they were what the generation was actually built from, misrepresenting audit history.
- **Represent a stop-out as its own `trade_lifecycles.status` value**: rejected for R6.1 — the R3 extractor grammar deliberately never introduced a separate stop event type (a stop-out is a `FULL_EXIT` with a free-text reason in `notes`); inventing a status value here would require a heuristic text match against `notes` that the grammar itself does not support, and was explicitly rejected during R6 planning as premature guessing.
- **Use `Decimal` for `remaining_fraction`**: rejected — several of the extractor's own approved fraction tokens (`1/3`, `1/6`) do not terminate in base 10; exact rational arithmetic (`fractions.Fraction`) avoids any rounding-residue risk when checking whether an exit exactly zeroes the remaining position.
- **Backfill lifecycle links for existing data as part of this migration**: rejected — migrations in this project are schema-only and side-effect-free by established convention (see `docs/DECISIONS/0001_...md`); linking is business logic that does not exist yet in R6.1 and, once it does (R6.4), must be run as a deliberate, observable, separately-authorized step, not an invisible migration side effect.

# Consequences

- `database/schema.sql` is unchanged; this migration layers on top of it exactly as R1's migrations did, via the existing `database/migrations/` framework and `schema_migrations` tracking table (`database/db.py`).
- `database/models.py` gains `TradeLifecycle` and `TradeLifecycleEvent` dataclasses, mirroring the new tables field-for-field, with no database access or business logic — matching every existing model in this file.
- `database/models.py`'s `TradeSignal.event_type` docstring, which had listed `STOP_EXIT` as an example value, is corrected to match the R3-approved event-type vocabulary (`ENTRY`, `ADD`, `ROLL_UP`, `PARTIAL_EXIT`, `FULL_EXIT` — exactly five, no `STOP_EXIT`); this is a documentation correction only, no behavior changes.
- No repository or service code reads or writes `trade_lifecycles`/`trade_lifecycle_events`/`trade_signals.lifecycle_id` as of this ADR — that begins in R6.3/R6.4.
- Every pre-existing `trade_signals` row (and every row inserted by ordinary R5 ingestion until R6.4 ships) has `lifecycle_id = NULL`; this is expected and correct, not a defect.
- Future schema changes for R6's own later slices (if any) must go through their own numbered migration and, if they touch the frozen design in a new way, their own ADR addendum — this ADR covers only the tables/columns/indexes listed above.

# Date

2026-07-28

# Approved By

Product Owner and Technical Architect (ChatGPT), R6 planning review — R6.1 scope only. Recovery Milestones R6.2 onward remain separately gated and are not authorized by this ADR.
