from __future__ import annotations

from tests.postgres_fixture import postgres_for_test

import asyncio
from copy import deepcopy
from dataclasses import replace
import html
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests.test_subagent_v2 import profile
from src.plugins.ai_chat.agent import DEFAULT_AGENT_REGISTRY
from src.plugins.ai_chat.agent import ContextPacket
from src.plugins.ai_chat.agent.control import LeaseLost
from src.plugins.ai_chat.agent.worker_report import (
    checked_report, retain_execution_facts, separate_cluster_artifacts,
)
from src.plugins.ai_chat.agent_loop import AgentTrace
from src.plugins.ai_chat.config import settings
from src.plugins.ai_chat.model_catalog import ModelCatalog
from src.plugins.ai_chat.subagents import SubAgentCoordinator, SubAgentStore, TaskStep


def report(ref="evidence#old"):
    return {"status": "success", "summary": "inspected", "facts": [],
            "findings": [{"description": "host is online", "evidence_refs": [ref]}],
            "completed": [], "authorization": [], "next_verification": [],
            "warnings": [], "unresolved": [], "handoff": [],
            "artifacts": [{"handle": "s123abc:/workspace/report.md", "name": "report.md"}]}


class ReportCorrectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = SubAgentStore(postgres_for_test(self))
        self.profile = profile()
        self.coordinator = SubAgentCoordinator(self.store,
            ModelCatalog({"test": self.profile}, default_profile="test"), logger=Mock())
        self.task = self.store.create_task(scope_key="group:1", conversation_id="group:1:user:2",
            requester_user_id=2, trigger_message_id=3, objective="inspect", max_parallelism=1, max_steps=4)
        self.store.set_task_state(self.task.task_id, "running", plan={"contract": {"version": 2}})
        self.run = self.store.create_run(self.task.task_id,
            TaskStep("inspect", "operator", "inspect", "report"), allowed_tools=[], model_profile="test")
        self.ref = self.store.record_evidence(self.task.task_id, self.run.run_id, "host_inspect",
            {"host_id": "h610"}, {"ok": True, "observed_at": 1000, "host": "h610"})["ref"]

    def tearDown(self):
        self.store.close()

    async def correct(self, original=None):
        return await self.coordinator._correct_worker_report(self.task, self.run, original or report(),
            evidence=self.store.task_evidence(self.task.task_id),
            spec=DEFAULT_AGENT_REGISTRY.worker("operator"), profile=self.profile,
            trace=AgentTrace(), event_sink=AsyncMock())

    async def valid_correction(self, text, history, tools, execute, **kwargs):
        await execute("read_task_evidence", {"ref": self.ref})
        return json.dumps(report(self.ref))

    async def test_correction_reads_current_evidence_only_and_preserves_transcript(self):
        history = [{"role": "user", "content": "previous independent work"}]
        self.store.save_agent_session(self.task.task_id, self.run.run_id, history,
            scope_key=self.task.scope_key, requester_user_id=2, model_profile="test", expected_version=0)

        async def model(text, history, tools, execute, **kw):
            self.assertEqual(history, [])
            self.assertIn("只读报告校验阶段", kw["system_context"])
            self.assertIn("仍描述原执行步骤", kw["system_context"])
            self.assertEqual([t["function"]["name"] for t in tools], ["read_task_evidence_batch", "read_task_evidence"])
            self.assertIn(self.ref, text)
            for name in ("ops_call", "sandbox_exec", "send_file_from_sandbox"):
                self.assertFalse(json.loads(await execute(name, {}))["ok"])
            self.assertFalse(json.loads(await execute("read_task_evidence", {"ref": "evidence#foreign"}))["ok"])
            self.assertTrue(json.loads(await execute("read_task_evidence", {"ref": self.ref}))["ok"])
            kw["transcript_sink"]([{"role": "assistant", "content": "corrected"}])
            return json.dumps(report(self.ref))

        with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools", side_effect=model) as call:
            result = await self.correct()
        self.assertEqual(result["report_validation"]["status"], "passed")
        self.assertEqual(result["status"], "success")
        stored = self.store.agent_session(self.task.task_id, self.run.run_id,
            scope_key=self.task.scope_key, requester_user_id=2)
        self.assertEqual(stored["messages"], history)
        self.assertEqual(stored["version"], 1)
        transcript = self.store.latest_run_checkpoint(self.task.task_id, self.run.run_id, "report_correction_transcript")
        self.assertEqual(transcript["messages"], [{"role": "assistant", "content": "corrected"}])
        self.assertEqual(call.call_count, 1)

    async def test_valid_report_costs_no_extra_model_call(self):
        with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools") as call:
            result = await self.correct(report(self.ref))
        call.assert_not_called()
        self.assertEqual(result["report_validation"]["status"], "passed")

    async def test_batch_corrects_eight_references_in_one_tool_call_and_survives_resume(self):
        refs = [self.store.record_evidence(self.task.task_id, self.run.run_id, "host_inspect",
            {"host_id": f"host-{i}"}, {"ok": True, "observed_at": 1000, "host": f"host-{i}"})["ref"] for i in range(8)]
        corrected = report(refs[0])
        corrected["findings"] = [{"description": f"host-{i} is online", "evidence_refs": [ref]}
                                  for i, ref in enumerate(refs)]
        async def model(text, history, tools, execute, **kw):
            self.assertIn("read_task_evidence_batch", text)
            reply = json.loads(await execute("read_task_evidence_batch", {"requests": [{"ref": ref} for ref in refs]}))
            self.assertTrue(reply["ok"])
            self.assertEqual(len(reply["results"]), 8)
            self.assertTrue(all(page["next_offset"] is None for page in reply["results"]))
            return json.dumps(corrected)
        with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools", side_effect=model) as call:
            result = await self.correct()
            restored = await self.correct()
        self.assertEqual(result, restored)
        self.assertEqual(result["status"], "success")
        self.assertEqual(call.await_count, 1)
        checkpoint = self.store.latest_run_checkpoint(self.task.task_id, self.run.run_id, "report_correction")
        self.assertEqual(set(checkpoint["read_refs"]), set(refs))

    async def test_batch_does_not_bypass_parent_switch_or_full_read_requirement(self):
        async def model(text, history, tools, execute, **kw):
            with patch("src.plugins.ai_chat.subagents.tool_enabled", side_effect=lambda name: name != "read_task_evidence"):
                reply = json.loads(await execute("read_task_evidence_batch", {"requests": [{"ref": self.ref}]}))
                self.assertFalse(reply["ok"])
            return json.dumps(report(self.ref))
        with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools", side_effect=model):
            result = await self.correct()
        self.assertEqual(result["report_validation"]["status"], "incomplete")

    async def test_actual_loop_asks_for_missing_evidence_without_reopening_execution(self):
        calls = 0
        async def completion(*args, **kwargs):
            nonlocal calls
            calls += 1
            tool_calls = []
            if calls == 2:
                feedback = [m["content"] for m in kwargs["messages"]
                            if m["role"] == "user" and 'kind="validation_feedback"' in m["content"]]
                self.assertEqual(len(feedback), 1)
                self.assertIn(self.ref, feedback[0])
                tool_calls = [SimpleNamespace(id="read-missing", function=SimpleNamespace(
                    name="read_task_evidence", arguments=json.dumps({"ref": self.ref})))]
            message = SimpleNamespace(content="" if tool_calls else json.dumps(report(self.ref)), tool_calls=tool_calls)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None), ""
        with patch("src.plugins.ai_chat.agent_loop._completion_with_optional_stream", side_effect=completion):
            result = await self.correct()
        self.assertEqual(calls, 3)
        self.assertEqual(result["report_validation"]["status"], "passed")
        self.assertEqual(result["artifacts"], report()["artifacts"])
        self.assertEqual(self.store.agent_session(self.task.task_id, self.run.run_id,
            scope_key=self.task.scope_key, requester_user_id=2)["messages"], [])
        feedback = self.store.latest_run_checkpoint(self.task.task_id, self.run.run_id, "report_correction_feedback")
        self.assertIn(self.ref, feedback["errors"][0])
        transcript = self.store.latest_run_checkpoint(self.task.task_id, self.run.run_id, "report_correction_transcript")
        self.assertTrue(any(m["role"] == "tool" for m in transcript["messages"]))
        with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools") as model:
            self.assertEqual(await self.correct(), result)
        model.assert_not_called()

    async def test_invalid_final_feedback_exhausts_same_budget_and_stays_partial(self):
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps(report(self.ref)), tool_calls=[]))], usage=None), ""
        with patch("src.plugins.ai_chat.agent_loop._completion_with_optional_stream",
                   new=AsyncMock(return_value=response)) as model:
            result = await self.correct()
        self.assertEqual(model.await_count, 4)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["report_validation"]["status"], "incomplete")
        self.assertEqual(result["artifacts"], report()["artifacts"])

    async def test_batch_pages_survive_actual_tool_loop_and_transport_limits(self):
        refs = [self.store.record_evidence(self.task.task_id, self.run.run_id, "host_inspect",
            {"host_id": f"host-{i}"}, {"ok": True, "content": '\\"中\n<&>' * 80})["ref"]
            for i in range(8)]
        corrected = report(refs[0])
        corrected["findings"] = [{"description": f"host-{i} inspected", "evidence_refs": [ref]}
                                  for i, ref in enumerate(refs)]
        original = deepcopy(corrected)
        for i, finding in enumerate(original["findings"]):
            finding["evidence_refs"] = [f"evidence#old-{i}"]
        fragments = {ref: [] for ref in refs}
        requests = [{"ref": ref, "offset": 0} for ref in refs]
        seen_calls = set()
        limits = replace(settings, tool_max_result_chars=5000, tool_max_context_chars=30000)

        async def completion(*args, **kwargs):
            nonlocal requests
            self.assertLessEqual(sum(len(message["content"]) for message in kwargs["messages"]
                if message["role"] == "tool"), limits.tool_max_context_chars)
            for message in kwargs["messages"]:
                if message["role"] != "tool" or message["tool_call_id"] in seen_calls:
                    continue
                seen_calls.add(message["tool_call_id"])
                self.assertLessEqual(len(message["content"]), limits.tool_max_result_chars)
                opening, _, body = message["content"].partition("\n")
                body, _, closing = body.rpartition("\n")
                self.assertTrue(opening.startswith('<untrusted kind="tool_result" id="'))
                self.assertEqual(closing, '</untrusted id="' + opening.split('id="')[1])
                payload = json.loads(html.unescape(body))
                self.assertTrue(payload["ok"])
                requests = []
                for page in payload["results"]:
                    self.assertTrue(page["ok"])
                    fragments[page["ref"]].append(page["content"])
                    if page["next_offset"] is not None:
                        requests.append({"ref": page["ref"], "offset": page["next_offset"]})
            calls = [] if not requests else [SimpleNamespace(id=f"batch-{len(seen_calls)}",
                function=SimpleNamespace(name="read_task_evidence_batch",
                    arguments=json.dumps({"requests": requests})))]
            message = SimpleNamespace(content="" if requests else json.dumps(corrected), tool_calls=calls)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None), ""

        with patch("src.plugins.ai_chat.subagents.settings", limits), patch(
            "src.plugins.ai_chat.agent_loop.settings", limits), patch(
            "src.plugins.ai_chat.agent_loop._completion_with_optional_stream", side_effect=completion):
            result = await self.correct(original)
        self.assertGreater(len(seen_calls), 1)
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["report_validation"]["status"], "passed")
        checkpoint = self.store.latest_run_checkpoint(self.task.task_id, self.run.run_id, "report_correction")
        self.assertEqual(set(checkpoint["read_refs"]), set(refs))
        for item in self.store.task_evidence(self.task.task_id):
            if item["evidence_id"] in fragments:
                self.assertEqual(json.loads("".join(fragments[item["evidence_id"]])), item["payload"])

    async def test_batch_can_finish_single_read_pagination_but_cannot_skip_pages(self):
        ref = self.store.record_evidence(self.task.task_id, self.run.run_id, "host_inspect", {},
            {"content": "x" * 16000})["ref"]
        async def model(text, history, tools, execute, **kw):
            page = json.loads(await execute("read_task_evidence", {"ref": ref}))
            self.assertIsNotNone(page["next_offset"])
            raw = await execute("read_task_evidence_batch", {"requests": [{"ref": ref, "offset": page["next_offset"]}]})
            self.assertTrue(json.loads(raw)["results"][0]["next_offset"] is None)
            return json.dumps(report(ref))
        with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools", side_effect=model):
            result = await self.correct()
        self.assertEqual(result["status"], "success")

    async def test_transport_budget_cannot_grant_credit_for_truncated_evidence(self):
        async def model(text, history, tools, execute, **kw):
            for _ in range(3):
                raw = await execute("read_task_evidence_batch", {"requests": [{"ref": self.ref}]})
                self.assertFalse(json.loads(raw)["ok"])
            return json.dumps(report(self.ref))
        settings = SimpleNamespace(tool_max_context_chars=40, tool_max_result_chars=12000)
        with patch("src.plugins.ai_chat.subagents.settings", settings), patch(
            "src.plugins.ai_chat.subagents.ask_deepseek_with_tools", side_effect=model):
            result = await self.correct()
        self.assertEqual(result["report_validation"]["status"], "incomplete")

    async def test_step_corrects_claims_then_captures_real_file(self):
        upload = "artifact_" + "a" * 32
        self.store.record_evidence(self.task.task_id, self.run.run_id, "cluster_artifact_upload", {},
            {"artifact_id": upload, "status": "staged", "sha256": "b" * 64, "size_bytes": 12})
        original = report()
        original["artifacts"].append({"handle": upload, "kind": "cluster_artifact"})
        capture = AsyncMock(side_effect=lambda _task, artifacts: [
            {**item, "snapshot": "c" * 64, "size": 12} for item in artifacts])
        hooks = SimpleNamespace(workspaces=SimpleNamespace(capture=capture), approval_checker=None,
            handoff_tool=None, compensate_tool=None)
        async def worker(text, history, tools, execute, **kwargs):
            if "[只读交付纠错]" in text:
                return await self.valid_correction(text, history, tools, execute, **kwargs)
            return json.dumps(original)

        with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools", side_effect=worker) as call:
            result = await self.coordinator._run_step(self.task,
                TaskStep("inspect", "operator", "inspect", "report"), self.run,
                context=ContextPacket(self.task.scope_key, self.task.conversation_id, 2, 3, "inspect"),
                upstream={}, selected_profile=self.profile, tools_by_name={},
                execute_tool=AsyncMock(), hooks=hooks)
        self.assertEqual(call.await_count, 2)
        self.assertEqual(result.state, "success")
        self.assertEqual(len(capture.call_args.args[1]), 1)
        self.assertEqual(result.result["artifacts"][0]["snapshot"], "c" * 64)
        self.assertEqual(result.result["metadata"]["cluster_artifact_refs"][0]["handle"], upload)
        self.assertEqual(self.store.runs(self.task.task_id)[0].status, "succeeded")

    async def test_completed_correction_is_reused_after_resume(self):
        with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools",
                   side_effect=self.valid_correction) as call:
            first = await self.correct()
            second = await self.correct()
        self.assertEqual(call.await_count, 1)
        self.assertEqual(first, second)

    async def test_real_step_resume_never_reopens_execution_after_correction_checkpoint(self):
        for status in ("started", "completed", "failed"):
            with self.subTest(status=status):
                with self.store._transaction() as cursor:
                    cursor.execute("DELETE FROM subagent_checkpoints WHERE task_id=?", (self.task.task_id,))
                with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools",
                           side_effect=self.valid_correction):
                    await self.correct()
                if status != "completed":
                    self.store.append_checkpoint(self.task.task_id, "report_correction",
                        {"status": status, "fingerprint": "prior", "original": report()}, run_id=self.run.run_id)
                execute = AsyncMock()
                with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools") as call:
                    outcome = await self.coordinator._run_step(self.task,
                        TaskStep("inspect", "operator", "inspect", "report"), self.run,
                        context=ContextPacket(self.task.scope_key, self.task.conversation_id, 2, 3, "inspect"),
                        upstream={}, selected_profile=self.profile, tools_by_name={}, execute_tool=execute)
                call.assert_not_called()
                execute.assert_not_called()
                self.assertEqual(outcome.result["artifacts"], report()["artifacts"])
                self.assertEqual(outcome.state, "success" if status == "completed" else "partial")

    async def test_replacing_reference_without_reading_cannot_pass(self):
        with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools",
                   new=AsyncMock(return_value=json.dumps(report(self.ref)))):
            result = await self.correct()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["report_validation"]["status"], "incomplete")

    async def test_partial_or_out_of_order_evidence_read_cannot_pass(self):
        large_ref = self.store.record_evidence(self.task.task_id, self.run.run_id,
            "host_inspect", {}, {"content": "x" * 15000})["ref"]
        async def model(text, history, tools, execute, **kwargs):
            first = json.loads(await execute("read_task_evidence", {"ref": large_ref}))
            self.assertIsNotNone(first["next_offset"])
            await execute("read_task_evidence", {"ref": large_ref, "offset": 15010})
            return json.dumps(report(large_ref))
        with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools", side_effect=model):
            result = await self.correct()
        self.assertEqual(result["report_validation"]["status"], "incomplete")

    async def test_complete_paginated_evidence_read_passes(self):
        large_ref = self.store.record_evidence(self.task.task_id, self.run.run_id,
            "host_inspect", {}, {"content": "x" * 15000})["ref"]
        async def model(text, history, tools, execute, **kwargs):
            offset = 0
            while offset is not None:
                payload = json.loads(await execute("read_task_evidence", {"ref": large_ref, "offset": offset}))
                offset = payload["next_offset"]
            return json.dumps(report(large_ref))
        with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools", side_effect=model):
            result = await self.correct()
        self.assertEqual(result["report_validation"]["status"], "passed")

    async def test_failed_correction_remains_partial_without_retry_loop(self):
        with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools",
                   new=AsyncMock(side_effect=TimeoutError("unavailable"))) as call:
            first = await self.correct()
            second = await self.correct()
        self.assertEqual(call.await_count, 1)
        self.assertEqual(first["status"], "partial")
        self.assertEqual(second["report_validation"]["status"], "incomplete")

    async def test_still_invalid_correction_does_not_pass_after_resume(self):
        with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools",
                   new=AsyncMock(return_value=json.dumps(report("evidence#still-invalid")))) as call:
            first = await self.correct()
            second = await self.correct()
        self.assertEqual(call.await_count, 1)
        self.assertEqual(first, second)
        self.assertEqual(second["report_validation"]["status"], "incomplete")

    async def test_started_correction_does_not_restart_on_unknown_outcome(self):
        self.store.append_checkpoint(self.task.task_id, "report_correction",
            {"status": "started", "fingerprint": "prior"}, run_id=self.run.run_id)
        with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools") as call:
            result = await self.correct()
        call.assert_not_called()
        self.assertEqual(result["status"], "partial")

    async def test_revision_change_cannot_publish_correction(self):
        async def model(*args, **kw):
            self.store.update_control(self.task.task_id, expected_version=0, revision=2)
            return json.dumps(report(self.ref))
        with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools", side_effect=model):
            with self.assertRaises(LeaseLost):
                await self.correct()
        self.assertEqual(self.store.latest_run_checkpoint(self.task.task_id, self.run.run_id,
            "report_correction")["status"], "started")

    async def test_cancellation_escapes_correction(self):
        async def model(*args, **kw):
            raise asyncio.CancelledError
        with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools", side_effect=model):
            with self.assertRaises(asyncio.CancelledError):
                await self.correct()

    def test_readonly_correction_cannot_hide_execution_gaps_or_change_files(self):
        original = report()
        original.update(status="partial", unresolved=["scan timed out"], warnings=["limited data"])
        corrected = report(self.ref)
        corrected["artifacts"] = [{"handle": "sffff00:/workspace/invented.pdf"}]
        result = retain_execution_facts(original, corrected)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["unresolved"], ["scan timed out"])
        self.assertEqual(result["artifacts"], original["artifacts"])

    def test_correction_cannot_silently_clear_all_claims(self):
        corrected = report(self.ref)
        corrected["findings"] = []
        result = checked_report(retain_execution_facts(report(), corrected),
            self.store.task_evidence(self.task.task_id), required=True)
        self.assertEqual(result["status"], "partial")

    def test_only_proven_cluster_upload_is_separated_from_files(self):
        handle = "artifact_" + "a" * 32
        payload = {"artifact_id": handle, "status": "staged", "sha256": "b" * 64,
                   "name": "report.md", "size_bytes": 10}
        self.store.record_evidence(self.task.task_id, self.run.run_id,
            "cluster_artifact_upload", {}, payload)
        original = report(self.ref)
        original["artifacts"].append({"handle": handle, "kind": "cluster_artifact"})
        pristine = deepcopy(original)
        result = separate_cluster_artifacts(original, self.store.task_evidence(self.task.task_id))
        self.assertEqual(original, pristine)
        self.assertEqual(len(result["artifacts"]), 1)
        refs = result["metadata"]["cluster_artifact_refs"]
        self.assertEqual(refs[0]["handle"], handle)
        self.assertEqual(refs[0]["delivery"], "not_a_qq_receipt")
        self.assertEqual(len(separate_cluster_artifacts(original, [])["artifacts"]), 2)

    def test_forged_or_failed_upload_cannot_remove_artifact_errors(self):
        original = report()
        original["artifacts"].append({"handle": "artifact_" + "c" * 32})
        original["metadata"] = {"cluster_artifact_refs": [{"handle": "forged"}]}
        self.store.record_evidence(self.task.task_id, self.run.run_id, "cluster_artifact_upload", {},
            {"ok": False, "status": "staged", "artifact_id": "artifact_" + "c" * 32, "sha256": "f" * 64})
        result = separate_cluster_artifacts(original, self.store.task_evidence(self.task.task_id))
        self.assertEqual(len(result["artifacts"]), 2)
        self.assertNotIn("cluster_artifact_refs", result["metadata"])


if __name__ == "__main__":
    unittest.main()
