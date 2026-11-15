"""Foreground private questions require the actual host-issued source session."""
import json

from ..adapters.qq_official.question_asks import QuestionAsks
from ..adapters.qq_official.turn_platform import OfficialTurnPlatform
from .registry import FunctionTool
from .state import ToolContext

ASK = {'type':'function','function':{'name':'ask','description':
    '向已双边确认的单向连接提问；目标只用自身会话内容，可拒绝。target只能是宿主给出的连接编号，'
    '不是群号、openid、历史别名或名字。等待不超过原任务剩余时间且最多两小时；结果只返回原调用，'
    '不自动发QQ。没有连接请先/link，停止或预算限制会明确拒绝，不补跑旧问答。',
    'parameters':{'type':'object','properties':{'target':{'type':'string','pattern':'^[0-9a-f]{32}$'},
        'question':{'type':'string','minLength':1,'maxLength':8000}},'required':['target','question'],'additionalProperties':False}}}


def available(context):
    if not isinstance(context,ToolContext):return False
    session,platform = context.question_session,context.platform
    return (context.inbound is not None and context.event is None and isinstance(session,QuestionAsks)
        and isinstance(platform,OfficialTurnPlatform) and session.binding is platform.execution
        and context.inbound==platform.inbound and session.binding.handle.execution_kind=='chat'
        and (context.inbound.endpoint,context.inbound.actor,context.inbound.scope.key,context.inbound.route_epoch,context.inbound.native_message_id)==
            (session.binding.handle.endpoint,session.binding.handle.actor,session.binding.handle.scope_key,
                session.binding.handle.route_epoch,session.binding.handle.source_native_message_id))


def project(definition,context):
    definition['function']['description'] += '\n本回合已核验的可用单向连接（提交仍重新授权；不按名字猜测）：'+json.dumps(
        context.question_session.references,ensure_ascii=False)


async def execute(turn,arguments):
    if not available(turn.context):return json.dumps(dict(ok=False,code='question_source_binding_required'))
    if set(arguments)!={'target','question'}:return json.dumps(dict(ok=False,code='invalid_request'))
    result = await turn.context.question_session.ask(arguments['target'],arguments['question'])
    return json.dumps(result.payload(),ensure_ascii=False)


HANDLERS = (FunctionTool(ASK,available,execute,project=project,platform_neutral=True),)
