"""Smart Pharmacy Inventory Management System — API + SPA server.

Run:  python app.py          -> http://127.0.0.1:8000
"""
import csv
import hmac
import io
import json
import logging
import random
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pharmacy import (alerts, analytics, auth, catalog, chatbot, config, db, forecasting,
                      ingest, seed, shelf, shelfops, suppliers)

BASE = Path(__file__).resolve().parent

# Windows consoles default to cp1252 and would crash on unicode output
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

config.setup_logging()
log = logging.getLogger("pharmacy")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    if config.SEED_ON_START and not seed.already_seeded():
        log.info("Seeding database from the bundled dataset ...")
        seed.run(quiet=False)
    alerts.refresh()
    forecasting.ensure_forecasts()
    auth.ensure_users()
    shelfops.ensure_shelves()
    shelfops.assign_initial_shelves()  # backfill shelves on legacy databases
    shelfops.refresh_tasks()
    log.info("Startup complete: env=%s db=%s auth=%s", config.ENV, config.DB_PATH,
             "on" if config.current_api_key() else "off")
    yield


app = FastAPI(
    title="Smart Pharmacy Inventory Management System",
    version="1.1.0",
    lifespan=lifespan,
    # interactive API docs stay on in development; disabled in production
    docs_url="/docs" if config.DOCS_ENABLED else None,
    redoc_url=None,
    openapi_url="/openapi.json" if config.DOCS_ENABLED else None,
)

# Middleware: added innermost-first, so request execution order is
# CORS -> logging -> auth -> security-headers -> gzip -> route.
app.add_middleware(GZipMiddleware, minimum_size=1024)
if config.CORS_ORIGINS:
    app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS,
                       allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    h = response.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith("/api"):
        h.setdefault("Cache-Control", "no-store")
    elif request.url.path == "/":
        # the SPA has no inline <script>, so a strict-ish CSP is safe
        h.setdefault("Content-Security-Policy",
                     "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                     "connect-src 'self'; font-src 'self'")
    return response


@app.middleware("http")
async def api_auth(request: Request, call_next):
    """Optional API-key gate — set PHARMACY_API_KEY to protect every /api route.

    Accepts `X-API-Key`, `Authorization: Bearer`, or `?api_key=` (the last one
    exists so browser download links for CSV reports keep working).
    """
    key = config.current_api_key()
    if key and request.url.path.startswith("/api"):
        auth = request.headers.get("Authorization") or ""
        supplied = (request.headers.get("X-API-Key")
                    or request.query_params.get("api_key")
                    or (auth[7:] if auth.startswith("Bearer ") else ""))
        if not hmac.compare_digest(supplied, key):
            return JSONResponse({"detail": "Unauthorized: valid X-API-Key required"},
                                status_code=401, headers={"WWW-Authenticate": "ApiKey"})
    return await call_next(request)


@app.middleware("http")
async def request_logging(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("%s %s FAILED rid=%s", request.method, request.url.path, rid)
        raise
    response.headers["X-Request-ID"] = rid
    path = request.url.path
    if not path.startswith("/static") and path != "/favicon.ico":
        log.info("%s %s -> %s %.1fms rid=%s", request.method, path,
                 response.status_code, (time.perf_counter() - t0) * 1000, rid)
    return response


# --------------------------------------------------------------------------- #
# login & roles: admin (everything) vs pharmacist (counter + shelf workflow)
# --------------------------------------------------------------------------- #
PUBLIC_API = {"/api/login", "/api/me", "/api/logout"}

# (method, path-prefix) pairs a pharmacist may call; admin may call everything.
PHARMACIST_RULES = (
    ("GET", "/api/meta"), ("GET", "/api/drugs"), ("GET", "/api/batches"),
    ("GET", "/api/expiry"), ("GET", "/api/alerts"), ("GET", "/api/shelf"),
    ("GET", "/api/counter"), ("POST", "/api/shelf"), ("POST", "/api/counter"),
    ("POST", "/api/prescriptions"), ("POST", "/api/dispense"), ("POST", "/api/alerts"),
)


def session_from_request(request: Request) -> dict | None:
    token = (request.headers.get("X-Session-Token")
             or request.query_params.get("token") or "")
    return auth.parse_token(token)


@app.middleware("http")
async def role_auth(request: Request, call_next):
    """Session login with two roles. `admin` sees every screen and dataset;
    `pharmacist` gets the counter (patient asks for medicine -> shelf + nearest
    expiry) and shelf workflow (putaway / shift directives). A valid machine
    API key (PHARMACY_API_KEY) acts as an admin service credential."""
    path = request.url.path
    if not path.startswith("/api") or path in PUBLIC_API:
        return await call_next(request)
    key = config.current_api_key()
    if key:
        hdr = request.headers.get("Authorization") or ""
        supplied = (request.headers.get("X-API-Key")
                    or request.query_params.get("api_key")
                    or (hdr[7:] if hdr.startswith("Bearer ") else ""))
        if supplied and hmac.compare_digest(supplied, key):
            return await call_next(request)
    user = session_from_request(request)
    if not user:
        return JSONResponse({"detail": "login required"}, status_code=401,
                            headers={"WWW-Authenticate": "Session"})
    if user.get("r") != "admin":
        if not any(request.method.upper() == m and path.startswith(p)
                   for m, p in PHARMACIST_RULES):
            return JSONResponse({"detail": "admin access required for this area"},
                                status_code=403)
    request.state.user = user
    return await call_next(request)


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"detail": "Internal server error"}, status_code=500)


# --------------------------------------------------------------------------- #
# health probes
# --------------------------------------------------------------------------- #
@app.get("/healthz")
def healthz():
    """Liveness: process is up (no data checks)."""
    return {"status": "ok"}


@app.get("/ready")
def ready():
    """Readiness: DB reachable and seed inputs present."""
    checks = {}
    try:
        db.scalar("SELECT COUNT(*) FROM sales")
        checks["database"] = "ok"
    except Exception as exc:  # pragma: no cover
        checks["database"] = f"error: {exc}"
    checks["dataset"] = "ok" if db.DATASET_DIR.exists() else "missing"
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse({"status": "ready" if ok else "degraded", "checks": checks},
                        status_code=200 if ok else 503)


# --------------------------------------------------------------------------- #
# login & users
# --------------------------------------------------------------------------- #
class LoginBody(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: LoginBody):
    """Two demo roles: admin/admin123 (full access), pharmacist/pharm123 (counter
    + shelf workflow). Returns an HMAC session token (X-Session-Token)."""
    user = auth.login(body.username, body.password)
    if not user:
        raise HTTPException(401, "invalid username or password")
    return user


@app.get("/api/me")
def me(request: Request):
    data = session_from_request(request)
    if not data:
        raise HTTPException(401, "not logged in")
    row = db.one("SELECT username, role, name FROM users WHERE username=?", (data.get("u"),))
    return row or dict(username=data.get("u"), role=data.get("r"), name=data.get("u"))


@app.post("/api/logout")
def logout():
    return dict(ok=True)  # tokens are stateless — the client simply drops it


class UserBody(BaseModel):
    username: str
    password: str
    role: str = "pharmacist"
    name: str = ""


@app.get("/api/users")
def list_users():
    return db.query("SELECT id, username, role, name, created_at FROM users ORDER BY id")


@app.post("/api/users")
def add_user(body: UserBody):
    uname = (body.username or "").strip().lower()
    if not uname:
        raise HTTPException(400, "username required")
    if body.role not in ("admin", "pharmacist"):
        raise HTTPException(400, "role must be admin or pharmacist")
    if len(body.password or "") < 6:
        raise HTTPException(400, "password must be at least 6 characters")
    if db.one("SELECT id FROM users WHERE username=?", (uname,)):
        raise HTTPException(409, "username already exists")
    uid = auth.create_user(uname, body.password, body.role, body.name)
    return dict(id=uid, username=uname, role=body.role)


# --------------------------------------------------------------------------- #
# dashboard
# --------------------------------------------------------------------------- #
@app.get("/api/meta")
def meta():
    s = db.settings()
    return dict(
        pharmacy=s.get("pharmacy_name"), as_of=date.today().isoformat(),
        settings=s,
        dataset=dict(name="Zenith 2k25 MedTech (Kaggle)",
                     url="https://www.kaggle.com/datasets/srinivaschundi/zenith-2k25-medtech"),
        source_counts=dict(
            sales=db.scalar("SELECT COUNT(*) FROM sales"),
            purchases=db.scalar("SELECT COUNT(*) FROM purchases"),
            batches=db.scalar("SELECT COUNT(*) FROM batches"),
        ),
    )


@app.get("/api/overview")
def overview():
    return analytics.overview()


@app.get("/api/charts")
def charts():
    return dict(
        monthly=analytics.sales_monthly(18),
        categories=analytics.category_breakdown(),
        category_sales=analytics.category_of_sales(),
        top_drugs=analytics.top_drugs(8),
        expiry_timeline=analytics.expiry_timeline(),
        supplier_spend=analytics.supplier_spend(),
        sales_window_90=analytics.db.scalar("SELECT MAX(date) FROM sales"),
        forecast_accuracy=[
            dict(drug=p["drug"], wmape=p.get("metrics", {}).get("wmape"),
                 model=p.get("model"), baseline=p.get("baseline_wmape"))
            for p in forecasting.current_forecasts()
        ],
    )


@app.get("/api/forecast-vs-actual")
def forecast_vs_actual(drug_id: int = Query(...), days: int = 56):
    return analytics.forecast_vs_actual(drug_id, days)


@app.get("/api/sales")
def sales(drug_id: int | None = None, days: int = 90):
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    if drug_id:
        return db.query("SELECT date, qty, revenue FROM sales_daily WHERE drug_id=? AND date>=? "
                        "ORDER BY date", (drug_id, cutoff))
    return db.query("SELECT date, SUM(qty) AS qty, SUM(revenue) AS revenue FROM sales_daily "
                    "WHERE date>=? GROUP BY date ORDER BY date", (cutoff,))


# --------------------------------------------------------------------------- #
# inventory / SmartShelf
# --------------------------------------------------------------------------- #
def _plan_for(drug: dict) -> dict:
    return alerts.reorder_plan_for(drug)


@app.get("/api/drugs")
def drugs():
    rows = []
    for d in db.query("SELECT * FROM drugs ORDER BY category, name"):
        avail = forecasting.available_stock(d["id"])
        rows.append(dict(
            id=d["id"], name=d["name"], generic=d["generic"], category=d["category"],
            form=d["form"], schedule=d["schedule"], storage=d["storage"],
            mrp=d["mrp"], cost=d["cost"], lead_time_days=d["lead_time_days"],
            reorder_point=d["reorder_point"], supplier_id=d["supplier_id"],
            usable=avail["usable"], expired=avail["expired"],
            usable_value=round(avail["usable_value"], 2),
            expired_value=round(avail["expired_value"], 2),
            batches=avail["batches"], plan=_plan_for(d),
        ))
    return rows


class DrugUpdate(BaseModel):
    lead_time_days: int | None = None
    reorder_point: float | None = None
    supplier_id: int | None = None


@app.patch("/api/drugs/{drug_id}")
def update_drug(drug_id: int, body: DrugUpdate):
    d = db.one("SELECT * FROM drugs WHERE id=?", (drug_id,))
    if not d:
        raise HTTPException(404, "drug not found")
    if body.lead_time_days is not None:
        db.execute("UPDATE drugs SET lead_time_days=? WHERE id=?", (body.lead_time_days, drug_id))
    if body.reorder_point is not None:
        db.execute("UPDATE drugs SET reorder_point=? WHERE id=?", (body.reorder_point, drug_id))
    if body.supplier_id is not None:
        db.execute("UPDATE drugs SET supplier_id=? WHERE id=?", (body.supplier_id, drug_id))
    alerts.refresh()
    return _plan_for(db.one("SELECT * FROM drugs WHERE id=?", (drug_id,)))


@app.get("/api/batches")
def batches(drug_id: int | None = None, status: str | None = None,
            include_empty: bool = False, limit: int = 500):
    rows = shelf.shelf(drug_id, include_empty=include_empty)
    if status:
        rows = [r for r in rows if r["status"] == status]
    return rows[:limit]


class DispenseBody(BaseModel):
    drug_id: int
    qty: int
    note: str = ""


@app.post("/api/dispense")
def dispense(body: DispenseBody):
    if body.qty <= 0:
        raise HTTPException(400, "qty must be positive")
    result = shelf.fefo_dispense(body.drug_id, body.qty, body.note)
    forecasting.ensure_forecasts()
    alerts.refresh()
    return result


class PrescriptionBody(BaseModel):
    name: str = ""          # medicine exactly as written on the prescription
    drug_id: int | None = None
    qty: int
    doctor: str = ""
    patient: str = ""
    note: str = ""


@app.post("/api/prescriptions")
def add_prescription(body: PrescriptionBody):
    """Add Medicine (doctor prescribed): resolve the medicine as written on the
    prescription slip (brand or generic, case/hyphen-insensitive) and FEFO-issue
    it — usable stock decrements immediately; expired batches are never issued."""
    if body.qty <= 0:
        raise HTTPException(400, "qty must be positive")
    drug = None
    if body.drug_id is not None:
        drug = db.one("SELECT * FROM drugs WHERE id=?", (body.drug_id,))
    else:
        norm = catalog.normalise_name(body.name)
        if norm:
            drug = db.one("SELECT * FROM drugs WHERE norm_name=?", (norm,))
            if not drug:  # generic molecule on the slip -> stocked brand (e.g. Paracetamol -> Dolo 650)
                drug = db.one(
                    "SELECT * FROM drugs WHERE LOWER(COALESCE(generic,''))=? ORDER BY id LIMIT 1",
                    (norm,))
    if not drug:
        label = body.name.strip() or (str(body.drug_id) if body.drug_id is not None else "(empty)")
        raise HTTPException(404, f"medicine not found in catalogue: {label}")
    bits = []
    if body.doctor.strip():
        bits.append(f"prescribed by {body.doctor.strip()}")
    if body.patient.strip():
        bits.append(f"patient {body.patient.strip()}")
    if body.note.strip():
        bits.append(body.note.strip())
    before = int(forecasting.available_stock(drug["id"])["usable"])
    result = shelf.fefo_dispense(drug["id"], body.qty, "; ".join(bits))
    after = int(forecasting.available_stock(drug["id"])["usable"])
    forecasting.ensure_forecasts()
    alerts.refresh()
    return dict(drug_id=drug["id"], drug=drug["name"],
                requested=result["requested"], dispensed=result["dispensed"],
                batches=result["batches"], shortfall=result["shortfall"],
                blocked_expired=result["blocked_expired"],
                usable_before=before, usable_after=after, at=result["at"])


# --------------------------------------------------------------------------- #
# physical shelves: putaway + system-directed shifts (pharmacist workflow)
# --------------------------------------------------------------------------- #
@app.get("/api/shelves")
def shelves():
    return db.query("SELECT * FROM shelves ORDER BY id")


@app.get("/api/shelf/tasks")
def shelf_tasks(status: str | None = "pending", limit: int = 200):
    return dict(items=shelfops.list_tasks(status, limit), counts=shelfops.task_counts())


@app.post("/api/shelf/tasks/refresh")
def shelf_tasks_refresh():
    shelfops.ensure_shelves()
    return shelfops.refresh_tasks()


@app.post("/api/shelf/tasks/{task_id}/done")
def shelf_task_done(task_id: int):
    task = shelfops.complete_task(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    shelfops.refresh_tasks()
    return dict(ok=True, task=task)


@app.get("/api/counter/lookup")
def counter_lookup(name: str, qty: int = 1):
    """Patient asked for a medicine: which shelf is it on, and which batch is
    nearest expiry? (FEFO pick recommendation, nothing issued yet.)"""
    norm = catalog.normalise_name(name)
    if not norm:
        raise HTTPException(400, "medicine name required")
    drug = (db.one("SELECT * FROM drugs WHERE norm_name=?", (norm,))
            or db.one("SELECT * FROM drugs WHERE LOWER(COALESCE(generic,''))=? "
                      "ORDER BY id LIMIT 1", (norm,)))
    if not drug:
        raise HTTPException(404, f"medicine not found in catalogue: {name}")
    stock = forecasting.available_stock(drug["id"])
    b = shelf.next_fefo_batch(drug["id"])
    pick = None
    if b:
        pick = dict(batch_id=b["id"], batch_no=b["batch_no"], expiry_date=b["expiry_date"],
                    days_to_expiry=b.get("days_to_expiry"), qty_remaining=b["qty_remaining"],
                    shelf_code=b.get("shelf_code"), shelf_zone=b.get("shelf_zone"))
    usable = int(stock["usable"])
    return dict(drug_id=drug["id"], drug=drug["name"], generic=drug["generic"],
                category=drug["category"], usable=usable, expired=int(stock["expired"]),
                pick=pick, requested_qty=qty,
                enough=bool(pick and pick["qty_remaining"] >= qty and usable >= qty))


# --------------------------------------------------------------------------- #
# alerts
# --------------------------------------------------------------------------- #
@app.get("/api/alerts")
def list_alerts(status: str | None = None, severity: str | None = None,
                atype: str | None = None):
    items = alerts.list_alerts(status, severity)
    if atype:
        items = [a for a in items if a["atype"] == atype]
    return dict(items=items, summary=alerts.summary())


@app.post("/api/alerts/refresh")
def refresh_alerts():
    items = alerts.refresh()
    return dict(items=items, summary=alerts.summary())


@app.post("/api/alerts/{alert_id}/ack")
def ack_alert(alert_id: int):
    alerts.acknowledge(alert_id)
    return dict(ok=True)


@app.post("/api/alerts/ack-all")
def ack_all():
    for r in alerts.list_alerts(status="active", limit=1000):
        alerts.acknowledge(r["id"])
    return dict(ok=True, summary=alerts.summary())


# --------------------------------------------------------------------------- #
# forecasting / reorders
# --------------------------------------------------------------------------- #
@app.get("/api/forecast")
def forecast(recompute: bool = False):
    if recompute:
        return forecasting.forecast_all()
    rows = forecasting.current_forecasts()
    if not rows:
        rows = forecasting.forecast_all()
    return rows


@app.post("/api/forecast/{drug_id}")
def reforecast(drug_id: int):
    return forecasting.forecast_drug(drug_id)


@app.get("/api/forecast/evaluation")
def forecast_evaluation(holdout: int = Query(56, ge=14, le=180)):
    """Back-test every candidate model on every SKU (model-selection audit)."""
    return forecasting.evaluate_all(holdout=holdout)


@app.get("/api/forecast/evaluation/{drug_id}")
def forecast_evaluation_one(drug_id: int, holdout: int = Query(56, ge=14, le=180)):
    return forecasting.evaluate_models(drug_id, holdout=holdout)


@app.get("/api/reports/forecast-evaluation.csv")
def forecast_evaluation_csv():
    rows = []
    for per in forecasting.evaluate_all():
        for m in per["models"]:
            rows.append(dict(
                drug=per["drug"], model=m["model"], rank=m["rank"],
                history_days=per["history_days"], holdout_days=per["holdout_days"],
                wmape=m["wmape"], weekly_mape=m["mape"], accuracy_1_minus_weekly_mape=m["accuracy"],
                mae=m["mae"], rmse=m["rmse"], fit_wmape=m["fit_wmape"],
                in_production="yes" if m["in_production"] else "no",
                params=" ".join("" if p is None else f"{p:g}" for p in m["params"]),
            ))
    return _csv(rows, "forecast_evaluation.csv")


class ReorderBody(BaseModel):
    drug_id: int
    qty: int | None = None
    due_date: str | None = None
    notes: str | None = None


@app.get("/api/reorders")
def reorders():
    history = db.query(
        "SELECT r.*, d.name AS drug, s.name AS supplier FROM reorders r "
        "JOIN drugs d ON d.id=r.drug_id LEFT JOIN suppliers s ON s.id=r.supplier_id "
        "ORDER BY r.id DESC")
    suggestions = []
    for p in forecasting.current_forecasts():
        plan = p.get("reorder", {})
        if not plan:
            continue
        d = db.one("SELECT name FROM suppliers WHERE id=(SELECT supplier_id FROM drugs WHERE id=?)",
                   (p["drug_id"],))
        suggestions.append(dict(
            drug_id=p["drug_id"], drug=p["drug"], supplier=d["name"] if d else None,
            available=plan.get("available"), demand=plan.get("demand_lead_time"),
            safety=plan.get("safety_stock"), reorder_point=plan.get("reorder_point"),
            order_qty=plan.get("order_qty"), status=plan.get("status"),
            due_date=plan.get("due_date"), cover_days=plan.get("cover_days"),
            avg_daily=p.get("avg_daily"), lead_time_days=plan.get("lead_time_days"),
        ))
    suggestions.sort(key=lambda s: (s["status"] != "order_now", s.get("due_date") or "9999"))
    return dict(history=history, suggestions=suggestions)


@app.post("/api/reorders")
def create_reorder(body: ReorderBody):
    d = db.one("SELECT * FROM drugs WHERE id=?", (body.drug_id,))
    if not d:
        raise HTTPException(404, "drug not found")
    plan = _plan_for(d)
    qty = body.qty or plan.get("order_qty") or 0
    rid = db.execute(
        "INSERT INTO reorders(drug_id, supplier_id, qty, due_date, reason, status, source, notes, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (d["id"], d["supplier_id"], qty, body.due_date or plan.get("due_date"),
         f"AI suggestion ({plan.get('status')})", "ordered", "manual", body.notes,
         db.now_iso(), db.now_iso()))
    notification = None
    supplier = db.one("SELECT * FROM suppliers WHERE id=?", (d["supplier_id"],))
    if supplier:
        subject, text = suppliers.reorder_email(d, plan, supplier)
        notification = suppliers.create_notification(supplier["id"], subject, text,
                                                     "reorder", rid, status="draft")
    return dict(id=rid, qty=qty, notification=notification)


class StatusBody(BaseModel):
    status: str


@app.patch("/api/reorders/{reorder_id}")
def reorder_status(reorder_id: int, body: StatusBody):
    if body.status not in ("suggested", "ordered", "received", "cancelled"):
        raise HTTPException(400, "invalid status")
    db.execute("UPDATE reorders SET status=?, updated_at=? WHERE id=?",
               (body.status, db.now_iso(), reorder_id))
    return dict(ok=True, status=body.status)


# --------------------------------------------------------------------------- #
# expiry / FEFO / waste / returns
# --------------------------------------------------------------------------- #
@app.get("/api/expiry")
def expiry():
    b = shelf.expiry_buckets()
    return dict(buckets=b["buckets"], per_drug=b["per_drug"], shelf=shelf.shelf()[:400])


class WasteBody(BaseModel):
    drug_id: int
    batch_no: str | None = None
    qty: int
    reason: str = "damaged"
    note: str = ""


@app.get("/api/waste")
def waste():
    shelf.sync_expired_waste()
    return dict(summary=shelf.waste_summary(), items=shelf.waste_list()[:200],
                suppliers=suppliers.supplier_stats())


@app.post("/api/waste")
def add_waste(body: WasteBody):
    try:
        result = shelf.add_waste(body.drug_id, body.batch_no, body.qty, body.reason, body.note)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    forecasting.ensure_forecasts()
    alerts.refresh()
    return result


class ReturnBody(BaseModel):
    supplier_id: int
    waste_ids: list[int] | None = None
    note: str = ""


@app.get("/api/returns")
def returns():
    return shelf.list_returns()


@app.post("/api/returns")
def create_return(body: ReturnBody):
    try:
        return shelf.create_return(body.supplier_id, body.waste_ids, body.note)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class ReturnStatusBody(BaseModel):
    status: str


@app.patch("/api/returns/{return_id}")
def return_status(return_id: int, body: ReturnStatusBody):
    try:
        return shelf.update_return_status(return_id, body.status)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# --------------------------------------------------------------------------- #
# suppliers / notifications
# --------------------------------------------------------------------------- #
@app.get("/api/suppliers")
def supplier_list():
    return dict(items=suppliers.supplier_stats(), notifications=suppliers.list_notifications())


class NotifyBody(BaseModel):
    supplier_id: int
    subject: str
    body: str
    send: bool = False


@app.post("/api/notifications")
def create_notification(body: NotifyBody):
    n = suppliers.create_notification(body.supplier_id, body.subject, body.body)
    return suppliers.mark_sent(n["id"]) if body.send else n


class NotifyStatusBody(BaseModel):
    status: str


@app.patch("/api/notifications/{nid}")
def notification_status(nid: int, body: NotifyStatusBody):
    if body.status == "sent":
        return suppliers.mark_sent(nid)
    if body.status in ("draft", "queued", "cancelled"):
        db.execute("UPDATE notifications SET status=? WHERE id=?", (body.status, nid))
        return suppliers.get(nid)
    raise HTTPException(400, "invalid status")


@app.post("/api/notifications/build-reorder-drafts")
def build_drafts():
    created = suppliers.build_reorder_notifications()
    return dict(created=len(created), items=created)


# --------------------------------------------------------------------------- #
# uploads / data quality
# --------------------------------------------------------------------------- #
@app.post("/api/upload")
async def upload(file: UploadFile = File(...), kind: str = Form("auto")):
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty file")
    try:
        summary = ingest.ingest(raw, file.filename or "upload",
                                kind_hint=None if kind == "auto" else kind)
    except Exception as exc:
        raise HTTPException(400, f"Could not process file: {exc}") from exc
    forecasting.ensure_forecasts()
    alerts.refresh()
    return summary


@app.get("/api/upload/template")
def upload_template():
    import pandas as pd

    sales = pd.DataFrame([
        dict(Transaction_ID="TXN-90001", Date=date.today().isoformat(), Drug_Name="Dolo 650",
             Batch_Number="DOL-2301-95", Qty_Sold=4, MRP_Unit_Price=30.0, Total_Amount=120.0),
        dict(Transaction_ID="TXN-90002", Date=date.today().isoformat(), Drug_Name="Pan 40",
             Batch_Number="PAN-2301-58", Qty_Sold=2, MRP_Unit_Price=150.0, Total_Amount=300.0),
    ])
    purchases = pd.DataFrame([
        dict(Purchase_ID="PO-9001", Date_Received=date.today().isoformat(), Drug_Name="Dolo 650",
             Supplier_Name="Apollo Supply Chain", Batch_Number="DOL-2601-11", Qty_Received=500,
             Unit_Cost_Price=23.0, Total_Purchase_Cost=11500.0,
             Expiry_Date=(date.today() + timedelta(days=540)).isoformat()),
    ])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        sales.to_excel(writer, sheet_name="sales", index=False)
        purchases.to_excel(writer, sheet_name="purchases", index=False)
    return Response(
        content=buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=pharmacy_upload_template.xlsx"})


@app.get("/api/uploads")
def uploads():
    rows = db.query("SELECT * FROM uploads ORDER BY id DESC LIMIT 50")
    for r in rows:
        r["issues"] = db.jload(r["issues"], {})
    return rows


@app.get("/api/data-quality")
def data_quality(limit: int = 100):
    rows = db.query("SELECT * FROM quarantined ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        r["record"] = db.jload(r["record"], {})
        r["issues"] = db.jload(r["issues"], [])
    stats = db.query("SELECT kind, COUNT(*) AS n FROM quarantined GROUP BY kind")
    issue_counts: dict[str, int] = {}
    for r in db.query("SELECT issues FROM quarantined LIMIT 2000"):
        for issue in db.jload(r["issues"], []) or []:
            issue_counts[issue] = issue_counts.get(issue, 0) + 1
    uploads_rows = db.query("SELECT id, filename, kind, rows_total, rows_accepted, rows_rejected, "
                            "created_at FROM uploads ORDER BY id DESC LIMIT 20")
    return dict(rows=rows, stats=stats, issues=issue_counts, uploads=uploads_rows)


# --------------------------------------------------------------------------- #
# chatbot
# --------------------------------------------------------------------------- #
class ChatBody(BaseModel):
    message: str
    use_llm: bool = True


@app.post("/api/chat")
def chat(body: ChatBody):
    if not body.message.strip():
        raise HTTPException(400, "message required")
    return chatbot.respond(body.message, use_llm=body.use_llm)


@app.get("/api/chat/history")
def chat_history(limit: int = 40):
    return chatbot.history(limit)


# --------------------------------------------------------------------------- #
# reports (CSV export)
# --------------------------------------------------------------------------- #
def _csv(rows: list[dict], filename: str) -> Response:
    if not rows:
        rows = [{"info": "no data"}]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.get("/api/reports/waste.csv")
def waste_report():
    rows = shelf.waste_list()
    return _csv([{k: r.get(k) for k in ("id", "drug", "batch_no", "expiry_date", "qty",
                                        "reason", "unit_cost", "value", "status",
                                        "supplier", "note", "created_at")} for r in rows],
                "waste_report.csv")


@app.get("/api/reports/inventory.csv")
def inventory_report():
    rows = []
    for d in db.query("SELECT * FROM drugs ORDER BY name"):
        avail = forecasting.available_stock(d["id"])
        plan = _plan_for(d)
        rows.append(dict(medicine=d["name"], generic=d["generic"], category=d["category"],
                         form=d["form"], usable_stock=avail["usable"],
                         expired_stock=avail["expired"], stock_value=round(avail["usable_value"], 2),
                         mrp=d["mrp"], days_of_cover=plan.get("cover_days"),
                         reorder_point=plan.get("reorder_point"),
                         suggested_order_qty=plan.get("order_qty"),
                         status=plan.get("status"), lead_time_days=d["lead_time_days"]))
    return _csv(rows, "inventory_report.csv")


@app.get("/api/reports/forecast.csv")
def forecast_report():
    rows = []
    for p in forecasting.current_forecasts():
        plan = p.get("reorder", {})
        rows.append(dict(medicine=p["drug"], model=p.get("model"),
                         avg_daily_units=p.get("avg_daily"),
                         forecast_30d=round(sum(d["qty"] for d in p.get("daily", [])), 1),
                         wmape_pct=p.get("metrics", {}).get("wmape"),
                         baseline=p.get("baseline_model"),
                         available=plan.get("available"), reorder_point=plan.get("reorder_point"),
                         suggested_order=plan.get("order_qty"), status=plan.get("status"),
                         due_date=plan.get("due_date")))
    return _csv(rows, "forecast_report.csv")


@app.get("/api/reports/expiry.csv")
def expiry_report():
    rows = db.query(
        "SELECT d.name AS medicine, d.generic, b.batch_no, s.name AS supplier, "
        "b.expiry_date, "
        "CAST(julianday(b.expiry_date)-julianday(date('now')) AS INTEGER) AS days_left, "
        "b.qty_remaining AS units, "
        "ROUND(b.qty_remaining*b.unit_cost, 2) AS value_at_cost, "
        "CASE WHEN b.expiry_date < date('now') THEN 'expired' "
        "WHEN b.expiry_date <= date('now','+30 day') THEN 'critical' "
        "WHEN b.expiry_date <= date('now','+90 day') THEN 'warning' "
        "ELSE 'future' END AS bucket "
        "FROM batches b JOIN drugs d ON d.id=b.drug_id "
        "LEFT JOIN suppliers s ON s.id=b.supplier_id "
        "WHERE b.qty_remaining>0 AND b.expiry_date IS NOT NULL "
        "ORDER BY b.expiry_date"
    )
    return _csv(rows, "expiry_report.csv")


# --------------------------------------------------------------------------- #
# settings + simulator
# --------------------------------------------------------------------------- #
class SettingsBody(BaseModel):
    values: dict


@app.post("/api/settings")
def update_settings(body: SettingsBody):
    allowed = set(db.DEFAULT_SETTINGS)
    for k, v in body.values.items():
        if k in allowed:
            db.set_setting(k, v)
    alerts.refresh()
    return db.settings()


@app.post("/api/admin/backup")
def admin_backup():
    """Online SQLite backup into data/backups (safe while serving traffic)."""
    dest = db.backup_to()
    return dict(path=str(dest), size_bytes=dest.stat().st_size, created_at=db.now_iso())


@app.post("/api/simulate/day")
def simulate_day(days: int = 1, seed_value: int | None = None):
    """Push one simulated POS day through the normal ingestion pipeline."""
    if days < 1 or days > 30:
        raise HTTPException(400, "days must be 1..30")
    rng = random.Random(seed_value)
    start = date.today() - timedelta(days=days - 1)
    rows = []
    forecasts = forecasting.current_forecasts()
    idx = 0
    for _ in range(days):
        for p in forecasts:
            daily = {d["date"]: d for d in p.get("daily", [])}
            day = (start + timedelta(days=_)).isoformat()
            qty = daily.get(day, {}).get("qty", p.get("avg_daily", 5))
            units = max(0, int(round(qty * rng.uniform(0.6, 1.4))))
            if units <= 0:
                continue
            drug = db.one("SELECT * FROM drugs WHERE id=?", (p["drug_id"],))
            batches = db.query(
                "SELECT batch_no FROM batches WHERE drug_id=? AND qty_remaining>0 "
                "ORDER BY expiry_date LIMIT 1", (p["drug_id"],))
            batch_no = batches[0]["batch_no"] if batches else None
            price = drug["mrp"] or 30
            rows.append(dict(Transaction_ID=f"SIM-{day}-{p['drug_id']}-{idx}", Date=day,
                             Drug_Name=p["drug"], Batch_Number=batch_no, Qty_Sold=units,
                             MRP_Unit_Price=price, Total_Amount=round(units * price, 2)))
            idx += 1
    if not rows:
        return dict(inserted=0, note="nothing to simulate")
    summary = ingest.ingest(json.dumps(rows).encode(), f"simulated_pos_{days}d.json",
                            kind_hint="sales", source="pos_sim")
    forecasting.ensure_forecasts()
    alerts.refresh()
    summary["note"] = "simulated POS feed (demo data, source=pos_sim)"
    return summary


@app.get("/api/activity")
def activity(limit: int = 25):
    return dict(
        uploads=db.query("SELECT id, filename, kind, rows_total, rows_accepted, rows_rejected, "
                         "created_at FROM uploads ORDER BY id DESC LIMIT ?", (limit,)),
        reorders=db.query("SELECT r.id, r.qty, r.status, r.created_at, d.name AS drug "
                          "FROM reorders r JOIN drugs d ON d.id=r.drug_id ORDER BY r.id DESC LIMIT ?", (limit,)),
        waste=db.query("SELECT w.id, w.qty, w.value, w.reason, w.status, w.created_at, d.name AS drug "
                       "FROM waste w JOIN drugs d ON d.id=w.drug_id ORDER BY w.id DESC LIMIT ?", (limit,)),
        notifications=suppliers.list_notifications(limit=limit),
    )


# --------------------------------------------------------------------------- #
# static SPA
# --------------------------------------------------------------------------- #
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


@app.get("/")
def index():
    return FileResponse(str(BASE / "static" / "index.html"))


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


if __name__ == "__main__":
    import uvicorn

    # HOST/PORT env overrides (used by Docker); PORT=0/empty safely falls back to 8000
    print("\n  Smart Pharmacy Inventory Management System")
    print(f"  -> http://{config.HOST}:{config.PORT}\n")
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="info")
