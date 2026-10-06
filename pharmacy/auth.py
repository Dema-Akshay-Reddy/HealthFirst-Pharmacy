"""Users & sessions: admin vs pharmacist login.

Passwords are PBKDF2-SHA256 hashed; sessions are stateless HMAC-signed tokens
(sent by the SPA as `X-Session-Token`, also accepted as `?token=` for plain
browser links). Two demo accounts are provisioned on first boot:
  admin / admin123       — full access to every screen and dataset
  pharmacist / pharm123  — counter + shelf workflow only
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
    ("admin", "admin123", "admin", "Administrator"),
    ("pharmacist", "pharm123", "pharmacist", "Pharmacist"),
]


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
    """Create the demo accounts once, so a fresh DB is instantly usable."""
    if db.scalar("SELECT COUNT(*) FROM users"):
        return
    for username, password, role, name in DEFAULT_USERS:
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
