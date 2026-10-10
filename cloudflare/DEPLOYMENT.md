# Deployment Guide — Two-Database Synchronization

> Scope: `pharmacy-primary` (DB_A, authoritative writer) → `pharmacy-secondary` (DB_B, read copy).
> This is a **transactional outbox + state-based replication** implementation. Cloudflare's built-in
> read replication cannot sync two *independent* databases, so the Worker replicates explicitly.

---

## 1. Pre-flight checklists

### 1.1 Nothing on production
- [ ] `git status` clean (all changes committed locally first).
- [ ] Back up both production databases (wrangler v4 has **no** `d1 backup` command — use
      `d1 export`; `cloudflare/backups/` is gitignored because these files contain production data):
      ```powershell
      cd C:\Users\aksha\desktop\pharmacy\cloudflare
      New-Item -ItemType Directory -Force -Path .\backups | Out-Null
      $D = Get-Date -Format yyyyMMdd
      npx wrangler d1 export pharmacy-primary   --remote --output .\backups\pharmacy-primary-$D.sql
      npx wrangler d1 export pharmacy-secondary --remote --output .\backups\pharmacy-secondary-$D.sql
      ```
- [ ] Verify the backup files exist and are non-trivial (e.g. `Get-Item .\backups\*.sql | Select Length`
      — a healthy export is megabytes, not bytes, and contains `INSERT INTO` statements).

### 1.2 Code review & automated tests
- [ ] `node --check cloudflare/src/replication.js`
- [ ] `node --check cloudflare/src/db.js`
- [ ] `node --check cloudflare/src/archive.js`
- [ ] `node --check cloudflare/src/worker.js`
- [ ] `node --check cloudflare/src/chat.js && node --check cloudflare/src/bloom.js`
- [ ] `cd cloudflare; node --test test/` — Bloom filter, `planCapture` shape classification (13 tests)
- [ ] `python -m pytest -q` — backend, auth/RBAC, FEFO, billing, chat storage, **plus the
      Worker's own chat API tests** (111 tests). `tests/test_worker_chat.py` boots the real
      Worker under Miniflare on `WORKER_TEST_PORT` (default 18787) against an isolated temp D1,
      so it needs `node` + `cloudflare/node_modules` and skips cleanly if they are missing.
      It covers: per-user isolation, cross-user read/rename/delete/send → 404, idempotent
      retries (no duplicate messages), pagination defaults, ordering/timestamps, validation
      (400/404/405), clear-history scoping, and chat replication to the secondary.
- [ ] Local end-to-end (needs `npx wrangler dev` on 127.0.0.1:8787):
      `python cloudflare/test/e2e_local.py` — 31 checks covering per-user chat isolation,
      cross-user IDOR rejection (404), idempotent retries, pagination, dispense atomicity,
      Bloom negative → 404, and outbox drain.
- [ ] `npx wrangler deploy --dry-run` (or full dry run if supported by the pinned wrangler) — no upload.

### 1.3 Migrations on both databases (before deploy)
Apply **in order** — `0001_replication.sql` first, then `0002_chat.sql` — to **both** databases:

```powershell
cd C:\Users\aksha\desktop\pharmacy\cloudflare

# 0001 — transactional-outbox replication tables
npx wrangler d1 execute pharmacy-primary   --remote --file=./migrations/0001_replication.sql
npx wrangler d1 execute pharmacy-secondary --remote --file=./migrations/0001_replication.sql

# 0002 — per-user chatbot storage (chat_conversations, chat_messages, indexes)
npx wrangler d1 execute pharmacy-primary   --remote --file=./migrations/0002_chat.sql
npx wrangler d1 execute pharmacy-secondary --remote --file=./migrations/0002_chat.sql
```

Both migrations are idempotent: every statement is `IF NOT EXISTS` style, purely additive (no
existing table, column, index or row is modified or dropped), and safe to re-run. The Worker also
executes this DDL lazily per isolate (`ensureSchemaPrimary()`), so a code-before-migration deploy
order cannot break writes or chat — but run them explicitly anyway so the secondary has the chat
tables *before* the first chat write replicates.

Verify the chat schema landed on both databases:

```powershell
npx wrangler d1 execute pharmacy-primary   --remote --command "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'chat_%'"
npx wrangler d1 execute pharmacy-secondary --remote --command "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'chat_%'"
# both must list chat_conversations and chat_messages
```

---

## 2. Deploy the Worker (preview first)

```powershell
cd C:\Users\aksha\desktop\pharmacy\cloudflare
npx wrangler deploy --dry-run
npx wrangler deploy
```

> Wrangler v4's `deploy` has **no `--remote` flag** (that flag belongs to `d1` subcommands) —
> passing it makes wrangler print usage and exit 1. `deploy` always publishes to the remote
> account; verified against wrangler 4.86.0.

Verify live (note: some HTTP client signatures — e.g. Python's `urllib` — are rejected by the
edge with error 1010; `curl` and real browsers work fine. If a tool gets 1010, send a browser UA
with `-A "Mozilla/5.0"`):
```powershell
curl -s -A "Mozilla/5.0" https://pharmacy-api.akshayreddy1451.workers.dev/api/health
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
$BASE=https://pharmacy-api.akshayreddy1451.workers.dev
$TOKEN=<admin session token>   # POST /api/login

curl -s -A "Mozilla/5.0" $BASE/api/sync/status -H "X-Session-Token: $TOKEN"
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

### Spot-check chat replication & isolation (post-deploy)

```powershell
# 1) two users, two independent conversation lists
$TOK_A = (curl -s $BASE/api/login -H "Content-Type: application/json" -d '{"username":"admin","password":"..."}' | ConvertFrom-Json).token
$TOK_B = (curl -s $BASE/api/login -H "Content-Type: application/json" -d '{"username":"pharmacist","password":"..."}' | ConvertFrom-Json).token
curl -s $BASE/api/chat/conversations -H "X-Session-Token: $TOK_A"   # only A's
curl -s $BASE/api/chat/conversations -H "X-Session-Token: $TOK_B"   # only B's

# 2) direct object reference with the OTHER user's conversation id must be 404
curl -s -o NUL -w "%{http_code}`n" "$BASE/api/chat/conversations/<A-conv-id>/messages" -H "X-Session-Token: $TOK_B"   # 404
curl -s -o NUL -w "%{http_code}`n" -X DELETE "$BASE/api/chat/conversations/<A-conv-id>" -H "X-Session-Token: $TOK_B"   # 404

# 3) chat rows replicated to the secondary
curl -s $BASE/api/sync/status -H "X-Session-Token: $TOK_A"          # caught_up: true
npx wrangler d1 execute pharmacy-secondary --remote --command "SELECT (SELECT COUNT(*) FROM chat_conversations) AS convs, (SELECT COUNT(*) FROM chat_messages) AS msgs"
# counts must equal the primary's
```

Chat content is private: never log message bodies, never put message text in URLs/query strings,
and keep conversation ids out of shared dashboards.

### Bloom filter observability

`GET /api/meta` reports `catalog_bloom` — `{ ready, capacity, size_bits, hash_functions, kib,
inserts, estimated_fp_rate }`. `ready: false` means no filter is built yet in that isolate (the
pre-check fails open and the SQL still runs). A `false` answer is only given for a fresh, complete
filter; a positive is always confirmed against `drugs` in D1.

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
npx wrangler deploy
```

If the changes were never committed, roll back with the Cloudflare dashboard
(Workers → pharmacy-api → Deployments → previous version → Rollback) instead of git.

**Migrations do not need rolling back.** Both `0001` and `0002` are additive-only and the old
code ignores the extra tables, so leaving them in place is the correct and safe rollback posture.
Never `DROP` the chat or replication tables during a code rollback — that would delete user
conversation history and replication audit state.

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
