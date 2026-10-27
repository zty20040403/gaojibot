"""Durable dispatch boundary: an unresolved remote effect is never replayed."""

from __future__ import annotations

import json
import re
import secrets

from ..agent.evidence import redact
from .contracts import canonical, fingerprint, group_scope
from .store import McpConflict, McpDenied, McpStore

TERMINAL = frozenset({"succeeded", "failed", "needs_attention", "cancelled"})


class McpCallStore(McpStore):
    @staticmethod
    def _call(row):
        if row is None:
            return None
        value = dict(row)
        value["binding"] = json.loads(value.pop("binding_json"))
        value["result"] = json.loads(value.pop("result_json"))
        return value

    @staticmethod
    def _configuration(server, tool):
        return {"server": server["name"], "server_version": server["version"],
            "config": server["config"], "remote_name": tool["remote_name"],
            "tool_version": tool["version"], "tool_fingerprint": tool["fingerprint"], "trust": tool["trust"]}

    def _current_call(self, db, row):
        if row is None or row["expires_at"] <= self.clock():
            raise McpDenied("MCP call is absent or expired")
        binding = json.loads(row["binding_json"])
        if fingerprint(binding) != row["binding_hash"]:
            raise McpDenied("MCP call binding changed")
        server, tool = self._resolve_in(db, row["server_name"], row["remote_name"], row["scope_key"])
        if binding["configuration"] != self._configuration(server, tool):
            raise McpConflict("MCP configuration changed after this invocation was prepared")
        return binding

    def prepare_call(self, *, scope_key, bot_id, user_id, invocation, name, remote, arguments,
                     expected_configuration=None):
        group_scope(scope_key)
        if any(not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", value) for value in (bot_id, user_id)):
            raise ValueError("MCP calls require concrete bot and user identities")
        if not isinstance(invocation, str) or not 1 <= len(invocation) <= 256:
            raise ValueError("MCP calls require a durable invocation identity")
        if not isinstance(arguments, dict) or len(canonical(arguments).encode()) > 32768:
            raise ValueError("MCP arguments exceed their limit")
        identity = fingerprint([scope_key, bot_id, user_id, invocation])
        now = int(self.clock())
        with self.transaction() as db:
            if expected_configuration is not None:
                current = self._resolve_in(db, name, remote, scope_key)
                if self._configuration(*current) != expected_configuration:
                    raise McpConflict("MCP catalog changed after the model received its tool list")
            old = db.execute("SELECT * FROM mcp_calls WHERE invocation_hash=?", (identity,)).fetchone()
            if old:
                binding = json.loads(old["binding_json"])
                if (old["server_name"] != name or old["remote_name"] != remote
                        or canonical(binding["arguments"]) != canonical(arguments)):
                    raise McpConflict("MCP invocation identity cannot be reused with other arguments")
                if expected_configuration is not None and binding["configuration"] != expected_configuration:
                    raise McpConflict("MCP catalog no longer matches this invocation")
                return self._call(old)
            server, tool = self._resolve_in(db, name, remote, scope_key)
            configuration = self._configuration(server, tool)
            if expected_configuration is not None and configuration != expected_configuration:
                raise McpConflict("MCP catalog changed after the model received its tool list")
            if db.execute("""SELECT COUNT(*) FROM mcp_calls WHERE scope_key=?
                    AND status IN ('waiting','ready','connecting','dispatched') AND expires_at>?""", (scope_key, now)).fetchone()[0] >= 32:
                raise McpDenied("MCP group pending-call limit reached")
            binding = {"configuration": configuration, "arguments": arguments,
                       "scope_key": scope_key, "bot_id": bot_id, "user_id": user_id}
            call_id = "mcp-call-" + secrets.token_hex(16)
            status = "ready" if tool["trust"] == "safe" else "waiting"
            db.execute("""INSERT INTO mcp_calls(call_id,invocation_hash,scope_key,bot_id,user_id,
                server_name,remote_name,binding_json,binding_hash,status,created_at,updated_at,expires_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (call_id, identity, scope_key, bot_id, user_id,
                name, remote, canonical(binding), fingerprint(binding), status, now, now, now + 600))
            return self._call(db.execute("SELECT * FROM mcp_calls WHERE call_id=?", (call_id,)).fetchone())

    def call(self, call_id, *, scope_key, bot_id, user_id):
        # End users cannot observe another user's remote result, even inside the same group.
        with self._lock:
            return self._call(self._connection.execute("""SELECT * FROM mcp_calls
                WHERE call_id=? AND scope_key=? AND bot_id=? AND user_id=?""",
                (call_id, scope_key, bot_id, user_id)).fetchone())

    @staticmethod
    def approval_payload(call):
        return {"call_id": call["call_id"], "binding_hash": call["binding_hash"]}

    def authorize_call(self, call_id, approval_id):
        """Called only by the registered MobileAuthorization executor after quorum validation."""
        with self.transaction() as db:
            row = db.execute("SELECT * FROM mcp_calls WHERE call_id=?", (call_id,)).fetchone()
            binding = self._current_call(db, row)
            approval = db.execute("SELECT * FROM admin_approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if (binding["configuration"]["trust"] != "approval" or not approval
                    or approval["kind"] != "mcp_tool" or approval["quorum"] != 2
                    or approval["status"] != "executing" or approval["parent_approval_id"] is not None
                    or approval["lease_until"] <= self.clock()
                    or approval["bot_id"] != row["bot_id"] or approval["qq_id"] != row["user_id"]
                    or approval["payload_json"] != canonical(self.approval_payload(row))):
                raise McpDenied("MCP call requires its exact executing two-admin approval")
            if row["approval_id"] and row["approval_id"] != approval_id:
                raise McpDenied("MCP call is already bound to another approval")
            if row["status"] != "waiting":
                return self._call(row)
            db.execute("UPDATE mcp_calls SET status='ready',approval_id=?,updated_at=? WHERE call_id=?",
                       (approval_id, int(self.clock()), call_id))
            return self._call(db.execute("SELECT * FROM mcp_calls WHERE call_id=?", (call_id,)).fetchone())

    def claim_call(self, call_id, *, owner, scope_key, bot_id, user_id):
        if not isinstance(owner, str) or not re.fullmatch(r"[a-f0-9]{32}", owner):
            raise ValueError("MCP call owner must be a random execution token")
        now = int(self.clock())
        with self.transaction() as db:
            row = db.execute("""SELECT * FROM mcp_calls WHERE call_id=?
                AND scope_key=? AND bot_id=? AND user_id=?""", (call_id, scope_key, bot_id, user_id)).fetchone()
            if row is None:
                raise McpDenied("MCP call is outside this actor's scope")
            if row["status"] != "ready":
                return None
            binding = self._current_call(db, row)
            if binding["configuration"]["trust"] == "approval" and not row["approval_id"]:
                raise McpDenied("MCP call has no exact approval")
            db.execute("UPDATE mcp_calls SET status='connecting',owner=?,lease_until=?,updated_at=? WHERE call_id=?",
                       (owner, now + 180, now, call_id))
            return self._call(db.execute("SELECT * FROM mcp_calls WHERE call_id=?", (call_id,)).fetchone())

    def dispatch_call(self, call_id, owner):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM mcp_calls WHERE call_id=?", (call_id,)).fetchone()
            self._current_call(db, row)
            if row["status"] != "connecting" or row["owner"] != owner or row["lease_until"] <= self.clock():
                raise McpDenied("MCP call dispatch ownership is no longer valid")
            if row["approval_id"]:
                approval = db.execute("SELECT status,lease_until FROM admin_approvals WHERE approval_id=?", (row["approval_id"],)).fetchone()
                if approval is None or approval["status"] != "executing" or approval["lease_until"] <= self.clock():
                    raise McpDenied("MCP approval is no longer executing")
            now = int(self.clock())
            db.execute("UPDATE mcp_calls SET status='dispatched',dispatched_at=?,updated_at=? WHERE call_id=?", (now, now, call_id))

    def finish_call(self, call_id, owner, result, *, unconfirmed=False):
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise ValueError("MCP receipt must explicitly report success or failure")
        encoded = canonical(redact(result))
        if len(encoded.encode()) > 128 * 1024:
            raise ValueError("MCP receipt exceeds its limit")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM mcp_calls WHERE call_id=?", (call_id,)).fetchone()
            if row is None or row["owner"] != owner or row["status"] not in {"connecting", "dispatched"}:
                raise McpDenied("MCP receipt does not own this call")
            if result["ok"] and row["status"] != "dispatched":
                raise McpDenied("MCP cannot succeed before dispatch")
            status = "needs_attention" if unconfirmed else "succeeded" if result["ok"] else "failed"
            db.execute("UPDATE mcp_calls SET status=?,result_json=?,lease_until=0,updated_at=? WHERE call_id=?",
                       (status, encoded, int(self.clock()), call_id))

    def recover_calls(self):
        now = int(self.clock())
        with self.transaction() as db:
            # A crash before the send boundary may reconnect. A crash after it may
            # have changed remote state: never infer failure or replay the mutation.
            unknown = canonical({"ok": False, "code": "remote_result_unconfirmed", "needs_attention": True})
            db.execute("""UPDATE mcp_calls SET status='needs_attention',result_json=?,updated_at=?
                WHERE status='dispatched' AND lease_until<=?""", (unknown, now, now))
            db.execute("""UPDATE mcp_calls SET status='ready',owner='',lease_until=0,updated_at=?
                WHERE status='connecting' AND lease_until<=? AND expires_at>?""", (now, now, now))
            db.execute("""UPDATE mcp_calls SET status='cancelled',result_json=?,updated_at=?
                WHERE status IN ('waiting','ready','connecting') AND expires_at<=?""",
                       (canonical({"ok": False, "code": "mcp_call_expired"}), now, now))

    def call_summary(self, call):
        return {key: call[key] for key in ("call_id", "scope_key", "bot_id", "user_id", "server_name",
            "remote_name", "status", "approval_id", "created_at", "updated_at", "dispatched_at", "result")}

    def admin_calls(self, *, offset=0, limit=5):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Invalid MCP call page")
        with self._lock:
            total = self._connection.execute("SELECT COUNT(*) FROM mcp_calls").fetchone()[0]
            rows = self._connection.execute("SELECT * FROM mcp_calls ORDER BY created_at DESC, call_id LIMIT ? OFFSET ?",
                                            (limit, offset)).fetchall()
        return {"items": [self.call_summary(self._call(row)) for row in rows], "total": total, "offset": offset}
