"""Select the existing task authority from its persisted migration source.

This is host composition, not a grant or a queue. Selection never uses a model
profile, a caller's claimed item kind, or a newly observed route epoch. Each
selected domain still performs its full current source/job/permission checks.
"""
from src.bot_storage.database import StoreRow
from src.plugins.ai_chat.agent.control import LeaseLost
from src.plugins.ai_chat.storage.jobs import DurableJob

from .keys import account_job_kind
from .migration_subagents import ConversionDenied, SubAgentContinuations
from .migration_types import MigrationDenied


_BINDING = ('task_id', 'account_key', 'endpoint_key', 'requester_actor_key',
    'job_id', 'migration_item_id', 'source_execution_id',
    'source_canonical_message_id', 'source_native_message_id',
    'endpoint_generation', 'route_epoch', 'execution_deadline_at',
    'migration_result_deadline_at', 'tool_profile')
_TASK = ('task_id', 'account_key', 'endpoint_key', 'requester_actor_key',
    'migration_item_id', 'project_id', 'scope_key', 'status')


def _deny():
    raise LeaseLost('migration_task_source_unavailable')


def _same(supplied, current, keys):
    if type(supplied) not in (dict, StoreRow) or any(
            supplied.get(key) != current[key] for key in keys):
        _deny()


class MigrationTaskSources:
    def __init__(self, *, token, domains, journal):
        self.token, self.domains, self.journal = token, domains, journal

    def _select(self, cursor, task_id, *, native=False):
        if type(task_id) is not int or task_id <= 0:
            _deny()
        binding = cursor.execute('SELECT * FROM qq_official_subagent_dispatches WHERE task_id=?',
            (task_id,)).fetchone()
        task = cursor.execute('SELECT * FROM subagent_tasks WHERE task_id=?', (task_id,)).fetchone()
        if (binding is None or task is None or binding['account_key'] != self.token.account_key
                or task['account_key'] != self.token.account_key
                or task['endpoint_key'] != binding['endpoint_key']
                or task['requester_actor_key'] != binding['requester_actor_key']
                or task['migration_item_id'] != binding['migration_item_id']):
            _deny()
        if binding['migration_item_id'] is None:
            if native:
                return None, dict(binding), dict(task)
            _deny()
        source = cursor.execute('''SELECT c.*, i.kind AS source_kind,
            i.job_id AS item_job_id, i.target_actor_key AS item_actor_key,
            r.target_account_key AS route_account_key, r.target_endpoint_key AS route_endpoint_key
            FROM migration_subagent_continuations c
            JOIN migration_items i USING(item_id)
            JOIN migration_journals j USING(migration_id)
            JOIN migration_routes r USING(route_id)
            WHERE c.target_task_id=?''', (task_id,)).fetchone()
        if (source is None or source['item_id'] != binding['migration_item_id']
                or source['source_kind'] not in {'subagent', 'project_task'}
                or source['account_key'] != self.token.account_key
                or source['route_account_key'] != self.token.account_key
                or source['endpoint_key'] != binding['endpoint_key']
                or source['route_endpoint_key'] != binding['endpoint_key']
                or source['actor_key'] != binding['requester_actor_key']
                or source['item_actor_key'] != binding['requester_actor_key']
                or source['job_id'] != binding['job_id']
                or source['item_job_id'] != binding['job_id']
                or (source['source_kind'] == 'project_task') != (task['project_id'] is not None)):
            _deny()
        domain = self.domains.get(source['source_kind'])
        if (not isinstance(domain, SubAgentContinuations) or domain.token is not self.token
                or domain.journal is not self.journal
                or getattr(domain, 'domain_kind', 'subagent') != source['source_kind']):
            _deny()
        return domain, dict(binding), dict(task)

    def _job(self, job, binding):
        if (type(job) is not DurableJob or job.job_id != binding['job_id']
                or job.kind != account_job_kind('subagent', self.token.account_key)
                or job.payload != {'task_id': binding['task_id'], 'revision': 1}):
            _deny()

    def begin_job(self, cursor, *, job, now):
        if type(job) is not DurableJob or type(job.payload) is not dict:
            _deny()
        domain, binding, _ = self._select(cursor, job.payload.get('task_id'), native=True)
        self._job(job, binding)
        return domain.begin_job(cursor, job=job, now=now) if domain is not None else None

    def assert_current(self, cursor, *, binding, task, job, now):
        if type(task) not in (dict, StoreRow):
            _deny()
        domain, actual, record = self._select(cursor, task.get('task_id'))
        _same(binding, actual, _BINDING)
        _same(task, record, _TASK)
        if job is not None:
            self._job(job, actual)
        return domain.assert_current(cursor, binding=actual, task=record, job=job, now=now)

    def tool_source(self, cursor, *, task_id, job, now):
        domain, binding, _ = self._select(cursor, task_id)
        self._job(job, binding)
        return domain.tool_source(cursor, task_id=task_id, job=job, now=now)

    def _from_binding(self, cursor, binding, job):
        if type(binding) not in (dict, StoreRow):
            _deny()
        domain, actual, _ = self._select(cursor, binding.get('task_id'))
        _same(binding, actual, _BINDING)
        if job is not None:
            self._job(job, actual)
        return domain, actual

    def enqueue_proof(self, cursor, *, binding, job, now):
        domain, actual = self._from_binding(cursor, binding, job)
        return domain.enqueue_proof(cursor, binding=actual, job=job, now=now)

    def bind_plan(self, cursor, *, binding, plan_id, job, now):
        domain, actual = self._from_binding(cursor, binding, job)
        return domain.bind_plan(cursor, binding=actual, plan_id=plan_id, job=job, now=now)

    def current_plan_source(self, cursor, plan_id, now):
        source = cursor.execute('SELECT target_task_id FROM migration_subagent_continuations WHERE plan_id=?',
            (plan_id,)).fetchone()
        if source is None:
            return False
        try:
            domain, binding, _ = self._select(cursor, source['target_task_id'])
            if binding['plan_id'] != plan_id:
                _deny()
            return domain.current_plan_source(cursor, plan_id, now)
        except (ConversionDenied, LeaseLost):
            # A revoked task cancels its plan. Account lease loss and database
            # failures have distinct types and must still stop the sender.
            raise MigrationDenied('migration_task_source_unavailable') from None
