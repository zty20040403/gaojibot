"""Real PG/RPC attachment staging and recovery, with no live QQ uploads."""

import asyncio
from dataclasses import replace
import time
from unittest.mock import AsyncMock, patch
import unittest

import nonebot
from nonebot.adapters.onebot.v11 import Bot

nonebot.init()

from src.plugins.ai_chat.bridges import BridgeOutcomeUnknown, BridgePermanentError
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.project_file_outbox import ProjectFileOutbox
from src.plugins.ai_chat.project_file_transport import deliver_file, reconcile_files
from src.plugins.ai_chat.projects import ProjectConflict
from src.plugins.ai_chat.project_io import ProjectIOStore
from src.plugins.ai_chat.onebot_delivery import OneBotDelivery
from src.plugins.ai_chat.projects import ProjectAccess
from src.plugins.ai_chat.tools.state import ToolContext
from tests.postgres_fixture import postgres_for_test
from tests import test_project_tool_executor as fixture
from tests.test_routine_tools import event


class ProjectFileOutboxTests(unittest.IsolatedAsyncioTestCase):
    call = fixture.ProjectToolExecutorTests.call

    async def asyncSetUp(self):
        await fixture.ProjectToolExecutorTests.asyncSetUp(self)
        self.deliveries = DeliveryStore(postgres_for_test(self))
        self.addCleanup(self.deliveries.close)
        self.app.delivery_store = self.deliveries
        self.runtime.files = ProjectFileOutbox(self.runtime.io, self.deliveries)
        self.root_files = []
        self.uploads = []

        async def api(api_name, **args):
            if api_name == "get_group_root_files":
                return {"files": self.root_files}
            if api_name in {"upload_group_file", "upload_private_file"}:
                self.uploads.append(args)
                return {}
            raise AssertionError(api_name)

        self.bot.call_api = AsyncMock(side_effect=api)
        self.enterContext(
            patch(
                "src.plugins.ai_chat.project_file_transport.tool_enabled",
                return_value=True,
            )
        )

    async def queued(self, *, call_id="send", name="report.txt", content="hello"):
        result = await self.call(
            "sandbox_write_file", {"path": name, "content": content}
        )
        self.assertTrue(result["ok"], result)
        result = await self.call(
            "send_file_from_sandbox", {"path": name}, call_id=call_id
        )
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["delivered"])
        self.assertEqual(self.uploads, [])
        return result, self.deliveries.get(result["delivery_id"])

    def claim(self):
        return self.deliveries.claim_due()[0]

    def receipt(self, delivery, **changes):
        record = self.runtime.files.identity(delivery)
        return {
            "file_id": "received-1",
            "file_name": record["filename"],
            "file_size": record["byte_size"],
            "uploader": int(self.bot.self_id),
            "upload_time": int(time.time()),
            **changes,
        }

    async def test_immutable_queue_replay_and_original_project_after_focus_change(self):
        result, delivery = await self.queued()
        await self.call(
            "sandbox_write_file", {"path": "report.txt", "content": "changed"}
        )
        self.assertEqual(self.runtime.files.prepare(self.access, delivery), b"hello")
        self.runtime.store.create(self.access, "Other", quota_bytes=64 * 1024**2)
        self.runtime.files = ProjectFileOutbox(
            ProjectIOStore(self.runtime.store, self.runtime.usage), self.deliveries
        )
        before = len(self.effects)
        replay = await self.call(
            "send_file_from_sandbox", {"path": "report.txt"}, call_id="send"
        )
        self.assertEqual(replay, result)
        self.assertEqual(len(self.effects), before)
        self.assertEqual(len(self.deliveries.claim_due()), 1)

    async def test_group_receipt_required_and_reconciled_without_second_upload(self):
        _, delivery = await self.queued()
        claimed = self.claim()
        with self.assertRaises(BridgeOutcomeUnknown):
            await deliver_file(self.app, self.bot, claimed)
        self.deliveries.mark_ambiguous(delivery.delivery_id, "lost reply")
        self.root_files.append(self.receipt(delivery))
        await reconcile_files(self.app, [self.bot])
        self.assertEqual(self.deliveries.get(delivery.delivery_id).status, "committed")
        self.assertEqual(len(self.uploads), 1)
        await reconcile_files(self.app, [self.bot])
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(
            self.runtime.files.status(self.access, self.pid)[0]["status"], "committed"
        )

    async def test_upload_timeout_then_restart_does_not_repeat(self):
        _, delivery = await self.queued()
        api = self.bot.call_api.side_effect

        async def timeout(api_name, **args):
            if api_name == "upload_group_file":
                await api(api_name, **args)
                self.root_files.append(self.receipt(delivery))
                raise TimeoutError("accepted remotely")
            return await api(api_name, **args)

        self.bot.call_api.side_effect = timeout
        with self.assertRaises(BridgeOutcomeUnknown):
            await deliver_file(self.app, self.bot, self.claim())
        reopened = DeliveryStore(postgres_for_test(self))
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get(delivery.delivery_id).status, "ambiguous")
        await reconcile_files(self.app, [self.bot])
        self.assertEqual(reopened.get(delivery.delivery_id).status, "committed")
        self.assertEqual(len(self.uploads), 1)

    async def test_crash_before_upload_and_before_read_settlement_recovers(self):
        await self.call(
            "sandbox_write_file", {"path": "report.txt", "content": "hello"}
        )
        original = self.runtime.io.observe_result

        def interrupted(grant, result):
            original(grant, result)
            raise OSError("process lost after atomic queue commit")

        with patch.object(self.runtime.io, "observe_result", side_effect=interrupted):
            result = await self.call("send_file_from_sandbox", {"path": "report.txt"})
        self.assertFalse(result["ok"])
        self.assertEqual(
            self.runtime.io.status(self.access, self.pid)["state"], "needs_attention"
        )
        delivery = self.claim()
        reopened = DeliveryStore(postgres_for_test(self))
        self.addCleanup(reopened.close)
        await reconcile_files(self.app, [self.bot])
        self.assertIsNone(self.runtime.io.status(self.access, self.pid))
        self.assertEqual(reopened.get(delivery.delivery_id).status, "pending")
        self.assertEqual(self.uploads, [])
        self.root_files.append(self.receipt(delivery))
        await deliver_file(self.app, self.bot, self.claim())
        self.assertEqual(reopened.get(delivery.delivery_id).status, "committed")
        self.assertEqual(len(self.uploads), 1)

    async def test_wrong_bot_scope_removed_member_and_disabled_group_never_upload(self):
        _, delivery = await self.queued()
        claimed = self.claim()
        self.bot.self_id = "999"
        with self.assertRaises(BridgePermanentError):
            await deliver_file(self.app, self.bot, claimed)
        self.bot.self_id = "301"
        wrong = replace(claimed, target_native_conversation_id="999")
        with self.assertRaises(BridgePermanentError):
            await deliver_file(self.app, self.bot, wrong)
        self.bot.get_group_member_list.return_value = [{"user_id": 301}]
        with self.assertRaises(BridgePermanentError):
            await deliver_file(self.app, self.bot, claimed)
        self.bot.get_group_member_list.return_value = [
            {"user_id": 201},
            {"user_id": 301},
        ]
        self.handlers.group_enabled.return_value = False
        with self.assertRaises(BridgePermanentError):
            await deliver_file(self.app, self.bot, claimed)
        self.assertIsNone(self.runtime.files.identity(delivery)["upload_started_at"])
        self.assertEqual(self.uploads, [])

    async def test_missing_wrong_and_old_receipts_are_not_success(self):
        _, delivery = await self.queued()
        self.root_files = [
            self.receipt(delivery, uploader=202),
            self.receipt(delivery, file_size=999),
            self.receipt(delivery, upload_time=1),
            self.receipt(delivery, file_id=""),
        ]
        with self.assertRaises(BridgeOutcomeUnknown):
            await deliver_file(self.app, self.bot, self.claim())
        self.deliveries.mark_ambiguous(delivery.delivery_id, "unconfirmed")
        await reconcile_files(self.app, [self.bot])
        self.assertEqual(self.deliveries.get(delivery.delivery_id).status, "ambiguous")
        self.assertEqual(len(self.uploads), 1)

    async def test_pending_or_failed_delivery_blocks_lifecycle_and_survives_gc(self):
        _, delivery = await self.queued()
        for status in ("pending", "failed"):
            if status == "failed":
                self.deliveries.mark_failed(
                    self.claim().delivery_id, "failed", retryable=False
                )
            project = self.runtime.store.get(self.access, self.pid)
            with self.assertRaises(ProjectConflict):
                self.runtime.lifecycle.request(
                    self.access,
                    self.pid,
                    "stop",
                    expected_version=project["version"],
                    request_key="stop",
                )
            self.runtime.store.collect_blobs(now=int(time.time()) + 2 * 86400)
            self.assertEqual(
                self.runtime.files.prepare(self.access, delivery), b"hello"
            )

    async def test_confirmed_copy_released_and_quota_accounted(self):
        _, delivery = await self.queued()
        self.assertEqual(
            self.runtime.usage.status(self.access, self.pid)["delivery_bytes"], 5
        )
        self.root_files.append(self.receipt(delivery))
        await deliver_file(self.app, self.bot, self.claim())
        now = int(time.time())
        self.runtime.store.collect_blobs(now=now)
        self.assertIsNone(self.runtime.files.identity(delivery)["blob_sha256"])
        self.assertEqual(
            self.runtime.usage.status(self.access, self.pid)["delivery_bytes"], 0
        )
        removed = self.runtime.store.collect_blobs(now=now + 86401)
        self.assertIn(self.runtime.files.identity(delivery)["sha256"], removed)
        self.assertEqual(self.deliveries.get(delivery.delivery_id).status, "committed")

    async def test_quota_refusal_creates_no_outbox_or_blob_and_releases_read(self):
        await self.call(
            "sandbox_write_file", {"path": "report.txt", "content": "hello"}
        )
        with self.runtime.store._transaction() as cursor:
            cursor.execute(
                "UPDATE project_scope_limits SET quota_bytes=1 WHERE scope_key=?",
                (self.scope.key,),
            )
        result = await self.call("send_file_from_sandbox", {"path": "report.txt"})
        self.assertEqual(result["code"], "project_scope_quota_full")
        self.assertEqual(self.deliveries.claim_due(), [])
        self.assertEqual(
            list(self.runtime.store.blobs.orphan_candidates(int(time.time()) + 1)), []
        )
        self.assertIsNone(self.runtime.io.status(self.access, self.pid))

    async def test_concurrent_dispatch_cancellation_and_explicit_retry_never_upload_twice(
        self,
    ):
        _, delivery = await self.queued()
        claimed = self.claim()
        started = asyncio.Event()

        async def suspended(api_name, **args):
            if api_name == "upload_group_file":
                self.uploads.append(args)
                started.set()
                await asyncio.Future()
            return {"files": []}

        self.bot.call_api.side_effect = suspended
        task = asyncio.create_task(deliver_file(self.app, self.bot, claimed))
        done, _ = await asyncio.wait(
            (task, asyncio.create_task(started.wait())),
            timeout=5,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            await task
        self.assertTrue(started.is_set())
        with self.assertRaises(BridgeOutcomeUnknown):
            await deliver_file(self.app, self.bot, claimed)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.deliveries.park_interrupted_attempts()
        with self.assertRaises(BridgeOutcomeUnknown):
            await deliver_file(self.app, self.bot, claimed)
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(self.deliveries.get(delivery.delivery_id).status, "ambiguous")

    async def test_transport_worker_uses_bound_account_not_first_online_bot(self):
        _, delivery = await self.queued()
        self.root_files.append(self.receipt(delivery))
        self.app.logger = unittest.mock.Mock()
        handler = OneBotDelivery(self.handlers)
        wrong = unittest.mock.Mock(spec=Bot, self_id="999")
        with (
            patch(
                "src.plugins.ai_chat.onebot_delivery.get_bots",
                return_value={"999": wrong, "301": self.bot},
            ),
            patch(
                "src.plugins.ai_chat.onebot_delivery.delivery_blocker",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "src.plugins.ai_chat.onebot_delivery.asyncio.sleep",
                new=AsyncMock(side_effect=[None, asyncio.CancelledError]),
            ),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await handler._delivery_loop()
        self.assertEqual(self.deliveries.get(delivery.delivery_id).status, "committed")
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(self.uploads[0]["group_id"], 101)

    async def test_tool_disabled_defers_without_spending_attempt(self):
        _, delivery = await self.queued()
        with patch(
            "src.plugins.ai_chat.project_file_transport.tool_enabled",
            return_value=False,
        ):
            await deliver_file(self.app, self.bot, self.claim())
        record = self.deliveries.get(delivery.delivery_id)
        self.assertEqual((record.status, record.attempts), ("pending", 0))
        self.assertEqual(self.uploads, [])

    async def test_corrupt_copy_is_not_uploaded(self):
        _, delivery = await self.queued()
        digest = self.runtime.files.identity(delivery)["sha256"]
        path = self.runtime.store.blobs.path(digest)
        path.chmod(0o600)
        path.write_bytes(b"wrong")
        path.chmod(0o400)
        with self.assertRaises(BridgePermanentError):
            await deliver_file(self.app, self.bot, self.claim())
        self.assertEqual(self.uploads, [])

    async def test_cancel_after_dispatch_retains_file_until_actual_receipt(self):
        _, delivery = await self.queued()
        with self.assertRaises(BridgeOutcomeUnknown):
            await deliver_file(self.app, self.bot, self.claim())
        self.deliveries.mark_ambiguous(delivery.delivery_id, "unknown")
        self.assertTrue(self.deliveries.cancel(delivery.delivery_id))
        self.runtime.store.collect_blobs(now=int(time.time()) + 86401)
        self.assertEqual(self.runtime.files.prepare(self.access, delivery), b"hello")
        project = self.runtime.store.get(self.access, self.pid)
        with self.assertRaises(ProjectConflict):
            self.runtime.lifecycle.request(
                self.access,
                self.pid,
                "stop",
                expected_version=project["version"],
                request_key="cancelled",
            )
        self.root_files.append(self.receipt(delivery))
        await reconcile_files(self.app, [self.bot])
        self.assertEqual(self.deliveries.get(delivery.delivery_id).status, "committed")

    async def test_cancel_before_dispatch_allows_copy_cleanup(self):
        _, delivery = await self.queued()
        self.assertTrue(self.deliveries.cancel(delivery.delivery_id))
        self.runtime.store.collect_blobs()
        self.assertIsNone(self.runtime.files.identity(delivery)["blob_sha256"])
        project = self.runtime.store.get(self.access, self.pid)
        self.runtime.lifecycle.request(
            self.access,
            self.pid,
            "stop",
            expected_version=project["version"],
            request_key="cancelled",
        )
        self.assertEqual(self.uploads, [])

    async def test_private_project_only_uploads_to_owner_and_does_not_assume_empty_ack(
        self,
    ):
        scope = replace(self.scope, kind="private", native_conversation_id="201")
        access = ProjectAccess(scope, self.access.principal_id, member=True)
        project = self.runtime.store.create(access, "Private", quota_bytes=64 * 1024**2)
        self.runtime.lifecycle.request(
            access,
            project["project_id"],
            "wake",
            expected_version=project["version"],
            request_key="private",
        )
        await self.worker.run_once()
        await self.runtime.sampler.run_once()
        context = ToolContext(self.handlers, event(group=None), frozenset({"projects"}))
        written = await self.call(
            "sandbox_write_file",
            {"path": "mine.txt", "content": "private"},
            context=context,
        )
        self.assertTrue(written["ok"], written)
        result = await self.call(
            "send_file_from_sandbox", {"path": "mine.txt"}, context=context
        )
        self.assertTrue(result["ok"], result)
        with self.assertRaises(BridgeOutcomeUnknown):
            await deliver_file(self.app, self.bot, self.claim())
        self.assertEqual(self.uploads[0]["user_id"], 201)
        self.assertNotIn("group_id", self.uploads[0])
        with self.assertRaises(PermissionError):
            self.runtime.files.prepare(
                self.access, self.deliveries.get(result["delivery_id"])
            )

    async def test_unknown_accounting_does_not_stage_or_stall_read(self):
        await self.call(
            "sandbox_write_file", {"path": "report.txt", "content": "hello"}
        )
        with self.runtime.store._transaction() as cursor:
            cursor.execute(
                "UPDATE project_usage SET complete=FALSE WHERE project_id=?",
                (self.pid,),
            )
        result = await self.call("send_file_from_sandbox", {"path": "report.txt"})
        self.assertEqual(result["code"], "project_scope_usage_unknown")
        self.assertIsNone(self.runtime.io.status(self.access, self.pid))
        self.assertEqual(self.deliveries.claim_due(), [])


if __name__ == "__main__":
    unittest.main()
