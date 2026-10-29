"""Console-authenticated, source-bound text/file verification through M3.

No network or file generation happens here. The existing immutable artifact
and its existing file job may finish a bounded verification; unknown uploads or
sends retain their original M3/M6 no-retry rules. No caller DTO grants access.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import weakref

from src.plugins.ai_chat.message_ir import MessageBody,TextNode,body_to_json
from src.plugins.ai_chat.platform_types import ActorKey,EndpointKey
from .authority import OwnerAuthority
from .migration_types import MigrationDenied,VerificationEvidence
from .renderer import render_plan


@dataclass(frozen=True,eq=False,repr=False)
class _Request:
    grant: object
    payload: dict
    source: dict
    window: dict
    owner: object
    expires_at: int


def verification_body(nonce):
    return MessageBody((TextNode(0,'迁移文本验证：'+nonce),))


class VerificationHost:
    def __init__(self, *, journal, control):
        self.journal,self.control=journal,control
        self._issued=weakref.WeakKeyDictionary()
        self.owners=OwnerAuthority()

    def prepare(self,cursor,*,grant,request,outbox,token,now):
        """Caller acquired the real cookie+CSRF grant for this exact request."""
        state,route=self.journal._load(cursor,request['migration_id'])
        self.control.begin(cursor,grant,action='verification_send',request=request,
            preview_digest=state['preview_digest'],now=now)
        if request['kind'] not in {'text','file'} or (request['kind']=='file') != (request['artifact_id'] is not None):
            raise MigrationDenied('verification_kind_mismatch')
        source=cursor.execute('SELECT * FROM qq_migration_native_sources WHERE inbox_id=?', (request['inbox_id'],)).fetchone()
        if source is None or source['account_key']!=token.account_key or source['endpoint_key']!=route['target_endpoint_key']:
            raise MigrationDenied('verification_original_source_unavailable')
        endpoint,actor=EndpointKey.parse(source['endpoint_key']),ActorKey.parse(source['actor_key'])
        owner=self.owners.owner_for_actor(cursor,endpoint,actor)
        if owner is None:raise MigrationDenied('verification_owner_unavailable')
        window=cursor.execute('''SELECT * FROM qq_official_reply_windows WHERE account_key=? AND endpoint_key=?
            AND credential_kind='msg_id' AND credential_value=? AND state='active' AND expires_at>?
            AND endpoint_generation=? AND route_epoch=? AND used_count+reserved_count<max_replies''',
            (token.account_key,endpoint.key,source['native_message_id'],now,source['endpoint_generation'],source['endpoint_route_epoch'])).fetchone()
        if window is None:raise MigrationDenied('verification_original_window_unavailable')
        expires=min(now+30,window['expires_at'],state['verification_until'] or 0)
        if expires<=now:raise MigrationDenied('verification_expired')
        value=_Request(grant,dict(request),dict(source),dict(window),owner,expires)
        self._issued[value]=cursor.execute('SELECT txid_current() AS txid').fetchone()['txid']
        proof=self.journal.issue_verification(cursor,migration_id=request['migration_id'],kind=request['kind'],actor=actor,source=value,now=now)
        old=cursor.execute('SELECT * FROM migration_verification_sources WHERE request_id=?',(proof.request_id,)).fetchone()
        fields=dict(request_id=proof.request_id,migration_id=proof.migration_id,kind=proof.kind,
            account_key=token.account_key,endpoint_key=endpoint.key,actor_key=actor.key,
            inbox_id=source['inbox_id'],window_id=window['window_id'],owner_grant_id=owner.grant_id,
            owner_revision=owner.revision,artifact_id=request['artifact_id'],created_at=now,expires_at=expires)
        if old is not None:
            # A second request cannot reset the original window, deadline or
            # replace a verification artifact after a timeout/restart.
            fixed={k:v for k,v in fields.items() if k not in {'created_at','expires_at'}}
            if any(old[k]!=v for k,v in fixed.items()):raise MigrationDenied('verification_source_already_fixed')
            fields=dict(old)
        else:
            cursor.execute('INSERT INTO migration_verification_sources ('+','.join(fields)+') VALUES ('+','.join('?' for _ in fields)+')',tuple(fields.values()))
        self._current(cursor,proof,now)
        if proof.kind=='file':
            self._artifact(cursor,fields)
            plan=None  # Existing file worker performs the only upload/send path.
        else:
            canonical=cursor.execute('SELECT canonical_message_id FROM qq_official_inbound_events WHERE inbox_id=?',(source['inbox_id'],)).fetchone()[0]
            plan=outbox.enqueue(token,rendered=render_plan(verification_body(proof.nonce),endpoint,max_parts=1,markdown=False),
                requester=actor,logical_key='migration-verification:'+proof.request_id,source_scope_key=endpoint.scope.key,
                source_native_message_id=source['native_message_id'],source_canonical_message_id=canonical,
                expires_at=fields['expires_at'],transaction_cursor=cursor,source_proof=proof)
            self.journal.bind_verification_plan(cursor,proof,plan_id=plan.plan_id,now=now)
        self.control.audit(cursor,grant,resource_id=proof.migration_id,now=now)
        self._issued.pop(value,None)
        return plan

    def request(self,cursor,source,*,migration_id,endpoint,actor,kind,nonce,deadline,now):
        if (type(source) is not _Request or self._issued.get(source)!=cursor.execute('SELECT txid_current() AS txid').fetchone()['txid']
                or source.payload['migration_id']!=migration_id or source.payload['kind']!=kind
                or source.source['endpoint_key']!=endpoint.key or source.source['actor_key']!=actor.key
                or source.expires_at>deadline or now>=source.expires_at):
            raise MigrationDenied('verification_request_not_issued')
        state,_=self.journal._load(cursor,migration_id)
        self.control.control(cursor,source.grant,action='verification_send',preview_digest=state['preview_digest'],now=now)

    def _source(self,cursor,proof):
        row=cursor.execute('SELECT * FROM migration_verification_sources WHERE request_id=?',(proof.request_id,)).fetchone()
        if (row is None or row['migration_id']!=proof.migration_id or row['kind']!=proof.kind
                or row['endpoint_key']!=proof.endpoint.key or row['actor_key']!=proof.actor.key):
            raise MigrationDenied('verification_source_unavailable')
        return row

    def _current(self,cursor,proof,now):
        row=self._source(cursor,proof)
        owner=self.owners.owner_for_actor(cursor,proof.endpoint,proof.actor)
        window=cursor.execute('SELECT * FROM qq_official_reply_windows WHERE window_id=?',(row['window_id'],)).fetchone()
        source=cursor.execute('SELECT * FROM qq_migration_native_sources WHERE inbox_id=?',(row['inbox_id'],)).fetchone()
        if (now>=row['expires_at'] or owner is None or (owner.grant_id,owner.revision)!=(row['owner_grant_id'],row['owner_revision'])
                or window is None or window['state']!='active' or window['expires_at']<=now
                or window['endpoint_generation']!=proof.endpoint_generation or window['route_epoch']!=proof.endpoint_route_epoch
                or source is None or source['native_message_id']!=window['credential_value']
                or source['endpoint_generation']!=proof.endpoint_generation or source['endpoint_route_epoch']!=proof.endpoint_route_epoch):
            raise MigrationDenied('verification_source_expired')
        return row

    def _artifact(self,cursor,source):
        row=cursor.execute('SELECT * FROM qq_official_file_artifacts WHERE artifact_id=?',(source['artifact_id'],)).fetchone()
        original=cursor.execute('SELECT * FROM qq_official_inbound_events WHERE inbox_id=?',(source['inbox_id'],)).fetchone()
        if (row is None or row['account_key']!=source['account_key'] or row['endpoint_key']!=source['endpoint_key']
                or row['requester_actor_key']!=source['actor_key'] or row['bytes_state']!='available'
                or row['source_canonical_message_id']!=original['canonical_message_id']
                or row['source_native_message_id']!=original['native_message_id']):
            raise MigrationDenied('verification_artifact_source_mismatch')
        return row

    def file_proof(self,cursor,*,artifact,now,plan_id=None):
        source=cursor.execute('SELECT * FROM migration_verification_sources WHERE artifact_id=?',(artifact['artifact_id'],)).fetchone()
        if source is None:return None
        intent,proof=self.journal._verification_current(cursor,source['migration_id'],'file',now)
        self._current(cursor,proof,now)
        actual=self._artifact(cursor,source)
        fields=('artifact_id','account_key','endpoint_key','requester_actor_key','source_execution_id',
            'source_canonical_message_id','source_native_message_id','source_scope_key','sha256','byte_size','mime','filename')
        if any(actual[k]!=artifact[k] for k in fields) or (plan_id is not None and intent['plan_id']!=plan_id):
            raise MigrationDenied('verification_file_plan_mismatch')
        return proof

    def plan(self,cursor,proof,plan_id,*,now):
        source=self._current(cursor,proof,now)
        plan=cursor.execute('SELECT * FROM delivery_plans WHERE plan_id=?',(plan_id,)).fetchone()
        parts=cursor.execute('SELECT * FROM deliveries WHERE plan_id=? ORDER BY part_index',(plan_id,)).fetchall()
        original=cursor.execute('SELECT * FROM qq_official_inbound_events WHERE inbox_id=?',(source['inbox_id'],)).fetchone()
        if (plan is None or len(parts)!=1 or plan['expires_at']>source['expires_at']
                or parts[0]['reply_to_native_message_id']!=original['native_message_id']
                or plan['source_canonical_message_id']!=original['canonical_message_id']):
            raise MigrationDenied('verification_plan_source_mismatch')
        if parts[0]['current_attempt_id'] is not None:
            attempt=cursor.execute('SELECT send_mode,window_id FROM delivery_attempts WHERE attempt_id=?',
                (parts[0]['current_attempt_id'],)).fetchone()
            if attempt is None or attempt['send_mode']!='passive' or attempt['window_id']!=source['window_id']:
                raise MigrationDenied('verification_original_window_required')
        if proof.kind=='text':
            if (plan['payload_mode']!='persistent' or plan['purpose']!='final'
                    or parts[0]['body_json']!=body_to_json(verification_body(proof.nonce))):
                raise MigrationDenied('verification_text_mismatch')
        else:
            artifact=self._artifact(cursor,source)
            manifest=json.loads(plan['manifest_json'])
            ref=manifest.get('protected_ref',{})
            if (plan['payload_mode']!='protected' or plan['purpose']!='requested_artifact'
                    or ref.get('artifact_id')!=artifact['artifact_id']):
                raise MigrationDenied('verification_file_required')
            credential=cursor.execute('SELECT * FROM qq_official_file_credentials WHERE credential_ref=? AND artifact_id=?',
                (ref.get('credential_ref'),artifact['artifact_id'])).fetchone()
            if credential is None or credential['content_commitment']!=ref.get('content_commitment'):
                raise MigrationDenied('verification_file_credential_missing')
            intent=cursor.execute('SELECT plan_id FROM migration_verification_intents WHERE request_id=?',(proof.request_id,)).fetchone()
            if intent['plan_id'] is not None and cursor.execute('''SELECT 1 FROM qq_official_file_message_plans
                WHERE plan_id=? AND artifact_id=? AND delivery_id=?''',(plan_id,artifact['artifact_id'],parts[0]['delivery_id'])).fetchone() is None:
                raise MigrationDenied('verification_file_association_missing')

    def receipt(self,cursor,source,*,migration_id,target_endpoint,nonce,now):
        # Historical applied receipt only: current owner/window expiry cannot
        # erase a fact. Journal separately gates which migration may complete.
        if not isinstance(source,str) or not 1<=len(source)<=64:raise MigrationDenied('verification_receipt_required')
        row=cursor.execute('''SELECT r.receipt_id,d.delivery_id,v.kind,v.nonce,v.actor_key,s.artifact_id,p.payload_mode
            FROM delivery_receipts r JOIN delivery_attempts a USING(attempt_id)
            JOIN deliveries d USING(delivery_id) JOIN delivery_plans p USING(plan_id)
            JOIN migration_verification_intents v ON v.plan_id=p.plan_id
            JOIN migration_verification_sources s ON s.request_id=v.request_id
            WHERE r.receipt_id=? AND v.migration_id=? AND v.endpoint_key=? AND v.nonce=?
              AND r.applied_at IS NOT NULL AND a.state='committed' AND d.status='committed' ''',
            (source,migration_id,target_endpoint.key,nonce)).fetchone()
        if row is None:raise MigrationDenied('verification_applied_receipt_required')
        if row['kind']=='file' and (row['payload_mode']!='protected' or cursor.execute('''SELECT 1
            FROM qq_official_file_message_plans WHERE delivery_id=? AND artifact_id=?''',
            (row['delivery_id'],row['artifact_id'])).fetchone() is None):
            raise MigrationDenied('verification_file_receipt_required')
        return VerificationEvidence(row['receipt_id'],row['delivery_id'],row['kind'],row['nonce'])
