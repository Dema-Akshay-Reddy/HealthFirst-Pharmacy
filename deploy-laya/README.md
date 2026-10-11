# Laya prediction service — Render deployment bundle

Deploys the REAL `pharmacy/laya.py` sklearn engine (`data/models/laya.pkl`,
verified with `check_bundle.py`) as a tiny FastAPI service. The Cloudflare
Worker POSTs per-SKU state snapshots from its D1 database and gets back the
same card payloads the local app renders.

## Files
- `laya_service.py` — FastAPI app (`GET /health`, `POST /v1/laya/latest`)
- `pharmacy/` — config.py, db.py, laya.py (engine; only these 4 modules needed)
- `data/models/laya.pkl` — the trained model (7 MB)
- `requirements.txt`, `check_bundle.py`

## Deploy on Render (free web service)
1. Push this folder to a **private GitHub repo** (or use Render's CLI).
2. Render → New → **Web Service** → connect the repo → root directory = this folder.
3. Settings:
   - Runtime: **Python 3** (version is pinned by `.python-version` → 3.12.10;
     also set env var `PYTHON_VERSION=3.12.10` in the dashboard as a belt-and-
     braces. Render's default 3.14 has no cp314 wheels for numpy 2.2.1 /
     scikit-learn 1.6.1, so pip would compile them from source — that is the
     multi-minute build seen in the logs)
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn laya_service:app --host 0.0.0.0 --port $PORT`
   - Instance type: **Free**
4. Environment → add secret: **LAYA_SHARED_KEY** = a long random string
   (e.g. `openssl rand -hex 32`). Never commit it.
5. Deploy. Note the service URL: `https://<app-name>.onrender.com`.
6. Then on Cloudflare (in the `cloudflare/` folder):
   - `npx wrangler secret put LAYA_SHARED_KEY` → paste the same key
   - `npx wrangler secret put LAYA_SERVICE_URL` → `https://<app-name>.onrender.com`
7. Open https://pharmacy-api.akshayreddy1451.workers.dev/#forecast — the Laya
   outlook now computes real predictions per SKU.

## Notes
- Free tier sleeps after 15 min idle; first forecast load takes ~1 min to
  wake. All other site functions are unaffected.
- Prediction integrity is preserved: failed SKUs come back as `error` rows,
  never fabricated predictions.
