from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import nonebot

nonebot.init()

from nonebot.adapters.onebot.v11 import GroupMessageEvent
from src.plugins.ai_chat.command_handlers import CommandHandlers
from src.plugins.ai_chat.long_term_memory import (
    LongTermMemoryError,
    LongTermMemoryStore,
)


class LongTermMemoryStoreTests(unittest.TestCase):
    def test_command_lookup_uses_injected_store_and_visible_scopes(self) -> None:
        event = GroupMessageEvent.model_validate({
            "time": 100, "self_id": 9, "post_type": "message", "message_type": "group",
            "sub_type": "normal", "message_id": 42, "group_id": 1, "user_id": 2,
            "message": [], "raw_message": "", "font": 0,
            "sender": {"user_id": 2, "nickname": "tester"},
        })
        with tempfile.TemporaryDirectory() as directory:
            store = LongTermMemoryStore(Path(directory) / "memory.json")
            commands = CommandHandlers(SimpleNamespace(
                context=SimpleNamespace(long_term_memory=store),
                chat=SimpleNamespace(_conversation_id=lambda _: "group:1:user:2"),
            ))
            for scope, kind, visible in (
                ("group:1", "group", True),
                ("group:1:user:2", "user", True),
                ("group:1:user:3", "user", False),
                ("group:2", "group", False),
            ):
                with self.subTest(scope=scope):
                    entry, _ = store.add(scope, kind, "test memory", creator_user_id=2)
                    found = commands._find_visible_memory(event, entry.id)
                    self.assertEqual(found, entry if visible else None)
            self.assertIsNone(commands._find_visible_memory(event, 9999))

    def test_scopes_are_isolated_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.json"
            store = LongTermMemoryStore(path)
            user_entry, created = store.add(
                "group:1:user:2",
                "user",
                "喜欢绿色",
                creator_user_id=2,
            )
            group_entry, _ = store.add(
                "group:1",
                "group",
                "项目使用 Python 3.12",
                creator_user_id=2,
            )

            self.assertTrue(created)
            duplicate, duplicate_created = store.add(
                "group:1:user:2",
                "user",
                "喜欢绿色",
                creator_user_id=2,
            )
            self.assertFalse(duplicate_created)
            self.assertEqual(duplicate.id, user_entry.id)

            reloaded = LongTermMemoryStore(path)
            self.assertEqual(
                [entry.id for entry in reloaded.list_entries(["group:1:user:2"])],
                [user_entry.id],
            )
            self.assertEqual(
                [entry.id for entry in reloaded.list_entries(["group:1"])],
                [group_entry.id],
            )
            self.assertEqual(reloaded.list_entries(["group:2"]), [])

    def test_remove_requires_visible_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LongTermMemoryStore(Path(directory) / "memory.json")
            entry, _ = store.add(
                "group:1:user:2",
                "user",
                "偏好简短回答",
                creator_user_id=2,
            )
            self.assertFalse(store.remove(entry.id, ["group:1:user:3"]))
            self.assertTrue(store.remove(entry.id, ["group:1:user:2"]))

    def test_scope_limit_discards_oldest_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LongTermMemoryStore(
                Path(directory) / "memory.json",
                max_entries_per_scope=2,
            )
            first, _ = store.add("private:1", "user", "第一条", creator_user_id=1)
            second, _ = store.add("private:1", "user", "第二条", creator_user_id=1)
            third, _ = store.add("private:1", "user", "第三条", creator_user_id=1)
            self.assertEqual(
                [entry.id for entry in store.list_entries(["private:1"])],
                [second.id, third.id],
            )
            self.assertNotIn(first.id, [second.id, third.id])

    def test_rejects_empty_and_oversized_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LongTermMemoryStore(
                Path(directory) / "memory.json",
                max_content_chars=50,
            )
            with self.assertRaises(LongTermMemoryError):
                store.add("private:1", "user", "", creator_user_id=1)
            with self.assertRaises(LongTermMemoryError):
                store.add("private:1", "user", "x" * 51, creator_user_id=1)

    def test_provenance_mutations_persist_and_updates_use_cas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.json"
            store = LongTermMemoryStore(path)
            entry, _ = store.add(
                "group:1",
                "group",
                "项目使用 Python",
                creator_user_id=99,
                creator_principal_id=7,
                source_message_id=42,
            )
            updated = store.update(
                entry.id,
                "项目使用 Python 3.12",
                ["group:1"],
                expected_version=1,
                actor_principal_id=7,
                source_message_id=43,
            )

            self.assertEqual(updated.version, 2)
            with self.assertRaises(LongTermMemoryError):
                store.update(
                    entry.id,
                    "stale write",
                    ["group:1"],
                    expected_version=1,
                )

            reloaded = LongTermMemoryStore(path)
            audit = reloaded.audit(["group:1"])
            self.assertEqual([item.action for item in audit], ["update", "create"])
            self.assertEqual(audit[0].source_message_id, 43)
            self.assertEqual(audit[1].actor_principal_id, 7)

    def test_relevant_render_is_sparse_and_keeps_scopes_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LongTermMemoryStore(Path(directory) / "memory.json")
            store.add(
                "group:1",
                "group",
                "项目使用 Python 3.12",
                creator_user_id=2,
            )
            store.add(
                "group:2",
                "group",
                "另一个群使用 Rust",
                creator_user_id=3,
            )
            store.add(
                "group:1:user:2",
                "user",
                "喜欢绿色主题",
                creator_user_id=2,
            )

            relevant = store.render_relevant(
                "group:1",
                "group:1:user:2",
                "Python 项目怎么升级？",
            )
            unrelated = store.render_relevant(
                "group:1",
                "group:1:user:2",
                "今天天气如何？",
            )
            recalled = store.render_relevant(
                "group:1",
                "group:1:user:2",
                "你还记得我吗？",
                fallback_user=True,
            )

            self.assertIn("Python 3.12", relevant)
            self.assertNotIn("Rust", relevant)
            self.assertEqual(unrelated, "")
            self.assertIn("喜欢绿色主题", recalled)


if __name__ == "__main__":
    unittest.main()
