"""Delivery records used by the per-conversation sender.

Plans remain an interface to existing turn/file producers until S3/S6. Sending
uses no reply-window reservations, quota tables or proof/permit objects.
"""
from __future__ import annotations

from contextlib import nullcontext
import json
import time

from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from src.plugins.ai_chat.message_ir import MediaNode, MessageBody, TextNode, body_to_json
from .binding import control_for_delivery, control_authorized
from .protected_payload import envelope_for_wire, validate_content
from .protected_plans import ProtectedBinding, protected_rendered, load_protected_ref, binding_for
from .identity_store import snapshot
from .lease import assert_epoch
from .send_types import OfficialAttemptContext, OutboxInvariantError, SendOutcome, SendReservation, digest, stable_json
from .plans import DEFAULT_MAX_WAIT_SECONDS, enqueue_plan, keyed_digest
from .renderer import content_payload, render_plan, restore_plan


def begin_official_attempt(cursor, delivery_id, context, now):
    assert_epoch(cursor, context.token)
    row = cursor.execute("SELECT * FROM deliveries WHERE delivery_id=? AND account_key=? FOR UPDATE",
        (delivery_id, context.token.account_key)).fetchone()
    if row is None or row['status'] != 'pending' or row['attempts'] != context.expected_attempts:
        return None
    fields = dict(delivery_id=delivery_id, attempt=row['attempts'] + 1,
        state='dispatched', detail='', started_at=now, dispatched_at=now,
        acceptance='unknown', **dict(context.fields))
    attempt = cursor.execute("INSERT INTO delivery_attempts (" + ",".join(fields)
        + ") VALUES (" + ",".join("?" for _ in fields) + ") RETURNING attempt_id", tuple(fields.values())).fetchone()
    cursor.execute("""UPDATE deliveries SET status='sending',attempts=?,lease_until=?,claim_epoch=?,
        current_attempt_id=?,wait_reason=NULL,last_error='',updated_at=? WHERE delivery_id=?""",
        (fields['attempt'], fields['send_by_at'], context.token.epoch, attempt['attempt_id'], now, delivery_id))
    return cursor.execute('SELECT * FROM deliveries WHERE delivery_id=?', (delivery_id,)).fetchone()


class OfficialSendStore:
    def __init__(self, deliveries, *, admission, pepper, clock=time.time,
                 certification='unknown', allow_newer_windows=True, allow_proactive_probe=False,
                 verified_boundary=None, mention_authority=None, max_wait_seconds=DEFAULT_MAX_WAIT_SECONDS,
                 protected_payloads=None, approval_authority=None, source_guard=None):
        self.deliveries, self.admission, self.pepper, self.clock = deliveries, admission, pepper, clock
        self.certification = certification
        self.allow_newer_windows, self.allow_proactive_probe = allow_newer_windows, allow_proactive_probe
        self.verified_boundary = verified_boundary
        self.mention_authority = mention_authority or (lambda cursor, endpoint: frozenset())
        self.max_wait_seconds = max_wait_seconds
        self.protected_payloads, self.approval_authority, self.source_guard = protected_payloads, approval_authority, source_guard
        self.file_authority = None
        self._ephemeral = {}

    def enqueue(self, token, *, rendered, requester, logical_key, source_scope_key,
                expires_at, purpose="final", source_native_message_id="",
                source_canonical_message_id=None, turn_id=None, challenge_id=None,
                transaction_cursor=None, binding_preview=None, control_reply_id=None, approval_source_id=None, source_proof=None):
        now = int(self.clock())
        with (nullcontext(transaction_cursor) if transaction_cursor is not None else self.deliveries.transaction()) as cursor:
            ref = enqueue_plan(cursor, token=token, rendered=rendered, requester=requester,
                logical_key=logical_key, source_scope_key=source_scope_key, expires_at=expires_at, now=now,
                purpose=purpose, source_native_message_id=source_native_message_id,
                source_canonical_message_id=source_canonical_message_id, turn_id=turn_id,
                admission=self.admission, challenge_id=challenge_id,
                pepper=self.pepper if challenge_id is not None or approval_source_id is not None else None, binding_preview=binding_preview, control_reply_id=control_reply_id,
                max_wait_seconds=self.max_wait_seconds, approval_source_id=approval_source_id,
                approval_authority=self.approval_authority, source_guard=None if purpose=='progress' else self.source_guard, source_proof=source_proof)
        if purpose in {"binding_otp", "approval_otp"} and ref.created:
            for delivery_id in ref.delivery_ids:
                self._ephemeral[delivery_id] = rendered
        return ref

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

    def enqueue_file(self, token, *, artifact, uploaded, transaction_cursor):
        cursor = transaction_cursor
        endpoint = EndpointKey.parse(artifact['endpoint_key'])
        now = int(self.clock())
        turn = cursor.execute('SELECT journal_turn_id FROM qq_official_turn_executions WHERE execution_id=?',
                              (artifact['source_execution_id'],)).fetchone()
        ref = enqueue_plan(cursor, token=token,
            rendered=render_plan(MessageBody((TextNode(0, '文件：' + artifact['filename']),)), endpoint),
            requester=ActorKey.parse(artifact['requester_actor_key']),
            logical_key='file:' + artifact['artifact_id'], source_scope_key=artifact['source_scope_key'],
            source_native_message_id=artifact['source_native_message_id'] or '',
            source_canonical_message_id=artifact['source_canonical_message_id'],
            turn_id=turn['journal_turn_id'] if turn else None,
            expires_at=min(artifact['pending_deadline_at'], uploaded.expires_at), now=now,
            purpose='requested_artifact', admission=self.admission, max_wait_seconds=self.max_wait_seconds)
        if ref.created:
            manifest = stable_json({'v': 1, 'media': {'file_info': uploaded.file_info},
                'artifact_id': artifact['artifact_id'], 'source_native_message_id': artifact['source_native_message_id'] or ''})
            body = body_to_json(MessageBody((MediaNode(0, 'file', name=artifact['filename'], mime=artifact['mime'],
                source_type='qq_official_file', raw_data={'artifact_id': artifact['artifact_id'],
                    'sha256': artifact['sha256'], 'size': artifact['byte_size']}),)))
            cursor.execute('UPDATE delivery_plans SET manifest_json=?,manifest_digest=? WHERE plan_id=?',
                           (manifest, digest(manifest), ref.plan_id))
            cursor.execute('UPDATE deliveries SET body_json=? WHERE plan_id=?', (body, ref.plan_id))
        return ref

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
            if 'media' in manifest:
                from .file_authority import assert_file_current
                assert_file_current(cursor, token=token, artifact_id=manifest['artifact_id'],
                    admission=self.admission, now=now, plan_id=row['plan_id'], source_authority=self.file_authority)
                return {'msg_type': 7, 'media': manifest['media']}
            rendered = restore_plan(manifest["rendered"], endpoint=endpoint,
                allowed_mentions=self.mention_authority(cursor, endpoint), max_chars=20000, max_parts=16)
        return content_payload(rendered, row["part_index"], plain_fallback=row["variant"] == "plain_fallback")

    def list_due(self, token, *, limit=100):
        now = int(self.clock())
        with self.deliveries.transaction() as cursor:
            assert_epoch(cursor, token)
            cursor.execute("""UPDATE deliveries SET status='failed',wait_reason='delivery_expired',updated_at=?
                WHERE account_key=? AND status='pending' AND expires_at<=?""", (now, token.account_key, now))
            rows = cursor.execute("""SELECT delivery_id FROM deliveries WHERE account_key=?
                AND status='pending' AND next_attempt_at<=? ORDER BY delivery_id LIMIT ?""",
                (token.account_key, now, limit)).fetchall()
            cursor.execute("""UPDATE qq_official_control_decisions c SET state='terminal',updated_at=?
                FROM deliveries d WHERE c.delivery_id=d.delivery_id AND c.account_key=d.account_key
                AND c.account_key=? AND c.state<>'terminal'
                AND d.status IN ('committed','failed','cancelled','ambiguous')""", (now, token.account_key))
        return tuple(row['delivery_id'] for row in rows)

    def get(self, token, delivery_id):
        with self.deliveries.transaction() as cursor:
            return cursor.execute('SELECT * FROM deliveries WHERE delivery_id=? AND account_key=?',
                (delivery_id, token.account_key)).fetchone()

    def latest_inbound(self, endpoint):
        with self.deliveries.transaction() as cursor:
            return cursor.execute("""SELECT native_message_id,received_at FROM qq_official_inbound_events
                WHERE endpoint_key=? AND native_message_id IS NOT NULL
                AND event_type IN ('GROUP_AT_MESSAGE_CREATE','GROUP_MESSAGE_CREATE','C2C_MESSAGE_CREATE')
                AND echo_state='not_echo' AND status NOT IN ('quarantined','revoked')
                ORDER BY accepted_order DESC LIMIT 1""", (endpoint.key,)).fetchone()

    def passive_history(self, endpoint, msg_id):
        # Seed RAM counters after restart from actual attempts; no quota table.
        with self.deliveries.transaction() as cursor:
            return cursor.execute("""SELECT COALESCE(MAX(a.msg_seq),0) AS seq,
                COUNT(*) FILTER (WHERE a.acceptance<>'confirmed_no') AS used
                FROM delivery_attempts a JOIN deliveries d USING(delivery_id)
                WHERE a.endpoint_key=? AND a.send_mode='passive'
                AND COALESCE(a.wire_payload_json::jsonb->>'msg_id',
                    a.wire_payload_json::jsonb#>>'{routing,msg_id}',d.reply_to_native_message_id)=?""",
                (endpoint.key,msg_id)).fetchone()

    def route(self, endpoint):
        with self.deliveries.transaction() as cursor:
            return cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?', (endpoint.key,)).fetchone()

    def deny_proactive(self, token, endpoint):
        with self.deliveries.transaction() as cursor:
            assert_epoch(cursor, token)
            cursor.execute("UPDATE qq_official_endpoints SET proactive_state='denied',updated_at=? WHERE endpoint_key=?",
                (int(self.clock()), endpoint.key))

    def prepare(self, token, delivery_id, *, send_mode, msg_id=None, msg_seq=None):
        now = int(self.clock())
        with self.deliveries.transaction() as cursor:
            assert_epoch(cursor, token)
            row = cursor.execute('SELECT * FROM deliveries WHERE delivery_id=? AND account_key=? FOR UPDATE',
                (delivery_id, token.account_key)).fetchone()
            if row is None:
                return SendOutcome('failed', 'delivery_not_found', delivery_id)
            if row['status'] != 'pending':
                return SendOutcome(row['status'], row['wait_reason'] or '', delivery_id)
            endpoint = EndpointKey.parse(row['endpoint_key'])
            actor = ActorKey.parse(row['requester_actor_key'])
            route = cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?', (endpoint.key,)).fetchone()
            control = control_for_delivery(cursor, row)
            authorized = control_authorized(cursor, control, endpoint, actor) if control else self.admission(cursor, endpoint, actor)
            if row['purpose'] == 'approval_otp':
                authorized = authorized and self.approval_authority.delivery_current(cursor, token=token, delivery=row, now=now)
            if not authorized or route['membership_state'] != 'present' or route['binding_state'] == 'revoked':
                return self._finish(cursor, token, row, 'failed', 'delivery_not_authorized', now)
            if now >= row['expires_at']:
                return self._finish(cursor, token, row, 'failed', 'delivery_expired', now)
            if row['payload_mode'] == 'ephemeral' and delivery_id not in self._ephemeral:
                return self._finish(cursor, token, row, 'failed', 'ephemeral_payload_lost', now)
            payload = self._content(cursor, row, endpoint, token=token, now=now)
            if send_mode == 'passive':
                payload.update(msg_id=msg_id, msg_seq=msg_seq)
            indexes = cursor.execute("""SELECT index_value,ambiguous FROM qq_official_message_indexes
                WHERE endpoint_key=? AND native_message_id=? AND index_kind IN ('msg_idx','ref_idx')""",
                (endpoint.key, row['reply_to_native_message_id'])).fetchall()
            if indexes and not any(r['ambiguous'] for r in indexes) and len({r['index_value'] for r in indexes}) == 1:
                payload['message_reference'] = {'message_id': indexes[0]['index_value']}
            wire = stable_json(payload)
            ephemeral = row['payload_mode'] == 'ephemeral'
            wire_digest = keyed_digest(self.pepper, 'wire', [delivery_id, row['attempts'] + 1, wire]) if ephemeral else digest(wire)
            stored_wire, storage_digest = (None if ephemeral else wire), None
            if row['payload_mode'] == 'protected':
                stored_wire, storage_digest, _ = envelope_for_wire(load_protected_ref(cursor, row), wire)
            author = snapshot(cursor, endpoint.account)
            fields = dict(account_key=token.account_key, endpoint_key=endpoint.key, fencing_epoch=token.epoch,
                endpoint_generation=row['endpoint_generation'], route_epoch=row['route_epoch'],
                capability_revision=route['capability_revision'], bot_actor_key=author.bot_actor.key,
                bot_identity_source=author.source, bot_identity_revision=author.revision, send_mode=send_mode,
                window_id=None, msg_seq=msg_seq, window_charge_state='none', payload_mode=row['payload_mode'],
                payload_digest=wire_digest, digest_scheme=row['digest_scheme'], digest_key_id=row['digest_key_id'],
                wire_payload_json=stored_wire, wire_storage_digest=storage_digest, send_by_at=now + 30)
            claimed = self.deliveries.begin_direct_attempt(delivery_id, now=now, transaction_cursor=cursor,
                official_context=OfficialAttemptContext(token, row['attempts'], tuple(fields.items())))
            if claimed is None:
                return SendOutcome('sending', '', delivery_id)
            if row['purpose'] == 'approval_otp' and row['attempts'] == 0:
                self.approval_authority.bind_attempt(cursor, token=token, delivery=row,
                    attempt_id=claimed.current_attempt_id, now=now)
            return SendReservation(delivery_id, claimed.current_attempt_id, claimed.attempts, token.epoch,
                endpoint, wire, wire_digest, now + 30, row['payload_mode'])

    def _finish(self, cursor, token, row, status, reason, now):
        cursor.execute('UPDATE deliveries SET status=?,wait_reason=?,last_error=?,lease_until=NULL,updated_at=? WHERE delivery_id=?',
            (status, reason or None, '' if row['payload_mode']=='ephemeral' else reason, now, row['delivery_id']))
        return SendOutcome(status, reason, row['delivery_id'])

    def fail(self, token, delivery_id, reason, *, reservation=None, error=None, retry=False, plain=False):
        now = int(self.clock())
        unknown = error is not None and error.accepted is not False
        with self.deliveries.transaction() as cursor:
            assert_epoch(cursor, token)
            row = cursor.execute('SELECT * FROM deliveries WHERE delivery_id=? AND account_key=? FOR UPDATE',
                (delivery_id, token.account_key)).fetchone()
            if reservation is not None:
                cursor.execute("""UPDATE delivery_attempts SET state=?,acceptance=?,finished_at=?,failure_category=?,
                    failure_stage=?,http_status=?,platform_code=? WHERE attempt_id=?""",
                    ('ambiguous' if unknown else 'confirmed_unaccepted', 'unknown' if unknown else 'confirmed_no',
                     now, reason, error.stage if error else None, error.status if error else None,
                     error.code if error else None, reservation.attempt_id))
            if plain:
                cursor.execute("UPDATE deliveries SET variant='plain_fallback',format_fallback_used=TRUE WHERE delivery_id=?", (delivery_id,))
            return self._finish(cursor, token, row, 'ambiguous' if unknown else 'pending' if retry else 'failed', reason, now)

    def capture_receipt(self, reservation, body):
        from .send_ledger import record_receipt
        with self.deliveries.transaction() as cursor:
            return record_receipt(cursor, reservation, body, int(self.clock()))

    def finalize(self, token, receipt_id, *, ledger, journal=None):
        from .send_ledger import finalize_receipt
        with self.deliveries.transaction() as cursor:
            result = finalize_receipt(cursor, token=token, receipt_id=receipt_id, ledger=ledger, journal=journal, now=int(self.clock()))
            return result

    def recover(self, token):
        # A crash cannot tell us whether an in-flight POST arrived. Never resend it.
        with self.deliveries.transaction() as cursor:
            assert_epoch(cursor, token)
            cursor.execute("""UPDATE delivery_attempts SET state='ambiguous',acceptance='unknown',finished_at=?
                WHERE account_key=? AND state IN ('reserved','dispatched')""", (int(self.clock()), token.account_key))
            cursor.execute("""UPDATE deliveries SET status='ambiguous',wait_reason='restart_during_send',lease_until=NULL
                WHERE account_key=? AND status='sending'""", (token.account_key,))
            return tuple(r['receipt_id'] for r in cursor.execute('SELECT receipt_id FROM delivery_receipts WHERE account_key=? AND applied_at IS NULL', (token.account_key,)).fetchall())

    def forget_ephemeral(self, delivery_id):
        self._ephemeral.pop(delivery_id, None)
