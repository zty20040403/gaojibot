"""Versioned physical-conversation controls; login/CSRF remain the authority."""
import time

from fastapi import Depends, HTTPException

from ..admin_control import AdminVersionConflict
from ..adapters.qq_official.conversation_control import change, state
from ..platform_types import EndpointKey
from .context import authorize, mutation_context, mutation_payload
from .contracts import AdminMutationContext, ConversationControlRequest


def create_router(context):
    router,services = context.new_router(),context.services

    @router.get('/api/conversations')
    def conversations():
        authorize()
        if services.database is None:
            return context.versioned('conversations',{'available':False,'items':[]})
        connection = services.database.store_connection()
        try:
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            endpoints = connection.execute('''SELECT endpoint_key,kind,binding_state,membership_state
                FROM qq_official_endpoints ORDER BY endpoint_key LIMIT 500''').fetchall()
            scopes = tuple(EndpointKey.parse(row['endpoint_key']).scope.key for row in endpoints)
            settings = {row['scope_key']:row for row in connection.execute(
                'SELECT scope_key,stopped,stop_generation FROM conversation_settings WHERE scope_key IN ('+
                ','.join('?' for _ in scopes)+')',scopes).fetchall()} if scopes else {}
            items = [dict(row,**{key:settings.get(EndpointKey.parse(row['endpoint_key']).scope.key,{}).get(key,default)
                for key,default in (('stopped',False),('stop_generation',0))}) for row in endpoints]
            version = connection.execute("SELECT version FROM admin_resource_versions WHERE resource_key='conversations'").fetchone()
            return {'available':True,'items':items,'api_version':'v1','resource':'conversations',
                'resource_version':version['version'] if version else 0}
        finally:
            connection.close()

    @router.post('/api/conversations/control')
    def control(selection: ConversationControlRequest, mutation_info: AdminMutationContext = Depends(mutation_context)):
        authorize()
        if services.database is None:
            raise HTTPException(503,'会话控制未配置')
        if mutation_info.expected_version is None:
            raise HTTPException(428,'请先刷新会话状态，再提交带 If-Match 的操作')
        try:
            endpoint = EndpointKey.parse(selection.endpoint_key)
            if endpoint.account.platform!='qq-official':
                raise ValueError
        except (TypeError,ValueError):
            raise HTTPException(404,'找不到该 QQ 会话') from None
        connection = services.database.store_connection()
        cursor = connection.cursor()
        try:
            cursor.execute('SELECT account_key FROM qq_official_accounts WHERE account_key=? FOR UPDATE',(endpoint.account.key,))
            def operation():
                return state(cursor,endpoint),change(cursor,endpoint,selection.action,now=int(time.time()),
                    actor=mutation_info.actor,authorized=True)
            result = context.control_store.mutate_transaction(cursor,'conversations',
                expected_version=mutation_info.expected_version,actor=mutation_info.actor,
                action='conversation.'+selection.action,target=endpoint.key,operation=operation)
            connection.commit()
        except AdminVersionConflict as error:
            connection.rollback()
            raise HTTPException(409,{'code':'resource_version_conflict','resource':error.resource_key,
                'expected_version':error.expected,'current_version':error.current}) from None
        except ValueError:
            connection.rollback()
            raise HTTPException(404,'找不到该 QQ 会话') from None
        except Exception:
            connection.rollback()
            raise HTTPException(503,'会话控制未能提交，请刷新后重试') from None
        finally:
            cursor.close()
            connection.close()
        runtime = services.official_runtime() if callable(services.official_runtime) else None
        notify = getattr(runtime,'control_changed',None)
        if notify is not None:
            notify(endpoint.key,result.value)
        context.event_broker.publish('conversations','tasks','backgroundAgents')
        return mutation_payload(result,**result.value)

    return router
