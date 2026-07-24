# Decision Title

Extend the frozen V1 schema to support noisy Discord message storage, parse-status/parser-version tracking, channel-scoped idempotent ingestion, and trade lifecycle linking (Recovery Milestone R1).

# Status

Accepted (R1 scope only — schema and migration foundation).

# Context

v0.1.0 shipped with `docs/DATABASE_DESIGN_V1.md` explicitly frozen and `database/schema.sql` carrying a header comment prohibiting new tables, columns, or constraints beyond that design. That design deliberately excluded a channel concept, a strike column, parser version, parse status, processing state, and any per-channel checkpoint — all noted in its own Section 3 and Section 8 as out of scope for V1.

After release, the Product Owner supplied a real Discord channel-history corpus (traders `Bdorts`, `TC`, `spacemonkey`, `Matae`, `Sarang` across AVGO/IBM/NVDA/TSLA/QQQ/SPX/JPM/MU) that the shipped parser and schema cannot represent: it has no way to store which channel a message came from, no way to classify whether a message was successfully parsed, no way to reprocess a stored message under a newer parser, and no way to enforce "same channel + same message ID = same message" idempotency. The parser's own Milestone 2C.1 handoff already flagged that its grammar was never validated against real-world samples.

This ADR authorizes the Recovery (R-series) milestone track — approved by the Product Owner via the plan at `C:\Users\sstay\.claude\plans\serene-stargazing-shamir.md` — to extend the schema additively. This is explicitly not Phase 2E (no Discord bot, API, auth, or live event stream): it is schema/storage work to support paste-based batch ingestion of exported/copied text.

# Decision

Extend the schema **additively only** for tables, columns, and data — no existing table is dropped, no existing row is touched, and no column is removed or retyped. The one exception is a single obsolete *index* (see the uniqueness policy below), which is replaced rather than data:

- New tables: `channels`, `import_batches`, `message_extractions` (one row per parse *attempt*, not per message — reprocessing supersedes the prior current row rather than mutating it).
- `raw_messages` gains nullable `channel_id`, `import_batch_id`, `sequence_in_batch`.
- `trade_signals` gains nullable `strike`, `expiration_raw`, `event_type`, `qualifier`, `stated_entry_price`, `stated_return_pct`, `notes`, `extraction_id`. `lifecycle_id` is deliberately deferred to the milestone that introduces `trade_lifecycles` (R6) rather than added as a dangling forward reference now.
- `traders` gains nullable `canonical_name` (lowercased/trimmed), backfilled for existing rows, enabling future case-insensitive identity resolution (e.g. `Matae`/`matae`) without changing today's allowance for duplicate `(source_id, name)` rows.
- A new `database/migrations/` directory and `schema_migrations` tracking table replace the assumption that `schema.sql`'s `CREATE ... IF NOT EXISTS` statements are the only schema-change mechanism ever needed. `schema.sql` remains the v0.1.0 baseline; numbered migration files layer additive changes on top, tracked so each applies exactly once.
- A one-time backfill migration inserts a synthetic `parser_version='legacy'`, `parse_status='parsed'` `message_extractions` row per pre-existing `raw_messages` row, so v0.1.0 data satisfies the new join-based status queries without reprocessing.

### Migration atomicity

Each migration file is applied inside one explicit SQLite transaction (`BEGIN IMMEDIATE` ... `COMMIT`) that covers *both* its own statements *and* the `INSERT` recording it in `schema_migrations`. `sqlite3.Connection.executescript()` is not used for this, because it unconditionally issues a `COMMIT` before running, which would commit our explicit `BEGIN` out from under us and reopen the exact window we need closed — a mid-script failure could leave earlier statements in that same file committed while the tracking row is not. Instead, each migration file's SQL is split into individual statements (`database/db.py`'s `_split_sql_statements`) and executed one at a time via `connection.execute()` inside the explicit transaction; on any failure the whole transaction is rolled back (verified empirically: SQLite fully supports rolling back DDL, including `ALTER TABLE ADD COLUMN` and `CREATE INDEX`, when it runs inside an explicit transaction) and the exception propagates. This guarantees:

- A failed migration is never recorded in `schema_migrations`.
- A migration whose statements all succeed cannot end up unrecorded — the insert is inside the same transaction, not a separate step after.
- No partial DDL/DML from a failed migration is left behind; retrying the same filename after a fix starts from a clean slate rather than double-applying part of it.
- Migrations already applied and committed in earlier calls are unaffected by a later migration's failure — each file is its own transaction.

`apply_migrations()` takes an optional `migrations_dir` parameter (defaulting to `database/migrations`) specifically so this behavior can be tested against deliberately-broken, disposable migration files without touching the project's real ones.

### Raw-message uniqueness policy (final)

The authoritative idempotency key is **`(channel_id, external_id)`**, not `(source_id, external_id)`. The original v0.1.0 `idx_raw_messages_source_external_id` index does not simply gain a sibling index — it is fully replaced, because leaving it in place would keep blocking a message ID from validly repeating across two different channels of the same source, which is exactly the scenario channel-scoped ingestion needs to allow. The final policy, implemented as two partial unique indexes:

- `idx_raw_messages_channel_external_id` on `(channel_id, external_id)` `WHERE channel_id IS NOT NULL AND external_id IS NOT NULL` — within a real channel, `external_id` must be unique; the same `external_id` **may** repeat across two different channels.
- `idx_raw_messages_null_channel_source_external_id` on `(source_id, external_id)` `WHERE channel_id IS NULL AND external_id IS NOT NULL` — for rows with no channel (e.g. today's manual-entry path, which never sets `channel_id`), `external_id` must still be unique per source, exactly matching the pre-migration behavior, so existing callers that never adopt channels keep the same duplicate protection they had before.

The corresponding `CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_messages_source_external_id` statement was **removed from `database/schema.sql`'s baseline**, not merely superseded by a later `DROP INDEX`. This is necessary, not cosmetic: `schema.sql` is re-applied via `executescript()` on every `initialize_database()` call, so if that statement remained, its own `IF NOT EXISTS` would silently recreate the old index again the very next time the application started, undoing the migration's `DROP INDEX` every time. The migration (`0003_extend_raw_messages.sql`) still issues `DROP INDEX IF EXISTS idx_raw_messages_source_external_id` so an already-upgraded v0.1.0 database (where the old index was physically created under the old `schema.sql`) has it removed exactly once; the removal from `schema.sql` itself is what makes that removal permanent across every future application start, including for brand-new databases that never had the old index at all.

# Alternatives Considered

- **Do nothing / keep the schema frozen indefinitely**: rejected — the real corpus demonstrates the frozen V1 design cannot represent the required Discord workflow (channel scoping, parse status, reprocessing, lifecycle linking) at all, not merely suboptimally.
- **Replace `schema.sql` in place with a new v2 shape and require a manual re-import**: rejected — would lose or require re-entering any existing v0.1.0 data; violates the additive/backward-compatible requirement and the project's stated care around destructive changes.
- **Store channel ID and parse status inside the existing opaque `raw_messages.metadata` JSON blob instead of new columns**: rejected — the metadata blob is explicitly documented as "not queried inside the DB," which would make idempotent `(channel, message_id)` lookups and per-channel checkpoint queries impossible to express in SQL.
- **Add `lifecycle_id` to `trade_signals` now, pointing at a not-yet-existing `trade_lifecycles` table**: rejected — would be a forward reference to an R6 deliverable, out of scope for an R1 schema-only milestone; deferred to land alongside `trade_lifecycles` itself.
- **Keep the old `(source_id, external_id)` index alongside the new channel-scoped one, rather than replacing it**: rejected on review — the two indexes together would still block the same `external_id` from repeating across two different channels of the same source, defeating the whole point of channel-scoped idempotency. A single combined `(channel_id, external_id)` index without a NULL-channel carve-out was also rejected, because SQL treats any NULL in a unique index's key as distinct from every other NULL, so it would silently stop enforcing uniqueness at all for `channel_id IS NULL` rows — breaking existing duplicate-message-ID protection for callers that don't use channels. Two separate partial indexes (one scoped to `channel_id IS NOT NULL`, one scoped to `channel_id IS NULL`) is what correctly preserves both behaviors.
- **Apply the uniqueness fix only via a migration's `DROP INDEX`, without touching `schema.sql`**: rejected — `schema.sql` is re-applied on every `initialize_database()` call, so its own `CREATE UNIQUE INDEX IF NOT EXISTS` for the old index would silently recreate it on the very next application start, making the `DROP INDEX` non-durable. The statement had to be removed from `schema.sql`'s baseline itself.
- **Use `executescript()` for migrations, accepting non-atomic tracking**: rejected on review — `executescript()` always force-commits before running, which is incompatible with wrapping a migration's DDL and its `schema_migrations` tracking row in one rollback-able transaction. Statements are now split and executed individually inside an explicit transaction instead.

# Consequences

- `database/schema.sql`'s header comment ("do not add tables, columns, or constraints beyond what that design specifies") is now superseded for the additive changes listed above; `docs/DATABASE_DESIGN_V1.md` remains historically accurate as the V1 design and is not edited, per the project's ADR practice of never modifying an accepted document — a new design doc/addendum should be written in a later recovery milestone if a consolidated V2 reference is wanted.
- `database/schema.sql` itself required one small edit beyond "additive-only": the old `idx_raw_messages_source_external_id` `CREATE UNIQUE INDEX` statement was removed (see the uniqueness policy above). This is the only removal in R1's entire changeset, is limited to an index (no table, column, or row), and is required for the fix to be durable across application restarts rather than cosmetic.
- Every future schema change must go through a numbered `database/migrations/*.sql` file rather than editing `schema.sql` directly, and must remain additive unless a future ADR explicitly authorizes a breaking change.
- Tests (`tests/test_migrations.py::V010BackwardCompatibilityTests`) simulate upgrading a real v0.1.0 database using a frozen literal copy of the original `schema.sql` content — including the now-removed index — rather than reading the live (already-corrected) file, so the backward-compatibility simulation stays accurate regardless of future edits to `schema.sql`.
- Migrations are applied one file per explicit transaction (see "Migration atomicity" above); `tests/test_migrations.py::MigrationAtomicityTests` verifies failure/rollback/retry behavior against disposable, deliberately-broken migration files.
- Existing repository functions (`create_raw_message`, `create_trade_signal`, `create_trader`) gained additive, defaulted keyword arguments; all existing call sites remain valid unchanged.
- `get_trade_signals_matching` and `get_trade_signals_for_review` (the existing duplicate-detection and Review UI queries) are deliberately left untouched in R1 — wiring new columns into review/display behavior is out of scope until the corresponding later milestone (parser, service, and UI changes).
- A v0.1.0 database can be upgraded in place by simply calling `initialize_database()` again; no manual migration steps are required, and no existing row's `raw_text`, price, or identity is altered.

# Date

2026-07-24

# Approved By

Product Owner (recovery milestone plan approval, R1 scope authorized this session).
