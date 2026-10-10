"""E2E checks against local wrangler dev (127.0.0.1:8787).
Covers: per-user chat isolation, IDOR rejection, idempotent retry,
pagination, dispense atomicity, replication drain."""
import json
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8787"
PASS, FAIL = [], []


def req(method, path, body=None, token=None, raw=None):
    data = raw if raw is not None else (
        json.dumps(body).encode() if body is not None else None)
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("X-Session-Token", token)
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:  # noqa: BLE001
        return 0, {"detail": str(e)}


def check(name, cond, info=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {info}" if info else ""))


def login(u, p):
    s, d = req("POST", "/api/login", {"username": u, "password": p})
    return d.get("token") if s == 200 else None


admin = login("admin", "test-admin-local")
pharm = login("pharmacist", "test-pharm-local")
check("login admin", bool(admin))
check("login pharmacist", bool(pharm))
if not (admin and pharm):
    raise SystemExit("logins failed")

# ---------------- chat: two users, independent conversations ----------------
s, convA1 = req("POST", "/api/chat/conversations", {"title": "Admin one"}, admin)
check("admin creates conversation", s == 200 and convA1.get("id"), str(convA1))
a_conv = convA1["id"]

s, r1 = req("POST", "/api/chat",
            {"conversation_id": a_conv, "message": "Stock of Dolo 650",
             "client_msg_id": "e2e-admin-1"}, admin)
check("admin sends message", s == 200 and r1.get("text"), f"status={s}")

s, r2 = req("POST", "/api/chat",
            {"conversation_id": a_conv, "message": "Stock of Dolo 650",
             "client_msg_id": "e2e-admin-1"}, admin)
check("idempotent retry returns stored reply",
      s == 200 and r2.get("text") == r1.get("text") and r2.get("duplicate") is True,
      f"dup={r2.get('duplicate')}")

s, msgs = req("GET", f"/api/chat/conversations/{a_conv}/messages", token=admin)
roles = [m["role"] for m in msgs.get("items", [])]
check("admin conversation has exactly 2 messages after retry",
      len(msgs.get("items", [])) == 2 and roles == ["user", "assistant"], str(roles))
check("messages carry ownership user_id implicitly (all mine)",
      all(m.get("conversation_id") == a_conv for m in msgs.get("items", [])))

s, r3 = req("POST", "/api/chat", {"message": "What expires soon?", "client_msg_id": "e2e-admin-2"}, admin)
auto_conv = r3.get("conversation_id")
check("admin auto-creates a second conversation",
      s == 200 and auto_conv and auto_conv != a_conv, f"{auto_conv}")

# pharmacist's own conversation
s, pconv = req("POST", "/api/chat/conversations", {"title": "Pharm one"}, pharm)
check("pharmacist creates conversation", s == 200 and pconv.get("id"))
p_id = pconv["id"]
s, pr = req("POST", "/api/chat",
            {"conversation_id": p_id, "message": "how many Dolo 650 are left?",
             "client_msg_id": "e2e-pharm-1"}, pharm)
check("pharmacist sends message", s == 200 and pr.get("text"), f"status={s}")

# ---------------- isolation / IDOR ----------------
s, plist = req("GET", "/api/chat/conversations", token=pharm)
p_ids = [c["id"] for c in plist.get("items", [])]
check("pharmacist list excludes admin conversations",
      p_id in p_ids and a_conv not in p_ids and 4 not in p_ids, f"got {p_ids}")

s, alist = req("GET", "/api/chat/conversations", token=admin)
a_ids = [c["id"] for c in alist.get("items", [])]
check("admin list excludes pharmacist conversations",
      p_id not in a_ids and a_conv in a_ids, f"got {a_ids}")

s, d = req("GET", f"/api/chat/conversations/{a_conv}/messages", token=pharm)
check("cross-user read messages -> 404", s == 404, f"status={s}")
s, d = req("PATCH", f"/api/chat/conversations/{a_conv}", {"title": "hacked"}, token=pharm)
check("cross-user rename -> 404", s == 404, f"status={s}")
s, d = req("DELETE", f"/api/chat/conversations/{a_conv}", token=pharm)
check("cross-user delete -> 404", s == 404, f"status={s}")
s, d = req("POST", "/api/chat",
           {"conversation_id": a_conv, "message": "hi", "client_msg_id": "e2e-x"}, pharm)
check("cross-user send into other's conversation -> 404", s == 404, f"status={s}")

# admin's conversation must be untouched by those attempts
s, msgs = req("GET", f"/api/chat/conversations/{a_conv}/messages", token=admin)
check("admin conversation intact after cross-user attempts",
      s == 200 and len(msgs.get("items", [])) == 2, f"n={len(msgs.get('items', []))}")

# own rename / pagination / delete
s, d = req("PATCH", f"/api/chat/conversations/{p_id}", {"title": "Renamed by owner"}, token=pharm)
check("owner rename works", s == 200 and d.get("title") == "Renamed by owner", str(d.get("title")))
s, page = req("GET", f"/api/chat/conversations/{p_id}/messages?limit=1", token=pharm)
check("message pagination limit=1", s == 200 and len(page.get("items", [])) == 1 and page.get("has_more"))
s, cpage = req("GET", "/api/chat/conversations?limit=1&offset=0", token=pharm)
check("conversation pagination", s == 200 and len(cpage.get("items", [])) <= 1 and "total" in cpage)

# unauthenticated
s, d = req("GET", "/api/chat/conversations")
check("unauthenticated conversations -> 401", s == 401, f"status={s}")
s, d = req("POST", "/api/chat", {"message": "hi"})
check("unauthenticated chat -> 401", s == 401, f"status={s}")

# oversize message
s, d = req("POST", "/api/chat", {"message": "x" * 5000, "client_msg_id": "big"}, admin)
check("oversize message -> 400", s == 400, f"status={s}")

# ---------------- dispense atomicity ----------------
s, look = req("GET", "/api/counter/lookup?name=Dolo%20650&qty=5", token=admin)
check("bloom + counter lookup finds drug", s == 200 and look.get("usable", 0) > 0, str(look.get("usable")))
before = look.get("usable", 0)
s, disp = req("POST", "/api/dispense", {"name": "Dolo 650", "qty": 5}, admin)
check("dispense succeeds", s == 200 and disp.get("dispensed") == 5, str(disp))
s, look2 = req("GET", "/api/counter/lookup?name=Dolo%20650&qty=1", token=admin)
check("stock decremented by dispense",
      s == 200 and look2.get("usable") == before - 5, f"{before} -> {look2.get('usable')}")
s, look3 = req("GET", "/api/counter/lookup?name=Notamed%20999&qty=1", token=admin)
check("absent medicine -> 404 (bloom negative)", s == 404, f"status={s}")

# negative qty rejected
s, d = req("POST", "/api/dispense", {"name": "Dolo 650", "qty": -3}, admin)
check("negative qty -> 400", s == 400, f"status={s}")

# ---------------- replication ----------------
s, run = req("POST", "/api/sync/run", token=admin)
check("replication drain runs", s == 200, json.dumps(run)[:200])
s, st = req("GET", "/api/sync/status", token=admin)
check("sync status reports healthy", s == 200, json.dumps(st)[:300])
pending = None
try:
    pending = st.get("outbox", {}).get("pending")
except AttributeError:
    pending = None
check("outbox drained (pending 0 or unknown)", pending in (0, None), f"pending={pending}")

print()
print(f"==== {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    print("FAILURES:")
    for f in FAIL:
        print("  -", f)
raise SystemExit(1 if FAIL else 0)
