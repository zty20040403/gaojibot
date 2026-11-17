"""Deadline enforcement while the Pi RPC or a host operation is unresponsive."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock

from src.plugins.ai_chat.agent_loop import AgentTrace
from src.plugins.ai_chat.pi_engine import DEADLINE_NOTE
import tests.test_pi_engine as pi_fixture


class AbortServer(pi_fixture.FakePiRpcServer):
    def __init__(self, *, stall: str = '') -> None:
        super().__init__()
        self.stall = stall
        self.blocked = asyncio.Event()
        self.abort_sent = asyncio.Event()

    async def send(self, message):
        kind = message.get('type')
        if kind == self.stall:
            self.requests.append(message)
            self.blocked.set()
            return  # Pi accepted the bytes but never acknowledged the command.
        if kind == 'abort':
            self.abort_sent.set()
        await super().send(message)


class PiDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def wait_event(self, event, message):
        try:
            await asyncio.wait_for(event.wait(), 2)
        except TimeoutError:
            self.fail(message)

    def make_engine(self, server):
        fixture = pi_fixture.PiEngineTests()
        engine = fixture._engine(server)
        engine.clock = asyncio.get_running_loop().time
        row = {'job_id': 77, 'task': 'TEST task', 'deadline_at': engine.clock() + 0.5}
        return fixture, engine, row

    async def test_stalled_steer_does_not_block_deadline_abort(self):
        server = AbortServer(stall='steer')
        fixture, engine, row = self.make_engine(server)
        task = asyncio.create_task(engine(row, fixture._session(),
            feedback=AsyncMock(return_value=['TEST steering']), progress=AsyncMock(), trace=AgentTrace()))
        try:
            await self.wait_event(server.blocked, 'steer was not sent')
            await self.wait_event(server.abort_sent, 'deadline did not abort a stalled steer')
            report = await asyncio.wait_for(task, 2)
            self.assertEqual(report, DEADLINE_NOTE + '\nTEST partial pi report')
            self.assertEqual([r['type'] for r in server.requests], ['prompt', 'steer', 'abort'])
            fixture.gateway.revoke.assert_called_once_with('TEST_TOKEN')
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_stalled_prompt_ack_does_not_block_deadline_abort(self):
        server = AbortServer(stall='prompt')
        fixture, engine, row = self.make_engine(server)
        task = asyncio.create_task(engine(row, fixture._session(),
            feedback=AsyncMock(return_value=[]), progress=AsyncMock(), trace=AgentTrace()))
        try:
            await self.wait_event(server.blocked, 'prompt was not sent')
            await self.wait_event(server.abort_sent, 'deadline did not abort a stalled prompt')
            report = await asyncio.wait_for(task, 2)
            self.assertEqual(report, DEADLINE_NOTE + '\nTEST partial pi report')
            self.assertEqual([r['type'] for r in server.requests], ['prompt', 'abort'])
            fixture.gateway.revoke.assert_called_once_with('TEST_TOKEN')
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_deadline_aborts_while_feedback_cleanup_is_draining(self):
        server = AbortServer()
        fixture, engine, row = self.make_engine(server)
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def feedback():
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cleaning.set()
                await release.wait()
        task = asyncio.create_task(engine(row, fixture._session(),
            feedback=feedback, progress=AsyncMock(), trace=AgentTrace()))
        try:
            await self.wait_event(entered, 'feedback was not started')
            await self.wait_event(cleaning, 'deadline did not cancel feedback')
            await self.wait_event(server.abort_sent, 'deadline waited for feedback cleanup before abort')
            self.assertFalse(task.done(), 'feedback cleanup must be drained')
            release.set()
            report = await asyncio.wait_for(task, 2)
            self.assertEqual(report, DEADLINE_NOTE + '\nTEST partial pi report')
            fixture.gateway.revoke.assert_called_once_with('TEST_TOKEN')
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_deadline_during_vm_start_revokes_before_draining(self):
        server = AbortServer()
        fixture, engine, row = self.make_engine(server)
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def start_vm(_row):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cleaning.set()
                await release.wait()
        engine.start_vm = start_vm
        task = asyncio.create_task(engine(row, fixture._session(),
            feedback=AsyncMock(return_value=[]), progress=AsyncMock(), trace=AgentTrace()))
        try:
            await self.wait_event(entered, 'VM startup was not started')
            await self.wait_event(cleaning, 'deadline did not cancel VM startup')
            self.assertFalse(task.done(), 'startup cleanup must be drained')
            fixture.gateway.revoke.assert_called_once_with('TEST_TOKEN')
            release.set()
            report = await asyncio.wait_for(task, 2)
            self.assertTrue(report.startswith(DEADLINE_NOTE))
            self.assertEqual(server.requests, [])
            fixture.exec.assert_not_awaited()
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_deadline_during_tunnel_wait_revokes_and_never_prompts(self):
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class LateTunnel(AbortServer):
            async def wait(self, _timeout):
                entered.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    cleaning.set()
                    await release.wait()
        server = LateTunnel()
        fixture, engine, row = self.make_engine(server)
        task = asyncio.create_task(engine(row, fixture._session(),
            feedback=AsyncMock(return_value=[]), progress=AsyncMock(), trace=AgentTrace()))
        try:
            await self.wait_event(entered, 'tunnel wait was not started')
            await self.wait_event(cleaning, 'deadline did not cancel tunnel wait')
            fixture.gateway.revoke.assert_called_once_with('TEST_TOKEN')
            self.assertFalse(task.done(), 'tunnel cleanup must be drained')
            release.set()
            self.assertTrue((await asyncio.wait_for(task, 2)).startswith(DEADLINE_NOTE))
            self.assertEqual(server.requests, [])
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_expired_job_does_not_start_vm(self):
        server = AbortServer()
        fixture, engine, row = self.make_engine(server)
        row['deadline_at'] = engine.clock() - 1
        report = await engine(row, fixture._session(), feedback=AsyncMock(return_value=[]),
            progress=AsyncMock(), trace=AgentTrace())
        self.assertTrue(report.startswith(DEADLINE_NOTE))
        engine.start_vm.assert_not_awaited()
        fixture.exec.assert_not_awaited()
        fixture.gateway.start.assert_not_awaited()
        fixture.gateway.revoke.assert_not_called()

    async def test_cancel_suppressing_feedback_cannot_send_late_steer(self):
        server = AbortServer()
        fixture, engine, row = self.make_engine(server)
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def feedback():
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
                return ['must not be sent']
        task = asyncio.create_task(engine(row, fixture._session(), feedback=feedback,
            progress=AsyncMock(), trace=AgentTrace()))
        try:
            await self.wait_event(entered, 'feedback was not started')
            await self.wait_event(cancelled, 'deadline did not cancel feedback')
            await self.wait_event(server.abort_sent, 'deadline did not send abort')
            release.set()
            report = await asyncio.wait_for(task, 2)
            self.assertTrue(report.startswith(DEADLINE_NOTE))
            self.assertEqual([r['type'] for r in server.requests], ['prompt', 'abort'])
            fixture.gateway.revoke.assert_called_once_with('TEST_TOKEN')
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_deadline_preserves_previously_received_text_and_usage(self):
        class Partial(AbortServer):
            async def send(self, message):
                if message.get('type') == 'abort':
                    self.requests.append(message)
                    self.abort_sent.set()
                    self.push({'id': message['id'], 'type': 'response', 'success': True})
                    self.push({'type': 'agent_settled', 'aborted': True})
                    return
                await super().send(message)
                if message.get('type') == 'prompt':
                    self.push({'type': 'message_end', 'message': {'role': 'assistant',
                        'content': [{'type': 'text', 'text': 'TEST previous report'}],
                        'usage': {'input': 11, 'output': 4, 'totalTokens': 15}, 'stopReason': 'stop'}})
        server = Partial(stall='steer')
        fixture, engine, row = self.make_engine(server)
        trace = AgentTrace()
        task = asyncio.create_task(engine(row, fixture._session(),
            feedback=AsyncMock(return_value=['TEST steering']), progress=AsyncMock(), trace=trace))
        try:
            await self.wait_event(server.blocked, 'steer was not sent')
            await self.wait_event(server.abort_sent, 'deadline did not send abort')
            report = await asyncio.wait_for(task, 2)
            self.assertEqual(report, DEADLINE_NOTE + '\nTEST previous report')
            self.assertEqual((trace.input_tokens, trace.output_tokens, trace.total_tokens), (11, 4, 15))
            fixture.gateway.revoke.assert_called_once_with('TEST_TOKEN')
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_repeated_external_cancel_drains_one_abort(self):
        entered, release, interrupted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class SlowAbort(AbortServer):
            async def send(self, message):
                if message.get('type') == 'abort':
                    entered.set()
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        interrupted.set()
                        raise
                await super().send(message)
        server = SlowAbort()
        fixture, engine, row = self.make_engine(server)
        row['deadline_at'] += 30
        started = asyncio.Event()
        async def progress(_name):
            started.set()
        task = asyncio.create_task(engine(row, fixture._session(),
            feedback=AsyncMock(return_value=[]), progress=progress, trace=AgentTrace()))
        try:
            await self.wait_event(started, 'Pi did not start')
            task.cancel()
            await self.wait_event(entered, 'cancellation did not send abort')
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done(), 'repeated cancellation interrupted abort cleanup')
            self.assertFalse(interrupted.is_set())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual([r['type'] for r in server.requests].count('abort'), 1)
            fixture.gateway.revoke.assert_called_once_with('TEST_TOKEN')
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_transport_timeout_before_deadline_is_not_a_deadline(self):
        class Disconnected(AbortServer):
            async def wait(self, timeout):
                raise TimeoutError('TEST tunnel timed out')
        server = Disconnected()
        fixture, engine, row = self.make_engine(server)
        row['deadline_at'] += 30
        with self.assertRaisesRegex(TimeoutError, 'TEST tunnel timed out'):
            await engine(row, fixture._session(), feedback=AsyncMock(return_value=[]),
                progress=AsyncMock(), trace=AgentTrace())
        fixture.gateway.revoke.assert_called_once_with('TEST_TOKEN')
