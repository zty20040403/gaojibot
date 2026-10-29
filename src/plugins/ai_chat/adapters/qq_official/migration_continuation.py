"""M5 continuation: original domain rows + original durable queue + M3, no Event shim.

Journal is a mandatory host authority port, not a caller supplied boolean. It
issues locally registered proofs after reading the current journal and mapping;
no proof/digest/endpoint from a job payload is trusted. All methods with cursor
participate in the caller's transaction and never commit or sleep.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
import threading
import uuid

from .migration_preview import Selection, SourceRef
from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.adapters.qq_official.identity_store import snapshot
from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
from src.plugins.ai_chat.adapters.qq_official.outbox_types import stable_json
from src.plugins.ai_chat.agent.control import JobFence, active_job_fence, LeaseLost as ModelLeaseLost
from src.plugins.ai_chat.message_ir import MessageBody, TextNode, body_from_json, body_to_json
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from src.plugins.ai_chat.routines import Routine

PARKED = 2**63 - 1


class ContinuationDenied(ValueError):
    def __init__(self):
        super().__init__('migration_continuation_unavailable')


def number(value):
    if type(value) is not int or not 0 <= value < PARKED:
        raise ContinuationDenied()
    return value


def row(cursor, query, parameters=()):
    found = cursor.execute(query, parameters).fetchone()
    return None if found is None else dict(found)


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True, repr=False)
class ScheduledWork:
    endpoint: EndpointKey
    actor: ActorKey
    prompt: str
    execution_deadline: int
    result_deadline: int
    max_tokens: int
    run_id: str


class SharedScheduledModel:
    """The existing common loop; intentionally no host/project/file tools.

    Profile selection is a trusted host callback using the structured actor.
    Capability-granted project work is a separate integration, not inferred from
    old numeric creator/project memberships. No fabricated inbound message.
    """
    def __init__(self, profile_for, usage_callback=None):
        self.profile_for = profile_for
        self.usage_callback = usage_callback

    async def __call__(self, work):
        from src.plugins.ai_chat.agent.control import assert_job_owned
        from src.plugins.ai_chat.agent_loop import ask_deepseek_with_tools
        from src.plugins.ai_chat.model_budget import ModelCallBudget, model_budget_scope
        assert_job_owned()
        profile = self.profile_for(work)
        profile = replace(profile, max_output_tokens=min(profile.max_output_tokens or work.max_tokens, work.max_tokens))
        async def unavailable(*args, **kwargs):
            raise ContinuationDenied()
        budget = ModelCallBudget(work.max_tokens, before_request=assert_job_owned)
        try:
            with model_budget_scope(budget):
                answer = await ask_deepseek_with_tools(work.prompt, [], [], unavailable,
                    profile=profile, max_tool_rounds=0,
                    system_context='Execute this approved scheduled text task. No tools are authorized.')
        finally:
            if self.usage_callback is not None:
                await self.usage_callback(work, budget.summary())
        assert_job_owned()
        return MessageBody((TextNode(0, answer),))


class Continuations:
    def __init__(self, database, *, token, jobs, usage, outbox, journal, preview, clock, run_model, database_calls):
        self.database, self.token, self.jobs, self.usage = database, token, jobs, usage
        self.outbox, self.journal, self.preview = outbox, journal, preview
        self.clock, self.run_model = clock, run_model
        self.database_calls = database_calls
        self._clock_lock = threading.Lock()
        self._last_now = 0

    def _now(self):
        value=self.clock()
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<=value<PARKED:
            raise ContinuationDenied()
        with self._clock_lock:
            self._last_now=max(self._last_now,int(value))
            return self._last_now

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

    def _proof(self, cursor, proof, operation, now, job=None, plan_id=None):
        number(now)
        self.journal.require_item(cursor, proof, operation=operation, job=job, now=now,
            **({'plan_id': plan_id} if plan_id is not None else {}))
        assert_epoch(cursor, self.token)
        endpoint, actor = proof.target_endpoint, proof.target_actor
        if (endpoint.account.key != self.token.account_key or not actor.matches_endpoint(endpoint)
                or actor.identity_kind == 'bot'):
            raise ContinuationDenied()
        route = row(cursor, 'SELECT * FROM qq_official_endpoints WHERE endpoint_key=? FOR UPDATE', (endpoint.key,))
        if (route is None or route['membership_state'] != 'present'
                or route['binding_state'] == 'revoked'
                or (endpoint.kind == 'group' and route['binding_state'] != 'active')
                or route['generation'] != proof.endpoint_generation
                or route['route_epoch'] != proof.endpoint_route_epoch):
            raise ContinuationDenied()
        # The journal port also performs current full-actor admission and logical
        # route/mapping/grant checks; membership is never inferred from history.

    def _bot_native_id(self, cursor, endpoint):
        verified = snapshot(cursor, endpoint.account)
        if verified is None or verified.bot_actor.account != endpoint.account:
            raise ContinuationDenied()
        return verified.bot_actor.native_user_id

    def prepare(self, cursor, proof, *, now):
        self._proof(cursor, proof, 'prepare', now)
        existing = row(cursor, 'SELECT * FROM qq_official_schedule_bindings WHERE item_id=? FOR UPDATE', (proof.item_id,))
        if existing:
            self._same(existing, proof)
            return existing
        source = SourceRef(proof.source_kind, proof.source_key)
        selection = Selection(proof.source_endpoint, proof.source_scope, proof.target_endpoint)
        if source.kind == 'reminder':
            original = row(cursor, 'SELECT * FROM reminders WHERE reminder_id=? FOR UPDATE', (int(source.key),))
            old_job = None
        elif source.kind == 'routine' and source.key.startswith('run:'):
            original = row(cursor, 'SELECT * FROM routine_runs WHERE run_id=?', (source.key[4:],))
            if original is None:
                raise ContinuationDenied()
            spec = row(cursor, 'SELECT * FROM agent_routines WHERE routine_id=? FOR UPDATE', (original['routine_id'],))
            old_job = row(cursor, 'SELECT * FROM durable_jobs WHERE job_id=? FOR UPDATE', (original['job_id'],))
            original = row(cursor, 'SELECT * FROM routine_runs WHERE run_id=? FOR UPDATE', (source.key[4:],))
            if spec is None or spec['project_id'] is not None or old_job is None or old_job['status'] == 'running':
                raise ContinuationDenied()
        else:
            raise ContinuationDenied()
        exported = self.preview.preview(cursor, selection, source, now=now)
        if (exported.disposition != 'unsent' or exported.safe_digest != proof.export_digest
                or exported.export['mapping']['mapping_id'] != proof.mapping_id
                or exported.export['mapping']['revision'] != proof.mapping_revision):
            raise ContinuationDenied()
        data = exported.export
        due = data['scheduled_for'] if source.kind == 'reminder' else data['scheduled_at']
        execution_deadline, result_deadline = number(proof.execution_deadline), number(proof.result_deadline)
        if (not due < result_deadline or not execution_deadline <= result_deadline or now >= result_deadline
                or (exported.original_deadline is not None and exported.original_deadline != execution_deadline)):
            raise ContinuationDenied()
        endpoint, actor = proof.target_endpoint, proof.target_actor
        identity = (endpoint.account.key, endpoint.key, actor.key, proof.endpoint_generation, proof.endpoint_route_epoch)
        reminder_id = routine_id = run_id = None
        if source.kind == 'reminder':
            # A legacy sent marker is weak evidence; preview never exports it.
            cursor.execute("UPDATE reminders SET status='cancelled',lease_until=NULL,last_error='migrated' WHERE reminder_id=? AND status='pending' AND attempts=0", (original['reminder_id'],))
            if cursor.rowcount != 1:
                raise ContinuationDenied()
            cursor.execute('''INSERT INTO reminders
                (scope_key,platform,conversation_kind,native_conversation_id,creator_native_user_id,
                 message,scheduled_for,next_attempt_at,status,attempts,created_at,last_error,
                 account_key,endpoint_key,creator_actor_key,endpoint_generation,endpoint_route_epoch)
                VALUES (?,?,?,?,?,?,?,?,'pending',0,?,'',?,?,?,?,?) RETURNING reminder_id''',
                (endpoint.scope.key, 'qq-official', endpoint.kind, endpoint.native_conversation_id,
                 actor.native_user_id, data['text'], due, due, original['created_at'], *identity))
            reminder_id = cursor.fetchone()[0]
        else:
            # Freeze only the selected occurrence; parent schedule separately
            # requires its own approved finite schedule migration item.
            if original['status'] not in {'queued', 'generated', 'enqueued'}:
                raise ContinuationDenied()
            cursor.execute("UPDATE routine_runs SET status='cancelled',updated_at=? WHERE run_id=?", (now, original['run_id']))
            cursor.execute("UPDATE durable_jobs SET status='cancelled',finished_at=?,updated_at=? WHERE job_id=? AND status<>'running'", (now, now, old_job['job_id']))
            cursor.execute('''INSERT INTO agent_routines
                (scope_key,platform,kind,conversation_id,bot_id,creator_id,name,prompt,cron,timezone,
                 enabled,version,max_tokens,daily_tokens,daily_runs,next_due_at,created_at,updated_at,
                 account_key,endpoint_key,creator_actor_key,endpoint_generation,endpoint_route_epoch)
                VALUES (?,'qq-official',?,?,?,?,?,?,?,?,TRUE,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING routine_id''',
                (endpoint.scope.key, endpoint.kind, endpoint.native_conversation_id, self._bot_native_id(cursor, endpoint),
                 actor.native_user_id, data['name'], data['prompt'], data['cron'], data['timezone'], data['version'],
                 data['max_tokens'], data['daily_tokens'], data['daily_runs'], data['next_due_at'], now, now, *identity))
            routine_id = cursor.fetchone()[0]
            budget_origin = spec['budget_origin_routine_id'] or spec['routine_id']
            origin = row(cursor, 'SELECT budget_origin_routine_id FROM agent_routines WHERE routine_id=?', (budget_origin,))
            if origin is None or origin['budget_origin_routine_id'] is not None:
                raise ContinuationDenied()
            cursor.execute('UPDATE agent_routines SET budget_origin_routine_id=? WHERE routine_id=?',
                (budget_origin, routine_id))
            target = row(cursor, 'SELECT * FROM agent_routines WHERE routine_id=?', (routine_id,))
            target_spec = Routine(**target)
            run_id = uuid.uuid4().hex
            body = body_to_json(MessageBody((TextNode(0, data['result_text']),))) if data['generated'] else ''
            cursor.execute('''INSERT INTO routine_runs
                (run_id,routine_id,version,scheduled_at,missed_from,spec_json,status,body_json,
                 deadline_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (run_id, routine_id, target_spec.version, due, data['missed_from'],
                 json.dumps(asdict(target_spec), ensure_ascii=False), 'generated' if body else 'queued',
                 body, execution_deadline, now, now))
        binding_id = uuid.uuid4().hex
        cursor.execute('''INSERT INTO qq_official_schedule_bindings
            (binding_id,item_id,item_revision,export_digest,source_kind,source_key,source_revision,source_scope_key,
             source_canonical_message_id,source_native_message_id,target_reminder_id,target_routine_id,target_run_id,
             account_key,endpoint_key,actor_key,logical_route_epoch,endpoint_generation,endpoint_route_epoch,
             due_at,execution_deadline,result_deadline,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (binding_id, proof.item_id, proof.item_revision, proof.export_digest, source.kind, source.key, proof.source_revision,
             proof.source_scope.key, proof.source_canonical_message_id, proof.source_native_message_id,
             reminder_id, routine_id, run_id, endpoint.account.key, endpoint.key, actor.key,
             proof.logical_route_epoch, proof.endpoint_generation, proof.endpoint_route_epoch,
             due, execution_deadline, result_deadline, now))
        job, _ = self.jobs.enqueue(kind=account_job_kind(source.kind, endpoint.account.key),
            idempotency_key='qq-migration:' + _digest(proof.item_id), scope_key=endpoint.scope.key,
            payload={'binding_id': binding_id}, resume_on_lease_loss=True,
            run_at=PARKED, now=now, transaction_cursor=cursor)
        cursor.execute('UPDATE qq_official_schedule_bindings SET job_id=? WHERE binding_id=?', (job.job_id, binding_id))
        if run_id:
            cursor.execute('UPDATE routine_runs SET job_id=? WHERE run_id=?', (job.job_id, run_id))
        self.journal.bind_item_job(cursor, proof, job_id=job.job_id, expected_kind=job.kind, now=now)
        return row(cursor, 'SELECT * FROM qq_official_schedule_bindings WHERE binding_id=?', (binding_id,))

    @staticmethod
    def _same(binding, proof):
        expected = (proof.source_revision, proof.item_revision, proof.export_digest, proof.target_endpoint.account.key,
            proof.target_endpoint.key, proof.target_actor.key, proof.logical_route_epoch,
            proof.endpoint_generation, proof.endpoint_route_epoch, proof.execution_deadline, proof.result_deadline)
        actual = tuple(binding[key] for key in ('source_revision','item_revision','export_digest','account_key','endpoint_key',
            'actor_key','logical_route_epoch','endpoint_generation','endpoint_route_epoch','execution_deadline','result_deadline'))
        if actual != expected:
            raise ContinuationDenied()

    def activate(self, cursor, proof, *, now):
        self._proof(cursor, proof, 'activate', now)
        binding = row(cursor, 'SELECT * FROM qq_official_schedule_bindings WHERE item_id=? FOR UPDATE', (proof.item_id,))
        if binding is None:
            raise ContinuationDenied()
        self._same(binding, proof)
        if binding['activated_at'] is not None:
            return binding
        # Past absolute deadlines are terminal, not a fresh lease on content.
        if now >= binding['result_deadline']:
            cursor.execute("UPDATE durable_jobs SET status='cancelled',finished_at=?,updated_at=? WHERE job_id=? AND status='pending' AND attempts=0", (now, now, binding['job_id']))
            self._terminal(cursor, binding, 'migration_deadline_expired', now)
        else:
            self.journal.activate_item(cursor, proof, due_at=binding['due_at'], now=now)
        cursor.execute('UPDATE qq_official_schedule_bindings SET activated_at=? WHERE binding_id=?', (now, binding['binding_id']))
        return binding

    def _current(self, cursor, job, *, operation, now):
        binding = row(cursor, 'SELECT * FROM qq_official_schedule_bindings WHERE binding_id=?', (job.payload.get('binding_id'),))
        if binding is None:
            raise ContinuationDenied()
        proof = self.journal.issue_item(cursor, item_id=binding['item_id'], operation=operation, job=job, now=now)
        self._proof(cursor, proof, operation, now, job)
        binding = row(cursor, 'SELECT * FROM qq_official_schedule_bindings WHERE binding_id=? FOR UPDATE', (binding['binding_id'],))
        self._same(binding, proof)
        self._source_version_current(cursor, binding)
        owned = row(cursor, '''SELECT * FROM durable_jobs WHERE job_id=? AND status='running'
            AND lease_owner=? AND lease_generation=? AND lease_until>? FOR UPDATE''',
            (job.job_id, job.lease_owner, job.lease_generation, now))
        if (owned is None or job.job_id != binding['job_id'] or binding['activated_at'] is None
                or job.kind != account_job_kind(binding['source_kind'], self.token.account_key)
                or now >= binding['result_deadline']):
            raise ContinuationDenied()
        if binding['target_reminder_id']:
            domain = row(cursor, 'SELECT * FROM reminders WHERE reminder_id=? FOR UPDATE', (binding['target_reminder_id'],))
            active = domain and domain['status'] in {'pending','sending','sent'}
        else:
            spec = row(cursor, 'SELECT * FROM agent_routines WHERE routine_id=? FOR UPDATE', (binding['target_routine_id'],))
            domain = row(cursor, 'SELECT * FROM routine_runs WHERE run_id=? FOR UPDATE', (binding['target_run_id'],))
            active = (spec and spec['enabled'] and spec['state']=='active' and domain
                and spec['version']==domain['version'] and domain['status'] in {'queued','running','generated','enqueued'})
        if not active:
            raise ContinuationDenied()
        if binding['target_run_id']:
            frozen = json.loads(domain['spec_json'])
            keys = ('scope_key','platform','kind','conversation_id','bot_id','creator_id','prompt','cron','timezone','version',
                'max_tokens','daily_tokens','daily_runs','account_key','endpoint_key','creator_actor_key','endpoint_generation','endpoint_route_epoch','budget_origin_routine_id')
            if any(frozen.get(key) != spec[key] for key in keys):
                raise ContinuationDenied()
        actual = domain if binding['target_reminder_id'] else spec
        if tuple(actual[k] for k in ('account_key','endpoint_key','creator_actor_key','endpoint_generation','endpoint_route_epoch')) != tuple(binding[k] for k in ('account_key','endpoint_key','actor_key','endpoint_generation','endpoint_route_epoch')):
            raise ContinuationDenied()
        return binding, domain

    def _terminal(self, cursor, binding, reason, now):
        if binding['target_reminder_id']:
            cursor.execute("UPDATE reminders SET status='cancelled',last_error=? WHERE reminder_id=?", (reason, binding['target_reminder_id']))
        else:
            cursor.execute("UPDATE routine_runs SET status='failed',last_error=?,updated_at=? WHERE run_id=?", (reason, now, binding['target_run_id']))

    def begin(self, job):
        now = self._now()
        with self.tx() as cursor:
            discovery = row(cursor, 'SELECT * FROM qq_official_schedule_bindings WHERE binding_id=?', (job.payload.get('binding_id'),))
            if discovery and now >= discovery['result_deadline']:
                self._expire_owned(cursor, job, discovery, now)
                return 'expired', None
            prior = row(cursor, '''SELECT b.plan_id,b.execution_deadline,r.status FROM qq_official_schedule_bindings b
                LEFT JOIN routine_runs r ON r.run_id=b.target_run_id WHERE b.binding_id=?''', (job.payload.get('binding_id'),))
            operation = 'send' if prior and (prior['plan_id'] or prior['status']=='generated' or now>=prior['execution_deadline']) else 'execute'
            binding, domain = self._current(cursor, job, operation=operation, now=now)
            if binding['plan_id']:
                return 'handed_off', None
            if binding['target_reminder_id']:
                return 'ready', MessageBody((TextNode(0, domain['message']),))
            if domain['status'] == 'generated':
                return 'ready', body_from_json(domain['body_json'])
            if domain['status'] == 'running':
                self._terminal(cursor, binding, 'migration_execution_unknown', now)
                return 'needs_attention', None
            if now >= binding['execution_deadline']:
                self._terminal(cursor, binding, 'migration_deadline_expired', now)
                return 'expired', None
            attempt = uuid.uuid4().hex
            if not self.usage.reserve_routine(run_id=domain['run_id'], attempt_id=attempt, now=now, transaction_cursor=cursor):
                self._terminal(cursor, binding, 'migration_budget_exhausted', now)
                return 'budget_exhausted', None
            cursor.execute("UPDATE routine_runs SET status='running',attempt_id=?,updated_at=? WHERE run_id=?", (attempt, now, domain['run_id']))
            spec = row(cursor, 'SELECT * FROM agent_routines WHERE routine_id=?', (domain['routine_id'],))
            return 'execute', ScheduledWork(EndpointKey.parse(binding['endpoint_key']), ActorKey.parse(binding['actor_key']),
                spec['prompt'], binding['execution_deadline'], binding['result_deadline'], spec['max_tokens'], domain['run_id'])

    def capture_result(self, job, body):
        if not isinstance(body, MessageBody) or not body.nodes or not all(isinstance(n, TextNode) for n in body.nodes):
            raise ContinuationDenied()
        encoded = body_to_json(body)
        if len(encoded.encode()) > 2_000_000:
            raise ContinuationDenied()
        now = self._now()
        with self.tx() as cursor:
            binding, domain = self._current(cursor, job, operation='execute', now=now)
            if binding['target_reminder_id'] or domain['status'] not in {'running','generated'}:
                raise ContinuationDenied()
            if domain['status']=='generated' and encoded != domain['body_json']:
                raise ContinuationDenied()
            cursor.execute("UPDATE routine_runs SET status='generated',body_json=?,updated_at=? WHERE run_id=?", (encoded, now, domain['run_id']))

    def save_and_enqueue(self, job, body):
        if not isinstance(body, MessageBody) or not body.nodes or not all(isinstance(n, TextNode) for n in body.nodes):
            raise ContinuationDenied()
        now = self._now()
        with self.tx() as cursor:
            binding, domain = self._current(cursor, job, operation='send', now=now)
            if binding['plan_id']:
                return binding['plan_id']
            encoded = body_to_json(body)
            if binding['target_reminder_id']:
                if encoded != body_to_json(MessageBody((TextNode(0, domain['message']),))):
                    raise ContinuationDenied()
            else:
                if domain['status']!='generated' or encoded != domain['body_json']:
                    raise ContinuationDenied()
            endpoint = EndpointKey.parse(binding['endpoint_key'])
            proof = self.journal.issue_item(cursor, item_id=binding['item_id'], operation='send', job=job, now=now)
            plan = self.outbox.enqueue(self.token, rendered=render_plan(body, endpoint),
                requester=ActorKey.parse(binding['actor_key']),
                logical_key='qq-migration-result:' + binding['item_id'],
                source_scope_key=endpoint.scope.key, expires_at=binding['result_deadline'],
                purpose='background_result', source_native_message_id='', source_canonical_message_id=None,
                source_proof=proof, transaction_cursor=cursor)
            cursor.execute('UPDATE qq_official_schedule_bindings SET plan_id=? WHERE binding_id=?', (plan.plan_id, binding['binding_id']))
            self.journal.bind_item_plan(cursor, proof, plan_id=plan.plan_id, job=job, now=now)
            if binding['target_reminder_id']:
                cursor.execute("UPDATE reminders SET status='sending',lease_until=NULL WHERE reminder_id=?", (binding['target_reminder_id'],))
            else:
                cursor.execute("UPDATE routine_runs SET status='enqueued',delivery_id=?,updated_at=? WHERE run_id=?", (plan.delivery_ids[0], now, domain['run_id']))
            return plan.plan_id

    def expire(self, cursor, item_id, *, now):
        """Local terminal cleanup only; no current actor execution permission."""
        binding = row(cursor, 'SELECT * FROM qq_official_schedule_bindings WHERE item_id=?', (item_id,))
        if not binding or now<binding['result_deadline']:
            raise ContinuationDenied()
        proof = self.journal.issue_item(cursor,item_id=item_id,operation='expire',now=now)
        assert_epoch(cursor,self.token)
        self._same(binding,proof)
        self.journal.expire_item(cursor,proof,now=now)
        if binding['target_run_id'] is None and binding['target_routine_id'] is not None:
            cursor.execute("UPDATE agent_routines SET enabled=FALSE,state='paused',updated_at=? WHERE routine_id=?", (now,binding['target_routine_id']))
        elif binding['plan_id'] is None:
            self._terminal(cursor,binding,'migration_deadline_expired',now)
        # An old plan/receipt is immutable evidence; M3 performs its own expiry
        # or late acceptance projection. Never turn unknown into known failure.
        return binding

    def _expire_owned(self,cursor,job,binding,now):
        proof = self.journal.issue_item(cursor,item_id=binding['item_id'],operation='expire',now=now)
        assert_epoch(cursor,self.token)
        self._same(binding,proof)
        current=row(cursor,'''SELECT * FROM durable_jobs WHERE job_id=? AND status='running'
            AND lease_owner=? AND lease_generation=? AND lease_until>? FOR UPDATE''',
            (job.job_id,job.lease_owner,job.lease_generation,now))
        if not current or binding['job_id']!=job.job_id:
            raise ContinuationDenied()
        self.validate_bound_job(cursor,proof,job,operation='expire',now=now)
        if binding['plan_id'] is None:
            self._terminal(cursor,binding,'migration_deadline_expired',now)
        # Existing worker settles its owned job; later expire() marks the item.

    def reconcile_delivery(self,cursor,plan_id):
        """Historical M3 projection; accepted receipt is not new send authority."""
        binding=row(cursor,'SELECT * FROM qq_official_schedule_bindings WHERE plan_id=? FOR UPDATE',(plan_id,))
        if binding is None:
            return False
        plan=row(cursor,'SELECT * FROM delivery_plans WHERE plan_id=?',(plan_id,))
        deliveries=[dict(r) for r in cursor.execute('SELECT * FROM deliveries WHERE plan_id=? ORDER BY part_index',(plan_id,)).fetchall()]
        if not plan or len(deliveries)!=plan['part_count'] or any(r['account_key']!=binding['account_key'] or r['endpoint_key']!=binding['endpoint_key'] for r in deliveries):
            raise ContinuationDenied()
        statuses={r['status'] for r in deliveries}
        if statuses=={'committed'}:
            timestamp=max(r['updated_at'] for r in deliveries)
            if binding['target_reminder_id']:
                cursor.execute("UPDATE reminders SET status='sent',sent_at=?,lease_until=NULL,last_error='' WHERE reminder_id=?",(timestamp,binding['target_reminder_id']))
            return True
        reason='migration_delivery_unknown' if 'ambiguous' in statuses else 'migration_delivery_partial' if 'committed' in statuses and statuses<={'committed','failed','cancelled'} else 'migration_delivery_not_accepted' if statuses<={'failed','cancelled'} else ''
        if reason:
            if binding['target_reminder_id']:
                cursor.execute('UPDATE reminders SET last_error=?,status=? WHERE reminder_id=?',
                    (reason,'cancelled' if reason=='migration_delivery_not_accepted' else 'sending',binding['target_reminder_id']))
            else:
                cursor.execute('UPDATE routine_runs SET last_error=? WHERE run_id=?',(reason,binding['target_run_id']))
        return True

    def _source_version_current(self,cursor,binding):
        if binding['parent_binding_id'] is None:
            return
        parent=row(cursor,'SELECT * FROM qq_official_schedule_bindings WHERE binding_id=?',(binding['parent_binding_id'],))
        if parent is None or not parent['source_key'].startswith('spec:'):
            raise ContinuationDenied()
        source=row(cursor,'SELECT version,enabled FROM agent_routines WHERE routine_id=?',(int(parent['source_key'][5:]),))
        if source is None or source['enabled'] or source['version']!=binding['source_revision']:
            raise ContinuationDenied()

    def validate_bound_job(self, cursor, item, job, *, operation, now, due_at=None):
        item_id = item.item_id if hasattr(item, 'item_id') else item['item_id']
        job_id = job.job_id if hasattr(job, 'job_id') else job['job_id']
        binding = row(cursor, 'SELECT * FROM qq_official_schedule_bindings WHERE item_id=? AND job_id=?', (item_id, job_id))
        if binding is None or (due_at is not None and due_at!=binding['due_at']):
            raise ContinuationDenied()
        if operation!='expire':
            self._source_version_current(cursor,binding)
        if binding['target_run_id']:
            domain = row(cursor, 'SELECT job_id,routine_id,scheduled_at FROM routine_runs WHERE run_id=?', (binding['target_run_id'],))
            if not domain or domain['job_id']!=job_id or domain['routine_id']!=binding['target_routine_id'] or domain['scheduled_at']!=binding['due_at']:
                raise ContinuationDenied()
        elif binding['target_reminder_id']:
            if not row(cursor, 'SELECT 1 FROM reminders WHERE reminder_id=? AND account_key=? AND endpoint_key=? AND creator_actor_key=?',
                    (binding['target_reminder_id'],binding['account_key'],binding['endpoint_key'],binding['actor_key'])):
                raise ContinuationDenied()
        else:
            raise ContinuationDenied()

    def validate_bound_plan(self, cursor, item, plan_id, *, now):
        item_id = item.item_id if hasattr(item, 'item_id') else item['item_id']
        binding = row(cursor, 'SELECT * FROM qq_official_schedule_bindings WHERE item_id=? AND plan_id=?', (item_id, plan_id))
        plan = row(cursor, 'SELECT * FROM delivery_plans WHERE plan_id=?', (plan_id,))
        if not binding or not plan or any(binding[a]!=plan[b] for a,b in
                (('account_key','account_key'),('endpoint_key','endpoint_key'),('actor_key','requester_actor_key'),('endpoint_generation','endpoint_generation'),('endpoint_route_epoch','route_epoch'))):
            raise ContinuationDenied()
        self._source_version_current(cursor,binding)

    def current_plan_source(self, cursor, plan_id, now):
        """M3 trusted port before every new attempt/physical send; not receipt.

        Returns False only when this is not a migrated schedule plan. Existing
        migration binding with missing authority raises; never silently bypass.
        The original finished job need not be running to deliver its result.
        """
        binding = row(cursor, 'SELECT * FROM qq_official_schedule_bindings WHERE plan_id=?', (plan_id,))
        if binding is None:
            return False
        proof = self.journal.issue_item(cursor, item_id=binding['item_id'], operation='deliver', plan_id=plan_id, now=now)
        self._proof(cursor, proof, 'deliver', now, plan_id=plan_id)
        self._same(binding, proof)
        self._source_version_current(cursor, binding)
        plan = row(cursor, 'SELECT * FROM delivery_plans WHERE plan_id=?', (plan_id,))
        if (plan is None or plan['account_key']!=binding['account_key'] or plan['endpoint_key']!=binding['endpoint_key']
                or plan['requester_actor_key']!=binding['actor_key'] or plan['expires_at']!=binding['result_deadline']
                or plan['idempotency_key']!='qqp1:'+_digest(stable_json(['qq-plan-v1',binding['account_key'],binding['endpoint_key'],'qq-migration-result:'+binding['item_id']])) or now>=binding['result_deadline']):
            raise ContinuationDenied()
        if binding['target_reminder_id']:
            domain = row(cursor, 'SELECT status FROM reminders WHERE reminder_id=?', (binding['target_reminder_id'],))
            if domain is None or domain['status'] not in {'sending','sent'}:
                raise ContinuationDenied()
        else:
            domain = row(cursor, '''SELECT a.enabled,a.version,r.version AS run_version,r.status FROM agent_routines a
                JOIN routine_runs r USING(routine_id) WHERE r.run_id=?''', (binding['target_run_id'],))
            if domain is None or not domain['enabled'] or domain['version']!=domain['run_version'] or domain['status']!='enqueued':
                raise ContinuationDenied()
        return True

    async def execute(self, job):
        state, value = await self.database_calls(self.begin, job)
        if state in {'handed_off','needs_attention','expired','budget_exhausted'}:
            return {'status': state}
        def current():
            try:
                with self.tx() as cursor:
                    self._current(cursor, job, operation='execute', now=self._now())
                return True
            except Exception:
                return False
        fence = JobFence(job.job_id, job.lease_owner, job.attempts, current, job.lease_generation,
            lambda cursor: self._current(cursor, job, operation='execute', now=self._now()))
        context = active_job_fence.set(fence)
        try:
            if state=='execute':
                # Cancellation or a crash after model start never starts it again.
                async with asyncio.timeout(max(0, value.execution_deadline - self._now())):
                    value = await self.run_model(value)
                await self.database_calls(self.capture_result, job, value)
            plan_id = await self.database_calls(self.save_and_enqueue, job, value)
            return {'status':'handed_off', 'plan_id':plan_id}
        except ModelLeaseLost:
            return {'status': 'needs_attention'}
        except Exception:
            raise ContinuationDenied() from None
        finally:
            active_job_fence.reset(context)
