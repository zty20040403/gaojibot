"""Account-fenced scheduling over DeliveryStore, with short SQL transactions.

No network or sleep here. The sender waits for rate admission before prepare;
every actual send is represented by the existing begin_direct_attempt route.
"""
from __future__ import annotations

from .project_authority import RecipientEvidenceUnavailable

from contextlib import nullcontext
import json
import re
import threading
import time
import weakref

from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from .control_receipts import (
    ControlEvidenceError, control_id_for_delivery, delivery_authorized, claim_control_attempt, sync_control_terminal,
)
from .protected_payload import envelope_for_wire, validate_content
from .protected_plans import (ProtectedBinding, protected_rendered, load_protected_ref,
                              binding_for, verify_protected_attempt)
from .identity_store import snapshot
from .lease import assert_epoch
from .outbox_types import (
    OfficialAttemptContext, OutboxInvariantError, SendCandidate, SendOutcome,
    digest, fixed_reason, issue_reservation, require_issued, stable_json,
)
from .plans import (
    DEFAULT_MAX_WAIT_SECONDS, binding_preview_deadline, enqueue_plan, keyed_digest, validate_wait_limit,
)
from .rate_policy import LocalRatePermit
from .renderer import content_payload, restore_plan
from .send_policy import PolicyWait, check_send_policy, reserve_send_policy


def begin_official_attempt(cursor, delivery_id, context, now):
    """Called ONLY by DeliveryStore.begin_direct_attempt's official branch."""
    if not isinstance(context, OfficialAttemptContext):
        raise OutboxInvariantError("invalid_attempt_context")
    assert_epoch(cursor, context.token)
    row = cursor.execute("SELECT * FROM deliveries WHERE delivery_id=? AND account_key=? FOR UPDATE",
                         (delivery_id, context.token.account_key)).fetchone()
    if row is None or row["status"] != "pending" or row["attempts"] != context.expected_attempts:
        return None
    fields = dict(context.fields)
    required = {"account_key", "endpoint_key", "fencing_epoch", "endpoint_generation", "route_epoch",
        "capability_revision", "bot_actor_key", "bot_identity_source", "bot_identity_revision", "send_mode",
        "window_id", "msg_seq", "window_charge_state", "payload_mode", "payload_digest", "digest_scheme",
        "digest_key_id", "wire_payload_json", "send_by_at"}
    if (len(fields) != len(context.fields) or not required.issubset(fields)
            or set(fields) - required - {"wire_storage_digest"}
            or fields["account_key"] != context.token.account_key
            or fields["fencing_epoch"] != context.token.epoch
            or fields["endpoint_key"] != row["endpoint_key"]
            or fields["endpoint_generation"] != row["endpoint_generation"]
            or fields["route_epoch"] != row["route_epoch"]
            or fields["payload_mode"] != row["payload_mode"]):
        raise OutboxInvariantError("attempt_ownership_conflict")
    fields.setdefault("wire_storage_digest", None)
    number = row["attempts"] + 1
    fields = dict(delivery_id=delivery_id, attempt=number, state="reserved", detail="",
                  started_at=now, acceptance="unknown", **fields)
    attempt = cursor.execute("INSERT INTO delivery_attempts (" + ",".join(fields) + ") VALUES (" + ",".join("?" for _ in fields) + ") RETURNING attempt_id", tuple(fields.values())).fetchone()
    cursor.execute("""UPDATE deliveries SET status='sending',attempts=?,lease_until=?,claim_epoch=?,
        current_attempt_id=?,wait_reason=NULL,last_error='',updated_at=? WHERE delivery_id=?
        AND account_key=? AND status='pending' AND attempts=?""",
        (number, fields["send_by_at"], context.token.epoch, attempt["attempt_id"], now,
         delivery_id, context.token.account_key, context.expected_attempts))
    if cursor.rowcount != 1:
        raise OutboxInvariantError("attempt_claim_conflict")
    return cursor.execute("SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()


class OfficialOutbox:
    def __init__(self, deliveries, *, admission, pepper, clock=time.time,
                 certification="unknown", allow_newer_windows=False, allow_proactive_probe=False,
                 verified_boundary=None, mention_authority=None, max_wait_seconds=DEFAULT_MAX_WAIT_SECONDS, protected_payloads=None, approval_authority=None, source_guard=None):
        validate_wait_limit(max_wait_seconds)
        self.deliveries = deliveries
        self.admission = admission
        self.pepper = pepper
        self.clock = clock
        self.certification = certification
        self.allow_newer_windows = allow_newer_windows
        self.allow_proactive_probe = allow_proactive_probe
        self.verified_boundary = verified_boundary
        self.mention_authority = mention_authority or (lambda cursor, endpoint: frozenset())
        self.max_wait_seconds = max_wait_seconds
        self.protected_payloads = protected_payloads
        self.approval_authority = approval_authority
        self.source_guard = source_guard
        self._ephemeral = {}
        self._progress = {}
        self._invocations = weakref.WeakKeyDictionary()
        self._used_permits = weakref.WeakKeyDictionary()
        self._permit_lock = threading.Lock()

    def enqueue(self, token, *, rendered, requester, logical_key, source_scope_key,
                expires_at, purpose="final", source_native_message_id="",
                source_canonical_message_id=None, turn_id=None, challenge_id=None,
                transaction_cursor=None, binding_preview=None, control_reply_id=None, approval_source_id=None, source_proof=None):
        if purpose == "progress":
            raise OutboxInvariantError("progress_authority_unavailable")
        now = int(self.clock())
        with (nullcontext(transaction_cursor) if transaction_cursor is not None else self.deliveries.transaction()) as cursor:
            ref = enqueue_plan(cursor, token=token, rendered=rendered, requester=requester,
                logical_key=logical_key, source_scope_key=source_scope_key, expires_at=expires_at, now=now,
                purpose=purpose, source_native_message_id=source_native_message_id,
                source_canonical_message_id=source_canonical_message_id, turn_id=turn_id,
                admission=self.admission, challenge_id=challenge_id,
                pepper=self.pepper if challenge_id is not None or approval_source_id is not None else None, binding_preview=binding_preview, control_reply_id=control_reply_id,
                max_wait_seconds=self.max_wait_seconds, approval_source_id=approval_source_id,
                approval_authority=self.approval_authority, source_guard=self.source_guard, source_proof=source_proof)
        if purpose in {"binding_otp", "approval_otp"} and ref.created:
            for delivery_id in ref.delivery_ids:
                self._ephemeral[delivery_id] = rendered
        return ref

    def _prune_progress(self, now):
        self._progress = {key: value for key, value in self._progress.items()
                          if value.expires_at > now}

    def enqueue_progress(self, token, **kwargs):
        from .progress import enqueue_progress
        return enqueue_progress(self, token, **kwargs)

    def enqueue_artifact(self, token, *, content_ref, source_grant, endpoint, requester,
                         logical_key, source_scope_key, expires_at, source_native_message_id="",
                         source_canonical_message_id=None, turn_id=None, transaction_cursor=None, source_proof=None):
        if self.protected_payloads is None:
            raise OutboxInvariantError("protected_payload_port_unavailable")
        now = int(self.clock())
        with (nullcontext(transaction_cursor) if transaction_cursor is not None else self.deliveries.transaction()) as cursor:
            assert_epoch(cursor, token)
            route = cursor.execute("SELECT * FROM qq_official_endpoints WHERE account_key=? AND endpoint_key=? FOR UPDATE",
                (token.account_key, endpoint.key)).fetchone()
            if route is None or endpoint.account.key != token.account_key:
                raise OutboxInvariantError("delivery_endpoint_unavailable")
            binding = ProtectedBinding(endpoint, requester, source_scope_key, source_canonical_message_id,
                source_native_message_id, turn_id, route["generation"], route["route_epoch"], expires_at)
            self.protected_payloads.authorize_enqueue(cursor, token=token, source_grant=source_grant,
                content_ref=content_ref, binding=binding, now=now)
            return enqueue_plan(cursor, token=token, rendered=protected_rendered(endpoint), requester=requester,
                logical_key=logical_key, source_scope_key=source_scope_key, expires_at=expires_at, now=now,
                purpose="requested_artifact", source_native_message_id=source_native_message_id,
                source_canonical_message_id=source_canonical_message_id, turn_id=turn_id,
                admission=self.admission, max_wait_seconds=self.max_wait_seconds, protected_ref=content_ref,
                source_guard=self.source_guard, source_proof=source_proof)

    def list_due(self, token, *, limit=100):
        now = int(self.clock())
        with self.deliveries.transaction() as cursor:
            assert_epoch(cursor, token)
            cursor.execute("""UPDATE deliveries SET status='cancelled',wait_reason='delivery_expired',updated_at=?
                WHERE account_key=? AND status='pending' AND expires_at<=?""", (now, token.account_key, now))
            cursor.execute("""UPDATE deliveries d SET status='cancelled',wait_reason='previous_part_cancelled',updated_at=?
                WHERE d.account_key=? AND d.status='pending' AND EXISTS (
                    SELECT 1 FROM deliveries earlier WHERE earlier.plan_id=d.plan_id
                    AND earlier.part_index<d.part_index AND earlier.status IN ('failed','cancelled'))""", (now, token.account_key))
            cursor.execute("""UPDATE qq_official_control_decisions c SET state='terminal',updated_at=?
                FROM deliveries d WHERE c.delivery_id=d.delivery_id AND c.account_key=d.account_key
                  AND c.account_key=? AND c.state<>'terminal'
                  AND d.status IN ('committed','failed','cancelled','ambiguous')""", (now, token.account_key))
            # Bounded aging helps older work without turning one blocked plan
            # into a global endpoint head. A plan's physical order stays strict.
            rows = cursor.execute("""SELECT d.delivery_id FROM deliveries d
                WHERE d.account_key=? AND d.status='pending' AND d.next_attempt_at<=?
                  AND NOT EXISTS (SELECT 1 FROM deliveries earlier WHERE earlier.plan_id=d.plan_id
                      AND earlier.part_index<d.part_index AND earlier.status<>'committed')
                ORDER BY d.priority - LEAST(9, GREATEST(0,(?-d.created_at)/60)),d.created_at,d.delivery_id
                LIMIT ?""", (token.account_key, now, now, min(max(int(limit), 1), 100))).fetchall()
        return tuple(row["delivery_id"] for row in rows)

    def _load(self, cursor, token, delivery_id, now):
        assert_epoch(cursor, token)
        row = cursor.execute("SELECT * FROM deliveries WHERE delivery_id=? AND account_key=? FOR UPDATE",
                             (delivery_id, token.account_key)).fetchone()
        if row is None:
            return SendOutcome("unavailable", "delivery_not_found", delivery_id)
        if row["status"] != "pending":
            return SendOutcome(row["status"], "already_settled_or_inflight", delivery_id)
        if cursor.execute("""SELECT 1 FROM delivery_receipts r JOIN delivery_attempts a
            ON a.attempt_id=r.attempt_id WHERE a.delivery_id=? LIMIT 1""", (delivery_id,)).fetchone():
            return self._wait(cursor, row, "receipt_needs_finalization", now + 5, now)
        endpoint = EndpointKey.parse(row["endpoint_key"])
        actor = ActorKey.parse(row["requester_actor_key"])
        endpoint_row = cursor.execute("SELECT * FROM qq_official_endpoints WHERE endpoint_key=? AND account_key=? FOR UPDATE",
            (endpoint.key, token.account_key)).fetchone()
        reason = None
        if row["expires_at"] <= now:
            reason = "delivery_expired"
        elif (endpoint_row is None or endpoint_row["generation"] != row["endpoint_generation"]
                or endpoint_row["route_epoch"] != row["route_epoch"]):
            reason = "route_changed"
        elif not actor.matches_endpoint(endpoint) or actor.identity_kind == "bot":
            reason = "authorization_revoked"
        else:
            try:
                authorized = self._authorized(cursor, token, row, endpoint, actor, now)
            except RecipientEvidenceUnavailable:
                return self._wait(cursor, row, "recipient_evidence_unavailable", now + 15, now)
            if not authorized:
                reason = "authorization_revoked"
        if reason is None and row["payload_mode"] == "ephemeral" and delivery_id not in self._ephemeral:
            reason = "ephemeral_payload_lost"
        elif reason is None and not self._control_current(cursor, row, endpoint, actor, now):
            reason = "binding_control_expired"
        if reason:
            cursor.execute("""UPDATE deliveries SET status='cancelled',wait_reason=?,updated_at=?
                WHERE delivery_id=? AND status='pending'""", (reason, now, delivery_id))
            sync_control_terminal(cursor, token, delivery_id, now)
            return SendOutcome("cancelled", reason, delivery_id)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", endpoint.native_conversation_id):
            return self._wait(cursor, row, "unsupported_endpoint_contract", now + 30, now)
        if cursor.execute("""SELECT 1 FROM deliveries WHERE plan_id=? AND part_index<?
            AND status<>'committed' LIMIT 1""", (row["plan_id"], row["part_index"])).fetchone():
            return self._wait(cursor, row, "previous_part", now + 2, now)
        if row["next_attempt_at"] > now:
            return SendOutcome("pending", row["wait_reason"] or "scheduled", delivery_id, row["next_attempt_at"])
        if snapshot(cursor, endpoint.account) is None:
            return self._wait(cursor, row, "identity_unverified", now + 5, now)
        if row['payload_mode'] == 'ephemeral' and not self._ephemeral_current(cursor, token, row, now):
            cursor.execute("UPDATE deliveries SET status='cancelled',wait_reason='ephemeral_source_expired',updated_at=? WHERE delivery_id=?", (now, delivery_id))
            return SendOutcome('cancelled', 'ephemeral_source_expired', delivery_id)
        return row, endpoint, endpoint_row

    def _ephemeral_current(self, cursor, token, row, now):
        if row['purpose'] == 'approval_otp':
            return (self.approval_authority is not None
                and self.approval_authority.delivery_current(cursor, token=token, delivery=row, now=now))
        challenge = cursor.execute("""SELECT c.challenge_id FROM delivery_plans p
            JOIN qq_official_binding_challenges c ON c.challenge_id=p.challenge_id
            JOIN qq_official_owner_grants g ON g.grant_id=c.owner_grant_id
            WHERE p.plan_id=? AND c.state='sending' AND c.expires_at>?
              AND c.delivery_epoch=? AND g.state='active' AND g.revision=c.owner_revision""",
            (row["plan_id"], now, token.epoch)).fetchone()
        return challenge is not None

    def _authorized(self, cursor, token, row, endpoint, actor, now):
        if row["purpose"] == "progress":
            self._prune_progress(now)
            emission = self._progress.get(row["plan_id"])
            plan = cursor.execute('SELECT * FROM delivery_plans WHERE plan_id=?', (row['plan_id'],)).fetchone()
            try:
                if emission is None or not emission.current(cursor, token, plan, now):
                    return False
            except Exception:
                return False
        elif self.source_guard is not None:
            from .migration_types import MigrationDenied
            plan = cursor.execute('SELECT * FROM delivery_plans WHERE plan_id=?', (row['plan_id'],)).fetchone()
            try:
                if plan is None or self.source_guard(cursor, token=token, plan=plan,
                        operation='send', source_proof=None, now=now) is not True:
                    return False
            except MigrationDenied:
                return False
        if control_id_for_delivery(cursor, row) is None:
            if not self.admission(cursor, endpoint, actor):
                return False
            if row["payload_mode"] == "protected":
                if self.protected_payloads is None:
                    return False
                ref = load_protected_ref(cursor, row)
                try:
                    self.protected_payloads.authorize(cursor, token=token, content_ref=ref,
                        binding=binding_for(row), now=now)
                except RecipientEvidenceUnavailable:
                    raise
                except PermissionError:
                    return False
            return True
        try:
            return delivery_authorized(cursor, token, row, now)
        except ControlEvidenceError:
            return False

    @staticmethod
    def _control_current(cursor, row, endpoint, actor, now):
        if row["payload_mode"] == "ephemeral":
            return True
        stored = cursor.execute("SELECT manifest_json,manifest_digest FROM delivery_plans WHERE plan_id=?", (row["plan_id"],)).fetchone()
        if stored is None or digest(stored["manifest_json"]) != stored["manifest_digest"]:
            raise OutboxInvariantError("delivery_manifest_corrupt")
        control = json.loads(stored["manifest_json"]).get("control")
        if control is None:
            return True
        if isinstance(control, dict) and control.get("kind") == "authenticated_control":
            # _authorized rechecks the source/body/window/first-attempt authority.
            return set(control) == {"kind", "control_id"}
        if not isinstance(control, dict) or set(control) != {"kind", "candidate_id", "version"} or control["kind"] != "binding_preview":
            raise OutboxInvariantError("invalid_control_manifest")
        return binding_preview_deadline(cursor, endpoint, actor, (control["candidate_id"], control["version"]), now) is not None

    @staticmethod
    def _wait(cursor, row, reason, retry_at, now):
        fixed_reason(reason)
        if row["purpose"] in {"unsolicited", "progress"} and reason == "waiting_reply_window":
            cursor.execute("""UPDATE deliveries SET status='cancelled',wait_reason='unsolicited_window_unavailable',
                updated_at=? WHERE delivery_id=? AND status='pending'""", (now, row["delivery_id"]))
            return SendOutcome("cancelled", "unsolicited_window_unavailable", row["delivery_id"])
        retry_at = min(row["expires_at"], max(now + 1, retry_at or now + 5))
        cursor.execute("UPDATE deliveries SET wait_reason=?,next_attempt_at=?,updated_at=? WHERE delivery_id=? AND status='pending'",
                       (reason, retry_at, now, row["delivery_id"]))
        return SendOutcome("pending", reason, row["delivery_id"], retry_at)

    def _choice(self, cursor, token, row, endpoint, endpoint_row, permit, now):
        emission = self._progress.get(row["plan_id"]) if row["purpose"] == "progress" else None
        choice = check_send_policy(cursor, token=token, endpoint=endpoint, endpoint_row=endpoint_row,
            source_native_id=row["reply_to_native_message_id"] or None, purpose=row["purpose"],
            deadline=row["expires_at"], now=now, rate_permit=permit, certification=self.certification,
            allow_newer_windows=self.allow_newer_windows if row["purpose"] != "approval_otp" else False,
            allow_proactive_probe=self.allow_proactive_probe if row["purpose"] != "approval_otp" else False,
            verified_boundary=self.verified_boundary, control_reply_id=control_id_for_delivery(cursor, row),
            reserve_final_slots=emission.emitter.reserve_final_slots if emission is not None else 4)
        if row['purpose'] == 'approval_otp' and not isinstance(choice, PolicyWait):
            if self.approval_authority is None or not self.approval_authority.choice_current(cursor, delivery=row, choice=choice):
                return PolicyWait('waiting_reply_window')
        return choice

    def candidate(self, token, delivery_id):
        now = int(self.clock())
        with self.deliveries.transaction() as cursor:
            loaded = self._load(cursor, token, delivery_id, now)
            if isinstance(loaded, SendOutcome):
                return loaded
            row, endpoint, endpoint_row = loaded
            choice = self._choice(cursor, token, row, endpoint, endpoint_row, None, now)
            if isinstance(choice, PolicyWait):
                return self._wait(cursor, row, choice.reason, choice.retry_at, now)
            return SendCandidate(delivery_id, endpoint, choice.send_mode, choice.send_by_at, self.certification)

    def _content(self, cursor, row, endpoint, *, token=None, now=None):
        if row["payload_mode"] == "protected":
            if self.protected_payloads is None or token is None or now is None:
                raise OutboxInvariantError("protected_payload_port_unavailable")
            ref = load_protected_ref(cursor, row)
            if now >= ref.expires_at:
                raise OutboxInvariantError("protected_payload_expired")
            payload = self.protected_payloads.resolve(cursor, token=token, content_ref=ref,
                binding=binding_for(row), now=now)
            validate_content(ref, payload)
            return json.loads(stable_json(payload))
        if row["payload_mode"] == "ephemeral":
            rendered = self._ephemeral.get(row["delivery_id"])
            if rendered is None:
                raise OutboxInvariantError("ephemeral_payload_lost")
        else:
            plan = cursor.execute("SELECT * FROM delivery_plans WHERE plan_id=?", (row["plan_id"],)).fetchone()
            if plan is None or digest(plan["manifest_json"]) != plan["manifest_digest"]:
                raise OutboxInvariantError("delivery_manifest_corrupt")
            manifest = json.loads(plan["manifest_json"])
            if manifest.get("v") != 1 or manifest.get("source_native_message_id") != row["reply_to_native_message_id"]:
                raise OutboxInvariantError("delivery_manifest_conflict")
            rendered = restore_plan(manifest["rendered"], endpoint=endpoint,
                allowed_mentions=self.mention_authority(cursor, endpoint))
        return content_payload(rendered, row["part_index"], plain_fallback=row["variant"] == "plain_fallback")

    def prepare(self, token, delivery_id, rate_permit):
        if not isinstance(rate_permit, LocalRatePermit):
            raise OutboxInvariantError("rate_permit_required")
        with self._permit_lock:
            if rate_permit in self._used_permits:
                raise OutboxInvariantError("rate_permit_already_used")
            self._used_permits[rate_permit] = True
        now = int(self.clock())
        fields = None
        with self.deliveries.transaction() as cursor:
            loaded = self._load(cursor, token, delivery_id, now)
            if isinstance(loaded, SendOutcome):
                return loaded
            row, endpoint, endpoint_row = loaded
            choice = self._choice(cursor, token, row, endpoint, endpoint_row, rate_permit, now)
            if isinstance(choice, PolicyWait):
                return self._wait(cursor, row, choice.reason, choice.retry_at, now)
            if not rate_permit.matches(endpoint, choice.send_mode, certification=self.certification):
                return self._wait(cursor, row, "mode_changed", now + 1, now)
            author = snapshot(cursor, endpoint.account)
            try:
                payload = self._content(cursor, row, endpoint, token=token, now=now)
            except RecipientEvidenceUnavailable:
                return self._wait(cursor, row, "recipient_evidence_unavailable", now + 15, now)
            if choice.send_mode == "passive":
                if (choice.credential_kind not in {"msg_id", "event_id"}
                        or not isinstance(choice.credential_value, str) or not choice.credential_value
                        or type(choice.msg_seq) is not int or not 1 <= choice.msg_seq <= 65535):
                    raise OutboxInvariantError("invalid_window_allocation")
                payload[choice.credential_kind] = choice.credential_value
                payload["msg_seq"] = choice.msg_seq
            # Quotation is separate from the send credential. Only one exact
            # non-ambiguous mapping of the actual source may authorize a quote.
            indexes = cursor.execute("""SELECT index_value,ambiguous FROM qq_official_message_indexes
                WHERE account_key=? AND endpoint_key=? AND native_message_id=?
                  AND index_kind IN ('msg_idx','ref_idx')""",
                (token.account_key, endpoint.key, row["reply_to_native_message_id"])).fetchall()
            if indexes and not any(r["ambiguous"] for r in indexes) and len({r["index_value"] for r in indexes}) == 1:
                payload["message_reference"] = {"message_id": indexes[0]["index_value"]}
            wire = stable_json(payload)
            ephemeral = row["payload_mode"] == "ephemeral"
            wire_digest = (keyed_digest(self.pepper, "wire", [token.account_key, endpoint.key,
                row["plan_id"], delivery_id, row["attempts"] + 1, token.epoch, wire]) if ephemeral else digest(wire))
            stored_wire, storage_digest = (None if ephemeral else wire), None
            if row["payload_mode"] == "protected":
                stored_wire, storage_digest, verified_wire_digest = envelope_for_wire(load_protected_ref(cursor, row), wire)
                if verified_wire_digest != wire_digest:
                    raise OutboxInvariantError("protected_wire_commitment_conflict")
            attempt_fields = dict(account_key=token.account_key, endpoint_key=endpoint.key, fencing_epoch=token.epoch,
                endpoint_generation=row["endpoint_generation"], route_epoch=row["route_epoch"],
                capability_revision=choice.capability_revision, bot_actor_key=author.bot_actor.key,
                bot_identity_source=author.source, bot_identity_revision=author.revision, send_mode=choice.send_mode,
                window_id=choice.window_id, msg_seq=choice.msg_seq,
                window_charge_state="reserved" if choice.send_mode == "passive" else "none",
                payload_mode=row["payload_mode"], payload_digest=wire_digest, digest_scheme=row["digest_scheme"],
                digest_key_id=row["digest_key_id"], wire_payload_json=stored_wire, wire_storage_digest=storage_digest, send_by_at=choice.send_by_at)
            context = OfficialAttemptContext(token, row["attempts"], tuple(attempt_fields.items()))
            delivery = self.deliveries.begin_direct_attempt(delivery_id, now=now,
                transaction_cursor=cursor, official_context=context)
            if delivery is None:
                raise OutboxInvariantError("delivery_claim_conflict")
            reserve_send_policy(cursor, token=token, choice=choice, attempt_id=delivery.current_attempt_id, now=now,
                control_reply_id=control_id_for_delivery(cursor, row))
            if row['purpose'] == 'approval_otp':
                self.approval_authority.bind_attempt(cursor, token=token, delivery=row,
                    attempt_id=delivery.current_attempt_id, now=now)
            control_id = control_id_for_delivery(cursor, row)
            if control_id is not None:
                claim_control_attempt(cursor, token, control_id=control_id, attempt_id=delivery.current_attempt_id, now=now)
            fields = dict(delivery_id=delivery_id, attempt_id=delivery.current_attempt_id,
                attempt_number=delivery.attempts, fencing_epoch=token.epoch, endpoint=endpoint,
                wire_json=wire, payload_digest=wire_digest, send_by_at=choice.send_by_at, payload_mode=row["payload_mode"])
        reservation = issue_reservation(**fields)
        self._invocations[reservation] = "reserved"
        return reservation

    def mark_dispatching(self, token, reservation):
        require_issued(reservation)
        now = int(self.clock())
        with self.deliveries.transaction() as cursor:
            assert_epoch(cursor, token)
            if token.epoch != reservation.fencing_epoch or token.account_key != reservation.endpoint.account.key:
                return False
            row = cursor.execute("""SELECT d.*,a.send_mode,a.window_id,a.capability_revision AS attempt_capability_revision
                FROM deliveries d JOIN delivery_attempts a
                ON a.attempt_id=d.current_attempt_id WHERE d.delivery_id=? AND d.account_key=?
                AND d.current_attempt_id=? AND d.claim_epoch=? AND d.status='sending'
                AND a.state='reserved' AND a.payload_digest=? FOR UPDATE OF d,a""",
                (reservation.delivery_id, token.account_key, reservation.attempt_id, token.epoch, reservation.payload_digest)).fetchone()
            if row is None or now >= reservation.send_by_at:
                return False
            # A prior attempt can acquire positive evidence after this retry
            # was reserved. Recheck at the last durable pre-send boundary.
            if cursor.execute("""SELECT 1 FROM delivery_receipts r JOIN delivery_attempts a
                ON a.attempt_id=r.attempt_id WHERE a.delivery_id=? LIMIT 1""",
                (reservation.delivery_id,)).fetchone():
                return False
            endpoint = reservation.endpoint
            route = cursor.execute("SELECT * FROM qq_official_endpoints WHERE endpoint_key=? FOR UPDATE", (endpoint.key,)).fetchone()
            if route is None or route["generation"] != row["endpoint_generation"] or route["route_epoch"] != row["route_epoch"]:
                return False
            try:
                authorized = self._authorized(cursor, token, row, endpoint, ActorKey.parse(row["requester_actor_key"]), now)
            except RecipientEvidenceUnavailable:
                # Local issued reservation proves request_started has not run.
                # Sender settles confirmed-unsent and releases reserved charges.
                return False
            if not authorized or snapshot(cursor, endpoint.account) is None:
                return False
            if not self._control_current(cursor, row, endpoint, ActorKey.parse(row["requester_actor_key"]), now):
                return False
            try:
                # Native mentions are separately authorized: admission of the
                # requester alone does not preserve every frozen mention grant.
                payload = self._content(cursor, row, endpoint, token=token, now=now)
                if row["payload_mode"] == "protected":
                    attempt = cursor.execute("SELECT * FROM delivery_attempts WHERE attempt_id=?", (reservation.attempt_id,)).fetchone()
                    _, routing = verify_protected_attempt(cursor, row, attempt, reservation=reservation)
                    if stable_json({**payload, **routing}) != reservation.wire_json:
                        return False
            except Exception:
                return False
            if route["capability_revision"] != row["attempt_capability_revision"]:
                return False
            if row["purpose"] == "progress":
                if row["send_mode"] == "passive":
                    from .send_policy import progress_window_used
                    if progress_window_used(cursor, account_key=token.account_key,
                            window_id=row["window_id"], exclude_attempt_id=reservation.attempt_id):
                        return False
                elif route["proactive_state"] != "allowed":
                    return False
            if row["send_mode"] == "passive":
                if cursor.execute("""SELECT 1 FROM qq_official_reply_windows
                    WHERE window_id=? AND account_key=? AND endpoint_key=? AND state='active'
                      AND expires_at>? AND endpoint_generation=? AND route_epoch=?""",
                    (row["window_id"], token.account_key, endpoint.key, now,
                     row["endpoint_generation"], row["route_epoch"])).fetchone() is None:
                    return False
            elif route["proactive_state"] == "denied":
                return False
            cooldown = route["passive_blocked_until" if row["send_mode"] == "passive" else "proactive_blocked_until"]
            if cooldown > now:
                return False
            if row['payload_mode'] == 'ephemeral' and not self._ephemeral_current(cursor, token, row, now):
                return False
            cursor.execute("UPDATE delivery_attempts SET state='dispatched',dispatched_at=? WHERE attempt_id=? AND state='reserved'",
                           (now, reservation.attempt_id))
            changed = cursor.rowcount == 1
        if changed:
            self._invocations[reservation] = "ready"
        return changed

    def request_started(self, reservation):
        """Sender's synchronous, single-use handoff immediately before HTTP."""
        require_issued(reservation)
        if self._invocations.get(reservation) != "ready" or int(self.clock()) >= reservation.send_by_at:
            raise OutboxInvariantError("request_handoff_unavailable")
        self._invocations[reservation] = "invoked"
        return reservation.payload()

    def cancel_unsent(self, token, reservation, *, reason="request_not_started"):
        from .settlements import cancel_before_invocation
        require_issued(reservation)
        if self._invocations.get(reservation) not in {"reserved", "ready"}:
            raise OutboxInvariantError("no_local_unsent_proof")
        with self.deliveries.transaction() as cursor:
            result = cancel_before_invocation(cursor, token=token, reservation=reservation,
                                              now=int(self.clock()), reason=reason)
        self._invocations[reservation] = "settled"
        self.forget_ephemeral(reservation.delivery_id)
        return result

    def fail(self, token, reservation, error):
        from src.qq_official.errors import QQAPIError
        from .format_fallback import classify_format_rejection
        from .settlements import settle_failure
        require_issued(reservation)
        now = int(self.clock())
        with self.deliveries.transaction() as cursor:
            assert_epoch(cursor, token)
            row = cursor.execute("SELECT * FROM deliveries WHERE delivery_id=? AND account_key=?",
                                 (reservation.delivery_id, token.account_key)).fetchone()
            plain_digest = None
            if (row is not None and row["payload_mode"] == "persistent"
                    and row["variant"] == "primary" and not row["format_fallback_used"]
                    and isinstance(error, QQAPIError) and classify_format_rejection(error) is not None):
                try:
                    if json.loads(reservation.wire_json).get("msg_type") == 2:
                        variant_row = dict(row)
                        variant_row["variant"] = "plain_fallback"
                        plain_digest = digest(stable_json(self._content(cursor, variant_row, reservation.endpoint)))
                except Exception:
                    # Recovering an optional alternate format must not prevent
                    # settlement of an already invoked physical attempt.
                    plain_digest = None
            result = settle_failure(cursor, token=token, reservation=reservation, error=error, now=now,
                                    frozen_plain_digest=plain_digest)
        self._invocations[reservation] = "settled"
        self.forget_ephemeral(reservation.delivery_id)
        return result

    def capture_receipt(self, reservation, body):
        from .receipts import record_receipt_evidence
        with self.deliveries.transaction() as cursor:
            receipt_id = record_receipt_evidence(cursor, reservation=reservation, body=body, now=int(self.clock()))
        self._invocations[reservation] = "settled"
        self.forget_ephemeral(reservation.delivery_id)
        return receipt_id

    def finalize(self, token, receipt_id, *, ledger, journal=None):
        from .receipts import finalize_receipt
        with self.deliveries.transaction() as cursor:
            now = int(self.clock())
            result = finalize_receipt(cursor, token=token, receipt_id=receipt_id,
                                    ledger=ledger, journal=journal, now=now)
            sync_control_terminal(cursor, token, result.delivery_id, now)
            return result

    def recover(self, token):
        from .settlements import recover_account_sends
        with self.deliveries.transaction() as cursor:
            now = int(self.clock())
            self._prune_progress(now)
            live = tuple(key for key, emission in self._progress.items()
                         if emission.emitter.token is token and emission.emitter.epoch == token.epoch)
            return recover_account_sends(cursor, token=token, now=now, live_progress=live)

    def pending_receipts(self, token, *, limit=100, after_receipt_id=None):
        """Read one fenced page of unapplied evidence, without recovery writes.

        Receipt IDs provide a stable exclusive cursor independent of observation
        clocks. The sender wraps at the end and rate-limits sweeps, so unresolved
        earlier receipts cannot hide later evidence or cause a busy retry loop.
        A receipt appended behind the cursor is found on the next complete pass.
        """
        if (type(limit) is not int or not 1 <= limit <= 100
                or (after_receipt_id is not None and (
                    not isinstance(after_receipt_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", after_receipt_id)))):
            raise OutboxInvariantError("invalid_receipt_scan")
        with self.deliveries.transaction() as cursor:
            assert_epoch(cursor, token)
            rows = cursor.execute("""SELECT receipt_id FROM delivery_receipts
                WHERE account_key=? AND applied_at IS NULL AND receipt_id>?
                ORDER BY receipt_id LIMIT ?""",
                (token.account_key, after_receipt_id or "", limit)).fetchall()
        return tuple(row["receipt_id"] for row in rows)

    def forget_ephemeral(self, delivery_id):
        self._ephemeral.pop(delivery_id, None)
