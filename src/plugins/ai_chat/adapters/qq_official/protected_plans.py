"""Protected-plan bindings and immutable historical evidence. No secret storage."""
from __future__ import annotations

from dataclasses import dataclass
import json

from src.plugins.ai_chat.message_ir import MessageBody, TextNode, body_to_json
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from .outbox_types import OutboxInvariantError, digest, stable_json
from .protected_payload import ProtectedContentRef, verify_envelope
from .renderer import render_plan

PROTECTED_TEXT = '[受保护附件：文件内容及访问凭据已从会话上下文隐去。]'
PROTECTED_BODY = body_to_json(MessageBody((TextNode(0, PROTECTED_TEXT),)))


@dataclass(frozen=True, repr=False)
class ProtectedBinding:
    endpoint: EndpointKey
    requester: ActorKey
    source_scope_key: str
    source_canonical_message_id: int | None
    source_native_message_id: str
    turn_id: int | None
    endpoint_generation: int
    route_epoch: int
    expires_at: int
    plan_id: str | None = None
    delivery_id: int | None = None

    def __repr__(self):
        return 'ProtectedBinding(<protected>)'


def protected_rendered(endpoint):
    return render_plan(MessageBody((TextNode(0, PROTECTED_TEXT),)), endpoint)


def protected_manifest(ref, source_native_message_id):
    return stable_json({'v': 2, 'mode': 'protected', 'protected_ref': ref.data(),
        'control': None, 'source_native_message_id': source_native_message_id,
        'redaction': {'kind': 'artifact', 'body_json': PROTECTED_BODY}})


def binding_for(row):
    return ProtectedBinding(EndpointKey.parse(row['endpoint_key']), ActorKey.parse(row['requester_actor_key']),
        row['source_scope_key'], row['source_canonical_message_id'], row['reply_to_native_message_id'],
        row['turn_id'], row['endpoint_generation'], row['route_epoch'], row['expires_at'],
        row['plan_id'], row['delivery_id'])


def load_protected_ref(cursor, row):
    plan = cursor.execute('SELECT * FROM delivery_plans WHERE plan_id=?', (row['plan_id'],)).fetchone()
    try:
        keys = ('account_key','endpoint_key','requester_actor_key','source_scope_key',
                'source_canonical_message_id','turn_id','endpoint_generation','route_epoch',
                'expires_at','purpose','payload_mode')
        if (plan is None or any(plan[k] != row[k] for k in keys)
                or plan['part_count'] != 1 or row['part_index'] != 0
                or row['purpose'] != 'requested_artifact' or row['payload_mode'] != 'protected'
                or row['variant'] != 'primary' or row['format_fallback_used']
                or row['digest_scheme'] != 'sha256' or row['digest_key_id'] is not None
                or plan['challenge_id'] is not None or plan['challenge_expires_at'] is not None
                or row['body_json'] != PROTECTED_BODY
                or digest(plan['manifest_json']) != plan['manifest_digest']):
            raise ValueError
        data = json.loads(plan['manifest_json'])
        ref = ProtectedContentRef(**data['protected_ref'])
        if (plan['manifest_json'] != protected_manifest(ref, row['reply_to_native_message_id'])
                or row['payload_digest'] != ref.content_commitment
                or row['content_fingerprint'] != ref.content_commitment
                or row['expires_at'] > ref.expires_at):
            raise ValueError
        return ref
    except (ValueError, TypeError, KeyError, AttributeError):
        raise OutboxInvariantError('protected_plan_integrity_conflict') from None


def verify_protected_attempt(cursor, row, attempt, *, reservation=None):
    ref = load_protected_ref(cursor, row)
    try:
        if (attempt['payload_mode'] != 'protected' or attempt['digest_scheme'] != 'sha256'
                or attempt['digest_key_id'] is not None):
            raise ValueError
        _, routing = verify_envelope(attempt['wire_payload_json'],
            storage_digest=attempt['wire_storage_digest'], wire_digest=attempt['payload_digest'], ref=ref,
            wire_json=reservation.wire_json if reservation is not None else None)
        if attempt['send_mode'] == 'passive':
            window = cursor.execute('''SELECT credential_kind,credential_value FROM qq_official_reply_windows
                WHERE window_id=? AND account_key=? AND endpoint_key=?''',
                (attempt['window_id'],attempt['account_key'],attempt['endpoint_key'])).fetchone()
            if (window is None or routing.get('msg_seq') != attempt['msg_seq']
                    or routing.get(window['credential_kind']) != window['credential_value']):
                raise ValueError
        elif any(key in routing for key in ('msg_id','event_id','msg_seq')):
            raise ValueError
        return ref, routing
    except (ValueError, TypeError, KeyError, AttributeError):
        raise OutboxInvariantError('protected_attempt_integrity_conflict') from None
