"""Native status reads are fenced by the real command execution, not the model."""
from ...application.commands import CommandResult
from ...build_version import version_label
from ...model_billing_views import windows,status_lines
from ...budget_views import project,status_lines as budget_lines
from .conversation_control import state


def command(runtime,life,app):
    async def invoke(inbound,request):
        if request.argument:return CommandResult('syntax','用法：/status')
        version = version_label()
        if getattr(app,'model_billing',None) is None:
            return CommandResult('unavailable',f'版本：{version}\n宿主逐请求账单尚未配置；本人回合用量请用 /usage，运行任务请用 /task。')
        def read():
            with life.effect_scope(inbound) as cursor:
                stopped = state(cursor,inbound.endpoint)['stopped']
                now = int(runtime.store.clock())
                totals = windows(cursor,inbound.endpoint,now=now)
                budget = project(cursor,(inbound.endpoint,),now=now)[0]
                return CommandResult('ok',f'版本：{version}\n'+('本会话状态：已停止' if stopped else '本会话状态：未停止')+
                    '\n本会话模型用量（物理入口，不跟随历史别名）：\n'+status_lines(totals)+'\n'+budget_lines(budget)+
                    '\n仅统计已记录的模型请求；≥ 表示缺少用量。接线前旧用量不追算。')
        return await runtime._calls(read)
    return invoke
