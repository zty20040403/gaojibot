"""Project image routing and durable delivery using PG/RPC and a fixture QQ API."""

import asyncio
import base64
from dataclasses import replace
import unittest
from unittest.mock import patch

import nonebot

nonebot.init()

from src.plugins.ai_chat.bridges import BridgeOutcomeUnknown, BridgePermanentError
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.onebot_codec import scope_from_event
from src.plugins.ai_chat.project_file_outbox import MAX_IMAGE_BYTES
from src.plugins.ai_chat.project_file_transport import deliver_file, reconcile_files
from src.plugins.ai_chat.projects import ProjectAccess, ProjectConflict
from src.plugins.ai_chat.tools import domain_registry
from tests import test_project_file_outbox as fixture
from tests.postgres_fixture import postgres_for_test
from tests.test_routine_tools import event


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)


class ProjectImageOutboxTests(unittest.IsolatedAsyncioTestCase):
    call = fixture.ProjectFileOutboxTests.call
    claim = fixture.ProjectFileOutboxTests.claim

    async def asyncSetUp(self):
        await fixture.ProjectFileOutboxTests.asyncSetUp(self)
        self.image_response = {"message_id": -901}
        previous = self.bot.call_api.side_effect

        async def api(name, **args):
            if name in {"send_group_msg", "send_private_msg"}:
                self.uploads.append((name, args))
                return self.image_response
            return await previous(name, **args)

        self.bot.call_api.side_effect = api

    async def queue_image(
        self, *, content=PNG, path="figure.png", call_id="image", context=None, pid=None
    ):
        pid = pid or self.pid
        row = self.runtime.store.get(self.access, pid)
        await self.driver.write_file(row["scope_key"], row["sandbox_id"], path, content)
        result = await self.call(
            "send_image_from_sandbox", {"path": path}, call_id=call_id, context=context
        )
        return result

    async def test_registered_optional_sandbox_and_immutable_project_image_receipt(
        self,
    ):
        definitions = domain_registry.definitions(self.context)
        definition = next(
            d for d in definitions if d["function"]["name"] == "send_image_from_sandbox"
        )
        self.assertNotIn("sandbox_id", definition["function"]["parameters"]["required"])
        result = await self.queue_image()
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["queued"])
        self.assertFalse(result["delivered"])
        self.assertEqual(result["media_kind"], "image")
        self.assertFalse(self.uploads)
        delivery = self.claim()
        self.assertEqual(delivery.body.nodes[0].media_kind, "image")
        row = self.runtime.store.get(self.access, self.pid)
        await self.driver.write_file(
            row["scope_key"], row["sandbox_id"], "figure.png", b"changed"
        )
        self.assertEqual(self.runtime.files.prepare(self.access, delivery), PNG)
        self.runtime.store.create(self.access, "another", quota_bytes=64 * 1024**2)
        replay = await self.call(
            "send_image_from_sandbox", {"path": "figure.png"}, call_id="image"
        )
        self.assertEqual(replay, result)
        await deliver_file(self.app, self.bot, delivery)
        sent = self.deliveries.get(delivery.delivery_id)
        self.assertEqual((sent.status, sent.native_message_id), ("committed", "-901"))
        name, args = self.uploads[0]
        self.assertEqual(name, "send_group_msg")
        self.assertEqual(args["group_id"], 101)
        self.assertEqual(args["message"][0].type, "image")
        self.assertEqual(
            base64.b64decode(args["message"][0].data["file"].removeprefix("base64://")),
            PNG,
        )
        self.assertEqual(
            self.runtime.files.status(self.access, self.pid)[0]["media_kind"], "image"
        )

    async def test_private_image_uses_original_private_recipient(self):
        private_event = event(group=None)
        self.access = ProjectAccess(
            scope_from_event(private_event), self.access.principal_id, member=True
        )
        project = self.runtime.store.create(
            self.access, "private", quota_bytes=64 * 1024**2
        )
        self.runtime.lifecycle.request(
            self.access,
            project["project_id"],
            "wake",
            expected_version=project["version"],
            request_key="private-wake",
        )
        await self.worker.run_once()
        await self.runtime.sampler.run_once()
        context = replace(self.context, event=private_event, journal_turn_id=None)
        result = await self.queue_image(context=context, pid=project["project_id"])
        self.assertTrue(result["ok"], result)
        await deliver_file(self.app, self.bot, self.claim())
        name, args = self.uploads[0]
        self.assertEqual((name, args["user_id"]), ("send_private_msg", 201))
        self.assertNotIn("group_id", args)

    async def test_missing_or_invalid_message_receipt_never_means_delivered(self):
        for i, value in enumerate(
            (None, False, True, 0, "0", "", "file:1", "1.5", 1.5)
        ):
            with self.subTest(value=value):
                result = await self.queue_image(call_id=f"invalid-{i}")
                self.assertTrue(result["ok"], result)
                delivery = self.claim()
                self.image_response = {"message_id": value}
                with self.assertRaises(BridgeOutcomeUnknown):
                    await deliver_file(self.app, self.bot, delivery)
                self.deliveries.mark_ambiguous(delivery.delivery_id, "no receipt")
                await reconcile_files(self.app, [self.bot])
                self.assertEqual(
                    self.deliveries.get(delivery.delivery_id).status, "ambiguous"
                )
        self.assertEqual(len(self.uploads), 9)

    async def test_timeout_restart_and_explicit_retry_cannot_repeat_image(self):
        result = await self.queue_image()
        delivery = self.claim()
        previous = self.bot.call_api.side_effect

        async def lost(name, **args):
            await previous(name, **args)
            raise TimeoutError("image accepted, reply lost")

        self.bot.call_api.side_effect = lost
        with self.assertRaises(BridgeOutcomeUnknown):
            await deliver_file(self.app, self.bot, delivery)
        reopened = DeliveryStore(postgres_for_test(self))
        self.addCleanup(reopened.close)
        await reconcile_files(self.app, [self.bot])
        self.assertEqual(reopened.get(delivery.delivery_id).status, "ambiguous")
        self.assertIsNone(
            self.deliveries.reconcile_echo(
                self.scope, delivery.body, native_message_id="808"
            )
        )
        with self.assertRaises(BridgeOutcomeUnknown):
            await deliver_file(self.app, self.bot, delivery)
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(self.runtime.files.prepare(self.access, delivery), PNG)
        self.assertEqual(
            await self.call(
                "send_image_from_sandbox", {"path": "figure.png"}, call_id="image"
            ),
            result,
        )
        project = self.runtime.store.get(self.access, self.pid)
        with self.assertRaises(ProjectConflict):
            self.runtime.lifecycle.request(
                self.access,
                self.pid,
                "stop",
                expected_version=project["version"],
                request_key="blocked",
            )

    async def test_disabled_image_tool_revocation_wrong_bot_and_scope_do_not_send(self):
        await self.queue_image()
        delivery = self.claim()
        with patch(
            "src.plugins.ai_chat.project_file_transport.tool_enabled",
            side_effect=lambda name: name != "send_image_from_sandbox",
        ):
            await deliver_file(self.app, self.bot, delivery)
        self.assertIsNone(self.runtime.files.identity(delivery)["upload_started_at"])
        self.assertFalse(self.uploads)
        for scope in ("102", "201"):
            with self.assertRaises(BridgePermanentError):
                await deliver_file(
                    self.app,
                    self.bot,
                    replace(delivery, target_native_conversation_id=scope),
                )
        self.bot.self_id = "302"
        with self.assertRaises(BridgePermanentError):
            await deliver_file(self.app, self.bot, delivery)
        self.bot.self_id = "301"
        self.bot.get_group_member_list.return_value = [{"user_id": 301}]
        with self.assertRaises(BridgePermanentError):
            await deliver_file(self.app, self.bot, delivery)
        self.assertFalse(self.uploads)

    async def test_non_images_and_oversized_bytes_do_not_enter_cas_or_queue(self):
        for i, content in enumerate(
            (b"<html>not an image</html>", b"", PNG + b"x" * MAX_IMAGE_BYTES)
        ):
            with self.subTest(i=i):
                result = await self.queue_image(content=content, call_id=f"bad-{i}")
                self.assertFalse(result["ok"], result)
                self.assertIn(
                    result["code"],
                    {"project_attachment_invalid", "project_read_failed"},
                )
                self.assertIsNone(self.runtime.io.status(self.access, self.pid))
        self.assertFalse(self.deliveries.claim_due())
        self.assertEqual(
            self.runtime.usage.status(self.access, self.pid)["delivery_bytes"], 0
        )

    async def test_read_revocation_discards_image_before_queue(self):
        read = self.driver.read_file.side_effect

        async def revoked(*args, **kwargs):
            content = await read(*args, **kwargs)
            self.bot.get_group_member_list.return_value = [{"user_id": 301}]
            return content

        with patch.object(self.driver, "read_file", side_effect=revoked):
            result = await self.queue_image()
        self.assertEqual(result["code"], "project_forbidden")
        self.assertFalse(self.deliveries.claim_due())
        self.assertFalse(self.uploads)

    async def test_cancelled_send_is_not_repeated_and_copy_is_retained(self):
        await self.queue_image()
        delivery = self.claim()
        started = asyncio.Event()

        async def wait(name, **args):
            self.uploads.append((name, args))
            started.set()
            await asyncio.Future()

        self.bot.call_api.side_effect = wait
        task = asyncio.create_task(deliver_file(self.app, self.bot, delivery))
        try:
            async with asyncio.timeout(5):
                await started.wait()
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.deliveries.park_interrupted_attempts()
        await reconcile_files(self.app, [self.bot])
        with self.assertRaises(BridgeOutcomeUnknown):
            await deliver_file(self.app, self.bot, delivery)
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(self.runtime.files.prepare(self.access, delivery), PNG)
