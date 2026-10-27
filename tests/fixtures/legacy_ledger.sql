-- Frozen pre-PostgreSQL schema; used only by legacy import acceptance tests.
PRAGMA foreign_keys = ON;
CREATE TABLE conversations (
    conversation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key TEXT NOT NULL UNIQUE,
    platform TEXT NOT NULL,
    kind TEXT NOT NULL,
    native_conversation_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE principals (
    principal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE principal_identities (
    identity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    principal_id INTEGER NOT NULL REFERENCES principals(principal_id),
    platform TEXT NOT NULL,
    native_user_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    last_seen_at INTEGER NOT NULL,
    UNIQUE(platform, native_user_id)
);
CREATE TABLE messages (
    canonical_message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(conversation_id),
    native_message_id TEXT,
    sender_identity_id INTEGER REFERENCES principal_identities(identity_id),
    sender_principal_id INTEGER REFERENCES principals(principal_id),
    sender_native_user_id TEXT NOT NULL,
    sender_display TEXT NOT NULL,
    direction TEXT NOT NULL,
    message_kind TEXT NOT NULL DEFAULT 'chat',
    body_json TEXT NOT NULL,
    rendered_text TEXT NOT NULL,
    occurred_at INTEGER NOT NULL,
    reply_to_native_message_id TEXT,
    reply_to_canonical_message_id INTEGER REFERENCES messages(canonical_message_id),
    raw_event_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(conversation_id, native_message_id)
);
CREATE TABLE conversation_visibility (
    conversation_id INTEGER PRIMARY KEY REFERENCES conversations(conversation_id),
    min_canonical_message_id INTEGER NOT NULL DEFAULT 1,
    cleared_at INTEGER
);
