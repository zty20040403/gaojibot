import unittest

from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey
from src.plugins.ai_chat.agent.task_identity import (LegacyTaskOwner, StructuredTaskOwner, owner_from_payload,
                      require_same_owner)


def group(app="TEST_APP", environment="test", target="TEST_GROUP", user="TEST_USER"):
    account = AccountKey("qq-official", app, environment)
    endpoint = EndpointKey(account, "group", target)
    return StructuredTaskOwner(endpoint, ActorKey(account, "group_member", target, user))


class IdentityTests(unittest.TestCase):
    def test_legacy_payload_shape_and_numeric_meaning_unchanged(self):
        for value in (0, 123, -1):
            owner = LegacyTaskOwner(value)
            self.assertEqual(owner.as_payload(), {"requester_user_id": value})
            self.assertEqual(owner_from_payload(owner.as_payload()), owner)

    def test_official_round_trip_has_no_numeric_surrogate(self):
        owner = group()
        self.assertIsNone(owner.as_payload()["requester_user_id"])
        self.assertEqual(owner_from_payload(owner.as_payload()), owner)
        self.assertNotIn("TEST_USER", repr(owner))

    def test_same_literal_id_across_namespaces_is_not_the_owner(self):
        actual = group()
        account = actual.endpoint.account
        private = StructuredTaskOwner(EndpointKey(account, "private", "TEST_USER"),
            ActorKey(account, "c2c_user", "", "TEST_USER"))
        for other in (group(app="TEST_OTHER"), group(environment="sandbox"),
                      group(target="TEST_OTHER"), private, LegacyTaskOwner(123)):
            with self.subTest(other=type(other).__name__):
                with self.assertRaisesRegex(PermissionError, "task_owner_mismatch"):
                    require_same_owner(actual, other)

    def test_missing_malformed_and_mixed_structured_identity_fail_closed(self):
        valid = group().as_payload()
        for field in ("endpoint_key", "requester_actor_key", "task_identity_version"):
            payload = dict(valid)
            del payload[field]
            with self.assertRaises(ValueError):
                owner_from_payload(payload)
        for value in (0, 123, True, "123"):
            with self.assertRaises(ValueError):
                owner_from_payload({**valid, "requester_user_id": value})
        for value in (True, 0, 2, "1"):
            with self.assertRaises(ValueError):
                owner_from_payload({**valid, "task_identity_version": value})
        for value in (None, "", "invalid"):
            with self.assertRaises(ValueError):
                owner_from_payload({**valid, "requester_actor_key": value})

    def test_bot_or_cross_endpoint_actor_cannot_authorize_task(self):
        owner = group()
        for actor in (ActorKey(owner.endpoint.account, "bot", "", "TEST_BOT"),
                      group(target="TEST_OTHER").requester_actor,
                      group(app="TEST_OTHER").requester_actor):
            with self.assertRaises(ValueError):
                StructuredTaskOwner(owner.endpoint, actor)

    def test_long_and_control_bearing_identity_rejected_without_truncation(self):
        for value in ("x" * 257, "x\n", "中" * 86, "x\u200b", "x\ud800"):
            with self.assertRaises(ValueError):
                group(user=value)
        owner = group(user="x" * 256)
        self.assertEqual(owner.requester_actor.native_user_id, "x" * 256)


if __name__ == "__main__":
    unittest.main()
