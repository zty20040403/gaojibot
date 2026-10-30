"""Native official scheduled work using original domains, jobs, quota and M3.

No fabricated Event, migration proof, model loop,
remote API or implicit schedule policy. All cursor methods join one transaction.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import math
import secrets
import weakref

from src.plugins.ai_chat.adapters.qq_official.turn_source import assert_execution_current
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.adapters.qq_official.identity_store import snapshot
from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
from src.plugins.ai_chat.adapters.qq_official.scheduled_model import ScheduledWork
from src.plugins.ai_chat.agent.control import JobFence, active_job_fence, LeaseLost
from src.plugins.ai_chat.message_ir import MessageBody, TextNode, body_to_json, body_from_json
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey, InboundMessage
from src.plugins.ai_chat.routines import cron_time


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def integer(value, minimum=0):
    if type(value) is not int or not minimum <= value < 2**63-1:
        raise NativeScheduleDenied('invalid_contract')
    return value


def text(value, maximum):
    if not isinstance(value, str) or not value.strip() or '\x00' in value or len(value.encode()) > maximum:
        raise NativeScheduleDenied('invalid_contract')
    return value


class NativeScheduleDenied(PermissionError):
    pass


@dataclass(frozen=True, repr=False)
class NativeCommandSource:
    handle: object
    job: object
    inbound: InboundMessage


@dataclass(frozen=True)
class SchedulePolicy:
    policy_id: str = field(repr=False)
    revision: int
    authorization_until: int
    execution_seconds: int
    delivery_seconds: int


@dataclass(frozen=True)
class NativeCommandResult:
    status: str
    identifier: int | None = None
    revision: int | None = None
    next_due_at: int | None = None
    reason: str = ''
    authorization_until: int | None = None
    execution_seconds: int | None = None
    delivery_seconds: int | None = None


@dataclass(frozen=True, eq=False, repr=False)
class OccurrenceProof:
    occurrence_id: str
    job: object


class NativeSchedules:
    """Policy.approve/current are mandatory trusted-host ports, not tool input.

    approve(cursor, source, spec, now)->SchedulePolicy|None.
    current(cursor, source_row, immutable_spec, now)->strict bool.
    A policy update does not silently replace the persisted policy revision.
    """
    def __init__(self, *, database, token, jobs, usage, outbox, admission,
                 policy=None, clock, run_model=None, database_calls=asyncio.to_thread):
        self.database, self.token, self.jobs, self.usage = database, token, jobs, usage
        self.outbox, self.admission, self.policy = outbox, admission, policy
        self.clock, self.run_model = clock, run_model
        self.calls = database_calls
        self.kinds = tuple(account_job_kind(k, token.account_key) for k in ('reminder','routine'))
        self._issued = weakref.WeakKeyDictionary()
        self._model_attempts = {}
        self._projection_cursor = ""

    def now(self):
        value = self.clock()
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
            raise NativeScheduleDenied('invalid_clock')
        return integer(int(value))

    @contextmanager
    def tx(self):
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            yield cursor
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def command(self, cursor, source):
        if type(source) is not NativeCommandSource or not isinstance(source.inbound, InboundMessage):
            raise NativeScheduleDenied('native_source_required')
        h, inbound = source.handle, source.inbound
        row = assert_execution_current(cursor, token=self.token, handle=h, job=source.job,
            now=self.now(), admission=self.admission)
        if (row['state'] != 'executing' or self.now() >= row['deadline_at']
                or inbound.endpoint != h.endpoint or inbound.actor != h.actor
                or inbound.native_message_id != h.source_native_message_id or inbound.scope.key != h.scope_key):
            raise NativeScheduleDenied('native_source_mismatch')
        return row

    def _route(self, cursor, execution_id, endpoint, actor, operation):
        check = getattr(self.admission, 'require_execution', None)
        if check is not None:
            found = check(cursor, execution_id, endpoint=endpoint, actor=actor, operation=operation)
            return found['logical_route_epoch']
        return 0

    def _approve(self, cursor, source, spec):
        if self.policy is None:
            raise NativeScheduleDenied('schedule_policy_unavailable')
        decision = self.policy.approve(cursor, source, spec, self.now())
        if type(decision) is not SchedulePolicy:
            raise NativeScheduleDenied('schedule_policy_unavailable')
        text(decision.policy_id, 128)
        integer(decision.revision, 1)
        integer(decision.execution_seconds, 1)
        integer(decision.delivery_seconds, decision.execution_seconds)
        integer(decision.authorization_until, self.now()+1)
        if decision.delivery_seconds > self.outbox.max_wait_seconds:
            raise NativeScheduleDenied('delivery_contract_unsupported')
        return decision

    def _source(self, cursor, source, request_key, spec, policy, *, due_at, reminder_id=None, routine_id=None, revision=1):
        if len(request_key) != 64 or any(x not in '0123456789abcdef' for x in request_key):
            raise NativeScheduleDenied('invalid_request_key')
        h, now = source.handle, self.now()
        if not now<due_at<policy.authorization_until:raise NativeScheduleDenied('schedule_outside_authorization')
        epoch = self._route(cursor, h.execution_id, h.endpoint, h.actor, 'execute')
        encoded = canonical(spec)
        source_id = secrets.token_hex(16)
        cursor.execute('''INSERT INTO qq_official_native_schedule_sources
            (source_id,execution_id,request_key,account_key,endpoint_key,actor_key,scope_key,inbox_id,
             endpoint_generation,endpoint_route_epoch,logical_route_epoch,kind,reminder_id,routine_id,
             revision,first_due_at,spec_json,spec_digest,policy_id,policy_revision,authorization_until,
             execution_seconds,delivery_seconds,state,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?)''',
            (source_id,h.execution_id,request_key,self.token.account_key,h.endpoint.key,h.actor.key,h.endpoint.scope.key,h.inbox_id,
             h.endpoint_generation,h.route_epoch,epoch,spec['kind'],reminder_id,routine_id,revision,due_at,encoded,digest(encoded),
             policy.policy_id,policy.revision,policy.authorization_until,policy.execution_seconds,policy.delivery_seconds,now))
        return dict(cursor.execute('SELECT * FROM qq_official_native_schedule_sources WHERE source_id=?',(source_id,)).fetchone())

    def _prior(self,cursor,source,key,spec):
        prior=cursor.execute('SELECT * FROM qq_official_native_schedule_sources WHERE execution_id=? AND request_key=?',
            (source.handle.execution_id,key)).fetchone()
        if prior is None:
            return None
        if prior['spec_json'] != canonical(spec):
            raise NativeScheduleDenied('idempotency_conflict')
        return NativeCommandResult('created' if prior['revision']==1 else 'updated',
            prior['reminder_id'] or prior['routine_id'], prior['revision'], prior['first_due_at'],
            authorization_until=prior['authorization_until'],execution_seconds=prior['execution_seconds'],delivery_seconds=prior['delivery_seconds'])

    def create_reminder(self, source, *, request_key, text: str, scheduled_for: int):
        spec=dict(kind='reminder',text=globals()['text'](text,4000),scheduled_for=integer(scheduled_for))
        with self.tx() as c:
            self.command(c,source)
            if prior:=self._prior(c,source,request_key,spec):
                return prior
            policy=self._approve(c,source,spec)
            if scheduled_for>=policy.authorization_until:
                raise NativeScheduleDenied('schedule_outside_authorization')
            h,ep=source.handle,source.handle.endpoint
            count=c.execute("SELECT count(*) FROM reminders WHERE endpoint_key=? AND creator_actor_key=? AND status='pending'",(ep.key,h.actor.key)).fetchone()[0]
            if count>=50:raise NativeScheduleDenied('schedule_limit')
            rid=c.execute('''INSERT INTO reminders(scope_key,platform,conversation_kind,native_conversation_id,
                creator_native_user_id,message,scheduled_for,next_attempt_at,status,created_at,account_key,endpoint_key,
                creator_actor_key,endpoint_generation,endpoint_route_epoch)
                VALUES (?,'qq-official',?,?,?,?,?,?,'pending',?,?,?,?,?,?) RETURNING reminder_id''',
                (ep.scope.key,ep.kind,ep.native_conversation_id,h.actor.native_user_id,text,scheduled_for,scheduled_for,self.now(),
                 ep.account.key,ep.key,h.actor.key,h.endpoint_generation,h.route_epoch)).fetchone()[0]
            row=self._source(c,source,request_key,spec,policy,due_at=scheduled_for,reminder_id=rid)
            self._occurrence(c,row,scheduled_for)
            return NativeCommandResult('created',rid,1,scheduled_for,authorization_until=policy.authorization_until,
                execution_seconds=policy.execution_seconds,delivery_seconds=policy.delivery_seconds)

    def set_routine(self, source, *, request_key, name, prompt, cron, timezone,
                    max_tokens=4096,daily_tokens=16384,daily_runs=4,routine_id=None,expected_version=None,authorization_until=None):
        spec=dict(kind='routine',name=text(name,64),prompt=text(prompt,16000),cron=text(cron,120),timezone=text(timezone,128),
            max_tokens=integer(max_tokens,1024),daily_tokens=integer(daily_tokens,max_tokens),daily_runs=integer(daily_runs,1),
            routine_id=routine_id,expected_version=expected_version,authorization_until=authorization_until)
        if max_tokens>200000 or daily_tokens>2000000 or daily_runs>24:raise NativeScheduleDenied('invalid_contract')
        due=cron_time(cron,timezone,self.now())
        with self.tx() as c:
            self.command(c,source)
            if prior:=self._prior(c,source,request_key,spec):return prior
            policy=self._approve(c,source,spec)
            if due>=policy.authorization_until:raise NativeScheduleDenied('schedule_outside_authorization')
            h,ep=source.handle,source.handle.endpoint
            old=None
            if routine_id is not None:
                integer(routine_id,1)
                old=c.execute('SELECT * FROM agent_routines WHERE routine_id=? AND endpoint_key=? AND creator_actor_key=?',
                    (routine_id,ep.key,h.actor.key)).fetchone()
                if old is None:return NativeCommandResult('not_found')
                if old['version']!=expected_version:return NativeCommandResult('conflict')
                self._cancel(c,'routine',routine_id)
            elif c.execute('SELECT 1 FROM agent_routines WHERE endpoint_key=? AND creator_actor_key=? AND name=?',
                    (ep.key,h.actor.key,name)).fetchone():
                return NativeCommandResult('conflict',reason='explicit_routine_id_required')
            if old is None:
                count=c.execute("SELECT count(*) FROM agent_routines WHERE endpoint_key=? AND enabled",(ep.key,)).fetchone()[0]
                if count>=(10 if ep.kind=='group' else 5):raise NativeScheduleDenied('schedule_limit')
                author=snapshot(c,ep.account)
                if author is None:raise NativeScheduleDenied('verified_bot_identity_required')
                routine_id=c.execute('''INSERT INTO agent_routines
                    (scope_key,platform,kind,conversation_id,bot_id,creator_id,name,prompt,cron,timezone,enabled,version,
                     max_tokens,daily_tokens,daily_runs,next_due_at,created_at,updated_at,account_key,endpoint_key,
                     creator_actor_key,endpoint_generation,endpoint_route_epoch)
                    VALUES (?,'qq-official',?,?,?,?,?,?,?, ?,TRUE,1,?,?,?,?,?,?,?,?,?,?,?) RETURNING routine_id''',
                    (ep.scope.key,ep.kind,ep.native_conversation_id,author.bot_actor.native_user_id,h.actor.native_user_id,name,prompt,cron,timezone,
                     max_tokens,daily_tokens,daily_runs,due,self.now(),self.now(),ep.account.key,ep.key,h.actor.key,h.endpoint_generation,h.route_epoch)).fetchone()[0]
                revision=1
            else:
                revision=old['version']+1
                c.execute('''UPDATE agent_routines SET name=?,prompt=?,cron=?,timezone=?,enabled=TRUE,state='active',version=?,
                    max_tokens=?,daily_tokens=?,daily_runs=?,next_due_at=?,updated_at=? WHERE routine_id=?''',
                    (name,prompt,cron,timezone,revision,max_tokens,daily_tokens,daily_runs,due,self.now(),routine_id))
            self._source(c,source,request_key,spec,policy,due_at=due,routine_id=routine_id,revision=revision)
            return NativeCommandResult('created' if old is None else 'updated',routine_id,revision,due,authorization_until=policy.authorization_until,
                execution_seconds=policy.execution_seconds,delivery_seconds=policy.delivery_seconds)

    def list_owned(self, source):
        with self.tx() as c:
            self.command(c,source)
            return tuple(dict(r) for r in c.execute('''SELECT a.routine_id,a.version,a.name,a.prompt,a.cron,a.timezone,a.max_tokens,
                a.daily_tokens,a.daily_runs,a.enabled,a.next_due_at,s.authorization_until,s.execution_seconds,s.delivery_seconds
                FROM agent_routines a LEFT JOIN LATERAL (
                  SELECT authorization_until,execution_seconds,delivery_seconds FROM qq_official_native_schedule_sources
                  WHERE routine_id=a.routine_id ORDER BY revision DESC LIMIT 1) s ON TRUE
                WHERE a.endpoint_key=? AND a.creator_actor_key=?
                ORDER BY a.routine_id LIMIT 100''',(source.handle.endpoint.key,source.handle.actor.key)).fetchall())

    def list_reminders(self,source):
        with self.tx() as c:
            self.command(c,source)
            return tuple(dict(row) for row in c.execute("SELECT reminder_id,message,scheduled_for,status FROM reminders WHERE endpoint_key=? AND creator_actor_key=? AND status IN ('pending','sending') ORDER BY scheduled_for,reminder_id LIMIT 100",
                (source.handle.endpoint.key,source.handle.actor.key)).fetchall())

    def _lock_source_family(self,c,kind,identifier):
        # Every revision is locked in the same order before any parent/run/job
        # row. A caller that later cancels the family must never acquire older
        # revision sources after already holding its current source/parent.
        column='routine_id' if kind=='routine' else 'reminder_id'
        return c.execute(f'SELECT source_id FROM qq_official_native_schedule_sources WHERE {column}=? ORDER BY source_id FOR UPDATE',(identifier,)).fetchall()

    def _cancel(self,c,kind,identifier):
        column='routine_id' if kind=='routine' else 'reminder_id'
        # Global order: all revision sources -> parent -> run -> job -> occurrence.
        self._lock_source_family(c,kind,identifier)
        table='agent_routines' if kind=='routine' else 'reminders'
        c.execute(f'SELECT {column} FROM {table} WHERE {column}=? FOR UPDATE',(identifier,)).fetchone()
        if kind=='routine':c.execute('SELECT run_id FROM routine_runs WHERE routine_id=? ORDER BY run_id FOR UPDATE',(identifier,)).fetchall()
        c.execute(f'''SELECT j.job_id FROM durable_jobs j JOIN qq_official_native_schedule_occurrences o ON o.job_id=j.job_id
            JOIN qq_official_native_schedule_sources s USING(source_id) WHERE s.{column}=? ORDER BY j.job_id FOR UPDATE OF j''',(identifier,)).fetchall()
        c.execute(f'''SELECT o.occurrence_id FROM qq_official_native_schedule_occurrences o JOIN qq_official_native_schedule_sources s USING(source_id)
            WHERE s.{column}=? ORDER BY o.occurrence_id FOR UPDATE OF o''',(identifier,)).fetchall()
        c.execute(f"UPDATE qq_official_native_schedule_sources SET state='cancelled' WHERE {column}=?",(identifier,))
        c.execute(f'''UPDATE durable_jobs j SET status='cancelled',updated_at=?,finished_at=? FROM
            qq_official_native_schedule_occurrences o JOIN qq_official_native_schedule_sources s USING(source_id)
            WHERE s.{column}=? AND j.job_id=o.job_id AND j.status='pending' ''',(self.now(),self.now(),identifier))
        c.execute(f'''UPDATE qq_official_native_schedule_occurrences o SET state='cancelled' FROM
            qq_official_native_schedule_sources s WHERE o.source_id=s.source_id AND s.{column}=?
            AND o.state IN ('ready','result_ready')''',(identifier,))
        if kind=='routine':
            c.execute("UPDATE agent_routines SET enabled=FALSE,state='paused' WHERE routine_id=?",(identifier,))
            c.execute("UPDATE routine_runs SET status='cancelled' WHERE routine_id=? AND status IN ('queued','generated')",(identifier,))
        else:c.execute("UPDATE reminders SET status='cancelled' WHERE reminder_id=? AND status='pending'",(identifier,))

    def cancel(self,source,*,kind,identifier,expected_version=None):
        if kind not in {'reminder','routine'}:raise NativeScheduleDenied('invalid_kind')
        integer(identifier,1)
        with self.tx() as c:
            self.command(c,source)
            table,column=('agent_routines','routine_id') if kind=='routine' else ('reminders','reminder_id')
            old=c.execute(f'SELECT * FROM {table} WHERE {column}=? AND endpoint_key=? AND creator_actor_key=?',
                (identifier,source.handle.endpoint.key,source.handle.actor.key)).fetchone()
            if old is None:return NativeCommandResult('not_found')
            if kind=='routine' and expected_version!=old['version']:return NativeCommandResult('conflict')
            self._cancel(c,kind,identifier)
            if kind=='routine':c.execute('UPDATE agent_routines SET version=version+1 WHERE routine_id=?',(identifier,))
            return NativeCommandResult('cancelled',identifier)

    def current(self,c,row,operation):
        now=self.now()
        assert_epoch(c,self.token)
        ep,actor=EndpointKey.parse(row['endpoint_key']),ActorKey.parse(row['actor_key'])
        if row['account_key']!=self.token.account_key or not actor.matches_endpoint(ep):raise NativeScheduleDenied('namespace_changed')
        epoch=self._route(c,row['execution_id'],ep,actor,'send' if operation=='send' else 'execute')
        physical=c.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=? FOR UPDATE',(ep.key,)).fetchone()
        self._lock_source_family(c,row['kind'],row['routine_id'] or row['reminder_id'])
        row=c.execute('SELECT * FROM qq_official_native_schedule_sources WHERE source_id=? FOR UPDATE',(row['source_id'],)).fetchone()
        source=c.execute('SELECT * FROM qq_official_turn_executions WHERE execution_id=?',(row['execution_id'],)).fetchone()
        if (source is None or source['inbox_id']!=row['inbox_id'] or source['endpoint_key']!=ep.key or source['requester_actor_key']!=actor.key
                or source['state'] in {'needs_attention','cancelled'} or source['result_status'] not in {None,'succeeded','silence'}
                or row['state']!='active' or now>=row['authorization_until'] or epoch!=row['logical_route_epoch']
                or physical is None or physical['generation']!=row['endpoint_generation'] or physical['route_epoch']!=row['endpoint_route_epoch']
                or physical['membership_state']!='present' or physical['binding_state']=='revoked'
                or ep.kind=='group' and physical['binding_state']!='active' or self.admission(c,ep,actor) is not True
                or digest(row['spec_json'])!=row['spec_digest']):raise NativeScheduleDenied('schedule_source_changed')
        if self.policy is None or self.policy.current(c,row,json.loads(row['spec_json']),now) is not True:
            raise NativeScheduleDenied('schedule_policy_changed')
        if row['routine_id']:
            domain=c.execute('SELECT * FROM agent_routines WHERE routine_id=? FOR UPDATE',(row['routine_id'],)).fetchone()
            spec=json.loads(row['spec_json'])
            if (domain is None or not domain['enabled'] or domain['version']!=row['revision']
                    or domain['endpoint_key']!=ep.key or domain['creator_actor_key']!=actor.key
                    or any(domain[k]!=spec[k] for k in ('name','prompt','cron','timezone','max_tokens','daily_tokens','daily_runs'))):
                raise NativeScheduleDenied('schedule_revision_changed')
        else:
            domain=c.execute('SELECT * FROM reminders WHERE reminder_id=? FOR UPDATE',(row['reminder_id'],)).fetchone()
            spec=json.loads(row['spec_json'])
            if (domain is None or domain['status']=='cancelled' or domain['message']!=spec['text']
                    or domain['scheduled_for']!=spec['scheduled_for'] or domain['endpoint_key']!=ep.key or domain['creator_actor_key']!=actor.key):
                raise NativeScheduleDenied('schedule_revision_changed')
        if self.now()>=row['authorization_until']:raise NativeScheduleDenied('schedule_authorization_expired')
        return ep,actor,domain

    def _occurrence(self,c,source,due):
        now=self.now()
        execution=min(due+source['execution_seconds'],source['authorization_until'])
        deadline=min(due+source['delivery_seconds'],source['authorization_until'])
        if not due<execution<=deadline:raise NativeScheduleDenied('occurrence_outside_authorization')
        run_id=None
        if source['routine_id']:
            run_id=secrets.token_hex(16)
            spec=c.execute('SELECT * FROM agent_routines WHERE routine_id=?',(source['routine_id'],)).fetchone()
            c.execute('''INSERT INTO routine_runs(run_id,routine_id,version,scheduled_at,missed_from,spec_json,status,
                created_at,updated_at,deadline_at) VALUES (?,?,?,?,?,?,'queued',?,?,?)''',
                (run_id,source['routine_id'],source['revision'],due,due,canonical(dict(spec)),now,now,execution))
        oid=secrets.token_hex(16)
        job,_=self.jobs.enqueue(kind=account_job_kind(source['kind'],self.token.account_key),idempotency_key='native-schedule:'+oid,payload={'occurrence_id':oid},
            scope_key='qqn:'+digest(source['endpoint_key']),run_at=due,now=now,max_attempts=3,resume_on_lease_loss=True,transaction_cursor=c)
        c.execute('''INSERT INTO qq_official_native_schedule_occurrences(occurrence_id,source_id,account_key,endpoint_key,
            scheduled_at,execution_deadline,result_deadline,run_id,job_id,state,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,'ready',?)''',(oid,source['source_id'],source['account_key'],source['endpoint_key'],due,
            execution,deadline,run_id,job.job_id,now))
        if run_id:c.execute('UPDATE routine_runs SET job_id=? WHERE run_id=?',(job.job_id,run_id))
        return oid

    def materialize_due(self,limit=50):
        now=self.now()
        with self.tx() as c:
            assert_epoch(c,self.token)
            rows=c.execute('''SELECT s.* FROM qq_official_native_schedule_sources s JOIN agent_routines r USING(routine_id)
                WHERE s.account_key=? AND s.state='active' AND s.revision=r.version AND r.enabled AND r.next_due_at<=?
                ORDER BY r.next_due_at,r.routine_id LIMIT ?''',(self.token.account_key,now,min(max(limit,1),100))).fetchall()
            result=[]
            for raw in rows:
                source=dict(raw)
                if now>=source['authorization_until']:
                    self._cancel(c,'routine',source['routine_id']);continue
                try:
                    _,_,domain=self.current(c,source,'execute')
                except NativeScheduleDenied:
                    self._cancel(c,'routine',source['routine_id']);continue
                # Reuse legacy coalescing: latest due only; never backfill missed slots.
                due=max(domain['next_due_at'],cron_time(domain['cron'],domain['timezone'],now+1,previous=True))
                if due>=source['authorization_until']:
                    self._cancel(c,'routine',source['routine_id']);continue
                c.execute('UPDATE agent_routines SET next_due_at=?,updated_at=? WHERE routine_id=?',
                    (cron_time(domain['cron'],domain['timezone'],now),now,source['routine_id']))
                if c.execute("SELECT 1 FROM routine_runs WHERE routine_id=? AND status IN ('queued','running','generated')",(source['routine_id'],)).fetchone():continue
                result.append(self._occurrence(c,source,due))
            return tuple(result)

    def _owned(self,c,job,operation='execute'):
        assert_epoch(c,self.token)
        o=c.execute('SELECT * FROM qq_official_native_schedule_occurrences WHERE job_id=?',(job.job_id,)).fetchone()
        if o is None:raise NativeScheduleDenied('job_not_owned')
        s=c.execute('SELECT * FROM qq_official_native_schedule_sources WHERE source_id=?',(o['source_id'],)).fetchone()
        self.current(c,s,operation)
        if o['run_id']:
            run=c.execute('SELECT * FROM routine_runs WHERE run_id=? FOR UPDATE',(o['run_id'],)).fetchone()
            if (run is None or run['routine_id']!=s['routine_id'] or run['version']!=s['revision']
                    or run['job_id']!=job.job_id or run['scheduled_at']!=o['scheduled_at']):
                raise NativeScheduleDenied('occurrence_domain_changed')
        j=c.execute('SELECT * FROM durable_jobs WHERE job_id=? FOR UPDATE',(job.job_id,)).fetchone()
        if (job.kind!=account_job_kind(s['kind'],self.token.account_key) or job.scope_key!='qqn:'+digest(s['endpoint_key']) or job.payload!={'occurrence_id':o['occurrence_id']}
                or json.loads(j['payload_json'])!=job.payload or j['kind']!=job.kind or j['scope_key']!=job.scope_key
                or j['status']!='running' or j['lease_owner']!=job.lease_owner or j['lease_generation']!=job.lease_generation
                or j['lease_until']<=self.now() or o['state'] in {'cancelled','needs_attention'}):
            raise NativeScheduleDenied('job_not_owned')
        o=c.execute('SELECT * FROM qq_official_native_schedule_occurrences WHERE occurrence_id=? FOR UPDATE',(o['occurrence_id'],)).fetchone()
        return dict(s),dict(o)

    def _retire_owned(self,c,job,*,reason=None):
        """Local terminal bookkeeping; never creates a new effect grant."""
        assert_epoch(c,self.token)
        o=c.execute('SELECT * FROM qq_official_native_schedule_occurrences WHERE job_id=?',(job.job_id,)).fetchone()
        if o is None:raise NativeScheduleDenied('job_not_owned')
        source=c.execute('SELECT * FROM qq_official_native_schedule_sources WHERE source_id=?',(o['source_id'],)).fetchone()
        if source['account_key']!=self.token.account_key:raise NativeScheduleDenied('job_not_owned')
        expired=self.now()>=min(source['authorization_until'],o['result_deadline'])
        if not expired and reason is None:return None
        # Terminal bookkeeping needs no effect grant, but takes the same
        # all revision sources -> domain -> run -> job -> occurrence row-lock order.
        self._lock_source_family(c,source['kind'],source['routine_id'] or source['reminder_id'])
        source=c.execute('SELECT * FROM qq_official_native_schedule_sources WHERE source_id=? FOR UPDATE',(source['source_id'],)).fetchone()
        if source['routine_id']:
            c.execute('SELECT routine_id FROM agent_routines WHERE routine_id=? FOR UPDATE',(source['routine_id'],)).fetchone()
            c.execute('SELECT run_id FROM routine_runs WHERE run_id=? FOR UPDATE',(o['run_id'],)).fetchone()
        else:c.execute('SELECT reminder_id FROM reminders WHERE reminder_id=? FOR UPDATE',(source['reminder_id'],)).fetchone()
        j=c.execute('SELECT * FROM durable_jobs WHERE job_id=? FOR UPDATE',(job.job_id,)).fetchone()
        if (j is None or j['status']!='running' or j['lease_owner']!=job.lease_owner
                or j['lease_generation']!=job.lease_generation or j['lease_until']<=self.now()
                or job.payload!={'occurrence_id':o['occurrence_id']} or json.loads(j['payload_json'])!=job.payload
                or j['kind']!=account_job_kind(source['kind'],self.token.account_key)):
            raise NativeScheduleDenied('job_not_owned')
        o=c.execute('SELECT * FROM qq_official_native_schedule_occurrences WHERE occurrence_id=? FOR UPDATE',(o['occurrence_id'],)).fetchone()
        if o['state']=='handed_off':return 'handed_off'
        state='needs_attention' if o['state']=='executing' else 'cancelled'
        c.execute('UPDATE qq_official_native_schedule_occurrences SET state=? WHERE occurrence_id=?',(state,o['occurrence_id']))
        if o['run_id']:
            c.execute("UPDATE routine_runs SET status=?,last_error=? WHERE run_id=? AND status IN ('queued','running','generated')",
                ('failed' if state=='needs_attention' else 'cancelled',reason or 'schedule_expired',o['run_id']))
        elif not o['plan_id']:c.execute("UPDATE reminders SET status='cancelled' WHERE reminder_id=? AND status='pending'",(source['reminder_id'],))
        return 'needs_attention' if state=='needs_attention' else 'expired'

    def begin(self,job):
        with self.tx() as c:
            if retired:=self._retire_owned(c,job):return retired,None
            s,o=self._owned(c,job,'send')
            if self.now()>=o['result_deadline']:
                return self._retire_owned(c,job,reason='schedule_expired'),None
            if o['state']=='handed_off':return 'handed_off',o['plan_id']
            if o['state']=='result_ready':return 'result_ready',body_from_json(o['result_json'])
            if o['state']=='executing':
                c.execute("UPDATE qq_official_native_schedule_occurrences SET state='needs_attention' WHERE occurrence_id=?",(o['occurrence_id'],))
                if o['run_id']:c.execute("UPDATE routine_runs SET status='failed',last_error='execution_outcome_unknown' WHERE run_id=?",(o['run_id'],))
                return 'needs_attention',None
            if self.now()>=o['execution_deadline']:
                # A missed queued occurrence must also retire its real domain
                # run/reminder; otherwise queued blocks every later cron slot.
                return self._retire_owned(c,job,reason='execution_deadline_elapsed'),None
            if s['reminder_id']:
                body=MessageBody((TextNode(0,json.loads(s['spec_json'])['text']),))
                self._capture(c,o,body)
                return 'result_ready',body
            attempt=secrets.token_hex(16)
            if not self.usage.reserve_routine(run_id=o['run_id'],attempt_id=attempt,now=self.now(),transaction_cursor=c):
                c.execute("UPDATE qq_official_native_schedule_occurrences SET state='cancelled' WHERE occurrence_id=?",(o['occurrence_id'],))
                c.execute("UPDATE routine_runs SET status='skipped',last_error='budget_exhausted' WHERE run_id=?",(o['run_id'],))
                return 'quota_denied',None
            c.execute("UPDATE qq_official_native_schedule_occurrences SET state='executing',attempt_id=?,worker_owner=?,worker_generation=?,account_epoch=? WHERE occurrence_id=?",
                (attempt,job.lease_owner,job.lease_generation,self.token.epoch,o['occurrence_id']))
            c.execute("UPDATE routine_runs SET status='running',attempt_id=?,updated_at=? WHERE run_id=?",(attempt,self.now(),o['run_id']))
            spec=json.loads(s['spec_json'])
            work=ScheduledWork(EndpointKey.parse(s['endpoint_key']),ActorKey.parse(s['actor_key']),spec['prompt'],
                o['execution_deadline'],o['result_deadline'],spec['max_tokens'],o['run_id'])
            self._model_attempts[id(work)]=(weakref.ref(work),attempt)
            return 'execute',work

    async def settle_model_usage(self,work,summary):
        # Historical usage only, bound to the exact work object emitted before
        # the request. Loss of route authority never invents zero-cost usage.
        pair=self._model_attempts.get(id(work))
        if pair is None or pair[0]() is not work:raise NativeScheduleDenied('unissued_scheduled_work')
        await self.calls(self.usage.settle_routine,pair[1],
            provider=','.join(summary['providers']) or 'none',model=','.join(summary['models']) or 'none',
            input_tokens=summary['input_tokens'],output_tokens=summary['output_tokens'],cached_tokens=summary['cached_tokens'],
            usage_known=summary['usage_known'],now=self.now())
        self._model_attempts.pop(id(work),None)

    def _capture(self,c,o,body):
        if not isinstance(body,MessageBody) or not body.nodes or not all(isinstance(n,TextNode) for n in body.nodes):raise NativeScheduleDenied('invalid_result')
        encoded=body_to_json(body)
        if len(encoded.encode())>262144:raise NativeScheduleDenied('result_too_large')
        c.execute("UPDATE qq_official_native_schedule_occurrences SET state='result_ready',result_json=?,result_digest=? WHERE occurrence_id=?",
            (encoded,digest(encoded),o['occurrence_id']))
        if o['run_id']:c.execute("UPDATE routine_runs SET status='generated',body_json=?,updated_at=? WHERE run_id=?",(encoded,self.now(),o['run_id']))

    def capture(self,job,body):
        with self.tx() as c:
            _,o=self._owned(c,job)
            if (o['state']!='executing' or o['worker_owner']!=job.lease_owner or o['worker_generation']!=job.lease_generation
                    or o['account_epoch']!=self.token.epoch or self.now()>=o['execution_deadline']):raise NativeScheduleDenied('model_owner_lost')
            self._capture(c,o,body)

    def enqueue(self,job):
        with self.tx() as c:
            s,o=self._owned(c,job,'send')
            if o['state']=='handed_off':return o['plan_id']
            if o['state']!='result_ready' or self.now()>=o['result_deadline'] or digest(o['result_json'])!=o['result_digest']:
                raise NativeScheduleDenied('frozen_result_unavailable')
            if self.outbox.source_guard is None:raise NativeScheduleDenied('native_plan_guard_required')
            proof=OccurrenceProof(o['occurrence_id'],job);self._issued[proof]=True
            plan=self.outbox.enqueue(self.token,rendered=render_plan(body_from_json(o['result_json']),EndpointKey.parse(s['endpoint_key'])),
                requester=ActorKey.parse(s['actor_key']),logical_key='native-schedule:'+o['occurrence_id'],source_scope_key=s['scope_key'],
                source_native_message_id='',source_canonical_message_id=None,purpose='background_result',expires_at=o['result_deadline'],
                source_proof=proof,transaction_cursor=c)
            # The actual guard must have bound this exact plan in the same transaction.
            bound=c.execute('SELECT plan_id FROM qq_official_native_schedule_occurrences WHERE occurrence_id=?',(o['occurrence_id'],)).fetchone()[0]
            if bound!=plan.plan_id:raise NativeScheduleDenied('native_plan_guard_required')
            c.execute("UPDATE qq_official_native_schedule_occurrences SET state='handed_off' WHERE occurrence_id=?",(o['occurrence_id'],))
            if o['run_id']:c.execute("UPDATE routine_runs SET status='enqueued',delivery_id=? WHERE run_id=?",(plan.delivery_ids[0],o['run_id']))
            else:c.execute("UPDATE reminders SET status='sending' WHERE reminder_id=?",(s['reminder_id'],))
            return plan.plan_id

    def plan_guard(self,c,**kwargs):
        # M3's host-source denial type; not a migration source/proof.
        from src.plugins.ai_chat.adapters.qq_official.migration_types import MigrationDenied
        try:return self._plan_guard(c,**kwargs)
        except NativeScheduleDenied as error:raise MigrationDenied(str(error)) from None

    def _plan_guard(self,c,*,token,plan,operation,now,source_proof=None,logical_key=None,**_):
        if token is not self.token:raise NativeScheduleDenied('wrong_account_lease')
        if operation=='enqueue':
            if type(source_proof) is not OccurrenceProof or source_proof not in self._issued:raise NativeScheduleDenied('unissued_occurrence')
            s,o=self._owned(c,source_proof.job,'send')
            if (o['occurrence_id']!=source_proof.occurrence_id or logical_key!='native-schedule:'+o['occurrence_id']
                    or o['state']!='result_ready' or o['plan_id'] not in {None,plan['plan_id']}):raise NativeScheduleDenied('result_binding_changed')
        else:
            raw=c.execute('SELECT * FROM qq_official_native_schedule_occurrences WHERE plan_id=?',(plan['plan_id'],)).fetchone()
            if raw is None:raise NativeScheduleDenied('native_plan_source_missing')
            o=dict(raw);s=c.execute('SELECT * FROM qq_official_native_schedule_sources WHERE source_id=?',(o['source_id'],)).fetchone()
            self.current(c,s,'send')
            if o['state']!='handed_off':raise NativeScheduleDenied('native_plan_not_ready')
        if (plan['account_key']!=s['account_key'] or plan['endpoint_key']!=s['endpoint_key'] or plan['requester_actor_key']!=s['actor_key']
                or plan['purpose']!='background_result' or plan['expires_at']!=o['result_deadline'] or self.now()>=o['result_deadline']
                or plan['endpoint_generation']!=s['endpoint_generation'] or plan['route_epoch']!=s['endpoint_route_epoch']):
            raise NativeScheduleDenied('native_plan_identity_changed')
        manifest=json.loads(plan['manifest_json'])
        if (digest(o['result_json'])!=o['result_digest']
                or manifest.get('rendered')!=render_plan(body_from_json(o['result_json']),EndpointKey.parse(s['endpoint_key'])).to_json()):
            raise NativeScheduleDenied('native_plan_body_changed')
        if operation=='enqueue':c.execute('UPDATE qq_official_native_schedule_occurrences SET plan_id=? WHERE occurrence_id=?',(plan['plan_id'],o['occurrence_id']))
        return True

    def reconcile_delivery(self,c,plan_id):
        """Receipt-only historical projection; no current source/send authority."""
        assert_epoch(c,self.token)
        o=c.execute('SELECT * FROM qq_official_native_schedule_occurrences WHERE plan_id=? AND account_key=?',
            (plan_id,self.token.account_key)).fetchone()
        if o is None:return False
        source=c.execute('SELECT * FROM qq_official_native_schedule_sources WHERE source_id=?',(o['source_id'],)).fetchone()
        self._lock_source_family(c,source['kind'],source['routine_id'] or source['reminder_id'])
        source=c.execute('SELECT * FROM qq_official_native_schedule_sources WHERE source_id=? FOR UPDATE',(o['source_id'],)).fetchone()
        if source['reminder_id']:
            c.execute('SELECT reminder_id FROM reminders WHERE reminder_id=? FOR UPDATE',(source['reminder_id'],)).fetchone()
        else:
            c.execute('SELECT routine_id FROM agent_routines WHERE routine_id=? FOR UPDATE',(source['routine_id'],)).fetchone()
            c.execute('SELECT run_id FROM routine_runs WHERE run_id=? FOR UPDATE',(o['run_id'],)).fetchone()
        o=c.execute('SELECT * FROM qq_official_native_schedule_occurrences WHERE occurrence_id=? FOR UPDATE',(o['occurrence_id'],)).fetchone()
        plan=c.execute('SELECT * FROM delivery_plans WHERE plan_id=?',(plan_id,)).fetchone()
        parts=c.execute('SELECT * FROM deliveries WHERE plan_id=? ORDER BY part_index',(plan_id,)).fetchall()
        if (plan is None or len(parts)!=plan['part_count'] or plan['account_key']!=source['account_key']
                or plan['endpoint_key']!=source['endpoint_key'] or plan['requester_actor_key']!=source['actor_key']
                or any(part['account_key']!=source['account_key'] or part['endpoint_key']!=source['endpoint_key'] for part in parts)):
            raise NativeScheduleDenied('historical_plan_identity_changed')
        states={part['status'] for part in parts}
        if states=={'committed'}:
            if source['reminder_id']:
                c.execute("UPDATE reminders SET status='sent',sent_at=?,lease_until=NULL,last_error='' WHERE reminder_id=?",
                    (max(part['updated_at'] for part in parts),source['reminder_id']))
            return True
        reason=('delivery_unknown' if 'ambiguous' in states else 'delivery_partial'
            if 'committed' in states and states<={'committed','failed','cancelled'} else
            'delivery_not_accepted' if states<={'failed','cancelled'} else '')
        if reason and source['reminder_id']:
            c.execute('UPDATE reminders SET last_error=?,status=? WHERE reminder_id=?',
                (reason,'cancelled' if reason=='delivery_not_accepted' else 'sending',source['reminder_id']))
        elif reason:c.execute('UPDATE routine_runs SET last_error=? WHERE run_id=?',(reason,o['run_id']))
        return True

    def reconcile(self,limit=50):
        with self.tx() as c:
            assert_epoch(c,self.token)
            rows=c.execute('SELECT occurrence_id,plan_id FROM qq_official_native_schedule_occurrences WHERE account_key=? AND plan_id IS NOT NULL AND occurrence_id>? ORDER BY occurrence_id LIMIT ?',
                (self.token.account_key,self._projection_cursor,min(max(limit,1),100))).fetchall()
            for row in rows:self.reconcile_delivery(c,row['plan_id'])
            self._projection_cursor=rows[-1]['occurrence_id'] if rows else ''
            return len(rows)

    async def execute(self,job):
        state,value=await self.calls(self.begin,job)
        if state=='execute':
            if self.run_model is None:raise NativeScheduleDenied('model_port_unavailable')
            def guard(cursor):
                _,o=self._owned(cursor,job)
                if o['state']!='executing' or self.now()>=o['execution_deadline']:raise NativeScheduleDenied('model_owner_lost')
            def check():
                try:
                    with self.tx() as c:guard(c)
                    return True
                except Exception:return False
            marker=active_job_fence.set(JobFence(job.job_id,job.lease_owner,job.attempts,check,job.lease_generation,guard))
            try:
                async with asyncio.timeout(max(0,value.execution_deadline-self.now())):
                    body=await self.run_model(value)
                await self.calls(self.capture,job,body)
            except LeaseLost:
                try:
                    with self.tx() as c:self._retire_owned(c,job,reason='ownership_lost')
                except Exception:pass
                return {'status':'needs_attention','reason':'ownership_lost'}
            except Exception:
                try:
                    with self.tx() as c:self._retire_owned(c,job,reason='execution_outcome_unknown')
                except Exception:pass
                return {'status':'needs_attention','reason':'execution_outcome_unknown'}
            finally:
                active_job_fence.reset(marker)
                self._model_attempts.pop(id(value),None)
            state='result_ready'
        if state=='result_ready':
            return {'status':'queued','plan_id':await self.calls(self.enqueue,job)}
        return {'status':state}
