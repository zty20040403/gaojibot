from __future__ import annotations

from tests.postgres_fixture import postgres_for_test

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock

import httpx
import nonebot
from fastapi import FastAPI

nonebot.init()

from src.plugins.ai_chat.bridges import (
    BlueBubblesClient,
    BridgeEvent,
    BridgeManager,
    BridgeOutcomeUnknown,
    BridgePermanentError,
    BridgeRetryableError,
    MatrixClient,
    MirrorRouter,
    MirrorStateStore,
    decode_bluebubbles_webhook,
    decode_matrix_event,
    register_bridge_routes,
)
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode


ROUTES = json.dumps(
    [
        {
            "name": "main",
            "endpoints": [
                {
                    "platform": "onebot-v11",
                    "kind": "group",
                    "id": "100",
                    "bot_user_id": "999",
                },
                {
                    "platform": "matrix",
                    "kind": "group",
                    "id": "!room:example.org",
                    "bot_user_id": "@bot:example.org",
                },
            ],
        }
    ]
)


class BridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = MirrorRouter.from_json(ROUTES)
        self.ledger = MessageLedger(postgres_for_test(self))
        self.deliveries = DeliveryStore(postgres_for_test(self))
        self.state = MirrorStateStore(postgres_for_test(self))
        self.manager = BridgeManager(
            self.router,
            self.ledger,
            self.deliveries,
            self.state,
        )
        self.addCleanup(self.ledger.close)
        self.addCleanup(self.deliveries.close)
        self.addCleanup(self.state.close)

    def test_router_prefers_onebot_as_canonical_and_rejects_overlap(self) -> None:
        matrix = ConversationScope("matrix", "group", "!room:example.org")
        self.assertEqual(
            self.router.canonical_scope(matrix).key,
            "onebot-v11:group:100",
        )
        with self.assertRaises(ValueError):
            MirrorRouter.from_json(
                json.dumps(
                    [
                        json.loads(ROUTES)[0],
                        {
                            "name": "other",
                            "endpoints": [
                                {"platform": "onebot-v11", "id": "100"},
                                {"platform": "imessage", "id": "chat"},
                            ],
                        },
                    ]
                )
            )

        with self.assertRaisesRegex(ValueError, "keep OneBot as canonical"):
            MirrorRouter.from_json(
                json.dumps(
                    [
                        {
                            "name": "invalid-canonical",
                            "endpoints": [
                                {"platform": "onebot-v11", "id": "101"},
                                {
                                    "platform": "matrix",
                                    "id": "!other:example.org",
                                    "canonical": True,
                                },
                            ],
                        }
                    ]
                )
            )

    def test_mirror_deduplicates_and_maps_reply_to_native_copy(self) -> None:
        onebot = ConversationScope("onebot-v11", "group", "100")
        first = BridgeEvent(
            scope=onebot,
            native_event_id="101",
            sender_native_user_id="7",
            sender_display="Alice",
            body=MessageBody((TextNode(0, "hello"),)),
            occurred_at=100,
        )
        ingested = self.manager.ingest(first)
        duplicate = self.manager.ingest(first)
        self.assertEqual(ingested.status, "ingested")
        self.assertEqual(ingested.deliveries_created, 1)
        self.assertEqual(duplicate.status, "duplicate")
        delivery = self.deliveries.recent(limit=10)[0]
        self.assertIn("<Alice>", str(delivery.body.nodes[0]))

        claimed = self.deliveries.claim_due(now=100)[0]
        self.deliveries.mark_committed(
            claimed.delivery_id,
            native_message_id="$matrix-copy",
            now=101,
        )
        self.state.confirm_delivery(claimed.delivery_id, "$matrix-copy")

        matrix_reply = BridgeEvent(
            scope=ConversationScope("matrix", "group", "!room:example.org"),
            native_event_id="$reply",
            sender_native_user_id="@bob:example.org",
            sender_display="Bob",
            body=MessageBody((TextNode(0, "reply"),)),
            occurred_at=102,
            reply_to_native_message_id="$matrix-copy",
        )
        self.manager.ingest(matrix_reply)
        reply_delivery = self.deliveries.recent(limit=10)[0]
        self.assertEqual(reply_delivery.target_platform, "onebot-v11")
        self.assertEqual(reply_delivery.reply_to_native_message_id, "101")

    def test_matrix_send_uses_stable_transaction_id_and_native_reply(self) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(404, json={"errcode": "M_NOT_FOUND"})
            return httpx.Response(200, json={"event_id": "$sent"})

        async def run() -> str:
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            matrix = MatrixClient(
                "https://matrix.example.org",
                "secret",
                user_id="@bot:example.org",
                client=client,
            )
            delivery, _ = self.deliveries.enqueue(
                idempotency_key="same-operation",
                source_scope_key="onebot-v11:group:100",
                target_scope=ConversationScope(
                    "matrix", "group", "!room:example.org"
                ),
                body=MessageBody((TextNode(0, "hello"),)),
                reply_to_native_message_id="$parent",
                now=100,
            )
            result = await matrix.send(delivery)
            await client.aclose()
            return result

        result = asyncio.run(run())
        self.assertEqual(result, "$sent")
        self.assertEqual(requests[0].method, "GET")
        self.assertEqual(requests[1].method, "PUT")
        self.assertIn("/send/m.room.message/", requests[1].url.path)
        payload = json.loads(requests[1].content)
        self.assertEqual(
            payload["m.relates_to"]["m.in_reply_to"]["event_id"],
            "$parent",
        )

    def test_queued_bridge_messages_require_the_original_and_target_mapping(self):
        self.manager.matrix = Mock()
        source = ConversationScope("matrix", "group", "!room:example.org")
        event = BridgeEvent(source, "$one", "@alice:example.org", "Alice", MessageBody((TextNode(0, "private text"),)), 100)
        self.manager.ingest(event)
        delivery = self.deliveries.get(self.deliveries.recent(limit=1)[0].delivery_id)
        self.manager.validate_delivery(delivery)
        self.state.register_source(delivery.source_canonical_message_id, delivery.target_scope, "echo", occurred_at=101)
        self.assertEqual(self.state.original_scope(delivery.source_canonical_message_id), source)
        changed = json.loads(ROUTES)
        changed[0]["endpoints"][1]["id"] = "!different:example.org"
        self.manager.router = MirrorRouter.from_json(json.dumps(changed))
        with self.assertRaises(BridgePermanentError):
            self.manager.validate_delivery(delivery)
        self.manager.router = self.router
        self.manager.matrix = None
        with self.assertRaises(BridgePermanentError):
            self.manager.validate_delivery(delivery)
        ignored = self.manager.ingest(BridgeEvent(ConversationScope("matrix", "group", "!unknown:example.org"), "$unknown",
            "@alice:example.org", "Alice", MessageBody((TextNode(0, "must not store"),)), 102))
        self.assertEqual(ignored.status, "ignored")

    def test_changed_target_is_rejected_before_transport_send(self):
        self.manager.matrix = Mock(send=AsyncMock())
        self.manager.ingest(BridgeEvent(ConversationScope("onebot-v11", "group", "100"), "101", "7", "Alice",
            MessageBody((TextNode(0, "hello"),)), 100))
        delivery = self.deliveries.get(self.deliveries.recent(limit=1)[0].delivery_id)
        self.manager.router = MirrorRouter()
        with self.assertRaises(BridgePermanentError):
            asyncio.run(self.manager.deliver(delivery))
        self.manager.matrix.send.assert_not_awaited()

    def test_matrix_does_not_send_plaintext_after_room_enables_encryption(self):
        async def run(status, payload):
            requests = []
            def handler(request):
                requests.append(request)
                return httpx.Response(status, json=payload)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                matrix = MatrixClient("https://matrix.example.org", "secret", user_id="@bot:example.org", client=client)
                delivery, _ = self.deliveries.enqueue(idempotency_key="check-encryption", source_scope_key="onebot-v11:group:100",
                    target_scope=ConversationScope("matrix", "group", "!room:example.org"), body=MessageBody((TextNode(0, "private text"),)))
                with self.assertRaises((BridgePermanentError, BridgeRetryableError)):
                    await matrix.send(delivery)
            self.assertEqual([request.method for request in requests], ["GET"])
        for status, payload in ((200, {"algorithm": "m.megolm.v1.aes-sha2"}), (404, {"detail": "missing"}), (500, {})):
            asyncio.run(run(status, payload))

    def test_matrix_failures_do_not_persist_upstream_body_or_token(self):
        secret = "synthetic-matrix-token-not-real"
        async def run():
            def handler(request):
                if request.url.path.endswith("/state/m.room.encryption/"):
                    return httpx.Response(404, json={"errcode": "M_NOT_FOUND"})
                return httpx.Response(403, json={"error": secret, "private_message": "room contents"})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                matrix = MatrixClient("https://matrix.example.org", secret, user_id="@bot:example.org", client=client)
                delivery, _ = self.deliveries.enqueue(idempotency_key="matrix-secret-check", source_scope_key="onebot-v11:group:100",
                    target_scope=ConversationScope("matrix", "group", "!room:example.org"), body=MessageBody((TextNode(0, "hello"),)))
                for operation in (lambda: matrix.send(delivery), lambda: matrix.sync(), lambda: matrix.backfill("!room:example.org", "cursor")):
                    with self.assertRaises((BridgePermanentError, BridgeRetryableError)) as error:
                        await operation()
                    self.assertEqual(str(error.exception), "Matrix HTTP 403")
        asyncio.run(run())

    def test_bluebubbles_timeout_is_outcome_unknown(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("late", request=request)

        async def run() -> None:
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            imessage = BlueBubblesClient(
                "http://bluebubbles.local",
                "secret",
                client=client,
            )
            delivery, _ = self.deliveries.enqueue(
                idempotency_key="imessage-operation",
                source_scope_key="onebot-v11:group:100",
                target_scope=ConversationScope("imessage", "group", "chat-guid"),
                body=MessageBody((TextNode(0, "hello"),)),
                now=100,
            )
            with self.assertRaises(BridgeOutcomeUnknown):
                await imessage.send(delivery)
            await client.aclose()

        asyncio.run(run())

    def test_decoders_and_authenticated_webhook(self) -> None:
        matrix = decode_matrix_event(
            "!room:example.org",
            {
                "type": "m.room.message",
                "event_id": "$event",
                "sender": "@alice:example.org",
                "origin_server_ts": 100000,
                "content": {
                    "msgtype": "m.text",
                    "body": "hello",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$old"}},
                },
            },
        )
        self.assertIsNotNone(matrix)
        self.assertEqual(matrix.reply_to_native_message_id, "$old")  # type: ignore[union-attr]
        self.assertEqual(matrix.sender_display, "Matrix 用户")  # type: ignore[union-attr]

        imessage = decode_bluebubbles_webhook(
            {
                "type": "new-message",
                "data": {
                    "guid": "i-message",
                    "text": "hi",
                    "dateCreated": 100,
                    "isFromMe": False,
                    "handle": {"address": "+86123"},
                    "chats": [{"guid": "chat-guid"}],
                },
            },
            configured_chat_guid="chat-guid",
        )
        self.assertIsNotNone(imessage)
        self.assertEqual(imessage.sender_display, "iMessage 用户")  # type: ignore[union-attr]

        app = FastAPI()
        register_bridge_routes(
            app,
            self.manager,
            matrix_appservice_token="matrix-secret",
        )

        async def request_route():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                denied = await client.put(
                    "/_matrix/app/v1/transactions/1", json={"events": []}
                )
                allowed = await client.put(
                    "/_matrix/app/v1/transactions/1",
                    headers={"Authorization": "Bearer matrix-secret"},
                    json={"events": []},
                )
                return denied, allowed

        denied, allowed = asyncio.run(request_route())
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(allowed.status_code, 200)

        unconfigured = FastAPI()
        register_bridge_routes(unconfigured, self.manager)

        async def request_unconfigured():
            transport = httpx.ASGITransport(app=unconfigured)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                return await client.put(
                    "/_matrix/app/v1/transactions/1",
                    json={"events": []},
                )

        self.assertEqual(asyncio.run(request_unconfigured()).status_code, 503)


if __name__ == "__main__":
    unittest.main()
