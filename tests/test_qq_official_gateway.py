from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
import unittest

from src.qq_official.credentials import Credentials
from src.qq_official.errors import QQAPIError
from src.qq_official.gateway import (
    GatewayBackpressureError, GatewayCallbackError, GatewayCheckpoint, GatewayConnection, GatewayDisconnected,
    GatewayError, IdentifyBudget, classify_close_code,
)
from src.qq_official.http import RestClient
from src.qq_official.testing.fake_platform import (
    FAKE_DUPLICATE_SEQUENCE, FAKE_UNKNOWN_WINDOW, FakePlatform,
)
from src.qq_official.token import TokenManager


class FastClock:
    """Advance retry delays, leave long heartbeat/Hello timers pending."""
    def __init__(self):
        self.now = 0.0
        self.delays = []

    def __call__(self):
        return self.now

    async def sleep(self, delay):
        if delay >= 30:
            await asyncio.Event().wait()
        else:
            self.delays.append(delay)
            self.now += delay


class ManualClock:
    def __init__(self):
        self.now = 0.0
        self.timers = []

    def __call__(self):
        return self.now

    async def sleep(self, delay):
        future = asyncio.get_running_loop().create_future()
        self.timers.append((self.now + delay, future))
        await future

    async def tick(self, seconds):
        self.now += seconds
        for deadline, future in self.timers:
            if deadline <= self.now and not future.done():
                future.set_result(None)
        # Yield only for asyncio scheduling, never wait for wall-clock time.
        for _ in range(5):
            await asyncio.sleep(0)


class StubToken:
    credentials = Credentials("TEST_APP_ID", "TEST_APP_SECRET")
    def __init__(self):
        self.invalidations = 0

    async def get(self):
        return "TEST_ACCESS_TOKEN"

    def invalidate(self, token=None):
        self.invalidations += 1


class StubRest:
    allow_test_server = True
    def __init__(self):
        self.token_manager = StubToken()
        self.queries = 0
        self.errors = deque()

    async def gateway(self):
        self.queries += 1
        if self.errors:
            raise self.errors.popleft()
        return {"url": "ws://127.0.0.1:10000/ws", "session_start_limit": {
            "remaining": 100, "reset_after": 60_000, "max_concurrency": 1}}


class ScriptSocket:
    def __init__(self, frames):
        self.frames = deque(frames)
        self.sent = []
        self.closed = False
        self.waiter = asyncio.Event()

    async def receive(self):
        await asyncio.sleep(0)
        if self.frames:
            frame = self.frames.popleft()
            if isinstance(frame, Exception):
                raise frame
            return frame
        await self.waiter.wait()
        raise GatewayDisconnected(1000)

    async def send(self, frame):
        self.sent.append(frame)

    async def close(self):
        self.closed = True
        self.waiter.set()


class PushedSocket(ScriptSocket):
    def __init__(self, frames):
        super().__init__([])
        self.incoming = asyncio.Queue()
        for frame in frames:
            self.incoming.put_nowait(frame)

    async def receive(self):
        frame = await self.incoming.get()
        if isinstance(frame, Exception):
            raise frame
        return frame

    async def close(self):
        self.closed = True
        self.incoming.put_nowait(GatewayDisconnected(1000))


def connector_for(sockets):
    pending = deque(sockets)
    @asynccontextmanager
    async def connect(url):
        socket = pending.popleft()
        try:
            yield socket
        finally:
            await socket.close()
    return connect


HELLO = {"op": 10, "d": {"heartbeat_interval": 60_000}}
READY = {"op": 0, "t": "READY", "s": 0, "d": {"session_id": "TEST_SESSION"}}


class GatewayPolicyTests(unittest.TestCase):
    def test_close_codes_are_table_driven_and_conservative(self):
        rows = [(4004, "refresh", True), (4006, "identify", True), (4007, "identify", True),
                (4009, "resume", False), (4008, "resume", False), (1000, "resume", False),
                (1006, "resume", False), (4999, "resume", False)]
        rows += [(n, "identify", True) for n in range(4900, 4914)]
        rows += [(n, "stop", False) for n in (4001, 4002, 4010, 4011, 4012, 4013, 4014, 4914, 4915)]
        for code, action, gap in rows:
            with self.subTest(code=code):
                policy = classify_close_code(code)
                self.assertEqual((policy.action, policy.gap), (action, gap))
        self.assertIn("disagree", classify_close_code(4914).reason)
        self.assertEqual(classify_close_code(4008).minimum_delay, 60)

    def test_budget_does_not_restore_consumed_stale_observation(self):
        budget = IdentifyBudget()
        raw = {"remaining": 2, "reset_after": 10_000, "max_concurrency": 4}
        budget.update(raw, 0)
        budget.consume(0)
        budget.update(raw, 1)
        self.assertEqual(budget.remaining, 1)
        self.assertEqual(budget.delay(1), 4)
        budget.consume(5)
        self.assertEqual(budget.delay(6), 4)
        budget.update({**raw, "remaining": 7}, 10)
        self.assertEqual(budget.remaining, 7)
        self.assertEqual(budget.max_concurrency, 4)

    def test_invalid_budgets_and_checkpoints_fail_closed(self):
        for field, value in (("remaining", -1), ("remaining", True), ("max_concurrency", 0),
                             ("reset_after", float("nan"))):
            raw = {"remaining": 1, "reset_after": 10_000, "max_concurrency": 1}
            raw[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(GatewayError):
                IdentifyBudget().update(raw, 0)
        with self.assertRaises(ValueError):
            GatewayCheckpoint("", 0)
        self.assertNotIn("TEST_SESSION", repr(GatewayCheckpoint("TEST_SESSION", 0)))


class GatewayUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_resumed_can_advance_over_unreplayed_sequences(self):
        socket = ScriptSocket([HELLO, {"op": 0, "t": "RESUMED", "s": 141, "d": {}},
                               {"op": 0, "t": "GROUP_AT_MESSAGE_CREATE", "s": 142, "d": {}}])
        clock, observed, checkpoints = FastClock(), [], []
        async def save(value):
            checkpoints.append(value.seq)
        gateway = GatewayConnection(StubRest(), checkpoint=GatewayCheckpoint("TEST_SESSION", 108),
                                    on_checkpoint=save, connector=connector_for([socket]),
                                    clock=clock, sleep=clock.sleep, max_reconnects=0)
        async def callback(frame):
            observed.append(frame["s"])
            self.assertEqual(gateway.state, "ready")
            await gateway.stop()
        await gateway.run(callback)
        self.assertEqual(observed, [142])
        self.assertEqual(checkpoints, [141, 142])
        self.assertFalse(gateway.message_gap)

    async def test_callback_failure_does_not_advance_resume(self):
        event = {"op": 0, "t": "GROUP_MESSAGE_CREATE", "s": 1, "id": "TEST_EVENT_1", "d": {"id": "TEST_MESSAGE_1"}}
        socket = ScriptSocket([HELLO, READY, event])
        clock = FastClock()
        gateway = GatewayConnection(StubRest(), connector=connector_for([socket]), clock=clock, sleep=clock.sleep)
        stored = []
        async def callback(frame):
            if frame["t"] != "READY":
                raise RuntimeError("TEST_SECRET_SHOULD_NOT_LEAK")
            stored.append(frame)
        with self.assertRaises(GatewayCallbackError) as caught:
            await gateway.run(callback)
        self.assertEqual(gateway.checkpoint.seq, 0)
        self.assertEqual(gateway.health["reason"], "durable_callback_failed")
        self.assertNotIn("TEST_SECRET", str(caught.exception))
        self.assertEqual(len(stored), 1)

    async def test_checkpoint_write_failure_does_not_publish_in_memory_seq(self):
        clock = FastClock()
        socket = ScriptSocket([HELLO, READY])
        async def checkpoint(_):
            raise OSError("TEST_SECRET")
        gateway = GatewayConnection(StubRest(), connector=connector_for([socket]),
                                    clock=clock, sleep=clock.sleep, on_checkpoint=checkpoint)
        async def callback(_):
            pass
        with self.assertRaises(GatewayCallbackError):
            await gateway.run(callback)
        self.assertIsNone(gateway.checkpoint)

    async def test_op7_resumes_from_acknowledged_seq_and_preserves_envelope(self):
        frame = {"op": 0, "t": "GROUP_MESSAGE_CREATE", "s": 1, "id": "TEST_EVENT", "d": {"id": "TEST_MESSAGE"}}
        a = ScriptSocket([HELLO, READY, frame, {"op": 7}])
        b = ScriptSocket([HELLO, {"op": 0, "t": "RESUMED", "s": 2, "d": {}}])
        clock = FastClock()
        gateway = GatewayConnection(StubRest(), connector=connector_for([a, b]), clock=clock, sleep=clock.sleep)
        delivered = []
        async def saved(checkpoint):
            if checkpoint is not None and checkpoint.seq == 2:
                await gateway.stop()
        gateway.on_checkpoint = saved
        async def callback(event):
            delivered.append(event)
        await gateway.run(callback)
        self.assertEqual(delivered, [READY, frame])
        self.assertEqual(b.sent[0], {"op": 6, "d": {"token": "QQBot TEST_ACCESS_TOKEN", "session_id": "TEST_SESSION", "seq": 1}})
        self.assertEqual(gateway.checkpoint.seq, 2)

    async def test_invalid_resume_clears_durable_checkpoint_and_identifies(self):
        a = ScriptSocket([HELLO, {"op": 9, "d": False}])
        b = ScriptSocket([HELLO, READY])
        clock, checkpoints = FastClock(), []
        async def save(value):
            checkpoints.append(value)
        gateway = GatewayConnection(StubRest(), checkpoint=GatewayCheckpoint("TEST_OLD", 5),
                                    on_checkpoint=save, connector=connector_for([a, b]), clock=clock, sleep=clock.sleep)
        async def callback(_):
            await gateway.stop()
        await gateway.run(callback)
        self.assertEqual(a.sent[0]["op"], 6)
        self.assertEqual(b.sent[0]["op"], 2)
        self.assertIsNone(checkpoints[0])
        self.assertTrue(gateway.message_gap)

    async def test_seq_gap_cannot_advance_checkpoint(self):
        event = {"op": 0, "t": "GROUP_MESSAGE_CREATE", "s": 2, "d": {}}
        socket = ScriptSocket([HELLO, READY, event])
        clock, observed = FastClock(), []
        gateway = GatewayConnection(StubRest(), connector=connector_for([socket]),
                                    clock=clock, sleep=clock.sleep, max_reconnects=0)
        async def callback(frame):
            observed.append(frame)
        await gateway.run(callback)
        self.assertEqual([e["s"] for e in observed], [0])
        self.assertEqual(gateway.checkpoint, GatewayCheckpoint("TEST_SESSION", 0))
        self.assertIsNone(gateway.last_close_code)
        self.assertTrue(gateway.message_gap)

    async def test_local_sequence_gap_resumes_and_recovers_missing_dispatch(self):
        first = {"op": 0, "t": "GROUP_MESSAGE_CREATE", "s": 1, "id": "TEST_EVENT_1", "d": {}}
        second = {"op": 0, "t": "GROUP_MESSAGE_CREATE", "s": 2, "id": "TEST_EVENT_2", "d": {}}
        a = ScriptSocket([HELLO, READY, second])
        b = ScriptSocket([HELLO, first, second, {"op": 0, "t": "RESUMED", "s": 3, "d": {}}])
        clock, observed, checkpoints = FastClock(), [], []
        async def save(checkpoint):
            checkpoints.append(checkpoint)
            if checkpoint is not None and checkpoint.seq == 3:
                await gateway.stop()
        gateway = GatewayConnection(StubRest(), connector=connector_for([a, b]),
                                    clock=clock, sleep=clock.sleep, on_checkpoint=save)
        async def callback(frame):
            observed.append(frame)
        await gateway.run(callback)
        self.assertEqual([frame["s"] for frame in observed], [0, 1, 2])
        self.assertEqual([checkpoint.seq for checkpoint in checkpoints], [0, 1, 2, 3])
        self.assertEqual(b.sent[0]["op"], 6)
        self.assertEqual(b.sent[0]["d"]["seq"], 0)
        self.assertEqual(b.sent[0]["d"]["session_id"], "TEST_SESSION")
        self.assertEqual(gateway.checkpoint.seq, 3)

    async def test_unexpected_ready_during_resume_cannot_mix_sessions(self):
        socket = ScriptSocket([HELLO, READY])
        clock = FastClock()
        gateway = GatewayConnection(StubRest(), checkpoint=GatewayCheckpoint("TEST_OLD", 20),
                                    connector=connector_for([socket]), clock=clock, sleep=clock.sleep)
        async def callback(_):
            self.fail("unexpected READY must not reach durable consumer")
        with self.assertRaises(GatewayError):
            await gateway.run(callback)
        self.assertEqual(gateway.checkpoint, GatewayCheckpoint("TEST_OLD", 20))

    async def test_4009_tries_resume_but_auth_failure_is_bounded(self):
        for code, expected_op, expected_invalidations in ((4009, 6, 0), (4004, 2, 1)):
            with self.subTest(code=code):
                a = ScriptSocket([HELLO, GatewayDisconnected(code)])
                ready = READY if expected_op == 2 else {"op": 0, "t": "RESUMED", "s": 1, "d": {}}
                b = ScriptSocket([HELLO, ready])
                clock, rest = FastClock(), StubRest()
                gateway = GatewayConnection(rest, checkpoint=GatewayCheckpoint("TEST_SESSION", 0),
                                            connector=connector_for([a, b]), clock=clock, sleep=clock.sleep)
                async def saved(value):
                    if value is not None:
                        await gateway.stop()
                gateway.on_checkpoint = saved
                async def callback(_):
                    pass
                await gateway.run(callback)
                self.assertEqual(b.sent[0]["op"], expected_op)
                self.assertEqual(rest.token_manager.invalidations, expected_invalidations)
        sockets = [ScriptSocket([HELLO, GatewayDisconnected(4004)]) for _ in range(2)]
        clock, rest = FastClock(), StubRest()
        gateway = GatewayConnection(rest, connector=connector_for(sockets), clock=clock, sleep=clock.sleep)
        await gateway.run(callback)
        self.assertEqual(gateway.reason, "authentication_retry_exhausted")
        self.assertEqual(rest.token_manager.invalidations, 1)

    async def test_old_gateway_4004_does_not_invalidate_concurrently_refreshed_token(self):
        class RacingToken(StubToken):
            current = "TEST_OLD"
            invalidated_current = False
            async def get(self):
                used = self.current
                self.current = "TEST_NEW"
                return used
            def invalidate(self, token=None):
                if token is None or token == self.current:
                    self.invalidated_current = True
        rest, clock = StubRest(), FastClock()
        rest.token_manager = RacingToken()
        a = ScriptSocket([HELLO, GatewayDisconnected(4004)])
        b = ScriptSocket([HELLO, READY])
        gateway = GatewayConnection(rest, connector=connector_for([a, b]), clock=clock, sleep=clock.sleep)
        async def callback(_):
            await gateway.stop()
        await gateway.run(callback)
        self.assertEqual(a.sent[0]["d"]["token"], "QQBot TEST_OLD")
        self.assertEqual(b.sent[0]["d"]["token"], "QQBot TEST_NEW")
        self.assertFalse(rest.token_manager.invalidated_current)

    async def test_exhausted_identify_budget_waits_and_refetches(self):
        rest, clock = StubRest(), FastClock()
        original = rest.gateway
        async def discovery():
            response = await original()
            response["session_start_limit"].update(
                remaining=0 if rest.queries == 1 else 2, reset_after=5_000,
                max_concurrency=3)
            return response
        rest.gateway = discovery
        socket = ScriptSocket([HELLO, READY])
        gateway = GatewayConnection(rest, connector=connector_for([socket]), clock=clock, sleep=clock.sleep)
        async def callback(_):
            await gateway.stop()
        await gateway.run(callback)
        self.assertEqual(rest.queries, 2)
        self.assertEqual(clock.delays, [5])
        self.assertEqual(gateway.budget.max_concurrency, 3)
        self.assertEqual(gateway.budget.remaining, 1)

    async def test_missing_ready_is_bounded_even_with_ack_capable_connection(self):
        clock = ManualClock()
        gateway = GatewayConnection(StubRest(), clock=clock, sleep=clock.sleep)
        task = asyncio.create_task(gateway._ready_watchdog())
        await clock.tick(0)
        await clock.tick(30)
        with self.assertRaises(GatewayDisconnected):
            await task

    async def test_rest_failure_keeps_structured_diagnostics_not_fake_close_code(self):
        rest, clock = StubRest(), FastClock()
        rest.errors.extend(QQAPIError("authentication", accepted=False, status=401, code=123, stage="gateway") for _ in range(2))
        gateway = GatewayConnection(rest, clock=clock, sleep=clock.sleep)
        async def callback(_):
            pass
        await gateway.run(callback)
        self.assertIsNone(gateway.health["close_code"])
        self.assertEqual(gateway.health["rest_error"], {"category": "authentication", "status": 401, "code": 123, "stage": "gateway"})
        self.assertEqual(gateway.reason, "authentication_retry_exhausted")

    async def test_slow_durable_callback_does_not_block_heartbeat_ack(self):
        clock = ManualClock()
        frame = {"op": 0, "t": "GROUP_MESSAGE_CREATE", "s": 1, "d": {}}
        socket = PushedSocket([{"op": 10, "d": {"heartbeat_interval": 5000}}, frame])
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        gateway = GatewayConnection(StubRest(), checkpoint=GatewayCheckpoint("TEST_SESSION", 0),
                                    connector=connector_for([socket]), clock=clock, sleep=clock.sleep)
        async def callback(_):
            entered.set()
            await release.wait()
            finished.set()
        task = asyncio.create_task(gateway.run(callback))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await clock.tick(5)
            self.assertEqual(socket.sent[-1], {"op": 1, "d": 0})
            socket.incoming.put_nowait({"op": 11})
            await clock.tick(0)
            await clock.tick(5)
            self.assertEqual([item for item in socket.sent if item["op"] == 1],
                             [{"op": 1, "d": 0}, {"op": 1, "d": 0}])
            self.assertFalse(task.done())
            self.assertEqual(gateway.checkpoint.seq, 0)
            release.set()
            await asyncio.wait_for(finished.wait(), 2)
            self.assertEqual(gateway.checkpoint.seq, 1)
            await gateway.stop()
            await asyncio.wait_for(task, 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_dispatch_queue_overflow_stops_without_acknowledging_backlog(self):
        frame = lambda seq: {"op": 0, "t": "GROUP_MESSAGE_CREATE", "s": seq, "d": {}}
        socket = PushedSocket([HELLO, frame(1)])
        entered = asyncio.Event()
        clock = FastClock()
        gateway = GatewayConnection(StubRest(), checkpoint=GatewayCheckpoint("TEST_SESSION", 0),
                                    connector=connector_for([socket]), clock=clock, sleep=clock.sleep,
                                    max_dispatch_queue=1)
        async def callback(_):
            entered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(gateway.run(callback))
        await asyncio.wait_for(entered.wait(), 2)
        socket.incoming.put_nowait(frame(2))
        socket.incoming.put_nowait(frame(3))
        with self.assertRaises(GatewayBackpressureError):
            await asyncio.wait_for(task, 2)
        self.assertEqual(gateway.checkpoint.seq, 0)
        self.assertEqual(gateway.reason, "dispatch_queue_overflow")
        self.assertEqual(gateway.health["dispatch_queue_limit"], 1)
        self.assertTrue(socket.closed)

    async def test_durable_callback_failure_wins_over_simultaneous_disconnect(self):
        for release_first in (True, False):
            with self.subTest(release_first=release_first):
                event = {"op": 0, "t": "GROUP_MESSAGE_CREATE", "s": 1, "d": {}}
                socket = PushedSocket([HELLO, event])
                entered, release = asyncio.Event(), asyncio.Event()
                clock = FastClock()
                gateway = GatewayConnection(StubRest(), checkpoint=GatewayCheckpoint("TEST_SESSION", 0),
                                            connector=connector_for([socket]), clock=clock, sleep=clock.sleep)
                async def callback(_):
                    entered.set()
                    await release.wait()
                    raise RuntimeError("TEST_PRIVATE_DATABASE_DETAIL")
                task = asyncio.create_task(gateway.run(callback))
                await asyncio.wait_for(entered.wait(), 2)
                if release_first:
                    release.set()
                    socket.incoming.put_nowait(GatewayDisconnected(1006))
                else:
                    socket.incoming.put_nowait(GatewayDisconnected(1006))
                    release.set()
                with self.assertRaises(GatewayCallbackError) as caught:
                    await asyncio.wait_for(task, 2)
                self.assertNotIn("TEST_PRIVATE", str(caught.exception))
                self.assertEqual(gateway.checkpoint.seq, 0)
                self.assertEqual(gateway.reason, "durable_callback_failed")

    async def test_http_auth_retry_preserves_checkpoint_and_does_not_invalidate_again(self):
        rest, clock = StubRest(), FastClock()
        rest.errors.append(QQAPIError("authentication", accepted=False, status=401, stage="request"))
        # The REST layer has already conditionally invalidated its used token.
        # The gateway must not issue another unqualified invalidation.
        socket = ScriptSocket([HELLO, {"op": 0, "t": "RESUMED", "s": 1, "d": {}}])
        checkpoints = []
        async def save(checkpoint):
            checkpoints.append(checkpoint)
        async def saved(value):
            await save(value)
            await gateway.stop()
        gateway = GatewayConnection(rest, checkpoint=GatewayCheckpoint("TEST_SESSION", 0),
                                    on_checkpoint=saved, connector=connector_for([socket]),
                                    clock=clock, sleep=clock.sleep)
        async def callback(_):
            await gateway.stop()
        await gateway.run(callback)
        self.assertEqual(rest.queries, 2)
        self.assertEqual(rest.token_manager.invalidations, 0)
        self.assertEqual(socket.sent[0]["op"], 6)
        self.assertEqual(socket.sent[0]["d"]["session_id"], "TEST_SESSION")
        self.assertEqual(socket.sent[0]["d"]["seq"], 0)
        self.assertEqual(checkpoints, [GatewayCheckpoint("TEST_SESSION", 1)])
        self.assertFalse(gateway.message_gap)

    async def test_http_rate_limit_honors_retry_after_before_another_discovery(self):
        rest, clock = StubRest(), FastClock()
        rest.errors.extend([
            QQAPIError("rate_limited", accepted=False, status=429, retry_after=120),
            GatewayError("TEST_END_AFTER_RETRY"),
        ])
        async def advance(seconds):
            clock.delays.append(seconds)
            clock.now += seconds
        gateway = GatewayConnection(rest, clock=clock, sleep=advance, random=lambda: 0)
        async def callback(_):
            self.fail("no connection should be established")
        with self.assertRaises(GatewayError):
            await gateway.run(callback)
        self.assertEqual(rest.queries, 2)
        self.assertEqual(clock.delays, [120])
        self.assertEqual(clock.now, 120)

    async def test_http_rate_limit_cooldown_is_interruptible(self):
        rest = StubRest()
        rest.errors.append(QQAPIError("rate_limited", accepted=False, status=429, retry_after=3600))
        waiting = asyncio.Event()
        delays = []
        async def pause(seconds):
            delays.append(seconds)
            waiting.set()
            await asyncio.Event().wait()
        gateway = GatewayConnection(rest, sleep=pause)
        async def callback(_):
            self.fail("no connection should be established")
        task = asyncio.create_task(gateway.run(callback))
        await asyncio.wait_for(waiting.wait(), 2)
        await gateway.stop()
        await asyncio.wait_for(task, 2)
        self.assertEqual(delays, [3600])
        self.assertEqual(rest.queries, 1)
        self.assertEqual(gateway.reason, "stopped")

    async def test_missing_ack_disconnects_without_real_sleep(self):
        clock = ManualClock()
        socket = ScriptSocket([])
        gateway = GatewayConnection(StubRest(), checkpoint=GatewayCheckpoint("TEST_SESSION", 9),
                                    clock=clock, sleep=clock.sleep)
        heartbeat = asyncio.create_task(gateway._heartbeat(socket, 5))
        await clock.tick(0)
        await clock.tick(5)
        self.assertEqual(socket.sent, [{"op": 1, "d": 9}])
        await clock.tick(5)
        with self.assertRaises(GatewayDisconnected):
            await heartbeat

    async def test_server_heartbeat_uses_actual_ack_deadline_not_periodic_tick(self):
        clock = ManualClock()
        socket = PushedSocket([])
        gateway = GatewayConnection(StubRest(), checkpoint=GatewayCheckpoint("TEST_SESSION", 0),
                                    clock=clock, sleep=clock.sleep)
        heartbeat = asyncio.create_task(gateway._heartbeat(socket, 5))
        reader = asyncio.create_task(gateway._receive(socket, asyncio.Queue()))
        try:
            await clock.tick(0)
            await clock.tick(4)
            socket.incoming.put_nowait({"op": 1})
            await clock.tick(0)
            self.assertEqual(socket.sent, [{"op": 1, "d": 0}])
            await clock.tick(1)
            self.assertFalse(heartbeat.done(), "only one second has elapsed since server-requested heartbeat")
            await clock.tick(3)
            self.assertFalse(heartbeat.done())
            # A second server request must not renew the original deadline.
            socket.incoming.put_nowait({"op": 1})
            await clock.tick(0)
            await clock.tick(1)
            with self.assertRaises(GatewayDisconnected):
                await heartbeat
        finally:
            heartbeat.cancel()
            reader.cancel()
            await asyncio.gather(heartbeat, reader, return_exceptions=True)

    async def test_ack_allows_next_heartbeat_without_real_sleep(self):
        clock = ManualClock()
        socket = ScriptSocket([{"op": 11}])
        gateway = GatewayConnection(StubRest(), clock=clock, sleep=clock.sleep)
        heartbeat = asyncio.create_task(gateway._heartbeat(socket, 5))
        await clock.tick(0)
        await clock.tick(5)
        receiver = asyncio.create_task(gateway._receive(socket, None))
        await clock.tick(0)
        await clock.tick(5)
        self.assertEqual(len(socket.sent), 2)
        heartbeat.cancel()
        receiver.cancel()
        await asyncio.gather(heartbeat, receiver, return_exceptions=True)

    async def test_hello_timeout_and_unknown_closes_exhaust_retries(self):
        clock = ManualClock()
        socket = ScriptSocket([])
        gateway = GatewayConnection(StubRest(), clock=clock, sleep=clock.sleep)
        task = asyncio.create_task(gateway._hello(socket))
        await clock.tick(0)
        await clock.tick(30)
        with self.assertRaises(GatewayDisconnected):
            await task
        sockets = [ScriptSocket([HELLO, GatewayDisconnected(4999)]) for _ in range(3)]
        clock = FastClock()
        gateway = GatewayConnection(StubRest(), connector=connector_for(sockets), max_reconnects=2,
                                    clock=clock, sleep=clock.sleep, random=lambda: 0)
        async def callback(_):
            pass
        await gateway.run(callback)
        self.assertEqual(gateway.reason, "reconnect_budget_exhausted")
        self.assertTrue(all(socket.closed for socket in sockets))
        self.assertTrue(all(delay <= 60 for delay in clock.delays))

    async def test_default_reconnect_survives_extended_outage(self):
        from unittest.mock import AsyncMock

        gateway = GatewayConnection(StubRest(), sleep=AsyncMock())
        checkpoint = GatewayCheckpoint("TEST_SESSION", 8)
        gateway.checkpoint = checkpoint
        failures = 0

        async def session(url, callback):
            nonlocal failures
            if failures < 12:
                failures += 1
                raise ConnectionError("PRIVATE_NETWORK_DETAILS")
            await gateway.stop()

        gateway._session = session
        with self.assertLogs("src.qq_official.gateway", level="WARNING") as logs:
            await gateway.run(AsyncMock())
        self.assertEqual(failures, 12)
        self.assertIs(gateway.checkpoint, checkpoint)
        self.assertEqual(gateway.reason, "stopped")
        self.assertNotIn("PRIVATE_NETWORK_DETAILS", "\n".join(logs.output))
        self.assertTrue(all(call.args[0] <= 60 for call in gateway.sleep.call_args_list))


class GatewayFakeServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakePlatform()
        await self.fake.start()
        credentials = Credentials(self.fake.app_id, self.fake.app_secret)
        self.tokens = TokenManager(credentials, base_url=self.fake.base_url, allow_test_server=True)
        self.rest = RestClient(self.tokens, allow_test_server=True)

    async def asyncTearDown(self):
        await self.rest.aclose()
        await self.tokens.aclose()
        await self.fake.close()

    async def test_real_loopback_identify_dispatch_and_passive_reply(self):
        gateway = GatewayConnection(self.rest)
        delivered = []
        async def callback(frame):
            delivered.append(frame)
            if frame["t"] == "GROUP_AT_MESSAGE_CREATE":
                response = await self.rest.send_message("group", "GROUP_OPENID_TEST_1", {
                    "msg_type": 0, "content": "TEST_REPLY", "msg_id": frame["d"]["id"], "msg_seq": 1})
                self.assertEqual(response["id"], "TEST_SENT_1")
                await gateway.stop()
        task = asyncio.create_task(gateway.run(callback))
        try:
            self.assertEqual(await asyncio.wait_for(self.fake.wait_ready(), 2), 2)
            frame = await self.fake.emit("GROUP_AT_MESSAGE_CREATE", {
                "id": "TEST_MESSAGE_1", "group_openid": "GROUP_OPENID_TEST_1",
                "content": "TEST_REQUEST", "author": {"member_openid": "TEST_MEMBER"}})
            await asyncio.wait_for(task, 2)
            self.assertEqual(delivered[-1], frame)
            self.assertEqual(gateway.checkpoint.seq, frame["s"])
            self.assertEqual(self.fake.received[0]["payload"]["msg_id"], "TEST_MESSAGE_1")
        finally:
            await gateway.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_loopback_resume_replays_unacknowledged_and_invalid_session_identifies(self):
        frame = await self.fake.emit("GROUP_MESSAGE_CREATE", {
            "id": "TEST_OFFLINE_MESSAGE", "group_openid": "TEST_GROUP"})
        clock = FastClock()
        gateway = GatewayConnection(self.rest, checkpoint=GatewayCheckpoint("TEST_SESSION_1", 0),
                                    clock=clock, sleep=clock.sleep)
        observed = []
        async def saved(value):
            if value is not None and value.seq > frame["s"]:
                await gateway.stop()
        gateway.on_checkpoint = saved
        async def resumed(event):
            observed.append(event)
        await asyncio.wait_for(gateway.run(resumed), 2)
        self.assertEqual(observed[0], frame)
        self.assertEqual(self.fake.resume_count, 1)
        self.assertEqual(self.fake.identify_count, 0)
        self.fake.resume_invalid_count = 1
        gateway = GatewayConnection(self.rest, checkpoint=GatewayCheckpoint("TEST_SESSION_1", self.fake._seq),
                                    clock=clock, sleep=clock.sleep)
        async def identified(event):
            if event["t"] == "READY":
                await gateway.stop()
        await asyncio.wait_for(gateway.run(identified), 2)
        self.assertEqual(self.fake.resume_count, 2)
        self.assertEqual(self.fake.identify_count, 1)
        self.assertTrue(gateway.message_gap)

    async def test_websocket_redirect_is_rejected_before_sending_token(self):
        self.fake.redirect_gateway_to = self.fake.base_url + "/untrusted"
        gateway = GatewayConnection(self.rest, max_reconnects=0)
        async def callback(_):
            self.fail("redirect must not connect")
        with self.assertRaises(GatewayError):
            await gateway.run(callback)
        self.assertEqual(self.fake.identify_count, 0)
        self.assertEqual(gateway.reason, "gateway_protocol_error")

    async def test_loopback_op7_and_heartbeat_ack(self):
        clock = FastClock()
        gateway = GatewayConnection(self.rest, clock=clock, sleep=clock.sleep)
        ready = asyncio.Event()
        async def saved(value):
            if self.fake.resume_count:
                await gateway.stop()
        gateway.on_checkpoint = saved
        async def callback(event):
            if event["t"] == "READY":
                ready.set()
        task = asyncio.create_task(gateway.run(callback))
        try:
            self.assertEqual(await asyncio.wait_for(self.fake.wait_ready(), 2), 2)
            await ready.wait()
            await self.fake.send_opcode(1)
            # The ordered frame exchange has no elapsed-time sleep.
            await self.fake.send_opcode(7)
            await asyncio.wait_for(task, 2)
            self.assertEqual(self.fake.resume_count, 1)
            self.assertEqual(self.fake.heartbeats, [0])
        finally:
            await gateway.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_replayed_message_never_resets_reply_window(self):
        self.fake.clock = lambda: 1000
        event = {"id": "TEST_REPLAY", "group_openid": "TEST_GROUP"}
        await self.fake.emit("GROUP_AT_MESSAGE_CREATE", event)
        window = self.fake.windows[("group", "TEST_GROUP", "msg_id", "TEST_REPLAY")]
        await self.rest.send_message("group", "TEST_GROUP", {
            "msg_type": 0, "content": "TEST", "msg_id": "TEST_REPLAY", "msg_seq": 1})
        self.fake.clock = lambda: 1100
        await self.fake.emit("GROUP_AT_MESSAGE_CREATE", event)
        self.assertIs(self.fake.windows[("group", "TEST_GROUP", "msg_id", "TEST_REPLAY")], window)
        self.assertEqual((window.expires_at, window.used, window.sequences), (1300, 1, {1}))

    async def test_daily_counter_is_fixed_period_and_does_not_refill_each_minute(self):
        target = "TEST_GROUP"
        self.fake.clock = lambda: 1000
        self.fake.proactive_allowed[("group", target)] = True
        self.fake._proactive_day_counts[("group", target, 0)] = 1000
        payload = {"msg_type": 0, "content": "TEST"}
        for now in (1000, 1061, 86399):
            self.fake.clock = lambda: now
            with self.assertRaises(QQAPIError) as caught:
                await self.rest.send_message("group", target, payload)
            self.assertEqual(caught.exception.category, "rate_limited")
        self.fake.clock = lambda: 86400
        await self.rest.send_message("group", target, payload)
        self.assertEqual(self.fake._proactive_day_counts[("group", target, 1)], 1)

    async def test_group_and_c2c_window_limits_duplicate_and_expiry(self):
        for kind, limit in (("group", 5), ("user", 4)):
            target = "TEST_TARGET"
            window = self.fake.add_window(kind, target, "TEST_MSG")
            for seq in range(1, limit + 1):
                await self.rest.send_message(kind, target, {"msg_type": 0, "content": "TEST", "msg_id": "TEST_MSG", "msg_seq": seq})
            with self.assertRaises(QQAPIError) as caught:
                await self.rest.send_message(kind, target, {"msg_type": 0, "content": "TEST", "msg_id": "TEST_MSG", "msg_seq": 1})
            self.assertEqual(caught.exception.code, FAKE_DUPLICATE_SEQUENCE)
            self.assertIsNone(caught.exception.accepted)
            with self.assertRaises(QQAPIError) as caught:
                await self.rest.send_message(kind, target, {"msg_type": 0, "content": "TEST", "msg_id": "TEST_MSG", "msg_seq": limit + 1})
            self.assertEqual(caught.exception.code, 40034128)
            window.expires_at = self.fake.clock() - 1
            with self.assertRaises(QQAPIError) as caught:
                await self.rest.send_message(kind, target, {"msg_type": 0, "content": "TEST", "msg_id": "TEST_MSG", "msg_seq": limit + 2})
            self.assertEqual(caught.exception.code, FAKE_UNKNOWN_WINDOW)

    async def test_lifecycle_proactive_body_error_and_accepted_response_loss(self):
        target, payload = "GROUP_OPENID_TEST_1", {"msg_type": 0, "content": "TEST_RESULT"}
        with self.assertRaises(QQAPIError) as caught:
            await self.rest.send_message("group", target, payload)
        self.assertEqual(caught.exception.code, 40034105)
        await self.fake.emit("GROUP_MSG_RECEIVE", {"group_openid": target})
        self.fake.next_errors.append((200, 40034006))
        with self.assertRaises(QQAPIError) as caught:
            await self.rest.send_message("group", target, payload)
        self.assertEqual(caught.exception.category, "content_rejected")
        self.assertFalse(self.fake.received)
        self.fake.drop_next_response = True
        with self.assertRaises(QQAPIError) as caught:
            await self.rest.send_message("group", target, payload)
        self.assertIsNone(caught.exception.accepted)
        self.assertEqual(len(self.fake.received), 1)
        await self.fake.emit("GROUP_MSG_REJECT", {"group_openid": target})
        with self.assertRaises(QQAPIError):
            await self.rest.send_message("group", target, payload)
        self.assertEqual(len(self.fake.received), 1)

    async def test_full_message_lifecycle_c2c_and_quota_fixtures(self):
        target = "TEST_GROUP"
        await self.fake.emit("GROUP_MESSAGE_CREATE", {"id": "TEST_UNMENTIONED", "group_openid": target})
        self.assertFalse(self.fake.windows)
        self.fake.nonmention_reply_allowed = True
        await self.fake.emit("GROUP_MESSAGE_CREATE", {"id": "TEST_FULL", "group_openid": target})
        self.assertIn(("group", target, "msg_id", "TEST_FULL"), self.fake.windows)
        await self.fake.emit("C2C_MESSAGE_CREATE", {"id": "TEST_C2C", "author": {"user_openid": "TEST_USER"}})
        self.assertIn(("user", "TEST_USER", "msg_id", "TEST_C2C"), self.fake.windows)
        await self.fake.emit("GROUP_DEL_ROBOT", {"group_openid": target})
        self.assertNotIn(("group", target, "msg_id", "TEST_FULL"), self.fake.windows)
        self.fake.clock = lambda: 1000
        self.fake.proactive_allowed[("group", target)] = True
        payload = {"msg_type": 0, "content": "TEST_RESULT"}
        await asyncio.gather(*(self.rest.send_message("group", target, payload) for _ in range(20)))
        with self.assertRaises(QQAPIError) as caught:
            await self.rest.send_message("group", target, payload)
        self.assertEqual(caught.exception.category, "rate_limited")


if __name__ == "__main__":
    unittest.main()
