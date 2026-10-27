"""Canonical security DDL installed in PostgreSQL through Alembic."""

STATEMENTS = (
    """CREATE TABLE admin_security_lock (lock_id INTEGER PRIMARY KEY, revision BIGINT NOT NULL)""",
    """CREATE TABLE admin_accounts (
        account_id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('admin','member')),
        qq_id TEXT UNIQUE, enabled INTEGER NOT NULL DEFAULT 1,
        version BIGINT NOT NULL DEFAULT 1, created_at BIGINT NOT NULL,
        CHECK (role <> 'admin' OR qq_id IS NOT NULL))""",
    """CREATE TABLE admin_sessions (
        session_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES admin_accounts(account_id),
        account_version BIGINT NOT NULL, csrf_hash TEXT NOT NULL,
        expires_at BIGINT NOT NULL, created_at BIGINT NOT NULL)""",
    """CREATE TABLE admin_approvals (
        approval_id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES admin_accounts(account_id),
        account_version BIGINT NOT NULL, qq_id TEXT NOT NULL, bot_id TEXT NOT NULL,
        session_hash TEXT NOT NULL, kind TEXT NOT NULL,
        payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL, summary TEXT NOT NULL,
        code_hash TEXT NOT NULL, code_generation BIGINT NOT NULL DEFAULT 1,
        attempts INTEGER NOT NULL DEFAULT 0, expires_at BIGINT NOT NULL,
        status TEXT NOT NULL CHECK (status IN
            ('sending','pending','queued','executing','succeeded','failed','expired',
             'cancelled','locked','delivery_failed','needs_attention')),
        result_json TEXT NOT NULL DEFAULT '{}', created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL, consumed_at BIGINT, execution_owner TEXT NOT NULL DEFAULT '',
        lease_until BIGINT NOT NULL DEFAULT 0, notice_sent INTEGER NOT NULL DEFAULT 0)""",
    "CREATE INDEX ix_admin_approvals_queue ON admin_approvals(status, created_at)",
    "CREATE INDEX ix_admin_approvals_account ON admin_approvals(account_id, created_at)",
    """CREATE TABLE admin_approval_references (
        reference TEXT PRIMARY KEY, approval_id TEXT NOT NULL REFERENCES admin_approvals(approval_id))""",
    """CREATE TABLE admin_fleet_watches (
        path TEXT PRIMARY KEY, account_json TEXT NOT NULL, bot_id TEXT NOT NULL,
        actor TEXT NOT NULL, origin TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'watching',
        updated_at BIGINT NOT NULL, lease_until BIGINT NOT NULL DEFAULT 0)""",
    """CREATE TABLE admin_approval_codes (
        approval_id TEXT NOT NULL REFERENCES admin_approvals(approval_id),
        code_fingerprint TEXT NOT NULL, PRIMARY KEY (approval_id, code_fingerprint))""",
    """CREATE TABLE admin_rate_limits (
        bucket TEXT PRIMARY KEY, window_start BIGINT NOT NULL, hits INTEGER NOT NULL)""",
    """CREATE TABLE admin_security_audit (
        audit_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, action TEXT NOT NULL,
        target TEXT NOT NULL, detail TEXT NOT NULL, created_at BIGINT NOT NULL)""",
)
