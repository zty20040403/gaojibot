"""Official turn output: streaming sections and final replies share the sender."""
from __future__ import annotations

from dataclasses import dataclass, field
import time

from src.plugins.ai_chat.adapters.qq_official.identity_store import snapshot
from src.plugins.ai_chat.adapters.qq_official.lease import LeaseLost, assert_epoch
from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
from src.plugins.ai_chat.message_ir import MessageBody, TextNode, body_to_json
from src.plugins.ai_chat.message_lowering import OutboundCapabilities
from src.plugins.ai_chat.platform_types import (
    AccountKey, ActorKey, EffectiveCapabilities, EndpointKey, InboundMessage,
    PlatformFeatures, SendOutcome, SendRequest,
)


class PlatformBoundaryError(RuntimeError):
    """Fixed safe diagnostic only; external exceptions are never interpolated."""


@dataclass(frozen=True)
class TransportState:
    """Runtime observation only, not user supplied proof or send authority."""

    account: AccountKey = field(repr=False)
    credentials_ready: bool
    authenticated: bool


@dataclass(frozen=True)
class TurnReadiness:
    blocker: str | None
    delivery_condition: str = "not_checked"


@dataclass(frozen=True)
class TurnDeliveryPolicy:
    """Trusted runtime settings; never decoded from SendRequest or model output."""

    allow_newer_windows: bool = False
    allow_proactive_probe: bool = False
    markdown: bool = True
    max_chars: int = 2000
    max_parts: int = 4
    max_bytes: int | None = None

    def __post_init__(self):
        if any(type(value) is not bool for value in (
            self.allow_newer_windows, self.allow_proactive_probe, self.markdown,
        )):
            raise PlatformBoundaryError("invalid_delivery_policy")
        if (type(self.max_chars) is not int or not 1 <= self.max_chars <= 20000
                or type(self.max_parts) is not int or not 1 <= self.max_parts <= 16
                or (self.max_bytes is not None and (type(self.max_bytes) is not int
                    or not 1 <= self.max_bytes <= 1024 * 1024))):
            raise PlatformBoundaryError("invalid_delivery_policy")


@dataclass(frozen=True)
class MembershipEvidence:
    """An admitted current endpoint observation; never a display-name guess.

    Its provider must establish source authenticity from the admitted inbox and
    actual sender actor. The struct alone does not prove membership. No owner,
    moderator, roster, history-scope or cross-endpoint rights follow from it.
    """

    actor: ActorKey = field(repr=False)
    endpoint: EndpointKey = field(repr=False)
    generation: int
    route_epoch: int
    observed_at: int
    expires_at: int
    source: str


@dataclass(frozen=True)
class ExecutionBinding:
    """Runtime-only closure over the execution module's locally issued handle."""

    api: object = field(repr=False)
    token: object = field(repr=False)
    handle: object = field(repr=False)
    job: object = field(repr=False)
    admission: object = field(repr=False)

    def current(self, cursor, now):
        return self.api.assert_execution_current(cursor, token=self.token, handle=self.handle,
            job=self.job, now=now, admission=self.admission)

    def result(self, cursor, now):
        return self.api.load_result(cursor, token=self.token, handle=self.handle,
            job=self.job, now=now, admission=self.admission)

    def logical_key(self):
        return self.api.logical_result_key(self.handle)

    def attach(self, cursor, plan_id, now):
        return self.api.attach_plan(cursor, token=self.token, handle=self.handle,
            job=self.job, plan_id=plan_id, now=now, admission=self.admission)


class OfficialTurnPlatform:
    capabilities = OutboundCapabilities(text=True, mention="native", max_native_media=0)
    features = PlatformFeatures(markdown=True)
    stream_enabled = True
    progress_enabled = True

    def __init__(self, inbound: InboundMessage, *, execution: ExecutionBinding,
                 outbox, database_calls, transport_state, capability_evidence=None,
                 membership_evidence=None, sender_wake=None, policy=None, clock=time.time):
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
        from .progress import ProgressContext, ProgressEmitter
        def current_progress(cursor, now):
            row = execution.current(cursor, now)
            if row["state"] != "executing":
                raise PlatformBoundaryError("progress_execution_unavailable")
            return ProgressContext("execution:" + handle.execution_id, handle.endpoint, handle.actor,
                handle.scope_key, handle.deadline_at, handle.source_native_message_id, handle.canonical_message_id)
        self._transport_state, self._clock = transport_state, clock
        self._capability_evidence = capability_evidence
        self._membership_evidence = membership_evidence or (lambda *_: ())
        self._sender_wake = sender_wake or (lambda: None)
        self.policy = policy or TurnDeliveryPolicy()
        if not isinstance(self.policy, TurnDeliveryPolicy):
            raise PlatformBoundaryError("invalid_delivery_policy")
        self.progress = ProgressEmitter(token=execution.token, outbox=outbox,
            current=current_progress, database_calls=database_calls, wake=sender_wake,
            reserve_final_slots=self.policy.max_parts)

    def _endpoint(self, endpoint):
        if endpoint != self.endpoint:
            raise PlatformBoundaryError("endpoint_mismatch")

    def _route(self, cursor):
        return cursor.execute("SELECT * FROM qq_official_endpoints WHERE account_key=? AND endpoint_key=? FOR UPDATE",
            (self.account.key, self.endpoint.key)).fetchone()

    def _readiness(self, transport):
        now = int(self._clock())
        with self.outbox.deliveries.transaction() as cursor:
            assert_epoch(cursor, self.execution.token)
            route = self._route(cursor)
            if route is None:
                return TurnReadiness("endpoint_unavailable")
            handle = self.execution.handle
            if (route["generation"] != handle.endpoint_generation or route["route_epoch"] != handle.route_epoch):
                return TurnReadiness("route_changed")
            if route["membership_state"] != "present":
                return TurnReadiness("membership_unavailable")
            if route["binding_state"] == "revoked" or (self.endpoint.kind == "group" and route["binding_state"] != "active"):
                return TurnReadiness("binding_unavailable")
            if not self.execution.admission(cursor, self.endpoint, self.inbound.actor):
                return TurnReadiness("actor_not_admitted")
            current = self.execution.current(cursor, now)
            if current["state"] not in {"prepared", "executing", "result_ready", "handed_off"}:
                return TurnReadiness("execution_unavailable")
            if now >= handle.deadline_at:
                return TurnReadiness("execution_expired")
            if transport.credentials_ready is not True:
                return TurnReadiness("credentials_unavailable")
            if transport.authenticated is not True:
                return TurnReadiness("transport_not_authenticated")
            if snapshot(cursor, self.account) is None:
                return TurnReadiness("bot_identity_unverified")
            condition = 'passive_candidate' if route['proactive_state']=='denied' else 'proactive_candidate'
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

    def _effective(self):
        now = int(self._clock())
        with self.outbox.deliveries.transaction() as cursor:
            self.execution.current(cursor, now)
            route = self._route(cursor)
            if route is None:
                raise PlatformBoundaryError("endpoint_unavailable")
            evidence = (self._capability_evidence(cursor, self.endpoint, now)
                        if self._capability_evidence else EffectiveCapabilities(self.endpoint, route["capability_revision"], {}))
            if not isinstance(evidence, EffectiveCapabilities) or evidence.endpoint != self.endpoint:
                raise PlatformBoundaryError("capability_evidence_mismatch")
            if evidence.revision != route["capability_revision"]:
                return EffectiveCapabilities(self.endpoint, route["capability_revision"], {})
            # Only transport/state ingestion can provide evidence provenance and
            # expiry. Implementation support never invents an 'available' grant.
            return evidence

    async def effective_capabilities(self, endpoint):
        self._endpoint(endpoint)
        return await self._calls(self._effective)

    def _mentions(self, cursor, now):
        handle = self.execution.handle
        observed = self._membership_evidence(cursor, self.endpoint, now)
        if not isinstance(observed, tuple) or len(observed) > 256:
            raise PlatformBoundaryError("invalid_membership_evidence")
        allowed = set()
        for item in observed:
            if not isinstance(item, MembershipEvidence):
                raise PlatformBoundaryError("invalid_membership_evidence")
            if (item.endpoint == self.endpoint and item.actor.matches_endpoint(self.endpoint)
                    and item.actor.identity_kind == "group_member"
                    and type(item.generation) is int and type(item.route_epoch) is int
                    and item.generation == handle.endpoint_generation and item.route_epoch == handle.route_epoch
                    and type(item.observed_at) is int and type(item.expires_at) is int
                    and 0 <= item.observed_at <= now < item.expires_at
                    and item.source == "admitted_inbound"):
                allowed.add(item.actor)
        # M3 must revalidate the same native mention authority immediately before
        # network handoff. Default M3/provider state gives no native mentions.
        current = self.outbox.mention_authority(cursor, self.endpoint)
        if not isinstance(current, frozenset):
            raise PlatformBoundaryError("invalid_mention_authority")
        return frozenset(allowed.intersection(current))

    def _plan_outcome(self, cursor, plan_id):
        rows = cursor.execute("""SELECT d.delivery_id,d.status,d.native_message_id,d.wait_reason,a.state AS attempt_state,
            a.acceptance,EXISTS(SELECT 1 FROM delivery_receipts r WHERE r.attempt_id=a.attempt_id
                AND r.account_key=d.account_key AND r.endpoint_key=d.endpoint_key
                AND r.native_message_id=d.native_message_id AND r.applied_at IS NOT NULL) AS has_receipt
            FROM deliveries d LEFT JOIN delivery_attempts a ON a.attempt_id=d.current_attempt_id
            WHERE d.plan_id=? AND d.account_key=? AND d.endpoint_key=? ORDER BY d.part_index""",
            (plan_id, self.account.key, self.endpoint.key)).fetchall()
        if not rows:
            raise PlatformBoundaryError("delivery_plan_missing")
        evidence = {"plan_id": plan_id, "delivery_ids": tuple(row["delivery_id"] for row in rows)}
        statuses = {row["status"] for row in rows}
        if "ambiguous" in statuses:
            return SendOutcome("ambiguous", receipt=evidence, reason="（可能没发出去）")
        if statuses.intersection({"failed", "cancelled"}):
            reason = next((r['wait_reason'] for r in rows if r['wait_reason']), 'delivery_stopped')
            if reason == 'proactive_disabled_no_reply_window':
                reason = '这个群没开主动发言，请群主打开'
            return SendOutcome("failed", receipt=evidence, reason=reason)
        if all(row["status"] == "committed" and row["attempt_state"] == "committed"
               and row["acceptance"] == "confirmed_yes" and row["has_receipt"] for row in rows):
            ids = tuple(row["native_message_id"] for row in rows)
            return SendOutcome("committed", native_message_id=ids[-1], receipt={**evidence, "native_message_ids": ids})
        return SendOutcome("pending", receipt=evidence, reason="accepted_awaiting_commit" if "accepted" in statuses else "delivery_pending")

    def _enqueue_frozen_result(self, request):
        now = int(self._clock())
        with self.outbox.deliveries.transaction() as cursor:
            current = self.execution.current(cursor, now)
            frozen = self.execution.result(cursor, now)
            if body_to_json(request.body) != body_to_json(frozen):
                raise PlatformBoundaryError("frozen_result_mismatch")
            if current["state"] == "handed_off":
                if current["plan_id"] is None:
                    raise PlatformBoundaryError("silent_execution_has_no_send")
                return self._plan_outcome(cursor, current["plan_id"])
            if current["state"] != "result_ready":
                raise PlatformBoundaryError("frozen_result_required")
            if now >= self.execution.handle.deadline_at:
                return SendOutcome("failed", reason="delivery_expired")
            rendered = render_plan(frozen, self.endpoint, markdown=self.policy.markdown,
                max_chars=self.policy.max_chars, max_parts=self.policy.max_parts, max_bytes=self.policy.max_bytes,
                allowed_mentions=self._mentions(cursor, now), paragraphs=True, math_mode="dollar_delimiters")
            if rendered.status != "ready":
                return SendOutcome("failed", reason="needs_attachment")
            handle = self.execution.handle
            ref = self.outbox.enqueue(self.execution.token, rendered=rendered, requester=handle.actor,
                logical_key=self.execution.logical_key(), source_scope_key=handle.scope_key,
                source_canonical_message_id=handle.canonical_message_id, turn_id=current["journal_turn_id"],
                expires_at=handle.deadline_at, purpose=handle.result_purpose,
                source_native_message_id=self.inbound.native_message_id, transaction_cursor=cursor)
            self.execution.attach(cursor, ref.plan_id, now)
            return self._plan_outcome(cursor, ref.plan_id)

    def _enqueue_fragment(self, text, index):
        now = int(self._clock())
        handle = self.execution.handle
        with self.outbox.deliveries.transaction() as cursor:
            current = self.execution.current(cursor, now)
            rendered = render_plan(MessageBody((TextNode(0, text),)), self.endpoint,
                markdown=self.policy.markdown, max_chars=self.policy.max_chars,
                max_parts=16, max_bytes=self.policy.max_bytes,
                paragraphs=True, math_mode="dollar_delimiters")
            self.outbox.enqueue(self.execution.token, rendered=rendered, requester=handle.actor,
                logical_key=f"{self.execution.logical_key()}:stream:{index}",
                source_scope_key=handle.scope_key, source_native_message_id=self.inbound.native_message_id,
                source_canonical_message_id=handle.canonical_message_id, turn_id=current["journal_turn_id"],
                expires_at=handle.deadline_at, purpose=handle.result_purpose, transaction_cursor=cursor)

    async def send_fragment(self, text, index):
        await self._calls(self._enqueue_fragment, text, index)
        self._sender_wake()

    async def send(self, request):
        if not isinstance(request, SendRequest):
            return SendOutcome("failed", reason="invalid_send_request")
        self._endpoint(request.endpoint)
        if request.actor != self.inbound.actor or request.route_epoch != self.execution.handle.route_epoch:
            return SendOutcome("failed", reason="request_identity_mismatch")
        if request.delivery_id != self.execution.logical_key() or request.fragment_id:
            return SendOutcome("failed", reason="request_logical_identity_mismatch")
        if request.reply_credential is not None and request.reply_credential != self.inbound.reply_credential:
            return SendOutcome("failed", reason="unverified_reply_credential")
        if request.reply_to_native_message_id not in {None, self.inbound.native_message_id}:
            return SendOutcome("failed", reason="unverified_reply_target")
        try:
            result = await self._calls(self._enqueue_frozen_result, request)
        except PlatformBoundaryError:
            return SendOutcome("failed", reason="frozen_delivery_rejected")
        except LeaseLost:
            return SendOutcome("failed", reason="account_lease_unavailable")
        except Exception:
            return SendOutcome("failed", reason="execution_or_storage_unavailable")
        try:
            self._sender_wake()
        except Exception:
            pass  # Committed queue handoff survives a failed in-memory wake.
        return result

    async def recall(self, endpoint, native_message_id):
        self._endpoint(endpoint)
        return False
