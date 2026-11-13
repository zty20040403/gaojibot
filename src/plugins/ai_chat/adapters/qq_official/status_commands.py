"""Native status reads are fenced by the real command execution, not the model."""
from ...application.commands import CommandResult
from ...model_billing_views import windows,status_lines
from .conversation_control import state


def command(runtime,life,app):
    async def invoke(inbound,request):
        if request.argument:return CommandResult('syntax','用法：/status')
        if getattr(app,'model_billing',None) is None:
            return CommandResult('unavailable','宿主逐请求账单尚未配置；本人回合用量请用 /usage，运行任务请用 /task。')
        def read():
            with life.effect_scope(inbound) as cursor:
                stopped = state(cursor,inbound.endpoint)['stopped']
                totals = windows(cursor,inbound.endpoint,now=int(runtime.store.clock()))
                return CommandResult('ok',('本会话状态：已停止' if stopped else '本会话状态：未停止')+
                    '\n宿主账单（物理入口，不跟随历史别名）：\n'+status_lines(totals)+
                    '\n按请求当时声明的美元单价计价，不是供应商结算账单；≥ 表示缺价或缺用量。接线前旧用量不追算。')
        return await runtime._calls(read)
    return invoke
