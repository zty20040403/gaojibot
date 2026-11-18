"""Deadline enforcement while container operations or host callbacks are unresponsive."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock

from src.plugins.ai_chat.agent_loop import AgentTrace
from src.plugins.ai_chat.pi_engine import DEADLINE_NOTE
import tests.test_pi_engine as pi_fixture


class StallGuest(pi_fixture.FakePiGuest):
    def __init__(self, *, stall: str = '', **kwargs) -> None:
        super().__init__(wait_for_steer=True, **kwargs)
        self.stall = stall
        self.blocked = asyncio.Event()

    async def exec(self, vm: str, script: str) -> str:
        if self.stall == 'steer' and 'steered_count' in script and 'inbox' in script:
            self.blocked.set()
            await asyncio.Future()
        if self.stall == 'launch' and 'GAOJI_JOB_EOF' in script:
            self.blocked.set()
            await asyncio.Future()
        return await super().exec(vm, script)


class PiDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def wait_event(self, event, message):
        try:
            await asyncio.wait_for(event.wait(), 2)
        except TimeoutError:
            self.fail(message)

    def make_engine(self, guest):
        fixture = pi_fixture.PiEngineTests()
        engine = fixture._engine(guest)
        engine.clock = asyncio.get_running_loop().time
        row = {'job_id': 77, 'task': 'TEST task', 'deadline_at': engine.clock() + 0.35}
        return fixture, engine, row

    async def test_stalled_steer_does_not_block_deadline_stop(self):
        guest = StallGuest(stall='steer')
        fixture, engine, row = self.make_engine(guest)
        task = asyncio.create_task(
            engine(
                row,
                fixture._session(),
                feedback=AsyncMock(return_value=['TEST steering']),
                progress=AsyncMock(),
                trace=AgentTrace(),
            )
        )
        try:
            await self.wait_event(guest.blocked, 'steer was not started')
            await self.wait_event(guest.stop_called, 'deadline did not stop a stalled steer')
            report = await asyncio.wait_for(task, 2)
            self.assertEqual(report, DEADLINE_NOTE + '\nTEST partial pi report')
            fixture.gateway.revoke.assert_called_once_with('TEST_TOKEN')
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_stalled_launch_does_not_block_deadline_stop(self):
        guest = StallGuest(stall='launch')
        fixture, engine, row = self.make_engine(guest)
        task = asyncio.create_task(
            engine(
                row,
                fixture._session(),
                feedback=AsyncMock(return_value=[]),
                progress=AsyncMock(),
                trace=AgentTrace(),
            )
        )
        try:
            await self.wait_event(guest.blocked, 'launch was not started')
            await self.wait_event(guest.stop_called, 'deadline did not stop a stalled launch')
            report = await asyncio.wait_for(task, 2)
            self.assertEqual(report, DEADLINE_NOTE + '\nTEST partial pi report')
            fixture.gateway.revoke.assert_called_once_with('TEST_TOKEN')
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_deadline_stops_while_feedback_cleanup_is_draining(self):
        guest = StallGuest()
        fixture, engine, row = self.make_engine(guest)
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def feedback():
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cleaning.set()
                await release.wait()

        task = asyncio.create_task(
            engine(row, fixture._session(), feedback=feedback, progress=AsyncMock(), trace=AgentTrace())
        )
        try:
            await self.wait_event(entered, 'feedback was not started')
            await self.wait_event(cleaning, 'deadline did not cancel feedback')
            await self.wait_event(guest.stop_called, 'deadline waited for feedback cleanup before stop')
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

    async def test_deadline_during_vm_start_revokes_and_drains(self):
        guest = StallGuest()
        fixture, engine, row = self.make_engine(guest)
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def start_vm(_row):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cleaning.set()
                await release.wait()

        engine.start_vm = start_vm
        task = asyncio.create_task(
            engine(row, fixture._session(), feedback=AsyncMock(return_value=[]), progress=AsyncMock(), trace=AgentTrace())
        )
        try:
            await self.wait_event(entered, 'VM startup was not started')
            await self.wait_event(cleaning, 'deadline did not cancel VM startup')
            self.assertFalse(task.done(), 'startup cleanup must be drained')
            release.set()
            report = await asyncio.wait_for(task, 2)
            self.assertTrue(report.startswith(DEADLINE_NOTE))
            fixture.exec.assert_not_awaited()
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_expired_job_does_not_start_vm(self):
        guest = StallGuest()
        fixture, engine, row = self.make_engine(guest)
        row['deadline_at'] = engine.clock() - 1
        report = await engine(
            row, fixture._session(), feedback=AsyncMock(return_value=[]), progress=AsyncMock(), trace=AgentTrace()
        )
        self.assertTrue(report.startswith(DEADLINE_NOTE))
        engine.start_vm.assert_not_awaited()
        fixture.exec.assert_not_awaited()
        fixture.gateway.start.assert_not_awaited()
        fixture.gateway.revoke.assert_not_called()

    async def test_cancel_suppressing_feedback_cannot_send_late_steer(self):
        guest = StallGuest()
        fixture, engine, row = self.make_engine(guest)
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def feedback():
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
                return ['must not be sent']

        task = asyncio.create_task(
            engine(row, fixture._session(), feedback=feedback, progress=AsyncMock(), trace=AgentTrace())
        )
        try:
            await self.wait_event(entered, 'feedback was not started')
            await self.wait_event(cancelled, 'deadline did not cancel feedback')
            await self.wait_event(guest.stop_called, 'deadline did not stop unit')
            release.set()
            report = await asyncio.wait_for(task, 2)
            self.assertTrue(report.startswith(DEADLINE_NOTE))
            self.assertEqual(guest.steers, [])
            fixture.gateway.revoke.assert_called_once_with('TEST_TOKEN')
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_deadline_preserves_partial_text_and_usage(self):
        guest = StallGuest(stall='steer', partial_text='TEST previous report')
        fixture, engine, row = self.make_engine(guest)
        trace = AgentTrace()
        task = asyncio.create_task(
            engine(
                row,
                fixture._session(),
                feedback=AsyncMock(return_value=['TEST steering']),
                progress=AsyncMock(),
                trace=trace,
            )
        )
        try:
            await self.wait_event(guest.blocked, 'steer was not started')
            await self.wait_event(guest.stop_called, 'deadline did not stop unit')
            report = await asyncio.wait_for(task, 2)
            self.assertEqual(report, DEADLINE_NOTE + '\nTEST previous report')
            self.assertEqual((trace.input_tokens, trace.output_tokens, trace.total_tokens), (11, 4, 15))
            fixture.gateway.revoke.assert_called_once_with('TEST_TOKEN')
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_repeated_external_cancel_drains_one_stop(self):
        entered, release, interrupted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        guest = StallGuest()

        async def slow_stop_exec(vm, script):
            if script.startswith('if command -v systemctl'):
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    interrupted.set()
                    raise
            return await guest.exec(vm, script)

        fixture, engine, row = self.make_engine(guest)
        engine.guest_exec = AsyncMock(side_effect=slow_stop_exec)
        row['deadline_at'] += 30
        started = asyncio.Event()

        async def progress(_name):
            started.set()

        task = asyncio.create_task(
            engine(row, fixture._session(), feedback=AsyncMock(return_value=[]), progress=progress, trace=AgentTrace())
        )
        try:
            await self.wait_event(started, 'Pi did not start')
            task.cancel()
            await self.wait_event(entered, 'cancellation did not call stop')
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done(), 'repeated cancellation interrupted stop cleanup')
            self.assertFalse(interrupted.is_set())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            stop_calls = [c for c in engine.guest_exec.call_args_list if c.args[1].startswith('if command -v systemctl')]
            self.assertEqual(len(stop_calls), 1)
            fixture.gateway.revoke.assert_called_once_with('TEST_TOKEN')
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_startup_crash_before_deadline_is_not_a_deadline(self):
        guest = pi_fixture.FakePiGuest(crash_on_start=True)
        fixture, engine, row = self.make_engine(guest)
        row['deadline_at'] += 30
        with self.assertRaisesRegex(RuntimeError, '后台进程没能启动'):
            await engine(
                row, fixture._session(), feedback=AsyncMock(return_value=[]), progress=AsyncMock(), trace=AgentTrace()
            )
        fixture.gateway.revoke.assert_called_once_with('TEST_TOKEN')
