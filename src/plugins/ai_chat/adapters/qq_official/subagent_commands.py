"""Owner-only durable task projection composed with RunningTaskRegistry.

Caller supplies the command's already-fenced cursor (life.effect_scope). No
transaction is opened here and canonical history is never an authorization key.
"""
from dataclasses import dataclass
import re

from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.agent.task_identity import StructuredTaskOwner


@dataclass(frozen=True)
class TaskView:
    task_id: str
    elapsed_seconds: int
    summary: str


def _admit(cursor, token, endpoint, actor, admission):
    StructuredTaskOwner(endpoint, actor)
    if endpoint.account.key != token.account_key:
        raise PermissionError('subagent_command_account_mismatch')
    assert_epoch(cursor, token)
    route = cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=? FOR UPDATE',
                           (endpoint.key,)).fetchone()
    if (route is None or route['binding_state'] == 'revoked'
            or (endpoint.kind == 'group' and route['binding_state'] != 'active')
            or route['membership_state'] != 'present'
            or admission(cursor, endpoint, actor) is not True):
        raise PermissionError('subagent_command_not_admitted')


def _view(row, now):
    status = row['status']
    if row.get('delivery_status'):
        status += '/' + row['delivery_status']
    if row.get('result_suppressed_reason') == 'result_needs_attachment':
        status += '/未发送：结果超过消息上限，完整结果已保留'
    return TaskView(f"task#{row['task_id']}", max(0, int((row['finished_at'] or now)-row['created_at'])),
                    f"{status} · {str(row['objective'])[:200]}")


def list_for_endpoint(cursor, *, token, endpoint, actor, now, admission, limit=20):
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('invalid_subagent_query_limit')
    _admit(cursor, token, endpoint, actor, admission)
    rows = cursor.execute('''SELECT t.*, b.suppressed_reason AS result_suppressed_reason, (SELECT CASE
            WHEN COUNT(*)=0 THEN NULL
            WHEN BOOL_OR(d.status='ambiguous') THEN 'ambiguous'
            WHEN BOOL_AND(d.status='committed') THEN 'committed'
            WHEN BOOL_OR(d.status IN ('failed','cancelled')) THEN 'failed'
            ELSE 'pending' END FROM deliveries d WHERE d.plan_id=b.plan_id) AS delivery_status
        FROM subagent_tasks t JOIN qq_official_subagent_dispatches b ON b.task_id=t.task_id
        WHERE t.account_key=? AND t.endpoint_key=? AND t.requester_actor_key=?
        ORDER BY t.updated_at DESC,t.task_id DESC LIMIT ?''',
        (token.account_key, endpoint.key, actor.key, limit)).fetchall()
    return tuple(_view(dict(row), now) for row in rows)


def cancel_for_endpoint(cursor, *, token, endpoint, actor, task_id, now, admission):
    _admit(cursor, token, endpoint, actor, admission)
    if not isinstance(task_id, str) or re.fullmatch(r'task#[1-9][0-9]{0,18}', task_id) is None:
        return None
    identifier = int(task_id[5:])
    if identifier > 9223372036854775807:
        return None
    # Read ownership before locking any different task. Account->endpoint->job
    # ->manifest->control->task matches the execution/result transaction order.
    source = cursor.execute('''SELECT b.job_id FROM qq_official_subagent_dispatches b
        JOIN subagent_tasks t ON t.task_id=b.task_id
        WHERE t.task_id=? AND t.account_key=? AND t.endpoint_key=? AND t.requester_actor_key=?''',
        (identifier, token.account_key, endpoint.key, actor.key)).fetchone()
    if source is None:
        return None
    cursor.execute('SELECT job_id FROM durable_jobs WHERE job_id=? FOR UPDATE', (source['job_id'],))
    cursor.execute('SELECT task_id FROM qq_official_subagent_dispatches WHERE task_id=? FOR UPDATE', (identifier,))
    control = cursor.execute('SELECT revision FROM subagent_controls WHERE task_id=? FOR UPDATE', (identifier,)).fetchone()
    task = cursor.execute('SELECT * FROM subagent_tasks WHERE task_id=? FOR UPDATE', (identifier,)).fetchone()
    if (task is None or control is None or control['revision'] != 1
            or task['endpoint_key'] != endpoint.key or task['requester_actor_key'] != actor.key
            or task['status'] in {'completed','partial','failed','cancelled'}):
        return None
    # This stops local continuation. It never claims an external model request
    # or a previously sent message was remotely cancelled/recalled.
    cursor.execute('''UPDATE durable_jobs SET status='cancelled',lease_owner='',lease_until=NULL,
        updated_at=?,finished_at=? WHERE job_id=? AND status IN ('pending','running')''',
        (now, now, source['job_id']))
    cursor.execute('''UPDATE subagent_tasks SET cancel_requested=TRUE,status='cancelled',
        updated_at=?,finished_at=?,last_error='cancelled_by_actual_requester' WHERE task_id=?''',
        (now, now, identifier))
    cursor.execute('''UPDATE subagent_runs SET status=CASE WHEN status='pending' THEN 'skipped' ELSE 'cancelled' END,
        last_error='cancelled_by_actual_requester',finished_at=? WHERE task_id=?
        AND status IN ('pending','running','interrupted','waiting_external')''', (now, identifier))
    cursor.execute('''UPDATE qq_official_subagent_dispatches
        SET suppressed_reason='cancelled_by_actual_requester',updated_at=?
        WHERE task_id=? AND plan_id IS NULL''', (now, identifier))
    cursor.execute('UPDATE subagent_controls SET final_queued_revision=1 WHERE task_id=?', (identifier,))
    return _view({**dict(task), 'status':'cancelled', 'finished_at':now}, now)


@dataclass
class TaskRegistryAdapter:
    running: object
    token: object
    current_cursor: object
    admission: object
    clock: object

    def list_for_endpoint(self, endpoint, actor):
        cursor = self.current_cursor()
        if cursor is None:
            raise PermissionError('subagent_command_effect_scope_required')
        durable = list_for_endpoint(cursor, token=self.token, endpoint=endpoint, actor=actor,
            now=int(self.clock()), admission=self.admission)
        active = self.running.list_for_endpoint(endpoint, actor) if self.running else ()
        return (*active, *durable)

    def cancel_for_endpoint(self, endpoint, actor, task_id=None):
        cursor = self.current_cursor()
        if cursor is None:
            raise PermissionError('subagent_command_effect_scope_required')
        _admit(cursor, self.token, endpoint, actor, self.admission)
        if task_id is None:
            # Preserve default foreground cancellation; if none exists select
            # the newest actual owner's still-active durable task.
            active = self.running.list_for_endpoint(endpoint, actor) if self.running else ()
            if active:
                return self.running.cancel_for_endpoint(endpoint, actor, active[0].task_id)
            choices = list_for_endpoint(cursor, token=self.token, endpoint=endpoint, actor=actor,
                now=int(self.clock()), admission=self.admission)
            for choice in choices:
                result = cancel_for_endpoint(cursor, token=self.token, endpoint=endpoint, actor=actor,
                    task_id=choice.task_id, now=int(self.clock()), admission=self.admission)
                if result is not None:
                    return result
            return None
        if isinstance(task_id, str) and task_id.startswith('task#'):
            return cancel_for_endpoint(cursor, token=self.token, endpoint=endpoint, actor=actor,
                task_id=task_id, now=int(self.clock()), admission=self.admission)
        return self.running.cancel_for_endpoint(endpoint, actor, task_id) if self.running else None
