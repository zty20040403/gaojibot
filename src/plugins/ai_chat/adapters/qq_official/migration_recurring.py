"""Bounded extension of existing agent_routines materialization, not a queue.

One approved parent schedule owns existing routine row and finite child items.
No catch-up of pre-cutover/unknown source occurrences. cron_time reuses the
existing timezone/latest-missed policy. Every child gets its original-slot-based
absolute deadlines, existing routine_runs UNIQUE and existing durable job.
"""
from dataclasses import asdict
import json
import uuid

from .migration_continuation import Continuations, ContinuationDenied, PARKED, row, number, _digest
from .migration_preview import Selection, SourceRef
from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
from src.plugins.ai_chat.routines import Routine, cron_time


def schedule_of(proof):
    try:
        schedule = json.loads(proof.schedule_json)
    except (TypeError, ValueError):
        raise ContinuationDenied() from None
    expected = {'cron','timezone','version','cutover','schedule_until','max_occurrences',
        'execution_seconds','result_seconds','capability_grant'}
    if not isinstance(schedule,dict) or set(schedule)!=expected:
        raise ContinuationDenied()
    for name in expected-{'cron','timezone','capability_grant'}:
        number(schedule[name])
    if (not 0 < schedule['execution_seconds'] <= schedule['result_seconds'] <= 86400
            or not 1 <= schedule['max_occurrences'] <= 10000
            or schedule['version']<1 or schedule['cutover']>=schedule['schedule_until']
            or schedule['capability_grant']!={'profile':'official-scheduled-text-v1','tools':[]}):
        raise ContinuationDenied()
    cron_time(schedule['cron'],schedule['timezone'],schedule['cutover'])
    return schedule


def schedule_digest(schedule):
    return _digest(json.dumps(schedule,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False))


class ScheduledContinuations(Continuations):
    def prepare(self,cursor,proof,*,now):
        if proof.source_kind!='routine' or not proof.source_key.startswith('spec:'):
            return super().prepare(cursor,proof,now=now)
        self._proof(cursor,proof,'prepare',now)
        existing=row(cursor,'SELECT * FROM qq_official_schedule_bindings WHERE item_id=? FOR UPDATE',(proof.item_id,))
        if existing:
            self._same(existing,proof)
            return existing
        schedule=schedule_of(proof)
        original=row(cursor,'SELECT * FROM agent_routines WHERE routine_id=? FOR UPDATE',(int(proof.source_key[5:]),))
        source=SourceRef('routine',proof.source_key)
        exported=self.preview.preview(cursor,Selection(proof.source_endpoint,proof.source_scope,proof.target_endpoint),source,now=now)
        if (original is None or original['project_id'] is not None or exported.disposition!='unsent'
                or exported.safe_digest!=proof.export_digest or exported.export['type']!='routine_schedule'
                or schedule['version']!=original['version'] or schedule['cron']!=original['cron']
                or schedule['timezone']!=original['timezone']
                or proof.result_deadline!=schedule['schedule_until'] or proof.execution_deadline!=schedule['schedule_until']):
            raise ContinuationDenied()
        endpoint,actor=proof.target_endpoint,proof.target_actor
        # Old occurrences retain their own records/evidence. Disabling this old
        # spec stops later materialization, not historical receipt settlement.
        cursor.execute("UPDATE agent_routines SET enabled=FALSE,state='paused',updated_at=? WHERE routine_id=?",(now,original['routine_id']))
        identity=(endpoint.account.key,endpoint.key,actor.key,proof.endpoint_generation,proof.endpoint_route_epoch)
        cursor.execute('''INSERT INTO agent_routines
            (scope_key,platform,kind,conversation_id,bot_id,creator_id,name,prompt,cron,timezone,
             enabled,version,max_tokens,daily_tokens,daily_runs,next_due_at,created_at,updated_at,
             account_key,endpoint_key,creator_actor_key,endpoint_generation,endpoint_route_epoch,budget_origin_routine_id)
            VALUES (?,'qq-official',?,?,?,?,?,?,?,?,TRUE,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING routine_id''',
            (endpoint.scope.key,endpoint.kind,endpoint.native_conversation_id,self._bot_native_id(cursor,endpoint),actor.native_user_id,
             original['name'],original['prompt'],original['cron'],original['timezone'],original['version'],
             original['max_tokens'],original['daily_tokens'],original['daily_runs'],original['next_due_at'],now,now,*identity,
             original['budget_origin_routine_id'] or original['routine_id']))
        routine_id=cursor.fetchone()[0]
        origin=row(cursor,'SELECT budget_origin_routine_id FROM agent_routines WHERE routine_id=?',
            (original['budget_origin_routine_id'] or original['routine_id'],))
        if origin is None or origin['budget_origin_routine_id'] is not None:
            raise ContinuationDenied()
        binding_id=uuid.uuid4().hex
        cursor.execute('''INSERT INTO qq_official_schedule_bindings
            (binding_id,item_id,item_revision,export_digest,source_kind,source_key,source_revision,source_scope_key,
             source_canonical_message_id,source_native_message_id,target_routine_id,account_key,endpoint_key,actor_key,
             logical_route_epoch,endpoint_generation,endpoint_route_epoch,due_at,execution_deadline,result_deadline,created_at)
            VALUES (?,?,?,?,'routine',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (binding_id,proof.item_id,proof.item_revision,proof.export_digest,proof.source_key,proof.source_revision,
             proof.source_scope.key,proof.source_canonical_message_id,proof.source_native_message_id,routine_id,
             endpoint.account.key,endpoint.key,actor.key,proof.logical_route_epoch,proof.endpoint_generation,
             proof.endpoint_route_epoch,original['next_due_at'],proof.execution_deadline,proof.result_deadline,now))
        self.journal.bind_schedule(cursor,proof,target_routine_id=routine_id,now=now)
        return row(cursor,'SELECT * FROM qq_official_schedule_bindings WHERE binding_id=?',(binding_id,))

    def activate(self,cursor,proof,*,now):
        if proof.schedule_json is None:
            return super().activate(cursor,proof,now=now)
        self._proof(cursor,proof,'activate',now)
        binding=row(cursor,'SELECT * FROM qq_official_schedule_bindings WHERE item_id=? FOR UPDATE',(proof.item_id,))
        if binding is None or binding['job_id'] is not None:
            raise ContinuationDenied()
        self._same(binding,proof)
        self.journal.activate_schedule(cursor,proof,now=now)
        cursor.execute('UPDATE qq_official_schedule_bindings SET activated_at=COALESCE(activated_at,?) WHERE binding_id=?',(now,binding['binding_id']))
        return binding

    def validate_bound_schedule(self,cursor,item,target_routine_id,*,operation,now):
        item_id=item.item_id if hasattr(item,'item_id') else item['item_id']
        binding=row(cursor,'SELECT * FROM qq_official_schedule_bindings WHERE item_id=? AND target_routine_id=? AND target_run_id IS NULL AND job_id IS NULL',(item_id,target_routine_id))
        if not binding:
            raise ContinuationDenied()
        spec=row(cursor,'SELECT * FROM agent_routines WHERE routine_id=?',(target_routine_id,))
        if not spec or spec['account_key']!=binding['account_key'] or spec['endpoint_key']!=binding['endpoint_key'] or spec['creator_actor_key']!=binding['actor_key']:
            raise ContinuationDenied()

    def validate_occurrence(self,cursor,parent,*,run_id,scheduled_at,job_id,now):
        schedule=schedule_of(parent)
        binding=row(cursor,'SELECT * FROM qq_official_schedule_bindings WHERE item_id=?',(parent.item_id,))
        if binding is None or binding['target_run_id'] is not None or binding['activated_at'] is None:
            raise ContinuationDenied()
        self._same(binding,parent)
        spec=row(cursor,'SELECT * FROM agent_routines WHERE routine_id=?',(binding['target_routine_id'],))
        original=row(cursor,'SELECT * FROM agent_routines WHERE routine_id=?',(int(parent.source_key[5:]),))
        run=row(cursor,'SELECT * FROM routine_runs WHERE run_id=?',(run_id,))
        job=row(cursor,'SELECT * FROM durable_jobs WHERE job_id=?',(job_id,))
        expected_kind=account_job_kind('routine',parent.target_endpoint.account.key)
        # Root source version remains frozen, not mutable current task prompt.
        if (not spec or not original or original['enabled'] or original['version']!=schedule['version']
                or original['cron']!=schedule['cron'] or original['timezone']!=schedule['timezone']
                or not spec['enabled'] or spec['state']!='active' or spec['version']!=schedule['version']
                or (spec['cron'],spec['timezone'],spec['prompt'],spec['max_tokens'],spec['daily_tokens'],spec['daily_runs'])
                  !=(original['cron'],original['timezone'],original['prompt'],original['max_tokens'],original['daily_tokens'],original['daily_runs'])
                or not run or run['routine_id']!=spec['routine_id'] or run['version']!=schedule['version']
                or run['scheduled_at']!=scheduled_at or run['job_id']!=job_id or run['status']!='queued'
                or not job or job['kind']!=expected_kind or job['status']!='pending' or job['attempts']!=0
                or job['next_attempt_at']!=PARKED
                or not schedule['cutover']<scheduled_at<schedule['schedule_until']
                or cron_time(schedule['cron'],schedule['timezone'],scheduled_at-1)!=scheduled_at
                or run['deadline_at']!=min(scheduled_at+schedule['execution_seconds'],schedule['schedule_until'])):
            raise ContinuationDenied()
        if row(cursor,'SELECT 1 FROM routine_runs WHERE routine_id=? AND scheduled_at=?',(original['routine_id'],scheduled_at)):
            # Even an old accepted result must not be duplicated. Old unsent
            # occurrences require their own migration item, not schedule replay.
            raise ContinuationDenied()
        return dict(run_id=run_id,scheduled_at=scheduled_at,job_id=job_id,
            expected_kind=expected_kind,schedule_digest=schedule_digest(schedule))

    def materialize_due(self,cursor,parent,*,now):
        self._proof(cursor,parent,'activate',now)
        schedule=schedule_of(parent)
        binding=row(cursor,'SELECT * FROM qq_official_schedule_bindings WHERE item_id=? FOR UPDATE',(parent.item_id,))
        if not binding or binding['activated_at'] is None:
            raise ContinuationDenied()
        self._same(binding,parent)
        spec=row(cursor,'SELECT * FROM agent_routines WHERE routine_id=? FOR UPDATE',(binding['target_routine_id'],))
        if not spec or not spec['enabled'] or spec['state']!='active' or spec['version']!=schedule['version']:
            raise ContinuationDenied()
        if now>=schedule['schedule_until']:
            return None
        if spec['next_due_at']>now:
            return None
        due=max(spec['next_due_at'],cron_time(spec['cron'],spec['timezone'],now+1,previous=True),
            cron_time(spec['cron'],spec['timezone'],schedule['cutover']))
        next_due=cron_time(spec['cron'],spec['timezone'],now)
        cursor.execute('UPDATE agent_routines SET next_due_at=?,updated_at=? WHERE routine_id=?',(next_due,now,spec['routine_id']))
        if due>now or due>=schedule['schedule_until'] or min(due+schedule['execution_seconds'],schedule['schedule_until'])<=now:
            return None
        if row(cursor,"SELECT 1 FROM routine_runs WHERE routine_id=? AND status IN ('queued','running','generated')",(spec['routine_id'],)):
            return None
        if row(cursor,'SELECT 1 FROM routine_runs WHERE routine_id=? AND scheduled_at=?',(int(parent.source_key[5:]),due)):
            return None
        count=cursor.execute('SELECT COUNT(*) FROM qq_official_schedule_bindings WHERE parent_binding_id=?',(binding['binding_id'],)).fetchone()[0]
        if count>=schedule['max_occurrences']:
            return None
        run_id=uuid.uuid4().hex
        execution_deadline=min(due+schedule['execution_seconds'],schedule['schedule_until'])
        result_deadline=min(due+schedule['result_seconds'],schedule['schedule_until'])
        cursor.execute('''INSERT INTO routine_runs
            (run_id,routine_id,version,scheduled_at,missed_from,spec_json,status,deadline_at,created_at,updated_at)
            VALUES (?,?,?,?,?,?,'queued',?,?,?) ON CONFLICT(routine_id,version,scheduled_at) DO NOTHING''',
            (run_id,spec['routine_id'],spec['version'],due,spec['next_due_at'],json.dumps(asdict(Routine(**spec)),ensure_ascii=False),execution_deadline,now,now))
        if cursor.rowcount!=1:
            return None
        child_binding=uuid.uuid4().hex
        job,_=self.jobs.enqueue(kind=account_job_kind('routine',parent.target_endpoint.account.key),
            idempotency_key='qq-migration-occurrence:'+_digest(parent.item_id+':'+str(due)),
            payload={'binding_id':child_binding},scope_key=parent.target_endpoint.scope.key,
            resume_on_lease_loss=True,run_at=PARKED,now=now,transaction_cursor=cursor)
        cursor.execute('UPDATE routine_runs SET job_id=? WHERE run_id=?',(job.job_id,run_id))
        child=self.journal.add_occurrence(cursor,parent,run_id=run_id,scheduled_at=due,job_id=job.job_id,
            expected_kind=job.kind,execution_deadline=execution_deadline,result_deadline=result_deadline,now=now)
        cursor.execute('''INSERT INTO qq_official_schedule_bindings
            (binding_id,item_id,item_revision,export_digest,parent_binding_id,source_kind,source_key,source_revision,
             source_scope_key,target_routine_id,target_run_id,account_key,endpoint_key,actor_key,logical_route_epoch,
             endpoint_generation,endpoint_route_epoch,due_at,execution_deadline,result_deadline,job_id,created_at)
            VALUES (?,?,?,?,?,'routine',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (child_binding,child.item_id,child.item_revision,child.export_digest,binding['binding_id'],child.source_key,
             child.source_revision,child.source_scope.key,spec['routine_id'],run_id,child.target_endpoint.account.key,
             child.target_endpoint.key,child.target_actor.key,child.logical_route_epoch,child.endpoint_generation,
             child.endpoint_route_epoch,due,execution_deadline,result_deadline,job.job_id,now))
        super().activate(cursor,child,now=now)
        return row(cursor,'SELECT * FROM qq_official_schedule_bindings WHERE binding_id=?',(child_binding,))
