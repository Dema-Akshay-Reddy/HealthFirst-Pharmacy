# Deployment Guide — Two-Database Synchronization

> Scope: `pharmacy-primary` (DB_A, authoritative writer) → `pharmacy-secondary` (DB_B, read copy).
> This is a **transactional outbox + state-based replication** implementation. Cloudflare's built-in
> read replication cannot sync two *independent* databases, so the Worker replicates explicitly.

---

## 1. Pre-flight checklists

### 1.1 Nothing on production
- [ ] `git status` clean (all changes committed locally first).
- [ ] Back up both production databases: `wrangler d1 backup pharmacy-primary`, `wrangler d1 backup pharmacy-secondary`.
- [ ] Verify the production backup file exists and is non-trivial (e.g. `wc -c primary-backup.sql`).

### 1.2 Code review
- [ ] `node --check cloudflare/src/replication.js`
- [ ] `node --check cloudflare/src/db.js`
- [ ] `node --check cloudflare/src/archive.js`
- [ ] `node --check cloudflare/src/worker.js`
- [ ] `npx wrangler deploy --dry-run` (or full dry run if supported by the pinned wrangler) — no upload.

### 1.3 Migrations on both databases (before deploy)
```powershell
cd C:\Users\aksha\desktop\pharmacy\cloudflare
npx wrangler d1 execute pharmacy-primary --remote --file=./migrations/0001_replication.sql
npx wrangler d1 execute pharmacy-secondary --remote --file=./migrations/0001_replication.sql
```
The migration is idempotent: every statement is `IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` style
and can be re-run safely.

---

## 2. Deploy the Worker (preview first)

```powershell
cd C:\Users\aksha\desktop\pharmacy\cloudflare
npx wrangler deploy --dry-run
npx wrangler deploy --remote
```

Verify live:
```powershell
curl -s https://pharmacy-api.example.com/api/health
# → {"status":"ok","databases":2,"archive":"r2","cron":"monthly"}
```

---

## 3. First-run sync protocol (after deploy, before any traffic)

The Worker lazily creates `replication_outbox` / `replication_state` on the primary with
`ensureSchemaPrimary()` on the first write/drain, so a deploy-before-migrate order can never break
writes. Still, run the official one-time sync:

```powershell
# 1) Confirm bindings
curl -s https://pharmacy-api.example.com/api/sync/status

# 2) Full one-shot reconciliation (with a realistic budget) — normally NOT needed after a clean
#    initial import, but it re-verifies every row and prunes divergence. Use prune=1 once.
curl -s -X POST https://pharmacy-api.example.com/api/sync/reconcile?prune=1

# 3) Drain the outbox (idempotent, safe to run twice)
curl -s -X POST https://pharmacy-api.example.com/api/sync/run
```

Expected first-run status:
```json
{
  "mode": "transactional outbox (primary → secondary, state-based apply)",
  "configured": { "primary": true, "secondary": true, "archive": false },
  "reachable": { "primary": true, "secondary": true },
  "pending": 0,
  "unreplicated": { "count": 0, "recent": [] },
  "lag_seconds": 0,
  "last_applied_seq": <seq>,
  "last_success_at": "<ISO>",
  "needs_reconcile": false,
  "last_failure": null,
  "secondary_watermark": { "last_applied_seq": <seq>, "at": "<ISO>" },
  "caught_up": true
}
```

`caught_up: true` means: secondary reachable, zero pending, zero unreplicated, and no open failure.
`configured` only tells you the bindings exist — it does **not** mean synchronization has happened.

---

## 4. Verifying synchronization (post-deploy, every day)

```powershell
$BASE=https://pharmacy-api.example.com
$TOKEN=<admin session token>   # POST /api/login

curl -s $BASE/api/sync/status -H "X-Session-Token: $TOKEN"
```

The `/api/sync/status` endpoint reports:

| Field | Meaning |
|---|---|
| `configured` | bindings exist (not a sync guarantee) |
| `reachable.primary` / `secondary` | D1 is answering queries |
| `pending` | outbox events not yet applied to DB_B |
| `unreplicated.count` | events flagged `unknown` (unrecognised SQL shape) |
| `lag_seconds` | time since the oldest pending event was created |
| `last_applied_seq` / `secondary_watermark.last_applied_seq` | last applied on primary / on secondary |
| `last_success_at` | when the last drain committed |
| `last_failure` | error + timestamp + consecutive count |
| `needs_reconcile` | `replication_state.reconciled_at` is unset |
| `caught_up` | `reachable.secondary && pending==0 && unreplicated==0 && no last_failure` |

### Spot-check data equality (same values on both databases)

```powershell
# inventory counters
curl -s "$BASE/api/counter/lookup?name=Dolo 650&qty=1" -H "X-Session-Token: $TOKEN"
# invoices
curl -s "$BASE/api/invoices" -H "X-Session-Token: $TOKEN"
# sales_daily
curl -s "$BASE/api/sales?drug_id=1" -H "X-Session-Token: $TOKEN"
# batches
curl -s "$BASE/api/batches?drug_id=1" -H "X-Session-Token: $TOKEN"
```

Every row count, every `usable` counter, and every invoice/bill must match between the two
databases. The outbox applies **final row state** (re-read from the primary per event group), so
deletes, updates, and multi-table workflows (dispense → `batches` + `sales` + `bills` + `bill_items`
in one atomic secondary batch) are all covered.

---

## 5. Rollback

### 5.1 Code rollback (deploy previous commit)
```powershell
cd C:\Users\aksha\desktop\pharmacy\cloudflare
git reset --hard <previous-good-commit>
npx wrangler deploy --remote
```

### 5.2 Data rollback (last resort — requires a verified backup)
1. Stop the Worker (or set `ENVIRONMENT=rollback` for a blue/green split).
2. Restore `pharmacy-primary` from `primary-backup.sql` **only if** the backup was taken before the
   bad deploy.
3. Re-sync `pharmacy-secondary` from the restored primary:
   ```powershell
   npx wrangler d1 execute pharmacy-secondary --remote --file=C:\Users\aksha\desktop\pharmacy\primary-backup.sql
   ```
4. Re-run the first-run sync protocol (section 3) to rebuild the outbox watermark.
5. Re-enable traffic, verify `caught_up: true` and spot-check data equality.

> Never re-run `db:init` or `db:seed` against production as a shortcut. Never reset production
> databases without a verified recovery plan and explicit approval.

---

## 6. Monthly archive (R2)

Keep the existing schedule `5 0 1 * *` (00:05 UTC on the 1st).

- The archive binding `ARCHIVE` is **disabled** in `wrangler.jsonc` (R2 not activated — API error 10042).
- The Worker handles a missing binding gracefully: `handleArchive()` returns
  `{ ok: false, skipped: true, reason: "R2 ARCHIVE binding not available..." }` and logs it.
- To activate: enable R2 in the Cloudflare dashboard (free tier includes 10 GB), set the bucket
  binding `pharmacy-archive`, uncomment the `r2_buckets` block, redeploy.
- The archive snapshot is point-in-time-ish (it builds the image from the current primary in
  streamable chunks, then writes it to R2). Progress is tracked in `archive_runs` / `sync_bookmarks`
  — independent from the cross-database replication state.
- Retention: archive rows older than 24 months are deleted by the scheduled cleanup.

---

## 7. Monitoring & alerting

- The scheduled job `* * * * *` drains the outbox (safety net for any `waitUntil`-lost drains).
- `/api/sync/status` is the source of truth for replication health in dashboards.
- `needs_reconcile: true` → run the reconcile endpoint once; does not delete data.
- `last_failure` non-null → check the worker logs (`npx wrangler tail`), then reconcile or restore.
