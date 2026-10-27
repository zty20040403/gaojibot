from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor

from src.bot_security.store import SecurityError, SecurityStore, canonical, digest
from tests.postgres_fixture import postgres_for_test
from tests.test_account_security import BOT, PASSWORD, QQ


class ApprovalQuorumTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.database = postgres_for_test(self)
        self.store = SecurityStore(self.database, b"x" * 32, clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.owner = self.store.bootstrap("owner", PASSWORD, QQ)
        self.peer = self.create_account("reviewer", "223456789")

    def create_account(self, name, qq, role="admin"):
        return self.store.apply_account_change(self.owner, {"action": "create", "username": name,
            "password_hash": "unused-test-hash", "role": role, "qq_id": qq})

    def propose(self, **kwargs):
        options = {"kind": "server_task", "payload": {"kind": "task", "id": 7, "revision": 1},
                   "summary": "Only the frozen test task"}
        options.update(kwargs)
        row, code = self.store.propose(self.owner, bot_id=BOT, **options)
        if code is not None:
            self.store.delivered(row["approval_id"], row["code_generation"], True)
        return row, code

    def review(self, parent, qq=None):
        row, code = self.store.request_review(parent["approval_id"], qq or self.peer["qq_id"], BOT)
        if code is not None:
            self.store.delivered(row["approval_id"], row["code_generation"], True)
        return row, code

    def confirm(self, row, code):
        return self.store.confirm(row["approval_id"], row["qq_id"], BOT, code)

    def authorize(self):
        parent, code = self.propose()
        self.confirm(parent, code)
        self.confirm(*self.review(parent))
        return parent

    def test_one_admin_never_executes_repeated_votes_cannot_add_to_quorum(self):
        parent, code = self.propose(quorum=1)
        self.assertEqual(parent["quorum"], 2)
        result = self.confirm(parent, code)
        self.assertEqual((result["status"], result["approved_count"]), ("awaiting_quorum", 1))
        self.assertIsNone(self.store.claim("worker"))
        with self.assertRaises(SecurityError):
            self.confirm(parent, code)
        with self.assertRaises(SecurityError):
            self.review(parent, QQ)
        self.assertIsNone(self.store.claim("worker"))

    def test_peer_can_approve_first_and_only_root_is_executed(self):
        parent, code = self.propose()
        result = self.confirm(*self.review(parent))
        self.assertEqual((result["status"], result["approved_count"]), ("pending", 1))
        self.assertIsNone(self.store.claim("worker"))
        self.assertEqual(self.confirm(parent, code)["status"], "queued")
        claimed = self.store.claim("worker")
        self.assertEqual(claimed["approval_id"], parent["approval_id"])
        self.assertEqual(claimed["kind"], "server_task")
        self.assertEqual(claimed["account"]["account_id"], self.owner["account_id"])
        self.assertIsNone(self.store.claim("worker-2"))
        self.store.validate_execution_approval(parent["approval_id"], "worker")
        self.store.finish(parent["approval_id"], "worker", {"ok": True}, status="succeeded")
        self.assertEqual([r["approval_id"] for r in self.store.unsent_receipts()], [parent["approval_id"]])

    def test_unknown_member_same_person_and_wrong_bot_cannot_review(self):
        self.create_account("member", "323456789", "member")
        parent, _ = self.propose()
        for qq in (QQ, "323456789", "423456789"):
            with self.assertRaises(SecurityError):
                self.review(parent, qq)
        with self.assertRaises(SecurityError):
            self.store.request_review(parent["approval_id"], self.peer["qq_id"], "other-bot")
        with self.assertRaises(SecurityError):
            self.store.propose(self.peer, bot_id=BOT, kind="quorum_vote", payload={}, summary="forged")
        with self.assertRaises(SecurityError):
            self.create_account("duplicate", QQ)

    def test_no_second_administrator_is_explicitly_blocked_not_silently_downgraded(self):
        self.store.apply_account_change(self.owner, {"action": "update", "account_id": self.peer["account_id"],
            "expected_version": 1, "changes": {"enabled": False}})
        with self.assertRaisesRegex(SecurityError, "第二位"):
            self.propose()
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM admin_approvals").fetchone()[0], 0)

    def test_review_is_idempotent_and_survives_store_restart(self):
        parent, code = self.propose()
        self.confirm(parent, code)
        peer, peer_code = self.review(parent)
        again, duplicate_code = self.review(parent)
        self.assertEqual(again["approval_id"], peer["approval_id"])
        self.assertIsNone(duplicate_code)
        restarted = SecurityStore(self.database, b"x" * 32, clock=lambda: self.now)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.confirm(None, self.peer["qq_id"], BOT, peer_code)["status"], "queued")
        self.assertIsNotNone(restarted.claim("worker"))

    def test_concurrent_confirms_and_claims_execute_once(self):
        parent, code = self.propose()
        peer, peer_code = self.review(parent)
        second = SecurityStore(self.database, b"x" * 32, clock=lambda: self.now)
        self.addCleanup(second.close)

        def vote(index):
            row, value = (parent, code) if index % 2 else (peer, peer_code)
            store = self.store if index % 3 else second
            try:
                store.confirm(None, row["qq_id"], BOT, value)
                return True
            except SecurityError:
                return False

        with ThreadPoolExecutor(max_workers=4) as executor:
            self.assertEqual(sum(executor.map(vote, range(8))), 2)
            claims = list(executor.map(lambda i: (self.store if i % 2 else second).claim(str(i)), range(4)))
        self.assertEqual(sum(item is not None for item in claims), 1)

    def test_parent_resend_discards_both_votes_and_old_peer_code(self):
        parent, code = self.propose()
        self.confirm(parent, code)
        peer, peer_code = self.review(parent)
        fresh, fresh_code = self.store.resend(parent["approval_id"], QQ, BOT)
        self.store.delivered(fresh["approval_id"], fresh["code_generation"], True)
        for row, value in ((parent, code), (peer, peer_code)):
            with self.assertRaises(SecurityError):
                self.confirm(row, value)
        self.assertEqual(self.confirm(fresh, fresh_code)["status"], "awaiting_quorum")
        second_peer, second_code = self.review(fresh)
        self.assertNotEqual(second_peer["approval_id"], peer["approval_id"])
        self.assertEqual(self.confirm(second_peer, second_code)["status"], "queued")

    def test_peer_resend_replaces_only_peer_challenge_and_never_extends_root_expiry(self):
        parent, code = self.propose()
        self.confirm(parent, code)
        peer, peer_code = self.review(parent)
        self.now += 120
        fresh, fresh_code = self.store.resend(peer["approval_id"], self.peer["qq_id"], BOT)
        self.assertEqual(fresh["expires_at"], parent["expires_at"])
        self.store.delivered(fresh["approval_id"], fresh["code_generation"], True)
        with self.assertRaises(SecurityError):
            self.confirm(peer, peer_code)
        self.assertEqual(self.confirm(fresh, fresh_code)["status"], "queued")

    def test_expired_waiting_quorum_cannot_be_claimed_or_reviewed(self):
        parent, code = self.propose()
        self.confirm(parent, code)
        peer, peer_code = self.review(parent)
        self.now += 181
        self.assertEqual(self.store.get(parent["approval_id"], self.owner["account_id"])["status"], "expired")
        with self.assertRaises(SecurityError):
            self.confirm(peer, peer_code)
        with self.assertRaises(SecurityError):
            self.review(parent)
        self.assertIsNone(self.store.claim("worker"))

    def test_payload_or_quorum_mutation_invalidates_original_otp(self):
        parent, code = self.propose()
        with self.store.transaction() as db:
            db.execute("UPDATE admin_approvals SET quorum=1 WHERE approval_id=?", (parent["approval_id"],))
        with self.assertRaises(SecurityError):
            self.confirm(parent, code)
        self.assertIsNone(self.store.claim("worker"))

    def test_payload_change_cannot_reuse_peer_vote_even_if_hash_recomputed(self):
        parent, code = self.propose()
        self.confirm(*self.review(parent))
        with self.store.transaction() as db:
            payload = canonical({"kind": "task", "id": 8, "revision": 2})
            db.execute("UPDATE admin_approvals SET payload_json=?, payload_hash=? WHERE approval_id=?", (payload, digest(payload), parent["approval_id"]))
        with self.assertRaises(SecurityError):
            self.confirm(parent, code)
        self.assertIsNone(self.store.claim("worker"))

    def test_changed_peer_role_cancels_queue_and_invalidates_durable_grant(self):
        parent = self.authorize()
        self.store.claim("worker")
        self.store.finish(parent["approval_id"], "worker", {"ok": True}, status="succeeded")
        self.assertIsNotNone(self.store.task_authorization(self.owner, BOT, {"kind": "task", "id": 7, "revision": 1}))
        self.store.apply_account_change(self.owner, {"action": "update", "account_id": self.peer["account_id"],
            "expected_version": 1, "changes": {"role": "member"}})
        self.assertIsNone(self.store.task_authorization(self.owner, BOT, {"kind": "task", "id": 7, "revision": 1}))

    def test_peer_revocation_before_claim_prevents_execution(self):
        parent = self.authorize()
        self.store.revoke_review(parent["approval_id"], self.peer["qq_id"], BOT)
        self.assertIsNone(self.store.claim("worker"))
        self.assertEqual(self.store.get(parent["approval_id"], self.owner["account_id"])["status"], "cancelled")

    def test_account_revocation_during_preflight_blocks_external_side_effect(self):
        parent = self.authorize()
        self.store.claim("worker")
        with self.assertRaises(SecurityError):
            self.store.revoke_review(parent["approval_id"], self.peer["qq_id"], BOT)
        self.store.apply_account_change(self.owner, {"action": "update", "account_id": self.peer["account_id"],
            "expected_version": 1, "changes": {"enabled": False}})
        with self.assertRaises(SecurityError):
            self.store.validate_execution_approval(parent["approval_id"], "worker")

    def test_creator_logout_and_task_revision_cannot_reuse_authorization(self):
        login = self.store.login("owner", PASSWORD, "test")
        self.owner = self.store.session(login["session"])
        parent = self.authorize()
        self.store.logout(login["session"])
        self.assertIsNone(self.store.claim("worker"))
        self.assertIsNone(self.store.task_authorization(self.owner, BOT, {"kind": "task", "id": 7, "revision": 2}))
        self.assertEqual(self.store.get(parent["approval_id"], self.owner["account_id"])["status"], "cancelled")

    def test_generic_tools_can_require_quorum_without_changing_read_only_actions(self):
        parent, code = self.propose(kind="mcp_tool", quorum=2)
        self.confirm(parent, code)
        self.assertIsNone(self.store.claim("worker"))
        self.confirm(*self.review(parent))
        self.assertEqual(self.store.claim("worker")["kind"], "mcp_tool")

    def test_quorum_policy_is_not_supplied_by_model_payload(self):
        parent, _ = self.propose(kind="fleet", payload={"quorum": 1, "risk": "low"}, quorum=1)
        self.assertEqual(parent["quorum"], 2)
        for value in (0, 3, True, "2"):
            with self.assertRaises(ValueError):
                self.propose(quorum=value)

    def test_review_preserves_full_maximum_length_summary(self):
        parent, _ = self.propose(summary="完整参数" * 3000)
        peer, _ = self.review(parent)
        self.assertEqual(peer["summary"], parent["summary"])

    def test_revoking_finished_command_cannot_relabel_real_execution_as_cancelled(self):
        parent, code = self.propose(kind="fleet")
        self.confirm(parent, code)
        self.confirm(*self.review(parent))
        self.store.claim("worker")
        self.store.finish(parent["approval_id"], "worker", {"ok": True}, status="succeeded")
        with self.assertRaisesRegex(SecurityError, "执行记录"):
            self.store.revoke_review(parent["approval_id"], self.peer["qq_id"], BOT)
        self.assertEqual(self.store.get(parent["approval_id"], self.owner["account_id"])["status"], "succeeded")
