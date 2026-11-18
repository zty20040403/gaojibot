"""Official turn platform: section streaming, progress, and direct outbox handoff."""
from __future__ import annotations

from dataclasses import dataclass, field
import time

from src.plugins.ai_chat.adapters.qq_official.conversation_control import permitted
from src.plugins.ai_chat.adapters.qq_official.identity_store import snapshot
from src.plugins.ai_chat.adapters.qq_official.lease import LeaseLost, assert_epoch
from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
from src.plugins.ai_chat.execution_control import LeaseLost as ToolLeaseLost
from src.plugins.ai_chat.message_ir import MessageBody
from src.plugins.ai_chat.message_lowering import OutboundCapabilities
from src.plugins.ai_chat.platform_types import (
    AccountKey, ActorKey, EffectiveCapabilities, EndpointKey, InboundMessage,
    PlatformFeatures, SendOutcome, SendRequest,
)


class PlatformBoundaryError(RuntimeError):
    """Fixed safe diagnostic only; external exceptions are never interpolated."""


class ExecutionError(RuntimeError):
    pass


class ExecutionFenceLost(ExecutionError, ToolLeaseLost):
    pass


@dataclass(eq=False)
class ExecutionHandle:
    execution_id: str
    inbox_id: str = field(repr=False)
    endpoint: EndpointKey = field(repr=False)
    actor: ActorKey = field(repr=False)
    canonical_message_id: int
    source_native_message_id: str = field(repr=False)
    scope_key: str = field(repr=False)
    endpoint_generation: int
    route_epoch: int
    account_fencing_epoch: int
    execution_revision: int
    deadline_at: int
    result_purpose: str
    execution_kind: str


def assert_execution_current(cursor, *, token, handle: ExecutionHandle, now: int, admission):
    assert_epoch(cursor, token)
    if handle.endpoint.account.key != token.account_key or not admission(cursor, handle.endpoint, handle.actor):
        raise ExecutionFenceLost("turn_not_admitted")
    row = cursor.execute("SELECT * FROM qq_official_turn_executions WHERE execution_id=?",
                         (handle.execution_id,)).fetchone()
    if row is None or row["account_fencing_epoch"] != token.epoch or row["state"] in {"cancelled", "needs_attention"}:
        raise ExecutionFenceLost("turn_closed")
    if not permitted(cursor, handle.endpoint, canonical=row["canonical_message_id"],
                     commands=row["execution_kind"] == "command"):
        raise ExecutionFenceLost("conversation_stopped_or_replaced")
    return row


@dataclass(frozen=True)
class TransportState:
    account: AccountKey = field(repr=False)
    credentials_ready: bool
    authenticated: bool


@dataclass(frozen=True)
class TurnReadiness:
    blocker: str | None
    delivery_condition: str = "not_checked"


@dataclass(frozen=True)
class TurnDeliveryPolicy:
    markdown: bool = True
    max_chars: int = 2000
    max_parts: int = 4
    max_bytes: int | None = None
    max_wait_seconds: int = 86400
    file_link_ttl_seconds: int = 3600

    def __post_init__(self):
        if type(self.markdown) is not bool:
            raise PlatformBoundaryError("invalid_delivery_policy")
        if (type(self.max_chars) is not int or not 1 <= self.max_chars <= 20000
                or type(self.max_parts) is not int or not 1 <= self.max_parts <= 16
                or (self.max_bytes is not None and (type(self.max_bytes) is not int
                    or not 1 <= self.max_bytes <= 1024 * 1024))
                or type(self.max_wait_seconds) is not int or not 1 <= self.max_wait_seconds <= 7 * 86400
                or type(self.file_link_ttl_seconds) is not int or not 60 <= self.file_link_ttl_seconds <= 3600):
            raise PlatformBoundaryError("invalid_delivery_policy")

    def turn_policy(self) -> TurnDeliveryPolicy:
        return self


@dataclass(frozen=True)
class ExecutionBinding:
    token: object = field(repr=False)
    handle: ExecutionHandle = field(repr=False)
    admission: object = field(repr=False)

    def current(self, cursor, now):
        return assert_execution_current(
            cursor, token=self.token, handle=self.handle, now=now, admission=self.admission,
        )

    def logical_key(self) -> str:
        return "qq-turn-result:" + self.handle.execution_id


class OfficialTurnPlatform:
    capabilities = OutboundCapabilities(text=True, mention="native", max_native_media=0)
    features = PlatformFeatures(markdown=True)
    stream_enabled = True
    progress_enabled = True

    def __init__(self, inbound: InboundMessage, *, execution: ExecutionBinding,
                 outbox, database_calls, transport_state, sender_wake=None,
                 policy=None, clock=time.time):
        if not isinstance(inbound, InboundMessage) or not isinstance(execution, ExecutionBinding):
            raise PlatformBoundaryError("invalid_turn_binding")
        handle = execution.handle
        if (inbound.endpoint.account.platform != "qq-official"
                or inbound.endpoint != handle.endpoint or inbound.actor != handle.actor
                or inbound.route_epoch != handle.route_epoch or inbound.scope.key != handle.scope_key
                or inbound.native_message_id != handle.source_native_message_id
                or execution.token.account_key != handle.endpoint.account.key
                or handle.result_purpose not in {"final", "background_result"}):
            raise PlatformBoundaryError("turn_binding_mismatch")
        self.account, self.endpoint, self.inbound = inbound.endpoint.account, inbound.endpoint, inbound
        self.execution, self.outbox, self._calls = execution, outbox, database_calls
        # Uploaded media (stickers) queued by the turn's finish after the reply text.
        self.pending_media = []
        self._transport_state, self._clock = transport_state, clock
        self._sender_wake = sender_wake or (lambda: None)
        self.policy = policy or TurnDeliveryPolicy()
        if not isinstance(self.policy, TurnDeliveryPolicy):
            raise PlatformBoundaryError("invalid_delivery_policy")
        from .progress import ProgressContext, ProgressEmitter
        def current_progress(cursor, now):
            row = execution.current(cursor, now)
            if row["state"] != "executing":
                raise PlatformBoundaryError("progress_execution_unavailable")
            return ProgressContext("execution:" + handle.execution_id, handle.endpoint, handle.actor,
                handle.scope_key, handle.deadline_at, handle.source_native_message_id, handle.canonical_message_id)
        self.progress = ProgressEmitter(token=execution.token, outbox=outbox,
            current=current_progress, database_calls=database_calls, wake=sender_wake,
            reserve_final_slots=self.policy.max_parts)

    def _endpoint(self, endpoint):
        if endpoint != self.endpoint:
            raise PlatformBoundaryError("endpoint_mismatch")

    def _readiness(self, transport):
        now = int(self._clock())
        with self.outbox.deliveries.transaction() as cursor:
            assert_epoch(cursor, self.execution.token)
            route = cursor.execute("SELECT * FROM qq_official_endpoints WHERE account_key=? AND endpoint_key=? FOR UPDATE",
                                   (self.account.key, self.endpoint.key)).fetchone()
            if route is None:
                return TurnReadiness("endpoint_unavailable")
            handle = self.execution.handle
            if route["generation"] != handle.endpoint_generation or route["route_epoch"] != handle.route_epoch:
                return TurnReadiness("route_changed")
            if route["membership_state"] != "present":
                return TurnReadiness("membership_unavailable")
            if route["binding_state"] == "revoked" or (self.endpoint.kind == "group" and route["binding_state"] != "active"):
                return TurnReadiness("binding_unavailable")
            if not self.execution.admission(cursor, self.endpoint, self.inbound.actor):
                return TurnReadiness("actor_not_admitted")
            current = self.execution.current(cursor, now)
            if current["state"] not in {"prepared", "executing", "handed_off"}:
                return TurnReadiness("execution_unavailable")
            if now >= handle.deadline_at:
                return TurnReadiness("execution_expired")
            if transport.credentials_ready is not True:
                return TurnReadiness("credentials_unavailable")
            if transport.authenticated is not True:
                return TurnReadiness("transport_not_authenticated")
            if snapshot(cursor, self.account) is None:
                return TurnReadiness("bot_identity_unverified")
            condition = "passive_candidate" if route["proactive_state"] == "denied" else "proactive_candidate"
            return TurnReadiness(None, condition)

    async def readiness(self, endpoint):
        self._endpoint(endpoint)
        try:
            transport = self._transport_state()
        except Exception:
            return TurnReadiness("transport_unavailable")
        if not isinstance(transport, TransportState) or transport.account != self.account:
            return TurnReadiness("transport_account_mismatch")
        try:
            return await self._calls(self._readiness, transport)
        except LeaseLost:
            return TurnReadiness("account_lease_unavailable")
        except Exception:
            return TurnReadiness("execution_or_storage_unavailable")

    async def ready(self, endpoint):
        return (await self.readiness(endpoint)).blocker

    async def effective_capabilities(self, endpoint):
        self._endpoint(endpoint)
        return EffectiveCapabilities(self.endpoint, 0, {})

    def enqueue_reply(self, cursor, body: MessageBody, *, now: int, turn_id: int | None = None) -> str:
        handle = self.execution.handle
        current = self.execution.current(cursor, now)
        if current["state"] == "handed_off":
            if current["plan_id"] is None:
                raise PlatformBoundaryError("silent_execution_has_no_send")
            return str(current["plan_id"])
        if now >= handle.deadline_at:
            raise PlatformBoundaryError("delivery_expired")
        rendered = render_plan(
            body, self.endpoint, markdown=self.policy.markdown,
            max_chars=self.policy.max_chars, max_parts=self.policy.max_parts,
            max_bytes=self.policy.max_bytes,
            allowed_mentions=self.outbox.mention_authority(cursor, self.endpoint),
            paragraphs=True, math_mode="dollar_delimiters",
        )
        if rendered.status != "ready":
            raise PlatformBoundaryError("needs_attachment")
        ref = self.outbox.enqueue(
            self.execution.token, rendered=rendered, requester=handle.actor,
            logical_key=self.execution.logical_key(), source_scope_key=handle.scope_key,
            source_canonical_message_id=handle.canonical_message_id,
            turn_id=turn_id if turn_id is not None else current["journal_turn_id"],
            expires_at=handle.deadline_at, purpose=handle.result_purpose,
            source_native_message_id=self.inbound.native_message_id, transaction_cursor=cursor,
        )
        cursor.execute("UPDATE qq_official_turn_executions SET state='handed_off',plan_id=?,handed_off_at=?,updated_at=? WHERE execution_id=?",
                       (ref.plan_id, now, now, handle.execution_id))
        return ref.plan_id

    def _enqueue_fragment(self, text, index):
        from .mentions import parse_mention_body
        now = int(self._clock())
        handle = self.execution.handle
        with self.outbox.deliveries.transaction() as cursor:
            current = self.execution.current(cursor, now)
            rendered = render_plan(parse_mention_body(cursor, self.endpoint, text), self.endpoint,
                markdown=self.policy.markdown, max_chars=self.policy.max_chars,
                max_parts=16, max_bytes=self.policy.max_bytes,
                allowed_mentions=self.outbox.mention_authority(cursor, self.endpoint),
                paragraphs=True, math_mode="dollar_delimiters")
            self.outbox.enqueue(self.execution.token, rendered=rendered, requester=handle.actor,
                logical_key=f"{self.execution.logical_key()}:stream:{index}",
                source_scope_key=handle.scope_key, source_native_message_id=self.inbound.native_message_id,
                source_canonical_message_id=handle.canonical_message_id, turn_id=current["journal_turn_id"],
                expires_at=handle.deadline_at, purpose=handle.result_purpose, transaction_cursor=cursor)

    async def send_fragment(self, text, index):
        await self._calls(self._enqueue_fragment, text, index)
        self._sender_wake()

    def wake(self):
        try:
            self._sender_wake()
        except Exception:
            pass

    async def send(self, request):
        if not isinstance(request, SendRequest):
            return SendOutcome("failed", reason="invalid_send_request")
        self._endpoint(request.endpoint)
        if (request.actor != self.inbound.actor or request.route_epoch != self.execution.handle.route_epoch
                or request.delivery_id != self.execution.logical_key() or request.fragment_id
                or request.reply_to_native_message_id not in {None, self.inbound.native_message_id}):
            return SendOutcome("failed", reason="request_identity_mismatch")
        try:
            def run():
                now = int(self._clock())
                with self.outbox.deliveries.transaction() as cursor:
                    plan_id = self.enqueue_reply(cursor, request.body, now=now)
                    rows = cursor.execute("SELECT delivery_id FROM deliveries WHERE plan_id=? ORDER BY part_index",
                                          (plan_id,)).fetchall()
                    return SendOutcome("pending", receipt={"plan_id": plan_id,
                        "delivery_ids": tuple(row["delivery_id"] for row in rows)}, reason="delivery_pending")
            outcome = await self._calls(run)
        except PlatformBoundaryError as exc:
            reason = str(exc) if str(exc) in {"needs_attachment", "delivery_expired"} else "frozen_delivery_rejected"
            return SendOutcome("failed", reason=reason)
        except LeaseLost:
            return SendOutcome("failed", reason="account_lease_unavailable")
        except Exception:
            return SendOutcome("failed", reason="execution_or_storage_unavailable")
        self.wake()
        return outcome

    async def recall(self, endpoint, native_message_id):
        self._endpoint(endpoint)
        return False
