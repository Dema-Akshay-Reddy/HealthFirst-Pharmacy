-- Migration: per-user chatbot storage (chat_conversations + chat_messages).
--
-- Apply to BOTH databases:
--   npx wrangler d1 execute pharmacy-primary   --remote --file=./migrations/0002_chat.sql
--   npx wrangler d1 execute pharmacy-secondary --remote --file=./migrations/0002_chat.sql
--
-- All statements are idempotent (IF NOT EXISTS) and additive: no existing
-- table, column, index, or row is modified or dropped. The Worker also runs
-- this DDL lazily per isolate (src/replication.js ensureSchemaPrimary), so a
-- code-before-migration deployment order cannot break the chat feature.

CREATE TABLE IF NOT EXISTS chat_conversations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  title TEXT NOT NULL DEFAULT 'New chat',
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now')),
  archived_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_chat_conversations_user
ON chat_conversations(user_id, updated_at);

CREATE TABLE IF NOT EXISTS chat_messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id INTEGER NOT NULL REFERENCES chat_conversations(id),
  user_id INTEGER NOT NULL REFERENCES users(id),
  role TEXT NOT NULL CHECK(role IN ('user','assistant')),
  content TEXT NOT NULL,
  client_msg_id TEXT,
  metadata TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  UNIQUE(conversation_id, client_msg_id)
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_conv
ON chat_messages(conversation_id, id);

CREATE INDEX IF NOT EXISTS idx_chat_messages_user
ON chat_messages(user_id, id);
