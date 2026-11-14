"""Budget resource CAS shares native config transactions, not conversation versions."""
import time

from fastapi import Depends,HTTPException

from ..admin_control import AdminVersionConflict
from ..budget_views import project
from ..conversation_config import SETTINGS,change
from ..platform_types import EndpointKey
from .context import authorize,mutation_context
from .contracts import AdminMutationContext,ConversationBudgetRequest


def create_router(context):
    router,services = context.new_router(),context.services

    @router.get('/api/conversations/budgets')
    def budgets():
        authorize()
        if services.database is None:return context.versioned('budgets',dict(available=False,items=[],timezone='Asia/Shanghai'))
        connection = services.database.store_connection()
        try:
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            endpoints = connection.execute('SELECT endpoint_key FROM qq_official_endpoints ORDER BY endpoint_key LIMIT 501').fetchall()
            items = project(connection.cursor(),tuple(EndpointKey.parse(row['endpoint_key']) for row in endpoints[:500]),now=int(time.time()))
            version = connection.execute("SELECT version FROM admin_resource_versions WHERE resource_key='budgets'").fetchone()
            return dict(available=True,items=items,timezone='Asia/Shanghai',truncated=len(endpoints)>500,
                api_version='v1',resource='budgets',resource_version=version[0] if version else 0)
        finally:connection.close()

    @router.post('/api/conversations/budgets')
    def configure(selection: ConversationBudgetRequest,info: AdminMutationContext = Depends(mutation_context)):
        authorize()
        if services.database is None:raise HTTPException(503,'预算设置未配置')
        if info.expected_version is None:raise HTTPException(428,'请刷新预算快照并提供其 If-Match')
        try:
            endpoint = EndpointKey.parse(selection.endpoint_key)
            if endpoint.account.platform!='qq-official':raise ValueError
        except (TypeError,ValueError):raise HTTPException(404,'找不到该 QQ 会话') from None
        try:value = SETTINGS['daily_budget_usd'].validate('off' if selection.daily_budget_usd is None else selection.daily_budget_usd)
        except ValueError:raise HTTPException(400,'预算须为 off 或 0–1000000 的美元十进制，最多6位小数') from None
        connection = services.database.store_connection()
        cursor = connection.cursor()
        try:
            now = int(time.time())
            change(cursor,endpoint,'daily_budget_usd',value,actor=info.actor,now=now,expected_version=info.expected_version)
            version = cursor.execute("SELECT version FROM admin_resource_versions WHERE resource_key='budgets'").fetchone()[0]
            view = project(cursor,(endpoint,),now=now)[0]
            connection.commit()
        except AdminVersionConflict as error:
            connection.rollback()
            raise HTTPException(409,dict(code='resource_version_conflict',resource=error.resource_key,
                expected_version=error.expected,current_version=error.current)) from None
        except ValueError:
            connection.rollback(); raise HTTPException(404,'找不到该 QQ 会话') from None
        except Exception:
            connection.rollback(); raise HTTPException(503,'预算设置未提交，请刷新后重试') from None
        finally:cursor.close(); connection.close()
        context.event_broker.publish('budgets','conversations')
        return dict(api_version='v1',resource='budgets',resource_version=version,**view)

    return router
