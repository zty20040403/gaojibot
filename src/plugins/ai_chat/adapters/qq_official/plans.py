"""Delivery manifests backed by the existing deliveries table."""
from __future__ import annotations

import secrets

from src.plugins.ai_chat.message_ir import body_to_json
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey

from .binding import control_authorized
from .lease import assert_epoch
from .renderer import RenderedPlan, content_payload
from .send_types import OutboxInvariantError, PlanRef, digest, stable_json

PURPOSE_PRIORITIES = {
    "final": 0,
    "requested_artifact": 0,
    "background_result": 10,
    "progress": 20,
    "unsolicited": 30,
}
DEFAULT_MAX_WAIT_SECONDS = 86400
MAX_WAIT_SECONDS = 7 * 86400


def validate_wait_limit(value: int) -> None:
    if type(value) is not int or not 1 <= value <= MAX_WAIT_SECONDS:
        raise OutboxInvariantError("invalid_wait_limit")


def enqueue_plan(cursor, *, token, rendered: RenderedPlan, requester: ActorKey,
                 logical_key: str, source_scope_key: str, expires_at: int, now: int,
                 purpose: str = "final", source_native_message_id: str = "",
                 source_canonical_message_id: int | None = None, turn_id: int | None = None,
                 admission=None, control_reply_id=None, epoch_guard=assert_epoch,
                 max_wait_seconds: int = DEFAULT_MAX_WAIT_SECONDS,
                 source_guard=None, source_proof=None,
                 source_generation=None, vm_request=None) -> PlanRef:
    epoch_guard(cursor, token)

    def require_source(plan_id):
        if source_guard is None:
            return
        plan = cursor.execute("SELECT * FROM delivery_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if source_guard(cursor, token=token, plan=plan, operation="enqueue",
                        logical_key=logical_key, source_proof=source_proof, now=now) is not True:
            raise OutboxInvariantError("delivery_source_unavailable")

    validate_wait_limit(max_wait_seconds)
    vm_action = vm_request is not None
    if (not isinstance(rendered, RenderedPlan) or rendered.status != "ready" or not rendered.parts
            or not isinstance(requester, ActorKey) or requester.identity_kind == "bot" and not vm_action
            or not requester.matches_endpoint(rendered.endpoint)
            or token.account_key != rendered.endpoint.account.key
            or purpose not in PURPOSE_PRIORITIES or not callable(admission)
            or not isinstance(logical_key, str) or not logical_key or len(logical_key.encode()) > 4096
            or not isinstance(source_scope_key, str) or not 1 <= len(source_scope_key.encode()) <= 1000
            or type(now) is not int or type(expires_at) is not int
            or not 0 <= now < expires_at <= 2**63 - 1):
        raise OutboxInvariantError("invalid_delivery_plan")
    if expires_at - now > max_wait_seconds:
        raise OutboxInvariantError("delivery_wait_limit_exceeded")
    endpoint = rendered.endpoint
    if (len(endpoint.account.key.encode()) > 512 or len(endpoint.key.encode()) > 1000
            or len(requester.key.encode()) > 1280
            or not isinstance(source_native_message_id, str)
            or len(source_native_message_id.encode()) > 256):
        raise OutboxInvariantError("invalid_delivery_namespace")
    endpoint_row = cursor.execute("""SELECT * FROM qq_official_endpoints
        WHERE account_key=? AND endpoint_key=? FOR UPDATE""", (token.account_key, endpoint.key)).fetchone()
    if endpoint_row is None or EndpointKey.parse(endpoint_row["endpoint_key"]) != endpoint:
        raise OutboxInvariantError("delivery_endpoint_unavailable")
    if vm_action:
        from .identity_store import snapshot
        author = snapshot(cursor, endpoint.account)
        if (author is None or requester != author.bot_actor or purpose != "unsolicited"
                or source_native_message_id or source_canonical_message_id is not None or turn_id is not None
                or endpoint_row["binding_state"] == "revoked" or endpoint_row["membership_state"] != "present"):
            raise OutboxInvariantError("vm_source_mismatch")
        if endpoint.kind == "private":
            peer = ActorKey(endpoint.account, "c2c_user", "", endpoint.native_conversation_id)
            if not admission(cursor, endpoint, peer):
                raise OutboxInvariantError("delivery_not_authorized")
    control_view = control_reply_id is not None and control_authorized(cursor, control_reply_id, endpoint, requester)
    if control_reply_id is not None and not control_view:
        raise OutboxInvariantError("control_source_unavailable")
    if not control_view and not vm_action and not admission(cursor, endpoint, requester):
        raise OutboxInvariantError("delivery_not_authorized")
    if not control_view and endpoint.kind == "group" and (endpoint_row["binding_state"] != "active"
                                                            or endpoint_row["membership_state"] != "present"):
        raise OutboxInvariantError("delivery_endpoint_unavailable")
    control = {"kind": "authenticated_control", "control_id": control_reply_id} if control_view else None
    manifest_payload = {"v": 1, "rendered": rendered.to_json(), "control": control,
                        "source_native_message_id": source_native_message_id}
    if vm_action:
        manifest_payload["vm_request"] = vm_request
    manifest = stable_json(manifest_payload)
    identity = ["qq-plan-v1", token.account_key, endpoint.key, logical_key]
    idem = "qqp1:" + digest(stable_json(identity))
    contents = [content_payload(rendered, i) for i in range(len(rendered.parts))]
    from .conversation_control import permitted, state
    command = cursor.execute("""SELECT 1 FROM qq_official_turn_executions WHERE endpoint_key=? AND requester_actor_key=?
        AND canonical_message_id=? AND execution_kind='command' AND state IN ('prepared','executing','result_ready','handed_off')""",
        (endpoint.key, requester.key, source_canonical_message_id)).fetchone() is not None
    if not control_view and not permitted(cursor, endpoint,
            canonical=source_canonical_message_id if purpose != "background_result" else None,
            generation=source_generation, commands=command):
        raise OutboxInvariantError("conversation_stopped_or_replaced")
    immutable = dict(conversation_generation=state(cursor, endpoint)["stop_generation"],
        account_key=token.account_key, endpoint_key=endpoint.key,
        requester_actor_key=requester.key, source_scope_key=source_scope_key,
        source_canonical_message_id=source_canonical_message_id, turn_id=turn_id,
        endpoint_generation=endpoint_row["generation"], route_epoch=endpoint_row["route_epoch"],
        capability_revision=endpoint_row["capability_revision"], purpose=purpose, payload_mode="persistent",
        manifest_json=manifest, manifest_digest=digest(manifest), digest_scheme="sha256", digest_key_id=None,
        part_count=len(rendered.parts), expires_at=expires_at, priority=PURPOSE_PRIORITIES[purpose])
    existing = cursor.execute("SELECT * FROM delivery_plans WHERE idempotency_key=?", (idem,)).fetchone()
    if existing is not None:
        rows = cursor.execute("SELECT delivery_id FROM deliveries WHERE plan_id=? ORDER BY part_index", (existing["plan_id"],)).fetchall()
        require_source(existing["plan_id"])
        return PlanRef(existing["plan_id"], tuple(r["delivery_id"] for r in rows), False)
    plan_id = secrets.token_hex(16)
    fields = {"plan_id": plan_id, "idempotency_key": idem, **immutable, "created_at": now}
    cursor.execute("INSERT INTO delivery_plans (" + ",".join(fields) + ") VALUES (" + ",".join("?" for _ in fields) + ")", tuple(fields.values()))
    delivery_ids = []
    for i, part in enumerate(rendered.parts):
        part_idem = "qqd1:" + digest(stable_json([*identity, i]))
        payload_digest = digest(stable_json(contents[i]))
        fields = dict(idempotency_key=part_idem, source_scope_key=source_scope_key,
            source_canonical_message_id=source_canonical_message_id, turn_id=turn_id,
            target_platform="qq-official", target_kind=endpoint.kind,
            target_native_conversation_id=endpoint.native_conversation_id,
            reply_to_native_message_id=source_native_message_id, body_json=body_to_json(part.canonical_body),
            content_fingerprint=payload_digest, status="pending", attempts=0,
            next_attempt_at=now, native_message_id="", last_error="", created_at=now, updated_at=now,
            plan_id=plan_id, part_index=i, account_key=token.account_key, endpoint_key=endpoint.key,
            requester_actor_key=requester.key, endpoint_generation=endpoint_row["generation"],
            route_epoch=endpoint_row["route_epoch"], capability_revision=endpoint_row["capability_revision"],
            purpose=purpose, payload_mode="persistent", variant="primary", payload_digest=payload_digest,
            digest_scheme="sha256", digest_key_id=None, priority=PURPOSE_PRIORITIES[purpose], expires_at=expires_at)
        row = cursor.execute("INSERT INTO deliveries (" + ",".join(fields) + ") VALUES (" + ",".join("?" for _ in fields) + ") RETURNING delivery_id", tuple(fields.values())).fetchone()
        delivery_ids.append(row["delivery_id"])
    require_source(plan_id)
    return PlanRef(plan_id, tuple(delivery_ids), True)
