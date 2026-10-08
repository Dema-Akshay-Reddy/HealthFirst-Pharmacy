"""Users & sessions: admin vs pharmacist login.

Passwords are PBKDF2-SHA256 hashed; sessions are stateless HMAC-signed tokens
(sent by the SPA as `X-Session-Token`, also accepted as `?token=` for plain
browser links). Accounts are provisioned on first boot from env vars
(`PHARMACY_ADMIN_PASSWORD`, `PHARMACIST_PASSWORD`); outside production an
unset variable falls back to the documented local-demo passwords, and in
production unset means no account is seeded (fail-closed).
"""
import base64
import hashlib
import hmac
import json
import os
import time

from . import db

TOKEN_TTL_HOURS = 12
_PBKDF2_ITERATIONS = 60_000

DEFAULT_USERS = [
    # username, password env var, role, display name, local-demo fallback
    ("admin", "PHARMACY_ADMIN_PASSWORD", "admin", "Administrator", "admin123"),
    ("pharmacist", "PHARMACIST_PASSWORD", "pharmacist", "Pharmacist", "pharm123"),
]


def _configured_password(env_name: str, demo: str) -> str | None:
    """Seeded passwords come from the environment; the demo fallback exists
    only outside production — in production an unset variable means the
    account is not seeded (provision via POST /api/users instead)."""
    value = (os.environ.get(env_name) or "").strip()
    if value:
        return value
    if (os.environ.get("PHARMACY_ENV") or "development").strip().lower() == "production":
        return None
    return demo


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"{salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split("$", 1)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt_hex), _PBKDF2_ITERATIONS)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), dk_hex)


def ensure_users() -> None:
    """Create the configured accounts once, so a fresh DB is instantly usable."""
    if db.scalar("SELECT COUNT(*) FROM users"):
        return
    for username, env_name, role, name, demo in DEFAULT_USERS:
        password = _configured_password(env_name, demo)
        if not password:
            continue
        db.execute(
            "INSERT OR IGNORE INTO users(username, password_hash, role, name, created_at) "
            "VALUES(?,?,?,?,?)",
            (username, hash_password(password), role, name, db.now_iso()),
        )


def create_user(username: str, password: str, role: str, name: str = "") -> int:
    return db.execute(
        "INSERT INTO users(username, password_hash, role, name, created_at) VALUES(?,?,?,?,?)",
        (username.strip().lower(), hash_password(password), role,
         name.strip() or username.strip().lower(), db.now_iso()),
    )


def _secret() -> str:
    """Token-signing secret, generated once and persisted in settings."""
    s = db.get_setting("session_secret")
    if not s:
        s = os.urandom(32).hex()
        db.set_setting("session_secret", s)
    return s


def make_token(username: str, role: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(
        {"u": username, "r": role, "exp": int(time.time()) + TOKEN_TTL_HOURS * 3600},
        separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def parse_token(token: str) -> dict | None:
    """Return {u, r, exp} for a valid, unexpired token; None otherwise."""
    if not token or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    expected = hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("exp", 0) < time.time():
        return None
    return data


def login(username: str, password: str) -> dict | None:
    row = db.one("SELECT * FROM users WHERE username=?", ((username or "").strip().lower(),))
    if not row or not verify_password(password or "", row["password_hash"]):
        return None
    return dict(token=make_token(row["username"], row["role"]),
                username=row["username"], role=row["role"], name=row["name"])
