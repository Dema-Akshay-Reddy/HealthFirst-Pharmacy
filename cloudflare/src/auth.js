/**
 * Sessions & roles on the Worker (mirrors pharmacy/auth.py):
 *   admin       — full API access
 *   pharmacist  — counter + shelf workflow only
 * Tokens are HMAC-signed (secret in SYNC_SECRET, auto-generated in KV-less
 * settings table) and sent by the SPA as X-Session-Token.
 */
import { all, first, scalar, write } from "./db.js";

const ROLES = {
  admin: null, // null = everything
  pharmacist: [
    ["GET", "/api/meta"], ["GET", "/api/drugs"], ["GET", "/api/batches"],
    ["GET", "/api/expiry"], ["GET", "/api/alerts"], ["GET", "/api/shelf"],
    ["GET", "/api/shelf/tasks"], ["POST", "/api/shelf/tasks"],
    ["GET", "/api/counter"], ["POST", "/api/prescriptions"], ["POST", "/api/dispense"],
  ],
};

export async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function makeHash(password, salt = "pharmacy-salt-v1") {
  return `sha256$${salt}$${await sha256Hex(salt + ":" + password)}`;
}

export async function ensureUsers(session, env) {
  if (await scalar(session, "SELECT COUNT(*) FROM users")) return;
  // Seeded credentials come from Worker env/secrets — never from source code.
  // With no secrets configured (fail-closed) no accounts exist and login is
  // impossible until an admin provisions users via POST /api/users.
  const adminPassword = env?.ADMIN_PASSWORD;
  const pharmacistPassword = env?.PHARMACIST_PASSWORD;
  if (adminPassword) {
    await write(session,
      "INSERT OR IGNORE INTO users(username, password_hash, role, name, created_at) VALUES(?,?,?,?,datetime('now'))",
      ["admin", await makeHash(adminPassword), "admin", "Administrator"]);
  }
  if (pharmacistPassword) {
    await write(session,
      "INSERT OR IGNORE INTO users(username, password_hash, role, name, created_at) VALUES(?,?,?,?,datetime('now'))",
      ["pharmacist", await makeHash(pharmacistPassword), "pharmacist", "Pharmacist"]);
  }
}

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

export async function makeToken(session, username, role) {
  const payload = btoa(JSON.stringify(
    { u: username, r: role, exp: Date.now() + 12 * 3600 * 1000 })).replace(/=+$/, "");
  return `${payload}.${await hmacHex(payload, await secret(session))}`;
}

export async function parseToken(session, token) {
  if (!token || !token.includes(".")) return null;
  const [payload, sig] = token.split(".");
  if ((await hmacHex(payload, await secret(session))) !== sig) return null;
  try {
    const data = JSON.parse(atob(payload));
    return data.exp > Date.now() ? data : null;
  } catch { return null; }
}

export async function login(session, username, password) {
  const row = await first(session, "SELECT * FROM users WHERE username = ?",
    [(username || "").trim().toLowerCase()]);
  if (!row) return null;
  const [_, salt, digest] = row.password_hash.split("$");
  if ((await makeHash(password || "", salt)) !== `sha256$${salt}$${digest}`) return null;
  return { token: await makeToken(session, row.username, row.role),
    username: row.username, role: row.role, name: row.name };
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
