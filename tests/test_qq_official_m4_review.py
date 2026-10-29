"""Independent M4 regression checks at the real runtime/FakePlatform boundary."""
from datetime import datetime, timezone
import json
import re
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.plugins.ai_chat.model_catalog import ModelCatalog
from src.plugins.ai_chat.platform_types import EndpointKey
from tests import test_qq_official_turn_endtoend as fixture
from tests.test_qq_official_runtime_sender import verified_ready_platform


class M4ReviewTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.OfficialTurnEndToEndTests.setUp
    runtime = fixture.OfficialTurnEndToEndTests.runtime
    query = fixture.OfficialTurnEndToEndTests.query
    wait_db = fixture.OfficialTurnEndToEndTests.wait_db
    application = fixture.OfficialTurnEndToEndTests.application
    open_runtime = fixture.OfficialTurnEndToEndTests.open_runtime
    emit = fixture.OfficialTurnEndToEndTests.emit
    complete = fixture.OfficialTurnEndToEndTests.complete

    async def bound_group(self, fake, runtime, *, grant_owner=False):
        await self.emit(fake, native="TEST_REVIEW_CREATE", content="/bind create")
        challenge = (await self.wait_db("SELECT challenge_id FROM qq_official_binding_challenges WHERE state='issued'"))[0]
        # Binding can use any live private reply window for this endpoint;
        # same-second windows need not select the create command's message.
        delivery = (await self.wait_db(
            "SELECT d.native_message_id FROM deliveries d "
            "JOIN delivery_plans p ON p.plan_id=d.plan_id "
            "WHERE p.challenge_id=? AND d.purpose='binding_otp' AND d.status='committed'",
            (challenge["challenge_id"],),
        ))[0]
        sent = next(item for item in fake.received if item["receipt"]["id"] == delivery["native_message_id"])
        code = re.search(r"一次性验证码：(\d{6})", sent["payload"]["content"]).group(1)
        await self.group_message(fake, "TEST_REVIEW_SUBMIT", f"/bind submit {challenge['challenge_id']} {code}")
        candidate = (await self.wait_db("SELECT candidate_id,version FROM qq_official_binding_candidates WHERE state='pending'"))[0]
        suffix = " --grant-owner" if grant_owner else ""
        await self.emit(fake, native="TEST_REVIEW_CONFIRM", content=f"/bind confirm {candidate['candidate_id']} {candidate['version']}{suffix}")
        await self.wait_db("SELECT candidate_id FROM qq_official_binding_candidates WHERE state='confirmed'")
        endpoint = EndpointKey(runtime.loaded.account, "group", "TEST_GROUP")
        row = self.query("SELECT binding_state FROM qq_official_endpoints WHERE endpoint_key=?", (endpoint.key,))[0]
        self.assertEqual(row["binding_state"], "active")
        return endpoint

    async def group_message(self, fake, native, content, *, reference=None):
        # This fixture explicitly models a platform that permits a passive
        # reply to a received non-mention message; production is not inferred.
        fake.nonmention_reply_allowed = True
        ext = {"msg_idx": "TEST_IDX_" + native}
        if reference is not None:
            ext["ref_msg_idx"] = reference
        await fake.emit("GROUP_MESSAGE_CREATE", {
            "id": native, "content": content, "group_openid": "TEST_GROUP",
            "author": {"member_openid": "TEST_MEMBER"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message_scene": {"ext": ext},
        })

    async def test_bound_official_group_runs_without_unreachable_manual_enable_override(self):
        app, calls = self.application(), []

        async def completion(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="TEST bound group answer", tool_calls=[]))], usage=None)

        with patch("src.plugins.ai_chat.agent_loop._create_completion", new=completion):
            async with verified_ready_platform() as fake:
                runtime = await self.open_runtime(fake, app)
                endpoint = await self.bound_group(fake, runtime)
                for native, content in (("TEST_REVIEW_NO_OWNER_MODEL", "/model group other"),
                                        ("TEST_REVIEW_NO_OWNER_EFFORT", "/effort group high")):
                    await self.emit(fake, native=native, content=content, group=True)
                    await self.complete(native)
                    reply = next(item["payload"]["markdown"]["content"] for item in fake.received if item["payload"].get("msg_id") == native)
                    self.assertIn("未授权", reply)
                self.assertIsNone(app.model_preferences.get_scope_default(endpoint.scope))
                self.assertIsNone(app.reasoning_preferences.get_scope_member_default(endpoint.scope))
                await self.emit(fake, native="TEST_REVIEW_BOUND", content="TEST question", group=True)
                await self.complete("TEST_REVIEW_BOUND")
                await runtime.stop()
                self.assertEqual(len(calls), 1, "An explicitly admitted official group must have a reachable chat-enable path")
                self.assertEqual(next(item["payload"]["markdown"]["content"] for item in fake.received if item["payload"].get("msg_id") == "TEST_REVIEW_BOUND"), "TEST bound group answer")

    async def test_non_at_context_and_exact_bot_quote_continue_through_real_pipeline(self):
        app, calls = self.application(), []

        async def completion(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=f"TEST answer {len(calls)}", tool_calls=[]))], usage=None)

        with patch("src.plugins.ai_chat.agent_loop._create_completion", new=completion):
            async with verified_ready_platform() as fake:
                runtime = await self.open_runtime(fake, app)
                await self.bound_group(fake, runtime)
                await self.group_message(fake, "TEST_AMBIENT", "TEST_AMBIENT_VISIBLE_EVIDENCE")
                await self.wait_db("SELECT inbox_id FROM qq_official_inbound_events WHERE native_message_id='TEST_AMBIENT' AND status='processed'")
                self.assertEqual(calls, [])
                await self.emit(fake, native="TEST_REVIEW_AT", content="TEST summarize recent discussion", group=True)
                await self.complete("TEST_REVIEW_AT")
                self.assertIn("TEST_AMBIENT_VISIBLE_EVIDENCE", json.dumps(calls[0], ensure_ascii=False, default=str))
                reply = next(item["receipt"] for item in fake.received if item["payload"].get("msg_id") == "TEST_REVIEW_AT")
                await self.group_message(fake, "TEST_REVIEW_QUOTE", "TEST explain the previous answer", reference=reply["ext_info"]["ref_idx"])
                try:
                    await self.complete("TEST_REVIEW_QUOTE")
                except TimeoutError:
                    details = {
                        "inbox": self.query("SELECT status,reason_code,echo_state,dispatch_job_id,trigger_requested,canonical_message_id FROM qq_official_inbound_events WHERE native_message_id='TEST_REVIEW_QUOTE'"),
                        "jobs": self.query("SELECT status,last_error,result_json FROM durable_jobs WHERE kind LIKE ?", ("qq_official.turn.%",)),
                        "executions": self.query("SELECT state,reason_code FROM qq_official_turn_executions"),
                        "deliveries": self.query("SELECT status,wait_reason,last_error FROM deliveries"),
                        "attempts": self.query("SELECT state,failure_category FROM delivery_attempts"),
                        "model_calls": len(calls),
                    }
                    self.fail(json.dumps(details, default=str))
                await runtime.stop()
                self.assertEqual(len(calls), 2)
                self.assertIn("TEST answer 1", json.dumps(calls[1], ensure_ascii=False, default=str))
                sent = next(item for item in fake.received if item["payload"].get("msg_id") == "TEST_REVIEW_QUOTE")
                self.assertEqual(sent["payload"]["msg_seq"], 1)
                source = self.query("SELECT reply_to_native_message_id FROM messages WHERE native_message_id='TEST_REVIEW_QUOTE'")[0]
                self.assertEqual(source["reply_to_native_message_id"], reply["id"])

    async def test_explicit_disable_and_revocation_cannot_be_bypassed_by_enable_override(self):
        app = self.application()
        with patch("src.plugins.ai_chat.agent_loop._create_completion", side_effect=AssertionError("disabled group called model")) as completion:
            async with verified_ready_platform() as fake:
                runtime = await self.open_runtime(fake, app)
                endpoint = await self.bound_group(fake, runtime)
                app.model_preferences.set_scope_enabled(endpoint.scope, False)
                await self.emit(fake, native="TEST_REVIEW_DISABLED", content="TEST disabled question", group=True)
                await self.complete("TEST_REVIEW_DISABLED")
                self.assertIn("没有开启 AI", next(item["payload"]["markdown"]["content"] for item in fake.received if item["payload"].get("msg_id") == "TEST_REVIEW_DISABLED"))
                app.model_preferences.set_scope_enabled(endpoint.scope, True)

                def revoke():
                    with runtime.store.transaction() as cursor:
                        runtime.store.epoch_guard(cursor, runtime._token)
                        cursor.execute("UPDATE qq_official_endpoints SET binding_state='revoked' WHERE endpoint_key=?", (endpoint.key,))
                await runtime._calls(revoke)
                await self.emit(fake, native="TEST_REVIEW_REVOKED", content="TEST revoked question", group=True)
                rows = await self.wait_db("SELECT status,dispatch_job_id FROM qq_official_inbound_events WHERE native_message_id='TEST_REVIEW_REVOKED'")
                self.assertEqual(rows[0]["status"], "skipped")
                self.assertIsNone(rows[0]["dispatch_job_id"])
                await runtime.stop()
                completion.assert_not_called()
                self.assertFalse(any(item["payload"].get("msg_id") == "TEST_REVIEW_REVOKED" for item in fake.received))

    async def test_effort_obeys_actual_provider_and_group_default_is_not_owner_personal_model(self):
        app = self.application()
        app.model_catalog = ModelCatalog.from_json(json.dumps({"default": "supported", "profiles": {
            "supported": {"provider": "openai", "model": "TEST_GPT", "api_key_required": False},
            "unsupported": {"provider": "deepseek", "model": "TEST_DEEPSEEK", "api_key_required": False},
        }}), default_profile="supported", environ={})
        with patch("src.plugins.ai_chat.agent_loop._create_completion", side_effect=AssertionError("command called model")):
            async with verified_ready_platform() as fake:
                runtime = await self.open_runtime(fake, app)
                ordinal = 0

                async def command(text, *, group=False):
                    nonlocal ordinal
                    ordinal += 1
                    native = f"TEST_REVIEW_EFFORT_{ordinal}"
                    await self.emit(fake, native=native, content=text, group=group)
                    await self.complete(native)
                    return next(item["payload"]["markdown"]["content"] for item in fake.received if item["payload"].get("msg_id") == native)

                await command("/model unsupported")
                self.assertIn("不在宿主授权范围", await command("/effort high"))
                self.assertEqual(app.reasoning_preferences.items(), [])
                # Reset remains a supported cleanup even on a provider without
                # effort levels, including an old override from another model.
                personal_key = app.model_preferences.items()[0][0]
                app.reasoning_preferences.set(personal_key, "high")
                self.assertIn("已恢复", await command("/effort reset"))
                self.assertIsNone(app.reasoning_preferences.get_explicit(personal_key))
                await command("/model supported")
                self.assertIn("已设置", await command("/effort high"))
                self.assertEqual(app.reasoning_preferences.get_explicit(personal_key), "high")

                endpoint = await self.bound_group(fake, runtime, grant_owner=True)
                await command("/model unsupported", group=True)
                # Owner's personal choice cannot disable the supported group
                # default, nor enable an unsupported group default's setting.
                self.assertIn("已设置", await command("/effort group high", group=True))
                self.assertEqual(app.reasoning_preferences.get_scope_member_default(endpoint.scope), "high")
                await command("/model group unsupported", group=True)
                await command("/model supported", group=True)
                self.assertIn("不在宿主授权范围", await command("/effort group low", group=True))
                self.assertEqual(app.reasoning_preferences.get_scope_member_default(endpoint.scope), "high")
                self.assertIn("已恢复", await command("/effort group reset", group=True))
                self.assertIsNone(app.reasoning_preferences.get_scope_member_default(endpoint.scope))
                await runtime.stop()


if __name__ == "__main__":
    unittest.main()
