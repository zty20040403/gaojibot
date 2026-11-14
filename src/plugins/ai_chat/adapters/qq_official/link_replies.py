"""Frozen control outcome, not command text, a borrowed reply window or authority."""
from dataclasses import asdict
import hmac
import json
import re

from ...platform_types import ActorKey,EndpointKey
from .lease import assert_epoch
from .linking import LinkDecision

PROOF = ('account_epoch','endpoint_key','actor_key','scope_key','endpoint_generation','route_epoch',
    'receipt_hash','authority_hash','received_at','expires_at')
OUTCOME = ('status','challenge_ref','link_ref','entries_json','more')
STATUSES = {'sending','issued','confirming','active','expired','cancelled','denied','unsupported','limited',
    'invalid','already_linked','revoked','listed','use_unlink'}


def outcome(decision):
    if not isinstance(decision,LinkDecision) or decision.status not in STATUSES or type(decision.more) is not bool:
        raise ValueError('link_reply_outcome_invalid')
    entries = [asdict(entry) for entry in decision.entries]
    if len(entries)>32:raise ValueError('link_reply_outcome_invalid')
    for entry in entries:
        if (set(entry)!={'link_id','direction','peer','state','generation'} or not re.fullmatch(r'[0-9a-f]{32}',entry['link_id'])
            or entry['direction'] not in {'outgoing','incoming'} or not re.fullmatch(r'(群会话|私聊会话) [0-9a-f]{12}',entry['peer'])
            or entry['state'] not in {'active','revoked'} or type(entry['generation']) is not int or entry['generation']<1):
            raise ValueError('link_reply_outcome_invalid')
    if (decision.challenge_id and not re.fullmatch(r'LC-[0-9a-f]{32}',decision.challenge_id)
        or decision.link_id and not re.fullmatch(r'[0-9a-f]{32}',decision.link_id)
        or decision.status!='listed' and (entries or decision.more)):
        raise ValueError('link_reply_outcome_invalid')
    return dict(status=decision.status,challenge_ref=decision.challenge_id or None,link_ref=decision.link_id or None,
        entries_json=json.dumps(entries,ensure_ascii=False,separators=(',',':'),sort_keys=True),more=decision.more)


class LinkReplies:
    def __init__(self,ingress,admission=None):self.ingress,self.authority,self.admission = ingress,ingress.authority,admission

    def get(self,cursor,token,inbox_id):
        assert_epoch(cursor,token)
        return cursor.execute('SELECT * FROM qq_official_link_replies WHERE source_inbox_id=? AND account_key=? FOR UPDATE',
            (inbox_id,token.account_key)).fetchone()

    def source_hash(self,row):
        return self.authority._hash('qq-link-control-reply:v1',
            [row[key] for key in ('source_inbox_id','account_key',*PROOF,*OUTCOME)])

    def route(self,cursor,token,endpoint,actor):
        row = self.authority._endpoint(cursor,token,endpoint)
        if not row or not actor.matches_endpoint(endpoint) or actor.identity_kind not in {'c2c_user','group_member'}:return None
        if endpoint.kind=='private' and (not callable(self.admission) or not self.admission(cursor,endpoint,actor)):return None
        return row

    def capture(self,cursor,token,message,inbox_id,decision):
        if decision.code is not None or not self.authority._fresh(message):return  # The OTP has its separate source/plan.
        present = self.route(cursor,token,message.endpoint,message.actor)
        if present is None:return
        proof = self.authority.proof(cursor,token,message)
        if proof is None:
            if decision.status not in {'denied','unsupported'}:return  # Never downgrade a privileged snapshot.
            proof = dict(account_epoch=token.epoch,endpoint_key=message.endpoint.key,actor_key=message.actor.key,
                scope_key=present['canonical_scope_key'],endpoint_generation=present['generation'],route_epoch=present['route_epoch'],
                receipt_hash=self.authority.receipt(message),authority_hash=None,received_at=message.received_at,
                expires_at=message.platform_occurred_at+600)
        row = dict(source_inbox_id=inbox_id,account_key=token.account_key)|proof|outcome(decision)
        now = int(self.authority.clock())
        if not proof['received_at']<=now<proof['expires_at']:return
        row['source_hash'] = self.source_hash(row)
        cursor.execute('INSERT INTO qq_official_link_replies('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))

    def source(self,cursor,token,row):
        assert_epoch(cursor,token)
        if row is None or row['account_key']!=token.account_key or row['account_epoch']!=token.epoch:
            raise PermissionError('link_reply_source_unavailable')
        endpoint,actor = EndpointKey.parse(row['endpoint_key']),ActorKey.parse(row['actor_key'])
        present = self.route(cursor,token,endpoint,actor)
        if (present is None or (present['generation'],present['route_epoch'],present['canonical_scope_key'])
                !=(row['endpoint_generation'],row['route_epoch'],row['scope_key'])
            or not hmac.compare_digest(row['source_hash'],self.source_hash(row))):raise PermissionError('link_reply_source_changed')
        original = cursor.execute('SELECT * FROM qq_official_inbound_events WHERE inbox_id=?',(row['source_inbox_id'],)).fetchone()
        if (original is None or original['payload_json'] is not None or original['status']!='processed'
            or original['reason_code']!='link_control' or original['echo_state']!='not_echo'
            or (original['account_key'],original['endpoint_key'],original['link_control_actor_key'],original['link_control_receipt_hash'],
                original['received_at'],original['endpoint_generation'],original['route_epoch'])
              !=(row['account_key'],row['endpoint_key'],row['actor_key'],row['receipt_hash'],row['received_at'],row['endpoint_generation'],row['route_epoch'])
            or original['link_control_platform_at'] is None or row['expires_at']>original['link_control_platform_at']+600):
            raise PermissionError('link_reply_original_input_unavailable')
        receipt = self.authority._hash('qq-link-receipt:v1',[row['endpoint_key'],row['actor_key'],original['native_message_id'],
            original['event_id'],original['link_control_platform_at'],original['received_at']])
        if not hmac.compare_digest(receipt,row['receipt_hash']):raise PermissionError('link_reply_original_input_unavailable')
        gateway = cursor.execute('''SELECT 1 FROM qq_official_gateway_receipts WHERE account_key=? AND inbox_id=?
            AND event_type=? AND disposition='control' AND received_at=?''',
            (token.account_key,row['source_inbox_id'],original['event_type'],original['received_at'])).fetchone()
        if gateway is None:raise PermissionError('link_reply_authenticated_receipt_unavailable')
        if row['authority_hash'] is not None:
            if not self.authority.current(cursor,token,{key:row[key] for key in PROOF}):raise PermissionError('link_reply_authority_changed')
        elif (row['status'] not in {'denied','unsupported'} or row['challenge_ref'] or row['link_ref']
            or row['entries_json']!='[]' or row['more']):raise PermissionError('link_reply_authority_required')
        now = int(self.authority.clock())
        if not row['received_at']<=now<row['expires_at']:raise PermissionError('link_reply_expired')
        assert_epoch(cursor,token)
        return original
