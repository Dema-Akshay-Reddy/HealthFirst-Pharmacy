# Architecture: Transactional Outbox (Primary → Secondary)

> For the record: `pharmacy-primary` (DB_A) and `pharmacy-secondary` (DB_B) are **two independent
> D1 databases**. Cloudflare's read-replication feature only syncs a replica of ONE database, so it
> cannot keep these two in sync. The Worker therefore implements an explicit, transactional outbox.

---

## 1. Decisions

1. **Transactional outbox** in D1 (not a separate message queue). Every business write lands in
   `session.batch()` together with its outbox event row, so commit-abort is atomic.
2. **State-based apply** on the secondary: the drainer re-reads the current primary state per event
   group and upserts the final row values. We never apply deltas (e.g. "decrement by 2"), because a
   retried event must be a pure re-write, never a second decrement.
3. **One atomic batch per event group** on DB_B. Grouped by request `txn` (captures whole
   multi-table workflows), so a dispense of `batches`+`sales`+`bills`+`bill_items` lands atomically
   and can roll back together if any single statement fails.
4. **FK-safe ordering**: parents before children for upserts, children before parents for deletes.
5. **Watermark in the same atomic batch as the data** on the secondary — a committed data batch is
   proof of commit; a failed data batch is never committed.
6. **Per-isolate idempotency**: `ensureSchemaPrimary()` is memoized per isolate; the outbox DDL is
   `CREATE TABLE IF NOT EXISTS`.
7. **Retention**: applied events older than 7 days are pruned (keeps the outbox small). Nothing older
   than that is needed for recovery.
8. **Bookkeeping tables** (`sync_bookmarks`, `replication_outbox`, `replication_state`) are
   per-database and never replicated.

---

## 2. Capture (write path)

`db.js` `write()` is the only write choke point. For every captured statement:

| Statement shape | Capture mechanism | Guarantee |
|---|---|---|
| `INSERT ... (cols) VALUES (?,?)` with all PK columns in the column list | **pre-capture** — outbox event with the PK values as literal params, inserted atomically with the row | row can be fetched exactly with its PK right after |
| `INSERT ...` (PK auto or computed) | **post-capture** — `INSERT INTO replication_outbox SELECT ... FROM table WHERE rowid = last_insert_rowid()` | the just-inserted row's PK is captured; no phantom events |
| `UPDATE table SET ... WHERE ...` | **pre-capture** — `INSERT INTO replication_outbox SELECT ... FROM table WHERE <where>` | all matching rows are captured; where-clause params are scoped into the same batch |
| `DELETE FROM table WHERE ...` | **pre-capture** — same pattern | all matching rows are captured |
| Bookkeeping (`sync_bookmarks`, `replication_outbox`, `replication_state`) | `kind: none` — plain write, not captured | nothing self-referential |
| Unrecognised shape | `kind: unknown` — the write still happens, but the event is flagged and reported as `unreplicated` in `/api/sync/status` (never silently skipped) | no write path is silent |

Every captured write is executed in a single `session.batch()` — the business statement and its
capture event are one transaction. Either both commit or neither does.

---

## 3. Apply (drain path)

`replication.js` `drainReplication()`:

1. Read up to `maxEvents` (default 500) pending outbox events, ordered by `id`.
2. Group by `txn` → whole workflows apply atomically on DB_B.
3. For each group, build one batch:
   - upserts in FK-safe order (parents first);
   - deletes in reverse FK order (children first);
   - watermark `INSERT INTO replication_state ... ON CONFLICT(id) DO UPDATE` appended to the same
     batch.
4. Mark events applied and commit the watermark in the same atomic batch on the primary
   (direct statement, not captured).
5. Stop on the first error → next trigger retries the whole group. `consecutive_failures` is
   incremented and the error is stored as `last_failure` for `/api/sync/status`.
6. Time-budgeted (default 20 s) to never block a cron indefinitely.

Idempotency is structural, not best-effort:
- **Duplicates** (same `txn` drained twice): the apply step re-reads the primary state; an upsert
  with identical values is a no-op; a delete of an already-deleted row is a no-op. Row counts never
  change.
- **Reorders** (secondary behind): the watermark updates; the drain simply picks up the next batch.

---

## 4. Failure & recovery

| Scenario | Behaviour |
|---|---|
| Secondary unavailable / SQL error | drain group fails → `failed` counted, `last_failure` stored, remaining events retried next trigger. Temporary outages self-heal. |
| Table renamed/removed on secondary | error recorded, pending events retried after repair (verified live: rename → failure → restore → success) |
| Permanent divergence (data mismatch, extra/missing rows) | `POST /api/sync/reconcile?prune=1` re-verifies every PK and prunes rows that no longer exist on the primary. Never destructive to a healthy secondary. |
| Deploy-before-migrate ordering | primary outbox/schema created lazily on first write/drain — write path never breaks |
| Lost `waitUntil` drain | cron `* * * * *` re-drains the whole outbox every minute — events cannot be lost |

---

## 5. Guarantees & limitations

### Guarantees
- Every primary data change is captured atomically with its event.
- No duplicate effects: state-based apply + per-group atomic batches.
- Multi-table workflows (dispense → `batches`+`sales`+`bills`+`bill_items`) apply atomically on the
  secondary.
- Inserts, updates, and deletes are all supported.
- Temporary secondary failures are detected and retried (verified live).
- Monitoring reports pending, lag, last applied, last success, and latest failure.

### Limitations
- **No cross-region sync to a third database** — the outbox only mirrors primary → secondary.
- **No server-side foreign-key enforcement on DB_B** for replicated tables (D1 in Workers has
  limited FK support in some configurations) — the outbox's own ordering layer replaces the
  FK-guarantee. The primary (DB_A, where the app's `PRAGMA foreign_keys=ON`) keeps the real
  constraint enforcement.
- **Eventual consistency** between the two databases (replicated via the Worker, not via
  Cloudflare read replication).
- `SELECT`/read-only statements are **not** captured (they carry no state changes).
- The outbox only covers statements that go through `write()` — any future write helper must go
  through the same path or the data would diverge silently.
- Rowid-post-capture captures the *first* row of the affected set; a guarded
  `INSERT...SELECT` that inserts 0 rows captures a stale rowid, which is harmless because the
  apply step re-reads current row state.
