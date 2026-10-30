"""Immediate, at-most-once proactive classification after real M2 ingestion.

The durable row is a source/decision marker, never a dispatch queue. Interrupted
classification is not recovered. Ordinary ingest retries cannot rescore it.
"""
from dataclasses import dataclass
import asyncio
import json
import math
import weakref

from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
from src.plugins.ai_chat.adapters.qq_official.migration_types import MigrationDenied
from src.plugins.ai_chat.agent.control import JobFence,active_job_fence,LeaseLost
from src.plugins.ai_chat.message_ir import MessageBody,TextNode
from src.plugins.ai_chat.platform_types import EndpointKey,ActorKey
from src.plugins.ai_chat.proactive import is_candidate_message
from src.plugins.ai_chat.adapters.qq_official.proactive_delivery import ProactiveLifetime,score_and_deliver


@dataclass(frozen=True,repr=False,eq=False)
class ProactiveCandidate:
    inbox_id: str
    job: object


class ProactiveIngress:
    def __init__(self,*,token,transaction,admission,outbox,scorer,clock,database_calls,
                 threshold=95,percent=0,max_checks_per_hour=0,random_value=lambda:1):
        self.token,self.tx,self.admission,self.outbox=token,transaction,admission,outbox
        self.scorer,self.clock,self.calls=scorer,clock,database_calls
        self.threshold,self.percent,self.max_checks,self.random=threshold,percent,max_checks_per_hour,random_value
        self._issued=weakref.WeakKeyDictionary()

    def _now(self):
        value=self.clock()
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:raise MigrationDenied('invalid_proactive_clock')
        return int(value)

    def _load(self,c,inbox_id,*,job=None):
        assert_epoch(c,self.token)
        inbox=c.execute('SELECT * FROM qq_official_inbound_events WHERE inbox_id=? AND account_key=?',(inbox_id,self.token.account_key)).fetchone()
        if inbox is None:raise MigrationDenied('proactive_source_unavailable')
        ep=EndpointKey.parse(inbox['endpoint_key'])
        message=c.execute('SELECT m.*,co.scope_key FROM messages m JOIN conversations co USING(conversation_id) JOIN conversation_visibility v USING(conversation_id) WHERE canonical_message_id=? AND m.canonical_message_id>=v.min_canonical_message_id',
            (inbox['canonical_message_id'],)).fetchone()
        if message is None:raise MigrationDenied('proactive_source_unavailable')
        actor=ActorKey.parse(message['sender_actor_key'])
        if getattr(self.admission,'require_inbox',None) is not None:
            self.admission.require_inbox(c,inbox_id,endpoint=ep,actor=actor,operation='execute' if job else 'send')
        elif c.execute('SELECT 1 FROM migration_routes LIMIT 1').fetchone():raise MigrationDenied('logical_source_provider_required')
        physical=c.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=? FOR UPDATE',(ep.key,)).fetchone()
        if (ep.kind!='group' or not actor.matches_endpoint(ep) or actor.identity_kind=='bot' or inbox['status']!='processed'
                or inbox['echo_state']!='not_echo' or inbox['trigger_requested'] or inbox['dispatch_job_id'] is not None
                or inbox['turn_text_version']!=1 or not is_candidate_message(inbox['turn_text'])
                or message['source_endpoint_key']!=ep.key or message['native_message_id']!=inbox['native_message_id']
                or message['direction']!='inbound' or message['reply_to_native_message_id']
                or physical['membership_state']!='present' or physical['binding_state']!='active'
                or physical['generation']!=inbox['endpoint_generation'] or physical['route_epoch']!=inbox['route_epoch']
                or self.admission(c,ep,actor) is not True
                or not c.execute('SELECT 1 FROM qq_official_gateway_receipts WHERE inbox_id=? AND account_key=?',(inbox_id,self.token.account_key)).fetchone()):
            raise MigrationDenied('proactive_source_unavailable')
        return inbox,ep,actor,message,physical

    def begin(self,job):
        if not 0<self.percent<=100 or not 0<self.max_checks<=120 or self.random()>=self.percent/100:return None
        with self.tx() as c:
            inbox,ep,actor,_,_=self._load(c,job.payload.get('inbox_id'),job=job)
            now=self._now()
            if not inbox['received_at']<=now<inbox['received_at']+30:return None
            count=c.execute('SELECT count(*) FROM qq_official_proactive_observations WHERE endpoint_key=? AND created_at>?',
                (ep.key,now-3600)).fetchone()[0]
            if count>=self.max_checks:return None
            inserted=c.execute('''INSERT INTO qq_official_proactive_observations
                (inbox_id,account_key,endpoint_key,actor_key,canonical_message_id,source_native_message_id,source_received_at,
                 deadline_at,ingest_job_id,worker_owner,worker_generation,account_epoch,endpoint_generation,endpoint_route_epoch,state,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'scoring',?) ON CONFLICT DO NOTHING''',
                (inbox['inbox_id'],ep.account.key,ep.key,actor.key,inbox['canonical_message_id'],inbox['native_message_id'],inbox['received_at'],
                 inbox['received_at']+30,job.job_id,job.lease_owner,job.lease_generation,self.token.epoch,inbox['endpoint_generation'],inbox['route_epoch'],now))
            if inserted.rowcount!=1:return None
            candidate=ProactiveCandidate(inbox['inbox_id'],job);self._issued[candidate]=True
            return candidate

    def _candidate(self,c,source):
        if type(source) is not ProactiveCandidate or source not in self._issued:raise MigrationDenied('unissued_proactive_source')
        loaded=self._load(c,source.inbox_id,job=source.job)
        row=c.execute('SELECT * FROM qq_official_proactive_observations WHERE inbox_id=?',(source.inbox_id,)).fetchone()
        if (row is None or row['state']!='scoring' or row['worker_owner']!=source.job.lease_owner
                or row['worker_generation']!=source.job.lease_generation or row['account_epoch']!=self.token.epoch):
            raise MigrationDenied('proactive_source_lost')
        return row,loaded

    def _current(self,source):
        with self.tx() as c:
            row,_=self._candidate(c,source)
            return ProactiveLifetime(row['source_received_at'],row['deadline_at'])

    async def current(self,source):return await self.calls(self._current,source)

    def _text(self,source):
        with self.tx() as c:
            _,(inbox,ep,actor,message,_)=self._candidate(c,source)
            # Current actual-endpoint visible chat only; a canonical history alias
            # does not implicitly include other physical conversations here.
            recent=c.execute('''SELECT m.body_json FROM messages m JOIN conversation_visibility v USING(conversation_id)
                WHERE m.source_endpoint_key=? AND m.canonical_message_id>=v.min_canonical_message_id
                AND m.canonical_message_id<=? ORDER BY m.canonical_message_id DESC LIMIT 20''',
                (ep.key,message['canonical_message_id'])).fetchall()
            return inbox['turn_text'],tuple(r['body_json'] for r in reversed(recent)),ep,actor

    async def score(self,source):
        text,history,endpoint,actor=await self.calls(self._text,source)
        return await self.scorer(text=text,history=history,endpoint=endpoint,actor=actor)

    def _send_policy(self,source,*,purpose,deadline):
        with self.tx() as c:
            row,(inbox,ep,_,_,physical)=self._candidate(c,source)
            if deadline!=row['deadline_at'] or purpose!='unsolicited':raise MigrationDenied('proactive_deadline_changed')
            return physical['membership_state']=='present' and physical['binding_state']=='active'

    async def send_policy(self,*args,**kwargs):return await self.calls(self._send_policy,*args,**kwargs)

    def _enqueue(self,source,text,*,purpose,expires_at):
        with self.tx() as c:
            row,(inbox,ep,actor,message,_)=self._candidate(c,source)
            if not self._now()<expires_at<=row['deadline_at']:raise MigrationDenied('proactive_source_expired')
            plan=self.outbox.enqueue(self.token,rendered=render_plan(MessageBody((TextNode(0,text),)),ep),requester=actor,
                logical_key='proactive:'+source.inbox_id,source_scope_key=message['scope_key'],source_native_message_id=inbox['native_message_id'],
                source_canonical_message_id=message['canonical_message_id'],expires_at=expires_at,purpose=purpose,
                source_proof=source,transaction_cursor=c)
            c.execute("UPDATE qq_official_proactive_observations SET state='queued',plan_id=? WHERE inbox_id=?",(plan.plan_id,source.inbox_id))
            return plan.plan_id

    async def enqueue(self,*args,**kwargs):return await self.calls(self._enqueue,*args,**kwargs)

    def _discard(self,source):
        with self.tx() as c:
            assert_epoch(c,self.token)
            c.execute("UPDATE qq_official_proactive_observations SET state='discarded' WHERE inbox_id=? AND account_epoch=? AND state='scoring'",
                (source.inbox_id,self.token.epoch))

    async def on_ingested(self,job):
        # Called only after process_inbox's transaction commits. Failure of an
        # optional classifier cannot turn a durable ingest into repeated scoring.
        try:source=await self.calls(self.begin,job)
        except MigrationDenied:return {'status':'discarded','reason':'ineligible_ingress'}
        if source is None:return {'status':'discarded','reason':'not_selected'}
        def check():
            try:return self._now()<self._current(source).deadline_at
            except Exception:return False
        marker=active_job_fence.set(JobFence(job.job_id,job.lease_owner,job.attempts,check,job.lease_generation))
        try:
            lifetime=await self.current(source)
            async with asyncio.timeout(max(0,lifetime.deadline_at-self._now())):
                result=await score_and_deliver(source=source,scorer=self.score,current=self.current,send_policy=self.send_policy,
                    enqueue=self.enqueue,now=self._now,threshold=self.threshold)
            if result.status=='discarded':await self.calls(self._discard,source)
            return {'status':result.status,'reason':result.reason}
        except (Exception,LeaseLost):
            try:await self.calls(self._discard,source)
            except Exception:pass
            return {'status':'discarded','reason':'proactive_unavailable'}
        finally:active_job_fence.reset(marker)

    def plan_guard(self,c,*,token,plan,operation,now,source_proof=None,**_):
        if token is not self.token:raise MigrationDenied('proactive_account_changed')
        if operation=='enqueue':
            row,(inbox,ep,actor,message,_)=self._candidate(c,source_proof)
        else:
            row=c.execute('SELECT * FROM qq_official_proactive_observations WHERE plan_id=?',(plan['plan_id'],)).fetchone()
            if row is None or row['state']!='queued':raise MigrationDenied('proactive_plan_source_missing')
            inbox,ep,actor,message,_=self._load(c,row['inbox_id'])
        if (plan['purpose']!='unsolicited' or plan['account_key']!=ep.account.key or plan['endpoint_key']!=ep.key
                or plan['requester_actor_key']!=actor.key or plan['source_canonical_message_id']!=message['canonical_message_id']
                or plan['source_scope_key']!=message['scope_key'] or plan['endpoint_generation']!=inbox['endpoint_generation']
                or plan['route_epoch']!=inbox['route_epoch'] or not self._now()<plan['expires_at']<=row['deadline_at']
                or json.loads(plan['manifest_json']).get('source_native_message_id')!=row['source_native_message_id']):
            raise MigrationDenied('proactive_plan_source_changed')
        return True
