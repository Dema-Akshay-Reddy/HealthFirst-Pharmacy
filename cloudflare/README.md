# Smart Pharmacy Inventory — Cloudflare architecture

Implemented with **Wrangler**: 2 real-time-synced databases, a load balancer, and a
monthly-updated archive — one Worker serving both the API and the existing SPA
(`../static/`).

```
                        ┌──────────────────────────────────────────────┐
   users ── HTTPS ──▶   │  Cloudflare anycast edge   (network LB)      │
                        │  • routes each user to the nearest colo      │
                        │  • TLS, DDoS, caching — no LB VM to run      │
                        └───────────────┬──────────────────────────────┘
                                        │
                        ┌───────────────▼──────────────────────────────┐
                        │  Worker: pharmacy-api (src/worker.js)        │
                        │  • app-level load balancer / router          │
                        │  • auth: admin vs pharmacist (roles)         │
                        │  • serves the SPA from ASSETS                │
                        └───┬───────────────┬───────────────┬──────────┘
                            │ writes        │ reads         │ cron (5 0 1 * *)
                            ▼               ▼               ▼
                  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
                  │ D1 PRIMARY       │  │ D1 REPLICA       │  │ R2 ARCHIVE       │
                  │ (pharmacy-       │  │ (pharmacy-       │  │ (pharmacy-       │
                  │  primary)        │  │  primary replica)│  │  archive)        │
                  │ writable,        │◄─┼─ read replication│  │ monthly snapshot │
                  │ single writer    │  │  = continuous,   │  │ archives/…-YYYY-MM│
                  └────────┬─────────┘  │  real-time sync  │  │ .json + retention│
                           │            └──────────────────┘  │  24 months       │
                           │  archive reads a consistent cut  └──────────────────┘
                           └──────────────────────────────────────▲
                                        (bookmark recorded in the snapshot)
```

## The two databases, in real time, in sync

One logical dataset, two synchronized copies — Cloudflare D1 **read replication**:

- **Primary (`DB_A`)** — the only writable copy. Every INSERT/UPDATE lands here.
- **Replica (`DB_B`)** — read-only copy that Cloudflare syncs **continuously**
  from the primary (real time, not nightly/diff-based).
- **Consistency** — the Worker uses the **D1 Sessions API** with a sequential-consistency
  **bookmark**. The newest bookmark is returned in `X-D1-Bookmark` on every response and
  accepted back on the next request (header or `?bookmark=`). Reads then go to a replica
  that is guaranteed at least as fresh as the client's last write → read-your-writes
  across the two databases.
- **Visibility** — `GET /api/sync/status` shows the replication mode, the last stored
  bookmark and the current session bookmark.

Deploy it (production):

```bash
cd cloudflare
npm install
npx wrangler d1 create pharmacy-primary          # → put the id in wrangler.jsonc
npx wrangler d1 enable-read-replication pharmacy-primary
npx wrangler d1 execute pharmacy-primary --remote --file=./schema.sql
npx wrangler d1 execute pharmacy-primary --remote --file=./seed.sql
npx wrangler r2 bucket create pharmacy-archive
npx wrangler deploy
```

## Load balancer

Two tiers, both included:

1. **Network LB** — Cloudflare's anycast edge picks the nearest colo per user; traffic
   to the Worker is automatically load-balanced across Cloudflare's network. No LB
   instance to provision or pay for.
2. **DB LB** — the D1 Sessions API load-balances each *session's* reads across healthy
   replicas (nearest location) while pinning writes to the primary.

## The archive (updates every month)

- `wrangler.jsonc` → `"crons": ["5 0 1 * *"]` — 00:05 UTC on the 1st of every month.
- `src/archive.js` reads **all 17 tables** from the primary (bounded 5,000-row chunks so
  big `sales` tables never hit Worker memory limits), captures the D1 **bookmark** of the
  cut, and writes `archives/pharmacy-YYYY-MM.json` to the R2 `pharmacy-archive` bucket.
- Every run records an audit row in `archive_runs` (period, rows, bytes, status) — and the
  snapshot is written to R2 *before* the audit row, so a failed upload never leaves a
  false audit entry.
- **Retention:** snapshots older than 24 months are deleted on each run. Override by
  changing `RETENTION_MONTHS` in `src/archive.js`.
- Trigger manually anytime: `POST /api/archive/run` (admin), or locally:
  `curl "http://127.0.0.1:8787/__scheduled?cron=5+0+1+*+*"`.

## API surface (Worker)

| Endpoint | Method | Role | What |
|---|---|---|---|
| `/api/login`, `/api/me`, `/api/logout` | POST/GET | public | sessions (admin / pharmacist) |
| `/api/meta` | GET | both | counts, settings, DB + archive info |
| `/api/drugs` | GET | both | stock per medicine (usable / expired / shelf) |
| `/api/batches?drug_id=` | GET | both | FEFO batch list with shelves |
| `/api/counter/lookup?name=&qty=` | GET | both | **shelf + nearest-expiry batch** for a patient request |
| `/api/prescriptions`, `/api/dispense` | POST | both | FEFO issue → stock decrement (sales row recorded) |
| `/api/shelf/tasks`, `/refresh`, `/:id/done` | GET/POST | both | putaway / shift directives |
| `/api/alerts` | GET | both | active alerts |
| `/api/sync/status` | GET | both | replication + bookmark state |
| `/api/archive/run` | POST | admin | run the monthly archive now |

## Local development

```bash
cd cloudflare
npm install
npm run db:init && npm run db:seed       # local D1 (miniflare state)
npx wrangler dev --port 8787 --test-scheduled
# http://127.0.0.1:8787  → SPA + API
# http://127.0.0.1:8787/__scheduled?cron=5+0+1+*+*   → fire the monthly archive
```

Demo accounts: `admin / admin123`, `pharmacist / pharm123` (seeded on first request).

## Live deployment (Oct 2026)

- **URL**: https://pharmacy-api.akshayreddy1451.workers.dev  (D1 `pharmacy-primary`, APAC)
- Schema + seed pushed with `wrangler d1 execute --remote`. SPA served from the same
  Worker via the assets binding (`/static/*` requests are rewritten to root assets).
- Every SPA endpoint is served by the Worker: analytics/overview/charts/activity,
  suppliers, expiry buckets, waste + returns (+ status side-effects), reorder
  suggestions with a computed per-drug `plan` (lead-time demand + 3-day safety),
  uploads/data-quality, users, settings, notifications (+ build-reorder-drafts),
  per-batch FEFO shelf model (rank, days-to-expiry, status), grounded chat, and
  inventory/waste/expiry CSV reports + upload template.

### Known limitations (vs the full Python service)

- **R2 archive is skipped**: bucket creation needs a one-time "Enable R2" in the
  Cloudflare dashboard (API error 10042). `POST /api/archive/run` and the monthly
  cron return `{ok:false, skipped:true}` until the `r2_buckets` block in
  wrangler.jsonc is re-enabled and redeployed.
- **File upload (`POST /api/upload`)** is Python-service-only (Excel parsing +
  quarantine pipeline) — the Worker returns 404 and the UI shows a toast.
- **Forecast model & evaluation** (`/api/forecast`, `/api/forecast/evaluation`,
  `/api/reports/forecast*.csv` payloads) run in the Python service; the Worker
  serves empty forecast data and computes reorder plans from 90-day sales history
  instead. The drug modal's back-test panel renders empty for the same reason.

## Notes & limitations

- Wrangler is pinned to **4.86.0** because the local toolchain runs Node 20; the latest
  wrangler requires Node 22. The `read_replication` config key is accepted by newer
  wrangler versions and by the production API — this pin only affects a local warning.
- The replica binding `DB_B` points at the same database id; after
  `enable-read-replication` in production, reads routed through sessions land on the
  nearest replica copy automatically. No schema or code change is needed.
- D1 read replication is in public beta; sessions/bookmarks are the documented
  consistency mechanism (see Cloudflare docs, "Global read replication").
