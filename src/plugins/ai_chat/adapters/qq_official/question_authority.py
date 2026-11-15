"""Host-issued question origins; target receives no source actor/owner capability."""
import hashlib
import hmac
import re

from ... import daily_budgets
from ...platform_types import ActorKey,EndpointKey
from .conversation_control import permitted,state
from .lease import assert_epoch
from .link_ingress import LinkIngress
from .turn_platform import ExecutionBinding


class QuestionAuthority:
    def __init__(self,ingress,*,admission):
        if not isinstance(ingress,LinkIngress) or not callable(admission):raise ValueError('question_host_authority_required')
        self.ingress,self.admission = ingress,admission; self.authority = ingress.authority

    def now(self):return int(self.authority.clock())

    def _request(self,row):
        return self.authority._hash('qq-question-request:v1',[row[key] for key in ('account_key','account_epoch',
            'source_execution_id','source_execution_revision','source_endpoint_key','source_actor_key','link_id','link_generation','question')])

    def _origin(self,cursor,token,row):
        assert_epoch(cursor,token)
        endpoint,actor = EndpointKey.parse(row['source_endpoint_key']),ActorKey.parse(row['source_actor_key'])
        if (row['account_key']!=token.account_key or row['account_epoch']!=token.epoch or not actor.matches_endpoint(endpoint)
            or actor.identity_kind not in {'group_member','c2c_user'} or not self.admission(cursor,endpoint,actor)):
            raise PermissionError('question_source_unavailable')
        route = cursor.execute('SELECT * FROM qq_official_endpoints WHERE account_key=? AND endpoint_key=?',
            (token.account_key,endpoint.key)).fetchone()
        execution = cursor.execute('''SELECT * FROM qq_official_turn_executions WHERE execution_id=? AND account_key=?
            AND endpoint_key=? AND requester_actor_key=?''',(row['source_execution_id'],token.account_key,endpoint.key,actor.key)).fetchone()
        if (not route or not execution or execution['state'] not in {'executing','result_ready','handed_off'}
            or execution['execution_kind']!='chat' or route['membership_state']!='present' or route['binding_state']=='revoked'
            or (route['generation'],route['route_epoch'],route['canonical_scope_key'])!=(row['source_generation'],row['source_route_epoch'],row['source_scope_key'])
            or (execution['account_fencing_epoch'],execution['execution_revision'],execution['endpoint_generation'],execution['route_epoch'],execution['scope_key'],execution['deadline_at'])
                !=(row['account_epoch'],row['source_execution_revision'],row['source_generation'],row['source_route_epoch'],row['source_scope_key'],row['source_deadline_at'])
            or not permitted(cursor,endpoint,generation=row['source_stop_generation'])):
            raise PermissionError('question_source_unavailable')
        return execution

    def _edge(self,cursor,token,row):
        edge = cursor.execute('''SELECT * FROM qq_official_conversation_links WHERE account_key=? AND link_id=?
            AND source_endpoint_key=?''',(token.account_key,row['link_id'],row['source_endpoint_key'])).fetchone()
        names = ('source_scope_key','target_scope_key','source_generation','source_route_epoch','target_generation','target_route_epoch','target_endpoint_key')
        if (not edge or row.get('link_generation',edge['generation'])!=edge['generation']
            or any(row[key]!=edge[key] for key in names if key in row)
            or not self.ingress.management.current(cursor,token,edge)):
            raise PermissionError('question_link_unavailable')
        return edge

    def _target(self,cursor,token,row):
        self._edge(cursor,token,row)
        target = EndpointKey.parse(row['target_endpoint_key'])
        if not permitted(cursor,target,generation=row['target_stop_generation']):raise PermissionError('question_target_stopped')
        daily_budgets.require(cursor,target,now=self.now(),generation=row['target_budget_generation'])
        return target

    def capture(self,cursor,token,binding,reference,question):
        if (not isinstance(binding,ExecutionBinding) or binding.token!=token or type(reference) is not str
            or re.fullmatch('[0-9a-f]{32}',reference) is None or type(question) is not str or not 0<len(question.strip())<=8000):
            raise ValueError('question_request_invalid')
        execution = binding.current(cursor,self.now()); handle = binding.handle
        if (execution['state']!='executing' or execution['execution_kind']!='chat'
            or (execution['execution_id'],execution['account_key'],execution['endpoint_key'],execution['requester_actor_key'],
                execution['account_fencing_epoch'],execution['scope_key'],execution['endpoint_generation'],execution['route_epoch'],
                execution['execution_revision'],execution['deadline_at'],execution['canonical_message_id'],execution['source_native_message_id'])
                !=(handle.execution_id,token.account_key,handle.endpoint.key,handle.actor.key,token.epoch,handle.scope_key,
                    handle.endpoint_generation,handle.route_epoch,handle.execution_revision,handle.deadline_at,handle.canonical_message_id,handle.source_native_message_id)):
            raise PermissionError('question_source_unavailable')
        row = dict(account_key=token.account_key,account_epoch=token.epoch,source_execution_id=handle.execution_id,
            source_actor_key=handle.actor.key,source_execution_revision=handle.execution_revision,source_endpoint_key=handle.endpoint.key,
            source_scope_key=handle.scope_key,source_generation=handle.endpoint_generation,source_route_epoch=handle.route_epoch,
            source_stop_generation=state(cursor,handle.endpoint)['stop_generation'],source_deadline_at=handle.deadline_at,
            link_id=reference,question=question.strip())
        self._origin(cursor,token,row); edge = self._edge(cursor,token,row)
        for key in ('target_endpoint_key','target_scope_key','target_generation','target_route_epoch'):row[key] = edge[key]
        row['link_generation'] = edge['generation']; target = EndpointKey.parse(row['target_endpoint_key'])
        control = state(cursor,target)
        if control['stopped']:raise PermissionError('question_target_stopped')
        row['target_stop_generation'] = control['stop_generation']
        row['target_budget_generation'] = daily_budgets.require(cursor,target,now=self.now())
        # Permission reads can cross deadlines. Validate all original fences again,
        # then use the final checked time once; never prolong the source task.
        final = binding.current(cursor,self.now())
        if final['state']!='executing':raise PermissionError('question_source_unavailable')
        self._origin(cursor,token,row); self._target(cursor,token,row)
        now = self.now()
        if now>=handle.deadline_at:raise PermissionError('question_source_expired')
        row.update(created_at=now,deadline_at=min(now+7200,handle.deadline_at))
        row['request_hmac'] = self._request(row)
        return row

    def current(self,cursor,token,row):
        assert_epoch(cursor,token)
        stored = cursor.execute('SELECT * FROM qq_official_conversation_questions WHERE account_key=? AND question_id=?',
            (token.account_key,row['question_id'])).fetchone()
        # Intent HMAC stays stable for deduplication. The immutable store, not a
        # caller's repaired snapshot, supplies stop/budget/time and result facts.
        if stored is None or dict(stored)!=dict(row):raise PermissionError('question_source_unavailable')
        if row['state'] not in {'queued','running','answered','refused'} or row['consumed_at'] is not None:raise PermissionError('question_closed')
        if not hmac.compare_digest(row['request_hmac'],self._request(row)):raise PermissionError('question_source_unavailable')
        self._origin(cursor,token,row); target = self._target(cursor,token,row)
        self._origin(cursor,token,row); self._target(cursor,token,row)
        now = self.now()
        if not row['created_at']<=now<row['deadline_at']<=min(row['created_at']+7200,row['source_deadline_at']):
            raise PermissionError('question_source_expired')
        return target

    def target_input(self,cursor,token,row):
        """Verified data only, never a privileged source InboundMessage."""
        self.current(cursor,token,row)
        source = EndpointKey.parse(row['source_endpoint_key'])
        label = ('群会话 ' if source.kind=='group' else '私聊会话 ')+hashlib.sha256(source.key.encode()).hexdigest()[:12]
        return {'question':row['question'],'source_label':label}
