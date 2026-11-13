"""Persistent owner-private intent identity; no HTTP, VM, or shell execution."""
from dataclasses import dataclass,field
import hashlib
import hmac
import json
import re
import secrets
import time

from ...ops_plans import fingerprint,OpsPlanError
from ...platform_types import ActorKey,EndpointKey
from .conversation_control import permitted
from .lease import assert_epoch

TERMINAL = frozenset({'succeeded','failed','denied','expired','cancelled','unknown'})


def retire_runtime(transaction,token,now,reason):
    # Recovery also runs with operations disabled: an old claim is unknown,
    # not forever "executing", and an unanswered notice must not stay live.
    with transaction() as cursor:
        assert_epoch(cursor,token)
        cursor.execute("""UPDATE ops_actions SET state=CASE WHEN state='executing' THEN 'unknown'
            WHEN expires_at<=? THEN 'expired' ELSE 'cancelled' END,
            code_hash='',error_code=?,finished_at=?,updated_at=? WHERE account_key=? AND finished_at IS NULL""",
            (now,reason,now,now,token.account_key))


@dataclass(frozen=True)
class Proposal:
    action: dict = field(repr=False)
    code: str | None = field(default=None,repr=False)


class OpsActions:
    def __init__(self,token,*,authority,admission,pepper,clock=time.time):
        if not isinstance(pepper,bytes) or len(pepper)<32:
            raise ValueError('ops_approval_key_unavailable')
        self.token,self.authority,self.admission,self.pepper,self.clock = token,authority,admission,pepper,clock

    def _source(self,cursor,job_id,owner,generation):
        assert_epoch(cursor,self.token)
        now = int(self.clock())
        row = cursor.execute('''SELECT b.*,j.status,j.lease_owner,j.lease_generation,j.lease_until
            FROM background_jobs b JOIN durable_jobs j USING(job_id) WHERE b.job_id=?''',(job_id,)).fetchone()
        if (row is None or row['account_key']!=self.token.account_key or row['state']!='running' or row['status']!='running'
                or row['lease_owner']!=owner or row['lease_generation']!=generation or row['lease_until'] is None or row['lease_until']<=now or row['deadline_at']<=now):
            raise PermissionError('ops_original_job_unavailable')
        endpoint,actor = EndpointKey.parse(row['endpoint_key']),ActorKey.parse(row['actor_key'])
        route = cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?',(endpoint.key,)).fetchone()
        if (endpoint.kind!='private' or actor.identity_kind!='c2c_user' or not actor.matches_endpoint(endpoint)
                or endpoint.account.key!=self.token.account_key or row['scope_key']!=endpoint.scope.key
                or route is None or route['canonical_scope_key']!=endpoint.scope.key
                or route['generation']!=row['endpoint_generation'] or route['route_epoch']!=row['route_epoch']
                or route['membership_state']!='present' or route['binding_state']=='revoked'
                or not permitted(cursor,endpoint,generation=row['conversation_generation'])
                or self.admission(cursor,endpoint,actor) is not True):
            raise PermissionError('ops_private_source_unavailable')
        grant = self.authority.owner_for_actor(cursor,endpoint,actor) if self.authority is not None else None
        if grant is None:
            raise PermissionError('ops_owner_unavailable')
        return dict(row),grant

    def code_hash(self,action_id,code):
        return hmac.new(self.pepper,b'ops-consent-v1:'+action_id.encode()+b':'+code.encode(),hashlib.sha256).hexdigest()

    def get(self,cursor,action_id):
        assert_epoch(cursor,self.token)
        row = cursor.execute('SELECT * FROM ops_actions WHERE action_id=? AND account_key=? FOR UPDATE',
            (action_id,self.token.account_key)).fetchone()
        if row is None:
            raise PermissionError('ops_action_unavailable')
        return dict(row)

    def check(self,cursor,row,*,binding=None):
        source,grant = self._source(cursor,row['job_id'],row['job_lease_owner'],row['job_lease_generation'])
        if (row['account_epoch']!=self.token.epoch or grant.grant_id!=row['owner_grant_id'] or grant.revision!=row['owner_revision']
                or any(row[key]!=source[key] for key in ('account_key','endpoint_key','actor_key','scope_key',
                    'endpoint_generation','route_epoch','conversation_generation'))
                or row['intent_hash']!=fingerprint([row['plan_name'],json.loads(row['parameters_json']),row['binding_hash']])
                or binding is not None and binding!=row['binding_hash']):
            raise PermissionError('ops_intent_source_changed')
        if row['state'] in {'sending','pending','approved'} and row['expires_at']<=int(self.clock()):
            raise PermissionError('ops_consent_expired')

    def close(self,cursor,action_id,*,state,error='',summary=''):
        row = self.get(cursor,action_id)
        if row['state'] in TERMINAL:
            return row
        allowed = {'succeeded','failed','unknown'} if row['state']=='executing' else {'denied','expired','cancelled'}
        if (state not in allowed or error and re.fullmatch(r'[a-z0-9_]{1,80}',error) is None
                or type(summary) is not str or len(summary.encode())>16384):
            raise ValueError('invalid_ops_outcome')
        now = int(self.clock())
        return dict(cursor.execute('''UPDATE ops_actions SET state=?,code_hash='',error_code=?,output_summary=?,
            finished_at=?,updated_at=? WHERE action_id=? RETURNING *''',(state,error,summary,now,now,action_id)).fetchone())

    def retire(self,cursor,row):
        """Never restart work after loss of the original task/authority/lease."""
        if row['state'] not in TERMINAL:
            try:
                self.check(cursor,row)
            except PermissionError:
                state = 'unknown' if row['state']=='executing' else 'expired' if row['expires_at']<=int(self.clock()) else 'cancelled'
                return self.close(cursor,row['action_id'],state=state,error='ops_source_closed')
        return row

    def request(self,cursor,*,job,plan,parameters,binding,request_id):
        source,grant = self._source(cursor,job.job_id,job.lease_owner,job.lease_generation)
        if (type(request_id) is not str or re.fullmatch(r'[A-Za-z0-9_-]{1,64}',request_id) is None
                or type(binding) is not str or re.fullmatch(r'[a-f0-9]{64}',binding) is None):
            raise OpsPlanError('invalid_ops_request')
        plan.render(parameters)  # Validation precedes the durable intent, never shell expansion.
        encoded = json.dumps(parameters,ensure_ascii=False,sort_keys=True,separators=(',',':'))
        if len(encoded.encode())>16384:
            raise OpsPlanError('ops_arguments_too_large')
        intent = fingerprint([plan.name,parameters,binding])
        old = cursor.execute('SELECT * FROM ops_actions WHERE job_id=? AND request_id=?',(job.job_id,request_id)).fetchone()
        if old is not None:
            if old['intent_hash']!=intent:
                raise OpsPlanError('ops_request_conflict')
            return Proposal(self.retire(cursor,dict(old)))  # A code is issued exactly once.
        pending = cursor.execute("SELECT * FROM ops_actions WHERE endpoint_key=? AND state IN ('sending','pending')",
            (source['endpoint_key'],)).fetchone()
        if pending is not None and self.retire(cursor,dict(pending))['state'] not in TERMINAL:
            raise OpsPlanError('ops_question_already_pending')
        now,action_id,code = int(self.clock()),secrets.token_hex(16),f'{secrets.randbelow(1000000):06d}'
        fields = {key:source[key] for key in ('account_key','endpoint_key','actor_key','scope_key',
            'endpoint_generation','route_epoch','conversation_generation')}
        fields.update(action_id=action_id,job_id=job.job_id,request_id=request_id,account_epoch=self.token.epoch,
            job_lease_owner=job.lease_owner,job_lease_generation=job.lease_generation,
            owner_grant_id=grant.grant_id,owner_revision=grant.revision,plan_name=plan.name,parameters_json=encoded,
            binding_hash=binding,intent_hash=intent,state='sending',code_hash=self.code_hash(action_id,code),
            created_at=now,updated_at=now,expires_at=min(now+600,source['deadline_at']))
        row = cursor.execute('INSERT INTO ops_actions ('+','.join(fields)+') VALUES ('+','.join('?' for _ in fields)+') RETURNING *',tuple(fields.values())).fetchone()
        return Proposal(dict(row),code)
