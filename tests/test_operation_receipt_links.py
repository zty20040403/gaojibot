from __future__ import annotations

from tests.postgres_fixture import postgres_for_test

import asyncio
import copy
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tests.test_subagent_v2 import profile
from tests.test_task_outcomes import contract, review
from src.plugins.ai_chat.agent import ContextPacket
from src.plugins.ai_chat.agent.control import LeaseLost
from src.plugins.ai_chat.agent.external import ExternalCalls
from src.plugins.ai_chat.agent.outcomes import evaluate_acceptance
from src.plugins.ai_chat.agent.progress import task_progress
from src.plugins.ai_chat.agent.receipt_links import (
    RECEIPT_TOOL, digest, evidence_fingerprint, link_operation_receipts,
)
from src.plugins.ai_chat.agent_loop import AgentTrace
from src.plugins.ai_chat.model_catalog import ModelCatalog
from src.plugins.ai_chat.subagents import AgentExecutionHooks, StepOutcome, SubAgentCoordinator, SubAgentStore, TaskStep


class OperationReceiptLinksTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = SubAgentStore(postgres_for_test(self))
        self.task = self.new_task()
        self.step = TaskStep("inspect", "operator", "inspect", "report")
        self.run = self.store.create_run(self.task.task_id, self.step, allowed_tools=[], model_profile="qwen-local")
        self.lookup = AsyncMock(side_effect=self.proof)

    def new_task(self):
        return self.store.create_task(scope_key="group:1", conversation_id="group:1:user:2", requester_user_id=2,
            trigger_message_id=None, objective="inspect", max_parallelism=3, max_steps=5)

    def tearDown(self):
        self.store.close()

    async def source(self, *, task=None, run=None, call_id="call1", response=None):
        task, run = task or self.task, run or self.run
        tracker = ExternalCalls(self.store, task.task_id, run.run_id)
        tracker.call_id = call_id
        tracker.tool_name = "ops_call"
        tracker.arguments = {"operation": "exec.run", "params": {"host": "h610", "command": "df -B1 /"}}
        await tracker.request("POST", "/v1/ops/call", tracker.arguments, actor="qq:2", origin=task.scope_key,
            perform=AsyncMock(return_value=response or {"ok": False, "error": "Approval wait expired"}))
        return next(item for item in self.store.external_calls(task.task_id) if item["call_id"] == call_id)

    def proof(self, key, *, actor, origin):
        return {"source": "gaoji-control", "historical": True, "intent_key": key, "actor_id": "admin:kenneth",
            "origin_scope": origin, "operation_id": "op_original", "backend_job_id": "job_original",
            "operation": "exec.run", "host_id": "h610", "authorized_before_dispatch": True,
            "params_hash": digest({"host": "h610", "command": "df -B1 /"}), "contract_hash": "original-contract",
            "approval": {"operation_id": "op_original", "contract_hash": "original-contract", "consumed_at": 1001},
            "created_at": 1000, "dispatched_at": 1002, "recorded_status": "failed", "status_recorded_at": 1003}

    def revise(self, *, include_run=True):
        control = self.store.control(self.task.task_id)
        revision = control["revision"] + 1
        self.store.append_checkpoint(self.task.task_id, "revision_requested", {
            "revision": revision, "previous_runs": [{"run_id": self.run.run_id}] if include_run else []})
        self.store.update_control(self.task.task_id, expected_version=control["version"], revision=revision)

    async def link(self, run_ids=None):
        await link_operation_receipts(self.store, self.task,
            {self.run.run_id} if run_ids is None else run_ids, self.lookup)
        return [item for item in self.store.task_evidence(self.task.task_id) if item["tool_name"] == RECEIPT_TOOL]

    async def test_resolved_error_without_receipt_links_across_revision_without_replay(self):
        source = await self.source()
        self.revise()
        result = await self.link()
        self.assertEqual(len(result), 1)
        payload = result[0]["payload"]
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["source_request"]["revision"], 1)
        self.assertEqual(payload["source_request"]["request_hash"], digest(source["request"]))
        self.assertEqual(payload["receipt"]["recorded_status"], "failed")
        self.assertEqual(self.store.external_calls(self.task.task_id, revision=1), [source])
        self.assertEqual(self.store.external_calls(self.task.task_id), [])
        self.assertEqual(self.lookup.call_args.kwargs, {"actor": "qq:2", "origin": self.task.scope_key})
        fingerprint = evidence_fingerprint(result)
        self.assertEqual(evidence_fingerprint(await self.link()), fingerprint)
        self.lookup.assert_awaited_once()

    async def test_other_task_and_unpermitted_step_are_not_queried(self):
        other = self.new_task()
        other_run = self.store.create_run(other.task_id, self.step, allowed_tools=[], model_profile="qwen-local")
        await self.source(task=other, run=other_run)
        await self.source()
        self.assertEqual(await self.link({other_run.run_id}), [])
        self.lookup.assert_not_awaited()
        self.revise(include_run=False)
        self.assertEqual(await self.link(), [])
        self.lookup.assert_not_awaited()

    async def test_revision_ancestry_is_not_lost_after_checkpoint_page_limit(self):
        await self.source()
        for _ in range(205):
            self.store.append_checkpoint(self.task.task_id, "tool_progress", {})
        self.revise()
        self.assertEqual(len(await self.link()), 1)
        self.lookup.assert_awaited_once()

    async def test_actor_scope_key_method_and_payload_are_checked_before_lookup(self):
        original = await self.source()
        invalid = []
        for key, value in (("actor", "qq:3"), ("origin", "group:2"), ("method", "GET"), ("path", "/v1/jobs")):
            item = copy.deepcopy(original)
            item["request"][key] = value
            invalid.append(item)
        item = copy.deepcopy(original)
        item["request"]["body"]["idempotency_key"] = "subagent:" + "a" * 64
        invalid.append(item)
        item = copy.deepcopy(original)
        item["request"]["body"]["params"] = None
        invalid.append(item)
        for item in invalid:
            with self.subTest(item=item), patch.object(self.store, "external_calls", return_value=[item]):
                self.assertEqual(await self.link(), [])
        self.lookup.assert_not_awaited()

    async def test_missing_or_mismatched_proof_never_replays_or_becomes_evidence(self):
        source = await self.source(response={"operation": {"operation_id": "op_original", "backend_operation_id": "job_original", "status": "failed"}})
        self.lookup.side_effect = LookupError("not found")
        self.assertEqual(await self.link(), [])
        self.lookup.side_effect = None
        base = self.proof(source["request"]["body"]["idempotency_key"], actor="qq:2", origin=self.task.scope_key)
        for key, value in (("intent_key", "other"), ("params_hash", "other"), ("host_id", "tank"),
                           ("operation_id", "op_other"), ("backend_job_id", "job_other"),
                           ("operation", "host.reboot"), ("origin_scope", "group:other"),
                           ("approval", {"contract_hash": "bad"})):
            self.lookup.return_value = {**base, key: value}
            with self.subTest(key=key):
                self.assertEqual(await self.link(), [])
        self.assertEqual(self.store.external_calls(self.task.task_id), [source])

    async def test_unapproved_receipt_is_unverified_and_retried_read_only(self):
        source = await self.source()
        base = self.proof(source["request"]["body"]["idempotency_key"], actor="qq:2", origin=self.task.scope_key)
        self.lookup.side_effect = None
        self.lookup.return_value = {**base, "approval": {}, "authorized_before_dispatch": False, "dispatched_at": None}
        items = await self.link()
        self.assertFalse(items[0]["payload"]["ok"])
        self.assertEqual(evaluate_acceptance(contract(), items, review([items[0]["evidence_id"]]),
            task_created_at=self.task.created_at)["status"], "unverified")
        self.lookup.return_value = base
        self.assertTrue(any(item["payload"]["ok"] for item in await self.link()))
        self.assertEqual(self.lookup.await_count, 2)

    async def test_historical_dispatch_cannot_prove_current_health_or_effect(self):
        await self.source()
        items = await self.link()
        refs = [item["evidence_id"] for item in items]
        for kind, extra in (("host_inspection", {}), ("disk_delta", {"mountpoint": "/"}),
                            ("service_effect", {"unit": "test.service", "action": "restart"}), ("host_reboot", {})):
            with self.subTest(kind=kind):
                result = evaluate_acceptance(contract(kind, host_id="h610", **extra), items, review(refs),
                                             task_created_at=self.task.created_at)
                self.assertEqual(result["status"], "unverified")

    async def test_console_separates_historical_authorization_from_current_results(self):
        await self.source()
        items = await self.link()
        value = task_progress(self.task, [], items, [], [], None, revision=1)
        self.assertEqual(value["stages"][2]["status"], "completed")
        self.assertTrue(value["operations"][0]["historical"])
        self.assertEqual(value["operations"][0]["updated_at"], 1002)
        self.assertNotEqual(value["stages"][4]["status"], "passed")
        waiting = [{"run_id": self.run.run_id, "call_id": "other", "updated_at": 1004, "status": "resolved",
                    "response": {"operation_id": "op_other", "status": "failed"}}]
        value = task_progress(self.task, [], items, waiting, [], None, revision=1)
        self.assertEqual(value["stages"][2]["status"], "unverified")

    async def test_revision_or_cancellation_during_lookup_prevents_link(self):
        await self.source()
        async def racing(*args, **kwargs):
            self.revise()
            return self.proof(*args, **kwargs)
        self.lookup.side_effect = racing
        with self.assertRaises(LeaseLost):
            await self.link()
        self.assertFalse(self.store.task_evidence(self.task.task_id))
        with patch.object(self.store, "cancellation_requested", return_value=True):
            with self.assertRaises(asyncio.CancelledError):
                await self.link()

    async def test_prior_unexecuted_request_does_not_block_current_approval(self):
        source = await self.source()
        base = self.proof(source["request"]["body"]["idempotency_key"], actor="qq:2", origin=self.task.scope_key)
        self.lookup.side_effect = None
        self.lookup.return_value = {**base, "approval": {}, "authorized_before_dispatch": False,
            "dispatched_at": None, "recorded_status": "awaiting_approval"}
        self.revise()
        items = await self.link()
        current = await self.source(call_id="current", response={"operation_id": "op_current", "host_id": "h610",
            "status": "succeeded", "approval_ref": "approved_current"})
        value = task_progress(self.task, [], items, [current], [], None, revision=2)
        self.assertEqual(value["stages"][2]["status"], "completed")
        historical = next(row for row in value["operations"] if row.get("historical"))
        self.assertEqual(historical["source_revision"], 1)
        self.assertEqual(historical["status"], "unverified")
        self.assertFalse(items[0]["payload"]["ok"])
        self.assertNotEqual(value["stages"][4]["status"], "passed")

        current["response"].pop("approval_ref")
        self.assertEqual(task_progress(self.task, [], items, [current], [], None,
            revision=2)["stages"][2]["status"], "unverified")
        current["response"]["status"] = "awaiting_approval"
        self.assertEqual(task_progress(self.task, [], items, [current], [], None,
            revision=2)["stages"][2]["status"], "waiting")

    async def test_old_approval_does_not_authorize_a_new_revision(self):
        await self.source()
        self.revise()
        items = await self.link()
        value = task_progress(self.task, [], items, [], [], None, revision=2)
        self.assertEqual(value["stages"][2]["status"], "not_required")
        self.assertEqual(value["operations"][0]["status"], "passed")
        current = await self.source(call_id="current", response={"operation_id": "op_current", "status": "failed"})
        value = task_progress(self.task, [], items, [current], [], None, revision=2)
        self.assertEqual(value["stages"][2]["status"], "unverified")

    async def test_acceptance_cache_changes_only_when_upstream_evidence_changes(self):
        await self.source()
        self.store.set_task_state(self.task.task_id, "running", plan={"contract": contract()})
        catalog = ModelCatalog({"qwen-local": profile("qwen-local")}, default_profile="qwen-local")
        coordinator = SubAgentCoordinator(self.store, catalog, logger=Mock())
        completed = {"inspect": StepOutcome(self.step, self.run, {"status": "success", "summary": "inspected"}, AgentTrace())}
        async def validate(task, step, run, **kwargs):
            items = self.store.task_evidence(task.task_id, run_ids={self.run.run_id})
            refs = [item["evidence_id"] for item in items if item["tool_name"] == RECEIPT_TOOL]
            result = {"status": "success", "summary": "reviewed", **review(refs)}
            self.store.record_evidence(task.task_id, run.run_id, "web_search", {}, {"ok": True, "content": "reviewer own evidence"})
            self.store.finish_run(run.run_id, "succeeded", result=result)
            return StepOutcome(step, run, result, AgentTrace())
        async def attempt():
            return await coordinator._validate_workflow(self.task, completed,
                context=ContextPacket(self.task.scope_key, self.task.conversation_id, 2, None, "inspect"),
                selected_profile=profile("qwen-local"), tools_by_name={}, execute_tool=AsyncMock(),
                hooks=AgentExecutionHooks(operation_receipt=self.lookup), parent_trace=AgentTrace(), progress=None)
        with patch.object(coordinator, "_run_step_reliably", side_effect=validate) as verifier:
            first = await attempt()
            second = await attempt()
            self.assertEqual(first["run_id"], second["run_id"])
            self.assertEqual(verifier.await_count, 1)
            self.store.record_evidence(self.task.task_id, self.run.run_id, "web_search", {}, {"ok": True, "content": "new source"})
            third = await attempt()
            self.assertNotEqual(first["run_id"], third["run_id"])
            self.assertEqual(verifier.await_count, 2)
