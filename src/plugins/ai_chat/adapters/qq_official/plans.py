"""Immutable manifests backed by the existing deliveries table.

All writes share the caller's fenced transaction. Ephemeral binding output has
only safe placeholders and keyed digests on disk, and cannot be replayed.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets

from src.plugins.ai_chat.message_ir import body_to_json
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey

from .lease import assert_epoch
from .outbox_types import OutboxInvariantError, PlanRef, digest, stable_json
from .renderer import RenderedPlan, content_payload


EPHEMERAL_BODY = '{"v":1,"nodes":[]}'
EPHEMERAL_MANIFEST = '{"v":1,"ephemeral":true}'
PURPOSE_PRIORITIES = {"final": 0, "requested_artifact": 0, "binding_otp": 0,
                      "background_result": 10, "progress": 20, "unsolicited": 30}
DEFAULT_MAX_WAIT_SECONDS = 86400
MAX_WAIT_SECONDS = 7 * 86400
SHORT_WAIT_SECONDS = 30


def validate_wait_limit(value: int) -> None:
    if type(value) is not int or not 1 <= value <= MAX_WAIT_SECONDS:
        raise OutboxInvariantError("invalid_wait_limit")


def keyed_digest(pepper: bytes, domain: str, value) -> str:
    if not isinstance(pepper, bytes) or len(pepper) < 32:
        raise OutboxInvariantError("ephemeral_key_unavailable")
    key = hmac.new(pepper, b"qq-official:ephemeral:v1:" + domain.encode("ascii"), hashlib.sha256).digest()
    return hmac.new(key, stable_json(value).encode("utf-8"), hashlib.sha256).hexdigest()


def binding_preview_deadline(cursor, endpoint, requester, preview, now):
    if (not isinstance(preview, tuple) or len(preview) != 2
            or not isinstance(preview[0], str) or not re.fullmatch(r"BN-[0-9a-f]{32}", preview[0])
            or type(preview[1]) is not int or preview[1] < 1):
        return None
    row = cursor.execute("""SELECT c.expires_at AS challenge_expiry,n.expires_at AS candidate_expiry
        FROM qq_official_binding_candidates n JOIN qq_official_binding_challenges c ON c.challenge_id=n.challenge_id
        JOIN qq_official_owner_grants g ON g.grant_id=c.owner_grant_id
        WHERE n.candidate_id=? AND n.version=? AND n.account_key=? AND n.state='pending' AND n.expires_at>?
          AND c.state='candidate' AND c.expires_at>? AND c.issuer_endpoint_key=? AND c.issuer_actor_key=?
          AND g.state='active' AND g.revision=c.owner_revision""",
        (preview[0], preview[1], endpoint.account.key, now, now, endpoint.key, requester.key)).fetchone()
    return min(row["challenge_expiry"], row["candidate_expiry"]) if row else None


from .control_receipts import validate_plan_authority, bind_control_plan


def enqueue_plan(cursor, *, token, rendered: RenderedPlan, requester: ActorKey,
                 logical_key: str, source_scope_key: str, expires_at: int, now: int,
                 purpose: str = "final", source_native_message_id: str = "",
                 source_canonical_message_id: int | None = None, turn_id: int | None = None,
                 admission=None, challenge_id: str | None = None, pepper: bytes | None = None,
                 digest_key_id: str = "v1", binding_preview=None, control_reply_id=None, epoch_guard=assert_epoch,
                 max_wait_seconds: int = DEFAULT_MAX_WAIT_SECONDS) -> PlanRef:
    """The adapter must pass current admission, not a model-supplied grant.

    The rendered object is sealed by the pure renderer. A duplicate identity
    must agree with every immutable field, including exact route and deadline.
    Ordinary callers cannot manufacture a binding challenge from message text.
    """
    epoch_guard(cursor, token)
    validate_wait_limit(max_wait_seconds)
    if (not isinstance(rendered, RenderedPlan) or rendered.status != "ready" or not rendered.parts
            or not isinstance(requester, ActorKey) or requester.identity_kind == "bot"
            or not requester.matches_endpoint(rendered.endpoint)
            or token.account_key != rendered.endpoint.account.key
            or purpose not in PURPOSE_PRIORITIES or not callable(admission)
            or not isinstance(logical_key, str) or not logical_key or len(logical_key.encode()) > 4096
            or not isinstance(source_scope_key, str) or not 1 <= len(source_scope_key.encode()) <= 1000
            or type(now) is not int or type(expires_at) is not int
            or not 0 <= now < expires_at <= 2**63 - 1):
        raise OutboxInvariantError("invalid_delivery_plan")
    wait_limit = min(max_wait_seconds, SHORT_WAIT_SECONDS) if purpose in {"binding_otp", "unsolicited", "progress"} else max_wait_seconds
    # Reject rather than clamp relative to each replay's now: the caller's
    # absolute deadline is part of the immutable idempotency contract.
    if expires_at - now > wait_limit:
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
    control_view = None
    if control_reply_id is not None:
        if challenge_id is not None or binding_preview is not None or turn_id is not None or source_canonical_message_id is not None:
            raise OutboxInvariantError("control_plan_not_exact")
        control_view = validate_plan_authority(cursor, token, control_id=control_reply_id, rendered=rendered,
            requester=requester, logical_key=logical_key, source_scope_key=source_scope_key,
            source_native_message_id=source_native_message_id, expires_at=expires_at, purpose=purpose, now=now)
    ephemeral = purpose == "binding_otp"
    if (ephemeral != (challenge_id is not None) or (not ephemeral and pepper is not None)
            or (control_view is None and not admission(cursor, endpoint, requester))):
        raise OutboxInvariantError("delivery_not_authorized")
    challenge_expires = None
    if ephemeral:
        challenge = cursor.execute("""SELECT c.* FROM qq_official_binding_challenges c
            JOIN qq_official_owner_grants g ON g.grant_id=c.owner_grant_id
            WHERE c.challenge_id=? AND c.account_key=? AND c.issuer_endpoint_key=?
              AND c.issuer_actor_key=? AND c.state='sending' AND c.expires_at>?
              AND g.state='active' AND g.revision=c.owner_revision FOR UPDATE OF c,g""",
            (challenge_id, token.account_key, endpoint.key, requester.key, now)).fetchone()
        if (challenge is None or len(rendered.parts) != 1 or expires_at > challenge["expires_at"]
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", digest_key_id)):
            raise OutboxInvariantError("binding_delivery_unavailable")
        challenge_expires = challenge["expires_at"]
    elif control_view is None and endpoint.kind == "group" and (endpoint_row["binding_state"] != "active"
                                         or endpoint_row["membership_state"] != "present"):
        raise OutboxInvariantError("delivery_endpoint_unavailable")
    # Include the original source in the frozen manifest, independently of a
    # later legal window. A newer credential never fabricates a quote target.
    control = {"kind": "authenticated_control", "control_id": control_reply_id} if control_view is not None else None
    if binding_preview is not None:
        boundary = binding_preview_deadline(cursor, endpoint, requester, binding_preview, now)
        if ephemeral or purpose != "final" or boundary is None or expires_at > min(boundary, now + 30):
            raise OutboxInvariantError("binding_preview_unavailable")
        control = {"kind": "binding_preview", "candidate_id": binding_preview[0], "version": binding_preview[1]}
    manifest = stable_json({"v": 1, "rendered": rendered.to_json(), "control": control,
                            "source_native_message_id": source_native_message_id}) if not ephemeral else EPHEMERAL_MANIFEST
    identity = ["qq-plan-v1", token.account_key, endpoint.key, logical_key]
    idem = "qqp1:" + digest(stable_json(identity))
    # Use the stable idempotency identity for the keyed content commitment, so
    # replay comparison is possible without storing an OTP or public SHA hash.
    contents = [content_payload(rendered, i) for i in range(len(rendered.parts))]
    manifest_digest = (keyed_digest(pepper, "content", [identity, requester.key, challenge_id, contents])
                       if ephemeral else digest(manifest))
    mode, scheme, key_id = ("ephemeral", "hmac_sha256_v1", digest_key_id) if ephemeral else ("persistent", "sha256", None)
    immutable = dict(account_key=token.account_key, endpoint_key=endpoint.key,
        requester_actor_key=requester.key, source_scope_key=source_scope_key,
        source_canonical_message_id=source_canonical_message_id, turn_id=turn_id,
        endpoint_generation=endpoint_row["generation"], route_epoch=endpoint_row["route_epoch"],
        capability_revision=endpoint_row["capability_revision"], purpose=purpose, payload_mode=mode,
        manifest_json=manifest, manifest_digest=manifest_digest, digest_scheme=scheme, digest_key_id=key_id,
        part_count=len(rendered.parts), expires_at=expires_at, priority=PURPOSE_PRIORITIES[purpose],
        challenge_id=challenge_id, challenge_expires_at=challenge_expires)
    existing = cursor.execute("SELECT * FROM delivery_plans WHERE idempotency_key=?", (idem,)).fetchone()
    if existing is not None:
        if any(existing[k] != v for k, v in immutable.items()):
            raise OutboxInvariantError("delivery_plan_identity_conflict")
        rows = cursor.execute("SELECT delivery_id FROM deliveries WHERE plan_id=? ORDER BY part_index", (existing["plan_id"],)).fetchall()
        if len(rows) != len(rendered.parts):
            raise OutboxInvariantError("delivery_plan_part_conflict")
        if control_view is not None:
            bind_control_plan(cursor, token, control_id=control_reply_id, plan_id=existing["plan_id"],
                delivery_id=rows[0]["delivery_id"], now=now)
        return PlanRef(existing["plan_id"], tuple(r["delivery_id"] for r in rows), False)
    plan_id = secrets.token_hex(16)
    fields = {"plan_id": plan_id, "idempotency_key": idem, **immutable, "created_at": now}
    cursor.execute("INSERT INTO delivery_plans (" + ",".join(fields) + ") VALUES (" + ",".join("?" for _ in fields) + ")", tuple(fields.values()))
    delivery_ids = []
    for i, part in enumerate(rendered.parts):
        part_idem = "qqd1:" + digest(stable_json([*identity, i]))
        payload_digest = (keyed_digest(pepper, "part", [identity, plan_id, i, requester.key, challenge_id, contents[i]])
                          if ephemeral else digest(stable_json(contents[i])))
        fields = dict(idempotency_key=part_idem, source_scope_key=source_scope_key,
            source_canonical_message_id=source_canonical_message_id, turn_id=turn_id,
            target_platform="qq-official", target_kind=endpoint.kind,
            target_native_conversation_id=endpoint.native_conversation_id,
            reply_to_native_message_id=source_native_message_id, body_json=EPHEMERAL_BODY if ephemeral else body_to_json(part.canonical_body),
            content_fingerprint="" if ephemeral else payload_digest, status="pending", attempts=0,
            next_attempt_at=now, native_message_id="", last_error="", created_at=now, updated_at=now,
            plan_id=plan_id, part_index=i, account_key=token.account_key, endpoint_key=endpoint.key,
            requester_actor_key=requester.key, endpoint_generation=endpoint_row["generation"],
            route_epoch=endpoint_row["route_epoch"], capability_revision=endpoint_row["capability_revision"],
            purpose=purpose, payload_mode=mode, variant="primary", payload_digest=payload_digest,
            digest_scheme=scheme, digest_key_id=key_id, priority=PURPOSE_PRIORITIES[purpose], expires_at=expires_at)
        row = cursor.execute("INSERT INTO deliveries (" + ",".join(fields) + ") VALUES (" + ",".join("?" for _ in fields) + ") RETURNING delivery_id", tuple(fields.values())).fetchone()
        delivery_ids.append(row["delivery_id"])
    if control_view is not None:
        bind_control_plan(cursor, token, control_id=control_reply_id, plan_id=plan_id, delivery_id=delivery_ids[0], now=now)
    return PlanRef(plan_id, tuple(delivery_ids), True)
