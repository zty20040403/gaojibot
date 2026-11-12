"""Audited program policy; guest work is reconciled only after the DB commit."""
import asyncio
from contextlib import contextmanager
import time

from fastapi import Depends, HTTPException, Query

from ..admin_control import AdminVersionConflict
from ..adapters.qq_official.program_status import ProgramStatus
from ..platform_types import EndpointKey
from .context import authorize, mutation_context, mutation_payload
from .contracts import AdminMutationContext, ProgramControlRequest, ProgramSettingsRequest


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
            snapshot = await reader.snapshot(endpoint,authorize=checked)
        return dict(available=True,vm_available=manager is not None,items=items,snapshot=snapshot,
            api_version='v1',resource='programs',resource_version=version)

    def mutate(selection, info, *, program=False):
        authorize()
        if info.expected_version is None:
            raise HTTPException(428,'请刷新程序状态并提供 If-Match')
        endpoint = parse_endpoint(selection.endpoint_key)
        try:
            with transaction() as cursor:
                cursor.execute('SELECT account_key FROM qq_official_accounts WHERE account_key=? FOR UPDATE',(endpoint.account.key,))
                existing(cursor,endpoint)
                def operation():
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
                    actor=info.actor,action='program.control' if program else 'program.settings',target=endpoint.key,operation=operation)
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

    return router
