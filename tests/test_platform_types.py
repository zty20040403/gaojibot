"""Opaque fake identifiers exercise identity boundaries, not numeric QQ aliases."""
from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import json
import unittest

from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import (
    AccountKey,
    ActorKey,
    CapabilityEvidence,
    EffectiveCapabilities,
    EndpointKey,
    InboundMessage,
    PlatformFeatures,
    ReplyCredential,
    SendOutcome,
    SendRequest,
)


ACCOUNT = AccountKey("qq-official", "TEST_APP_A", "test")
GROUP = EndpointKey(ACCOUNT, "group", "TEST_GROUP:甲/\"one\"")
ACTOR = ActorKey(ACCOUNT, "group_member", GROUP.native_conversation_id, "TEST_USER:乙")
BODY = MessageBody((TextNode(0, "测试正文"),))


def inbound(**changes):
    values = dict(
        endpoint=GROUP, actor=ACTOR, event_id="TEST_EVENT", native_message_id="TEST_MESSAGE",
        sender_display="测试用户", platform_role="member", body=BODY, mentions_bot=True,
        reply_to_native_message_id=None, occurred_at=100, reply_credential=None,
    )
    values.update(changes)
    return InboundMessage(**values)


class PlatformKeyTests(unittest.TestCase):
    def test_versioned_account_encoding_is_fixed(self):
        self.assertEqual(ACCOUNT.key, 'account:v1:["qq-official","TEST_APP_A","test"]')

    def test_opaque_identifiers_round_trip_without_normalizing(self):
        identifiers = [
            "TEST_NON_NUMERIC", "TEST:group:user:1", 'TEST/\\\"[],:',
            " TEST_WITH_SPACES ", "TEST_中文/😀", "TEST_é", "TEST_e\u0301",
            "TEST_001", "TEST_1", "TEST_\nnewline",
        ]
        keys = set()
        for native_id in identifiers:
            with self.subTest(native_id=native_id):
                account = AccountKey("qq-official", native_id, "test")
                endpoint = EndpointKey(account, "group", native_id)
                actor = ActorKey(account, "group_member", native_id, native_id)
                for value in (account, endpoint, actor):
                    self.assertEqual(type(value).parse(value.key), value)
                self.assertEqual(ConversationScope.from_key(endpoint.scope.key), endpoint.scope)
                keys.add(actor.key)
        self.assertEqual(len(keys), len(identifiers))

    def test_every_identity_dimension_is_preserved(self):
        accounts = [
            ACCOUNT, replace(ACCOUNT, app_id="TEST_APP_B"),
            replace(ACCOUNT, environment="sandbox"), replace(ACCOUNT, environment="production"),
        ]
        actors = []
        endpoints = []
        for account in accounts:
            for group in ("TEST_SAME_OPENID", "TEST_OTHER_GROUP"):
                endpoints.append(EndpointKey(account, "group", group))
                actors.append(ActorKey(account, "group_member", group, "TEST_SAME_OPENID"))
            endpoints.append(EndpointKey(account, "private", "TEST_SAME_OPENID"))
            actors.append(ActorKey(account, "c2c_user", "", "TEST_SAME_OPENID"))
        self.assertEqual(len(set(actors)), len(actors))
        self.assertEqual(len({actor.key for actor in actors}), len(actors))
        self.assertEqual(len({endpoint.key for endpoint in endpoints}), len(endpoints))
        self.assertEqual(len({endpoint.scope.key for endpoint in endpoints}), len(endpoints))

    def test_parsers_reject_malformed_and_noncanonical_forms(self):
        objects = (ACCOUNT, GROUP, ACTOR)
        for value in objects:
            prefix, payload = value.key.split(":v1:", 1)
            parts = json.loads(payload)
            invalid = [
                "", value.key + " ", prefix + ":v2:" + payload,
                prefix + ":v1:" + json.dumps(parts),
                prefix + ':v1:{"value":1}', prefix + ":v1:null",
                prefix + ":v1:" + json.dumps(parts[:-1]),
                prefix + ":v1:" + json.dumps([None, *parts[1:]], separators=(",", ":")),
            ]
            for key in invalid:
                with self.subTest(type=type(value).__name__, key=key):
                    with self.assertRaises(ValueError):
                        type(value).parse(key)

    def test_nested_account_key_must_also_be_canonical(self):
        parts = [ACCOUNT.key.replace(",", ", "), "group", "TEST_GROUP"]
        key = "endpoint:v1:" + json.dumps(parts, separators=(",", ":"))
        with self.assertRaises(ValueError):
            EndpointKey.parse(key)

    def test_numeric_and_blank_ids_are_not_coerced(self):
        for native_id in (12, None, "", " "):
            with self.subTest(native_id=native_id):
                with self.assertRaises(ValueError):
                    AccountKey("qq-official", native_id, "test")
                with self.assertRaises(ValueError):
                    EndpointKey(ACCOUNT, "group", native_id)
                with self.assertRaises(ValueError):
                    ActorKey(ACCOUNT, "group_member", "TEST_GROUP", native_id)

    def test_domain_validator_does_not_infer_cross_group_or_c2c_identity(self):
        cases = [
            (ACTOR, GROUP, True),
            (ACTOR, replace(GROUP, native_conversation_id="TEST_OTHER_GROUP"), False),
            (ACTOR, replace(GROUP, account=replace(ACCOUNT, app_id="TEST_APP_B")), False),
            (ACTOR, replace(GROUP, account=replace(ACCOUNT, environment="sandbox")), False),
            (ACTOR, EndpointKey(ACCOUNT, "private", ACTOR.native_user_id), False),
            (ActorKey(ACCOUNT, "c2c_user", "", ACTOR.native_user_id),
             EndpointKey(ACCOUNT, "private", ACTOR.native_user_id), True),
            (ActorKey(ACCOUNT, "c2c_user", "", ACTOR.native_user_id), GROUP, False),
            (ActorKey(ACCOUNT, "c2c_user", "", "TEST_OTHER_USER"),
             EndpointKey(ACCOUNT, "private", ACTOR.native_user_id), False),
        ]
        for actor, endpoint, expected in cases:
            with self.subTest(actor=actor.key, endpoint=endpoint.key):
                self.assertEqual(actor.matches_endpoint(endpoint), expected)

    def test_official_identity_kinds_require_explicit_namespace(self):
        for kind, namespace in (("user", ""), ("group_member", ""), ("c2c_user", "TEST_GROUP")):
            with self.subTest(kind=kind, namespace=namespace), self.assertRaises(ValueError):
                ActorKey(ACCOUNT, kind, namespace, "TEST_USER")

    def test_onebot_account_and_user_domain_are_explicit(self):
        account = AccountKey("onebot-v11", "999", "production")
        actor = ActorKey(account, "user", "", "123")
        self.assertTrue(actor.matches_endpoint(EndpointKey(account, "group", "789")))
        self.assertTrue(actor.matches_endpoint(EndpointKey(account, "private", "123")))
        self.assertFalse(actor.matches_endpoint(EndpointKey(account, "private", "124")))
        self.assertFalse(actor.matches_endpoint(EndpointKey(replace(account, app_id="998"), "group", "789")))
        with self.assertRaises(ValueError):
            ActorKey(account, "user", "789", "123")


class ConversationScopeCompatibilityTests(unittest.TestCase):
    def test_legacy_keys_stay_byte_for_byte_unchanged(self):
        cases = [
            (ConversationScope("onebot-v11", "group", "789", "123", "999"), "onebot-v11:group:789"),
            (ConversationScope("onebot-v11", "private", "00123"), "onebot-v11:private:00123"),
            (ConversationScope("matrix", "group", "!room:example.test"), "matrix:group:!room:example.test"),
        ]
        for scope, key in cases:
            with self.subTest(key=key):
                self.assertEqual(scope.key, key)
                self.assertEqual(scope.with_actor(456).key, key)
                decoded = ConversationScope.from_key(key)
                self.assertEqual(decoded.key, key)
                self.assertIsNone(decoded.account)
                self.assertEqual(decoded.actor_native_user_id, "")

    def test_account_scopes_round_trip_and_do_not_encode_actor_metadata(self):
        scope = GROUP.scope.with_actor(ACTOR.native_user_id)
        decoded = ConversationScope.from_key(scope.key)
        self.assertEqual(decoded.account, ACCOUNT)
        self.assertEqual(decoded.native_conversation_id, GROUP.native_conversation_id)
        self.assertEqual(decoded.actor_native_user_id, "")
        self.assertEqual(scope.key, scope.with_actor("TEST_OTHER_USER").key)
        self.assertNotEqual(scope.key, replace(scope, account=replace(ACCOUNT, app_id="TEST_APP_B")).key)

    def test_official_scopes_cannot_drop_account_dimension(self):
        with self.assertRaises(ValueError):
            ConversationScope("qq-official", "group", "TEST_GROUP")
        with self.assertRaises(ValueError):
            ConversationScope.from_key("qq-official:group:TEST_GROUP")
        with self.assertRaises(ValueError):
            ConversationScope("onebot-v11", "group", "789", account=ACCOUNT)


class InboundAndReplyTests(unittest.TestCase):
    def credential(self, **changes):
        values = dict(endpoint=GROUP, kind="msg_id", value="TEST_SENSITIVE_REPLY_VALUE",
                      issued_at=90, received_at=100, expires_at=120, max_replies=5)
        values.update(changes)
        return ReplyCredential(**values)

    def test_credential_value_is_hidden_from_recursive_repr(self):
        credential = self.credential()
        message = inbound(reply_credential=credential)
        request = SendRequest(GROUP, ACTOR, BODY, "TEST_DELIVERY", 1, credential)
        for value in (credential, message, request):
            self.assertNotIn(credential.value, repr(value))

    def test_replayed_credential_cannot_extend_or_reopen_window(self):
        credential = self.credential(received_at=150)
        self.assertEqual(credential.expires_at, 120)
        self.assertFalse(credential.valid_at(150))
        fresh = self.credential()
        self.assertFalse(fresh.valid_at(99))
        self.assertTrue(fresh.valid_at(100))
        self.assertTrue(fresh.valid_at(119))
        self.assertFalse(fresh.valid_at(120))
        with self.assertRaises(FrozenInstanceError):
            credential.expires_at = 180

    def test_canonical_history_binding_leaves_actual_identity_and_target_unchanged(self):
        history = ConversationScope("onebot-v11", "group", "789")
        message = inbound(canonical_scope=history, route_epoch=4, reply_credential=self.credential())
        self.assertEqual(message.scope.key, "onebot-v11:group:789")
        self.assertEqual(message.endpoint, GROUP)
        self.assertEqual(message.actor, ACTOR)
        self.assertEqual(message.reply_credential.endpoint, GROUP)
        self.assertNotEqual(message.scope.key, inbound().scope.key)

    def test_canonical_history_does_not_excuse_mismatched_actor_or_credential(self):
        history = ConversationScope("onebot-v11", "group", "789")
        wrong_endpoint = replace(GROUP, account=replace(ACCOUNT, app_id="TEST_APP_B"))
        for changes in (
            {"actor": replace(ACTOR, account=wrong_endpoint.account)},
            {"actor": replace(ACTOR, identity_namespace="TEST_OTHER_GROUP")},
            {"reply_credential": self.credential(endpoint=wrong_endpoint)},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                inbound(canonical_scope=history, **changes)

    def test_invalid_route_or_platform_role_is_rejected(self):
        for changes in ({"route_epoch": -1}, {"route_epoch": True}, {"platform_role": "local-owner"},
                        {"canonical_scope": "onebot-v11:group:789"}, {"mentions_bot": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                inbound(**changes)

    def test_send_intent_validates_its_own_target_without_history_scope(self):
        with self.assertRaises(ValueError):
            SendRequest(replace(GROUP, native_conversation_id="TEST_OTHER_GROUP"), ACTOR, BODY, "TEST_D", 0)
        with self.assertRaises(ValueError):
            SendRequest(GROUP, ACTOR, BODY, "TEST_D", 0, self.credential(endpoint=replace(GROUP, kind="private")))
        request = SendRequest(GROUP, ACTOR, BODY, "TEST_D", 7, self.credential(), "TEST_REPLY", "TEST_FRAGMENT")
        self.assertEqual(request.route_epoch, 7)
        self.assertFalse(hasattr(request, "canonical_scope"))


class CapabilityEvidenceTests(unittest.TestCase):
    def test_unverified_denied_expired_or_future_evidence_never_grants(self):
        for state, now, allowed in (
            ("unknown", 105, False), ("denied", 105, False),
            ("temporarily_unavailable", 105, False), ("available", 99, False),
            ("available", 100, True), ("available", 119, True), ("available", 120, False),
        ):
            with self.subTest(state=state, now=now):
                evidence = CapabilityEvidence(GROUP, state, "TEST_API_OBSERVATION", 100, 120)
                capabilities = EffectiveCapabilities(GROUP, 1, {"markdown": evidence})
                self.assertEqual(capabilities.allows("markdown", now), allowed)
                self.assertFalse(capabilities.allows("native_files", now))

    def test_capability_evidence_is_endpoint_bound_and_snapshot_immutable(self):
        evidence = CapabilityEvidence(GROUP, "available", "TEST_API_OBSERVATION", 100, 120)
        source = {"markdown": evidence}
        snapshot = EffectiveCapabilities(GROUP, 1, source)
        source.clear()
        self.assertTrue(snapshot.allows("markdown", 110))
        with self.assertRaises(TypeError):
            snapshot.grants["markdown"] = evidence
        for target in (replace(GROUP, native_conversation_id="TEST_OTHER"),
                       replace(GROUP, account=replace(ACCOUNT, environment="sandbox"))):
            with self.subTest(target=target.key), self.assertRaises(ValueError):
                EffectiveCapabilities(target, 1, {"markdown": evidence})

    def test_implementation_features_are_not_endpoint_grants(self):
        self.assertFalse(PlatformFeatures().native_files)
        self.assertTrue(PlatformFeatures(native_files=True).native_files)
        self.assertFalse(EffectiveCapabilities(GROUP, 0, {}).allows("native_files", 100))

    def test_send_outcomes_keep_pending_and_ambiguous_distinct(self):
        for status in ("pending", "ambiguous", "failed"):
            self.assertEqual(SendOutcome(status).status, status)
        with self.assertRaises(ValueError):
            SendOutcome("committed")
        receipt = {"TEST_PRIVATE_RECEIPT": "TEST_SENSITIVE"}
        outcome = SendOutcome("committed", "TEST_NATIVE_MESSAGE", receipt)
        receipt.clear()
        self.assertEqual(outcome.receipt["TEST_PRIVATE_RECEIPT"], "TEST_SENSITIVE")
        self.assertNotIn("TEST_SENSITIVE", repr(outcome))
        with self.assertRaises(ValueError):
            SendOutcome("success")


if __name__ == "__main__":
    unittest.main()
