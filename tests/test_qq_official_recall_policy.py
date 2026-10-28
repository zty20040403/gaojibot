from dataclasses import replace
import unittest

from src.plugins.ai_chat.ledger import CanonicalMessage
from src.plugins.ai_chat.message_ir import MessageBody
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, CapabilityEvidence, EffectiveCapabilities, EndpointKey
from src.plugins.ai_chat.adapters.qq_official.recall_policy import RecallLimits, RecallMessageTime, recall_eligibility


class RecallPolicyTests(unittest.TestCase):
    def setUp(self):
        self.account = AccountKey("qq-official", "TEST_RECALL", "test")
        self.endpoint = EndpointKey(self.account, "group", "TEST_GROUP")
        self.actor = ActorKey(self.account, "group_member", "TEST_GROUP", "TEST_MEMBER")
        self.bot = ActorKey(self.account, "bot", "", "TEST_BOT")
        self.message = CanonicalMessage(1, self.endpoint.scope.key, "TEST_MESSAGE", "TEST_BOT", None,
            "", "outbound", "chat", MessageBody(), "", 100, None, None, self.bot.key, self.endpoint.key)
        self.evidence = CapabilityEvidence(self.endpoint, "available", "TEST_VERIFIED", 90, 300)
        self.kwargs = dict(endpoint=self.endpoint, requester=self.actor, message=self.message,
            operation="recall_own", capabilities=EffectiveCapabilities(self.endpoint, 7, {"recall_own": self.evidence}),
            capability_revision=7, verified_bot=self.bot,
            limits=RecallLimits(self.endpoint, "recall_own", 120, 90, 300, "TEST_VERIFIED"),
            message_time=RecallMessageTime(self.endpoint, "TEST_MESSAGE", 100, "dispatch_started"),
            authorize=lambda *_: True, now=110)

    def check(self, **changes):
        return recall_eligibility(**{**self.kwargs, **changes})

    def test_verified_own_message_has_original_time_deadline(self):
        result = self.check()
        self.assertTrue(result.allowed)
        self.assertEqual(result.deadline, 215)
        self.assertFalse(self.check(now=215).allowed)
        self.assertFalse(self.check(now=99).allowed)

    def test_unknown_expired_denied_or_other_operation_never_grants_recall(self):
        for state in ("unknown", "denied", "temporarily_unavailable"):
            with self.subTest(state=state):
                caps = EffectiveCapabilities(self.endpoint, 7, {"recall_own": replace(self.evidence, state=state)})
                self.assertFalse(self.check(capabilities=caps).allowed)
        for changes in (dict(capabilities=None), dict(capability_revision=8), dict(limits=None),
                        dict(now=300), dict(limits=replace(self.kwargs["limits"], operation="recall_member")),
                        dict(capabilities=EffectiveCapabilities(self.endpoint, 7, {"recall_member": self.evidence}))):
            with self.subTest(changes=changes):
                self.assertFalse(self.check(**changes).allowed)

    def test_owner_or_platform_role_cannot_replace_exact_local_authorization(self):
        for authorize in (None, lambda *_: False, lambda *_: 1):
            self.assertFalse(self.check(authorize=authorize).allowed)

    def test_shared_history_native_id_or_app_id_does_not_prove_ownership(self):
        for msg in (replace(self.message, source_endpoint_key=""),
                    replace(self.message, source_endpoint_key=EndpointKey(self.account, "group", "TEST_OTHER").key),
                    replace(self.message, sender_actor_key=self.actor.key, sender_native_user_id=self.actor.native_user_id),
                    replace(self.message, direction="inbound"),
                    replace(self.message, sender_native_user_id=self.account.app_id)):
            with self.subTest(message=msg.canonical_message_id):
                self.assertFalse(self.check(message=msg).allowed)
        self.assertFalse(self.check(verified_bot=None).allowed)

    def test_group_moderation_requires_separate_evidence_and_human_author(self):
        inbound = replace(self.message, direction="inbound", sender_actor_key=self.actor.key,
                          sender_native_user_id=self.actor.native_user_id)
        caps = EffectiveCapabilities(self.endpoint, 7, {"recall_member": self.evidence})
        limits = replace(self.kwargs["limits"], operation="recall_member")
        self.assertFalse(self.check(operation="recall_member", message=inbound).allowed)
        result = self.check(operation="recall_member", message=inbound, capabilities=caps, limits=limits,
                            message_time=replace(self.kwargs["message_time"], source="platform"))
        self.assertTrue(result.allowed)
        self.assertFalse(self.check(operation="recall_member", capabilities=caps, limits=limits).allowed)

    def test_account_environment_and_endpoint_are_independent(self):
        other_account = AccountKey("qq-official", "TEST_RECALL", "sandbox")
        other_actor = ActorKey(other_account, "group_member", "TEST_GROUP", "TEST_MEMBER")
        self.assertFalse(self.check(requester=other_actor).allowed)
        self.assertFalse(self.check(verified_bot=ActorKey(other_account, "bot", "", "TEST_BOT")).allowed)
        other_endpoint = EndpointKey(self.account, "private", "TEST_MEMBER")
        other_requester = ActorKey(self.account, "c2c_user", "", "TEST_MEMBER")
        self.assertFalse(self.check(endpoint=other_endpoint, requester=other_requester).allowed)

    def test_missing_expired_or_guessed_limits_stay_unavailable(self):
        self.assertFalse(self.check(limits=replace(self.kwargs["limits"], expires_at=110)).allowed)
        for max_age in (0, True, -1, 86401):
            with self.subTest(max_age=max_age), self.assertRaises(ValueError):
                replace(self.kwargs["limits"], max_age_seconds=max_age)

    def test_delayed_receipt_cannot_extend_original_send_deadline(self):
        late = replace(self.message, occurred_at=130)
        self.assertEqual(self.check(message=late, now=140).deadline, 215)
        self.assertFalse(self.check(message=late, now=215).allowed)
        for evidence in (None, replace(self.kwargs["message_time"], source="receipt_observed"),
                         replace(self.kwargs["message_time"], native_message_id="TEST_OTHER"),
                         replace(self.kwargs["message_time"], occurred_at=131)):
            with self.subTest(evidence=evidence):
                self.assertFalse(self.check(message_time=evidence).allowed)
