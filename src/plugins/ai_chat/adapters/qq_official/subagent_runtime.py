"""Official text Sub-Agent adapter using the existing Coordinator.

No SDK, model loop, transport call, credential loading or independent queue.
Caller owns the account runtime, transaction factory and DatabaseCalls lifetime.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import logging
import traceback
import time
from types import SimpleNamespace

from src.plugins.ai_chat.agent.control import JobFence, LeaseLost, active_job_fence
from src.plugins.ai_chat.agent.task_identity import object_owner, require_same_owner, StructuredTaskOwner
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch, LeaseLost as AccountLeaseLost
from .keys import account_job_kind
HOST_TOOLS = frozenset()  # host operations were removed
from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
from src.plugins.ai_chat.message_ir import MessageBody, TextNode, body_to_json, body_from_json
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from src.plugins.ai_chat.subagents import _validate_plan
from src.plugins.ai_chat.workers.durable_jobs import JobDeferred


TERMINAL = frozenset({'completed', 'partial', 'failed', 'cancelled'})
TEXT_TOOLS = frozenset({
    'history', 'web_search', 'web_fetch', 'inspect_source', 'use_skill', 'say'})
FILE_TOOLS = frozenset({'sandbox_create', 'sandbox_list', 'sandbox_destroy', 'sandbox_exec',
    'sandbox_write_file', 'sandbox_read_file', 'send_file_from_sandbox', 'send_file'})
FILE_ROLES = frozenset({'researcher', 'analyst', 'coder', 'document'})


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def job_kind(account):
    return account_job_kind('subagent', account.key)

def submit(cursor, *, token, source_api, source_handle, source_job, tool_call_id,
           tool_name, packet, decision, profile, now, execution_deadline,
           result_wait_seconds, admission, coordinator, jobs, tool_profile='official-text-v1', host_admission=None):
    row = source_api.assert_execution_current(cursor, token=token, handle=source_handle,
        job=source_job, now=now, admission=admission)
    if (row['state'] != 'executing' or not isinstance(tool_call_id, str)
            or not 1 <= len(tool_call_id.encode()) <= 256
            or any(ord(c) < 32 for c in tool_call_id)
            or tool_name not in {'delegate_agent', 'run_subagents'}
            or type(execution_deadline) is not int or not now < execution_deadline <= now + 7200
            or type(result_wait_seconds) is not int or not 1 <= result_wait_seconds <= 86400
            or tool_profile not in {'official-text-v1', 'official-files-v1', 'official-host-v1'}):
        raise ValueError('invalid_subagent_submission')
    if tool_profile == 'official-host-v1' and (not callable(host_admission)
            or host_admission(cursor, source_handle) is not True):
        raise ValueError('official_host_profile_not_admitted')
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
        if tool_profile == 'official-host-v1':
            allowed |= HOST_TOOLS
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


def assert_current(cursor, *, token, task_id, now, admission, job=None, migration_authority=None, project_authority=None):
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
            or task['trigger_message_id'] != binding['source_canonical_message_id']):
        raise LeaseLost('official_subagent_identity_unavailable')
    dispatch = json.loads(control['dispatch_json'])
    if (binding['tool_profile'] not in {'official-text-v1', 'official-files-v1', 'official-host-v1', 'official-projects-v1'}
            or dispatch.get('tool_profile') != binding['tool_profile']):
        raise LeaseLost('official_subagent_tool_profile_changed')
    if (task['project_id'] is not None) != (binding['tool_profile'] == 'official-projects-v1'):
        raise LeaseLost('official_project_source_changed')
    if task['project_id'] is not None:
        if job is None or project_authority is None or (binding.get('migration_item_id') is not None and migration_authority is None):
            raise LeaseLost('official_project_authority_unavailable')
        try:
            accepted = project_authority(cursor, token=token, binding=dict(binding), task=dict(task), job=job, now=now)
        except (PermissionError, ValueError):
            raise LeaseLost('official_project_authority_unavailable') from None
        if accepted is not True:
            raise LeaseLost('official_project_authority_unavailable')
    if job is None and task['status'] not in TERMINAL:
        raise LeaseLost('official_subagent_execution_requires_job')
    if binding.get('migration_item_id') is not None:
        if migration_authority is None:
            raise LeaseLost('migration_authority_unavailable')
        migration_authority.assert_current(cursor, binding=dict(binding), task=dict(task), job=job, now=now)
    elif getattr(admission, 'require_execution', None) is not None:
        from .migration_types import MigrationDenied
        try:
            admission.require_execution(cursor, binding['source_execution_id'], endpoint=endpoint,
                actor=actor, operation='execute' if task['status'] not in TERMINAL else 'send')
        except MigrationDenied:
            raise LeaseLost('migration_source_unavailable') from None
    return dict(binding), dict(task)


def freeze_and_enqueue(cursor, *, token, task_id, now, outbox, admission, job=None, migration_authority=None, project_authority=None):
    binding, task = assert_current(cursor, token=token, task_id=task_id, now=now,
                                   admission=admission, job=job, migration_authority=migration_authority, project_authority=project_authority)
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
        if files and not result.get('frontend_answer'):
            acceptance = result.get('validation', {}).get('acceptance')
            text = outcome_report(acceptance if isinstance(acceptance, dict) else {},
                                  str(result.get('report_narrative') or ''), files)
        if task['status'] in {'failed', 'cancelled'} and not result.get('frontend_answer'):
            prefix = '任务未完成。' if task['status'] == 'failed' else '任务已取消。'
            text = prefix + ('\n' + text if files else '')
        body = MessageBody((TextNode(0, f'task#{task_id}\n{text}'),))
        encoded = body_to_json(body)
        expires = now + min(binding['result_wait_seconds'], outbox.max_wait_seconds)
        if binding.get('migration_item_id') is not None:
            expires = min(expires, binding['migration_result_deadline_at'])
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
    rendered = render_plan(body, endpoint, allowed_mentions=frozenset(),
                           paragraphs=True, paragraph_parts=16, max_parts=16)
    if rendered.status != 'ready':
        # Commit the complete result and its first absolute deadline even when
        # no wire plan can represent it. Retrying may not refreeze/extend it or
        # send a truncated prefix; the owner's task status exposes this result.
        cursor.execute('''UPDATE qq_official_subagent_dispatches
            SET suppressed_reason='result_needs_attachment',updated_at=? WHERE task_id=?''', (now, task_id))
        return None
    source_proof = (migration_authority.enqueue_proof(cursor, binding=binding, job=job, now=now)
        if binding.get('migration_item_id') is not None else None)
    ref = outbox.enqueue(token, rendered=rendered, requester=actor,
        logical_key=f'qq-subagent-result:{task_id}:1', source_scope_key=task['scope_key'],
        source_native_message_id=binding['source_native_message_id'] or '',
        source_canonical_message_id=binding['source_canonical_message_id'],
        expires_at=expires, purpose='background_result', transaction_cursor=cursor, source_proof=source_proof)
    cursor.execute('''UPDATE qq_official_subagent_dispatches SET plan_id=?,handed_off_at=?,updated_at=?
        WHERE task_id=? AND plan_id IS NULL''', (ref.plan_id, now, now, task_id))
    cursor.execute('UPDATE subagent_controls SET final_queued_revision=1 WHERE task_id=? AND revision=1', (task_id,))
    if binding.get('migration_item_id') is not None:
        migration_authority.bind_plan(cursor, binding=binding, plan_id=ref.plan_id, job=job, now=now)
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
    host_enabled: bool = False
    project_enabled: bool = False
    progress: object = None


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
    migration_authority: object = None
    project_finalizer: object = None
    project_authority: object = None
    present_result: object = None

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
                           admission=self.admission, job=job, migration_authority=self.migration_authority, project_authority=self.project_authority)

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
            if self.migration_authority is not None:
                def begin_migration():
                    with self.transaction() as cursor:
                        return self.migration_authority.begin_job(cursor, job=job, now=int(self.clock()))
                if await self.database_calls(begin_migration) is False:
                    return {'task_id': task_id, 'status': 'needs_attention', 'reason': 'migration_execution_unknown'}
            task = await self.database_calls(self.coordinator.store.get, task_id)
            if task.status not in TERMINAL:
                with self.transaction() as cursor:
                    binding, _ = assert_current(cursor, token=self.token, task_id=task_id,
                        now=int(self.clock()), admission=self.admission, job=job, migration_authority=self.migration_authority, project_authority=self.project_authority)
                remaining = binding['execution_deadline_at'] - self.clock()
                if remaining <= 0:
                    self.coordinator.store.set_task_state(task_id, 'failed', error='subagent_deadline')
                else:
                    if task.status not in {'queued', 'interrupted'}:
                        self.coordinator.store.interrupt_task(task_id)
                    project_enabled = binding['tool_profile'] == 'official-projects-v1'
                    files_enabled = binding['tool_profile'] in {'official-files-v1','official-projects-v1'}
                    if self.execution_factory is not None:
                        session = await self.execution_factory(job, task, files_enabled)
                        if not isinstance(session, TaskToolSession):
                            raise LeaseLost('official_task_context_invalid')
                    if files_enabled and (session is None or not session.files_enabled or session.hooks is None):
                        raise JobDeferred('official_task_files_unavailable', 30)
                    if project_enabled and (session is None or not session.project_enabled
                            or session.hooks.project_inputs is None or self.project_finalizer is None):
                        raise JobDeferred('official_task_projects_unavailable', 30)
                    host_enabled = binding['tool_profile'] == 'official-host-v1'
                    if host_enabled and (session is None or not session.host_enabled):
                        raise JobDeferred('official_task_host_unavailable', 3)
                    allowed = TEXT_TOOLS | FILE_TOOLS if files_enabled else TEXT_TOOLS
                    if host_enabled:
                        allowed |= HOST_TOOLS
                    progress = session.progress if session is not None else None
                    session_tools = session.tools if session is not None else self.tools
                    if progress is not None:
                        from ...ai_tools import SAY_TOOL
                        session_tools = (*[t for t in session_tools if t['function']['name'] != 'say'], SAY_TOOL)
                    tools = tuple(t for t in session_tools
                                  if t['function']['name'] in allowed)
                    saved_profile = self.coordinator.store.control(task_id)['dispatch'].get('profile')
                    profile = self.profile(saved_profile) if callable(self.profile) else self.profile
                    if profile is None or profile.name != saved_profile or not profile.configured:
                        raise JobDeferred('official_subagent_model_unavailable', 30)

                    async def execute_tool(name, args):
                        await self.database_calls(owned)
                        if name not in allowed or name not in {t['function']['name'] for t in tools}:
                            return '{"ok":false,"error":"tool_unavailable"}'
                        if name == "say":
                            if progress is None:
                                return '{"ok":false,"status":"suppressed","reason":"progress_unavailable"}'
                            return json.dumps(await progress.say(args.get("text")), ensure_ascii=False)
                        executor = session.execute_tool if session else self.execute_tool
                        if executor is None:
                            return '{"ok":false,"error":"tool_unavailable"}'
                        return await executor(name, args)

                    try:
                        async with asyncio.timeout(remaining):
                            await self.coordinator.resume(task_id, scope_key=task.scope_key,
                                requester_user_id=None, endpoint=task.endpoint,
                                requester_actor=task.requester_actor, selected_profile=profile,
                                tools=tools, execute_tool=execute_tool, progress=progress.report if progress is not None else None,
                                hooks=session.hooks if session else None)
                    except TimeoutError:
                        self.coordinator.store.set_task_state(task_id, 'failed', error='任务超时，已保留完成的工作。')


            if (self.coordinator.store.get(task_id).status == 'waiting_external'
                    and self.coordinator.store.control(task_id)['dispatch'].get('tool_profile') == 'official-host-v1'):
                raise JobDeferred('official_host_operation_pending', 3)

            if task.project_id is not None:
                terminal = self.coordinator.store.get(task_id)
                if terminal.status not in {'failed', 'cancelled'}:
                    if self.project_finalizer is None:
                        raise JobDeferred('official_task_projects_unavailable', 30)
                    if await self.project_finalizer(job, terminal) is False:
                        self.coordinator.store.set_task_state(task_id, 'partial',
                            result={**terminal.result, 'delivery_note': '项目保存未完成；请明确告知用户，已生成的结果仍可查看。'})

            if self.present_result is not None:
                await self.present_result(job, self.coordinator.store.get(task_id), session)

            def finish():
                with self.transaction() as cursor:
                    plan_id = freeze_and_enqueue(cursor, token=self.token, task_id=task_id,
                        now=int(self.clock()), outbox=self.outbox, admission=self.admission, job=job, migration_authority=self.migration_authority, project_authority=self.project_authority)
                    result = {'task_id': task_id, 'plan_id': plan_id}
                    if plan_id is None:
                        row = cursor.execute('''SELECT suppressed_reason FROM qq_official_subagent_dispatches
                            WHERE task_id=?''', (task_id,)).fetchone()
                        if row['suppressed_reason'] == 'result_needs_attachment':
                            result.update(delivery_status='not_sent', reason='result_needs_attachment')
                    return result
            return await self.database_calls(finish)
        except LeaseLost:
            return await self.authority_lost(job)
        except asyncio.CancelledError:
            raise
        except JobDeferred:
            raise
        except Exception as error:
            logging.getLogger(__name__).warning('subagent error=%s frames=%s', type(error).__name__,
                [(frame.name, frame.lineno) for frame in traceback.extract_tb(error.__traceback__)[-4:]])
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
                    AND t.status IN ('completed','partial','failed','cancelled') AND t.project_id IS NULL
                    ORDER BY b.task_id LIMIT ?''', (self.token.account_key, after_task_id, limit)).fetchall())
        result = []
        for task_id in await self.database_calls(candidates):
            if self.present_result is not None:
                # Requeue the existing durable task instead of bypassing the foreground.
                def requeue():
                    with self.transaction() as cursor:
                        row = cursor.execute('SELECT job_id FROM qq_official_subagent_dispatches WHERE task_id=?', (task_id,)).fetchone()
                        cursor.execute("UPDATE durable_jobs SET status='pending', attempts=0, next_attempt_at=?, lease_owner='', lease_until=NULL, finished_at=NULL WHERE job_id=? AND status IN ('failed','succeeded')", (int(self.clock()), row['job_id']))
                await self.database_calls(requeue)
                result.append((task_id, 'queued', None))
                continue
            def finish():
                with self.transaction() as cursor:
                    return freeze_and_enqueue(cursor, token=self.token, task_id=task_id,
                        now=int(self.clock()), outbox=self.outbox, admission=self.admission, migration_authority=self.migration_authority)
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
