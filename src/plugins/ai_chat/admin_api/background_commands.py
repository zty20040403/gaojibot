"""Read command facts and atomically audit cancellation / future-agent limits."""
from contextlib import contextmanager
import time

from fastapi import Depends, HTTPException, Query

from ..admin_control import AdminVersionConflict
from ..background_jobs import BackgroundJobs
from ..adapters.qq_official.background_commands import cancel_commands
from ..adapters.qq_official.program_actions import read_action
from ..platform_types import EndpointKey
from .context import authorize, mutation_context, mutation_payload
from .contracts import AdminMutationContext, AgentTimeLimitRequest, BackgroundCommandCancelRequest
from .programs import existing, parse_endpoint

FIELDS = '''command_id,endpoint_key,program_name,state,cancel_requested,created_at,started_at,
    deadline_at,finished_at,exit_code,elapsed_seconds,output_bytes,output_head,output_tail,
    error_code,notification_error,notification_action_id'''


def create_router(context):
    router,services = context.new_router(),context.services

    @contextmanager
    def transaction(*, readonly=False):
        if services.database is None:
            raise HTTPException(503,'后台命令存储未配置')
        connection = services.database.store_connection()
        cursor = connection.cursor()
        try:
            if readonly:
                cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            yield cursor
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            cursor.close(); connection.close()

    @router.get('/api/background-commands')
    def recent(endpoint_key: str = Query('',max_length=1000), active: bool = True):
        authorize()
        if services.database is None:
            return context.versioned('commands',dict(available=False,items=[],endpoints=[],truncated=False))
        with transaction(readonly=True) as cursor:
            if endpoint_key:
                existing(cursor,parse_endpoint(endpoint_key))
            endpoints = cursor.execute('SELECT endpoint_key FROM qq_official_endpoints ORDER BY endpoint_key LIMIT 501').fetchall()
            scopes = [EndpointKey.parse(row['endpoint_key']).scope.key for row in endpoints]
            limits = {row['scope_key']:row['agent_timeout_hours'] for row in cursor.execute(
                'SELECT scope_key,agent_timeout_hours FROM conversation_settings WHERE scope_key IN ('+
                ','.join('?' for _ in scopes)+')',scopes).fetchall()} if scopes else {}
            endpoints = [dict(row,hours=limits.get(scope,6)) for row,scope in zip(endpoints,scopes)]
            # Include an explicitly selected endpoint even beyond the bounded picker.
            if endpoint_key and not any(row['endpoint_key']==endpoint_key for row in endpoints[:500]):
                endpoints = [dict(endpoint_key=endpoint_key,hours=BackgroundJobs.hours(cursor,parse_endpoint(endpoint_key)))]+endpoints
            rows = cursor.execute('SELECT '+FIELDS+''' FROM qq_official_background_commands
                WHERE (?='' OR endpoint_key=?) AND (NOT ? OR finished_at IS NULL)
                ORDER BY created_at DESC,command_id DESC LIMIT 101''',(endpoint_key,endpoint_key,active)).fetchall()
            items = []
            for row in rows[:100]:
                value = dict(row,output_path='/work/.gaoji/out/'+row['command_id']+'.txt')
                if row['notification_action_id']:
                    value['notification'] = read_action(cursor,row['endpoint_key'],row['notification_action_id'])
                items.append(value)
            version = cursor.execute("SELECT version FROM admin_resource_versions WHERE resource_key='commands'").fetchone()
            return dict(available=True,items=items,endpoints=endpoints[:500],endpoints_truncated=len(endpoints)>500,
                truncated=len(rows)>100,api_version='v1',resource='commands',resource_version=version['version'] if version else 0)

    def mutate(selection, info, *, command_id=None):
        authorize()
        if info.expected_version is None:
            raise HTTPException(428,'请先刷新后台命令状态，再提供 If-Match')
        endpoint = parse_endpoint(selection.endpoint_key)
        try:
            with transaction() as cursor:
                cursor.execute('SELECT account_key FROM qq_official_accounts WHERE account_key=? FOR UPDATE',(endpoint.account.key,))
                existing(cursor,endpoint)
                def operation():
                    if command_id is None:
                        before = dict(hours=BackgroundJobs.hours(cursor,endpoint))
                        cursor.execute('''INSERT INTO conversation_settings(scope_key,agent_timeout_hours) VALUES (?,?)
                            ON CONFLICT(scope_key) DO UPDATE SET agent_timeout_hours=excluded.agent_timeout_hours''',
                            (endpoint.scope.key,selection.hours))
                        return before,dict(hours=selection.hours,applies_to='new_jobs')
                    columns = 'command_id,state,cancel_requested,finished_at'
                    row = cursor.execute('SELECT '+columns+' FROM qq_official_background_commands WHERE endpoint_key=? AND command_id=? FOR UPDATE',
                        (endpoint.key,command_id)).fetchone()
                    if row is None:
                        raise HTTPException(404,'找不到该会话中的后台命令')
                    cancel_commands(cursor,endpoint,int(time.time()),command_id=command_id)
                    after = cursor.execute('SELECT '+columns+' FROM qq_official_background_commands WHERE command_id=?',(command_id,)).fetchone()
                    return dict(row),dict(after,synchronization='pending')
                result = context.control_store.mutate_transaction(cursor,'commands',expected_version=info.expected_version,
                    actor=info.actor,action='background.command.cancel' if command_id else 'background.time_limit',
                    target=command_id or endpoint.key,operation=operation)
        except AdminVersionConflict as error:
            raise HTTPException(409,dict(code='resource_version_conflict',resource=error.resource_key,
                expected_version=error.expected,current_version=error.current)) from None
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503,'变更未能提交，请刷新后重试') from None
        runtime = services.official_runtime() if callable(services.official_runtime) else None
        lifecycle = getattr(getattr(runtime,'agent_service',None),'vm_lifecycle',None)
        if lifecycle is not None:
            lifecycle.commands.changed.set()
        context.event_broker.publish('commands','backgroundAgents')
        return mutation_payload(result,**result.value)

    @router.put('/api/background-commands/settings')
    def settings(selection: AgentTimeLimitRequest, info: AdminMutationContext = Depends(mutation_context)):
        return mutate(selection,info)

    @router.post('/api/background-commands/{command_id}/cancel')
    def cancel(command_id: str, selection: BackgroundCommandCancelRequest, info: AdminMutationContext = Depends(mutation_context)):
        return mutate(selection,info,command_id=command_id)

    return router
