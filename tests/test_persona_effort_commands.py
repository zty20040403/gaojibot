"""Acceptance tests for §8B (/persona per-group command) and §8C (/effort manual override over #169 router)."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from src.plugins.ai_chat.model_catalog import ModelCatalog
from src.plugins.ai_chat.platform_types import EndpointKey
from tests import test_qq_official_turn_endtoend as base
from tests.test_qq_official_runtime_sender import verified_ready_platform


class PersonaAndEffortCommandTests(unittest.IsolatedAsyncioTestCase):
    setUp = base.OfficialTurnEndToEndTests.setUp
    runtime = base.OfficialTurnEndToEndTests.runtime
    query = base.OfficialTurnEndToEndTests.query
    wait_db = base.OfficialTurnEndToEndTests.wait_db
    application = base.OfficialTurnEndToEndTests.application
    open_runtime = base.OfficialTurnEndToEndTests.open_runtime
    emit = base.OfficialTurnEndToEndTests.emit
    complete = base.OfficialTurnEndToEndTests.complete

    async def test_persona_command_restricts_to_admin_and_applies_to_turns_with_default_fallback(self):
        app = self.application()
        seen_systems: list[str] = []

        async def completion(**kwargs):
            seen_systems.append(kwargs["messages"][0]["content"])
            return base.model_response("收到", [], kwargs)

        with patch("src.plugins.ai_chat.agent_loop._create_completion", new=completion):
            async with verified_ready_platform() as fake:
                runtime = await self.open_runtime(fake, app)
                await self.emit(fake, native="TEST_P_INIT", content="init", group=True)
                await self.wait_db("SELECT inbox_id FROM qq_official_inbound_events WHERE native_message_id='TEST_P_INIT'")
                endpoint = EndpointKey(runtime.loaded.account, "group", "TEST_GROUP")
                with runtime.store.transaction() as cursor:
                    cursor.execute("UPDATE qq_official_endpoints SET binding_state='active' WHERE endpoint_key=?", (endpoint.key,))

                async def group_send(nid: str, text: str, *, role: str = "member", user: str = "TEST_U1") -> str:
                    await fake.emit("GROUP_AT_MESSAGE_CREATE", {
                        "id": nid,
                        "content": f"<@!TEST_BOT_MENTION> {text}",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "group_openid": "TEST_GROUP",
                        "author": {"member_openid": user, "username": user, "member_role": role},
                        "mentions": [{"id": "TEST_BOT_MENTION", "member_openid": "TEST_BOT_MENTION", "is_you": True, "bot": True}],
                    })
                    await self.complete(nid)
                    return fake.received[-1]["payload"]["markdown"]["content"]

                # 1. Default persona read by member
                r1 = await group_send("TEST_P1", "/persona", role="member")
                self.assertIn("假小子", r1)

                # 2. Ordinary member cannot set persona
                r2 = await group_send("TEST_P2", "/persona 你是一只猫", role="member")
                self.assertIn("只有本群群主、管理员或宿主 owner", r2)
                self.assertIsNone(app.model_preferences.get_scope_persona(endpoint.scope))

                # 3. Group admin sets custom persona
                r3 = await group_send("TEST_P3", "/persona 你是一位严谨的古典天文学家", role="admin", user="TEST_ADMIN")
                self.assertIn("已更新", r3)
                await group_send("TEST_P4", "今天看什么星", role="member")
                self.assertIn("严谨的古典天文学家", seen_systems[-1])
                # Redline rules remain intact regardless of custom persona
                self.assertIn("红线（不管人设怎么写）", seen_systems[-1])

                # 4. Group admin resets persona -> back to default tomboy persona
                r5 = await group_send("TEST_P5", "/persona default", role="admin", user="TEST_ADMIN")
                self.assertIn("已恢复默认人设", r5)
                await group_send("TEST_P6", "在干嘛", role="member")
                self.assertIn("假小子", seen_systems[-1])
                await runtime.stop()

    async def test_effort_manual_override_beats_auto_router_and_auto_resets(self):
        app = self.application()
        app.settings = replace(app.settings, reasoning_router_profile="test")
        prof = app.model_catalog.resolve("test").with_reasoning_effort("high")
        app.model_catalog = ModelCatalog(profiles={"test": prof}, default_profile="test")

        router_calls = 0
        seen_efforts: list[str | None] = []

        async def fake_router(catalog, profile_name, text):
            nonlocal router_calls
            router_calls += 1
            return "medium"

        async def completion(**kwargs):
            from src.plugins.ai_chat.agent_loop import _active_profile
            prof_cur = _active_profile.get()
            seen_efforts.append(prof_cur.reasoning_effort if prof_cur is not None else None)
            return base.model_response("ok", [], kwargs)

        with patch("src.plugins.ai_chat.reasoning_router.chat_effort", new=fake_router), \
                patch("src.plugins.ai_chat.agent_loop._create_completion", new=completion):
            async with verified_ready_platform() as fake:
                runtime = await self.open_runtime(fake, app)
                await self.emit(fake, native="TEST_EF_INIT", content="init", group=True)
                await self.wait_db("SELECT inbox_id FROM qq_official_inbound_events WHERE native_message_id='TEST_EF_INIT'")
                endpoint = EndpointKey(runtime.loaded.account, "group", "TEST_GROUP")
                with runtime.store.transaction() as cursor:
                    cursor.execute("UPDATE qq_official_endpoints SET binding_state='active' WHERE endpoint_key=?", (endpoint.key,))

                async def group_send(nid: str, text: str, *, role: str = "member", user: str = "TEST_U1") -> str:
                    await fake.emit("GROUP_AT_MESSAGE_CREATE", {
                        "id": nid,
                        "content": f"<@!TEST_BOT_MENTION> {text}",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "group_openid": "TEST_GROUP",
                        "author": {"member_openid": user, "username": user, "member_role": role},
                        "mentions": [{"id": "TEST_BOT_MENTION", "member_openid": "TEST_BOT_MENTION", "is_you": True, "bot": True}],
                    })
                    await self.complete(nid)
                    return fake.received[-1]["payload"]["markdown"]["content"]

                # 1. Default auto mode: /effort shows auto, chat invokes #169 router -> medium
                status_auto = await group_send("TEST_EF1", "/effort")
                self.assertIn("自动", status_auto)
                await group_send("TEST_EF2", "哈哈今天好热")
                self.assertEqual(router_calls, 1)
                self.assertEqual(seen_efforts[-1], "medium")

                # 2. Group admin sets /effort group low -> manual wins, router skipped!
                set_group = await group_send("TEST_EF3", "/effort group low", role="admin", user="TEST_ADMIN")
                self.assertIn("已设置", set_group)
                await group_send("TEST_EF4", "帮我推导一下量子谐振子")
                self.assertEqual(router_calls, 1)  # Router NOT called!
                self.assertEqual(seen_efforts[-1], "low")

                # 3. Group admin resets with /effort group auto -> router active again!
                reset_group = await group_send("TEST_EF5", "/effort group auto", role="admin", user="TEST_ADMIN")
                self.assertIn("已恢复", reset_group)
                await group_send("TEST_EF6", "再来聊聊")
                self.assertEqual(router_calls, 2)
                self.assertEqual(seen_efforts[-1], "medium")
                await runtime.stop()
