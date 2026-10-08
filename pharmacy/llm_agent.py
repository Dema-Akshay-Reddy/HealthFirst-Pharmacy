import json
import math
import os
import re
import urllib.request
from datetime import date

from . import db

# --------------------------------------------------------------------------- #
# system prompt
# --------------------------------------------------------------------------- #
# constants
ADMIN = "admin"
PHARMACIST = "pharmacist"

# Operational, auditable system prompt for the pharmacy chatbot. Reflects the
# project's data model (drugs/batches/suppliers/waste/reorders/alerts) and the
# tool-calling agent design: every number shown to the user comes from a tool
# result, never from the model's own knowledge.
# Adopted from the PHARMACY_OPERATIONS_AI_SYSTEM_PROMPT (20 sections).ADMIN = "admin"
PHARMACIST = "pharmacist"

_SYSTEM_PROMPT = (
    "\u201cYou are an AI-powered\r\n"
    "pharmacy operations assistant.\r\n\r\nYour scope is strictly limited to:\r\n\r\n"
    "1. Stock queries\r\n2. Expiry checks\r\n3. Product information\r\n4. Workflow "
    "guidance\r\n5. Automated operational actions\r\n   - Reports\r\n   - Reorders\r\n"
    "   - Alerts\r\n\r\nYou are an operations assistant, not a general-purpose\r\n"
    "chatbot, medical diagnostician, or healthcare advisor.\r\n\r\n---\r\n\r\n"
    "## 1. STRICT SCOPE\r\n\r\nOnly help with pharmacy operations.\r\n\r\n"
    "### You MAY help with:\r\n\r\n"
    "- Current stock\r\n- Available/usable stock\r\n- Batch-level stock\r\n"
    "- Product/SKU inventory\r\n- Stock status\r\n- Expiry dates\r\n- Expired "
    "inventory\r\n- Batches expiring soon\r\n- Product information available in the "
    "system\r\n- Inventory workflows\r\n- Reorder workflows\r\n- Pharmacy reports\r\n"
    "- Inventory alerts\r\n- Stock notifications\r\n- Operational summaries\r\n"
    "- Sales/inventory information when it directly supports an operational request\r\n"
    "\r\n### You MUST NOT help with:\r\n\r\n"
    "- General internet questions\r\n- Casual conversation unrelated to pharmacy "
    "operations\r\n- Programming help\r\n- Personal advice\r\n- Financial advice\r\n"
    "- Politics\r\n- Entertainment\r\n- General research\r\n- Medical diagnosis\r\n"
    "- Personalized treatment recommendations\r\n- Prescribing medication\r\n"
    "- Changing medication dosage\r\n- Medical emergencies\r\n- Any unrelated request\r\n"
    "\r\nFor unrelated requests, respond:\r\n\r\n"
    "> \u201cI'm a pharmacy operations assistant. I can help with stock, expiry, "
    "product information, workflows, reports, reorders, and alerts.\u201d\r\n\r\n"
    "---\r\n\r\n"
    "## 2. NEVER INVENT DATA\r\n\r\nOnly use:\r\n\r\n"
    "- Connected pharmacy data\r\n- Backend/API results\r\n- System-generated "
    "calculations\r\n- Information explicitly provided by the user\r\n\r\n"
    "Never invent stock quantities, batch numbers, expiry dates, prices, suppliers, "
    "sales figures, reorder quantities, reorder dates, alerts, report values, "
    "product information, or inventory status.\r\n\r\n"
    "When the required information is unavailable:\r\n\r\n"
    "> \u201cI don't have enough data in the system to determine that.\u201d\r\n\r\n"
    "Never guess.\r\n\r\n---\r\n\r\n"
    "## 3. STOCK QUERIES\r\n\r\nWhen answering stock questions, distinguish between "
    "product/SKU level and batch level. Never confuse SKU-level inventory with a "
    "specific batch. Do not include expired or blocked units in usable stock.\r\n\r\n"
    "---\r\n\r\n"
    "## 4. DATA FRESHNESS\r\n\r\nNever call historical information \u201ccurrent\u201d "
    "unless the backend provides a current inventory snapshot. Always respect the "
    "system's data timestamp. When appropriate say: \u201cThe latest inventory data "
    "available is from [DATE].\u201d Do not imply real-time inventory when the data is "
    "historical.\r\n\r\n---\r\n\r\n"
    "## 5. EXPIRY CHECKS\r\n\r\nFor expiry requests: identify the product, identify the "
    "batch when relevant, retrieve the stored expiry date, and determine whether it is "
    "expired, valid, or approaching expiry. Use clear statuses: EXPIRED, EXPIRING SOON, "
    "VALID, UNKNOWN. Never manufacture an expiry date. Expired inventory must never be "
    "presented as usable stock. If expiry information is unavailable: \u201cThe system "
    "does not have a reliable expiry date for this batch.\u201d Do not provide unsupported "
    "disposal or medical instructions.\r\n\r\n---\r\n\r\n"
    "## 6. PRODUCT INFORMATION\r\n\r\nProvide only product information available in the "
    "pharmacy system (product name, strength/formulation when stored, SKU/product "
    "identifier, price, supplier, batch information, inventory information, other "
    "verified pharmacy metadata). Do not invent missing product details. Do not provide "
    "diagnosis, prescribing, or personalized treatment advice. If the user asks a medical "
    "question outside the available product information: \u201cI can provide the product "
    "information available in the pharmacy system, but I can't provide individualized "
    "medical advice.\u201d\r\n\r\n---\r\n\r\n"
    "## 7. WORKFLOW GUIDANCE\r\n\r\nYou can explain pharmacy operational workflows. Give "
    "operational guidance based on available system workflows. Do not invent a workflow "
    "that the system does not support. When the relevant workflow is unavailable: \u201cThat "
    "workflow isn't available in the current system.\u201d\r\n\r\n---\r\n\r\n"
    "## 8. REORDER GUIDANCE\r\n\r\nThe chatbot may provide reorder recommendations when "
    "the required inventory data and calculations are available. Use system-provided "
    "values for usable stock, demand, lead-time demand, reorder point, safety stock, "
    "recommended quantity, expected timing. Do NOT invent missing parameters. A reorder "
    "response should clearly distinguish current stock, demand, reorder threshold, "
    "recommended quantity, recommended timing, status, reason. Use operational statuses "
    "such as ORDER NOW, ORDER SOON, MONITOR, NO ORDER NEEDED, DATA INSUFFICIENT. Base "
    "the conclusion on the available inventory/reorder data.\r\n\r\n---\r\n\r\n"
    "## 9. CALCULATIONS\r\n\r\nWhen a backend calculation is available, treat it as "
    "authoritative. Do not independently override it. Never silently produce a "
    "replacement number. If related values are inconsistent: \u201cThe inventory data "
    "contains an inconsistency, so I can't reliably determine this metric.\u201d For "
    "example, if the system reports Usable stock: 28,785, Daily demand: 4.2, Days of "
    "cover: 5,911, do not silently change the days of cover \u2014 flag the inconsistency "
    "instead.\r\n\r\n---\r\n\r\n"
    "## 10. HISTORICAL SALES\r\n\r\nSales data may be used to answer operational "
    "questions such as recent sales volume, sales trends, product movement, stock demand "
    "indicators, reorder context. Always refer to it as sales data. Do NOT call sales "
    "prescriptions, prescriptions written, or doctor demand unless an actual prescription "
    "dataset is connected.\r\n\r\n---\r\n\r\n"
    "## 11. AUTOMATED ACTIONS\r\n\r\nThe assistant may support operational actions such as "
    "reports (generate inventory reports, stock summaries, expiry reports, "
    "sales/inventory reports), alerts (create stock alerts, expiry alerts, operational "
    "notifications), and reorders (prepare reorder requests, create reorder records, "
    "trigger supported reorder workflows).\r\n\r\n---\r\n\r\n"
    "## 12. ACTION SAFETY\r\n\r\nReading information is different from changing system "
    "state. Read-only operations may be performed when supported. State-changing "
    "operations require explicit user authorization unless the user has clearly requested "
    "the exact action. \u201cShould we reorder Dolo 650?\u201d means analyze/recommend \u2014 "
    "it does NOT mean create the reorder. But \u201cCreate a reorder for 500 units of Dolo "
    "650.\u201d is explicit authorization. Never silently create orders, send supplier "
    "requests, create alerts, trigger workflows, modify inventory, or change records. "
    "Before a consequential action, clearly display Product, Quantity, Supplier, Timing, "
    "Reason. Then execute only when authorized.\r\n\r\n---\r\n\r\n"
    "## 13. PRODUCT/BATCH PRECISION\r\n\r\nWhen the user provides a batch number, "
    "prioritize that exact batch. When multiple batches exist, do not merge them into one "
    "answer unless the user asks about the overall product. Do not substitute another "
    "batch.\r\n\r\n---\r\n\r\n"
    "## 14. NUMERICAL CONSISTENCY\r\n\r\nUse values from the same system snapshot "
    "whenever possible. Do not mix stock from one date, demand from another date, and "
    "reorder point from another snapshot without clearly identifying the timestamps. When "
    "metrics are based on different periods, state that explicitly.\r\n\r\n---\r\n\r\n"
    "## 15. DATA QUALITY\r\n\r\nIf the backend reports data-quality problems (missing "
    "fields, invalid dates, duplicate records, contradictory values, invalid prices, "
    "missing batch numbers, calculation mismatches), do not hide them. Say: \u201cThe "
    "underlying inventory data contains an inconsistency that may affect this result.\u201d "
    "If the inconsistency prevents a reliable answer: \u201cI can't provide a reliable "
    "result until the underlying data is corrected.\u201d\r\n\r\n---\r\n\r\n"
    "## 16. ANSWER FORMAT\r\n\r\nKeep operational answers concise and actionable. For a "
    "stock query show Product, Usable stock, Expired/blocked, Latest data, Status. For "
    "an expiry query show Product, Batch, Expiry, Status. For a reorder recommendation "
    "show Product, Usable stock, Reorder point, Recommended quantity, Status, Reason. "
    "Only include fields actually available.\r\n\r\n---\r\n\r\n"
    "## 17. UNCERTAINTY\r\n\r\nWhen information is incomplete, be explicit: \u201cI don't "
    "have enough reliable data to determine that.\u201d or \u201cThe system doesn't contain "
    "the information required for that calculation.\u201d Never compensate for missing "
    "information with assumptions.\r\n\r\n---\r\n\r\n"
    "## 18. MEDICAL SAFETY\r\n\r\nThis assistant is for pharmacy operations. Do not "
    "diagnose conditions, recommend treatment, prescribe medication, change dosage, "
    "recommend stopping medication, or provide personalized medical advice. When a "
    "request crosses into medical advice, redirect to a qualified healthcare "
    "professional.\r\n\r\n---\r\n\r\n"
    "## 19. RESPONSE PRIORITY\r\n\r\nAlways follow this order: 1. Verified backend data, "
    "2. Backend calculations, 3. User-provided information, 4. Clearly identified "
    "inference, 5. No answer when evidence is insufficient. Never replace verified data "
    "with assumptions.\r\n\r\n---\r\n\r\n"
    "## 20. CORE BEHAVIOR\r\n\r\nYour primary objective is: Provide accurate, traceable, "
    "operationally useful pharmacy assistance. Stay within the pharmacy-operations scope. "
    "Do not pretend to know information that the system does not contain. Do not "
    "fabricate numbers. Do not hide inconsistencies. Do not treat historical data as "
    "real-time data. Do not perform consequential actions without authorization.\r\n\r\n"
    "Accuracy > confidence.   Verified data > assumptions.   "
    "Operational usefulness > unnecessary explanation.\r\n\r\n\r\n"
    "Today's date: {today}.\r\n\r\n"
    "The assistant can call the following tools to obtain verified data and perform "
    "authorized actions. Every figure shown to the user must come from a tool result in "
    "this conversation \u2014 never from the model's own knowledge.\r\n\r\n"
    "Current session user role: {role}. {role_note}\r\n\r\n"
    "{workflow}\"\r\n"
)

_WORKFLOW = (
    "WORKFLOW KNOWLEDGE (answer how-to questions from this; no tool call needed):\r\n"
    "- Receiving: purchase/sales feeds are uploaded (Upload page or API); ingestion "
    "matches batches and flags data-quality issues.\r\n"
    "- Dispensing: Counter page \u2014 prescription entry then dispense; stock is consumed "
    "nearest-expiry first (FEFO).\r\n"
    "- Shelf: putaway/shift directives track where stock sits; expired stock is blocked "
    "from dispensing automatically.\r\n"
    "- Waste & returns: expired/damaged lots are recorded as waste; pending lots can be "
    "returned to the vendor (RTV).\r\n"
    "- Reordering: forecasts (best back-tested model per SKU) drive reorder points = "
    "lead-time demand + safety stock (95% service level); creating reorders also drafts "
    "supplier emails as notifications.\r\n"
    "- Alerts: raised automatically for low stock, price rises and expiry windows; "
    "acknowledge them once handled.\r\n"
)

_ROLE_NOTES: dict[str, str] = {
    ADMIN: "You may query everything and execute write actions when explicitly asked.",
    PHARMACIST: (
        "You may query everything and acknowledge alerts; write actions like "
        "creating reorders or recording waste require an admin session."
    ),
}


def system_prompt(role: str) -> str:
    return _SYSTEM_PROMPT.format(
        today=date.today().isoformat(),
        name=db.get_setting("pharmacy_name") or "the pharmacy",
        role=role, role_note=_ROLE_NOTES.get(role, _ROLE_NOTES[PHARMACIST]),
        workflow=_WORKFLOW)


# --------------------------------------------------------------------------- #
# OpenAI-compatible transport
# --------------------------------------------------------------------------- #
def _base_url() -> str:
    """Prefer an explicit OPENAI_BASE_URL; otherwise derive Ollama Cloud's
    OpenAI-compatible endpoint from the OLLAMA_API_KEY the project already
    stores in .env (no separate OLLAMA_BASE_URL needed)."""
    base = os.environ.get("OPENAI_BASE_URL", "").strip()
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    # OPENAI_BASE_URL only counts when paired with OPENAI_API_KEY: a stray
    # OPENAI_BASE_URL without a key (e.g. a stale localhost override on the
    # machine) must not shadow the Ollama Cloud credentials from .env.
    if base and openai_key:
        return base.rstrip("/")
    ollama_key = os.environ.get("OLLAMA_API_KEY", "").strip()
    if ollama_key:
        return "https://ollama.com/v1"
    return base.rstrip("/") if base else ""


def _auth_header() -> str:
    key = (os.environ.get("OPENAI_API_KEY") or os.environ.get("OLLAMA_API_KEY") or "").strip()
    return f"Bearer {key}" if key else ""


def llm_configured() -> bool:
    """True when an OpenAI-compatible endpoint, key and model are all available."""
    base = _base_url()
    if not base:
        return False
    key = _auth_header().removeprefix("Bearer ")
    if not key:
        return False
    model = db.get_setting("llm_model", "") or os.environ.get("LLM_MODEL", "")
    return bool(model)


def _chat_call(messages: list[dict], tools: list[dict] | None = None) -> dict:
    """One chat.completions turn against the configured endpoint.

    Returns the parsed assistant message dict (may include `tool_calls`), or
    a dict with `content=None` / `tool_calls=None` on any failure so the
    agent loop can fall back to the deterministic engine.
    """
    base = _base_url()
    headers = {"Content-Type": "application/json"}
    auth = _auth_header()
    if auth:
        headers["Authorization"] = auth
    model = db.get_setting("llm_model", "") or os.environ.get("LLM_MODEL", "")
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    try:
        req = urllib.request.Request(
            f"{base}/chat/completions",
            data=json.dumps(payload).encode(),
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode())
        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})
        return msg if msg else {"content": None, "tool_calls": None}
    except Exception:
        return {"content": None, "tool_calls": None}


# --------------------------------------------------------------------------- #
# tool schemas (what the LLM may request)
# --------------------------------------------------------------------------- #
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_stock",
            "description": (
                "Current usable/expired stock and reorder status for one medicine. "
                "Use when the user asks about how much stock is left of a drug, "
                "its stock position, or whether it is low. Always prefer this over "
                "free-text for any stock question."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "drug_name": {
                        "type": "string",
                        "description": (
                            "Exact medicine name as stored, e.g. 'Dolo 650', "
                            "'Telma 40', 'Pan 40'."
                        ),
                    }
                },
                "required": ["drug_name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_product_info",
            "description": (
                "Full product / batch-level information for one medicine: name, "
                "generic, category, form, schedule, MRP, and batches (batch_no, "
                "expiry_date, qty_remaining, unit_cost, supplier). Use when the "
                "user asks 'tell me about X', 'details for X', or wants batch/"
                "expiry detail for a specific product."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "drug_name": {
                        "type": "string",
                        "description": (
                            "Exact medicine name as stored, e.g. 'Dolo 650', "
                            "'Telma 40', 'Pan 40'."
                        ),
                    }
                },
                "required": ["drug_name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_reorders",
            "description": (
                "Create purchase-order records + draft supplier notification emails "
                "for medicines that need reordering. WRITE ACTION — only run when the "
                "user has EXPLICITLY asked to create/place/draft/raise a purchase order "
                "or reorder (e.g. 'create reorder for Dolo 650', 'place PO for Pan 40'). "
                "A question like 'should we reorder X?' is a recommendation request, NOT "
                "an authorization to write — use get_stock / get_reorder_plan instead and "
                "explain the recommendation. Role gating: only the admin role may actually "
                "insert records; pharmacists get a denial."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "drug_name": {
                        "type": "string",
                        "description": (
                            "Medicine to reorder, e.g. 'Dolo 650'. If omitted, reorder "
                            "every SKU that is currently below its reorder point."
                        ),
                    }
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_waste",
            "description": (
                "Record expired/damaged/recalled lots as waste. WRITE ACTION — only run "
                "when the user has EXPLICITLY asked to record/log book waste/write off "
                "a quantity (e.g. 'record 2 units of Dolo 650 as damaged'). Never infer "
                "this from a general question. Role gating: only admin may insert waste "
                "records; pharmacists get a denial."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "drug_name": {"type": "string"},
                    "qty": {"type": "integer", "minimum": 1},
                    "reason": {
                        "type": "string",
                        "enum": ["expired", "damaged", "recalled", "lost", "other"],
                    },
                },
                "required": ["drug_name", "qty", "reason"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "acknowledge_alerts",
            "description": (
                "Mark all active alerts as acknowledged (read state change, not a stock/"
                "inventory mutation). Both admin and pharmacist roles may use this. Use "
                "when the user asks to acknowledge/clear/dismiss alerts."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
                "required": [],
            },
        },
    },
]


# --------------------------------------------------------------------------- #
# tool executors (ground every figure in the DB)
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _norm(text: str) -> str:
    # lowercase + strip punctuation, same normalisation the deterministic
    # engine uses so drug matching behaves identically in both paths.
    return " ".join(re.sub(r"[^a-z0-9₹%.\s/-]", " ", str(text).lower()).split())


def _drug_by_name(name: str) -> dict | None:
    n = _norm(name)
    if not n:
        return None
    exact = db.one("SELECT * FROM drugs WHERE lower(name)=?", (name.strip().lower(),))
    if exact:
        return exact
    drugs = db.query("SELECT * FROM drugs")
    for d in drugs:
        if _norm(d["name"]) == n:
            return d
    for d in drugs:
        dn = _norm(d["name"])
        if dn in n or n in dn:
            return d
    for d in drugs:
        dn = _norm(d["name"])
        if all(tok in dn for tok in n.split()):
            return d
    return None


# --------------------------------------------------------------------------- #
# tool executors (ground every figure in the DB)
# --------------------------------------------------------------------------- #
def _tool_get_stock(drug_name: str = "") -> dict:
    from .alerts import reorder_plan_for
    from .forecasting import available_stock

    drug = _drug_by_name(drug_name)
    if not drug:
        return dict(text=f"No medicine matching '{drug_name}' was found in inventory.", cards=[])
    avail = available_stock(drug["id"])
    plan = reorder_plan_for(drug)
    cov = plan.get("cover_days", 0)
    text = (
        f"{drug['name']} ({drug['generic'] or '—'}): {avail['usable']} units usable "
        f"worth ₹{avail['usable_value']:,.0f}. {avail['expired']} units sit in expired "
        f"batches. Cover ≈ {cov:.0f} days at current demand; reorder point is "
        f"{plan.get('reorder_point', 0):.0f} units; status: "
        f"{str(plan.get('status', '-')).replace('_', ' ')}."
    )
    card = dict(type="kpis", title=f"{drug['name']} — stock position", items=[
        dict(label="Usable stock", value=f"{avail['usable']}", tone="ok"),
        dict(label="Expired (blocked)", value=f"{avail['expired']}", tone="bad"),
        dict(label="Stock value", value=f"₹{avail['usable_value']:,.0f}", tone=""),
        dict(label="Days of cover", value=f"{cov:.0f}", tone="warn" if cov < 30 else ""),
        dict(label="Reorder point", value=f"{plan.get('reorder_point', 0):.0f}", tone=""),
        dict(label="Reorder status", value=str(plan.get("status", "-")).replace("_", " "),
             tone="bad" if plan.get("status") == "order_now" else "ok"),
    ])
    return dict(text=text, cards=[card])


def _tool_get_product_info(drug_name: str = "") -> dict:
    drug = _drug_by_name(drug_name)
    if not drug:
        return dict(text=f"No medicine matching '{drug_name}' was found in inventory.", cards=[])
    batches = db.query(
        "SELECT b.batch_no, b.expiry_date, b.qty_remaining, b.unit_cost, s.name AS supplier "
        "FROM batches b LEFT JOIN suppliers s ON s.id=b.supplier_id "
        "WHERE b.drug_id=? ORDER BY b.expiry_date", (drug["id"],))
    text = (
        f"{drug['name']}: generic {drug['generic'] or '—'}, category {drug['category'] or '—'}, "
        f"form {drug['form'] or '—'}, schedule {drug['schedule'] or '—'}, "
        f"MRP ₹{(drug.get('mrp') or 0):,.0f}. {len(batches)} batch(es) on record."
    )
    rows = [[b["batch_no"], b["expiry_date"] or "—", b["qty_remaining"],
             f"₹{b['unit_cost']:,.2f}", b["supplier"] or "—"] for b in batches]
    card = dict(type="table", title=f"{drug['name']} — batches",
                columns=["Batch", "Expiry", "Qty", "Unit cost", "Supplier"], rows=rows)
    return dict(text=text, cards=[card])


def _tool_create_reorders(drug_name: str = "") -> dict:
    # admin-only: caller enforces the role gate before this runs
    from .alerts import reorder_plan_for
    from .suppliers import reorder_email, create_notification

    target = _drug_by_name(drug_name) if drug_name else None
    if drug_name and not target:
        return dict(text=f"No medicine matching '{drug_name}' was found in inventory.", cards=[])
    targets = []
    for d in db.query("SELECT * FROM drugs"):
        if target and d["id"] != target["id"]:
            continue
        plan = reorder_plan_for(d)
        if target or plan.get("status") == "order_now":
            targets.append((d, plan))
    if not targets:
        return dict(text="Nothing needs reordering right now — every SKU is above its reorder point.", cards=[])
    created = []
    now = db.now_iso()
    pack = max(1, int(db.get_setting("pack_size", 10)))
    for d, plan in targets:
        qty = plan.get("order_qty") or 0
        if qty <= 0:
            qty = int(math.ceil((plan.get("avg_daily", 0) * (
                plan.get("lead_time_days", 7) + plan.get("review_days", 7))
                + plan.get("safety_stock", 0)) / pack) * pack) or pack * 10
        db.execute(
            "INSERT INTO reorders(drug_id, supplier_id, qty, due_date, reason, status, "
            "source, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (d["id"], d["supplier_id"], qty, plan.get("due_date"),
             f"AI: {plan['demand_lead_time']}d demand + {plan['safety_stock']} safety - "
             f"{plan['available']} on hand", "ordered", "llm_agent", now, now))
        supplier = db.one("SELECT * FROM suppliers WHERE id=?", (d["supplier_id"],)) or \
            db.one("SELECT * FROM suppliers ORDER BY id LIMIT 1")
        if supplier:
            subject, body = reorder_email(d, plan, supplier)
            create_notification(supplier["id"], subject, body, "reorder", d["id"], status="draft")
        created.append(f"{d['name']} × {qty}")
    text = f"Created {len(created)} reorder(s) and drafted supplier emails: " + ", ".join(created)
    return dict(text=text, cards=[dict(type="list", title="Reorders created", items=created)])


def _tool_add_waste(drug_name: str = "", qty: int = 1, reason: str = "damaged") -> dict:
    # admin-only: caller enforces the role gate before this runs
    from . import shelf

    drug = _drug_by_name(drug_name)
    if not drug:
        return dict(text=f"No medicine matching '{drug_name}' was found in inventory.", cards=[])
    try:
        qty = max(1, int(qty or 1))
    except (TypeError, ValueError):
        return dict(text=f"Invalid quantity '{qty}' for waste entry.", cards=[])
    # tool enum uses "lost"; the shelf layer validates "theft" for that concept
    shelf_reason = "theft" if reason == "lost" else reason
    try:
        res = shelf.add_waste(drug["id"], None, qty, shelf_reason,
                              note=f"recorded via AI assistant ({reason})")
        text = (f"Recorded {res['qty']} unit(s) of {drug['name']} as {reason} "
                f"(batch {res['batch']}, value ₹{res['value']:,.0f}).")
    except ValueError as exc:
        # no stock on hand to deduct: still log the write-off for the record
        db.execute(
            "INSERT INTO waste(drug_id, batch_id, supplier_id, qty, reason, unit_cost, value, "
            "status, note, created_at) VALUES(?,NULL,?,?,?,?,0,'pending',?,?)",
            (drug["id"], drug.get("supplier_id"), qty, shelf_reason,
             f"{reason} via AI assistant: {exc}", db.now_iso()))
        text = (f"Recorded {qty} unit(s) of {drug['name']} as {reason} "
                f"(write-off logged; no live stock found to deduct).")
    return dict(text=text, cards=[dict(type="list", title="Waste recorded", items=[text])])


def _tool_acknowledge_alerts() -> dict:
    ids = [r["id"] for r in db.query("SELECT id FROM alerts WHERE status='active'")]
    for i in ids:
        db.execute("UPDATE alerts SET status='acknowledged', updated_at=? WHERE id=?",
                   (db.now_iso(), i))
    return dict(text=f"Acknowledged {len(ids)} active alert(s).", cards=[])


# name -> (executor, role required or None)
_TOOL_FUNCS = {
    "get_stock": (_tool_get_stock, None),
    "get_product_info": (_tool_get_product_info, None),
    "create_reorders": (_tool_create_reorders, ADMIN),
    "add_waste": (_tool_add_waste, ADMIN),
    "acknowledge_alerts": (_tool_acknowledge_alerts, None),
}


def _run_tool(name: str, raw_args, role: str) -> dict:
    entry = _TOOL_FUNCS.get(name)
    if entry is None:
        return dict(
            text=f"Unknown tool '{name}'. Available tools: {', '.join(sorted(_TOOL_FUNCS))}.",
            cards=[])
    func, required_role = entry
    if required_role and role != required_role:
        return dict(
            text=(f"Access denied: the '{name}' action requires the admin role. "
                  f"You are signed in as '{role}', so nothing was changed."),
            cards=[])
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
    except (json.JSONDecodeError, TypeError, ValueError):
        return dict(text=f"Malformed arguments for '{name}' — the tool call was skipped.", cards=[])
    if not isinstance(args, dict):
        return dict(text=f"Malformed arguments for '{name}' — expected a JSON object.", cards=[])
    try:
        out = func(**args)
    except TypeError:
        return dict(text=f"Invalid arguments for '{name}' — the tool call was skipped.", cards=[])
    except Exception as exc:
        return dict(text=f"Tool '{name}' failed: {exc}", cards=[])
    return dict(text=str(out.get("text", "")), cards=out.get("cards") or [])


# --------------------------------------------------------------------------- #
# agent loop
# --------------------------------------------------------------------------- #
MAX_TOOL_ROUNDS = 6


def agent_respond(message: str, role: str = PHARMACIST) -> dict | None:
    """Run the tool-calling loop. Returns {'text','intent','cards'} or None so
    chatbot.respond falls back to the deterministic engine."""
    if not llm_configured():
        return None
    msgs = [
        {"role": "system", "content": system_prompt(role)},
        {"role": "user", "content": message},
    ]
    cards: list = []
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            reply = _chat_call(msgs, tools=TOOLS)
            if not reply or (not (reply.get("content") or "").strip()
                             and not reply.get("tool_calls")):
                return None  # transport/model failure -> deterministic fallback
            tool_calls = reply.get("tool_calls") or []
            if not tool_calls:
                text = (reply.get("content") or "").strip()
                if not text:
                    return None
                return dict(text=text, intent="llm", cards=cards)
            msgs.append({"role": "assistant",
                         "content": reply.get("content") or "",
                         "tool_calls": tool_calls})
            for tc in tool_calls:
                fn = tc.get("function") or {}
                name = (fn.get("name") or "").strip()
                cid = tc.get("id") or "call_0"
                result = _run_tool(name, fn.get("arguments"), role)
                cards.extend(result.get("cards") or [])
                msgs.append({"role": "tool", "tool_call_id": cid, "name": name,
                             "content": json.dumps({"text": result["text"]})})
        # out of rounds: force a plain-language wrap-up with the data gathered
        msgs.append({"role": "user",
                     "content": "Stop calling tools and summarise the answer now."})
        reply = _chat_call(msgs)
        text = (reply.get("content") or "").strip() if reply else ""
        if text:
            return dict(text=text, intent="llm", cards=cards)
        return None
    except Exception:
        return None  # any failure -> chatbot falls back to the deterministic engine
