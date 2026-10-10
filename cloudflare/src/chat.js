/**
 * Per-user chatbot storage: chat_conversations + chat_messages.
 *
 * Ownership is server-enforced: EVERY read/write carries `user_id = ?` from
 * the verified session token — conversation ids from the browser are never
 * trusted alone (prevents IDOR / cross-user history leakage).
 *
 * Idempotent retries: the client may send a `client_msg_id` (UUID). A retry
 * with the same id re-inserts nothing (UNIQUE(conversation_id, client_msg_id))
 * and returns the already-stored assistant reply if one exists — an
 * interrupted response can be retried without duplicate messages.
 */
import { all, first, writeMany, write } from "./db.js";

export const MAX_MESSAGE_CHARS = 4000;
const MAX_TITLE_CHARS = 80;
const CONV_PAGE = 20;   // default conversations per page
const MSG_PAGE = 50;    // default messages per page

/* Per-isolate send throttle: userId → timestamps of recent sends. */
const SEND_WINDOW_MS = 60_000;
const SEND_MAX = 20;
const sendTimes = new Map();

export class ChatError extends Error {
  constructor(status, detail) { super(detail); this.status = status; this.detail = detail; }
}

function checkSendRate(userId) {
  const now = Date.now();
  const hits = (sendTimes.get(userId) || []).filter((t) => now - t < SEND_WINDOW_MS);
  if (hits.length >= SEND_MAX) throw new ChatError(429, "too many messages — wait a moment");
  hits.push(now);
  sendTimes.set(userId, hits);
}

function clamp(n, min, max, dflt) {
  // Absent params arrive as `null` from URLSearchParams.get() — Number(null)
  // is 0 (finite!), which would silently turn an omitted `limit` into LIMIT 1.
  // Treat null/undefined/"" as "not provided" and use the default page size.
  if (n === null || n === undefined || n === "") return dflt;
  const v = Number(n);
  return Number.isFinite(v) ? Math.min(max, Math.max(min, Math.trunc(v))) : dflt;
}

/* ------------------------------- reads ---------------------------------- */

/** The user's conversations, newest activity first (paginated). */
export async function listConversations(session, userId, { limit, offset } = {}) {
  const lim = clamp(limit, 1, 100, CONV_PAGE);
  const off = clamp(offset, 0, 1_000_000, 0);
  const rows = await all(session,
    `SELECT id, title, created_at, updated_at, archived_at,
            (SELECT COUNT(*) FROM chat_messages m WHERE m.conversation_id = c.id) AS message_count
     FROM chat_conversations c WHERE user_id = ? AND archived_at IS NULL
     ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?`, [userId, lim, off]);
  const total = await first(session,
    "SELECT COUNT(*) AS n FROM chat_conversations WHERE user_id = ? AND archived_at IS NULL", [userId]);
  return { items: rows, total: total?.n || 0, limit: lim, offset: off };
}

/** Ownership-checked fetch: null when the id is someone else's or missing. */
export async function getConversation(session, userId, id) {
  const convId = Number(id);
  if (!convId || convId <= 0) return null;
  return first(session,
    "SELECT * FROM chat_conversations WHERE id = ? AND user_id = ?", [convId, userId]);
}

/** Messages of ONE owned conversation, oldest-first page (paginated). */
export async function listMessages(session, userId, convId, { limit, before } = {}) {
  const conv = await getConversation(session, userId, convId);
  if (!conv) throw new ChatError(404, "conversation not found");
  const lim = clamp(limit, 1, 200, MSG_PAGE);
  const beforeId = clamp(before, 0, Number.MAX_SAFE_INTEGER, 0);
  const rows = await all(session,
    `SELECT id, conversation_id, role, content, client_msg_id, metadata, created_at
     FROM chat_messages WHERE conversation_id = ? AND user_id = ? AND (? = 0 OR id < ?)
     ORDER BY id DESC LIMIT ?`, [conv.id, userId, beforeId, beforeId, lim]);
  return { items: rows.reverse(), has_more: rows.length === lim, limit: lim };
}

/** Backward-compatible flat history (recent messages across conversations). */
export async function recentHistory(session, userId, limit = 50) {
  const lim = clamp(limit, 1, 200, 50);
  const rows = await all(session,
    `SELECT id, conversation_id, role, content AS message, created_at
     FROM chat_messages WHERE user_id = ? ORDER BY id DESC LIMIT ?`, [userId, lim]);
  return rows.reverse();
}

/* ------------------------------- writes --------------------------------- */

function titleFrom(text) {
  const t = String(text).trim().replace(/\s+/g, " ");
  return (t.length > MAX_TITLE_CHARS ? t.slice(0, MAX_TITLE_CHARS - 1) + "…" : t) || "New chat";
}

export async function createConversation(session, userId, title) {
  const t = title ? String(title).trim().slice(0, MAX_TITLE_CHARS) : "New chat";
  const r = await write(session,
    "INSERT INTO chat_conversations(user_id, title, created_at, updated_at) VALUES(?,?,datetime('now'),datetime('now'))",
    [userId, t || "New chat"]);
  const id = r.result?.meta?.last_row_id;
  return id ? { id, user_id: userId, title: t || "New chat" } : null;
}

export async function renameConversation(session, userId, id, title) {
  const t = String(title || "").trim();
  if (!t || t.length > MAX_TITLE_CHARS) throw new ChatError(400, `title must be 1-${MAX_TITLE_CHARS} characters`);
  const conv = await getConversation(session, userId, id);
  if (!conv) throw new ChatError(404, "conversation not found");
  await write(session,
    "UPDATE chat_conversations SET title = ?, updated_at = datetime('now') WHERE id = ? AND user_id = ?",
    [t, conv.id, userId]);
  return { ...conv, title: t };
}

export async function archiveConversation(session, userId, id) {
  const conv = await getConversation(session, userId, id);
  if (!conv) throw new ChatError(404, "conversation not found");
  await write(session,
    "UPDATE chat_conversations SET archived_at = datetime('now') WHERE id = ? AND user_id = ?",
    [conv.id, userId]);
  return { ...conv, archived_at: true };
}

/** Delete a conversation AND its messages in ONE atomic batch. */
export async function deleteConversation(session, userId, id) {
  const conv = await getConversation(session, userId, id);
  if (!conv) throw new ChatError(404, "conversation not found");
  await writeMany(session, [
    { sql: "DELETE FROM chat_messages WHERE conversation_id = ? AND user_id = ?", params: [conv.id, userId] },
    { sql: "DELETE FROM chat_conversations WHERE id = ? AND user_id = ?", params: [conv.id, userId] },
  ]);
  return { ok: true, deleted: conv.id };
}

/** Clear ALL of the user's chat history (both tables, one atomic batch). */
export async function clearHistory(session, userId) {
  const convs = await all(session, "SELECT id FROM chat_conversations WHERE user_id = ?", [userId]);
  const ops = [{ sql: "DELETE FROM chat_messages WHERE user_id = ?", params: [userId] },
    { sql: "DELETE FROM chat_conversations WHERE user_id = ?", params: [userId] }];
  await writeMany(session, ops);
  return { ok: true, conversations: convs.length };
}

/**
 * Send flow (idempotent):
 *   1. validate + throttle
 *   2. resolve/create the owned conversation
 *   3. store the user message (INSERT OR IGNORE on client_msg_id)
 *   4. reuse the stored assistant reply when this is a retry
 *   5. otherwise generate + store the assistant reply
 * `respond(message)` is injected by the caller (avoids module cycles).
 */
export async function postMessage(session, userId, body, respond) {
  const text = String(body?.message ?? "").trim();
  if (!text) throw new ChatError(400, "message is required");
  if (text.length > MAX_MESSAGE_CHARS) {
    throw new ChatError(400, `message must be at most ${MAX_MESSAGE_CHARS} characters`);
  }
  checkSendRate(userId);

  const clientId = String(body?.client_msg_id || crypto.randomUUID()).slice(0, 64);

  let conv;
  if (body?.conversation_id) {
    conv = await getConversation(session, userId, body.conversation_id);
    if (!conv) throw new ChatError(404, "conversation not found");
  } else {
    // A retry may arrive without a conversation id (interrupted response,
    // lost client state). Reuse the conversation that already holds this
    // client_msg_id so a retry can never fork a duplicate thread or store a
    // second copy of the same user message.
    const prior = await first(session,
      "SELECT conversation_id FROM chat_messages WHERE user_id = ? AND client_msg_id = ? LIMIT 1",
      [userId, clientId]);
    conv = prior ? await getConversation(session, userId, prior.conversation_id) : null;
    if (!conv) {
      conv = await createConversation(session, userId, titleFrom(text));
      if (!conv) throw new ChatError(500, "could not create conversation");
    }
  }

  // 3. user message — duplicate-safe
  const userInsert = await write(session,
    "INSERT OR IGNORE INTO chat_messages(conversation_id, user_id, role, content, client_msg_id, created_at) " +
    "VALUES(?,?,'user',?,?,datetime('now'))", [conv.id, userId, text, clientId]);
  const isNewUserMsg = (userInsert.result?.meta?.changes ?? 1) > 0;

  // 4. retry with an already-answered client id → return the stored reply
  const assistantKey = `${clientId}:a`;
  const existingReply = await first(session,
    "SELECT content, metadata, id, created_at FROM chat_messages " +
    "WHERE conversation_id = ? AND user_id = ? AND client_msg_id = ?",
    [conv.id, userId, assistantKey]);
  if (existingReply) {
    return {
      text: existingReply.content, cards: parseCards(existingReply.metadata),
      conversation_id: conv.id, user_message_id: null, assistant_message_id: existingReply.id,
      duplicate: !isNewUserMsg,
    };
  }

  // 5. generate (user message is already durable — safe to retry on failure)
  let reply;
  try {
    reply = await respond(text);
  } catch (e) {
    throw new ChatError(502, "assistant generation failed — retry with the same message");
  }
  const assistantText = String(reply?.text ?? "").trim() || "Sorry, I could not produce a response.";
  const metadata = reply?.cards?.length ? JSON.stringify({ cards: reply.cards }) : null;

  let assistantRow = await first(session,
    "SELECT id FROM chat_messages WHERE conversation_id = ? AND user_id = ? AND client_msg_id = ?",
    [conv.id, userId, assistantKey]);
  if (!assistantRow) {
    const ins = await write(session,
      "INSERT OR IGNORE INTO chat_messages(conversation_id, user_id, role, content, client_msg_id, metadata, created_at) " +
      "VALUES(?,?,'assistant',?,?,?,datetime('now'))",
      [conv.id, userId, assistantText, assistantKey, metadata]);
    assistantRow = { id: ins.result?.meta?.last_row_id ?? null };
    // bump activity; keep the user's title unless it is still the default
    await write(session,
      "UPDATE chat_conversations SET updated_at = datetime('now'), " +
      "title = CASE WHEN title = 'New chat' THEN ? ELSE title END WHERE id = ? AND user_id = ?",
      [titleFrom(text), conv.id, userId]);
  }

  return {
    text: assistantText, cards: reply?.cards || [],
    conversation_id: conv.id, assistant_message_id: assistantRow?.id ?? null,
    duplicate: false,
  };
}

function parseCards(metadata) {
  if (!metadata) return [];
  try { return JSON.parse(metadata)?.cards || []; } catch { return []; }
}
