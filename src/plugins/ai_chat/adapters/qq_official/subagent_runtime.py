"""Official text Sub-Agent adapter using the existing Coordinator.

No SDK, model loop, transport call, credential loading or independent queue.
Caller owns the account runtime, transaction factory and DatabaseCalls lifetime.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import time
from types import SimpleNamespace

from src.plugins.ai_chat.agent.control import JobFence, LeaseLost, active_job_fence
from src.plugins.ai_chat.agent.task_identity import object_owner, require_same_owner, StructuredTaskOwner
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch, LeaseLost as AccountLeaseLost
from .keys import account_job_kind
from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
from src.plugins.ai_chat.message_ir import MessageBody, TextNode, body_to_json, body_from_json
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from src.plugins.ai_chat.subagents import _validate_plan
from src.plugins.ai_chat.workers.durable_jobs import JobDeferred


TERMINAL = frozenset({'completed', 'partial', 'failed', 'cancelled'})
TEXT_TOOLS = frozenset({'context_search', 'context_expand', 'get_message_by_id',
    'search_messages', 'memory_list', 'web_search', 'inspect_source', 'use_skill'})
FILE_TOOLS = frozenset({'sandbox_create', 'sandbox_list', 'sandbox_destroy', 'sandbox_exec',
    'sandbox_write_file', 'sandbox_read_file', 'send_file_from_sandbox'})
FILE_ROLES = frozenset({'researcher', 'analyst', 'coder', 'document'})


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def job_kind(account):
    return account_job_kind('subagent', account.key)

def submit(cursor, *, token, source_api, source_handle, source_job, tool_call_id,
           tool_name, packet, decision, profile, now, execution_deadline,
           result_wait_seconds, admission, coordinator, jobs, tool_profile='official-text-v1'):
    row = source_api.assert_execution_current(cursor, token=token, handle=source_handle,
        job=source_job, now=now, admission=admission)
    if (row['state'] != 'executing' or not isinstance(tool_call_id, str)
            or not 1 <= len(tool_call_id.encode()) <= 256
            or any(ord(c) < 32 for c in tool_call_id)
            or tool_name not in {'delegate_agent', 'run_subagents'}
            or type(execution_deadline) is not int or not now < execution_deadline <= now + 7200
            or type(result_wait_seconds) is not int or not 1 <= result_wait_seconds <= 86400
            or tool_profile not in {'official-text-v1', 'official-files-v1'}):
        raise ValueError('invalid_subagent_submission')
    expected = StructuredTaskOwner(source_handle.endpoint, source_handle.actor)
    require_same_owner(object_owner(packet), expected)
    if (packet.scope_key != source_handle.scope_key
            or packet.trigger_message_id != source_handle.canonical_message_id
            or not packet.conversation_id or len(packet.conversation_id.encode()) > 2048):
        raise ValueError('invalid_subagent_source')
    if decision is not None:
        _validate_plan({'steps': list(decision.steps)}, packet.objective,
                       coordinator.max_steps, strict=True)
        roles = FILE_ROLES if tool_profile == 'official-files-v1' else {'researcher', 'analyst'}
        allowed = TEXT_TOOLS | FILE_TOOLS if tool_profile == 'official-files-v1' else TEXT_TOOLS
        if any(step.get('agent') not in roles or not set(step.get('required_tools', ())) <= allowed
               for step in decision.steps):
            raise ValueError('unsupported_subagent_role')
    key = _digest(['qq-subagent:v1', token.account_key, source_handle.execution_id,
                   tool_call_id, tool_name])
    request_digest = _digest([packet.as_payload(), decision.as_payload() if decision else None,
        profile, execution_deadline, result_wait_seconds, tool_profile])
    existing = cursor.execute('''SELECT task_id,submission_digest FROM qq_official_subagent_dispatches
        WHERE source_execution_id=? AND submission_key=? FOR UPDATE''',
        (source_handle.execution_id, key)).fetchone()
    if existing:
        if existing['submission_digest'] != request_digest:
            raise ValueError('subagent_submission_conflict')
        return coordinator.store._task_row(cursor.execute(
            'SELECT * FROM subagent_tasks WHERE task_id=?', (existing['task_id'],)).fetchone())
    task = coordinator.store.prepare_task(packet=packet, decision=decision,
        dispatch={'version': 1, 'platform': 'qq-official', 'deadline': execution_deadline,
                  'profile': profile, 'tool_profile': tool_profile},
        max_parallelism=coordinator.max_parallelism, max_steps=coordinator.max_steps,
        transaction_cursor=cursor)
    pending, _ = jobs.enqueue(kind=job_kind(expected.endpoint.account),
        idempotency_key='qq-subagent:' + key,
        scope_key='qq-subagent-endpoint:' + _digest(expected.endpoint.key),
        payload={'task_id': task.task_id, 'revision': 1}, now=now,
        max_attempts=5, resume_on_lease_loss=True, transaction_cursor=cursor)
    cursor.execute('''INSERT INTO qq_official_subagent_dispatches
        (task_id,account_key,endpoint_key,requester_actor_key,source_execution_id,
         source_canonical_message_id,source_native_message_id,submission_key,submission_digest,
         job_id,endpoint_generation,route_epoch,tool_profile,execution_deadline_at,
         result_wait_seconds,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (task.task_id, token.account_key, expected.endpoint.key, expected.requester_actor.key,
         source_handle.execution_id, source_handle.canonical_message_id,
         source_handle.source_native_message_id, key, request_digest, pending.job_id,
         source_handle.endpoint_generation, source_handle.route_epoch, tool_profile,
         execution_deadline, result_wait_seconds, now, now))
    return task


def assert_current(cursor, *, token, task_id, now, admission, job=None):
    try:
        assert_epoch(cursor, token)
    except AccountLeaseLost:
        raise LeaseLost('official_subagent_account_unavailable') from None
    binding = cursor.execute('SELECT * FROM qq_official_subagent_dispatches WHERE task_id=?',
                             (task_id,)).fetchone()
    if binding is None or binding['account_key'] != token.account_key:
        raise LeaseLost('official_subagent_unavailable')
    endpoint, actor = EndpointKey.parse(binding['endpoint_key']), ActorKey.parse(binding['requester_actor_key'])
    StructuredTaskOwner(endpoint, actor)
    route = cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=? FOR UPDATE',
                           (endpoint.key,)).fetchone()
    if (route is None or route['binding_state'] == 'revoked'
            or (endpoint.kind == 'group' and route['binding_state'] != 'active')
            or route['membership_state'] != 'present'
            or route['generation'] != binding['endpoint_generation']
            or route['route_epoch'] != binding['route_epoch']
            or admission(cursor, endpoint, actor) is not True):
        raise LeaseLost('official_subagent_route_unavailable')
    if job is not None:
        current = cursor.execute('SELECT * FROM durable_jobs WHERE job_id=? FOR SHARE',
                                 (job.job_id,)).fetchone()
        if (job.job_id != binding['job_id'] or job.kind != job_kind(endpoint.account)
                or job.payload != {'task_id': task_id, 'revision': 1}
                or current is None or current['status'] != 'running'
                or current['lease_owner'] != job.lease_owner
                or current['lease_generation'] != job.lease_generation
                or current['attempts'] != job.attempts or (current['lease_until'] or 0) <= now):
            raise LeaseLost('official_subagent_job_unavailable')
    binding = cursor.execute('SELECT * FROM qq_official_subagent_dispatches WHERE task_id=? FOR UPDATE',
                             (task_id,)).fetchone()
    control = cursor.execute('SELECT * FROM subagent_controls WHERE task_id=? FOR UPDATE', (task_id,)).fetchone()
    task = cursor.execute('SELECT * FROM subagent_tasks WHERE task_id=? FOR UPDATE', (task_id,)).fetchone()
    if (control is None or control['revision'] != 1 or task is None
            or task['requester_user_id'] is not None or task['account_key'] != token.account_key
            or task['endpoint_key'] != endpoint.key or task['requester_actor_key'] != actor.key
            or task['trigger_message_id'] != binding['source_canonical_message_id']
            or task['project_id'] is not None):
        raise LeaseLost('official_subagent_identity_unavailable')
    dispatch = json.loads(control['dispatch_json'])
    if (binding['tool_profile'] not in {'official-text-v1', 'official-files-v1'}
            or dispatch.get('tool_profile') != binding['tool_profile']):
        raise LeaseLost('official_subagent_tool_profile_changed')
    if job is None and task['status'] not in TERMINAL:
        raise LeaseLost('official_subagent_execution_requires_job')
    return dict(binding), dict(task)


def freeze_and_enqueue(cursor, *, token, task_id, now, outbox, admission, job=None):
    binding, task = assert_current(cursor, token=token, task_id=task_id, now=now,
                                   admission=admission, job=job)
    if task['status'] not in TERMINAL:
        raise ValueError('subagent_result_not_ready')
    if binding['plan_id']:
        return binding['plan_id']
    if binding['suppressed_reason']:
        return None
    endpoint, actor = EndpointKey.parse(binding['endpoint_key']), ActorKey.parse(binding['requester_actor_key'])
    if binding['result_body_json'] is None:
        result = json.loads(task['result_json'])
        text = str(result.get('answer') or result.get('result', {}).get('summary') or task['status'])
        from .file_projection import project_task_files
        from src.plugins.ai_chat.agent.outcomes import outcome_report
        projection_task = SimpleNamespace(task_id=task_id, endpoint=endpoint, requester_actor=actor)
        files = [view.for_task(projection_task, 1)['payload']
                 for view in project_task_files(cursor, projection_task, 1)]
        if files:
            acceptance = result.get('validation', {}).get('acceptance')
            text = outcome_report(acceptance if isinstance(acceptance, dict) else {},
                                  str(result.get('report_narrative') or ''), files)
        if task['status'] in {'failed', 'cancelled'}:
            prefix = '任务未完成。' if task['status'] == 'failed' else '任务已取消。'
            text = prefix + ('\n' + text if files else '')
        body = MessageBody((TextNode(0, f'task#{task_id}\n{text}'),))
        encoded = body_to_json(body)
        expires = now + min(binding['result_wait_seconds'], outbox.max_wait_seconds)
        cursor.execute('''UPDATE qq_official_subagent_dispatches SET result_body_json=?,
            result_digest=?,result_task_status=?,result_at=?,result_expires_at=?,updated_at=?
            WHERE task_id=? AND result_body_json IS NULL''',
            (encoded, hashlib.sha256(encoded.encode()).hexdigest(), task['status'], now, expires, now, task_id))
    else:
        encoded, expires = binding['result_body_json'], binding['result_expires_at']
        if hashlib.sha256(encoded.encode()).hexdigest() != binding['result_digest']:
            raise ValueError('subagent_result_digest_mismatch')
        body = body_from_json(encoded)
    if now >= expires:
        cursor.execute('''UPDATE qq_official_subagent_dispatches
            SET suppressed_reason='result_expired',updated_at=? WHERE task_id=?''', (now, task_id))
        return None
    rendered = render_plan(body, endpoint, allowed_mentions=frozenset())
    ref = outbox.enqueue(token, rendered=rendered, requester=actor,
        logical_key=f'qq-subagent-result:{task_id}:1', source_scope_key=task['scope_key'],
        source_native_message_id=binding['source_native_message_id'],
        source_canonical_message_id=binding['source_canonical_message_id'],
        expires_at=expires, purpose='background_result', transaction_cursor=cursor)
    cursor.execute('''UPDATE qq_official_subagent_dispatches SET plan_id=?,handed_off_at=?,updated_at=?
        WHERE task_id=? AND plan_id IS NULL''', (ref.plan_id, now, now, task_id))
    cursor.execute('UPDATE subagent_controls SET final_queued_revision=1 WHERE task_id=? AND revision=1', (task_id,))
    return ref.plan_id


def settle_authority_loss(cursor, *, token, job, now, admission, coordinator):
    """Stop a permanently obsolete task, or bound recovery by its frozen deadline.

    A stale account owner may only return a queue disposition. The existing
    worker still owns the exact job-generation CAS; no account state is changed.
    Cancelling domain state requires both current account and job authority.
    """
    try:
        assert_epoch(cursor, token)
        account_current = True
    except AccountLeaseLost:
        account_current = False
    task_id = job.payload.get('task_id')
    binding = cursor.execute('''SELECT * FROM qq_official_subagent_dispatches
        WHERE task_id=? AND job_id=? AND account_key=?''',
        (task_id, job.job_id, token.account_key)).fetchone()
    if (binding is None or job.kind != account_job_kind('subagent', token.account_key)
            or job.payload != {'task_id': task_id, 'revision': 1}):
        return {'status': 'needs_attention', 'reason': 'subagent_source_unavailable'}
    remaining = binding['execution_deadline_at'] - now

    def defer():
        if remaining <= 0:
            return {'task_id': task_id, 'status': 'needs_attention',
                    'reason': 'subagent_expired_without_authority'}
        raise JobDeferred('official_subagent_authority_unavailable', min(30, remaining))

    if not account_current:
        return defer()
    endpoint = EndpointKey.parse(binding['endpoint_key'])
    actor = ActorKey.parse(binding['requester_actor_key'])
    route = cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=? FOR UPDATE',
                           (endpoint.key,)).fetchone()
    current = cursor.execute('SELECT * FROM durable_jobs WHERE job_id=? FOR SHARE',
                             (job.job_id,)).fetchone()
    if (current is None or current['status'] != 'running'
            or current['lease_owner'] != job.lease_owner
            or current['lease_generation'] != job.lease_generation
            or current['attempts'] != job.attempts or (current['lease_until'] or 0) <= now
            or current['kind'] != job.kind or json.loads(current['payload_json']) != job.payload):
        return defer()
    binding = cursor.execute('SELECT * FROM qq_official_subagent_dispatches WHERE task_id=? FOR UPDATE',
                             (task_id,)).fetchone()
    control = cursor.execute('SELECT * FROM subagent_controls WHERE task_id=? FOR UPDATE', (task_id,)).fetchone()
    task = cursor.execute('SELECT * FROM subagent_tasks WHERE task_id=? FOR UPDATE', (task_id,)).fetchone()
    if (control is None or control['revision'] != 1 or task is None
            or task['requester_user_id'] is not None or task['account_key'] != token.account_key
            or task['endpoint_key'] != endpoint.key or task['requester_actor_key'] != actor.key
            or task['trigger_message_id'] != binding['source_canonical_message_id']
            or task['project_id'] is not None):
        return {'task_id': task_id, 'status': 'needs_attention', 'reason': 'subagent_identity_unavailable'}
    if binding['plan_id']:
        return {'task_id': task_id, 'status': 'handed_off', 'plan_id': binding['plan_id']}
    if binding['suppressed_reason']:
        return {'task_id': task_id, 'status': 'suppressed', 'reason': binding['suppressed_reason']}
    invalid_route = (route is None or route['binding_state'] == 'revoked'
        or (endpoint.kind == 'group' and route['binding_state'] != 'active')
        or route['membership_state'] != 'present'
        or route['generation'] != binding['endpoint_generation']
        or route['route_epoch'] != binding['route_epoch']
        or admission(cursor, endpoint, actor) is not True)
    reason = 'subagent_route_unavailable' if invalid_route else 'subagent_deadline'
    if not invalid_route and (remaining > 0 or task['status'] in TERMINAL):
        return defer()
    if task['status'] not in TERMINAL:
        cursor.execute('''UPDATE subagent_runs SET
            status=CASE WHEN status='pending' THEN 'skipped' ELSE 'cancelled' END,
            last_error=?,finished_at=? WHERE task_id=?
            AND status IN ('pending','running','interrupted','waiting_external')''', (reason, now, task_id))
        cursor.execute('''UPDATE subagent_tasks SET status='cancelled',cancel_requested=TRUE,
            last_error=?,finished_at=?,updated_at=? WHERE task_id=?''', (reason, now, now, task_id))
        coordinator.store.append_event(task_id, 'task.cancelled', {'error': reason},
                                       now=now, transaction_cursor=cursor)
    cursor.execute('''UPDATE qq_official_subagent_dispatches
        SET suppressed_reason=?,updated_at=? WHERE task_id=? AND plan_id IS NULL''', (reason, now, task_id))
    return {'task_id': task_id, 'status': 'suppressed', 'reason': reason}


@dataclass
class TaskToolSession:
    tools: tuple
    execute_tool: object
    hooks: object = None
    close: object = None
    files_enabled: bool = False


@dataclass
class OfficialSubAgentDispatcher:
    token: object
    coordinator: object
    jobs: object
    outbox: object
    transaction: object
    database_calls: object
    admission: object
    profile: object
    tools: tuple = ()
    execute_tool: object = None
    clock: object = time.time
    execution_factory: object = None

    async def authority_lost(self, job):
        def settle():
            with self.transaction() as cursor:
                return settle_authority_loss(cursor, token=self.token, job=job,
                    now=int(self.clock()), admission=self.admission, coordinator=self.coordinator)
        return await self.database_calls(settle)

    async def execute(self, job):
        task_id = job.payload['task_id']

        def guard(cursor):
            assert_current(cursor, token=self.token, task_id=task_id, now=int(self.clock()),
                           admission=self.admission, job=job)

        def owned():
            with self.transaction() as cursor:
                guard(cursor)
            return True

        fence = JobFence(job.job_id, job.lease_owner, job.attempts, owned,
                         job.lease_generation, guard)
        mark = active_job_fence.set(fence)
        session = None
        try:
            await self.database_calls(owned)
            task = await self.database_calls(self.coordinator.store.get, task_id)
            if task.status not in TERMINAL:
                with self.transaction() as cursor:
                    binding, _ = assert_current(cursor, token=self.token, task_id=task_id,
                        now=int(self.clock()), admission=self.admission, job=job)
                remaining = binding['execution_deadline_at'] - self.clock()
                if remaining <= 0:
                    self.coordinator.store.set_task_state(task_id, 'failed', error='subagent_deadline')
                else:
                    if task.status not in {'queued', 'interrupted'}:
                        self.coordinator.store.interrupt_task(task_id)
                    files_enabled = binding['tool_profile'] == 'official-files-v1'
                    if self.execution_factory is not None:
                        session = await self.execution_factory(job, task, files_enabled)
                        if not isinstance(session, TaskToolSession):
                            raise LeaseLost('official_task_context_invalid')
                    if files_enabled and (session is None or not session.files_enabled or session.hooks is None):
                        raise JobDeferred('official_task_files_unavailable', 30)
                    allowed = TEXT_TOOLS | FILE_TOOLS if files_enabled else TEXT_TOOLS
                    tools = tuple(t for t in (session.tools if session else self.tools)
                                  if t['function']['name'] in allowed)
                    saved_profile = self.coordinator.store.control(task_id)['dispatch'].get('profile')
                    profile = self.profile(saved_profile) if callable(self.profile) else self.profile
                    if profile is None or profile.name != saved_profile or not profile.configured:
                        raise JobDeferred('official_subagent_model_unavailable', 30)

                    async def execute_tool(name, args):
                        await self.database_calls(owned)
                        if name not in allowed or name not in {t['function']['name'] for t in tools}:
                            return '{"ok":false,"error":"tool_unavailable"}'
                        executor = session.execute_tool if session else self.execute_tool
                        if executor is None:
                            return '{"ok":false,"error":"tool_unavailable"}'
                        return await executor(name, args)

                    async with asyncio.timeout(remaining):
                        await self.coordinator.resume(task_id, scope_key=task.scope_key,
                            requester_user_id=None, endpoint=task.endpoint,
                            requester_actor=task.requester_actor, selected_profile=profile,
                            tools=tools, execute_tool=execute_tool, progress=None,
                            hooks=session.hooks if session else None)

            def finish():
                with self.transaction() as cursor:
                    return freeze_and_enqueue(cursor, token=self.token, task_id=task_id,
                        now=int(self.clock()), outbox=self.outbox, admission=self.admission, job=job)
            plan_id = await self.database_calls(finish)
            return {'task_id': task_id, 'plan_id': plan_id}
        except LeaseLost:
            return await self.authority_lost(job)
        except asyncio.CancelledError:
            raise
        except JobDeferred:
            raise
        except Exception:
            # The generic DurableJobWorker logs exception text. Keep database,
            # model and persisted-context diagnostics inside the trusted store.
            raise JobDeferred('official_subagent_unavailable', 3) from None
        finally:
            try:
                if session is not None and session.close is not None:
                    await session.close()
            finally:
                active_job_fence.reset(mark)

    async def reconcile_results(self, *, after_task_id=0, limit=20):
        if type(after_task_id) is not int or after_task_id < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid_subagent_result_page')
        def candidates():
            with self.transaction() as cursor:
                assert_epoch(cursor, self.token)
                return tuple(row['task_id'] for row in cursor.execute('''SELECT b.task_id
                    FROM qq_official_subagent_dispatches b JOIN subagent_tasks t ON t.task_id=b.task_id
                    WHERE b.account_key=? AND b.task_id>? AND b.plan_id IS NULL
                    AND b.suppressed_reason IS NULL
                    AND t.status IN ('completed','partial','failed','cancelled')
                    ORDER BY b.task_id LIMIT ?''', (self.token.account_key, after_task_id, limit)).fetchall())
        result = []
        for task_id in await self.database_calls(candidates):
            def finish():
                with self.transaction() as cursor:
                    return freeze_and_enqueue(cursor, token=self.token, task_id=task_id,
                        now=int(self.clock()), outbox=self.outbox, admission=self.admission)
            try:
                plan_id = await self.database_calls(finish)
                result.append((task_id, 'handed_off' if plan_id else 'suppressed', plan_id))
            except (LeaseLost, AccountLeaseLost):
                result.append((task_id, 'authority_unavailable', None))
            except Exception:
                result.append((task_id, 'result_unavailable', None))
        # Caller carries the last scanned task_id and wraps after a short page;
        # an unfinalizable first page cannot monopolize the scan forever.
        return tuple(result)
