from __future__ import annotations

from tests.postgres_fixture import postgres_for_test

import asyncio
import json
import unittest
from unittest.mock import patch

import httpx
import nonebot

nonebot.init()

from src.plugins.ai_chat.bridges import (
    BlueBubblesClient, BridgeManager, BridgeOutcomeUnknown,
    BridgePermanentError, BridgeRetryableError, MirrorRouter, MirrorStateStore,
)
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode


START = 1_700_000_000_000


def routes(group="12345", *, second=False, matrix=False):
    bundles = [{"name": "test", "endpoints": [
        {"platform": "onebot-v11", "id": group, "bot_user_id": "99999"},
        {"platform": "imessage", "id": "chat-guid", "bot_user_id": "bot@example.invalid"},
    ]}]
    if matrix:
        bundles[0]["endpoints"].append({"platform": "matrix", "id": "!test:example.invalid"})
    if second:
        bundles.append({"name": "second", "endpoints": [
            {"platform": "onebot-v11", "id": "67890"},
            {"platform": "imessage", "id": "second-chat"},
        ]})
    return MirrorRouter.from_json(json.dumps(bundles))


def message(index, *, timestamp=None, chat="chat-guid", text="hello", own=False):
    return {"guid": f"event-{index}", "dateCreated": START + 1000 if timestamp is None else timestamp,
        "text": text, "isFromMe": own, "chats": [{"guid": chat}],
        "handle": {"address": "alice@example.invalid"} if not own else None}


class BlueBubblesPollTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ledger = MessageLedger(postgres_for_test(self))
        self.deliveries = DeliveryStore(postgres_for_test(self))
        self.state = MirrorStateStore(postgres_for_test(self))
        self.addCleanup(self.ledger.close)
        self.addCleanup(self.deliveries.close)
        self.addCleanup(lambda: self.state.close())
        self.messages, self.requests = [], []
        self.failure = None
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        self.addAsyncCleanup(self.http.aclose)
        self.client = BlueBubblesClient("https://bluebubbles.example.invalid", "synthetic-password",
            send_method="apple-script", client=self.http)
        self.manager = BridgeManager(routes(), self.ledger, self.deliveries, self.state,
            imessage=self.client, imessage_poll_enabled=True)
        self.scope = ConversationScope("imessage", "group", "chat-guid")

    def handler(self, request):
        self.requests.append(request)
        if self.failure:
            outcome = self.failure(request)
            if outcome is not None:
                return outcome
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.url.params["password"], "synthetic-password")
        self.assertEqual(request.url.params["sort"], "ASC")
        chat = request.url.path.split("/")[-2]
        after, before, offset, limit = (int(request.url.params[k]) for k in ("after", "before", "offset", "limit"))
        selected = sorted([raw for raw in self.messages if raw["chats"][0]["guid"] == chat
            and after <= raw["dateCreated"] <= before], key=lambda raw: (raw["dateCreated"], raw["guid"]))
        return httpx.Response(200, json={"status": 200, "data": selected[offset:offset + limit],
            "metadata": {"total": len(selected)}})

    async def tick(self, milliseconds):
        with patch("src.plugins.ai_chat.bridges.time.time", return_value=milliseconds / 1000):
            return await self.manager.sync_imessage_once()

    def cursor(self):
        endpoint = self.manager.router.endpoints("imessage")[0]
        return json.loads(self.state.get_cursor(self.manager._imessage_cursor_key(endpoint)))

    async def test_first_enable_skips_old_history_and_disabled_does_not_read(self):
        self.messages = [message("old", timestamp=START - 1000), message("new")]
        self.assertEqual(await self.tick(START), 0)
        self.assertEqual(self.requests, [])
        self.assertEqual(await self.tick(START + 5000), 1)
        self.assertFalse(self.state.source_seen(self.scope, "event-old"))
        self.assertTrue(self.state.source_seen(self.scope, "event-new"))
        self.manager.imessage_poll_enabled = False
        self.assertEqual(await self.tick(START + 10000), 0)
        self.assertEqual(len(self.requests), 1)

    async def test_inclusive_boundary_and_equal_timestamps_do_not_duplicate(self):
        self.messages = [message(index, timestamp=START + 3000) for index in range(450)]
        await self.tick(START)
        self.assertEqual(await self.tick(START + 5000), 400)
        self.assertEqual(self.cursor()["offset"], 400)
        self.assertEqual(await self.tick(START + 5000), 50)
        self.assertEqual(self.cursor()["start"], START + 3000)
        self.assertEqual(await self.tick(START + 10000), 0)
        self.assertEqual(await self.tick(START + 10000), 0)
        self.assertEqual(len(self.deliveries.recent(limit=500)), 450)

    async def test_failure_between_pages_and_process_restart_resume_saved_window(self):
        self.messages = [message(index) for index in range(450)]
        await self.tick(START)
        self.failure = lambda request: httpx.Response(503, json={"private": "secret body"}) if request.url.params["offset"] == "200" else None
        with self.assertRaisesRegex(BridgeRetryableError, "HTTP 503"):
            await self.tick(START + 5000)
        self.assertEqual((self.cursor()["offset"], self.cursor()["total"]), (200, 450))
        self.state.close()
        self.state = MirrorStateStore(postgres_for_test(self))
        self.manager = BridgeManager(
            routes(),
            self.ledger,
            self.deliveries,
            self.state,
            imessage=self.client,
            imessage_poll_enabled=True,
        )
        self.failure = None
        self.assertEqual(await self.tick(START + 15000), 250)
        self.assertEqual(self.cursor()["start"], START + 3000)
        self.assertEqual(len(self.deliveries.recent(limit=500)), 450)

    async def test_changed_page_count_replays_without_duplicating_outbox(self):
        self.messages = [message(f"{index:04}") for index in range(450)]
        await self.tick(START)
        await self.tick(START + 5000)
        self.messages.pop()
        self.assertEqual(await self.tick(START + 5000), 0)
        self.assertEqual(self.cursor()["offset"], 0)
        await self.tick(START + 5000)
        self.assertEqual(await self.tick(START + 5000), 49)
        self.assertEqual(len(self.deliveries.recent(limit=500)), 449)

    async def test_invalid_page_or_scope_does_not_advance_cursor(self):
        await self.tick(START)
        before = self.cursor()
        for raw in (message(1, chat="private-chat"), message(1, timestamp=START + 20000),
                    {**message(1), "dateCreated": None}, {**message(1), "guid": ""}):
            self.failure = lambda request, raw=raw: httpx.Response(200, json={"data": [raw], "metadata": {"total": 1}})
            with self.assertRaises(BridgeRetryableError):
                await self.tick(START + 5000)
            self.assertEqual(self.cursor(), before)
        self.assertEqual(self.deliveries.recent(), [])

    async def test_mapping_changed_while_reading_does_not_deliver_to_new_group(self):
        await self.tick(START)
        self.messages = [message(1)]
        def change(request):
            self.manager.router = routes("67890")
        self.failure = change
        with self.assertRaisesRegex(BridgeRetryableError, "mapping changed"):
            await self.tick(START + 5000)
        self.assertEqual(self.deliveries.recent(), [])
        self.failure = None
        self.assertEqual(await self.tick(START + 5000), 0)

    async def test_failed_chat_does_not_starve_another_mapping(self):
        self.manager.router = routes(second=True)
        self.messages = [message(1, chat="second-chat")]
        await self.tick(START)
        self.failure = lambda request: httpx.Response(404, json={}) if "chat-guid" in request.url.path else None
        with self.assertRaises(BridgeRetryableError):
            await self.tick(START + 5000)
        self.assertEqual(len(self.deliveries.recent()), 1)
        self.assertEqual(self.deliveries.recent()[0].target_native_conversation_id, "67890")

    async def test_partial_fanout_is_repaired_before_marking_source_consumed(self):
        self.manager.router = routes(matrix=True)
        self.messages = [message(1)]
        await self.tick(START)
        original = self.deliveries.enqueue
        calls = 0
        def enqueue(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated process interruption")
            return original(**kwargs)
        with patch.object(self.deliveries, "enqueue", side_effect=enqueue):
            with self.assertRaisesRegex(RuntimeError, "interruption"):
                await self.tick(START + 5000)
        self.assertFalse(self.state.source_seen(self.scope, "event-1"))
        self.assertEqual(len(self.deliveries.recent()), 1)
        self.assertEqual(await self.tick(START + 5000), 1)
        self.assertEqual(len(self.deliveries.recent()), 2)
        self.assertTrue(self.state.source_seen(self.scope, "event-1"))

    async def test_timeout_send_is_reconciled_by_polled_echo_without_resend(self):
        await self.tick(START)
        outbound, _ = self.deliveries.enqueue(idempotency_key="echo-test", source_scope_key="onebot-v11:group:12345",
            target_scope=self.scope, body=MessageBody((TextNode(0, "sent text"),)), now=START // 1000)
        self.deliveries.mark_ambiguous(outbound.delivery_id, "no receipt", now=START // 1000)
        self.messages = [message(1, text="sent text", own=True)]
        self.assertEqual(await self.tick(START + 5000), 0)
        self.assertEqual(self.deliveries.get(outbound.delivery_id).status, "committed")
        self.assertEqual(len(self.deliveries.recent()), 1)
        self.assertTrue(all(request.method == "GET" for request in self.requests))

    async def test_corrupt_cursor_is_preserved_and_no_history_is_read(self):
        key = self.manager._imessage_cursor_key(self.manager.router.endpoints("imessage")[0])
        self.state.set_cursor(key, "corrupt")
        with self.assertRaisesRegex(BridgeRetryableError, "cursor is invalid"):
            await self.tick(START)
        self.assertEqual(self.state.get_cursor(key), "corrupt")
        self.assertEqual(self.requests, [])

    async def test_cancellation_is_not_swallowed_and_preserves_progress(self):
        await self.tick(START)
        async def cancelled(*args, **kwargs):
            raise asyncio.CancelledError
        with patch.object(self.client, "messages", side_effect=cancelled):
            with self.assertRaises(asyncio.CancelledError):
                await self.tick(START + 5000)
        self.assertEqual(self.cursor()["start"], START)

    async def test_apple_script_uses_temp_guid_without_private_api_reply(self):
        def send(request):
            self.assertEqual(request.method, "POST")
            payload = json.loads(request.content)
            self.assertEqual(payload["method"], "apple-script")
            self.assertTrue(payload["tempGuid"].startswith("gaoji-"))
            self.assertNotIn("selectedMessageGuid", payload)
            return httpx.Response(200, json={"status": 200, "data": {"guid": "sent-guid", "error": 0}})
        self.failure = send
        record, _ = self.deliveries.enqueue(idempotency_key="script-send", source_scope_key="onebot-v11:group:12345",
            target_scope=self.scope, body=MessageBody((TextNode(0, "test"),)), reply_to_native_message_id="older")
        self.assertEqual(await self.client.send(record), "sent-guid")

    async def test_upstream_errors_never_include_password_or_body(self):
        record, _ = self.deliveries.enqueue(idempotency_key="bad-send", source_scope_key="onebot-v11:group:12345",
            target_scope=self.scope, body=MessageBody((TextNode(0, "test"),)))
        for status in (302, 401, 429, 500):
            self.failure = lambda request, status=status: httpx.Response(status,
                headers={"Location": "https://elsewhere.example.invalid/?secret=synthetic-password"},
                json={"error": "synthetic-password private body"})
            with self.assertRaises((BridgeOutcomeUnknown, BridgePermanentError)) as error:
                await self.client.send(record)
            self.assertEqual(str(error.exception), f"BlueBubbles HTTP {status}")
        self.assertEqual(len(self.requests), 4)
        def timeout(request):
            raise httpx.ReadTimeout(str(request.url))
        self.failure = timeout
        with self.assertRaisesRegex(BridgeOutcomeUnknown, "BlueBubbles transport failed") as error:
            await self.client.send(record)
        self.assertNotIn("synthetic-password", str(error.exception))

    async def test_invalid_json_after_send_is_not_retryable(self):
        self.failure = lambda request: httpx.Response(200, content=b"not json")
        record, _ = self.deliveries.enqueue(idempotency_key="invalid-send", source_scope_key="onebot-v11:group:12345",
            target_scope=self.scope, body=MessageBody((TextNode(0, "test"),)))
        with self.assertRaises(BridgeOutcomeUnknown):
            await self.client.send(record)
