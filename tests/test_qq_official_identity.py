from __future__ import annotations

import copy
from dataclasses import replace
import unittest

from src.plugins.ai_chat.adapters.qq_official.identity import (
    MAX_OBSERVATION_TIME,
    OFFICIAL_SDK_COMMIT,
    IdentityEvidence,
    IdentityState,
    TrustedTransportObservation,
    VerifiedBotIdentity,
    observe_ready,
    observe_self_api,
)
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.account = AccountKey("qq-official", "TEST_APP/特殊", "test")
        self.state = IdentityState(self.account)
        # Fake payloads never establish an identity for a production account.
        self.context = TrustedTransportObservation(self.account, True, 123, True)
        self.user = {"id": "TEST_BOT:原样/+", "bot": True, "username": "PRIVATE_NAME"}
        self.frame = {"op": 0, "t": "READY", "d": {"session_id": "TEST_SESSION", "user": self.user}}

    def test_ready_builds_bot_identity_from_exact_native_id(self):
        result = observe_ready(self.state, self.context, self.frame)
        self.assertTrue(result.accepted)
        self.assertEqual(result.state.status, "verified")
        self.assertEqual(result.state.bot_actor, ActorKey(self.account, "bot", "", self.user["id"]))
        evidence = result.state.identity.evidence[0]
        self.assertEqual((evidence.source, evidence.source_commit, evidence.first_observed_at), ("ready", OFFICIAL_SDK_COMMIT, 123))
        self.assertIn(OFFICIAL_SDK_COMMIT, evidence.source_url)

    def test_self_api_success_produces_same_bot_identity(self):
        result = observe_self_api(self.state, self.context, self.user)
        self.assertEqual(result.state.bot_actor.native_user_id, self.user["id"])
        self.assertEqual(result.state.identity.evidence[0].source, "self_api")

    def test_authenticated_self_api_may_omit_bot_per_endpoint_contract(self):
        payload = {name: value for name, value in self.user.items() if name != "bot"}
        result = observe_self_api(self.state, self.context, payload)
        self.assertTrue(result.accepted)
        self.assertEqual(result.state.bot_actor, ActorKey(self.account, "bot", "", self.user["id"]))
        self.assertEqual(result.state.identity.evidence[0].source, "self_api")
        self.assertNotIn("bot", payload)
        confirmed = observe_ready(result.state, self.context, self.frame)
        self.assertTrue(confirmed.accepted)
        self.assertEqual(len(confirmed.state.identity.evidence), 2)

    def test_missing_bot_self_api_still_requires_authenticated_success(self):
        for field in ("authenticated", "successful"):
            for value in (False, None, 0, 1, "true", {}, []):
                with self.subTest(field=field, value=repr(value)):
                    result = observe_self_api(
                        self.state, replace(self.context, **{field: value}), {"id": self.user["id"]},
                    )
                    self.assertFalse(result.accepted)
                    self.assertIs(result.state, self.state)

    def test_missing_bot_self_api_still_requires_exact_account_environment(self):
        accounts = (
            AccountKey("qq-official", "TEST_OTHER_APP", "test"),
            AccountKey("qq-official", self.account.app_id, "production"),
            AccountKey("qq-official", self.account.app_id, "sandbox"),
            AccountKey("onebot-v11", self.account.app_id, "test"),
        )
        for account in accounts:
            with self.subTest(platform=account.platform, environment=account.environment):
                result = observe_self_api(
                    self.state, replace(self.context, authenticated_account=account), {"id": self.user["id"]},
                )
                self.assertFalse(result.accepted)
                self.assertIs(result.state, self.state)
                self.assertEqual(result.reason, "account_environment_mismatch")

    def test_both_sources_record_bounded_evidence_and_observation_range(self):
        state = observe_ready(self.state, self.context, self.frame).state
        for timestamp in (121, 130, 125):
            state = observe_ready(state, replace(self.context, observed_at=timestamp), self.frame).state
        state = observe_self_api(state, replace(self.context, observed_at=135), self.user).state
        self.assertEqual(len(state.identity.evidence), 2)
        first = state.identity.evidence[0]
        self.assertEqual((first.first_observed_at, first.last_observed_at), (121, 130))
        self.assertEqual(state.status, "verified")

    def test_conflict_retains_first_identity_and_stays_blocked(self):
        original = observe_ready(self.state, self.context, self.frame).state
        conflict = observe_self_api(original, self.context, {"id": "TEST_OTHER_BOT", "bot": True})
        self.assertEqual(conflict.state.status, "mismatch")
        self.assertIs(conflict.state.identity, original.identity)
        self.assertIsNone(conflict.state.bot_actor)
        self.assertIsNotNone(conflict.conflicting_evidence)
        replay = observe_self_api(conflict.state, self.context, self.user)
        self.assertFalse(replay.accepted)
        self.assertEqual(replay.state, conflict.state)

    def test_account_and_environment_are_exact(self):
        accounts = (
            AccountKey("qq-official", "TEST_OTHER_APP", "test"),
            AccountKey("qq-official", self.account.app_id, "production"),
            AccountKey("qq-official", self.account.app_id, "sandbox"),
            AccountKey("onebot-v11", self.account.app_id, "test"),
        )
        for account in accounts:
            with self.subTest(account=account.environment):
                result = observe_ready(self.state, replace(self.context, authenticated_account=account), self.frame)
                self.assertFalse(result.accepted)
                self.assertIsNone(result.state.bot_actor)
                self.assertEqual(result.reason, "account_environment_mismatch")

    def test_strict_authenticated_and_success_flags(self):
        for value in (False, None, 1, "true", {}, []):
            with self.subTest(value=repr(value)):
                self.assertFalse(observe_ready(self.state, replace(self.context, authenticated=value), self.frame).accepted)
                self.assertFalse(observe_self_api(self.state, replace(self.context, successful=value), self.user).accepted)

    def test_bot_true_required_not_truthiness(self):
        for bot in (False, None, 0, 1, "true", "bot"):
            with self.subTest(bot=bot):
                self.assertFalse(observe_self_api(self.state, self.context, {"id": "TEST_ID", "bot": bot}).accepted)

    def test_ready_still_requires_explicit_strict_bot_true(self):
        for bot_field in ({}, *({"bot": value} for value in (False, None, 0, 1, "true"))):
            with self.subTest(bot_field=bot_field):
                frame = copy.deepcopy(self.frame)
                frame["d"]["user"] = {"id": self.user["id"], **bot_field}
                result = observe_ready(self.state, self.context, frame)
                self.assertFalse(result.accepted)
                self.assertIs(result.state, self.state)
                self.assertEqual(result.reason, "invalid_bot_identity")

    def test_required_native_id_and_postgres_safe_bounds(self):
        for value in (None, True, 123, "", " \t", "x\x00y", "x\ny", "x\u200dy", "\ud800", "a" * 257, "界" * 86):
            for bot_field in ({}, {"bot": True}):
                with self.subTest(value=repr(value), bot_present=bool(bot_field)):
                    result = observe_self_api(self.state, self.context, {"id": value, **bot_field})
                    self.assertEqual(result.state.status, "unknown")
        for value in ("a" * 256, "界" * 85, " TEST:原样/+ "):
            with self.subTest(valid_length=len(value)):
                result = observe_self_api(self.state, self.context, {"id": value, "bot": True})
                self.assertEqual(result.state.bot_actor.native_user_id, value)

    def test_no_appid_requester_or_openid_fallback(self):
        for payload in (
            {"app_id": self.account.app_id, "bot": True},
            {"user_openid": "TEST_HUMAN", "bot": True},
            {"member_openid": "TEST_HUMAN", "bot": True},
            {"requester": "TEST_HUMAN", "bot": True},
            {"author": self.user},
        ):
            with self.subTest(keys=tuple(payload)):
                self.assertIsNone(observe_self_api(self.state, self.context, payload).state.bot_actor)

    def test_ready_requires_exact_envelope_and_session(self):
        for frame in (None, [], {}, {"op": False, "t": "READY", "d": self.frame["d"]},
                      {"op": 0, "t": "RESUMED", "d": self.frame["d"]},
                      {"op": 0, "t": "READY", "d": {"user": self.user}}):
            with self.subTest(frame_type=type(frame).__name__):
                self.assertFalse(observe_ready(self.state, self.context, frame).accepted)

    def test_invalid_observation_time_does_not_become_evidence(self):
        for observed_at in (-1, True, 1.2, "123", None, 2**63):
            self.assertFalse(observe_self_api(self.state, replace(self.context, observed_at=observed_at), self.user).accepted)
        for observed_at in (0, MAX_OBSERVATION_TIME):
            self.assertTrue(observe_self_api(self.state, replace(self.context, observed_at=observed_at), self.user).accepted)

    def test_restored_identity_requires_native_id_and_real_evidence_structure(self):
        evidence = observe_self_api(self.state, self.context, self.user).state.identity.evidence
        for native_id, observations in (("", evidence), ("bad\x00id", evidence), ("TEST_ID", ()),
                                        ("TEST_ID", evidence * 2), ("TEST_ID", [evidence[0]]),
                                        ("TEST_ID", ({"source": "self_api"},))):
            with self.assertRaises(ValueError):
                VerifiedBotIdentity(native_id, observations)
        for identity in (None, {}, "TEST_ID"):
            with self.assertRaises(ValueError):
                IdentityState(self.account, "verified", identity)

    def test_restored_evidence_contract_and_timestamp_ranges_checked(self):
        evidence = observe_self_api(self.state, self.context, self.user).state.identity.evidence[0]
        changes = (
            {"source": "app_id"}, {"source_commit": "other"}, {"source_url": "https://example.com"},
            {"first_observed_at": -1}, {"last_observed_at": 2**63}, {"last_observed_at": True},
            {"first_observed_at": 124},
        )
        for change in changes:
            with self.subTest(field=next(iter(change))), self.assertRaises(ValueError):
                replace(evidence, **change)
        self.assertIsInstance(evidence, IdentityEvidence)

    def test_remote_error_cannot_masquerade_as_self_success(self):
        for value in (False, "0", 40034006, None):
            for name in ("code", "err_code"):
                result = observe_self_api(self.state, self.context, {**self.user, name: value})
                self.assertFalse(result.accepted)
        self.assertTrue(observe_self_api(self.state, self.context, {**self.user, "code": 0}).accepted)

    def test_invalid_observation_never_overwrites_existing_identity(self):
        state = observe_self_api(self.state, self.context, self.user).state
        rejected = observe_self_api(state, replace(self.context, authenticated=False), {"id": "TEST_OTHER", "bot": True})
        self.assertIs(rejected.state, state)

    def test_no_private_payload_retention_or_input_mutation(self):
        before = copy.deepcopy(self.frame)
        result = observe_ready(self.state, self.context, self.frame)
        self.assertEqual(self.frame, before)
        for secret in (self.account.app_id, self.user["id"], "PRIVATE_NAME", "TEST_SESSION"):
            self.assertNotIn(secret, repr(result))
        self.assertFalse(hasattr(result.state.identity, "payload"))

    def test_human_identity_remains_separate_even_same_native_string(self):
        bot = observe_self_api(self.state, self.context, self.user).state.bot_actor
        human = ActorKey(self.account, "c2c_user", "", self.user["id"])
        self.assertNotEqual(bot.key, human.key)



if __name__ == "__main__":
    unittest.main()
