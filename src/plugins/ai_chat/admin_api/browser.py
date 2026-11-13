"""Browser domains and audited once-only clearing; no secrets or guest work in SQL transactions."""
import asyncio
from contextlib import contextmanager
import hashlib
import json
import time
from uuid import uuid4

from fastapi import Depends, HTTPException, Query, Request

from src.bot_security.store import SecurityError
from ..admin_security import SESSION_COOKIE,require_admin
from ..admin_control import AdminVersionConflict
from ..adapters.qq_official.browser_mutations import mutate,BrowserActionUnknown
from ..adapters.qq_official.browser_runtime import BrowserUnavailable
from .context import mutation_context,mutation_payload
from .contracts import AdminMutationContext,BrowserClearRequest
from .programs import parse_endpoint,existing


def target(endpoint):
    return 'endpoint:'+hashlib.sha256(endpoint.key.encode()).hexdigest()


def create_router(context):
    router,services = context.new_router(),context.services

    @contextmanager
    def transaction():
        if services.database is None:
            raise HTTPException(503,'浏览器管理存储未配置')
        connection = services.database.store_connection(); cursor = connection.cursor()
        try:
            yield cursor
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            cursor.close(); connection.close()

    def runtime():
        current = services.official_runtime() if callable(services.official_runtime) else None
        lifecycle = getattr(getattr(current,'agent_service',None),'vm_lifecycle',None)
        return current,None if lifecycle is None or lifecycle.closed else lifecycle.browser

    def authority(request, account, current, *, version=None):
        cookie = request.cookies.get(SESSION_COOKIE,'')
        def checked(cursor):
            try:
                live = services.mobile_authorization.store.session(cookie)
            except SecurityError:
                raise PermissionError('browser_admin_session_revoked') from None
            if (live['role']!='admin' or live['account_id']!=account['account_id'] or live['version']!=account['version']
                    or services.official_runtime() is not current):
                raise PermissionError('browser_admin_changed')
            if version is not None:
                row = cursor.execute("SELECT version FROM admin_resource_versions WHERE resource_key='browser'").fetchone()
                if (row['version'] if row else 0)!=version:
                    raise PermissionError('browser_snapshot_changed')
        return checked

    def read(endpoint):
        with transaction() as cursor:
            if endpoint is not None:
                existing(cursor,endpoint)
            row = cursor.execute("SELECT version FROM admin_resource_versions WHERE resource_key='browser'").fetchone()
            version = row['version'] if row else 0
            recent = []
            if endpoint is not None:
                rows = cursor.execute('''SELECT a.resource_version,a.created_at,a.after_json,r.after_json AS outcome
                    FROM admin_audit_log a LEFT JOIN LATERAL (
                        SELECT after_json FROM admin_audit_log WHERE resource_key='browser'
                            AND resource_version=a.resource_version AND action='browser.clear.result'
                        ORDER BY audit_id DESC LIMIT 1) r ON TRUE
                    WHERE a.resource_key='browser' AND a.action='browser.clear.requested' AND a.target=?
                    ORDER BY a.audit_id DESC LIMIT 5''',(target(endpoint),)).fetchall()
                for row in rows:
                    value = json.loads(row['after_json'])
                    outcome = json.loads(row['outcome']) if row['outcome'] else value
                    recent.append(dict(operation_id=value['operation_id'],created_at=row['created_at'],
                        state=outcome['state'],error_code=outcome['error_code']))
            return version,recent

    @router.get('/api/conversations/browser')
    async def snapshot(request: Request, endpoint_key: str = Query('',max_length=1000)):
        account = require_admin()
        if services.database is None:
            return context.versioned('browser',dict(available=False,snapshot=None,recent=[]))
        endpoint = parse_endpoint(endpoint_key) if endpoint_key else None
        version,recent = await asyncio.to_thread(read,endpoint)
        current,browser = runtime()
        result = None
        if endpoint is not None and browser is not None:
            try:
                result = await browser.snapshot(endpoint,authorize=authority(request,account,current,version=version))
            except PermissionError:
                raise HTTPException(409,'会话、登录或浏览器配置已改变，请刷新') from None
            except BrowserUnavailable:
                raise HTTPException(503,'无法读取浏览器状态；未开机或清理资料') from None
        return dict(available=browser is not None,snapshot=result,recent=recent,
            api_version='v1',resource='browser',resource_version=version)

    @router.post('/api/conversations/browser/clear')
    async def clear(selection: BrowserClearRequest, request: Request, info: AdminMutationContext = Depends(mutation_context)):
        account = require_admin()
        if info.expected_version is None:
            raise HTTPException(428,'请刷新浏览器状态并提供 If-Match')
        endpoint = parse_endpoint(selection.endpoint_key)
        current,browser = runtime()
        if browser is None:
            raise HTTPException(503,'会话 VM 运行时不可用，未发起清理')
        checked = authority(request,account,current)
        identifier = uuid4().hex
        pending = dict(operation_id=identifier,state='unknown',error_code='result_not_confirmed')
        def begin():
            with transaction() as cursor:
                browser.sessions._route(cursor,endpoint)
                checked(cursor)
                return context.control_store.mutate_transaction(cursor,'browser',expected_version=info.expected_version,
                    actor=info.actor,action='browser.clear.requested',target=target(endpoint),
                    operation=lambda:(dict(endpoint_key=endpoint.key,fence=selection.fence),pending))
        try:
            receipt = await asyncio.to_thread(begin)
        except AdminVersionConflict as error:
            raise HTTPException(409,dict(code='resource_version_conflict',resource=error.resource_key,
                expected_version=error.expected,current_version=error.current)) from None
        except PermissionError:
            raise HTTPException(403,'会话或登录授权已失效，未发起清理') from None
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503,'清理意图未能提交，未发起清理') from None
        # Intent and version are durable before the first guest call. Restart or
        # cancelled HTTP leaves unknown; no worker ever replays this request.
        context.event_broker.publish('browser','audit')
        try:
            result = await mutate(browser,endpoint,authorize=checked,key='admin:'+identifier,
                action='clear',expected_fence=selection.fence)
            code = result.get('error','')
            state = 'cleared' if result.get('cleared') is True else ('unknown' if code in {
                'browser_action_unknown','browser_request_conflict'} else 'not_applied')
        except BrowserActionUnknown:
            state,code = 'unknown','browser_action_unknown'
        except PermissionError:
            state,code = 'not_applied','browser_authorization_changed'
        except BrowserUnavailable as error:
            state = 'not_applied'
            code = str(error) if str(error) in {'browser_busy','browser_snapshot_changed'} else 'browser_unavailable'
        outcome = dict(operation_id=identifier,state=state,error_code=code)
        def finish():
            with transaction() as cursor:
                cursor.execute('''INSERT INTO admin_audit_log(resource_key,resource_version,actor,action,target,status,
                    before_json,after_json,error,created_at) VALUES ('browser',?,?,'browser.clear.result',?,?,'{}',?,'',?)''',
                    (receipt.resource_version,info.actor,target(endpoint),'succeeded' if state=='cleared' else 'failed',
                        json.dumps(outcome,separators=(',',':')),int(time.time())))
        try:
            await asyncio.to_thread(finish)
        except Exception:
            raise HTTPException(503,'清理结果未能记入审计，可能已执行；请检查状态，不要自动重试') from None
        context.event_broker.publish('browser','audit')
        return mutation_payload(receipt,**outcome)

    return router
