"""Wake the foreground with a report; reuse the ordinary file and text outbox."""
from dataclasses import replace
import hashlib
from types import SimpleNamespace

from ...agent_loop import AgentTrace, ask_deepseek_with_tools
from ...application.turn_context import turn_window
from ...background_tools import BackgroundPlatform
from ...message_ir import MessageBody, TextNode
from ...prompt import system_prompt
from ...tool_policy import ToolCatalog, policy_for_tool
from ...tools import neutral_registry_for
from ...tools.background import DEFINITIONS
from ...tools.state import ToolContext, TurnState
from .file_artifacts import SourceBinding, _issue
from .renderer import render_plan


class ResultSource:
    kind = 'background'

    def __init__(self, service, row):
        self.service, self.row = service, row

    def current_source(self, cursor, binding, now):
        self.service.check(cursor, self.row)
        current = cursor.execute('SELECT * FROM background_jobs WHERE job_id=?', (self.row['job_id'],)).fetchone()
        if current['state'] == 'cancelled' or current['delivered_at'] is not None:
            raise PermissionError('后台任务结果已关闭。')
        return dict(current, source_scope_key=current['scope_key'],
            source_canonical_message_id=current['canonical_message_id'], source_native_message_id=current['native_message_id'])

    def bind_artifact(self, cursor, binding, fields, **kwargs):
        self.current_source(cursor, binding, kwargs['now'])


async def present(service, row, *, question=None):
    app, runtime = service.app, service.runtime
    inbound = await service.calls(service.restore, row)
    profile = app.model_catalog.resolve_preference(row['profile'])
    files, trace = None, AgentTrace()
    answer = row.get('frontend_answer', '') if question is None else ''
    try:
        if not answer:
            context = ToolContext(SimpleNamespace(context=app), inbound=inbound,
                platform=BackgroundPlatform(inbound.endpoint), capabilities=frozenset({'self', 'context', 'memory'}))
            if question is None and row['state'] != 'cancelled' and runtime.file_service is not None:
                job = await service.calls(runtime.jobs.get, row['job_id'])
                deadline = int(service.clock()) + min(runtime.outbox.max_wait_seconds, 86400)
                source = _issue(SourceBinding(service.token, job, runtime.store.admitted, None, None, deadline,
                    inbound.endpoint, inbound.actor, row['source_execution_id'], f"background:{row['job_id']}",
                    deadline, row['native_message_id'], extension=ResultSource(service, row)))
                files = await runtime.file_service.generation.bound_session(source)
                if files is not None:
                    context = files.context(context)
            turn = TurnState(context, model_profile=profile)
            registry = neutral_registry_for(context)
            tools = registry.definitions(context)
            if question is not None:
                tools.append(next(item for item in DEFINITIONS if item['function']['name'] == 'agent_steer'))

            async def execute(name, args):
                if name == 'agent_steer' and question is not None:
                    await service.calls(service.store.steer, row['job_id'], row['endpoint_key'], row['actor_key'], str(args['message']))
                    return '{"ok":true}'
                return await registry.execute(turn, name, args)

            history, memory, _ = await service.calls(turn_window, app, inbound, profile)
            report = (f"[job#{row['job_id']} 提问] {question}\n"
                '根据聊天记录能回答就调用 agent_steer，把答案交回后台，然后输出 [silence]。'
                '只有需要用户补充时才向用户提问，提醒用户可以直接告诉你补充要求。') if question is not None else (
                f"[job#{row['job_id']} {row['state']}] {row['title']}\n{row['report'][:2000]}\n"
                f"完整报告：/work/.gaoji/jobs/job-{row['job_id']}.md\n"
                '这是用户先前要求的后台任务结果，请用你的语气向用户交付。文件在当前会话工作区，用 send_file_from_sandbox 传报告中的路径即可发送；报告不完整时用 sandbox_read_file 读全文。'
                '任务取消时不要继续执行。失败须说明具体原因，不能声称已经成功。')
            answer = await ask_deepseek_with_tools(report, [], tools, execute, profile=profile, trace=trace,
                group_context=history, memory_context=memory, system_prompt=system_prompt(app.settings.system_prompt),
                tool_catalog=ToolCatalog(tools, ignore_unknown_fields=True, policies={name: replace(
                    policy_for_tool(name), timeout_seconds=310) for name in ('send_file', 'send_file_from_sandbox')}),
                max_tool_rounds=24, max_total_calls=80, max_calls_per_round=24)
            if question is None:
                await service.calls(service.store.update, row['job_id'], frontend_answer=answer)
    except Exception as error:
        app.logger.exception('Background foreground response failed')
        answer = f"job#{row['job_id']} 结果整理失败：{type(error).__name__}。\n" + (question or row['report'][:2000])
    finally:
        if files is not None:
            await files.close()
        await service.record_usage(row, trace)

    def enqueue():
        with runtime.store.transaction() as cursor:
            service.check(cursor, row)
            if answer.strip() != '[silence]':
                key = f"background:{row['job_id']}:" + ('result' if question is None else hashlib.sha256(question.encode()).hexdigest())
                runtime.outbox.enqueue(service.token, rendered=render_plan(MessageBody((TextNode(0, answer),)), inbound.endpoint),
                    requester=inbound.actor, logical_key=key, source_scope_key=row['scope_key'], purpose='background_result',
                    expires_at=int(service.clock()) + min(runtime.outbox.max_wait_seconds, 86400),
                    source_native_message_id=row['native_message_id'], source_canonical_message_id=row['canonical_message_id'],
                    transaction_cursor=cursor)
            if question is None:
                cursor.execute('UPDATE background_jobs SET delivered_at=? WHERE job_id=?', (int(service.clock()), row['job_id']))
    await service.calls(enqueue)
    if runtime.sender is not None:
        runtime.sender.wake()
