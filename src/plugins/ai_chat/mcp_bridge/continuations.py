"""Transactional approval handoff, continuation fencing and original-group Outbox."""

from __future__ import annotations

import json

from ..agent.control import LeaseLost
from ..model_budget import ModelBudgetExceeded
from ..message_ir import MessageBody, TextNode, body_to_json
from .calls import McpCallStore, TERMINAL
from .checkpoint import freeze_checkpoint, resume_transcript, validate_transcript
from .contracts import canonical, fingerprint, group_scope
from .store import McpConflict, McpDenied

CONTINUATION_JOB = "mcp_continuation"
DELIVERY_PREFIX = "mcp-final:"


class McpEffectUncertain(BaseException):
    pass


class McpContinuationBudget(ModelBudgetExceeded):
    pass


class McpContinuationStore(McpCallStore):
    def __init__(self, source, *, jobs, outbox, **kwargs):
        super().__init__(source, **kwargs)
        self.jobs, self.outbox = jobs, outbox

    @staticmethod
    def _continuation(row):
        value = dict(row)
        value["checkpoint"] = json.loads(value.pop("checkpoint_json"))
        value["messages"] = json.loads(value.pop("transcript_json"))
        value["reply"] = json.loads(value.pop("reply_json"))
        value["pending_checkpoint"] = json.loads(value.pop("pending_checkpoint_json"))
        if fingerprint(value["checkpoint"]) != value["checkpoint_hash"]:
            raise McpDenied("MCP continuation checkpoint changed")
        return value

    def attach_checkpoint(self, call_id, *, scope_key, bot_id, user_id, messages,
                          question, profile, message_id, message_time, reply=None, system_context="", history_length=0):
        with self.transaction() as db:
            row = db.execute("""SELECT * FROM mcp_calls WHERE call_id=?
                AND scope_key=? AND bot_id=? AND user_id=?""", (call_id, scope_key, bot_id, user_id)).fetchone()
            if row is None:
                raise McpDenied("MCP continuation belongs to another conversation or actor")
            call = self._call(row)
            checkpoint = freeze_checkpoint(messages, call, question=question, profile=profile,
                message_id=message_id, message_time=message_time, reply=reply, system_context=system_context,
                history_length=history_length)
            digest = fingerprint(checkpoint)
            old = db.execute("SELECT * FROM mcp_continuations WHERE call_id=?", (call_id,)).fetchone()
            if old is not None:
                if old["checkpoint_hash"] != digest:
                    raise McpConflict("MCP invocation cannot change its original conversation")
                return self._continuation(old)
            self._current_call(db, row)
            if call["status"] != "waiting" or call["binding"]["configuration"]["trust"] != "approval":
                raise McpDenied("MCP continuation must be saved before approval execution")
            now = int(self.clock())
            db.execute("""INSERT INTO mcp_continuations(call_id,checkpoint_json,checkpoint_hash,
                transcript_json,reply_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?)""",
                (call_id, canonical(checkpoint), digest, "[]", canonical(checkpoint["reply"]), now, now))
            return self._continuation(db.execute("SELECT * FROM mcp_continuations WHERE call_id=?", (call_id,)).fetchone())

    def materialize_continuations(self, *, limit=32):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid MCP continuation batch size")
        now = int(self.clock())
        with self.transaction() as db:
            rows = db.execute("""SELECT k.* FROM mcp_continuations k JOIN mcp_calls c USING(call_id)
                WHERE k.status='waiting' AND c.status IN ('succeeded','failed','needs_attention','cancelled')
                ORDER BY k.created_at,k.call_id LIMIT ? FOR UPDATE OF k""", (limit,)).fetchall()
            for row in rows:
                continuation = self._continuation(row)
                call = self._call(db.execute("SELECT * FROM mcp_calls WHERE call_id=?", (row["call_id"],)).fetchone())
                messages = resume_transcript(continuation["checkpoint"], call)
                job, _ = self.jobs.enqueue(kind=CONTINUATION_JOB, scope_key=call["scope_key"],
                    idempotency_key=f"mcp-resume:{call['call_id']}", payload={"call_id": call["call_id"]},
                    max_attempts=3, resume_on_lease_loss=True, now=now, transaction_cursor=db)
                db.execute("""UPDATE mcp_continuations SET status='queued',job_id=?,transcript_json=?,
                    updated_at=? WHERE call_id=?""", (job.job_id, canonical(messages), now, call["call_id"]))
            return len(rows)

    def _fenced(self, db, job):
        current = db.execute("SELECT * FROM durable_jobs WHERE job_id=? FOR UPDATE", (job.job_id,)).fetchone()
        if (current is None or current["status"] != "running" or current["lease_owner"] != job.lease_owner
                or current["attempts"] != job.attempts or current["lease_until"] <= self.clock()
                or current["lease_generation"] != job.lease_generation
                or current["kind"] != CONTINUATION_JOB or job.kind != CONTINUATION_JOB
                or current["scope_key"] != job.scope_key or json.loads(current["payload_json"]) != job.payload):
            raise LeaseLost("MCP continuation no longer owns its job lease")
        row = db.execute("SELECT * FROM mcp_continuations WHERE job_id=? FOR UPDATE", (job.job_id,)).fetchone()
        if (row is None or row["status"] not in {"queued", "enqueued"}
                or job.payload != {"call_id": row["call_id"]}
                or current["idempotency_key"] != f"mcp-resume:{row['call_id']}"):
            raise LeaseLost("MCP continuation job does not match its saved origin")
        call = self._call(db.execute("SELECT * FROM mcp_calls WHERE call_id=?", (row["call_id"],)).fetchone())
        if call is None or call["scope_key"] != job.scope_key or call["status"] not in TERMINAL:
            raise LeaseLost("MCP continuation has no final remote receipt")
        return self._continuation(row), call

    def execution(self, job):
        with self.transaction() as db:
            return self._fenced(db, job)

    def owns_execution(self, job):
        try:
            self.execution(job)
            return True
        except LeaseLost:
            return False

    def reserve_model_call(self, job, reserved_tokens=0):
        # A reservation survives crashes and provider failures; retries do not
        # receive a fresh budget merely by acquiring a new worker lease.
        if type(reserved_tokens) is not int or reserved_tokens < 0:
            raise ValueError("Invalid MCP model reservation")
        with self.transaction() as db:
            state, _ = self._fenced(db, job)
            if (state["status"] != "queued" or state["model_calls"] >= 8
                    or state["reserved_tokens"] + reserved_tokens > 262144):
                raise McpContinuationBudget("MCP continuation model budget exhausted")
            db.execute("""UPDATE mcp_continuations SET model_calls=model_calls+1,
                reserved_tokens=reserved_tokens+?,updated_at=? WHERE call_id=?""",
                (reserved_tokens, int(self.clock()), state["call_id"]))
            return state["model_calls"] + 1

    def save_transcript(self, job, messages, *, expected_revision, reply=None):
        validate_transcript(messages)
        if reply is not None and (not isinstance(reply, dict) or len(canonical(reply).encode()) > 2 * 1024 * 1024):
            raise ValueError("MCP saved reply exceeds its limit")
        with self.transaction() as db:
            state, _ = self._fenced(db, job)
            if (state["status"] != "queued" or type(expected_revision) is not int
                    or state["revision"] != expected_revision):
                raise McpConflict("MCP continuation transcript revision changed")
            old = state["messages"]
            if len(messages) < len(old) or messages[:len(old)] != old:
                raise McpConflict("MCP continuation cannot rewrite completed conversation evidence")
            db.execute("""UPDATE mcp_continuations SET transcript_json=?,revision=revision+1,reply_json=?,
                updated_at=? WHERE call_id=?""", (canonical(messages), canonical(state["reply"] if reply is None else reply),
                                                int(self.clock()), state["call_id"]))
            return state["revision"] + 1

    def pause_for_approval(self, job, call_id, messages, reply):
        with self.transaction() as db:
            state, origin = self._fenced(db, job)
            call = self._call(db.execute("SELECT * FROM mcp_calls WHERE call_id=?", (call_id,)).fetchone())
            if (state["status"] != "queued" or state["pending_call_id"] is not None or call is None
                    or call["status"] != "waiting" or call["binding"]["configuration"]["trust"] != "approval"
                    or any(call[key] != origin[key] for key in ("scope_key", "bot_id", "user_id"))
                    or messages != state["messages"]):
                raise McpDenied("MCP approval does not match the current continuation boundary")
            original = state["checkpoint"]
            checkpoint = freeze_checkpoint(messages, call, question=original["question"], profile=original["profile"],
                message_id=original["message_id"], message_time=original["message_time"],
                reply=reply, system_context=original["system_context"], history_length=original["history_length"])
            db.execute("""UPDATE mcp_continuations SET pending_call_id=?,pending_checkpoint_json=?,
                updated_at=? WHERE call_id=?""", (call_id, canonical(checkpoint), int(self.clock()), state["call_id"]))

    def resolve_pending_approval(self, job):
        with self.transaction() as db:
            state, origin = self._fenced(db, job)
            if state["pending_call_id"] is None:
                return state
            call = self._call(db.execute("SELECT * FROM mcp_calls WHERE call_id=?", (state["pending_call_id"],)).fetchone())
            if call is None or any(call[key] != origin[key] for key in ("scope_key", "bot_id", "user_id")):
                raise McpDenied("MCP pending approval scope changed")
            if call["status"] not in TERMINAL:
                return None
            messages = resume_transcript(state["pending_checkpoint"], call)
            if messages[:len(state["messages"])] != state["messages"]:
                raise McpDenied("MCP pending approval transcript changed")
            db.execute("""UPDATE mcp_continuations SET pending_call_id=NULL,pending_checkpoint_json='{}',
                transcript_json=?,reply_json=?,revision=revision+1,updated_at=? WHERE call_id=?""",
                (canonical(messages), canonical(state["pending_checkpoint"]["reply"]), int(self.clock()), state["call_id"]))
            return self._continuation(db.execute("SELECT * FROM mcp_continuations WHERE call_id=?", (state["call_id"],)).fetchone())

    def begin_effect(self, job, tool_call_id, name, arguments, *, idempotency):
        if idempotency not in {"pure", "idempotent", "non-idempotent"}:
            raise ValueError("Invalid MCP continuation host effect policy")
        digest = fingerprint(arguments)
        now = int(self.clock())
        with self.transaction() as db:
            state, _ = self._fenced(db, job)
            pending = validate_transcript(state["messages"])
            if (state["status"] != "queued" or not pending or next(iter(pending)) != tool_call_id
                    or pending[tool_call_id][0] != name or fingerprint(pending[tool_call_id][1]) != digest):
                raise McpDenied("MCP effect does not match the next persisted tool call")
            old = db.execute("SELECT * FROM mcp_continuation_effects WHERE call_id=? AND tool_call_id=?",
                             (state["call_id"], tool_call_id)).fetchone()
            if old is not None:
                if old["tool_name"] != name or old["arguments_hash"] != digest or old["idempotency"] != idempotency:
                    raise McpConflict("MCP effect identity cannot be reused with different input or policy")
                if old["status"] == "completed":
                    return old["result"]
                if idempotency == "non-idempotent":
                    raise McpEffectUncertain("MCP continuation effect has no receipt; do not execute it again")
                if old["lease_generation"] == job.lease_generation:
                    raise McpConflict("MCP tool is already in progress in this attempt")
                db.execute("UPDATE mcp_continuation_effects SET lease_generation=?,updated_at=? WHERE call_id=? AND tool_call_id=?",
                           (job.lease_generation, now, state["call_id"], tool_call_id))
                return None
            if db.execute("SELECT COUNT(*) FROM mcp_continuation_effects WHERE call_id=?", (state["call_id"],)).fetchone()[0] >= 32:
                raise McpContinuationBudget("MCP continuation tool budget exhausted")
            db.execute("""INSERT INTO mcp_continuation_effects(call_id,tool_call_id,tool_name,arguments_hash,
                idempotency,lease_generation,status,created_at,updated_at) VALUES (?,?,?,?,?,?,'started',?,?)""",
                (state["call_id"], tool_call_id, name, digest, idempotency, job.lease_generation, now, now))
            return None

    def finish_effect(self, job, tool_call_id, result):
        if not isinstance(result, str) or len(result.encode()) > 128 * 1024:
            raise ValueError("MCP continuation tool receipt exceeds its limit")
        with self.transaction() as db:
            state, _ = self._fenced(db, job)
            if state["status"] != "queued":
                raise McpDenied("MCP continuation already finished")
            db.execute("""UPDATE mcp_continuation_effects SET status='completed',result=?,updated_at=?
                WHERE call_id=? AND tool_call_id=? AND status='started' AND lease_generation=?""",
                (result, int(self.clock()), state["call_id"], tool_call_id, job.lease_generation))
            if db.rowcount != 1:
                raise McpDenied("MCP receipt does not own its effect")

    def uncertain_effects(self, job):
        with self.transaction() as db:
            state, _ = self._fenced(db, job)
            return [row[0] for row in db.execute("""SELECT tool_name FROM mcp_continuation_effects
                WHERE call_id=? AND status='started' AND idempotency='non-idempotent'""", (state["call_id"],)).fetchall()]

    def queue_result(self, job, body):
        if not body.nodes or len(body_to_json(body).encode()) > 512 * 1024:
            raise ValueError("MCP continuation reply is empty or too large")
        with self.transaction() as db:
            state, call = self._fenced(db, job)
            if state["status"] == "enqueued":
                return state["delivery_id"]
            delivery, _ = self.outbox.enqueue(
                idempotency_key=DELIVERY_PREFIX + call["call_id"], source_scope_key=call["scope_key"],
                target_scope=group_scope(call["scope_key"]), body=body,
                reply_to_native_message_id=state["checkpoint"]["message_id"] if state["checkpoint"]["message_id"] > 0 else None,
                now=int(self.clock()), transaction_cursor=db)
            db.execute("""UPDATE mcp_continuations SET status='enqueued',delivery_id=?,final_body_json=?,
                updated_at=? WHERE call_id=?""", (delivery.delivery_id, body_to_json(body), int(self.clock()), call["call_id"]))
            return delivery.delivery_id

    def reconcile_terminal_jobs(self):
        with self.transaction() as db:
            rows = db.execute("""SELECT k.call_id,c.scope_key,j.status FROM mcp_continuations k
                JOIN mcp_calls c USING(call_id) JOIN durable_jobs j ON j.job_id=k.job_id
                WHERE k.status='queued' AND j.status IN ('failed','cancelled')
                ORDER BY k.created_at LIMIT 32 FOR UPDATE OF k,j""").fetchall()
            for row in rows:
                notice = ("外部工具审批后的续跑已取消。" if row["status"] == "cancelled" else
                          "外部工具审批后的续跑失败，结果尚未完整生成。")
                body = MessageBody((TextNode(0, notice + "已执行的操作不会自动重复，请在控制台核对工具回执。"),))
                delivery, _ = self.outbox.enqueue(idempotency_key=DELIVERY_PREFIX + row["call_id"],
                    source_scope_key=row["scope_key"], target_scope=group_scope(row["scope_key"]), body=body,
                    now=int(self.clock()), transaction_cursor=db)
                db.execute("""UPDATE mcp_continuations SET status='enqueued',delivery_id=?,final_body_json=?,
                    updated_at=? WHERE call_id=?""", (delivery.delivery_id, body_to_json(body), int(self.clock()), row["call_id"]))
            return len(rows)

    def delivery_identity(self, delivery):
        # Selection and pre-send checks must use this row, never 'first online bot'.
        with self._lock:
            row = self._connection.execute("""SELECT c.scope_key,c.bot_id,c.user_id FROM mcp_continuations k
                JOIN mcp_calls c USING(call_id) WHERE k.delivery_id=? AND k.status='enqueued'
                AND c.call_id=?""", (delivery.delivery_id, delivery.idempotency_key.removeprefix(DELIVERY_PREFIX))).fetchone()
        if (row is None or not delivery.idempotency_key.startswith(DELIVERY_PREFIX)
                or delivery.target_scope.key != row["scope_key"] or delivery.source_scope_key != row["scope_key"]):
            raise McpDenied("MCP reply destination does not match its original conversation")
        return dict(row)
