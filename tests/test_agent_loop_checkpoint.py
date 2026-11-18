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


def tool(name):
    return {'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object'}}}


def batch(*names):
    calls = [NS(id=f'call-{i}', function=NS(name=name, arguments='{}')) for i, name in enumerate(names)]
    return NS(choices=[NS(message=NS(content='', tool_calls=calls))])


def final(text='done'):
    return NS(choices=[NS(message=NS(content=text, tool_calls=[]))])


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
                    execute_tool=interrupted, checkpoint_sink=store.save, idempotency_key_prefix='turn-2')
        await self.interrupted(args, batch('shell', 'other'), asyncio.CancelledError)
        self.assertEqual(store.state['phase'], 'inflight')
        execute = AsyncMock()
        with patch('src.plugins.ai_chat.agent_loop._create_completion', new=AsyncMock(return_value=final('unknown'))) as model:
            answer = await ask_deepseek_with_tools(**{**args, 'execute_tool': execute}, checkpoint_state=store.state)
        self.assertEqual(answer, 'unknown')
        execute.assert_not_awaited()
        request = model.await_args.kwargs
        self.assertNotIn('tools', request)
        self.assertIn('outcome', str(request['messages']))
        self.assertIn('停止后续', str(request['messages']))

    async def test_feedback_and_budget_survive_resume(self):
        store = Store(lambda s: s['phase'] == 'batch' and s['next_index'] == 1)
        feedback = AsyncMock(side_effect=[['remember this'], []])
        execute = AsyncMock(return_value='ok')
        args = dict(user_text='x', history=[], tools=[tool('one'), tool('two')], execute_tool=execute,
                    max_total_calls=1, feedback_provider=feedback, checkpoint_sink=store.save, idempotency_key_prefix='turn-3')
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
