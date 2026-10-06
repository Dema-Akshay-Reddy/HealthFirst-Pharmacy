# Smart Pharmacy Inventory Management System

AI-powered pharmacy inventory platform built for the **Zenith 2k25 MedTech HealthTech hackathon**.
It turns daily sales/purchase feeds into decisions: auto-categorised medicines, FEFO SmartShelf
allocation, AI demand forecasting with reorder planning, expiry and low-stock alerts, waste and
return-to-vendor (RTV) workflows, supplier scorecards with a notification outbox, and an
inventory chat copilot.

## Quick start

```bash
pip install -r requirements.txt
python app.py            # serves http://127.0.0.1:8000  (UI at /, API under /api/*)
```

or, with Docker:

```bash
docker compose up --build   # http://localhost:8000, DB persisted in the pharmacy-data volume
```

- **Python**: 3.10+ (developed on 3.12). No external services required — SQLite + WAL is used
  (`data/pharmacy.db`), created and seeded automatically on first start.
- **First run seeds the database** from the bundled Kaggle dataset (`data/zenith/*.json`):
  420 purchase rows, 10,566 valid sales (106 future-dated rows quarantined), 420 batches
  allocated FEFO into expiry buckets, waste ledger auto-populated for expired lots.
- Optional: `OPENAI_API_KEY` + `OPENAI_BASE_URL` (+ `LLM_MODEL` or the Settings page field) enable
  a light LLM polish on chatbot replies; everything works fully offline without it.
- `HOST` / `PORT` env vars override the default `127.0.0.1:8000` binding (the Dockerfile sets
  `HOST=0.0.0.0` so the container is reachable).

### Judge / demo sample files

`samples/` contains ready-made upload files for the **Upload** page:

| File | What it demonstrates |
|---|---|
| `sales_sample.csv` | 12 sales rows with case/hyphen name noise; 3 rows are deliberately invalid (zero qty, unknown medicine, future date) and land in quarantine |
| `purchases_sample.xlsx` | 6 clean purchase rows (one fresh batch per SKU, 2028 expiries) — exercises the Excel path and FEFO allocation |
| `sales_sample_malformed.json` | Concatenated-JSON feed with a truncated final object — the parser salvages the 2 complete rows |

Note: sales for unknown medicines are quarantined with an `unknown_medicine` badge instead of
silently creating new SKUs; purchases may introduce new medicines on purpose.

## Data pipeline

1. **Upload** any of Excel (`.xlsx`), CSV, or JSON on the *Upload* page — sales and purchases are
   auto-detected, columns are alias-matched (`qty`/`quantity`, `batch`/`batch_no`, …), dates and
   numerics are coerced.
2. **Validation & quarantine**: invalid rows (future dates, unknown drugs, non-positive qty,
   missing batch on purchase) never touch live tables — they land in the Quarantine view with the
   reason, and can be fixed and re-uploaded.
3. **Auto-categorisation**: 6 canonical SKUs (Dolo 650, Pan 40, Glycomet 500, Telma 40, Allegra 120,
   Azithral 500) are normalised through case/hyphen/alias noise via rule files (`pharmacy/catalog.py`).
4. **FEFO allocation**: each purchase fills the SmartShelf by batch expiry (First-Expiry-First-Out);
   dispensing serves from the earliest non-expired batch, expired batches block with a
   `blocked_expired` report.
5. **Data quality page**: per-upload row counts, quarantine reasons, date coverage.

## AI / decision modules

- **Forecasting** (`pharmacy/forecasting.py`): per-SKU selection between damped Holt-Winters
  (weekly seasonality, m=7), simple exponential smoothing, seasonal-naive and 28-day moving
  average — chosen by a 56-day back-test with a stability guard (reject fits with wMAPE > 200%).
  Long-horizon paths blend the fitted model with the weekday baseline. Headline accuracy
  ≈ **74.7%** (100 − mean weekly MAPE over the 6 SKUs; per-drug holdout weekly MAPE 18–34%).
  The **Model evaluation** table (Forecast page) re-runs the back-test for every candidate model
  per SKU — wMAPE / weekly MAPE / accuracy / MAE / RMSE vs the model in production — and is
  exportable as `GET /api/reports/forecast-evaluation.csv`.
- **Reorder planner**: ROP = lead-time demand + review-period demand + safety stock (service-level
  Z from Settings) computed from the forecast path; order qty rounded up to pack size. One-click
  draft-PO generation.
- **Alerts** (`pharmacy/alerts.py`): expired stock, expiry-soon windows (30/90 days from Settings),
  low stock, overstock — recomputed from live data, de-duplicated via `dedup_key`, auto-resolved.
- **Waste & RTV** (`pharmacy/shelf.py`): expired/damaged/recalled ledger with value impact,
  return-to-vendor creation grouped by supplier and batch, status workflow
  (`pending → return_requested → returned/credited`).
- **Supplier intelligence** (`pharmacy/suppliers.py`): scorecards (orders, spend, waste %, avg
  shelf-life at receipt) and a notification outbox with draft/send (simulated send — copy into
  your mail client) and mailto handoff.
- **Chat copilot** (`pharmacy/chatbot.py`): deterministic intents — stock, expiry/FEFO, waste,
  sales, forecast, reorder, supplier, returns, catalogue, alert-ack and PO-draft *actions* —
  with KPI/table/action cards. Optional LLM rewrites the wording without touching the numbers.

## Screens (single-page app, hash-routed)

| Route | What it shows | Roles |
|---|---|---|
| login | Sign-in screen — demo accounts **admin / admin123** and **pharmacist / pharm123** (admin can create more users via `POST /api/users`) | both |
| `#dashboard` | KPIs, sales trend, category mix, alerts feed, expiry exposure — KPIs, alert pill and alerts feed auto-refresh every 30 s while the tab is open | admin |
| `#inventory` | Per-SKU stock/valuation/policy, drug detail modal with batch list, dispense | admin |
| `#counter` | **Patient Counter** — patient asks for a medicine → the site names the **shelf** it sits on and the **nearest-expiry batch** (FEFO); one click issues it and stock decrements | pharmacist + admin |
| `#shelftasks` | **Shelf Tasks** — message inbox for the pharmacist: where to **place each new batch** (putaway) and **shelf-to-shelf shifts** (nearest-expiry → pick shelf, expired → quarantine), confirmed with one click | pharmacist + admin |
| `#shelf` | SmartShelf FEFO queue, expiry buckets, one-click RTV | admin |
| `#forecast` | Forecast-vs-actual charts, reorder suggestions, model evaluation table, CSV + draft POs | admin |
| `#alerts` | Severity tabs, acknowledge / acknowledge-all | admin |
| `#waste` | Waste ledger, returns workflow | admin |
| `#suppliers` | Scorecards, notification outbox | admin |
| `#upload` | Drag-drop Excel/CSV/JSON, quarantine, POS day simulator | admin |
| `#chat` | Inventory copilot | admin |
| `#settings` | Service level, review period, pack size, expiry windows, horizon | admin |

**Add Medicine (doctor prescribed)** — the top-bar “＋ Add medicine (Rx)” button (also on `#inventory`)
opens a prescription entry modal: type the medicine exactly as written on the slip (brand or generic
molecule, case/hyphen-insensitive, e.g. “Dolo-650” or “Paracetamol”), enter quantity + doctor/patient,
and the system FEFO-issues it from the earliest-expiry usable batch — usable stock decrements
immediately, expired batches are never issued (reported as `blocked_expired`), shortfalls are flagged.
API: `POST /api/prescriptions`.

## UI design system

The front end is a dependency-free design system (`static/styles.css`, v11 "Pharmly" skin) on design tokens —
dark-forest floating sidebar with lime accents, gray canvas, rounded white cards, pastel semantic palette
(green/amber/red/indigo), focus ring, 4 px spacing, type scale — with shared components: shimmer skeletons,
error panels (`role=alert` + retry), empty states, priority-action and activity feeds, sortable + paginated
tables (`aria-sort`, keyboard-activatable headers), accessible modals (focus trap, Escape, focus restore),
an avatar account menu, pill buttons/chips, and a responsive layout (sidebar becomes a drawer ≤ 960 px).
All interactive states are keyboard-reachable and screen-reader-labelled.

## Users & roles

Two roles guard the site (stateless HMAC session tokens, `X-Session-Token`; a valid
`PHARMACY_API_KEY` machine key acts as an admin service credential):

- **Admin** (`admin / admin123`) — full access: dashboards, inventory, forecasts, demand,
  requirements/reorders, suppliers, waste, uploads, chatbot, settings, user management
  (`POST /api/users` to add more admins/pharmacists).
- **Pharmacist** (`pharmacist / pharm123`) — the operational workflow only:
  1. **Patient Counter** — enters what the patient asked for; the site shows the **shelf**
     (pick face), the **nearest-expiry batch** and days left, and issues FEFO on confirm
     (stock decrements immediately).
  2. **Shelf Tasks** — the website *tells* the pharmacist what to do physically:
     “place new batch X on shelf Y” after every purchase upload, and “shift stock from
     shelf A to B” (nearest-expiry batch → the medicine's pick shelf, expired lots →
     quarantine Q1 for vendor return). Each directive is confirmed once done and the
     shelf map updates.
  3. **SmartShelf** — read-only FEFO queue and expiry buckets.

Physical layout: pick shelves `P1–P3`, reserve `R1–R4`, quarantine `Q1`, returns staging
`RT1`. Every batch carries a `shelf_id`; the nearest-expiry usable batch of each medicine
is always directed to its pick shelf.

## Cloudflare deployment (Wrangler)

A production-style edge deployment of this system lives in [`cloudflare/`](cloudflare/):
**2 real-time-synced databases** (D1 primary + read replica via D1 read replication and the
Sessions API bookmark), **load balancing** (Cloudflare anycast edge + D1 session routing),
and a **monthly archive** (cron `5 0 1 * *` → full snapshot to R2, 24-month retention).
See [cloudflare/README.md](cloudflare/README.md) for the architecture diagram and deploy steps.

## Architecture

```
app.py                 FastAPI app: all /api/* endpoints, lifespan seed + refresh
static/                SPA: index.html, styles.css (teal HealthTech theme), app.js
static/vendor/         Chart.js (vendored, offline-friendly)
pharmacy/
  db.py                SQLite WAL schema + settings store
  catalog.py           drug rules / auto-categorisation
  ingest.py            upload parsing, validation, quarantine, FEFO allocation
  forecasting.py       model selection, back-test, combined predict, reorder plan,
                       evaluate_models() per-model back-test report
  alerts.py            alert rules, dedup, auto-resolve
  shelf.py             FEFO shelf, expiry buckets, waste, RTV
  suppliers.py         scorecards, reorder/return notifications
  chatbot.py           intent engine + cards (+ optional LLM polish)
  analytics.py         overview aggregation
  seed.py              dataset bootstrap
data/
  zenith/              raw Kaggle feed (concatenated JSON objects)
  pharmacy.db          SQLite database
  server.log           last server run log
samples/               demo upload files (CSV, XLSX, malformed JSON)
Dockerfile             container image (seeds itself from data/zenith on first boot)
docker-compose.yml     single service + persistent data volume + healthcheck
```

## Dataset

Kaggle — *Zenith 2k25 MedTech* (srinivaschundi):
<https://www.kaggle.com/datasets/srinivaschundi/zenith-2k25-medtech/data>
Sales cover 2023-01-06 → 2025-11-30; 106 future-dated (2099) sale rows are quarantined by the
validation layer and surfaced on the Data Quality page.

## Demo script (2 minutes)

1. **Dashboard** — KPIs and alert pill straight from the seeded feed.
2. **Patient Counter (pharmacist)** — sign in as `pharmacist/pharm123`, type `Dolo-650`,
   **Find shelf** → “Shelf P1 · batch DOL-… · nearest expiry 8d left”, then **Issue** → stock −N.
3. **Shelf Tasks** — see “place new batch on shelf …” and “shift … to quarantine” messages; hit **Moved ✓**.
4. **Inventory → open a drug (admin)** — batches, valuation, dispense a few units FEFO. Then hit the
   top-bar **“＋ Add medicine (Rx)”**, type `Dolo-650`, qty 5, doctor name — watch usable stock drop.
3. **SmartShelf** — expiry buckets and the expired write-off queue; create an RTV.
4. **Forecast & Reorder** — charts vs actuals; generate draft POs.
5. **Alerts** — acknowledge a critical expired-stock alert.
6. **Upload** — drag `samples/sales_sample.csv` in; watch validation + quarantine (3 bad rows
   badge their reasons). Then upload `samples/purchases_sample.xlsx` to add fresh 2028 stock.
7. **Chat** — ask "what should I reorder this week?", "which batches expire soon",
   "how much waste did we have".
8. **Suppliers** — scorecards and outbox draft → send.

## Requirements coverage

Mapping to the hackathon feature / scenario / edge-case tables:

| Requirement | Where it is satisfied | Evidence |
|---|---|---|
| Demand forecasting (past sales, seasonal patterns, trends) | `pharmacy/forecasting.py` — 4 candidates, 56-day back-test, weekly seasonality, weekday factors | ~74.7% headline accuracy; Model-evaluation table on Forecast page |
| Real-Time alerts (low stock, expiry, reorder) | `pharmacy/alerts.py` — 11 rules incl. expired/expiry-soon/low/stockout/overstock/spike/price/approval/data-quality | 35 active alerts; dashboard pill auto-refreshes every 30 s |
| SmartShelf FEFO + vendor returns | `pharmacy/shelf.py` — FEFO allocation, expired batches block dispensing, RTV workflow | dispense returns `blocked_expired`; returns flow `requested→credited` |
| Waste analytics (expired/damaged/recalled + trends) | Waste page — by-reason doughnut, monthly trend, by-supplier chart, reason badges, CSV | 245 lots / ₹1.76 Cr tracked; manual damaged/recalled entry form |
| Dashboard & reporting | Dashboard — KPIs, expiry timeline, sales trends, alerts feed; 5 CSV reports | expiry report added at `/api/reports/expiry.csv` |
| Chatbot assistance (stock/expiry/vendor + quick reports) | `pharmacy/chatbot.py` — 14 intents, KPI/table/action cards, download links | verified live for stock, expiry, price, substitute, reports |
| Monsoon/seasonal demand scenario | Weekly seasonality (m=7) + weekday profile + trend blending feeds reorder quantities | Forecast page per-SKU cards show `trend_vs_prev` and model |
| Low-stock instant alert + restock amount | `low_stock` / `stockout_risk` alerts carry suggested order qty; live KPI refresh | verified via alert refresh + dashboard poll test |
| Near-expiry batch → “Expiring Soon” + FEFO + return | 30/90-day expiry alerts, FEFO queue, RTV draft action in chat + SmartShelf | 18 `expiry_soon` alerts active |
| Spoilage cause analysis + prevention | Waste `reason` field (expired/damaged/recalled) + Prevention-insights card | card computes dominant cause and corrective steps |
| Slow-moving items for the manager | `overstock` alerts (days of cover) in dashboard feed + Inventory days-of-cover | 6 overstock alerts active |
| Edge: FEFO earliest-expiry + nearing-expiry warning | `fefo_dispense` skips expired; `daysLeftBadge` + expiry alerts warn | verified `blocked_expired` on expired batches |
| Edge: price-surge flag + stock-up qty | `price_rising` alert rule (90d vs prior 90d, ≥8%) with suggested order qty | synthetic +30% test fires correctly; dormant on current flat-price data |
| Edge: transaction spike detection | `demand_spike` alert (7d vs 28-day baseline, ≥1.5×) with stock-adjust advice | fires live: Azithral 500, +60% above baseline |
| Edge: alternative-company substitution | `catalog.substitutes()` + `substitute` chatbot intent (molecule → category → explicit none) | honest “no equivalent stocked” path verified (catalog has unique molecules) |
| Edge: chatbot rejects non-pharmacy queries | Fallback states pharmacy-only scope explicitly | verified with a weather question |
| Edge: low-supply request acknowledged + manager notified | PO creation toast + `approval_request` alert until received/cancelled + outbox draft | synthetic reorder test fires the approval alert |

## Reset / reseed

Delete `data/pharmacy.db*` and restart — the lifespan hook reseeds automatically.

## Production hardening

The same codebase ships with the operational layer expected of a real deployment:

- **Configuration (12-factor)** — every path/switch is env-driven from `pharmacy/config.py`
  (`PHARMACY_DATA_DIR`, `PHARMACY_DB_PATH`, `PHARMACY_DATASET_DIR`, `HOST`/`PORT`,
  `PHARMACY_SEED_ON_START`, `PHARMACY_LOG_LEVEL`, `PHARMACY_ENV`, `PHARMACY_CORS_ORIGINS`).
  See `.env.example`. Dataset (read-only) is deliberately separate from mutable data so a
  mounted volume can never shadow it.
- **Security** — optional API-key gate: set `PHARMACY_API_KEY` and every `/api/*` route requires
  `X-API-Key` (or `Authorization: Bearer`, or `?api_key=` for browser downloads); the SPA prompts
  once and stores the key. Security headers (`X-Content-Type-Options`, `X-Frame-Options`, CSP on
  the SPA, `Cache-Control: no-store` on APIs) on every response; constant-time key comparison;
  `/docs` disabled when `PHARMACY_ENV=production`.
- **Observability** — structured startup logging, per-request log lines with `X-Request-ID`
  propagation, JSON 500s (no tracebacks leaked to clients), liveness `/healthz` and readiness
  `/ready` (DB + dataset checks) for load balancers and orchestrators.
- **Data safety** — WAL + `busy_timeout` + FK enforcement, hot-path indexes, and online backup:
  `POST /api/admin/backup` writes a consistent snapshot to `data/backups/` while serving traffic.
- **Quality gates** — 34 pytest tests (ingest validation/quarantine policy, FEFO dispensing with
  expired-stock blocking, alert engine idempotency, forecast engine + model evaluation, chatbot
  intents and scope, full API surface incl. the auth gate) run against an **isolated temp DB**,
  never the demo data. `ruff` clean. CI in `.github/workflows/ci.yml` (lint + tests on 3.12).
- **Deployment** — multi-layer Dockerfile: dataset baked at `/app/dataset`, non-root user,
  `HEALTHCHECK`, SQLite state in the `/app/data` volume. `docker compose up --build` = one-command
  demo; scale note: SQLite is single-writer, run one app worker (the default) behind a reverse proxy.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address (`PORT=0` safely falls back to 8000) |
| `PHARMACY_ENV` | `development` | `production` disables API docs |
| `PHARMACY_API_KEY` | *(unset = open demo mode)* | Enables the API-key gate |
| `PHARMACY_CORS_ORIGINS` | *(empty)* | Comma-separated allowed origins |
| `PHARMACY_DATA_DIR` / `PHARMACY_DB_PATH` | `data/` / `data/pharmacy.db` | Mutable state location |
| `PHARMACY_DATASET_DIR` | `data/zenith` | Read-only seed inputs |
| `PHARMACY_SEED_ON_START` | `1` | Auto-seed an empty DB at boot |
| `PHARMACY_LOG_LEVEL` | `INFO` (`WARNING` in production) | Logging verbosity |

### Run the checks

```bash
pip install -r requirements-dev.txt
ruff check .          # lint
pytest                # 34 tests, isolated temp DB
```
