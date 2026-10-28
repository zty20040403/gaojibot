"""Executed in a clean child process with all NoneBot imports forbidden."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import importlib.abc
import json
import logging
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class RejectPlatformSDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "nonebot" or fullname.startswith("nonebot."):
            raise AssertionError("Structured turn attempted to import NoneBot")


sys.meta_path.insert(0, RejectPlatformSDK())

from src.bot_storage import DatabaseCalls
from src.bot_storage.disposable import IsolatedPostgres
from src.plugins.ai_chat.application.platform_conversation import build_platform_chat_orchestrator
from src.plugins.ai_chat.config import settings
from src.plugins.ai_chat.context_store import ContextStore
from src.plugins.ai_chat.conversation_keys import conversation_key
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.long_term_memory import LongTermMemoryStore
from src.plugins.ai_chat.memory import ConversationMemory
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.message_lowering import OutboundCapabilities
from src.plugins.ai_chat.model_catalog import ModelCatalog
from src.plugins.ai_chat.model_preferences import ModelPreferenceStore
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey, InboundMessage, PlatformFeatures, ReplyCredential
from src.plugins.ai_chat.skills import SkillRegistry
from src.plugins.ai_chat.tasks import RunningTaskRegistry
from src.plugins.ai_chat.turn_journal import TurnJournal


async def main():
    fixture = IsolatedPostgres(os.environ.get("TEST_POSTGRES_DSN", ""))
    temporary = TemporaryDirectory()
    ledger = MessageLedger(fixture.database)
    journal = TurnJournal(fixture.database)
    projection = ContextStore(fixture.database)
    memories = LongTermMemoryStore(None)
    account = AccountKey("qq-official", "TEST_NO_SDK_APP", "test")
    endpoint = EndpointKey(account, "group", "TEST_GROUP:非数字")
    actor = ActorKey(account, "group_member", endpoint.native_conversation_id, "TEST_ACTOR:非数字")
    inbound = InboundMessage(
        endpoint, actor, "TEST_EVENT", "TEST_MESSAGE", "测试用户", "owner",
        MessageBody((TextNode(0, "请记住我喜欢绿茶"),)), True, None, 100,
        ReplyCredential(endpoint, "msg_id", "TEST_SENSITIVE_REPLY_CREDENTIAL", 90, 100, 120, 5),
        canonical_scope=ConversationScope("onebot-v11", "group", "900001"),
    )
    platform = SimpleNamespace(account=account, features=PlatformFeatures(), capabilities=OutboundCapabilities())
    preferences = ModelPreferenceStore(None)
    preferences.set_scope_enabled(endpoint.scope, True)
    preferences.set_scope_default(endpoint.scope, "actual_endpoint")
    preferences.set_scope_default(inbound.scope, "wrong_history")
    reasoning = ModelPreferenceStore(None)
    reasoning.set_scope_member_default(endpoint.scope, "low")
    reasoning.set(conversation_key(inbound), "high")
    tasks = RunningTaskRegistry()
    app = SimpleNamespace(
        settings=replace(settings, tool_intent_enabled=False), db_call=DatabaseCalls(),
        logger=logging.getLogger("test_platform_chat"), model_preferences=preferences,
        reasoning_preferences=reasoning, running_tasks=tasks,
        model_catalog=ModelCatalog.from_json(json.dumps({
            "default": "test", "profiles": {
                "test": {"model": "TEST_MODEL", "api_key_required": False},
                "actual_endpoint": {
                    "provider": "openai", "protocol": "openai-chat",
                    "model": "TEST_ACTUAL_ENDPOINT_MODEL", "api_key_required": False,
                },
                "wrong_history": {"model": "TEST_WRONG_HISTORY_MODEL", "api_key_required": False},
            },
        }), default_profile="test", environ={}),
        message_ledger=ledger, turn_journal=journal, context_store=projection,
        pin_store=None, source_store=None, topic_graph_store=None, semantic_index=None,
        long_term_memory=memories, memory=ConversationMemory(8),
        skill_registry=SkillRegistry(Path(temporary.name) / "skills"),
    )
    orchestrator = build_platform_chat_orchestrator(app)
    calls = []

    async def completion(**kwargs):
        calls.append(kwargs)
        from src.plugins.ai_chat.agent_loop import _active_profile
        from src.plugins.ai_chat.llm_gateway import _request_for_profile
        selected = _active_profile.get()
        assert selected.name == "actual_endpoint" and selected.reasoning_effort == "high"
        assert kwargs["model"] == "TEST_ACTUAL_ENDPOINT_MODEL"
        assert _request_for_profile(selected, kwargs)["reasoning_effort"] == "high"
        assert "TEST_SENSITIVE_REPLY_CREDENTIAL" not in json.dumps(kwargs, ensure_ascii=False, default=str)
        if len(calls) == 1:
            names = {item["function"]["name"] for item in kwargs["tools"]}
            assert "memory_add" in names and "sandbox_exec" not in names
            call = SimpleNamespace(id="TEST_TOOL_CALL", function=SimpleNamespace(
                name="memory_add", arguments=json.dumps({"scope": "user", "content": "喜欢绿茶"}),
            ))
            message = SimpleNamespace(content="", tool_calls=[call])
        else:
            receipts = [item["content"] for item in kwargs["messages"] if item["role"] == "tool"]
            assert any('"ok": true' in receipt for receipt in receipts)
            message = SimpleNamespace(content="已记住喜欢绿茶。", tool_calls=[])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

    try:
        with patch("src.plugins.ai_chat.agent_loop._create_completion", new=completion):
            result = await orchestrator.run(inbound, platform, "请记住我喜欢绿茶")
        assert result is not None and result.status == "succeeded", result
        saved = journal.get_turn_by_id(result.turn_id)
        assert saved is not None and saved.status == "succeeded" and saved.tool_call_count >= 1
        assert saved.profile == "actual_endpoint" and saved.model == "TEST_ACTUAL_ENDPOINT_MODEL"
        assert saved.scope_key == inbound.scope.key
        trigger = ledger.get_in_scope(inbound.scope, saved.trigger_canonical_message_id)
        assert trigger is not None and trigger.sender_actor_key == actor.key
        assert trigger.source_endpoint_key == endpoint.key
        assert app.memory.get(conversation_key(inbound))[-1]["content"] == result.reply
        personal = memories.list_entries([actor.key])
        assert len(personal) == 1 and personal[0].creator_actor_key == actor.key
        assert personal[0].creator_user_id is None
        assert tasks.list_all() == []
        assert not any(name == "nonebot" or name.startswith("nonebot.") for name in sys.modules)
        print(json.dumps({"status": result.status, "model_calls": len(calls), "tool_calls": saved.tool_call_count}))
    finally:
        projection.close()
        journal.close()
        ledger.close()
        fixture.close()
        temporary.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
