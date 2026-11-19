import asyncio
import json
import unittest
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from src.plugins.ai_chat.agent_loop import ask_deepseek_with_tools
from src.plugins.ai_chat.adapters.qq_official.progress import ProgressEmitter
from src.plugins.ai_chat.tool_invocation import active_tool_step
from src.plugins.ai_chat.model_catalog import ModelProfile
from tests.tool_loop_fixture import batch, final, tool


class Stop(Exception):
    pass


class Store:
    def __init__(self, stop=None):
        self.state = None
        self.stop = stop

    async def save(self, state):
        self.state = deepcopy(state)
        if self.stop and self.stop(state):
            raise Stop()


class CheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def interrupted(self, args, response, error=Stop):
        with patch('src.plugins.ai_chat.agent_loop._create_completion', new=AsyncMock(return_value=response)):
            with self.assertRaises(error):
                await ask_deepseek_with_tools(**args)

    async def test_completed_batch_resume_and_final_recovery(self):
        store = Store(lambda s: s['phase'] == 'batch' and s['next_index'] == 1)
        async def run(name, _arguments):
            self.assertEqual(store.state['phase'], 'inflight')
            self.assertEqual(active_tool_step.get(), ('turn-1', {'one': 1, 'two': 2}[name]))
            return {'one': 'first-result', 'two': 'second-result'}[name]
        execute = AsyncMock(side_effect=run)
        events = []
        async def event(item):
            events.append((item.kind, item.sequence))
        args = dict(user_text='private base prompt', history=[], tools=[tool('one'), tool('two')],
                    execute_tool=execute, checkpoint_sink=store.save, idempotency_key_prefix='turn-1', event_sink=event)
        blocked = Store(lambda s: s['phase'] == 'inflight')
        await self.interrupted({**args, 'checkpoint_sink': blocked.save}, batch('one', 'two'))
        execute.assert_not_awaited()
        await self.interrupted(args, batch('one', 'two'))
        self.assertNotIn('private base prompt', json.dumps(store.state))
        self.assertEqual(store.state['messages'][-1]['content'], 'first-result')
        store.stop = None
        with patch('src.plugins.ai_chat.agent_loop._create_completion', new=AsyncMock(return_value=final())) as model:
            self.assertEqual(await ask_deepseek_with_tools(**args, checkpoint_state=store.state), 'done')
        self.assertEqual(execute.await_count, 2)
        self.assertEqual(execute.await_args_list[1].args[0], 'two')
        self.assertIn('first-result', str(model.await_args.kwargs['messages']))
        self.assertEqual(events, [('tool_started', 1), ('tool_finished', 1), ('tool_started', 2), ('tool_finished', 2)])
        self.assertEqual(store.state['phase'], 'final')
        self.assertIsNone(active_tool_step.get())
        with patch('src.plugins.ai_chat.agent_loop._create_completion', new=AsyncMock()) as model:
            self.assertEqual(await ask_deepseek_with_tools(**args, checkpoint_state=store.state), 'done')
            model.assert_not_awaited()

    async def test_unknown_side_effect_stops_remaining_calls_and_model_has_no_tools(self):
        store = Store()
        async def interrupted(*_):
            raise asyncio.CancelledError()
        args = dict(user_text='x', history=[], tools=[tool('shell'), tool('other')],
                    execute_tool=interrupted, checkpoint_sink=store.save, idempotency_key_prefix='turn-2', foreground_quiet_calls=1)
        await self.interrupted(args, batch('shell', 'other'), asyncio.CancelledError)
        self.assertEqual(store.state['phase'], 'inflight')
        del store.state['last_progress_call']  # In-flight snapshot from before the quiet reminder.
        execute = AsyncMock()
        with patch('src.plugins.ai_chat.agent_loop._create_completion', new=AsyncMock(return_value=final('unknown'))) as model:
            answer = await ask_deepseek_with_tools(**{**args, 'execute_tool': execute}, checkpoint_state=store.state)
        self.assertEqual(answer, 'unknown')
        execute.assert_not_awaited()
        request = model.await_args.kwargs
        self.assertNotIn('tools', request)
        self.assertIn('outcome', str(request['messages']))
        self.assertIn('停止后续', str(request['messages']))

    async def test_unconfirmed_image_blocks_a_second_generation(self):
        execute = AsyncMock(return_value='{"ok":false,"code":"generation_unknown"}')
        model = AsyncMock(side_effect=[batch('generate_image', 'generate_image'), final('unknown')])
        with patch('src.plugins.ai_chat.agent_loop._create_completion', new=model):
            answer = await ask_deepseek_with_tools(user_text='test', history=[], tools=[tool('generate_image')], execute_tool=execute)
        self.assertEqual(answer, 'unknown')
        execute.assert_awaited_once()
        self.assertNotIn('tools', model.await_args.kwargs)

    async def test_feedback_and_budget_survive_resume(self):
        store = Store(lambda s: s['phase'] == 'batch' and s['next_index'] == 1)
        feedback = AsyncMock(side_effect=[['remember this'], []])
        execute = AsyncMock(return_value='ok')
        args = dict(user_text='x', history=[], tools=[tool('one'), tool('two')], execute_tool=execute,
                    max_total_calls=1, feedback_provider=feedback, checkpoint_sink=store.save, idempotency_key_prefix='turn-3', foreground_quiet_calls=2)
        await self.interrupted(args, batch('one', 'two'))
        store.stop = None
        with patch('src.plugins.ai_chat.agent_loop._create_completion', new=AsyncMock(return_value=final())) as model:
            await ask_deepseek_with_tools(**args, checkpoint_state=store.state)
        execute.assert_awaited_once()
        self.assertIn('remember this', str(model.await_args.kwargs['messages']))
        self.assertIn('预算', str(model.await_args.kwargs['messages']))
        self.assertNotIn('tools', model.await_args.kwargs)

    async def test_sensitive_results_and_images_are_not_persisted(self):
        store = Store(lambda s: s['phase'] == 'batch' and s['next_index'] == 1)
        secret = 'TEST_SECRET_VALUE'
        execute = AsyncMock(return_value=json.dumps({'password': secret, 'image': 'data:image/png;base64,AAAA'}))
        args = dict(user_text='x', history=[], tools=[tool('one')], execute_tool=execute,
                    checkpoint_sink=store.save, idempotency_key_prefix='turn-4')
        await self.interrupted(args, batch('one'))
        self.assertNotIn(secret, json.dumps(store.state))
        self.assertNotIn('base64', json.dumps(store.state))
        self.assertIn('不可用', store.state['messages'][-1]['content'])
        store.stop = None
        with patch('src.plugins.ai_chat.agent_loop._create_completion', new=AsyncMock(return_value=final())) as model:
            await ask_deepseek_with_tools(**args, checkpoint_state=store.state)
        self.assertIn('不可用', str(model.await_args.kwargs['messages']))
        execute.assert_awaited_once()

    async def test_image_payload_is_omitted_and_loss_is_reported(self):
        from src.plugins.ai_chat.tool_images import ToolImage
        store = Store(lambda s: s['phase'] == 'batch' and s['next_index'] == 1)
        execute = AsyncMock(return_value=ToolImage('caption', 'data:image/png;base64,TEST_IMAGE_DATA'))
        args = dict(user_text='x', history=[], tools=[tool('one')], execute_tool=execute,
                    checkpoint_sink=store.save, idempotency_key_prefix='turn-image')
        await self.interrupted(args, batch('one'))
        self.assertNotIn('TEST_IMAGE_DATA', json.dumps(store.state))
        store.stop = None
        with patch('src.plugins.ai_chat.agent_loop._create_completion', new=AsyncMock(return_value=final())) as model:
            await ask_deepseek_with_tools(**args, checkpoint_state=store.state)
        self.assertIn('图片未保存', str(model.await_args.kwargs['messages']))
        execute.assert_awaited_once()

    async def test_completed_fingerprint_blocks_repeat_in_later_round(self):
        store = Store(lambda s: s['phase'] == 'between')
        execute = AsyncMock(return_value='ok')
        args = dict(user_text='x', history=[], tools=[tool('say')], execute_tool=execute,
                    checkpoint_sink=store.save, idempotency_key_prefix='turn-repeat')
        await self.interrupted(args, batch('say'))
        store.stop = None
        with patch('src.plugins.ai_chat.agent_loop._create_completion',
                   new=AsyncMock(side_effect=[batch('say'), final()])) as model:
            await ask_deepseek_with_tools(**args, checkpoint_state=store.state)
        execute.assert_awaited_once()
        self.assertIn('重复', str(model.await_args.kwargs['messages']))
        self.assertNotIn('tools', model.await_args.kwargs)

    async def test_parallel_quiet_calls_remind_once_after_resume(self):
        names = tuple(f'tool-{index}' for index in range(13))
        store = Store(lambda state: state['phase'] == 'batch' and state['next_index'] == 11)
        execute = AsyncMock(side_effect=['ok'] * 12 + ['<&' * 12_000])
        args = dict(user_text='x', history=[], tools=[tool(name) for name in names],
                    execute_tool=execute, max_calls_per_round=24, foreground_quiet_calls=12,
                    checkpoint_sink=store.save, idempotency_key_prefix='turn-quiet')
        await self.interrupted(args, batch(*names))
        self.assertEqual(store.state['total_tool_calls'], 11)
        store.stop = lambda state: state['phase'] == 'between'
        with self.assertRaises(Stop):
            await ask_deepseek_with_tools(**args, checkpoint_state=store.state)
        self.assertEqual(sum(msg.get('quiet_notice', False) for msg in store.state['messages']), 1)
        store.stop = None
        with patch('src.plugins.ai_chat.agent_loop._create_completion',
                   new=AsyncMock(return_value=final())) as model:
            await ask_deepseek_with_tools(**args, checkpoint_state=store.state)
        tool_results = [message['content'] for message in model.await_args.kwargs['messages']
                        if message['role'] == 'tool']
        self.assertEqual(execute.await_count, 13)
        self.assertEqual(sum('[进度提醒]' in result for result in tool_results), 1)
        self.assertIn('[进度提醒]', tool_results[-1].split('</untrusted id=', 1)[1])

    async def test_receipt_warns_once_across_restart_and_unoffered_tool_cannot_run(self):
        profile = ModelProfile('TEST', 'test', 'openai-chat', 'TEST', '', max_input_tokens=20_000, max_output_tokens=10_000)
        store = Store(lambda s: s['phase'] == 'model' and s['tool_round'] == 2)
        execute = AsyncMock(return_value='ok')
        args = dict(user_text='x', history=[], system_prompt='TEST', profile=profile, execute_tool=execute,
                    tools=[tool('lookup'), tool('say')], foreground_quiet_calls=12, max_tool_rounds=40,
                    checkpoint_sink=store.save, idempotency_key_prefix='TEST_WRAP')
        response = batch('lookup')
        response.usage = {'prompt_tokens': 8100}  # 90% of (20k - 10k output - 1k margin).
        await self.interrupted(args, response)
        self.assertEqual(str(store.state['messages']).count('[前台收尾]'), 1)
        store.stop = None
        with patch('src.plugins.ai_chat.agent_loop._create_completion', new=AsyncMock(side_effect=[batch('lookup'), final()])) as model:
            await ask_deepseek_with_tools(**args, checkpoint_state=store.state)
        execute.assert_awaited_once()
        self.assertEqual(model.await_args_list[0].kwargs['tools'], [tool('say')])
        self.assertEqual(str(model.await_args.kwargs['messages']).count('[前台收尾]'), 1)
        self.assertNotIn('tools', model.await_args.kwargs)

    async def test_excess_input_never_dispatches_tools_or_an_oversized_final(self):
        profile = ModelProfile('TEST', 'test', 'openai-chat', 'TEST', '', max_input_tokens=8000, max_output_tokens=1000)
        for size, expected_calls in ((4300, 1), (7000, 0)):
            with self.subTest(size=size), patch('src.plugins.ai_chat.agent_loop._create_completion', new=AsyncMock(return_value=final())) as model:
                execute = AsyncMock()
                await ask_deepseek_with_tools('测' * size, [], [tool('say')], execute, system_prompt='TEST', profile=profile,
                                              foreground_quiet_calls=12, max_tool_rounds=40)
                execute.assert_not_called()
                self.assertEqual(model.await_count, expected_calls)
                if expected_calls:
                    self.assertNotIn('tools', model.await_args.kwargs)

    async def test_small_results_continue_past_sixteen_rounds(self):
        names = [f'tool-{i}' for i in range(18)]
        execute = AsyncMock(return_value='ok')
        with patch('src.plugins.ai_chat.agent_loop._create_completion', new=AsyncMock(side_effect=[*(batch(n) for n in names), final()])) as model:
            await ask_deepseek_with_tools('x', [], [tool(n) for n in names], execute, foreground_quiet_calls=12, max_tool_rounds=40)
        self.assertEqual(execute.await_count, 18)
        self.assertNotIn('[前台收尾]', str(model.await_args.kwargs['messages']))

    async def test_only_successful_say_resets_quiet_calls(self):
        names = [f'tool-{index}' for index in range(11)]
        for status in ('enqueued', 'already_reported', 'suppressed'):
            ok = status != 'suppressed'
            execute = AsyncMock(side_effect=['ok'] * 11 + [json.dumps({'ok': ok, 'status': status})])
            replies = [batch(*names), batch('say', start=11), final()]
            with self.subTest(status=status), patch('src.plugins.ai_chat.agent_loop._create_completion',
                    new=AsyncMock(side_effect=replies)) as model:
                await ask_deepseek_with_tools('x', [], [tool(n) for n in [*names, 'say']], execute,
                                              max_calls_per_round=24, foreground_quiet_calls=12)
                self.assertEqual(str(model.await_args.kwargs['messages']).count('[进度提醒]'), int(not ok))

    async def test_unsafe_assistant_and_final_are_not_resumable(self):
        store = Store(lambda s: s['phase'] == 'batch')
        call = batch('one')
        call.choices[0].message.tool_calls[0].function.arguments = '{"password":"TEST_SECRET"}'
        args = dict(user_text='x', history=[], tools=[tool('one')], execute_tool=AsyncMock(),
                    checkpoint_sink=store.save, idempotency_key_prefix='turn-5')
        await self.interrupted(args, call)
        self.assertNotIn('TEST_SECRET', json.dumps(store.state))
        self.assertFalse(store.state['resumable'])
        with self.assertRaisesRegex(RuntimeError, 'suppressed'):
            await ask_deepseek_with_tools(**args, checkpoint_state=store.state)
        store.stop = None
        with patch('src.plugins.ai_chat.agent_loop._create_completion', new=AsyncMock(return_value=final('password: TEST_SECRET'))):
            await ask_deepseek_with_tools(**args)
        self.assertFalse(store.state['resumable'])
        self.assertNotIn('TEST_SECRET', json.dumps(store.state))


class ProgressKeyTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_step_deduplicates_distinct_steps_do_not(self):
        keys = set()
        class Outbox:
            clock = staticmethod(lambda: 1)
            deliveries = NS(transaction=lambda: nullcontext(None))
            def enqueue(self, token, **kw):
                key = kw['logical_key']
                created = key not in keys
                keys.add(key)
                return NS(created=created)
        context = NS(source_key='source', endpoint=None, actor=None, scope_key='scope',
                     native_message_id='', canonical_message_id=None, deadline_at=100)
        async def calls(fn, *args):
            return fn(*args)
        emitter = ProgressEmitter(token=None, outbox=Outbox(), current=lambda *_: context, database_calls=calls)
        with patch('src.plugins.ai_chat.adapters.qq_official.progress.render_plan', return_value=None):
            for step, expected in ((1, 'enqueued'), (1, 'already_reported'), (2, 'enqueued')):
                token = active_tool_step.set(('turn', step))
                try:
                    self.assertEqual((await emitter.say('same'))['status'], expected)
                finally:
                    active_tool_step.reset(token)
        self.assertEqual(len(keys), 2)
