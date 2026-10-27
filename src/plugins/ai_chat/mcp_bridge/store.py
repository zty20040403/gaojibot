"""CAS catalog administration and hashed, revocable per-group capabilities."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import re
import secrets
import threading
import time

from src.bot_storage import DatabaseSource, open_store_connection
from .contracts import MAX_TOOLS, SERVER_NAME, canonical, fingerprint, group_scope, server_config, tool_definition, tool_name


class McpConflict(ValueError):
    pass


class McpDenied(PermissionError):
    pass


class McpRateLimited(McpDenied):
    pass


class McpStore:
    def __init__(self, source: DatabaseSource, *, clock=time.time):
        _, self._connection = open_store_connection(source)
        self._lock = threading.RLock()
        self.clock = clock

    def close(self):
        with self._lock:
            self._connection.close()

    @contextmanager
    def transaction(self):
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute("BEGIN")
                cursor.execute("SELECT pg_advisory_xact_lock(hashtext('mcp-config'))")
                yield cursor
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
            finally:
                cursor.close()

    @staticmethod
    def _admin(db, account):
        row = db.execute("SELECT * FROM admin_accounts WHERE account_id=?", (account["account_id"],)).fetchone()
        if not row or not row["enabled"] or row["role"] != "admin" or row["version"] != account["version"]:
            raise McpDenied("MCP changes require a current administrator")

    @staticmethod
    def _version(row, expected_version):
        if type(expected_version) is not int or expected_version != (row["version"] if row else 0):
            raise McpConflict("MCP resource changed; refresh before editing")

    @staticmethod
    def _server(row):
        if row is None:
            return None
        return {"name": row["name"], "config": json.loads(row["config_json"]),
                "scopes": json.loads(row["scopes_json"]), "enabled": row["enabled"],
                "version": row["version"], "updated_at": row["updated_at"]}

    @staticmethod
    def _tool(row):
        return {"name": tool_name(row["server_name"], row["remote_name"]),
                "server": row["server_name"], "remote_name": row["remote_name"],
                "definition": json.loads(row["definition_json"]), "fingerprint": row["fingerprint"],
                "present": row["present"], "enabled": row["enabled"], "trust": row["trust"],
                "version": row["version"]}

    def server(self, name):
        with self._lock:
            return self._server(self._connection.execute("SELECT * FROM mcp_servers WHERE name=?", (name,)).fetchone())

    def servers(self):
        with self._lock:
            return [self._server(row) for row in self._connection.execute("SELECT * FROM mcp_servers ORDER BY name").fetchall()]

    def configure(self, account, name, *, config, scopes, enabled, expected_version):
        if not isinstance(name, str) or not SERVER_NAME.fullmatch(name) or type(enabled) is not bool:
            raise ValueError("Invalid MCP server name or enabled flag")
        config = server_config(config)
        if not isinstance(scopes, list) or not 1 <= len(scopes) <= 50:
            raise ValueError("Choose 1-50 explicit MCP group scopes")
        scopes = sorted({group_scope(scope).key for scope in scopes})
        encoded, scope_json = canonical(config), canonical(scopes)
        now = int(self.clock())
        with self.transaction() as db:
            self._admin(db, account)
            old = db.execute("SELECT * FROM mcp_servers WHERE name=? FOR UPDATE", (name,)).fetchone()
            self._version(old, expected_version)
            if old is None and db.execute("SELECT COUNT(*) FROM mcp_servers").fetchone()[0] >= 32:
                raise ValueError("At most 32 MCP servers may be registered")
            db.execute("""INSERT INTO mcp_servers(name,config_json,scopes_json,enabled,version,updated_by,updated_at)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET config_json=excluded.config_json,
                scopes_json=excluded.scopes_json,enabled=excluded.enabled,version=excluded.version,
                updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
                (name, encoded, scope_json, enabled if old else False, expected_version + 1, account["account_id"], now))
            if old and (old["config_json"] != encoded or old["scopes_json"] != scope_json):
                db.execute("""UPDATE mcp_tools SET enabled=FALSE,trust='approval',present=FALSE,
                    version=version+1,updated_at=? WHERE server_name=?""", (now, name))
            return self._server(db.execute("SELECT * FROM mcp_servers WHERE name=?", (name,)).fetchone())

    def replace_catalog(self, account, name, definitions, *, expected_version):
        if not isinstance(definitions, list) or len(definitions) > MAX_TOOLS:
            raise ValueError("MCP catalog exceeds 64 tools")
        definitions = [tool_definition(value) for value in definitions]
        if len({value["name"] for value in definitions}) != len(definitions):
            raise ValueError("Duplicate MCP tool name")
        now = int(self.clock())
        with self.transaction() as db:
            self._admin(db, account)
            server = db.execute("SELECT * FROM mcp_servers WHERE name=? FOR UPDATE", (name,)).fetchone()
            if server is None:
                raise McpConflict("MCP server was removed")
            self._version(server, expected_version)
            old = {row["remote_name"]: dict(row) for row in db.execute("SELECT * FROM mcp_tools WHERE server_name=?", (name,)).fetchall()}
            names = set()
            for definition in definitions:
                remote, digest = definition["name"], fingerprint(definition)
                names.add(remote)
                previous = old.get(remote)
                if previous and previous["present"] and previous["fingerprint"] == digest:
                    continue
                db.execute("""INSERT INTO mcp_tools(server_name,remote_name,definition_json,fingerprint,
                    present,enabled,trust,version,updated_at) VALUES (?,?,?,?,TRUE,FALSE,'approval',?,?)
                    ON CONFLICT(server_name,remote_name) DO UPDATE SET definition_json=excluded.definition_json,
                    fingerprint=excluded.fingerprint,present=TRUE,enabled=FALSE,trust='approval',
                    version=excluded.version,updated_at=excluded.updated_at""",
                    (name, remote, canonical(definition), digest, previous["version"] + 1 if previous else 1, now))
            for remote, previous in old.items():
                if remote not in names and previous["present"]:
                    db.execute("""UPDATE mcp_tools SET present=FALSE,enabled=FALSE,trust='approval',
                        version=version+1,updated_at=? WHERE server_name=? AND remote_name=?""", (now, name, remote))
            db.execute("UPDATE mcp_servers SET version=version+1,updated_at=?,updated_by=? WHERE name=?",
                       (now, account["account_id"], name))
        return self.tools(name)

    def tools(self, name):
        with self._lock:
            return [self._tool(row) for row in self._connection.execute(
                "SELECT * FROM mcp_tools WHERE server_name=? ORDER BY remote_name", (name,)).fetchall()]

    def available(self, scope_key):
        """One atomic, bounded catalog snapshot; no remote discovery in a chat turn."""
        group_scope(scope_key)
        with self._lock:
            rows = self._connection.execute("""SELECT t.*, s.config_json, s.scopes_json,
                s.version AS server_version, s.updated_at AS server_updated_at
                FROM mcp_tools t JOIN mcp_servers s ON s.name=t.server_name
                WHERE s.enabled AND t.enabled AND t.present
                    AND jsonb_exists(s.scopes_json::jsonb, ?)
                ORDER BY s.name, t.remote_name LIMIT ?""", (scope_key, MAX_TOOLS)).fetchall()
        return [({"name": row["server_name"], "config": json.loads(row["config_json"]),
                  "scopes": json.loads(row["scopes_json"]), "enabled": True,
                  "version": row["server_version"], "updated_at": row["server_updated_at"]},
                 self._tool(row)) for row in rows]

    def configure_tool(self, account, name, remote, *, enabled, trust, expected_version, server_version, tool_fingerprint):
        if type(enabled) is not bool or trust not in {"approval", "safe"}:
            raise ValueError("Invalid MCP tool policy")
        with self.transaction() as db:
            self._admin(db, account)
            server = db.execute("SELECT * FROM mcp_servers WHERE name=?", (name,)).fetchone()
            self._version(server, server_version)
            row = db.execute("SELECT * FROM mcp_tools WHERE server_name=? AND remote_name=?", (name, remote)).fetchone()
            if row is None or not row["present"] or row["fingerprint"] != tool_fingerprint:
                raise McpConflict("MCP tool catalog changed")
            self._version(row, expected_version)
            db.execute("UPDATE mcp_tools SET enabled=?,trust=?,version=version+1,updated_at=? WHERE server_name=? AND remote_name=?",
                       (enabled, trust, int(self.clock()), name, remote))
            return self._tool(db.execute("SELECT * FROM mcp_tools WHERE server_name=? AND remote_name=?", (name, remote)).fetchone())

    def resolve(self, name, remote, scope_key):
        group_scope(scope_key)
        with self.transaction() as db:
            return self._resolve_in(db, name, remote, scope_key)

    def _resolve_in(self, db, name, remote, scope_key):
        server = self._server(db.execute("SELECT * FROM mcp_servers WHERE name=?", (name,)).fetchone())
        row = db.execute("SELECT * FROM mcp_tools WHERE server_name=? AND remote_name=?", (name, remote)).fetchone()
        if not server or not server["enabled"] or scope_key not in server["scopes"] or not row or not row["present"] or not row["enabled"]:
            raise McpDenied("MCP tool is not enabled for this group")
        return server, self._tool(row)

    def issue_token(self, account, *, scope_key, bot_id, label, lifetime_seconds=86400):
        group_scope(scope_key)
        if not isinstance(bot_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", bot_id):
            raise ValueError("A concrete QQ bot account is required")
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80:
            raise ValueError("Token label must have 1-80 characters")
        if type(lifetime_seconds) is not int or not 60 <= lifetime_seconds <= 30 * 86400:
            raise ValueError("Token lifetime must be 1 minute to 30 days")
        now, token_id, token = int(self.clock()), secrets.token_hex(12), "mcp_" + secrets.token_urlsafe(32)
        with self.transaction() as db:
            self._admin(db, account)
            if db.execute("SELECT COUNT(*) FROM mcp_group_tokens WHERE scope_key=? AND NOT revoked AND expires_at>?", (scope_key, now)).fetchone()[0] >= 20:
                raise ValueError("At most 20 active MCP tokens per group")
            db.execute("""INSERT INTO mcp_group_tokens(token_id,token_hash,label,scope_key,bot_id,
                account_id,account_version,version,revoked,expires_at,created_at)
                VALUES (?,?,?,?,?,?,?,1,FALSE,?,?)""", (token_id, hashlib.sha256(token.encode()).hexdigest(),
                label.strip(), scope_key, bot_id, account["account_id"], account["version"], now + lifetime_seconds, now))
        # Only this return contains the bearer secret, never list/audit projections.
        return {"token_id": token_id, "token": token, "scope_key": scope_key,
                "expires_at": now + lifetime_seconds, "version": 1}

    def tokens(self):
        with self._lock:
            return [dict(row) for row in self._connection.execute("""SELECT token_id,label,scope_key,bot_id,
                version,revoked,expires_at,created_at FROM mcp_group_tokens ORDER BY created_at DESC LIMIT 500""").fetchall()]

    def revoke_token(self, account, token_id, *, expected_version):
        with self.transaction() as db:
            self._admin(db, account)
            row = db.execute("SELECT * FROM mcp_group_tokens WHERE token_id=?", (token_id,)).fetchone()
            if row is None:
                raise McpConflict("MCP token does not exist")
            self._version(row, expected_version)
            db.execute("UPDATE mcp_group_tokens SET revoked=TRUE,version=version+1 WHERE token_id=?", (token_id,))

    def authenticate(self, token, *, consume=False):
        if not isinstance(token, str) or not re.fullmatch(r"mcp_[A-Za-z0-9_-]{43}", token):
            raise McpDenied("Invalid or expired MCP token")
        now = int(self.clock())
        with self.transaction() as db:
            row = db.execute("SELECT * FROM mcp_group_tokens WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            if not row or row["revoked"] or row["expires_at"] <= now:
                raise McpDenied("Invalid or expired MCP token")
            self._admin(db, {"account_id": row["account_id"], "version": row["account_version"]})
            if consume:
                count = row["rate_count"] if row["rate_slot"] == now // 60 else 0
                if count >= 60:
                    raise McpRateLimited("MCP group token request rate exceeded")
                db.execute("UPDATE mcp_group_tokens SET rate_slot=?,rate_count=? WHERE token_id=?", (now // 60, count + 1, row["token_id"]))
            return {"token_id": row["token_id"], "scope": group_scope(row["scope_key"]),
                    "bot_id": row["bot_id"], "expires_at": row["expires_at"]}
