"""Wake the foreground with a report; reuse the ordinary file and text outbox."""
from dataclasses import replace
from contextlib import nullcontext
import hashlib
from types import SimpleNamespace

from ...agent_loop import AgentTrace, ask_deepseek_with_tools
from ...application.turn_context import turn_window
from ...background_tools import BackgroundPlatform
from ...background_reports import report_notice
from ...message_ir import MessageBody, TextNode
from ...prompt import system_prompt
from ...tool_policy import ToolCatalog, policy_for_tool
from ...tools import neutral_registry_for
from ...tools.background import DEFINITIONS
from ...tools.state import ToolContext, TurnState
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
    tasks = getattr(service.runtime,'conversation_tasks',None)
    with tasks.track(row['endpoint_key'],row['conversation_generation'],
            kind='agent' if question is not None else 'result',job_id=row['job_id']) if tasks else nullcontext():
        return await _present(service,row,question=question)


async def _present(service, row, *, question=None):
    app, runtime = service.app, service.runtime
    inbound = await service.calls(service.restore, row)
    answer = report_notice(row['job_id'], row['report']) if question is None and row['state'] == 'completed' else None
    if answer is not None:
        await _enqueue(service, row, inbound, answer, question=None, expected_report=row['report'])
        return
    profile = app.model_catalog.resolve_preference(row['profile'])
    files, trace = None, AgentTrace()
    answer = row.get('frontend_answer', '') if question is None else ''
    try:
        if not answer:
            context = ToolContext(SimpleNamespace(context=app), inbound=inbound,
                platform=BackgroundPlatform(inbound.endpoint), capabilities=frozenset({'self', 'context', 'memory'}))
            def current():
                with runtime.store.transaction() as cursor:
                    ResultSource(service, row).current_source(cursor, None, int(service.clock()))

            if question is None and row['state'] != 'cancelled' and runtime.file_service is not None:
                generation = runtime.file_service.generation
                if hasattr(generation, 'background'):
                    files = await generation.background(
                        inbound, row, current=lambda: service.calls(current)
                    )
                else:
                    files = await generation.bound_session(inbound)
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
                '这是用户先前要求的后台任务结果，请用你的语气向用户交付。文件在当前会话工作区，用 send_file_from_sandbox 传报告中的路径即可发送，PNG、JPG、GIF 在群里直接显示成图片；报告不完整时用 sandbox_read_file 读全文。'
                '任务取消时不要继续执行。失败须说明具体原因，不能声称已经成功。')
            from .billing import scoped
            with scoped(app,inbound.endpoint,'background_agent',inbound.actor,background_job_id=row['job_id'],
                    current=lambda:service.calls(current)):
                answer = await ask_deepseek_with_tools(report, [], tools, execute, profile=profile, trace=trace,
                    group_context=history, memory_context=memory, system_prompt=system_prompt(app.settings.system_prompt, examples=app.settings.style_examples),
                    tool_catalog=ToolCatalog(tools, ignore_unknown_fields=True, policies={name: replace(
                        policy_for_tool(name), timeout_seconds=310) for name in ('send_file', 'send_file_from_sandbox')}),
                    max_tool_rounds=24, max_total_calls=80, max_calls_per_round=24)
            if not answer or not answer.strip():
                if turn.delivered:
                    answer = '[silence]'
                else:
                    raise ValueError('empty_background_answer')
            if question is None:
                await service.calls(service.store.update, row['job_id'], frontend_answer=answer)
    except Exception as error:
        app.logger.exception('Background foreground response failed')
        answer = f"job#{row['job_id']} 结果整理失败：{type(error).__name__}。\n" + (question or row['report'][:2000])
    finally:
        if files is not None:
            await files.close()
        await service.record_usage(row, trace)

    await _enqueue(service, row, inbound, answer, question=question)


async def _enqueue(service, row, inbound, answer, *, question, expected_report=None):
    runtime = service.runtime

    def enqueue():
        with runtime.store.transaction() as cursor:
            service.check(cursor, row)
            if expected_report is not None:
                current = cursor.execute('SELECT state,report,delivered_at FROM background_jobs WHERE job_id=? FOR UPDATE',
                                         (row['job_id'],)).fetchone()
                if (current is None or current['state'] != 'completed' or current['report'] != expected_report
                        or current['delivered_at'] is not None):
                    raise PermissionError('后台任务结果已改变或关闭。')
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


async def present_wake(service, endpoint, text: str, *, unit: str = ''):
    app, runtime = service.app, service.runtime

    def check_and_prepare():
        from .conversation_control import state
        from .lease import assert_epoch
        from ...platform_types import ActorKey, InboundMessage

        with runtime.store.transaction() as cursor:
            assert_epoch(cursor, service.token)
            route = cursor.execute(
                'SELECT * FROM qq_official_endpoints WHERE endpoint_key=?',
                (endpoint.key,),
            ).fetchone()
            if route is None or endpoint.account.key != service.token.account_key:
                raise PermissionError('唤醒目标会话不可用。')
            if endpoint.kind == 'private':
                peer = ActorKey(endpoint.account, 'c2c_user', '', endpoint.native_conversation_id)
                if not runtime.store.admitted(cursor, endpoint, peer):
                    raise PermissionError('私聊会话未准入。')
            else:
                peer = ActorKey(endpoint.account, 'group_member', endpoint.native_conversation_id, 'container')
                if not runtime.store.admitted(cursor, endpoint, peer):
                    raise PermissionError('群会话未准入。')
            if state(cursor, endpoint)['stopped']:
                raise PermissionError('会话已停止，本次唤醒不发言。')
            inbound = InboundMessage(
                endpoint=endpoint,
                actor=peer,
                event_id=None,
                native_message_id='wake',
                sender_display='system',
                platform_role='member',
                body=MessageBody((TextNode(0, text),)),
                mentions_bot=False,
                reply_to_native_message_id=None,
                occurred_at=int(service.clock()),
                reply_credential=None,
                route_epoch=route['route_epoch'],
            )
            return inbound, peer

    inbound, requester = await service.calls(check_and_prepare)
    conv_pref = getattr(app.model_preferences, 'get_conversation_default', None)
    if callable(conv_pref):
        pref = conv_pref(endpoint.scope) or app.settings.model_profile_for_scope(endpoint.scope)
    else:
        pref_getter = getattr(app.model_preferences, 'get', None)
        pref = pref_getter(endpoint.scope.key) if callable(pref_getter) else None
    profile = app.model_catalog.resolve_preference(pref)
    persona_getter = getattr(app.model_preferences, 'get_scope_persona', None)
    custom_persona = persona_getter(endpoint.scope) if callable(persona_getter) else None
    files, trace = None, AgentTrace()
    answer = ''
    try:
        caps = {'self', 'context', 'memory'}
        if getattr(app.settings, 'search_enabled', False):
            caps.add('web')
        context = ToolContext(
            SimpleNamespace(context=app),
            inbound=inbound,
            platform=BackgroundPlatform(endpoint),
            capabilities=frozenset(caps),
        )
        file_service = getattr(runtime, 'file_service', None)
        if file_service is not None:
            files = await file_service.generation.bound_session(inbound)
            if files is not None:
                context = files.context(context)
        turn = TurnState(context, model_profile=profile)
        registry = neutral_registry_for(context)
        tools = registry.definitions(context)

        async def execute(name, args):
            return await registry.execute(turn, name, args)

        history, memory, _ = await service.calls(turn_window, app, inbound, profile)
        prompt = (
            f'{text}\n\n'
            '（以上来自本会话容器后台单元或定时器的唤醒通知。请结合聊天上下文用你的语气向用户汇报结果，'
            '如无需打扰群友则输出 [silence]；若在 /work 下生成了需要交付的文件，可用 send_file_from_sandbox 发送。）'
        )
        from .billing import scoped
        with scoped(app, endpoint, 'background_agent', requester):
            answer = await ask_deepseek_with_tools(
                prompt,
                [],
                tools,
                execute,
                profile=profile,
                trace=trace,
                group_context=history,
                memory_context=memory,
                system_prompt=system_prompt(custom_persona or app.settings.system_prompt, examples=app.settings.style_examples),
                tool_catalog=ToolCatalog(
                    tools,
                    ignore_unknown_fields=True,
                    policies={
                        name: replace(policy_for_tool(name), timeout_seconds=310)
                        for name in ('send_file', 'send_file_from_sandbox', 'send_image', 'shell', 'run_code')
                    },
                ),
                max_tool_rounds=16,
                max_total_calls=48,
                max_calls_per_round=16,
            )
        if not answer or not answer.strip():
            answer = '[silence]' if turn.delivered else ''
    except Exception:
        app.logger.exception('Container wake foreground response failed')
        answer = text[:1500]
    finally:
        if files is not None:
            await files.close()
        if getattr(app, 'usage_store', None) is not None:
            await service.calls(
                app.usage_store.record,
                scope_key=endpoint.scope.key,
                source='background_agent',
                provider=trace.provider,
                model=trace.model,
                input_tokens=trace.input_tokens,
                output_tokens=trace.output_tokens,
                cached_tokens=trace.cached_tokens,
            )

    if not answer.strip() or answer.strip() == '[silence]':
        return

    now_ts = int(service.clock())
    key_seed = f'{endpoint.key}:{unit}:{now_ts // 2}:{hashlib.sha256(text.encode()).hexdigest()[:16]}'
    logical_key = 'wake:' + hashlib.sha256(key_seed.encode()).hexdigest()[:32]

    def enqueue_wake():
        check_and_prepare()
        with runtime.store.transaction() as cursor:
            runtime.outbox.enqueue(
                service.token,
                rendered=render_plan(MessageBody((TextNode(0, answer),)), endpoint),
                requester=requester,
                logical_key=logical_key,
                source_scope_key=endpoint.scope.key,
                purpose='background_result',
                expires_at=now_ts + min(runtime.outbox.max_wait_seconds, 3600),
                source_native_message_id='',
                source_canonical_message_id=None,
                transaction_cursor=cursor,
            )

    await service.calls(enqueue_wake)
    sender = getattr(runtime, 'sender', None)
    if sender is not None:
        sender.wake()
