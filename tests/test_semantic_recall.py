from __future__ import annotations

from tests.postgres_fixture import postgres_for_test

import asyncio
import importlib.util
from pathlib import Path
import sys
import unittest

import httpx
# This pure module can be tested without booting the entire NoneBot application.
_spec = importlib.util.spec_from_file_location("gaoji_test_semantic_recall", Path(__file__).parents[1] / "src/plugins/ai_chat/semantic_recall.py")
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)
EmbeddingClient = _module.EmbeddingClient
SemanticDocument = _module.SemanticDocument
SemanticHit = _module.SemanticHit
SemanticIndexState = _module.SemanticIndexState
SemanticRecallService = _module.SemanticRecallService


class MemoryVectorBackend:
    dimensions = 3

    def __init__(self) -> None:
        self.items = []

    def upsert(self, document, vector, *, model):
        self.items.append((document, list(vector), model))

    def search(self, scope_keys, query, vector, *, model, limit):
        return [
            SemanticHit(
                scope_key=self.items[0][0].scope_key,
                source_type=self.items[0][0].source_type,
                source_handle=self.items[0][0].source_handle,
                content=self.items[0][0].content,
                score=0.9,
                metadata=self.items[0][0].metadata,
            )
        ][:limit]

    def stats(self):
        return {"documents": len(self.items)}


class SemanticRecallTests(unittest.TestCase):
    def setUp(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            payload = __import__("json").loads(request.content)
            data = [
                {"index": index, "embedding": [1.0, 0.0, 0.5]}
                for index, _text in enumerate(payload["input"])
            ]
            return httpx.Response(200, json={"data": data})

        self.embedder = EmbeddingClient(
            base_url="https://embedding.test/v1",
            api_key="test-key",
            model="embed-test",
            dimensions=3,
            transport=httpx.MockTransport(handler),
        )
        self.backend = MemoryVectorBackend()
        self.service = SemanticRecallService(self.embedder, self.backend)

    def test_indexes_and_searches_with_scope(self) -> None:
        async def run():
            count = await self.service.index(
                [
                    SemanticDocument(
                        scope_key="onebot-v11:group:1",
                        source_type="message",
                        source_handle="msg#1",
                        content="Arch Linux package manager",
                        metadata={"sender": "[mention#1]"},
                    )
                ]
            )
            hits = await self.service.search(
                ["onebot-v11:group:1"],
                "pacman",
            )
            return count, hits

        count, hits = asyncio.run(run())
        self.assertEqual(count, 1)
        self.assertEqual(hits[0].source_handle, "msg#1")
        self.assertEqual(self.backend.items[0][2], "embed-test")

    def test_rejects_wrong_embedding_dimensions(self) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [1.0]}]},
            )

        client = EmbeddingClient(
            base_url="https://embedding.test/v1",
            api_key="test",
            model="embed-test",
            dimensions=3,
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaises(ValueError):
            asyncio.run(client.embed(["hello"]))

    def test_keyless_endpoint_omits_authorization_but_keeps_embedding_contract(self) -> None:
        captured = []
        async def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.2] * 1024}]})
        client = EmbeddingClient(base_url="https://keyless.test/v1", api_key="", model="bge-m3", dimensions=1024,
            transport=httpx.MockTransport(handler))
        self.assertTrue(client.configured)
        self.assertEqual(len(asyncio.run(client.embed(["connection check"]))[0]), 1024)
        self.assertNotIn("authorization", captured[0].headers)
        self.assertEqual(__import__("json").loads(captured[0].content),
            {"model": "bge-m3", "input": ["connection check"], "dimensions": 1024})

    def test_index_state_only_returns_changed_documents(self) -> None:
        state = SemanticIndexState(postgres_for_test(self))
        self.addCleanup(state.close)
        first = SemanticDocument(
            scope_key="onebot-v11:group:1",
            source_type="message",
            source_handle="msg#1",
            content="first",
            metadata={},
        )
        self.assertEqual(state.changed([first]), [first])
        state.mark([first])
        self.assertEqual(state.changed([first]), [])
        updated = SemanticDocument(
            scope_key=first.scope_key,
            source_type=first.source_type,
            source_handle=first.source_handle,
            content="second",
            metadata={},
        )
        self.assertEqual(state.changed([updated]), [updated])


if __name__ == "__main__":
    unittest.main()
