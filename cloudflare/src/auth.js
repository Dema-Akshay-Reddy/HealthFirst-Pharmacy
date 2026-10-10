/**
 * Sessions & roles on the Worker (mirrors pharmacy/auth.py):
 *   admin       — full API access
 *   pharmacist  — counter + shelf workflow + personal chatbot
 * Tokens are HMAC-signed (secret in settings table, fallback SYNC_SECRET env)
 * and sent by the SPA as X-Session-Token.
 *
 * Password storage: PBKDF2-HMAC-SHA-256, 100k iterations, 16-byte random
 * per-user salt (`pbkdf2$100000$salt$dk`). Legacy rows (`sha256$salt$hash`)
 * still verify and are transparently upgraded to PBKDF2 at next login.
 * Token payload carries the internal user id (`i`) so chat ownership never
 * depends on client-supplied identity; legacy tokens resolve the id server-side.
 */
import { all, first, scalar, write } from "./db.js";

const ROLES = {
  admin: null, // null = everything
  pharmacist: [
    ["GET", "/api/meta"], ["GET", "/api/drugs"], ["GET", "/api/batches"],
    ["GET", "/api/expiry"], ["GET", "/api/alerts"], ["GET", "/api/shelf"],
    ["GET", "/api/shelf/tasks"], ["POST", "/api/shelf/tasks"],
    ["GET", "/api/counter"], ["POST", "/api/prescriptions"], ["POST", "/api/dispense"],
    ["GET", "/api/invoices"], ["GET", "/api/forecast/seasonal"],
    // personal chatbot: every authenticated user gets their own conversations
    ["GET", "/api/chat"], ["POST", "/api/chat"],
    ["PATCH", "/api/chat"], ["DELETE", "/api/chat"],
  ],
};

/* ------------------------------ password hashing ------------------------ */

const PBKDF2_ITERATIONS = 100_000;

function hex(bytes) {
  return [...new Uint8Array(bytes)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function pbkdf2(password, saltBytes, iterations) {
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", salt: saltBytes, iterations, hash: "SHA-256" }, key, 256);
  return new Uint8Array(bits);
}

/** Constant-time string compare — avoids leaking digests via timing. */
function ctEqual(a, b) {
  const x = String(a), y = String(b);
  if (x.length !== y.length) return false;
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x.charCodeAt(i) ^ y.charCodeAt(i);
  return diff === 0;
}

/** New password hash: `pbkdf2$100000$<salt-hex>$<dk-hex>`. */
export async function hashPassword(password) {
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const dk = await pbkdf2(String(password ?? ""), salt, PBKDF2_ITERATIONS);
  return `pbkdf2$${PBKDF2_ITERATIONS}$${hex(salt)}$${hex(dk)}`;
}

/** Legacy single-round hash — kept only to verify old rows. */
export async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function makeHash(password, salt = "pharmacy-salt-v1") {
  return `sha256$${salt}$${await sha256Hex(salt + ":" + password)}`;
}

/** Verify a stored hash (either format) against a candidate password. */
async function verifyPassword(stored, password) {
  const parts = String(stored || "").split("$");
  if (parts[0] === "pbkdf2" && parts.length === 4) {
    const [, iterStr, saltHex, dkHex] = parts;
    const iterations = Number(iterStr) || PBKDF2_ITERATIONS;
    try {
      const saltBytes = new Uint8Array(saltHex.match(/.{2}/g).map((h) => parseInt(h, 16)));
      const dk = await pbkdf2(String(password ?? ""), saltBytes, iterations);
      return ctEqual(hex(dk), dkHex);
    } catch { return false; }
  }
  if (parts[0] === "sha256" && parts.length === 3) {
    const [, salt, digest] = parts;
    return ctEqual(await sha256Hex(salt + ":" + String(password ?? "")), digest);
  }
  return false;
}

/* ------------------------------ bootstrap ------------------------------- */

let usersEnsured = false; // per-isolate: seed check runs at most once

export async function ensureUsers(session, env) {
  if (usersEnsured) return;
  if (await scalar(session, "SELECT COUNT(*) FROM users")) { usersEnsured = true; return; }
  // Seeded credentials come from Worker env/secrets — never from source code.
  // With no secrets configured (fail-closed) no accounts exist and login is
  // impossible until an admin provisions users via POST /api/users.
  const adminPassword = env?.ADMIN_PASSWORD;
  const pharmacistPassword = env?.PHARMACIST_PASSWORD;
  if (adminPassword) {
    await write(session,
      "INSERT OR IGNORE INTO users(username, password_hash, role, name, created_at) VALUES(?,?,?,?,datetime('now'))",
      ["admin", await hashPassword(adminPassword), "admin", "Administrator"]);
  }
  if (pharmacistPassword) {
    await write(session,
      "INSERT OR IGNORE INTO users(username, password_hash, role, name, created_at) VALUES(?,?,?,?,datetime('now'))",
      ["pharmacist", await hashPassword(pharmacistPassword), "pharmacist", "Pharmacist"]);
  }
  usersEnsured = true;
}

/* -------------------------------- tokens -------------------------------- */

async function secret(session) {
  let s = (await first(session, "SELECT value FROM settings WHERE key = 'session_secret'"))?.value;
  if (!s) {
    s = crypto.randomUUID().replace(/-/g, "") + crypto.randomUUID().replace(/-/g, "");
    await write(session,
      "INSERT INTO settings(key, value, updated_at) VALUES('session_secret',?,datetime('now')) " +
      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", [s]);
  }
  return s;
}

async function hmacHex(payload, key) {
  const cryptoKey = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(key), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", cryptoKey, new TextEncoder().encode(payload));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** Tokens carry { u, r, i, exp } — `i` is the internal users.id. */
export async function makeToken(session, username, role, userId = null) {
  const payload = btoa(JSON.stringify(
    { u: username, r: role, i: userId, exp: Date.now() + 12 * 3600 * 1000 })).replace(/=+$/, "");
  return `${payload}.${await hmacHex(payload, await secret(session))}`;
}

export async function parseToken(session, token) {
  if (!token || !token.includes(".")) return null;
  const [payload, sig] = token.split(".");
  if (!ctEqual(await hmacHex(payload, await secret(session)), sig)) return null;
  try {
    const data = JSON.parse(atob(payload));
    return data.exp > Date.now() ? data : null;
  } catch { return null; }
}

/** Internal user id for a verified session user (legacy tokens → DB lookup). */
const userIdCache = new WeakMap(); // session → Map<username, id>
export async function resolveUserId(session, user) {
  if (!user?.u) return null;
  if (user.i) return user.i;
  let m = userIdCache.get(session);
  if (!m) { m = new Map(); userIdCache.set(session, m); }
  if (m.has(user.u)) return m.get(user.u);
  const id = await scalar(session, "SELECT id FROM users WHERE username = ?", [user.u], null);
  if (id) m.set(user.u, id);
  return id;
}

/* --------------------------------- login -------------------------------- */

// Per-isolate throttle: username+client → recent failure timestamps.
const LOGIN_WINDOW_MS = 5 * 60_000;
const LOGIN_MAX_ATTEMPTS = 10;
const loginAttempts = new Map();

function rateKey(username, ip) {
  return `${(username || "").toLowerCase()}|${ip || "?"}`;
}

function rateLimited(key) {
  const now = Date.now();
  const hits = (loginAttempts.get(key) || []).filter((t) => now - t < LOGIN_WINDOW_MS);
  loginAttempts.set(key, hits);
  return hits.length >= LOGIN_MAX_ATTEMPTS;
}

function recordFailure(key) {
  const hits = loginAttempts.get(key) || [];
  hits.push(Date.now());
  loginAttempts.set(key, hits);
}

function clientIp(request) {
  return request?.headers?.get("CF-Connecting-IP") || "local";
}

/**
 * Verify credentials + throttle. Always returns { status, body } so callers
 * map it straight onto the HTTP response:
 *   200 { token, username, role, name }
 *   401 { detail }  bad credentials (message identical for unknown user)
 *   429 { detail }  rate limited
 */
export async function login(session, request, username, password) {
  const key = rateKey(username, clientIp(request));
  if (rateLimited(key)) {
    return { status: 429, body: { detail: "too many login attempts — try again in a few minutes" } };
  }
  const row = await first(session, "SELECT * FROM users WHERE username = ?",
    [(username || "").trim().toLowerCase()]);
  const ok = row ? await verifyPassword(row.password_hash, password || "") : false;
  if (!ok || !row) {
    recordFailure(key);
    // identical message for unknown user vs wrong password (no enumeration)
    return { status: 401, body: { detail: "invalid username or password" } };
  }
  // transparent upgrade of legacy sha256 rows
  if (String(row.password_hash).startsWith("sha256$")) {
    try {
      await write(session, "UPDATE users SET password_hash = ? WHERE id = ?",
        [await hashPassword(password), row.id]);
    } catch { /* upgrade is best-effort — login still succeeds */ }
  }
  loginAttempts.delete(key);
  return {
    status: 200,
    body: {
      token: await makeToken(session, row.username, row.role, row.id),
      username: row.username, role: row.role, name: row.name,
    },
  };
}

/** Returns the session user, or null. 401s are raised by the caller. */
export async function currentUser(session, request) {
  const token = request.headers.get("X-Session-Token")
    || new URL(request.url).searchParams.get("token");
  return token ? parseToken(session, token) : null;
}

/** True when `user` (role) may call `method path` — admin always allowed. */
export function roleAllows(user, method, path) {
  if (!user) return false;
  if (user.r === "admin") return true;
  const rules = ROLES[user.r] || [];
  return rules.some(([m, prefix]) => method === m && path.startsWith(prefix));
}

export { all, first, scalar, write };
