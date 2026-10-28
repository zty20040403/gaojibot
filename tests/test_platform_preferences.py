from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import unittest

from src.plugins.ai_chat.conversation_keys import (
    ScopeSettingAddress, TurnAddress, conversation_key, scope_from_context_key,
)
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.model_preferences import ModelPreferenceStore
from src.plugins.ai_chat.admin_api.scoped_model_views import scoped_model_rows
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey
from tests.test_platform_types import inbound


class PlatformPreferenceTests(unittest.TestCase):
    def test_scope_settings_isolate_every_account_and_group_dimension(self):
        store = ModelPreferenceStore(None)
        scopes = [
            EndpointKey(AccountKey("qq-official", app, env), "group", group).scope
            for app in ("TEST_APP_A", "TEST_APP_B")
            for env in ("production", "sandbox")
            for group in ("TEST_GROUP:one", "TEST_GROUP:two")
        ]
        for index, scope in enumerate(scopes):
            store.set_scope_default(scope, f"TEST_MODEL_{index}")
            store.set_scope_enabled(scope, index % 2 == 0)
            store.set_scope_member_default(scope, f"TEST_EFFORT_{index}")
        for index, scope in enumerate(scopes):
            self.assertEqual(store.get_scope_default(scope), f"TEST_MODEL_{index}")
            self.assertEqual(store.get_scope_enabled_override(scope), index % 2 == 0)
            self.assertEqual(store.get_scope_member_default(scope), f"TEST_EFFORT_{index}")
        self.assertEqual(len(store.scoped_items()), 24)
        store.clear_scope_default(scopes[0])
        self.assertIsNone(store.get_scope_default(scopes[0]))
        self.assertEqual(store.get_scope_default(scopes[1]), "TEST_MODEL_1")

    def test_new_onebot_accessors_read_and_write_the_existing_keys(self):
        store = ModelPreferenceStore(None)
        scope = ConversationScope("onebot-v11", "group", "900001")
        store.set_group_default(900001, "TEST_MODEL")
        store.set_group_enabled(900001, False)
        self.assertEqual(store.get_scope_default(scope), "TEST_MODEL")
        self.assertFalse(store.get_scope_enabled_override(scope))
        store.set_scope_member_default(scope, "high")
        self.assertEqual(store.get_group_member_default(900001), "high")
        store.set_scope_vision_auto_describe(scope, True)
        self.assertTrue(store.get_group_vision_auto_describe_override(900001))
        self.assertEqual({key for key, _ in store.items()}, {
            "group:900001:default", "group:900001:enabled",
            "group:900001:member-default", "group:900001:vision-auto-describe",
        })

    def test_history_binding_does_not_merge_actor_lanes(self):
        message = inbound()
        other_account = replace(message.endpoint.account, app_id="TEST_OTHER_APP")
        other_endpoint = replace(message.endpoint, account=other_account)
        other_actor = replace(message.actor, account=other_account)
        canonical = ConversationScope("onebot-v11", "group", "900001")
        first = replace(message, canonical_scope=canonical)
        second = replace(message, endpoint=other_endpoint, actor=other_actor, canonical_scope=canonical)
        self.assertNotEqual(conversation_key(first), conversation_key(second))
        self.assertEqual(TurnAddress.parse(conversation_key(first)).actor, first.actor)
        self.assertEqual(scope_from_context_key(conversation_key(first)), canonical)

    def test_existing_onebot_conversation_ids_remain_exact(self):
        account = AccountKey("onebot-v11", "900099", "production")
        actor = ActorKey(account, "user", "", "900002")
        for kind, native_id, expected in (
            ("group", "900001", "group:900001:user:900002"),
            ("private", "900002", "private:900002"),
        ):
            message = inbound(endpoint=EndpointKey(account, kind, native_id), actor=actor)
            self.assertEqual(conversation_key(message), expected)

    def test_evidence_scope_codec_preserves_official_namespaces(self):
        message = inbound()
        for key in (message.scope.key, message.actor.key, conversation_key(message)):
            self.assertEqual(scope_from_context_key(key), message.scope)
        self.assertEqual(scope_from_context_key("group:900001:user:900002").key, "onebot-v11:group:900001")
        self.assertEqual(scope_from_context_key("group:matrix:!TEST_ROOM:TEST_SERVER").key, "matrix:group:!TEST_ROOM:TEST_SERVER")
        self.assertIsNone(scope_from_context_key("group:qq-official:TEST_GROUP"))
        self.assertIsNone(scope_from_context_key("turn:v1:[\"broken\"]"))

    def test_setting_decoder_rejects_unknown_or_noncanonical_keys(self):
        scope = inbound().scope
        value = ScopeSettingAddress(scope, "enabled")
        self.assertEqual(ScopeSettingAddress.parse(value.key), value)
        with self.assertRaises(ValueError):
            ScopeSettingAddress.parse(value.key + " ")
        with self.assertRaises(ValueError):
            ScopeSettingAddress(scope, "unknown")

    def test_admin_overview_lists_scoped_preferences_without_inventing_members(self):
        store, reasoning = ModelPreferenceStore(None), ModelPreferenceStore(None)
        first = inbound()
        other_account = replace(first.endpoint.account, app_id="TEST_OTHER_APP")
        second = replace(
            first, endpoint=replace(first.endpoint, account=other_account),
            actor=replace(first.actor, account=other_account),
            canonical_scope=ConversationScope("onebot-v11", "group", "900001"),
        )
        store.set_scope_default(first.endpoint.scope, "TEST_FIRST")
        store.set_scope_default(second.endpoint.scope, "TEST_SECOND")
        store.set(conversation_key(second), "TEST_ACTOR_OVERRIDE")
        reasoning.set_scope_member_default(second.endpoint.scope, "high")
        catalog = SimpleNamespace(resolve_preference=lambda value: SimpleNamespace(
            name=value or "TEST_DEFAULT", provider="TEST_PROVIDER", model="TEST_MODEL",
        ))
        rows = scoped_model_rows(catalog, store, reasoning, None)
        self.assertEqual(len(rows), 2)
        by_scope = {row["scope_key"]: row for row in rows}
        other_row = by_scope[second.endpoint.scope.key]
        self.assertEqual(other_row["default_profile"], "TEST_SECOND")
        self.assertEqual(other_row["member_reasoning_effort"], "high")
        self.assertEqual(other_row["overrides"], [{
            "actor_key": second.actor.key, "profile": "TEST_ACTOR_OVERRIDE",
            "turn_key": conversation_key(second), "history_scope_key": second.scope.key,
        }])
        self.assertNotIn("members", other_row)
        self.assertIsNone(other_row["enabled_override"])
        original = replace(second, canonical_scope=None)
        store.set(conversation_key(original), "TEST_ORIGINAL_HISTORY")
        other_row = next(row for row in scoped_model_rows(catalog, store, reasoning, None)
                         if row["scope_key"] == second.endpoint.scope.key)
        self.assertEqual({row["turn_key"]: row["profile"] for row in other_row["overrides"]}, {
            conversation_key(second): "TEST_ACTOR_OVERRIDE",
            conversation_key(original): "TEST_ORIGINAL_HISTORY",
        })
