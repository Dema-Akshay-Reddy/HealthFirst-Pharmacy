# Pharmacy Management System — Database Replication Feature

## 1. Goal

Keep `pharmacy-primary` (DB_A) and `pharmacy-secondary` (DB_B) synchronized. Both are **independent
D1 databases** — Cloudflare's read-replication feature cannot sync two separate databases, so the
Worker replicates explicitly via a transactional outbox.

The primary stays the single authoritative writer. All writes go to DB_A; a drainer applies them to
DB_B atomically, idempotently, with retries.

## 2. What was implemented

### New files
- **`src/replication.js`** — outbox capture, drain/apply, status, reconcile, retry + failure handling.
- **`migrations/0001_replication.sql`** — `replication_outbox`, `replication_state`, archive error column.
- **`test/planCapture.test.mjs`** — 32 SQL shapes classified correctly.
- **`DEPLOYMENT.md`** / **`OUTBOX-DESIGN.md`** — deployment and architecture docs.
- `src/db.js` — write() now captures an outbox event atomically with every business write.
- `src/worker.js` — `/api/sync/status`, `/api/sync/run`, `/api/sync/reconcile` routes; per-request
  drain via `ctx.waitUntil`; cron `* * * * *` safety net; `5 0 1 * *` archive untouched.

### Modified files
- `wrangler.jsonc` — cron `* * * * *` added (replication safety net). R2 archive block stays
  commented out (R2 not activated on the account). `DB_A`/`DB_B` bindings unchanged.
- `app.py`, `pharmacy/db.py`, `pharmacy/analytics.py`, `pharmacy/forecasting.py` — same `db.py`
  write() path; Python mirroring (no logic change).
- `static/app.js`, `static/index.html`, `static/styles.css`, `tests/test_roles.py` — Service Worker
  / UI: sync status panel, sync-run button, reconcile trigger, role checks. No logic change.

### Confirmed working locally
- **Multi-table dispense** (batches + sales + bills + bill_items): capture → drain → identical
  bills on primary and secondary (BILL-20261010-001, id 7).
- **Idempotent re-drain**: 3 drains → same bill count (no double-application).
- **Delete workflow**: DELETE captured → applied on both databases; row counts match.
- **Temporary failure**: rename `bills` on primary → drain fails → `last_failure` recorded →
  restore → drain succeeds → `caught_up: true` again.
- **Schema migration** is idempotent; `ensureSchemaPrimary()` is memoized per isolate, so a
  deploy-before-migrate order can never break writes.
- Full pytest suite: **101 tests, all pass**.

## 3. How it meets the requirements

| Requirement | Implementation |
|---|---|
| Insert / update / delete | PKS table; pre/post capture for inserts, pre-capture for updates/deletes |
| Multi-table workflows | Events grouped by `txn`, one atomic batch per group on DB_B |
| No duplicate effects | State-based apply (re-read primary state); idempotent re-drain verified |
| Temporary secondary failure | Recorded, retried on next trigger; verified live |
| Track last applied change | `replication_state.last_applied_seq` + `last_success_at` on both DBs |
| Report pending work | `GET /api/sync/status` → `pending`, `unreplicated`, `lag_seconds` |
| No unnecessary full scans | Outbox is ordered by `id` + indexed; reconciles are explicit, one-way |
| No silent skips | Unknown SQL shapes flagged `op='unknown'`, listed in status `unreplicated.recent` |
| Schema migration | `migrations/0001_replication.sql`; lazy DDL `IF NOT EXISTS` |

## 4. Monitoring endpoint `/api/sync/status`

```json
{
  "mode": "transactional outbox (primary → secondary, state-based apply)",
  "configured": { "primary": true, "secondary": true, "archive": false },
  "reachable": { "primary": true, "secondary": true },
  "pending": 0,
  "unreplicated": { "count": 0, "recent": [] },
  "oldest_pending_at": null,
  "lag_seconds": 0,
  "last_applied_seq": 58,
  "last_success_at": "2026-10-10 03:55:19",
  "needs_reconcile": false,
  "last_failure": null,
  "secondary_watermark": { "last_applied_seq": 58, "at": "2026-10-10T03:55:19.397Z" },
  "caught_up": true
}
```

- `caught_up: true` = secondary reachable + pending 0 + unreplicated 0 + no open failure.
- `configured` only means the bindings exist — it is **not** a sync guarantee.
- `last_failure` reports the latest error + timestamp + consecutive count.

## 5. Monthly archive (R2) — unchanged but gated

- Schedule `5 0 1 * *` preserved (00:05 UTC, 1st of every month).
- The `ARCHIVE` R2 binding is **disabled** (not activated on the account). The Worker returns
  `{ ok: false, skipped: true, reason: "R2 ARCHIVE binding not available…" }` and logs it.
- Snapshot is point-in-time-ish, chunked for memory limits; audit rows go through `write()` so they
  replicate. Progress tracked in `archive_runs` / `sync_bookmarks` — separate from replication.
- Retention: completed archive runs older than 24 months deleted by the scheduled cleanup.

## 6. Deployment

See `cloudflare/DEPLOYMENT.md`:
1. Back up both production databases → `wrangler d1 backup`.
2. Run the migration on both DBs (idempotent).
3. `npx wrangler deploy --dry-run`, then `--remote`.
4. First-run sync: `POST /api/sync/status` → reconcile once → `POST /api/sync/run`.
5. Verify `caught_up: true` + spot-check data equality.
6. Rollback: code `git reset --hard <prev>` + redeploy; data roll back from `primary-backup.sql`.

## 7. One-time rollback / divergence

```powershell
curl -s -X POST https://pharmacy-api.example.com/api/sync/reconcile?prune=1
```

Re-verifies every primary row against the secondary; deletes secondary rows that no longer exist.
Does not delete primary data. Use `prune=1` once after a fresh import to validate.

## 8. Known limitations

- **No cross-region sync** — primary → secondary only.
- D_B has no enforced FK constraints for replicated tables (the outbox's ordering layer replaces
  it); the primary (DB_A) keeps real FK enforcement.
- Eventual consistency between the two databases.
- Only statements through `write()` are captured; new write helpers must use the same path.
- `SELECT`/read-only statements are not captured.

## 9. Status

Implementation complete. Local dev verification done. Production deployment requires explicit
authorization (deployment commands and rollback instructions are in `cloudflare/DEPLOYMENT.md`).
