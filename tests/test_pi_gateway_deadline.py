"""Deadline and cancellation while the shared local gateway starts."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.plugins.ai_chat.agent_loop import AgentTrace
from src.plugins.ai_chat.pi_engine import DEADLINE_NOTE
from src.plugins.ai_chat.job_gateway import JobGateway
from src.plugins.ai_chat.pi_engine import PiEngine
from tests.test_job_gateway import free_port
import tests.test_pi_engine as pi_fixture


class PiGatewayDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_unresponsive_gateway_start_cannot_block_deadline(self):
        server = pi_fixture.FakePiRpcServer()
        fixture = pi_fixture.PiEngineTests()
        engine = fixture._engine(server)
        engine.clock = asyncio.get_running_loop().time
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def start():
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        fixture.gateway.start = AsyncMock(side_effect=start)
        fixture.gateway.open = Mock(return_value=('TEST_TOKEN', server))
        task = asyncio.create_task(engine(
            {'job_id': 81, 'task': 'TEST', 'deadline_at': engine.clock() + 0.5}, fixture._session(),
            feedback=AsyncMock(return_value=[]), progress=AsyncMock(), trace=AgentTrace()))
        try:
            await asyncio.wait_for(started.wait(), 2)
            try:
                report = await asyncio.wait_for(task, 2)
            except TimeoutError:
                self.fail('deadline waited indefinitely for gateway startup')
            self.assertEqual(report, DEADLINE_NOTE + '\nPi 未启动，未创建会话记录。')
            self.assertTrue(cancelled.is_set())
            fixture.gateway.open.assert_not_called()
            fixture.gateway.revoke.assert_not_called()
            engine.start_vm.assert_not_awaited()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_external_repeat_cancel_drains_gateway_start_cleanup(self):
        server = pi_fixture.FakePiRpcServer()
        fixture = pi_fixture.PiEngineTests()
        engine = fixture._engine(server)
        started, cleaning, release, finished = (asyncio.Event() for _ in range(4))
        async def start():
            started.set()
            try:
                await asyncio.Future()
            finally:
                cleaning.set()
                await release.wait()
                finished.set()
        fixture.gateway.start = AsyncMock(side_effect=start)
        task = asyncio.create_task(engine({'job_id': 82, 'task': 'TEST', 'deadline_at': 7200},
            fixture._session(), feedback=AsyncMock(return_value=[]), progress=AsyncMock(), trace=AgentTrace()))
        try:
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            await asyncio.wait_for(cleaning.wait(), 2)
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done(), 'repeated cancellation interrupted gateway cleanup')
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(finished.is_set())
            fixture.gateway.revoke.assert_not_called()
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_gateway_transport_timeout_is_not_deadline(self):
        fixture = pi_fixture.PiEngineTests()
        engine = fixture._engine(pi_fixture.FakePiRpcServer())
        fixture.gateway.start = AsyncMock(side_effect=TimeoutError('TEST gateway failure'))
        with self.assertRaisesRegex(TimeoutError, 'TEST gateway failure'):
            await engine({'job_id': 83, 'task': 'TEST', 'deadline_at': 7200}, fixture._session(),
                feedback=AsyncMock(return_value=[]), progress=AsyncMock(), trace=AgentTrace())

    async def test_gateway_reports_serve_exit_before_readiness(self):
        class EarlyServer:
            def __init__(self, _config):
                self.started = self.should_exit = False
            async def serve(self):
                return
        gateway = JobGateway(port=free_port(), upstream=lambda: None,
            model=lambda: ('TEST_MODEL', 'high'), logger=SimpleNamespace())
        with patch('src.plugins.ai_chat.job_gateway._Server', EarlyServer):
            try:
                with self.assertRaisesRegex(RuntimeError, 'before startup'):
                    try:
                        await asyncio.wait_for(gateway.start(), 1)
                    except TimeoutError:
                        self.fail('gateway waited forever after its HTTP task exited')
            finally:
                await gateway.close()

    async def test_deadline_then_another_job_waits_for_shared_gateway(self):
        started, release = asyncio.Event(), asyncio.Event()
        class DelayedServer:
            def __init__(self, _config):
                self.started = self.should_exit = False
            async def serve(self):
                started.set()
                await release.wait()
                self.started = True
                while not self.should_exit:
                    await asyncio.sleep(0.01)
        gateway = JobGateway(port=free_port(), upstream=lambda: None,
            model=lambda: ('TEST_MODEL', 'high'), logger=SimpleNamespace())
        fixture = pi_fixture.PiEngineTests()
        engine = PiEngine(gateway, SimpleNamespace(get=lambda: ('TEST_MODEL', 'high')),
            start_vm=AsyncMock(), guest_exec=AsyncMock(), clock=asyncio.get_running_loop().time)
        with patch('src.plugins.ai_chat.job_gateway._Server', DelayedServer):
            try:
                row = {'job_id': 84, 'task': 'TEST', 'deadline_at': engine.clock() + 0.5}
                report = await asyncio.wait_for(engine(row, fixture._session(), feedback=AsyncMock(),
                    progress=AsyncMock(), trace=AgentTrace()), 2)
                self.assertTrue(report.startswith(DEADLINE_NOTE))
                self.assertTrue(started.is_set())
                self.assertEqual(gateway.channels, {})
                next_start = asyncio.create_task(gateway.start())
                await asyncio.sleep(0.05)
                self.assertFalse(next_start.done(), 'next job must wait for the shared HTTP server')
                release.set()
                await asyncio.wait_for(next_start, 2)
                self.assertTrue(gateway.http.started)
            finally:
                release.set()
                await gateway.close()

    async def test_gateway_restart_waits_for_initial_http_start(self):
        started, release = asyncio.Event(), asyncio.Event()
        class DelayedServer:
            def __init__(self, _config):
                self.started = self.should_exit = False
            async def serve(self):
                started.set()
                await release.wait()
                self.started = True
                while not self.should_exit:
                    await asyncio.sleep(0.01)
        gateway = JobGateway(port=free_port(), upstream=lambda: None,
            model=lambda: ('TEST_MODEL', 'high'), logger=SimpleNamespace())
        with patch('src.plugins.ai_chat.job_gateway._Server', DelayedServer):
            first = asyncio.create_task(gateway.start())
            try:
                await asyncio.wait_for(started.wait(), 2)
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                second = asyncio.create_task(gateway.start())
                await asyncio.sleep(0.05)
                self.assertFalse(second.done(), 'gateway reported ready before HTTP startup completed')
                release.set()
                await asyncio.wait_for(second, 2)
                self.assertTrue(gateway.http.started)
            finally:
                release.set()
                if not first.done():
                    first.cancel()
                    await asyncio.gather(first, return_exceptions=True)
                await gateway.close()
