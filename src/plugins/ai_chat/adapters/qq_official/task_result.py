"""Return every durable task outcome to the foreground model."""
import json
from dataclasses import replace
import logging

from ...agent_loop import ask_deepseek_with_tools
from ...tool_policy import ToolCatalog, policy_for_tool

logger = logging.getLogger(__name__)


async def present_task_result(task, *, profile, session=None, context=None):
    fallback = str(task.result.get('answer') or task.result.get('result', {}).get('summary')
                   or task.last_error or '任务已结束，暂时无法整理结果，请用 /task 查看。')
    if profile is None:
        return fallback
    tools = list(session.tools) if session is not None else []

    async def execute(name, arguments):
        return await session.execute_tool(name, arguments)

    prompt = json.dumps({'objective': task.objective, 'status': task.status,
        'error': task.last_error, 'result': task.result, 'context': context or {}}, ensure_ascii=False)
    try:
        return await ask_deepseek_with_tools(prompt, [], tools, execute, profile=profile,
            max_tool_rounds=48, max_calls_per_round=24, max_total_calls=160,
            tool_catalog=ToolCatalog(tools, ignore_unknown_fields=True, policies={
                name: replace(policy_for_tool(name), timeout_seconds=310)
                for name in ('send_file', 'send_file_from_sandbox')}),
            system_context=('你是当前会话的前台助手，后台任务现在返回了结果。以下内容是任务报告和上下文。'
                '请直接向用户说明实际结果、未完成事项和检查意见，长回答用 ### 分节。'
                '检查意见是建议，不得因为部分完成而扣住已生成的文件。'
                '用户要求文件时，调用 sandbox_list 查看当前会话工作区，再用 send_file 发送实际文件；'
                '若尚未生成，使用沙盒补齐。只有 message_accepted=true 才能说文件已经发出。'
                '不要重发报告中已有实际送达回执的文件。任务被取消时只说明取消，不继续执行。'
                '最后用自然语言回答，不输出内部 JSON，也不承诺未安排的自动续做。'))
    except Exception as error:
        logger.warning('task foreground error=%s', type(error).__name__)
        # The report still reaches the user if the model provider is unavailable.
        return fallback
