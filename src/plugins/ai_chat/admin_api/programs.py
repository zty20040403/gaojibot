"""Audited program policy; guest work is reconciled only after the DB commit."""
import asyncio
from contextlib import contextmanager
import time
import json

from fastapi import Depends, HTTPException, Query

from ..admin_control import AdminVersionConflict
from ..background_tools import workspace_owner
from ..sandbox import SandboxError
from ..adapters.qq_official.program_status import ProgramStatus
from ..platform_types import EndpointKey
from ..message_ir import body_from_json, render_fallback_text
from .context import authorize, mutation_context, mutation_payload
from .contracts import AdminMutationContext, ProgramControlRequest, ProgramSettingsRequest, ProgramResumeRequest


def parse_endpoint(value):
    try:
        endpoint = EndpointKey.parse(value)
        if endpoint.account.platform!='qq-official':
            raise ValueError()
        return endpoint
    except (TypeError,ValueError):
        raise HTTPException(404,'找不到该 QQ 会话') from None


def existing(cursor, endpoint):
    row = cursor.execute('SELECT endpoint_key FROM qq_official_endpoints WHERE endpoint_key=?',(endpoint.key,)).fetchone()
    if row is None:
        raise HTTPException(404,'找不到该 QQ 会话')


def policy(cursor, endpoint):
    row = cursor.execute('''SELECT programs_enabled,program_say_limit,program_wake_limit
        FROM conversation_settings WHERE scope_key=?''',(endpoint.scope.key,)).fetchone()
    return dict(row) if row else dict(programs_enabled=True,program_say_limit=60,program_wake_limit=6)


def create_router(context):
    router,services = context.new_router(),context.services

    @contextmanager
    def transaction():
        if services.database is None:
            raise HTTPException(503,'程序状态未配置')
        connection = services.database.store_connection()
        cursor = connection.cursor()
        try:
            yield cursor
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            cursor.close(); connection.close()

    manager = services.sandbox_manager if getattr(services.sandbox_manager,'backend',None)=='vm' else None
    reader = ProgramStatus(transaction=transaction,manager=manager,calls=asyncio.to_thread,clock=time.time,route=existing)

    @router.get('/api/programs')
    async def programs(endpoint_key: str = Query('',max_length=1000)):
        authorize()
        if services.database is None:
            return context.versioned('programs',dict(available=False,items=[],snapshot=None))
        def listing():
            with transaction() as cursor:
                rows = cursor.execute('''SELECT endpoint_key,kind,binding_state,membership_state
                    FROM qq_official_endpoints ORDER BY endpoint_key LIMIT 500''').fetchall()
                version = cursor.execute("SELECT version FROM admin_resource_versions WHERE resource_key='programs'").fetchone()
                return [dict(row) for row in rows],version['version'] if version else 0
        items,version = await asyncio.to_thread(listing)
        snapshot = None
        if endpoint_key:
            endpoint = parse_endpoint(endpoint_key)
            def checked(cursor):
                authorize()
                current = cursor.execute("SELECT version FROM admin_resource_versions WHERE resource_key='programs'").fetchone()
                if (current['version'] if current else 0)!=version:
                    raise HTTPException(409,'程序配置已改变，请刷新')
            snapshot = await reader.snapshot(endpoint,authorize=checked,include_recovery=True)
        return dict(available=True,vm_available=manager is not None,items=items,snapshot=snapshot,
            api_version='v1',resource='programs',resource_version=version)

    @router.get('/api/programs/messages')
    def messages(endpoint_key: str = Query(...,max_length=1000),program_name: str = Query('',max_length=80),
                 before: int = Query(0,ge=0,le=2**63-1),limit: int = Query(50,ge=1,le=100)):
        authorize()
        endpoint = parse_endpoint(endpoint_key)
        with transaction() as cursor:
            existing(cursor,endpoint)
            rows = cursor.execute('''SELECT m.canonical_message_id,m.occurred_at,m.body_json,m.raw_event_json
                FROM messages m JOIN conversations c USING(conversation_id)
                JOIN conversation_visibility v USING(conversation_id)
                WHERE c.scope_key=? AND m.source_endpoint_key=? AND m.direction='outbound' AND m.message_kind='chat'
                    AND m.canonical_message_id>=v.min_canonical_message_id AND (?=0 OR m.canonical_message_id<?)
                    AND m.raw_event_json::jsonb->>'by'='program'
                    AND (?='' OR m.raw_event_json::jsonb->>'program'=? OR jsonb_exists(m.raw_event_json::jsonb->'programs',?))
                ORDER BY m.canonical_message_id DESC LIMIT ?''',
                (endpoint.scope.key,endpoint.key,before,before,program_name,program_name,program_name,limit+1)).fetchall()
        items = []
        for row in rows[:limit]:
            raw = json.loads(row['raw_event_json'])
            items.append(dict(id=row['canonical_message_id'],at=row['occurred_at'],program=raw.get('program',''),
                programs=raw.get('programs',[]),text=render_fallback_text(body_from_json(row['body_json']))[:8000]))
        return dict(items=items,next_cursor=items[-1]['id'] if len(rows)>limit else None)

    def mutate(selection, info, *, program=False, resume=None):
        authorize()
        if info.expected_version is None:
            raise HTTPException(428,'请刷新程序状态并提供 If-Match')
        endpoint = parse_endpoint(selection.endpoint_key)
        try:
            with transaction() as cursor:
                cursor.execute('SELECT account_key FROM qq_official_accounts WHERE account_key=? FOR UPDATE',(endpoint.account.key,))
                existing(cursor,endpoint)
                def operation():
                    if resume is not None:
                        return dict(recovery_fence=selection.recovery_fence),resume.enqueue(cursor,endpoint,
                            selection.sandbox_id,selection.recovery_fence)
                    if program:
                        previous = cursor.execute('SELECT enabled FROM program_controls WHERE scope_key=? AND program_name=?',
                            (endpoint.scope.key,selection.program_name)).fetchone()
                        if selection.enabled:
                            cursor.execute('DELETE FROM program_controls WHERE scope_key=? AND program_name=?',
                                (endpoint.scope.key,selection.program_name))
                        else:
                            if previous is None and cursor.execute('SELECT COUNT(*) AS n FROM program_controls WHERE scope_key=?',
                                    (endpoint.scope.key,)).fetchone()['n']>=256:
                                raise HTTPException(409,'程序控制项已达上限')
                            cursor.execute('''INSERT INTO program_controls(scope_key,program_name,enabled,updated_at) VALUES (?,?,FALSE,?)
                                ON CONFLICT(scope_key,program_name) DO UPDATE SET enabled=FALSE,updated_at=excluded.updated_at''',
                                (endpoint.scope.key,selection.program_name,int(time.time())))
                        return dict(enabled=previous['enabled'] if previous else True),dict(program_name=selection.program_name,enabled=selection.enabled)
                    before = policy(cursor,endpoint)
                    cursor.execute('''INSERT INTO conversation_settings(scope_key,programs_enabled,program_say_limit,program_wake_limit)
                        VALUES (?,?,?,?) ON CONFLICT(scope_key) DO UPDATE SET programs_enabled=excluded.programs_enabled,
                        program_say_limit=excluded.program_say_limit,program_wake_limit=excluded.program_wake_limit''',
                        (endpoint.scope.key,selection.enabled,selection.say_limit,selection.wake_limit))
                    return before,policy(cursor,endpoint)
                result = context.control_store.mutate_transaction(cursor,'programs',expected_version=info.expected_version,
                    actor=info.actor,action='program.resume' if resume is not None else 'program.control' if program else 'program.settings',
                    target=endpoint.key,operation=operation)
        except AdminVersionConflict as error:
            raise HTTPException(409,dict(code='resource_version_conflict',resource=error.resource_key,
                expected_version=error.expected,current_version=error.current)) from None
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503,'程序配置未能提交，请刷新后重试') from None
        runtime = services.official_runtime() if callable(services.official_runtime) else None
        lifecycle = getattr(getattr(runtime,'agent_service',None),'vm_lifecycle',None)
        if lifecycle is not None:
            lifecycle.keeper.wake()
        context.event_broker.publish('programs')
        return mutation_payload(result,**result.value,synchronization='pending')

    @router.post('/api/programs/settings')
    def settings(selection: ProgramSettingsRequest, info: AdminMutationContext = Depends(mutation_context)):
        return mutate(selection,info)

    @router.post('/api/programs/control')
    def control(selection: ProgramControlRequest, info: AdminMutationContext = Depends(mutation_context)):
        return mutate(selection,info,program=True)

    @router.post('/api/programs/resume')
    async def resume(selection: ProgramResumeRequest, info: AdminMutationContext = Depends(mutation_context)):
        authorize()
        if info.expected_version is None:
            raise HTTPException(428,'请刷新程序状态并提供 If-Match')
        runtime = services.official_runtime() if callable(services.official_runtime) else None
        lifecycle = getattr(getattr(runtime,'agent_service',None),'vm_lifecycle',None)
        if lifecycle is None or lifecycle.closed or manager is None:
            raise HTTPException(503,'会话 VM 运行时不可用，未提交恢复请求')
        endpoint = parse_endpoint(selection.endpoint_key)
        try:
            metadata = await manager._metadata(workspace_owner(endpoint),selection.sandbox_id)
        except SandboxError:
            raise HTTPException(404,'找不到该会话的 VM') from None
        if manager.program_state.recovery_fence(metadata)!=selection.recovery_fence or metadata['programs'].get('planned_stop'):
            raise HTTPException(409,'VM 状态已改变，请刷新后重新检查')
        return await asyncio.to_thread(mutate,selection,info,resume=lifecycle.admin)

    return router
