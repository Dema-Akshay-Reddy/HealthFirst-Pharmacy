"""Integration tests for the Worker's per-user chat endpoints (Miniflare).

Unlike ``cloudflare/test/e2e_local.py`` — which expects a manually started
``wrangler dev`` — this module is self-contained pytest:

  1. applies ``schema.sql`` + migrations ``0001``/``0002`` to an ISOLATED
     temporary local D1 state directory (so the developer's normal
     ``wrangler dev`` state and any production data are never touched, and
     the migrations are proven re-runnable on a current schema);
  2. starts ``wrangler dev`` (workerd / Miniflare) on ``WORKER_TEST_PORT``;
  3. exercises the real HTTP API the SPA uses;
  4. tears the Worker down afterwards.

Covers the chatbot acceptance criteria end-to-end at the HTTP level:
independent conversations per authenticated user, server-side ownership
(cross-user read/rename/delete/send -> 404), idempotent retries without
duplicate messages, pagination defaults, ordering/timestamps, validation,
clear-history scoping, and replication of chat rows to the secondary D1.

Run just this module::

    python -m pytest tests/test_worker_chat.py -v

Requires ``node`` + ``cloudflare/node_modules`` (wrangler); skips cleanly
when they are missing.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CF_DIR = ROOT / "cloudflare"
WRANGLER_JS = CF_DIR / "node_modules" / "wrangler" / "bin" / "wrangler.js"
PORT = int(os.environ.get("WORKER_TEST_PORT", "18787"))
INSPECTOR_PORT = PORT + 444  # stay clear of a running `wrangler dev`
BASE = f"http://127.0.0.1:{PORT}"
STARTUP_TIMEOUT_S = 120
INIT_FILES = [
    CF_DIR / "schema.sql",
    CF_DIR / "migrations" / "0001_replication.sql",
    CF_DIR / "migrations" / "0002_chat.sql",
]
D1_DATABASES = ("pharmacy-primary", "pharmacy-secondary")


# --------------------------------------------------------------------------
# subprocess / HTTP helpers
# --------------------------------------------------------------------------
def _wrangler(args: list[str], *, timeout: int = 180) -> str:
    """Run a wrangler CLI command in the Worker directory; return stdout."""
    node = shutil.which("node")
    if not node:
        raise RuntimeError("node not found on PATH")
    proc = subprocess.run(
        [node, str(WRANGLER_JS), *args],
        cwd=CF_DIR,
        timeout=timeout,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "WRANGLER_SEND_METRICS": "false"},
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"wrangler {' '.join(args)} exited {proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout[-2000:]}\n--- stderr ---\n{proc.stderr[-2000:]}"
        )
    return proc.stdout


def _parse_d1_json(out: str) -> dict:
    """d1 execute --json prints `[{ "results": [...], ... }]` (possibly with
    a little log noise around it) — return the first result row dict."""
    text = out.strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end <= start:
            raise AssertionError(f"unparseable d1 output:\n{out[:2000]}") from None
        payload = json.loads(text[start:end + 1])
    try:
        return payload[0]["results"][0]
    except (IndexError, KeyError, TypeError) as exc:
        raise AssertionError(f"unexpected d1 payload: {payload!r}") from exc


def _d1_query(state: Path, db: str, sql: str) -> dict:
    out = _wrangler([
        "d1", "execute", db, "--local", "--persist-to", str(state),
        "--json", "--command", sql,
    ])
    return _parse_d1_json(out)


def _api(method: str, path: str, body=None, token: str | None = None):
    """HTTP call against the Worker; returns (status, parsed_json)."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Session-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, (json.loads(raw) if raw.strip() else {})
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def _dev_var(name: str) -> str | None:
    """Read a local credential from cloudflare/.dev.vars (never committed
    values — the file is the Worker's local secret store)."""
    f = CF_DIR / ".dev.vars"
    if f.is_file():
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(name + "="):
                return line.split("=", 1)[1].strip() or None
    return os.environ.get(name) or None


def _login(username: str, password: str | None) -> str:
    if not password:
        pytest.skip(f"no password configured for {username} (.dev.vars / env)")
    last = None
    for _attempt in range(3):  # bounded: >10 failures/5min trips the login throttle
        status, body = _api("POST", "/api/login",
                            {"username": username, "password": password})
        if status == 200 and body.get("token"):
            return body["token"]
        if status == 401:
            pytest.fail(f"{username} login rejected: {body}")
        last = f"{status} {body}"
        time.sleep(2)
    pytest.fail(f"{username} login never succeeded: {last}")


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
@pytest.fixture(scope="session")
def worker(tmp_path_factory) -> Path:
    """Boot the real Worker under Miniflare against an isolated temp D1.

    Yields the state directory (needed for direct SQL checks against the
    primary/secondary databases); tears the Worker down afterwards.
    """
    if not shutil.which("node") or not WRANGLER_JS.is_file():
        pytest.skip("node + wrangler required for Worker integration tests")

    state = tmp_path_factory.mktemp("worker-d1")
    init_sql = state / "init.sql"
    init_sql.write_text(
        "\n\n".join(p.read_text(encoding="utf-8") for p in INIT_FILES),
        encoding="utf-8")
    for db in D1_DATABASES:  # fresh DB for each binding; migrations re-run harmlessly
        _wrangler(["d1", "execute", db, "--local",
                   "--persist-to", str(state), f"--file={init_sql}"])

    node = shutil.which("node")
    proc = subprocess.Popen(
        [node, str(WRANGLER_JS), "dev",
         "--port", str(PORT),
         "--inspector-port", str(INSPECTOR_PORT),
         "--persist-to", str(state)],
        cwd=CF_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env={**os.environ, "WRANGLER_SEND_METRICS": "false"},
        **({} if os.name == "nt" else {"start_new_session": True}),
    )
    log_lines: list[str] = []
    threading.Thread(
        target=lambda: [log_lines.append(ln) for ln in proc.stdout],
        daemon=True,
    ).start()

    def _tail() -> str:
        return "".join(log_lines[-40:]) or "(no output captured)"

    # health first (no DB), then one plain login attempt to prove users +
    # schema work. Raw _api here — never _login (it calls pytest.fail, whose
    # OutcomeException would bypass the failure cleanup below).
    try:
        deadline = time.time() + STARTUP_TIMEOUT_S
        reason = "not started"
        ready = False
        while time.time() < deadline:
            if proc.poll() is not None:
                pytest.fail(f"wrangler dev exited {proc.returncode}\n--- log ---\n{_tail()}")
            try:
                status, _ = _api("GET", "/api/health")
                if status == 200:
                    status, body = _api("POST", "/api/login",
                                        {"username": "admin",
                                         "password": _dev_var("ADMIN_PASSWORD") or ""})
                    if status == 200 and body.get("token"):
                        ready = True
                        break
                    reason = f"health ok, login={status} {body}"
                else:
                    reason = f"health returned {status}"
            except Exception as exc:  # noqa: BLE001 — server still starting
                reason = repr(exc)
            time.sleep(0.5)
        if not ready:
            pytest.fail(f"wrangler dev never became ready ({reason})\n--- log ---\n{_tail()}")
    except BaseException:
        _stop(proc)  # never leave an orphan Worker holding the port
        raise

    yield state

    _stop(proc)


@pytest.fixture(scope="session")
def admin(worker) -> str:
    """Session token for the seeded admin account."""
    return _login("admin", _dev_var("ADMIN_PASSWORD"))


@pytest.fixture(scope="session")
def pharmacist(worker) -> str:
    """Session token for the seeded pharmacist account."""
    return _login("pharmacist", _dev_var("PHARMACIST_PASSWORD"))


@pytest.fixture
def admin_conversation(admin) -> int:
    """A fresh, empty conversation owned by admin (no chat-rate cost)."""
    status, conv = _api("POST", "/api/chat/conversations",
                        {"title": "WCT target"}, admin)
    assert status == 200 and conv.get("id"), conv
    return conv["id"]


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------
def test_health_is_public(worker):
    status, body = _api("GET", "/api/health")
    assert status == 200 and body.get("status") == "ok", body


def test_chat_requires_authentication(worker):
    assert _api("GET", "/api/chat/conversations")[0] == 401
    assert _api("POST", "/api/chat", {"message": "hi"})[0] == 401
    assert _api("GET", "/api/chat/conversations/1/messages")[0] == 401
    assert _api("DELETE", "/api/chat/history")[0] == 401


def test_users_have_separate_conversations(admin, pharmacist):
    status, a = _api("POST", "/api/chat",
                     {"message": "WCT-OWNER-A hello", "client_msg_id": "wct-a1"},
                     admin)
    assert status == 200, a
    status, b = _api("POST", "/api/chat",
                     {"message": "WCT-OWNER-B hello", "client_msg_id": "wct-b1"},
                     pharmacist)
    assert status == 200, b
    assert a["conversation_id"] != b["conversation_id"]

    status, a_list = _api("GET", "/api/chat/conversations?limit=100", token=admin)
    assert status == 200
    a_ids = {c["id"] for c in a_list["items"]}
    assert a["conversation_id"] in a_ids
    assert b["conversation_id"] not in a_ids, "admin sees pharmacist's conversation"

    status, b_list = _api("GET", "/api/chat/conversations?limit=100", token=pharmacist)
    assert status == 200
    b_ids = {c["id"] for c in b_list["items"]}
    assert b["conversation_id"] in b_ids
    assert a["conversation_id"] not in b_ids, "pharmacist sees admin's conversation"

    # each user reads only their own history
    status, b_msgs = _api("GET", f"/api/chat/conversations/{b['conversation_id']}/messages",
                          token=pharmacist)
    assert status == 200
    assert [m["role"] for m in b_msgs["items"]] == ["user", "assistant"]
    status, _ = _api("GET", f"/api/chat/conversations/{b['conversation_id']}/messages",
                     token=admin)
    assert status == 404, "cross-user message read must be rejected"


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/chat/conversations/{conv}/messages", None),
    ("PATCH", "/api/chat/conversations/{conv}", {"title": "hacked"}),
    ("DELETE", "/api/chat/conversations/{conv}", None),
    ("POST", "/api/chat", {"conversation_id": "{conv}", "message": "injected"}),
])
def test_cross_user_operations_rejected(admin, pharmacist, admin_conversation, method, path, body):
    path = path.replace("{conv}", str(admin_conversation))
    if isinstance(body, dict):
        body = {k: (str(admin_conversation) if v == "{conv}" else v)
                for k, v in body.items()}
    status, resp = _api(method, path, body, pharmacist)
    assert status == 404, f"{method} {path} -> {status} {resp}"

    # admin's conversation is untouched and still listed
    status, listing = _api("GET", "/api/chat/conversations?limit=100", token=admin)
    assert status == 200
    assert admin_conversation in {c["id"] for c in listing["items"]}
    status, msgs = _api("GET", f"/api/chat/conversations/{admin_conversation}/messages",
                        token=admin)
    assert status == 200 and msgs["items"] == [], "no messages injected into admin's conversation"


def test_retry_returns_stored_reply_without_duplicates(admin):
    payload = {"message": "Stock of Dolo 650", "client_msg_id": "wct-retry-1"}
    status, first = _api("POST", "/api/chat", payload, admin)
    assert status == 200, first
    assert first.get("duplicate") is False

    status, second = _api("POST", "/api/chat", payload, admin)
    assert status == 200, second
    assert second["conversation_id"] == first["conversation_id"]
    assert second["text"] == first["text"], "retry must return the stored reply"
    assert second.get("duplicate") is True

    status, msgs = _api("GET",
                        f"/api/chat/conversations/{first['conversation_id']}/messages",
                        token=admin)
    assert status == 200
    assert [m["role"] for m in msgs["items"]] == ["user", "assistant"], \
        "a duplicate retry must not create extra messages"


def test_omitted_limit_returns_the_full_page(admin):
    """Regression: `Number(null) == 0` made an omitted `limit` become LIMIT 1."""
    status, conv = _api("POST", "/api/chat/conversations", {"title": "WCT page"}, admin)
    assert status == 200
    cid = conv["id"]
    status, _ = _api("POST", "/api/chat",
                     {"conversation_id": cid, "message": "WCT-OWNER-A page test",
                      "client_msg_id": "wct-page-1"}, admin)
    assert status == 200

    status, msgs = _api("GET", f"/api/chat/conversations/{cid}/messages", token=admin)
    assert status == 200
    assert len(msgs["items"]) == 2, \
        f"omitted limit must return the whole page, got {len(msgs['items'])}"

    status, convs = _api("GET", "/api/chat/conversations", token=admin)
    assert status == 200
    assert len(convs["items"]) == convs["total"], \
        "omitted limit must return every conversation on the first page"
    assert cid in {c["id"] for c in convs["items"]}

    # explicit limits still work
    status, page = _api("GET", f"/api/chat/conversations/{cid}/messages?limit=1", token=admin)
    assert status == 200 and len(page["items"]) == 1 and page["has_more"] is True


def test_messages_keep_order_and_timestamps(admin):
    status, conv = _api("POST", "/api/chat/conversations", {"title": "WCT order"}, admin)
    assert status == 200
    cid = conv["id"]
    for i in (1, 2):
        status, r = _api("POST", "/api/chat",
                         {"conversation_id": cid,
                          "message": f"WCT-OWNER-A order {i}",
                          "client_msg_id": f"wct-order-{i}"}, admin)
        assert status == 200, r

    status, msgs = _api("GET", f"/api/chat/conversations/{cid}/messages", token=admin)
    assert status == 200
    items = msgs["items"]
    assert [m["role"] for m in items] == ["user", "assistant", "user", "assistant"]
    ids = [m["id"] for m in items]
    assert ids == sorted(ids) and len(set(ids)) == 4, "message ids must be unique + ordered"
    stamps = [m["created_at"] for m in items]
    assert all(stamps) and stamps == sorted(stamps), "created_at must be present + monotonic"
    assert all(m["conversation_id"] == cid for m in items)


def test_validation_rejects_bad_input(admin):
    assert _api("POST", "/api/chat", {"message": ""}, admin)[0] == 400
    assert _api("POST", "/api/chat", {"message": "x" * 4001}, admin)[0] == 400
    assert _api("POST", "/api/chat",
                {"conversation_id": 999_999, "message": "hi"}, admin)[0] == 404

    status, conv = _api("POST", "/api/chat/conversations", {"title": "WCT validate"}, admin)
    assert status == 200
    cid = conv["id"]
    assert _api("PATCH", f"/api/chat/conversations/{cid}", {"title": "   "}, admin)[0] == 400
    assert _api("PATCH", f"/api/chat/conversations/{cid}",
                {"title": "x" * 200}, admin)[0] == 400
    assert _api("PATCH", f"/api/chat/conversations/{cid}", {}, admin)[0] == 400
    # direct fail() from a route case must still map to a real status
    # (regression: a bare {__error_status} used to answer 200 "undefined")
    assert _api("POST", f"/api/chat/conversations/{cid}", {"title": "x"}, admin)[0] == 405


def test_owner_rename_works_and_persists(admin):
    status, conv = _api("POST", "/api/chat/conversations", {"title": "Before"}, admin)
    assert status == 200
    cid = conv["id"]
    status, renamed = _api("PATCH", f"/api/chat/conversations/{cid}",
                           {"title": "After rename"}, admin)
    assert status == 200 and renamed["title"] == "After rename", renamed

    status, listing = _api("GET", "/api/chat/conversations?limit=100", token=admin)
    assert status == 200
    titles = {c["id"]: c["title"] for c in listing["items"]}
    assert titles.get(cid) == "After rename"


def test_delete_removes_conversation_and_messages(admin):
    status, conv = _api("POST", "/api/chat/conversations", {"title": "WCT delete"}, admin)
    assert status == 200
    cid = conv["id"]
    status, r = _api("POST", "/api/chat",
                     {"conversation_id": cid, "message": "WCT-OWNER-A delete me",
                      "client_msg_id": "wct-del-1"}, admin)
    assert status == 200, r

    status, r = _api("DELETE", f"/api/chat/conversations/{cid}", token=admin)
    assert status == 200 and r.get("deleted") == cid, r
    assert _api("GET", f"/api/chat/conversations/{cid}/messages", token=admin)[0] == 404
    status, listing = _api("GET", "/api/chat/conversations?limit=100", token=admin)
    assert cid not in {c["id"] for c in listing["items"]}


def test_clear_history_only_affects_the_caller(admin, pharmacist):
    status, a_conv = _api("POST", "/api/chat/conversations", {"title": "WCT mine"}, admin)
    assert status == 200
    status, _ = _api("POST", "/api/chat",
                     {"conversation_id": a_conv["id"],
                      "message": "WCT-OWNER-A clear", "client_msg_id": "wct-clear-a"},
                     admin)
    assert status == 200
    status, b_conv = _api("POST", "/api/chat/conversations", {"title": "WCT theirs"}, pharmacist)
    assert status == 200
    status, _ = _api("POST", "/api/chat",
                     {"conversation_id": b_conv["id"],
                      "message": "WCT-OWNER-B keep", "client_msg_id": "wct-clear-b"},
                     pharmacist)
    assert status == 200

    status, r = _api("DELETE", "/api/chat/history", token=admin)
    assert status == 200 and r.get("ok") is True, r

    status, a_list = _api("GET", "/api/chat/conversations", token=admin)
    assert status == 200 and a_list["total"] == 0, "admin history must be cleared"

    status, b_list = _api("GET", "/api/chat/conversations?limit=100", token=pharmacist)
    assert status == 200
    assert b_conv["id"] in {c["id"] for c in b_list["items"]}, \
        "clearing one user must never touch another user's history"
    status, b_msgs = _api("GET", f"/api/chat/conversations/{b_conv['id']}/messages",
                          token=pharmacist)
    assert status == 200 and len(b_msgs["items"]) == 2


def test_chat_rows_replicate_to_secondary(worker, admin, pharmacist):
    """Chat writes land on BOTH independent D1 databases, with correct owners."""
    status, a = _api("POST", "/api/chat",
                     {"message": "WCT-OWNER-A repl", "client_msg_id": "wct-repl-a"}, admin)
    assert status == 200, a
    status, b = _api("POST", "/api/chat",
                     {"message": "WCT-OWNER-B repl", "client_msg_id": "wct-repl-b"},
                     pharmacist)
    assert status == 200, b

    status, run = _api("POST", "/api/sync/run", token=admin)
    assert status == 200, run
    assert run.get("pending") == 0, f"outbox not drained: {run}"

    sql = ("SELECT (SELECT COUNT(*) FROM chat_conversations) AS convs, "
           "(SELECT COUNT(*) FROM chat_messages) AS msgs, "
           "(SELECT COUNT(DISTINCT user_id) FROM chat_messages "
           " WHERE content LIKE 'WCT-OWNER-%') AS owners")
    primary = _d1_query(worker, "pharmacy-primary", sql)
    secondary = None
    for _ in range(3):  # a waitUntil drain may still be committing concurrently
        secondary = _d1_query(worker, "pharmacy-secondary", sql)
        if secondary == primary:
            break
        time.sleep(2)

    assert primary["owners"] == 2, \
        f"messages must be stored under two distinct user ids: {primary}"
    assert secondary == primary, \
        f"secondary diverged from primary: {secondary} != {primary}"
