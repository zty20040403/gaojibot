"""Scoped persisted program status; reads never boot a VM or execute guest code."""
import json

from ...background_tools import workspace_owner
from ...prompt_context import quote_evidence
from ...application.commands import CommandResult


class ProgramStatus:
    def __init__(self, *, transaction, manager, calls, clock, route):
        self.transaction,self.manager,self.calls,self.clock,self.route = transaction,manager,calls,clock,route

    def _read(self, endpoint, authorize, *, details=True, recovery=False):
        service = self
        with service.transaction() as cursor:
            authorize(cursor)
            service.route(cursor,endpoint)
            if not details:
                return
            row = cursor.execute('''SELECT programs_enabled,program_say_limit,program_wake_limit
                FROM conversation_settings WHERE scope_key=?''',(endpoint.scope.key,)).fetchone()
            settings = dict(row) if row else dict(programs_enabled=True,program_say_limit=60,program_wake_limit=6)
            controls = cursor.execute('''SELECT program_name,enabled FROM program_controls
                WHERE scope_key=? ORDER BY program_name LIMIT 256''',(endpoint.scope.key,)).fetchall()
            counts = cursor.execute('''SELECT program_name,
                COUNT(*) FILTER(WHERE kind='say') AS say_requests,
                COUNT(*) FILTER(WHERE kind='wake') AS wake_requests,
                COUNT(*) FILTER(WHERE kind='send_file') AS file_requests
                FROM qq_official_program_actions WHERE endpoint_key=? AND created_at>=?
                GROUP BY program_name ORDER BY program_name LIMIT 64''',(endpoint.key,int(service.clock())-86400)).fetchall()
            failures = cursor.execute('''SELECT a.program_name,a.kind,
                CASE WHEN a.state IN ('failed','cancelled','ambiguous') THEN a.state
                    ELSE COALESCE(d.status,w.state,a.state) END AS state,
                CASE WHEN a.state IN ('failed','cancelled','ambiguous') THEN a.error_code
                    ELSE COALESCE(NULLIF(d.wait_reason,''),NULLIF(w.error_code,''),a.error_code) END AS error_code
                FROM qq_official_program_actions a
                LEFT JOIN qq_official_program_wakes w ON w.wake_id=a.wake_id
                LEFT JOIN qq_official_program_actions r ON r.action_id=w.reply_action_id
                LEFT JOIN deliveries d ON d.plan_id=COALESCE(a.plan_id,r.plan_id) AND d.part_index=0
                WHERE a.endpoint_key=? AND a.created_at>=? AND
                    (a.state IN ('failed','cancelled','ambiguous') OR d.status IN ('failed','cancelled','ambiguous')
                    OR w.state IN ('failed','cancelled','ambiguous'))
                ORDER BY a.created_at DESC,a.action_id DESC LIMIT 8''',(endpoint.key,int(service.clock())-86400)).fetchall()
            result = dict(settings=settings,controls=[dict(row) for row in controls],
                requests_24h=[dict(row) for row in counts],recent_failures=[dict(row) for row in failures])
            if recovery:
                from .keys import account_job_kind
                result['recovery_jobs'] = [dict(row) for row in cursor.execute('''SELECT job_id,status,last_error,updated_at
                    FROM durable_jobs WHERE kind=? AND scope_key=? ORDER BY job_id DESC LIMIT 5''',
                    (account_job_kind('program_admin',endpoint.account.key),endpoint.scope.key)).fetchall()]
            return result

    async def snapshot(self, endpoint, *, authorize, include_recovery=False):
        service = self
        result = await service.calls(self._read,endpoint,authorize,recovery=include_recovery)
        owner = workspace_owner(endpoint)
        vms = []
        for item in await service.manager._all_metadata() if service.manager is not None else ():
            if item.get('owner_ref')!=owner:
                continue
            metadata = await service.manager._metadata(owner,str(item['id']))
            state = metadata['programs']
            report = state.get('snapshot',{})
            vms.append(dict(sandbox_id=item['id'],retained=bool(state.get('retained')),
                suspended=bool(state.get('suspended')),paused=bool(state.get('paused')),
                alert=str(state.get('alert',''))[:500],hold_reason=str(state.get('hold_reason',''))[:120],
                updated_at=state.get('updated_at'),units=report.get('units',[])[:32],errors=report.get('errors',{})))
            if include_recovery:
                vms[-1]['recovery_fence'] = service.manager.program_state.recovery_fence(metadata)
            if len(vms)==8:
                break
        await service.calls(self._read,endpoint,authorize,details=False)
        return dict(result,vms=vms)

    async def context(self, turn):
        inbound,execution = turn.context.inbound,turn.context.platform.execution
        def authorize(cursor):
            source = execution.current(cursor,int(self.clock()))
            if source['endpoint_key']!=inbound.endpoint.key or source['requester_actor_key']!=inbound.actor.key:
                raise PermissionError('program_status_source_mismatch')
        result = await self.snapshot(inbound.endpoint,authorize=authorize)
        # Logs stay in the admin view, not every model request. Even unit names
        # and persisted error strings are evidence, never prompt instructions.
        for vm in result['vms']:
            vm['units'] = [dict(name=str(row.get('name',''))[:100],enabled=row.get('enabled') is not False,
                ActiveState=str(row.get('ActiveState','未知'))[:64],SubState=str(row.get('SubState',''))[:64])
                for row in vm['units'][:16]]
            vm['errors'] = {str(key)[:100]:str(value)[:200] for key,value in list(vm['errors'].items())[:4]}
        for row in result['recent_failures']:
            row['error_code'] = str(row['error_code'])[:200]
        result['controls'] = result['controls'][:32]
        result['requests_24h'] = result['requests_24h'][:16]
        if not (result['vms'] or result['recent_failures'] or result['controls'] or not result['settings']['programs_enabled']):
            return ''
        return ('[常驻程序]\n本会话持久快照，可能落后于正在执行的同步。请求次数不等于发送成功；'
            '暂停、停用或投递失败应如实说明，不能声称程序已正常通知。\n'
            + quote_evidence(json.dumps(result,ensure_ascii=False),'resident_programs'))

    async def command(self, inbound, request, *, authorize):
        if request.argument:
            return CommandResult('syntax','用法：/units')
        snapshot = await self.snapshot(inbound.endpoint,authorize=authorize)
        disabled = {row['program_name'] for row in snapshot['controls'] if not row['enabled']}
        lines = ['本会话常驻程序（最近持久快照）：']
        if not snapshot['settings']['programs_enabled']:
            lines.append('会话程序已停用。')
        for vm in snapshot['vms']:
            if vm['alert'] or vm['hold_reason']:
                lines.append(vm['alert'] or vm['hold_reason'])
            for row in vm['units']:
                name = str(row.get('name',''))
                off = not snapshot['settings']['programs_enabled'] or name.rsplit('.',1)[0] in disabled or row.get('enabled') is False
                lines.append(name+': '+('已停用；运行快照 ' if off else '')+str(row.get('ActiveState','未知')))
            if vm['errors']:
                lines.append('存在同步错误；请管理员查看日志。')
        for row in snapshot['requests_24h'][:16]:
            lines.append(f"{row['program_name']} · 24h 已接收请求：say {row['say_requests']} / wake {row['wake_requests']} / file {row['file_requests']}")
        for row in snapshot['recent_failures'][:4]:
            lines.append(f"{row['program_name']} {row['kind']}：{row['state']} · {str(row['error_code'])[:120]}")
        if len(lines)==1:
            lines.append('暂无已登记程序。')
        return CommandResult('ok','\n'.join(lines)[:6000])
