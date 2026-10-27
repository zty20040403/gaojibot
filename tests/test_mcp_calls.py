from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import unittest

import nonebot

nonebot.init()

from src.bot_security.store import SecurityStore
from src.plugins.ai_chat.mcp_bridge.calls import McpCallStore
from src.plugins.ai_chat.mcp_bridge.store import McpConflict, McpDenied
from tests.postgres_fixture import postgres_for_test
from tests.test_account_security import BOT, PASSWORD, QQ
from tests.test_mcp_boundaries import CONFIG, GROUP, OTHER, TOOL

OWNER = "a" * 32
SECOND = "b" * 32


class McpCallTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.database = postgres_for_test(self)
        self.security = SecurityStore(self.database, b"x" * 32, clock=lambda: self.now)
        self.store = McpCallStore(self.database, clock=lambda: self.now)
        self.addCleanup(self.security.close)
        self.addCleanup(self.store.close)
        self.admin = self.security.bootstrap("owner", PASSWORD, QQ)
        self.peer = self.security.apply_account_change(self.admin, {"action": "create", "username": "reviewer",
            "password_hash": "test-only", "role": "admin", "qq_id": "223456789"})
        self.store.configure(self.admin, "example", config=CONFIG, scopes=[GROUP], enabled=False, expected_version=0)
        self.store.replace_catalog(self.admin, "example", [TOOL], expected_version=1)
        self.store.configure(self.admin, "example", config=CONFIG, scopes=[GROUP], enabled=True, expected_version=2)
        self.policy("safe")

    def policy(self, trust):
        tool = self.store.tools("example")[0]
        self.store.configure_tool(self.admin, "example", "lookup", enabled=True, trust=trust,
            expected_version=tool["version"], server_version=self.store.server("example")["version"],
            tool_fingerprint=tool["fingerprint"])

    def prepare(self, **overrides):
        values = dict(scope_key=GROUP, bot_id=BOT, user_id=QQ, invocation="message:100:call:0",
                      name="example", remote="lookup", arguments={"query": "hello"})
        values.update(overrides)
        return self.store.prepare_call(**values)

    def read(self, call):
        return self.store.call(call["call_id"], scope_key=GROUP, bot_id=BOT, user_id=QQ)

    def claim(self, call, owner=OWNER, store=None):
        return (store or self.store).claim_call(call["call_id"], owner=owner, scope_key=GROUP, bot_id=BOT, user_id=QQ)

    def approval(self, call, *, wrong_payload=False):
        payload = self.store.approval_payload(call)
        if wrong_payload:
            payload["binding_hash"] = "changed"
        row, code = self.security.propose(self.admin, bot_id=BOT, kind="mcp_tool", payload=payload,
            summary="Exact MCP operation", quorum=1)
        self.assertEqual(row["quorum"], 2)
        self.security.delivered(row["approval_id"], row["code_generation"], True)
        self.security.confirm(row["approval_id"], QQ, BOT, code)
        review, code = self.security.request_review(row["approval_id"], self.peer["qq_id"], BOT)
        self.security.delivered(review["approval_id"], review["code_generation"], True)
        self.security.confirm(review["approval_id"], self.peer["qq_id"], BOT, code)
        claimed = self.security.claim("security-worker")
        self.assertEqual(claimed["approval_id"], row["approval_id"])
        self.security.validate_execution_approval(row["approval_id"], "security-worker")
        return row

    def test_prepare_is_idempotent_but_cannot_change_arguments(self):
        call = self.prepare()
        self.assertEqual(call["status"], "ready")
        self.assertEqual(self.prepare()["call_id"], call["call_id"])
        with self.assertRaises(McpConflict):
            self.prepare(arguments={"query": "another operation"})
        other_actor = self.prepare(user_id="333333333")
        self.assertNotEqual(call["call_id"], other_actor["call_id"])

    def test_group_bot_and_actor_are_hard_read_and_claim_boundaries(self):
        call = self.prepare()
        for scope, bot, user in ((OTHER, BOT, QQ), (GROUP, "333333333", QQ), (GROUP, BOT, "333333333")):
            self.assertIsNone(self.store.call(call["call_id"], scope_key=scope, bot_id=bot, user_id=user))
            with self.assertRaises(McpDenied):
                self.store.claim_call(call["call_id"], owner=OWNER, scope_key=scope, bot_id=bot, user_id=user)

    def test_one_dispatch_and_one_receipt_despite_concurrent_workers(self):
        call = self.prepare()
        second = McpCallStore(self.database, clock=lambda: self.now)
        self.addCleanup(second.close)
        def claim(pair):
            store, owner = pair
            return self.claim(call, owner=owner, store=store)
        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(claim, ((self.store, OWNER), (second, SECOND))))
        self.assertEqual(sum(item is not None for item in claims), 1)
        winner = next(item for item in claims if item is not None)["owner"]
        self.store.dispatch_call(call["call_id"], winner)
        with self.assertRaises(McpDenied):
            second.dispatch_call(call["call_id"], winner)
        self.store.finish_call(call["call_id"], winner, {"ok": True, "password": "redact-me", "value": 42})
        result = self.read(call)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["result"]["value"], 42)
        self.assertNotIn("redact-me", str(result["result"]))
        self.assertIsNone(self.claim(call))
        self.assertEqual(self.prepare()["status"], "succeeded")
        with self.assertRaises(McpDenied):
            self.store.finish_call(call["call_id"], winner, {"ok": False})

    def test_crash_before_dispatch_reclaims_with_fencing(self):
        call = self.prepare()
        self.claim(call)
        self.now += 181
        self.store.recover_calls()
        self.assertEqual(self.read(call)["status"], "ready")
        self.assertIsNotNone(self.claim(call, SECOND))
        with self.assertRaises(McpDenied):
            self.store.dispatch_call(call["call_id"], OWNER)
        self.store.dispatch_call(call["call_id"], SECOND)

    def test_crash_after_dispatch_never_replays_even_after_restarting_store(self):
        call = self.prepare()
        self.claim(call)
        self.store.dispatch_call(call["call_id"], OWNER)
        self.now += 181
        restarted = McpCallStore(self.database, clock=lambda: self.now)
        self.addCleanup(restarted.close)
        restarted.recover_calls()
        self.assertEqual(self.read(call)["status"], "needs_attention")
        self.assertTrue(self.read(call)["result"]["needs_attention"])
        self.assertIsNone(self.claim(call, SECOND, restarted))
        with self.assertRaises(McpDenied):
            restarted.dispatch_call(call["call_id"], OWNER)

    def test_config_changes_invalidate_prepared_and_connecting_calls(self):
        call = self.prepare()
        self.claim(call)
        self.policy("approval")
        with self.assertRaises(McpConflict):
            self.store.dispatch_call(call["call_id"], OWNER)
        self.assertEqual(self.read(call)["status"], "connecting")

    def test_default_risk_waits_for_exact_two_admin_approval(self):
        self.policy("approval")
        call = self.prepare()
        self.assertEqual(call["status"], "waiting")
        self.assertIsNone(self.claim(call))
        approval = self.approval(call)
        self.store.authorize_call(call["call_id"], approval["approval_id"])
        self.assertIsNotNone(self.claim(call))
        self.store.dispatch_call(call["call_id"], OWNER)
        self.store.finish_call(call["call_id"], OWNER, {"ok": True})
        self.assertEqual(self.read(call)["status"], "succeeded")

    def test_unrelated_approval_or_changed_policy_cannot_authorize(self):
        self.policy("approval")
        call = self.prepare()
        approval = self.approval(call, wrong_payload=True)
        with self.assertRaises(McpDenied):
            self.store.authorize_call(call["call_id"], approval["approval_id"])
        self.assertEqual(self.read(call)["status"], "waiting")

    def test_approval_stopping_before_dispatch_blocks_remote_call(self):
        self.policy("approval")
        call = self.prepare()
        approval = self.approval(call)
        self.store.authorize_call(call["call_id"], approval["approval_id"])
        self.claim(call)
        self.security.finish(approval["approval_id"], "security-worker", {"ok": False}, status="failed")
        with self.assertRaises(McpDenied):
            self.store.dispatch_call(call["call_id"], OWNER)

    def test_lost_approval_worker_lease_cannot_dispatch(self):
        from src.bot_security.store import SecurityError
        self.policy("approval")
        call = self.prepare()
        approval = self.approval(call)
        self.store.authorize_call(call["call_id"], approval["approval_id"])
        self.claim(call)
        self.now += 61
        with self.assertRaises(SecurityError):
            self.security.validate_execution_approval(approval["approval_id"], "security-worker")
        with self.assertRaises(McpDenied):
            self.store.dispatch_call(call["call_id"], OWNER)

    def test_expiry_limit_and_unconfirmed_receipt_are_explicit(self):
        call = self.prepare()
        for index in range(31):
            self.prepare(invocation=f"other-{index}")
        with self.assertRaises(McpDenied):
            self.prepare(invocation="over-budget")
        self.claim(call)
        self.store.dispatch_call(call["call_id"], OWNER)
        self.store.finish_call(call["call_id"], OWNER, {"ok": False, "code": "timeout"}, unconfirmed=True)
        self.assertEqual(self.read(call)["status"], "needs_attention")
        self.now += 601
        self.store.recover_calls()
        self.assertEqual(self.prepare(invocation="other-0")["status"], "cancelled")

    def test_cannot_claim_success_before_send_or_finish_other_owner(self):
        call = self.prepare()
        self.claim(call)
        with self.assertRaises(McpDenied):
            self.store.finish_call(call["call_id"], OWNER, {"ok": True})
        with self.assertRaises(McpDenied):
            self.store.finish_call(call["call_id"], SECOND, {"ok": False})
        with self.assertRaises(ValueError):
            self.store.finish_call(call["call_id"], OWNER, {"ok": "true"})

    def test_public_call_summary_has_no_arguments_credentials_or_execution_token(self):
        call = self.prepare(arguments={"token": "private-argument"})
        claimed = self.claim(call)
        summary = self.store.call_summary(claimed)
        self.assertNotIn("private-argument", str(summary))
        self.assertNotIn("binding", summary)
        self.assertNotIn("owner", summary)
