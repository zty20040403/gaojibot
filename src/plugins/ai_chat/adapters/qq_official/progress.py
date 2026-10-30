"""Best-effort progress using M3 plans and a live, exact execution authority.

Progress is deliberately not resumable. The durable plan/attempt remains audit
and late-receipt evidence; losing this process-local authority cancels unsent
progress. Every enqueue and physical send rechecks the real job/source fence.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from ...agent.control import LeaseLost as TaskLeaseLost
from ...message_ir import MessageBody, TextNode
from ...platform_types import ActorKey, EndpointKey
from .outbox_types import OutboxInvariantError, digest, stable_json
from .plans import enqueue_plan
from .renderer import render_plan
from .send_policy import PolicyWait, check_send_policy


@dataclass(frozen=True)
class ProgressContext:
    source_key: str
    endpoint: EndpointKey
    actor: ActorKey
    scope_key: str
    deadline_at: int
    native_message_id: str = ""
    canonical_message_id: int | None = None


class _Emission:
    def __init__(self, emitter, context, expires_at):
        self.emitter, self.context, self.expires_at = emitter, context, expires_at
        self.plan_id = None

    def current(self, cursor, token, plan, now):
        e, c = self.emitter, self.context
        if (token is not e.token or token.epoch != e.epoch or now >= self.expires_at
                or plan is None or plan["purpose"] != "progress"
                or plan["payload_mode"] != "persistent" or plan["part_count"] != 1
                or plan["account_key"] != c.endpoint.account.key
                or plan["endpoint_key"] != c.endpoint.key
                or plan["requester_actor_key"] != c.actor.key
                or plan["source_scope_key"] != c.scope_key
                or plan["source_canonical_message_id"] != c.canonical_message_id
                or plan["expires_at"] != self.expires_at
                or (self.plan_id is not None and plan["plan_id"] != self.plan_id)):
            return False
        try:
            return e.current(cursor, now) == c
        except (Exception, TaskLeaseLost):
            return False


def enqueue_progress(outbox, token, *, emission, rendered, logical_key, cursor, now):
    """Host-only entry. A model-facing DTO is never an emission capability."""
    if (type(emission) is not _Emission or emission.emitter.outbox is not outbox
            or emission.emitter.token is not token):
        raise OutboxInvariantError("progress_authority_unavailable")
    c = emission.context
    def guard(cursor, *, token, plan, now, **_):
        return emission.current(cursor, token, plan, now)
    ref = enqueue_plan(cursor, token=token, rendered=rendered, requester=c.actor,
        logical_key=logical_key, source_scope_key=c.scope_key,
        source_native_message_id=c.native_message_id,
        source_canonical_message_id=c.canonical_message_id,
        purpose="progress", expires_at=emission.expires_at, now=now,
        admission=outbox.admission, max_wait_seconds=outbox.max_wait_seconds,
        source_guard=guard)
    emission.plan_id = ref.plan_id
    outbox._progress[ref.plan_id] = emission
    return ref


class ProgressEmitter:
    """Constructed only by a host holding a real execution/job binding.

    current(cursor, now) must check the full account/job/source/actor/route
    authority and return immutable ProgressContext, or raise. It is invoked
    afresh by the existing sender fence, never trusted as a stored bool.
    """
    def __init__(self, *, token, outbox, current, database_calls, wake=None, reserve_final_slots=4):
        if type(reserve_final_slots) is not int or not 1 <= reserve_final_slots <= 16:
            raise ValueError("progress_final_slots_invalid")
        self.reserve_final_slots = reserve_final_slots
        self.token, self.epoch = token, token.epoch
        self.outbox, self.current = outbox, current
        self.database_calls, self.wake = database_calls, wake

    def _queue(self, text):
        value = self.outbox.clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("progress_clock_invalid")
        now = int(value)
        with self.outbox.deliveries.transaction() as cursor:
            c = self.current(cursor, now)
            if (not isinstance(c, ProgressContext) or c.endpoint.account.platform != "qq-official"
                    or c.endpoint.account.key != self.token.account_key
                    or not c.actor.matches_endpoint(c.endpoint) or c.actor.identity_kind == "bot"
                    or type(c.deadline_at) is not int or not now < c.deadline_at
                    or not isinstance(c.source_key, str) or not c.source_key):
                raise ValueError("progress_source_invalid")
            # Same source/text can never create a fresh plan on replay. In
            # particular a restart may not extend the original 30-second TTL.
            logical_key = "qq-progress:" + digest(stable_json([c.source_key, c.actor.key, text]))
            idem = "qqp1:" + digest(stable_json(["qq-plan-v1", self.token.account_key, c.endpoint.key, logical_key]))
            if cursor.execute("SELECT plan_id FROM delivery_plans WHERE idempotency_key=?", (idem,)).fetchone():
                return {"ok": True, "status": "suppressed", "reason": "already_reported"}
            route = cursor.execute("SELECT * FROM qq_official_endpoints WHERE account_key=? AND endpoint_key=? FOR UPDATE",
                (self.token.account_key, c.endpoint.key)).fetchone()
            if route is None:
                raise ValueError("progress_endpoint_missing")
            expires_at = min(now + 30, c.deadline_at)
            choice = check_send_policy(cursor, token=self.token, endpoint=c.endpoint, endpoint_row=route,
                source_native_id=c.native_message_id or None, purpose="progress", deadline=expires_at, now=now,
                certification=self.outbox.certification, allow_newer_windows=self.outbox.allow_newer_windows,
                allow_proactive_probe=False, verified_boundary=self.outbox.verified_boundary,
                reserve_final_slots=self.reserve_final_slots)
            if isinstance(choice, PolicyWait):
                return {"ok": True, "status": "suppressed", "reason": "progress_not_sendable"}
            # Bound live capabilities, independently of text cardinality. Old
            # plans keep their durable evidence but lose send authority.
            self.outbox._prune_progress(now)
            if len(self.outbox._progress) >= 128:
                return {"ok": True, "status": "suppressed", "reason": "progress_capacity"}
            rendered = render_plan(MessageBody((TextNode(0, text),)), c.endpoint,
                allowed_mentions=frozenset(), max_chars=200, max_parts=1)
            self.outbox.enqueue_progress(self.token, emission=_Emission(self, c, expires_at),
                rendered=rendered, logical_key=logical_key, cursor=cursor, now=now)
            return {"ok": True, "status": "enqueued"}

    async def say(self, text):
        if not isinstance(text, str) or not text.strip():
            return {"ok": False, "status": "suppressed", "reason": "progress_text_empty"}
        try:
            result = await self.database_calls(self._queue, text.strip()[:200])
            if result["status"] == "enqueued" and self.wake is not None:
                self.wake()
            return result
        except (Exception, TaskLeaseLost):
            # Fixed, non-sensitive diagnostic. Cancellation is BaseException
            # and must continue to cancel the task; ordinary progress errors do not.
            return {"ok": False, "status": "suppressed", "reason": "progress_unavailable"}

    async def report(self, text):
        await self.say(text)
