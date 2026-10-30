"""Official runtime isolation and recovery with real local PostgreSQL leases."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import psycopg

from src.plugins.ai_chat.adapters.qq_official.config import LoadedOfficialConfig, OfficialConfig
from src.plugins.ai_chat.adapters.qq_official.lease import DedicatedAccountLease, advisory_key_for
from src.plugins.ai_chat.adapters.qq_official import runtime as runtime_module
from src.plugins.ai_chat.adapters.qq_official.runtime import OfficialRuntime
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.lifecycle import BackgroundTaskSupervisor
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import AccountKey
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from src.qq_official.credentials import Credentials
from src.qq_official.gateway import GatewayCheckpoint
from src.qq_official.testing.fake_platform import FakePlatform
from tests.postgres_fixture import postgres_for_test


class ControlledGateway:
    """Protocol-shaped fake with explicit completion and no network or timers."""
    def __init__(self, *, checkpoint, on_checkpoint, ensure_current, ordinal):
        self.checkpoint = checkpoint
        self.on_checkpoint, self.ensure_current = on_checkpoint, ensure_current
        self.session = checkpoint.session_id if checkpoint else f"TEST_RUNTIME_SESSION_{ordinal}"
        self.seq = checkpoint.seq if checkpoint else -1
        self.queue = asyncio.Queue()
        self.ready = asyncio.Event()
        self.stopped = asyncio.Event()
        self.reason = "test_stopped"

    async def run(self, callback):
        self.seq += 1
        first = {"op": 0, "t": "RESUMED" if self.checkpoint else "READY", "s": self.seq,
                 "d": {} if self.checkpoint else {"session_id": self.session}}
        await self.ensure_current()
        await callback(first)
        await self.on_checkpoint(GatewayCheckpoint(self.session, self.seq))
        self.ready.set()
        while not self.stopped.is_set():
            item = await self.queue.get()
            if item is None:
                return
            frame, completed = item
            try:
                await callback(frame)
                await self.on_checkpoint(GatewayCheckpoint(self.session, frame["s"]))
            except BaseException as error:
                if not completed.done():
                    completed.set_exception(error)
                raise
            else:
                if not completed.done():
                    completed.set_result(frame)

    async def emit(self, content="test message", *, native_id=None, owner="TEST_RUNTIME_OWNER"):
        self.seq += 1
        frame = {"op": 0, "t": "C2C_MESSAGE_CREATE", "s": self.seq,
                 "id": f"TEST_EVENT_{self.seq}", "d": {
                     "id": native_id or f"TEST_MESSAGE_{self.seq}", "author": {"user_openid": owner},
                     "content": content, "timestamp": datetime.now(timezone.utc).isoformat(),
                     "message_scene": {"ext": {"msg_idx": f"TEST_INDEX_{self.seq}"}},
                 }}
        completed = asyncio.get_running_loop().create_future()
        await self.queue.put((frame, completed))
        return await completed

    async def finish(self, reason="reconnect_budget_exhausted"):
        self.reason = reason
        await self.queue.put(None)

    async def stop(self):
        self.stopped.set()
        await self.queue.put(None)


class ControlledProtocol:
    def __init__(self):
        self.gateways = []
        self.opened = asyncio.Queue()
        self.closed = []

    @asynccontextmanager
    async def __call__(self, loaded, *, checkpoint, on_checkpoint, ensure_current):
        await ensure_current()
        gateway = ControlledGateway(checkpoint=checkpoint, on_checkpoint=on_checkpoint,
                                    ensure_current=ensure_current, ordinal=len(self.gateways) + 1)
        self.gateways.append(gateway)
        await self.opened.put(gateway)
        try:
            yield gateway
        finally:
            await gateway.stop()
            self.closed.append(gateway)

    async def next_ready(self):
        gateway = await asyncio.wait_for(self.opened.get(), 5)
        await asyncio.wait_for(gateway.ready.wait(), 5)
        return gateway


def loaded_config(app_id="TEST_RUNTIME_APP", *, base="http://127.0.0.1:1", public=False):
    return LoadedOfficialConfig(
        AccountKey("qq-official", app_id, "test"), Credentials(app_id, "TEST_APP_SECRET"), base,
        () if public else ("TEST_RUNTIME_OWNER",), "public" if public else "owner_only", (),
        b"TEST_LOCAL_BINDING_PEPPER_32_BYTES_MINIMUM", allow_test_server=True,
    )


class DisabledRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_start_performs_no_credential_database_or_protocol_io(self):
        config = OfficialConfig(app_id_file="/must/not/read", app_secret_file="/must/not/read",
                                binding_pepper_file="/must/not/read")
        database = Mock()
        protocol = Mock(side_effect=AssertionError("protocol must remain dormant"))
        with patch.object(OfficialConfig, "load", side_effect=AssertionError("credentials must remain dormant")) as load:
            runtime = OfficialRuntime(config, database, logger=Mock(), protocol_factory=protocol,
                                      lease_factory=Mock(side_effect=AssertionError("no lease")))
            self.assertFalse(await runtime.start())
            self.assertEqual(runtime.status()["state"], "disabled")
            await runtime.stop()
            load.assert_not_called()
        self.assertEqual(database.mock_calls, [])
        protocol.assert_not_called()


class ProtocolCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_protocol_closes_every_resource_when_a_close_raises(self):
        for failed in (("gateway",), ("rest",), ("tokens",), ("gateway", "rest", "tokens")):
            with self.subTest(failed=failed):
                closed = []

                def close(name):
                    async def invoke():
                        closed.append(name)
                        if name in failed:
                            raise RuntimeError(f"TEST_SECRET_{name}_CLOSE_ERROR")
                    return AsyncMock(side_effect=invoke)

                loaded = loaded_config()
                tokens = SimpleNamespace(credentials=loaded.credentials, base_url=loaded.api_base,
                                         aclose=close("tokens"))
                rest = SimpleNamespace(aclose=close("rest"))
                gateway = SimpleNamespace(connector=Mock(), stop=close("gateway"))
                ensure_current = AsyncMock()
                with patch.object(runtime_module, "TokenManager", return_value=tokens), \
                        patch.object(runtime_module, "RestClient", return_value=rest), \
                        patch.object(runtime_module, "GatewayConnection", return_value=gateway):
                    with self.assertRaises(RuntimeError):
                        async with runtime_module.open_official_protocol(
                                loaded, checkpoint=None, on_checkpoint=AsyncMock(),
                                ensure_current=ensure_current) as opened:
                            self.assertIs(opened, gateway)
                self.assertEqual(closed, ["gateway", "rest", "tokens"])
                gateway.stop.assert_awaited_once()
                rest.aclose.assert_awaited_once()
                tokens.aclose.assert_awaited_once()
                ensure_current.assert_awaited_once()


class OfficialRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database = postgres_for_test(self)
        self.logger = Mock()

    def runtime(self, *, loaded=None, protocol=None, **kwargs):
        selected = loaded or loaded_config()
        config = SimpleNamespace(enabled=True, load=Mock(return_value=selected))
        arguments = {"probe_seconds": 0.01, "retry_seconds": 0.01, "allow_test_server": True,
                     "logger": self.logger, **kwargs}
        if protocol is not None:
            arguments["protocol_factory"] = protocol
        runtime = OfficialRuntime(config, self.database, **arguments)
        self.addAsyncCleanup(runtime.stop)
        return runtime

    def query(self, query, values=()):
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            rows = cursor.execute(query, values).fetchall()
            connection.commit()
            return [dict(row) for row in rows]
        except BaseException:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    async def wait_db(self, query, values=(), predicate=bool):
        async with asyncio.timeout(5):
            while True:
                rows = await asyncio.to_thread(self.query, query, values)
                if predicate(rows):
                    return rows
                await asyncio.sleep(0.01)

    async def wait_state(self, runtime, expected):
        async with asyncio.timeout(5):
            while runtime.state != expected:
                await asyncio.sleep(0.01)

    async def quiescent_pause(self, runtime):
        await self.wait_state(runtime, "paused")
        if hasattr(runtime, "_idle"):
            await asyncio.wait_for(runtime._idle.wait(), 5)
        else:
            await self.wait_db("SELECT holder_id FROM qq_official_accounts WHERE account_key=?",
                               (runtime.loaded.account.key,), lambda rows: rows and rows[0]["holder_id"] is None)

    async def test_busy_account_never_opens_gateway_and_stop_does_not_steal_lock(self):
        loaded = loaded_config()
        await asyncio.to_thread(self.query, """INSERT INTO qq_official_accounts
            (account_key,enabled,gateway_state,created_at,updated_at) VALUES (?,TRUE,'idle',100,100) RETURNING account_key""",
            (loaded.account.key,))
        owner = DedicatedAccountLease(self.database.dsn, loaded.account, schema=self.database.schema)
        self.addCleanup(owner.close)
        token = await asyncio.to_thread(owner.acquire)
        protocol = ControlledProtocol()
        runtime = self.runtime(loaded=loaded, protocol=protocol)
        self.assertTrue(await runtime.start())
        await self.wait_state(runtime, "waiting")
        self.assertEqual(protocol.gateways, [])
        await runtime.stop()
        self.assertTrue(await asyncio.to_thread(owner.probe))
        self.assertEqual(owner.token, token)

    async def test_default_protocol_fake_platform_ingests_without_business_send(self):
        async with FakePlatform() as fake:
            runtime = self.runtime(loaded=loaded_config(fake.app_id, base=fake.base_url, public=True))
            self.assertTrue(await runtime.start())
            self.assertEqual(await asyncio.wait_for(fake.wait_ready(), 5), 2)
            await fake.emit("C2C_MESSAGE_CREATE", {
                "id": "TEST_FULL_STACK_MESSAGE", "author": {"user_openid": "TEST_PUBLIC_C2C"},
                "content": "synthetic full stack input", "timestamp": datetime.now(timezone.utc).isoformat(),
                "message_scene": {"ext": {"msg_idx": "TEST_FULL_STACK_INDEX"}},
            })
            rows = await self.wait_db("SELECT * FROM qq_official_inbound_events WHERE native_message_id=? AND status='processed'",
                                      ("TEST_FULL_STACK_MESSAGE",))
            self.assertIsNotNone(rows[0]["canonical_message_id"])
            self.assertIsNone(rows[0]["dispatch_job_id"])
            for table in ("messages", "qq_official_messages", "qq_official_message_indexes", "qq_official_reply_windows"):
                count = await asyncio.to_thread(self.query, f"SELECT count(*) AS n FROM {table}")
                self.assertEqual(count[0]["n"], 1, table)
            turns = await asyncio.to_thread(self.query, "SELECT status FROM durable_jobs WHERE job_id=?", (rows[0]["dispatch_job_id"],))
            self.assertEqual(turns, [])
            self.assertEqual((fake.token_calls, fake.gateway_queries, fake.identify_count), (1, 1, 1))
            self.assertEqual(fake.received, [])
            await runtime.stop()
            async with asyncio.timeout(3):
                while fake.connections:
                    await asyncio.sleep(0.01)
            contender = DedicatedAccountLease(self.database.dsn, loaded_config(fake.app_id).account, schema=self.database.schema)
            self.addCleanup(contender.close)
            self.assertIsNotNone(await asyncio.to_thread(contender.acquire))

    async def test_independent_stop_preserves_other_runtime_onebot_and_shared_ledger(self):
        shared = MessageLedger(self.database)
        self.addCleanup(shared.close)
        legacy = BackgroundTaskSupervisor(self.logger)
        legacy_started = asyncio.Event()
        legacy_finish = asyncio.Event()

        async def onebot_loop():
            legacy_started.set()
            await legacy_finish.wait()

        legacy.start("onebot-existing", onebot_loop)
        self.addAsyncCleanup(legacy.stop_all)
        await legacy_started.wait()
        first_protocol, second_protocol = ControlledProtocol(), ControlledProtocol()
        first = self.runtime(protocol=first_protocol, ledger=shared)
        second = self.runtime(loaded=loaded_config("TEST_RUNTIME_OTHER"), protocol=second_protocol, ledger=shared)
        await first.start()
        await second.start()
        await first_protocol.next_ready()
        second_gateway = await second_protocol.next_ready()
        with patch.object(shared, "close", wraps=shared.close) as close:
            await first.stop()
            close.assert_not_called()
        self.assertEqual(legacy.running(), ("onebot-existing",))
        self.assertEqual(second.state, "running")
        await second_gateway.emit(native_id="TEST_SECOND_STILL_RUNNING")
        await self.wait_db("SELECT inbox_id FROM qq_official_inbound_events WHERE native_message_id=? AND status='processed'",
                           ("TEST_SECOND_STILL_RUNNING",))
        recorded = await asyncio.to_thread(shared.record_message, ConversationScope("onebot-v11", "group", "10001"),
            native_message_id="10002", sender_native_user_id="10003", sender_display="Legacy", body=MessageBody((TextNode(0, "still alive"),)))
        self.assertGreater(recorded.canonical_message_id, 0)

    async def test_gateway_terminal_budget_pauses_persistently_until_explicit_resume(self):
        protocol = ControlledProtocol()
        runtime = self.runtime(protocol=protocol)
        await runtime.start()
        gateway = await protocol.next_ready()
        await gateway.finish()
        await self.quiescent_pause(runtime)
        self.assertEqual(runtime.reason, "reconnect_budget_exhausted")
        self.assertEqual(len(protocol.gateways), 1)
        self.assertEqual(runtime._supervisor.failures(), {})
        self.assertTrue(runtime._supervisor.running())
        row = (await asyncio.to_thread(self.query, "SELECT gateway_state,pause_reason FROM qq_official_accounts"))[0]
        self.assertEqual(row, {"gateway_state": "paused", "pause_reason": "reconnect_budget_exhausted"})
        self.assertTrue(await runtime.resume())
        replacement = await protocol.next_ready()
        self.assertIsNotNone(replacement.checkpoint)
        self.assertEqual(len(protocol.gateways), 2)

    async def test_persisted_terminal_pause_is_not_reset_by_new_runtime_start(self):
        first_protocol = ControlledProtocol()
        first = self.runtime(protocol=first_protocol)
        await first.start()
        gateway = await first_protocol.next_ready()
        await gateway.finish("authentication_refresh_exhausted")
        await self.quiescent_pause(first)
        await first.stop()
        second_protocol = ControlledProtocol()
        second = self.runtime(protocol=second_protocol)
        await second.start()
        await self.quiescent_pause(second)
        self.assertEqual(second_protocol.gateways, [])
        self.assertTrue(await second.resume())
        await second_protocol.next_ready()

    async def test_resume_clears_stale_holder_only_after_actual_lock_is_released(self):
        protocol = ControlledProtocol()
        runtime = self.runtime(protocol=protocol)
        await runtime.start()
        gateway = await protocol.next_ready()
        previous_epoch = runtime._token.epoch
        await gateway.finish()
        await self.quiescent_pause(runtime)
        await asyncio.to_thread(self.query, """UPDATE qq_official_accounts
            SET holder_id=?,lock_backend_pid=77 WHERE account_key=? RETURNING account_key""",
            ("a" * 32, runtime.loaded.account.key))
        self.assertTrue(await runtime.resume())
        await protocol.next_ready()
        row = (await asyncio.to_thread(self.query, "SELECT holder_id,lock_backend_pid,fencing_epoch FROM qq_official_accounts"))[0]
        self.assertNotEqual(row["holder_id"], "a" * 32)
        self.assertEqual(row["fencing_epoch"], previous_epoch + 1)
        self.assertEqual(row["lock_backend_pid"], runtime._token.backend_pid)

    async def test_resume_cannot_clear_another_live_account_holder(self):
        protocol = ControlledProtocol()
        runtime = self.runtime(protocol=protocol)
        await runtime.start()
        gateway = await protocol.next_ready()
        await gateway.finish()
        await self.quiescent_pause(runtime)
        # Simulate a trusted control plane allowing a different live instance
        # to take over while this process is still locally paused.
        await asyncio.to_thread(self.query, """UPDATE qq_official_accounts
            SET gateway_state='idle',pause_reason=NULL WHERE account_key=? RETURNING account_key""",
            (runtime.loaded.account.key,))
        other = DedicatedAccountLease(self.database.dsn, runtime.loaded.account, schema=self.database.schema)
        self.addCleanup(other.close)
        self.assertIsNotNone(await asyncio.to_thread(other.acquire))
        before = await asyncio.to_thread(self.query, "SELECT * FROM qq_official_accounts")
        self.assertFalse(await runtime.resume())
        self.assertEqual(await asyncio.to_thread(self.query, "SELECT * FROM qq_official_accounts"), before)
        self.assertTrue(await asyncio.to_thread(other.probe))
        self.assertEqual(len(protocol.gateways), 1)

    async def test_backend_loss_stops_gateway_without_supervisor_reset(self):
        protocol = ControlledProtocol()
        runtime = self.runtime(protocol=protocol)
        await runtime.start()
        gateway = await protocol.next_ready()
        pid = runtime._token.backend_pid

        def terminate():
            with psycopg.connect(self.database.dsn, autocommit=True) as connection:
                return connection.execute("SELECT pg_terminate_backend(%s,1000)", (pid,)).fetchone()[0]

        self.assertTrue(await asyncio.to_thread(terminate))
        await self.wait_state(runtime, "paused")
        await asyncio.wait_for(gateway.stopped.wait(), 5)
        self.assertEqual(runtime.reason, "account_lease_lost")
        self.assertEqual(len(protocol.gateways), 1)
        self.assertEqual(runtime._supervisor.failures(), {})

    async def test_stop_drains_inflight_database_accept_before_lease_and_store_close(self):
        protocol = ControlledProtocol()
        runtime = self.runtime(protocol=protocol)
        await runtime.start()
        gateway = await protocol.next_ready()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = runtime.store.accept

        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test release was not signaled")
            return original(*args, **kwargs)

        with patch.object(runtime.store, "accept", side_effect=blocked):
            incoming = asyncio.create_task(gateway.emit(native_id="TEST_INFLIGHT_ACCEPT"))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            stopping = asyncio.create_task(runtime.stop())
            await asyncio.wait_for(gateway.stopped.wait(), 3)
            for _ in range(10):
                await asyncio.sleep(0)
            self.assertFalse(stopping.done())
            self.assertIsNotNone(runtime.store)
            release.set()
            await asyncio.wait_for(stopping, 5)
            await asyncio.gather(incoming, return_exceptions=True)
        rows = await asyncio.to_thread(self.query, "SELECT native_message_id FROM qq_official_inbound_events WHERE native_message_id=?",
                                       ("TEST_INFLIGHT_ACCEPT",))
        self.assertEqual(len(rows), 1)
        row = (await asyncio.to_thread(self.query, "SELECT holder_id FROM qq_official_accounts"))[0]
        self.assertIsNone(row["holder_id"])
        self.assertIsNone(runtime.store)

    async def test_stop_drains_inflight_queue_claim_before_closing_stores(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = DurableJobStore.claim_due

        def blocked(store, *args, **kwargs):
            entered.set()
            try:
                if not release.wait(5):
                    raise RuntimeError("test claim release was not signaled")
                return original(store, *args, **kwargs)
            finally:
                finished.set()

        self.addCleanup(release.set)
        protocol = ControlledProtocol()
        runtime = self.runtime(protocol=protocol)
        with patch.object(DurableJobStore, "claim_due", new=blocked):
            await runtime.start()
            gateway = await protocol.next_ready()
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            stopping = asyncio.create_task(runtime.stop())
            try:
                await asyncio.wait_for(gateway.stopped.wait(), 3)
                # A short scheduling bound detects an early stop return. The
                # database operation's completion is controlled only by release.
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(stopping), 0.05)
                self.assertFalse(finished.is_set())
                self.assertIsNotNone(runtime.jobs)
            finally:
                release.set()
                await asyncio.wait_for(stopping, 5)
                self.assertTrue(await asyncio.to_thread(finished.wait, 5))

    async def test_gateway_stop_failure_still_drains_claim_releases_lease_and_closes_stores(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = DurableJobStore.claim_due

        def blocked(store, *args, **kwargs):
            entered.set()
            try:
                if not release.wait(5):
                    raise RuntimeError("test claim release was not signaled")
                return original(store, *args, **kwargs)
            finally:
                finished.set()

        self.addCleanup(release.set)
        protocol = ControlledProtocol()
        runtime = self.runtime(protocol=protocol)
        close_attempted = asyncio.Event()

        async def broken_stop():
            close_attempted.set()
            raise RuntimeError("TEST_SECRET_GATEWAY_CLOSE_ERROR")

        with patch.object(DurableJobStore, "claim_due", new=blocked):
            await runtime.start()
            gateway = await protocol.next_ready()
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            stores = (runtime.store, runtime.jobs, runtime.ledger)
            for store in stores:
                store.close = Mock(wraps=store.close)
            with patch.object(gateway, "stop", new=AsyncMock(side_effect=broken_stop)):
                stopping = asyncio.create_task(runtime.stop())
                try:
                    await asyncio.wait_for(close_attempted.wait(), 3)
                    with self.assertRaises(TimeoutError):
                        await asyncio.wait_for(asyncio.shield(stopping), 0.05)
                    self.assertFalse(finished.is_set())
                    for store in stores:
                        store.close.assert_not_called()
                finally:
                    release.set()
                    await asyncio.wait_for(stopping, 5)
                    self.assertTrue(await asyncio.to_thread(finished.wait, 5))
            for store in stores:
                store.close.assert_called_once()
        self.assertEqual(runtime._supervisor.running(), ())
        self.assertEqual(runtime.state, "stopped")
        self.assertIsNone(runtime.store)
        self.assertIsNone(runtime.jobs)
        self.assertIsNone(runtime.ledger)
        account = (await asyncio.to_thread(self.query, "SELECT holder_id FROM qq_official_accounts"))[0]
        self.assertIsNone(account["holder_id"])
        # Cleanup failure may conservatively persist a paused account, so check
        # the real PostgreSQL lock independently of acquisition admission.
        lock = await asyncio.to_thread(self.query, "SELECT pg_try_advisory_xact_lock(?) AS acquired",
                                       (advisory_key_for(runtime.loaded.account),))
        self.assertTrue(lock[0]["acquired"])
        self.assertNotIn("TEST_SECRET_GATEWAY_CLOSE_ERROR", repr(self.logger.mock_calls) + json.dumps(runtime.status()))

    async def test_resume_waits_for_prior_lease_close_without_losing_wakeup(self):
        leases = []

        def factory(*args, **kwargs):
            lease = DedicatedAccountLease(*args, **kwargs)
            leases.append(lease)
            return lease

        protocol = ControlledProtocol()
        runtime = self.runtime(protocol=protocol, lease_factory=factory)
        await runtime.start()
        gateway = await protocol.next_ready()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = leases[0].close

        def blocked():
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test lease close release was not signaled")
            original()

        with patch.object(leases[0], "close", side_effect=blocked):
            await gateway.finish()
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            self.assertEqual(runtime.state, "paused")
            resuming = asyncio.create_task(runtime.resume())
            try:
                for _ in range(10):
                    await asyncio.sleep(0)
                self.assertFalse(resuming.done())
                self.assertEqual(len(leases), 1)
            finally:
                release.set()
            self.assertTrue(await asyncio.wait_for(resuming, 5))
        await protocol.next_ready()
        self.assertEqual(len(leases), 2)
        self.assertGreater(leases[1].token.epoch, 1)

    async def test_binding_failed_unknown_and_exception_cancel_without_retry(self):
        protocol = ControlledProtocol()
        sender = AsyncMock(side_effect=["failed", "unknown", RuntimeError("SECRET_SEND_ERROR")])
        runtime = self.runtime(protocol=protocol, binding_sender=sender)
        await runtime.start()
        gateway = await protocol.next_ready()
        for number in range(3):
            await gateway.emit("/bind create", native_id=f"TEST_BIND_{number}")
            rows = await asyncio.to_thread(self.query, "SELECT state,code_hmac FROM qq_official_binding_challenges")
            self.assertEqual(len(rows), number + 1)
            self.assertTrue(all(row == {"state": "cancelled", "code_hmac": ""} for row in rows))
        self.assertEqual(sender.await_count, 3)
        self.assertNotIn("SECRET_SEND_ERROR", json.dumps(runtime.status()) + repr(self.logger.mock_calls))

    async def test_cancelled_binding_send_recovers_as_cancelled_without_reemitting(self):
        entered = asyncio.Event()

        async def sender(effect):
            entered.set()
            await asyncio.Event().wait()

        protocol = ControlledProtocol()
        first = self.runtime(protocol=protocol, binding_sender=sender)
        await first.start()
        gateway = await protocol.next_ready()
        incoming = asyncio.create_task(gateway.emit("/bind create", native_id="TEST_UNCERTAIN_BIND"))
        await asyncio.wait_for(entered.wait(), 5)
        before = (await asyncio.to_thread(self.query, "SELECT state,code_hmac,expires_at FROM qq_official_binding_challenges"))[0]
        self.assertEqual(before["state"], "sending")
        self.assertTrue(before["code_hmac"])
        await first.stop()
        await asyncio.gather(incoming, return_exceptions=True)
        replacement_sender = AsyncMock(return_value="success")
        second_protocol = ControlledProtocol()
        second = self.runtime(protocol=second_protocol, binding_sender=replacement_sender)
        await second.start()
        await second_protocol.next_ready()
        after = (await asyncio.to_thread(self.query, "SELECT state,code_hmac,expires_at FROM qq_official_binding_challenges"))[0]
        self.assertEqual(after, {"state": "cancelled", "code_hmac": "", "expires_at": before["expires_at"]})
        replacement_sender.assert_not_awaited()

    async def test_configuration_failure_is_sanitized_and_never_opens_protocol(self):
        protocol = ControlledProtocol()
        runtime = self.runtime(protocol=protocol)
        runtime.config.load.side_effect = RuntimeError("TEST_SECRET_CONFIG_PATH")
        with self.assertRaisesRegex(RuntimeError, "could not initialize") as error:
            await runtime.start()
        self.assertNotIn("TEST_SECRET_CONFIG_PATH", str(error.exception))
        self.assertEqual(protocol.gateways, [])
        self.assertEqual(runtime.status()["state"], "failed")



    async def check_interrupted_initialization(self, *, cancel):
        entered, release = threading.Event(), threading.Event()
        owned = []
        original_ledger = MessageLedger
        original_jobs = DurableJobStore

        def jobs(*args, **kwargs):
            result = original_jobs(*args, **kwargs)
            result.close = Mock(wraps=result.close)
            owned.append(result)
            self.addCleanup(result.close)
            return result

        def ledger(*args, **kwargs):
            result = original_ledger(*args, **kwargs)
            result.close = Mock(wraps=result.close)
            owned.append(result)
            self.addCleanup(result.close)
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test initialization release was not signaled")
            return result

        self.addCleanup(release.set)
        protocol = ControlledProtocol()
        runtime = self.runtime(protocol=protocol)
        with patch.object(runtime_module, "MessageLedger", side_effect=ledger), \
                patch.object(runtime_module, "DurableJobStore", side_effect=jobs):
            starting = asyncio.create_task(runtime.start())
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            if cancel:
                starting.cancel()
                stopping = None
            else:
                stopping = asyncio.create_task(runtime.stop())
            for _ in range(10):
                await asyncio.sleep(0)
            self.assertFalse(starting.done())
            if stopping is not None:
                self.assertFalse(stopping.done())
            release.set()
            if cancel:
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(starting, 5)
            else:
                self.assertFalse(await asyncio.wait_for(starting, 5))
                await asyncio.wait_for(stopping, 5)
        self.assertEqual(len(owned), 2)
        for store in owned:
            store.close.assert_called_once()
        self.assertEqual(protocol.gateways, [])
        self.assertIsNone(runtime.store)
        self.assertIsNone(runtime.jobs)
        self.assertIsNone(runtime.ledger)
        self.assertEqual(runtime.state, "stopped")

    async def test_cancel_start_drains_initialization_and_closes_constructed_stores(self):
        await self.check_interrupted_initialization(cancel=True)

    async def test_stop_during_start_prevents_gateway_and_closes_constructed_stores(self):
        await self.check_interrupted_initialization(cancel=False)
