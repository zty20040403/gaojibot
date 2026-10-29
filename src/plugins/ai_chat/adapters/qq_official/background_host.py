"""Native M4 task source verification for the explicit official-host-v1 profile.

Metadata never grants access. Every use re-reads real task/dispatch/source/job
rows under locks, followed by the host-supplied logical route authority. Approval
may wait while the existing durable job is pending; physical operations require
its current claimed lease. Migrated tasks (no native execution source) stay denied.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import re
from typing import Protocol

from src.bot_security.store import SecurityError
from src.plugins.ai_chat.platform_types import BackgroundToolSource
from .approval_transport import FrozenSource, TaskPolicy
from .lease import assert_epoch
from .keys import account_job_kind

class RoutePort(Protocol):
    def require_current(self, cursor, *, source: FrozenSource, operation: str, now: int) -> None: ...

class DenyRoute:
    def require_current(self, *args, **kwargs):
        raise SecurityError('official_host_route_unavailable')

class NativeTaskSourcePort:
    def __init__(self, *, token, admission, route=None):
        self.token, self.admission, self.route = token, admission, route or DenyRoute()
        self._job = ContextVar('official_host_claimed_job', default=None)

    @contextmanager
    def bind_job(self, job):
        value = self._job.set(job)
        try:
            yield
        finally:
            self._job.reset(value)

    def source_for(self, cursor, *, background, route_epoch, now):
        if (not isinstance(background, BackgroundToolSource) or background.source_kind != 'subagent'
                or re.fullmatch(r'task:[1-9][0-9]{0,18}', background.source_ref) is None
                or int(background.source_ref.split(':')[1]) > 2**63-1):
            raise SecurityError('official_host_source_unavailable')
        row = cursor.execute('SELECT execution_deadline_at,route_epoch FROM qq_official_subagent_dispatches WHERE task_id=?',
                             (int(background.source_ref.split(':')[1]),)).fetchone()
        if row is None:
            raise SecurityError('official_host_source_unavailable')
        source = FrozenSource(background.endpoint, background.actor, background.scope,
            task_id=int(background.source_ref.split(':')[1]), revision=background.source_revision, route_epoch=route_epoch,
            endpoint_route_epoch=row['route_epoch'], deadline_at=row['execution_deadline_at'])
        self._check(cursor, source=source, operation='approve', now=now)
        return source

    def require_current(self, cursor, *, source, policy, operation, now):
        if not isinstance(policy, TaskPolicy):
            raise SecurityError('official_host_policy_unavailable')
        self._check(cursor, source=source, operation=operation, now=now)

    def _check(self, cursor, *, source, operation, now):
        if (not isinstance(source, FrozenSource) or source.task_id is None or source.execution_id is not None
                or operation not in {'approve','prepare','read','execute','journal'}
                or source.endpoint.account.key != self.token.account_key or source.scope != source.endpoint.scope
                or type(now) is not int or now >= source.deadline_at):
            raise SecurityError('official_host_source_unavailable')
        assert_epoch(cursor, self.token)
        endpoint = cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=? FOR UPDATE',
                                  (source.endpoint.key,)).fetchone()
        # Account/endpoint -> logical route -> job -> dispatch/control/task.
        self.route.require_current(cursor, source=source, operation='execute', now=now)
        before = cursor.execute('SELECT * FROM qq_official_subagent_dispatches WHERE task_id=?',
                                (source.task_id,)).fetchone()
        if before is None:
            raise SecurityError('official_host_source_unavailable')
        job = cursor.execute('SELECT * FROM durable_jobs WHERE job_id=? FOR SHARE', (before['job_id'],)).fetchone()
        dispatch = cursor.execute('SELECT * FROM qq_official_subagent_dispatches WHERE task_id=? FOR UPDATE',
                                  (source.task_id,)).fetchone()
        control = cursor.execute('SELECT * FROM subagent_controls WHERE task_id=? FOR UPDATE', (source.task_id,)).fetchone()
        task = cursor.execute('SELECT * FROM subagent_tasks WHERE task_id=? FOR UPDATE', (source.task_id,)).fetchone()
        if (endpoint is None or endpoint['account_key'] != self.token.account_key
                or endpoint['binding_state'] == 'revoked' or endpoint['membership_state'] != 'present'
                or (source.endpoint.kind == 'group' and endpoint['binding_state'] != 'active')
                or endpoint['route_epoch'] != source.endpoint_route_epoch
                or endpoint['generation'] != dispatch['endpoint_generation']
                or self.admission(cursor, source.endpoint, source.actor) is not True
                or dispatch['account_key'] != self.token.account_key or dispatch['endpoint_key'] != source.endpoint.key
                or dispatch['requester_actor_key'] != source.actor.key or dispatch['route_epoch'] != source.endpoint_route_epoch
                or dispatch['tool_profile'] != 'official-host-v1' or dispatch['execution_deadline_at'] != source.deadline_at
                or dispatch['source_execution_id'] is None or dispatch['job_id'] != before['job_id']
                or task is None or control is None or control['revision'] != source.revision
                or source.revision != 1 or task['requester_user_id'] is not None
                or task['account_key'] != self.token.account_key or task['endpoint_key'] != source.endpoint.key
                or task['requester_actor_key'] != source.actor.key or task['scope_key'] != source.scope.key
                or task['trigger_message_id'] != dispatch['source_canonical_message_id'] or task['project_id'] is not None
                or (operation!='journal' and (task['cancel_requested'] or task['finished_at'] is not None
                    or task['status'] not in {'received','queued','planning','running','verifying','interrupted','waiting_external'}))):
            raise SecurityError('official_host_source_unavailable')
        try:
            frozen = json.loads(control['dispatch_json'])
            payload = json.loads(job['payload_json']) if job else None
        except (ValueError,TypeError):
            raise SecurityError('official_host_job_unavailable') from None
        if type(frozen) is not dict or type(payload) is not dict:
            raise SecurityError('official_host_job_unavailable')
        if (frozen.get('tool_profile') != 'official-host-v1' or frozen.get('platform') != 'qq-official'
                or frozen.get('deadline') != source.deadline_at or job is None
                or job['kind'] != account_job_kind('subagent', self.token.account_key)
                or payload != {'task_id': source.task_id, 'revision': source.revision}
                or job['status'] not in {'pending','running'}):
            raise SecurityError('official_host_job_unavailable')
        # This is immutable producer provenance, not a requirement that the old
        # foreground execution still be running after it submitted this task.
        original = cursor.execute('SELECT * FROM qq_official_turn_executions WHERE execution_id=?',
                                  (dispatch['source_execution_id'],)).fetchone()
        if (original is None or original['execution_kind'] != 'chat'
                or original['account_key'] != self.token.account_key or original['endpoint_key'] != source.endpoint.key
                or original['requester_actor_key'] != source.actor.key or original['scope_key'] != source.scope.key
                or original['canonical_message_id'] != dispatch['source_canonical_message_id']
                or original['source_native_message_id'] != dispatch['source_native_message_id']
                or original['endpoint_generation'] != dispatch['endpoint_generation']
                or original['route_epoch'] != dispatch['route_epoch']):
            raise SecurityError('official_host_source_unavailable')
        if operation != 'approve':
            issued = self._job.get()
            if (issued is None or job['status'] != 'running' or issued.job_id != job['job_id']
                    or issued.kind != job['kind'] or issued.payload != payload
                    or issued.lease_owner != job['lease_owner'] or issued.lease_generation != job['lease_generation']
                    or issued.attempts != job['attempts'] or (job['lease_until'] or 0) <= now):
                raise SecurityError('official_host_job_unavailable')
        return dispatch
