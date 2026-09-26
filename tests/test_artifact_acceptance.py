from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests.test_subagent_v2 import decision, profile
from src.plugins.ai_chat.agent import ContextPacket
from src.plugins.ai_chat.agent.artifact_acceptance import (
    artifact_delivery_allowed,
    artifact_identity,
    artifact_verdicts,
    separate_review_artifacts,
)
from src.plugins.ai_chat.agent.execution import EntryDecision
from src.plugins.ai_chat.deepseek import DeepSeekTrace
from src.plugins.ai_chat.model_catalog import ModelCatalog
from src.plugins.ai_chat.subagents import (
    AgentExecutionHooks,
    StepOutcome,
    SubAgentCoordinator,
    SubAgentStore,
    TaskStep,
    _acceptance_repair_target,
    _apply_completed_repairs,
    _delivery_outcomes,
    _single_observed_artifact,
    _settled_task_status,
)


def artifact(digit="a"):
    return {
        "name": "source.zip",
        "handle": f"s{digit * 6}:/workspace/source.zip",
        "snapshot": digit * 64,
        "size": 128,
    }


def check_for(file, *, ok=True):
    return {
        "step": "build",
        "artifact": file["name"],
        "artifact_key": artifact_identity(file),
        "ok": ok,
    }


def review_for(file, *, status="passed"):
    return {
        "artifact_key": artifact_identity(file),
        "status": status,
        "reason": "Inspected the source and ran its tests.",
    }


class ArtifactAcceptanceTests(unittest.TestCase):
    def test_review_references_are_not_new_artifacts_or_acceptance_evidence(self):
        file = artifact()
        original = {"status": "success", "artifacts": [{"handle": file["handle"]}],
                    "metadata": {"artifact_reviews": [review_for(file)]}}
        result = separate_review_artifacts(original, {"build": {"artifacts": [file]}})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["artifacts"], [])
        self.assertEqual(original["artifacts"], [{"handle": file["handle"]}])
        self.assertEqual(result["metadata"]["review_artifact_references"][0]["snapshot"], file["snapshot"])
        self.assertEqual(artifact_verdicts([check_for(file)], result, executed=False)[0]["status"], "failed")

    def test_review_rejects_unknown_modified_or_ambiguous_artifacts(self):
        file = artifact()
        for returned, sources in (
            ({"handle": "sbbbbbb:/workspace/report.md"}, [file]),
            ({**file, "snapshot": "b" * 64}, [file]),
            ({**file, "size": 999}, [file]),
            ({"handle": file["handle"]}, [file, {**file, "snapshot": "b" * 64}]),
            (file, []),
            (file, [{**file, "snapshot": "invalid"}]),
        ):
            with self.subTest(returned=returned, sources=sources):
                result = separate_review_artifacts({"status": "success", "artifacts": [returned]},
                    {"build": {"artifacts": sources}})
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["artifacts"], [])
                self.assertTrue(result["unresolved"])

    def test_review_reference_preserves_real_failure_and_removes_forged_references(self):
        file = artifact()
        original = {"status": "partial", "artifacts": [file], "unresolved": ["tests failed"],
                    "metadata": {"review_artifact_references": [{"snapshot": "forged"}]}}
        result = separate_review_artifacts(original, {"build": {"artifacts": [file]}})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["unresolved"], ["tests failed"])
        self.assertEqual(result["metadata"]["review_artifact_references"][0]["snapshot"], file["snapshot"])

    def test_identity_prefers_snapshot_and_only_falls_back_to_handle(self):
        file = artifact()
        self.assertEqual(artifact_identity(file), file["snapshot"])
        self.assertEqual(artifact_identity({"handle": file["handle"]}), file["handle"])
        self.assertEqual(artifact_identity({"snapshot": "", "handle": file["handle"]}), file["handle"])
        self.assertEqual(artifact_identity({"name": file["name"]}), "")
        self.assertEqual(artifact_identity({}), "")

    def test_preview_failure_does_not_override_exact_file_review(self):
        file = artifact()
        result = {
            "status": "partial",
            "unresolved": ["The preview service is unavailable."],
            "metadata": {"artifact_reviews": [review_for(file)]},
        }
        verdicts = artifact_verdicts([check_for(file)], result, executed=True)
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0]["artifact_key"], file["snapshot"])
        self.assertEqual(verdicts[0]["status"], "passed")
        self.assertTrue(artifact_delivery_allowed(file, {"status": "failed", "artifacts": verdicts}))

    def test_file_verdict_fails_closed_for_invalid_or_unsubstantiated_reviews(self):
        file = artifact()
        passed = review_for(file)
        failed = review_for(file, status="failed")
        cases = (
            ("bad_checksum", {**check_for(file, ok=False), "error": "Checksum mismatch"}, [passed], True),
            ("truthy_check_is_not_true", {**check_for(file), "ok": "true"}, [passed], True),
            ("content_rejected", check_for(file), [failed], True),
            ("missing_review", check_for(file), [], True),
            ("conflicting_reviews", check_for(file), [passed, failed], True),
            ("duplicate_passed_reviews", check_for(file), [passed, passed], True),
            ("wrong_hash", check_for(file), [review_for(artifact("b"))], True),
            ("filename_is_not_identity", check_for(file), [{**passed, "artifact_key": file["name"]}], True),
            ("handle_cannot_override_snapshot", check_for(file), [{**passed, "artifact_key": file["handle"]}], True),
            ("missing_tool_evidence", check_for(file), [passed], False),
            ("missing_identity", {**check_for(file), "artifact_key": ""}, [{**passed, "artifact_key": ""}], True),
        )
        for label, check, reviews, executed in cases:
            with self.subTest(case=label):
                verdicts = artifact_verdicts(
                    [check], {"status": "success", "metadata": {"artifact_reviews": reviews}},
                    executed=executed,
                )
                self.assertEqual(len(verdicts), 1)
                self.assertEqual(verdicts[0]["status"], "failed")
                self.assertFalse(artifact_delivery_allowed(file, {"status": "passed", "artifacts": verdicts}))

    def test_missing_or_malformed_review_metadata_cannot_authorize_file(self):
        file = artifact()
        for metadata in (None, {}, [], {"artifact_reviews": None}, {"artifact_reviews": {}},
                         {"artifact_reviews": [None, "passed", {"status": "passed"}]}):
            with self.subTest(metadata=metadata):
                verdicts = artifact_verdicts(
                    [check_for(file)], {"status": "success", "metadata": metadata}, executed=True,
                )
                self.assertEqual(verdicts[0]["status"], "failed")

    def test_delivery_gate_rejects_missing_duplicate_conflicting_and_wrong_hash_verdicts(self):
        file = artifact()
        passed = review_for(file)
        for reviews in ([], [review_for(file, status="failed")], [passed, passed],
                        [passed, review_for(file, status="failed")], [review_for(artifact("b"))]):
            with self.subTest(reviews=reviews):
                self.assertFalse(artifact_delivery_allowed(file, {"status": "passed", "artifacts": reviews}))

    def test_same_name_and_handle_with_different_snapshots_have_separate_verdicts(self):
        first = artifact()
        second = {**first, "snapshot": "b" * 64}
        verdicts = artifact_verdicts(
            [check_for(first), check_for(second)],
            {"metadata": {"artifact_reviews": [review_for(second, status="failed"), review_for(first)]}},
            executed=True,
        )
        self.assertEqual([item["artifact_key"] for item in verdicts], [first["snapshot"], second["snapshot"]])
        validation = {"status": "failed", "artifacts": verdicts}
        self.assertTrue(artifact_delivery_allowed(first, validation))
        self.assertFalse(artifact_delivery_allowed(second, validation))


class ArtifactAcceptanceRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "agents.sqlite3"
        self.catalog = ModelCatalog(
            {name: profile(name) for name in ("qwen-local", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol")},
            default_profile="qwen-local",
        )
        self.store = SubAgentStore(self.path)
        self.coordinator = SubAgentCoordinator(self.store, self.catalog, logger=Mock())
        self.packet = ContextPacket("group:1", "group:1:user:2", 2, 3, "Build and send the source archive")
        self.workspaces = SimpleNamespace(
            validate=AsyncMock(return_value={"ok": True}),
            deliver=AsyncMock(return_value=json.dumps({"ok": True})),
        )
        self.hooks = AgentExecutionHooks(workspaces=self.workspaces)
        self.execute = AsyncMock(return_value=json.dumps({"ok": True}))
        enabled = patch("src.plugins.ai_chat.subagents.tool_enabled", return_value=True)
        enabled.start()
        self.addCleanup(enabled.stop)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def submit(self):
        payload = decision("workflow")
        payload["delivery_required"] = True
        task = self.coordinator.submit(
            packet=self.packet, decision=EntryDecision.parse(payload),
            dispatch={"bot_id": "123", "event": {"user_id": 2}, "profile": "qwen-local"},
        )
        self.store.set_task_state(task.task_id, "running", plan={"contract": {"delivery_required": True}})
        return self.store.get(task.task_id)

    def outcome(self, task, key="build", *, artifacts=(), dependencies=(), state="success"):
        step = TaskStep(key, "coder", key, "source archive", dependencies)
        run = self.store.create_run(task.task_id, step, allowed_tools=[], model_profile="qwen-local")
        self.workspaces.capture = AsyncMock()
        result = {"status": state, "summary": key, "artifacts": list(artifacts)}
        self.store.finish_run(run.run_id, "succeeded" if state == "success" else state, result=result)
        return StepOutcome(step, run, result, DeepSeekTrace(), state)

    def reopen(self, task, completed):
        self.store.close()
        self.store = SubAgentStore(self.path)
        self.coordinator = SubAgentCoordinator(self.store, self.catalog, logger=Mock())
        runs = {run.run_id: run for run in self.store.runs(task.task_id)}
        restored = {
            key: StepOutcome(value.step, runs[value.run.run_id], runs[value.run.run_id].result,
                             DeepSeekTrace(), value.state)
            for key, value in completed.items()
        }
        return self.store.get(task.task_id), restored

    def reviewer(self, result, *, evidence="valid"):
        async def run_review(task, step, run, **kwargs):
            if evidence != "missing":
                run_id = run.run_id
                if evidence == "other_run":
                    run_id = next(item.run_id for item in self.store.runs(task.task_id) if item.run_id != run.run_id)
                self.store.append_event(
                    task.task_id,
                    "agent.tool_started" if evidence == "started_only" else "agent.tool_finished",
                    {
                        "tool_name": "import_agent_artifact" if evidence == "import_only" else "sandbox_exec",
                        "result": json.dumps({"returncode": 1 if evidence == "failed_command" else 0}),
                    },
                    run_id=run_id,
                )
            state = result["status"]
            self.store.finish_run(run.run_id, "succeeded" if state == "success" else state, result=result)
            return StepOutcome(step, run, result, DeepSeekTrace(), state)
        return AsyncMock(side_effect=run_review)

    async def validate(self, task, completed):
        return await self.coordinator._validate_workflow(
            task, completed, context=self.packet, selected_profile=self.catalog.default,
            tools_by_name={}, execute_tool=self.execute, hooks=self.hooks,
            parent_trace=None, progress=None,
        )

    async def deliver(self, task, completed, validation):
        return await self.coordinator._deliver_requested_artifacts(
            task, completed, execute_tool=self.execute, delivered_artifacts=set(),
            progress=None, hooks=self.hooks, validation=validation,
        )

    async def test_real_review_step_does_not_recapture_author_artifact(self):
        task = self.submit()
        file = artifact()
        completed = {"build": self.outcome(task, artifacts=[file])}
        self.workspaces.capture = AsyncMock(side_effect=AssertionError("review must not export author sandbox"))

        async def model(text, history, tools, execute, **kwargs):
            self.assertIn("artifacts 必须为空数组", text)
            run = next(item for item in self.store.runs(task.task_id) if item.step_key.startswith("acceptance_r"))
            self.store.append_event(task.task_id, "agent.tool_finished", {
                "tool_name": "sandbox_exec", "result": json.dumps({"returncode": 0}),
            }, run_id=run.run_id)
            return json.dumps({"status": "success", "summary": "Checked exact report bytes",
                "artifacts": [{"handle": file["handle"], "name": file["name"]}],
                "metadata": {"artifact_reviews": [review_for(file)]}})

        with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools", side_effect=model):
            validated = await self.validate(task, completed)
        self.assertEqual(validated["status"], "passed")
        self.workspaces.capture.assert_not_awaited()
        review = next(item for item in self.store.runs(task.task_id) if item.run_id == validated["run_id"])
        self.assertEqual(review.status, "succeeded")
        self.assertEqual(review.result["artifacts"], [])
        self.assertEqual(review.result["metadata"]["review_artifact_references"][0]["snapshot"], file["snapshot"])
        self.assertEqual(completed["build"].result["artifacts"], [file])
        task, completed = self.reopen(task, completed)
        with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools") as model:
            cached = await self.validate(task, completed)
        model.assert_not_called()
        self.assertEqual(cached, validated)

    async def test_review_mode_does_not_accept_undeclared_upstream_reference(self):
        task = self.submit()
        file = artifact()
        step = TaskStep("review", "coder", "review", "findings", ("build",))
        run = self.store.create_run(task.task_id, step, allowed_tools=[], model_profile="qwen-local")
        self.workspaces.capture = AsyncMock()
        with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools", new=AsyncMock(return_value=json.dumps({
            "status": "success", "summary": "Checked", "artifacts": [file],
        }))):
            result = await self.coordinator._run_step_reliably(task, step, run, context=self.packet,
                upstream={"previous_version": {"artifacts": [file]}},
                selected_profile=self.catalog.default, tools_by_name={}, execute_tool=self.execute,
                hooks=self.hooks, review_only=True)
        self.assertEqual(result.state, "failed")
        self.workspaces.capture.assert_not_awaited()
        self.assertEqual(result.result["artifacts"], [])

    async def test_review_scratch_pdf_is_not_recovered_as_deliverable(self):
        task = self.submit()
        file = artifact()
        step = TaskStep("review", "coder", "Check the PDF", "review", ("build",))
        run = self.store.create_run(task.task_id, step, allowed_tools=[], model_profile="qwen-local")
        self.workspaces.capture = AsyncMock()

        async def model(text, history, tools, execute, **kwargs):
            self.store.record_evidence(task.task_id, run.run_id, "sandbox_exec",
                {"sandbox_id": "s183bee"}, {"ok": True, "observed_manifest": {
                    "changed_workspace_paths": [f"tasks/{task.task_id}/steps/review/review.pdf"],
                }})
            return json.dumps({"status": "success", "summary": "PDF checked",
                "artifacts": [], "metadata": {"artifact_reviews": [review_for(file)]}})

        with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools", side_effect=model):
            result = await self.coordinator._run_step_reliably(task, step, run, context=self.packet,
                upstream={"build": {"artifacts": [file]}}, selected_profile=self.catalog.default,
                tools_by_name={}, execute_tool=self.execute, hooks=self.hooks, review_only=True)
        self.assertEqual(result.state, "success")
        self.assertEqual(result.result["artifacts"], [])
        self.assertIsNotNone(_single_observed_artifact(
            self.store.task_evidence(task.task_id, run_ids={run.run_id}), run.run_id, task.objective))
        self.workspaces.capture.assert_not_awaited()

    async def test_workflow_sends_exact_passed_file_despite_failed_preview_acceptance(self):
        task = self.submit()
        file = artifact()
        completed = {"build": self.outcome(task, artifacts=[file])}
        validation = {
            "status": "failed", "summary": "The preview service is unavailable.",
            "artifacts": [review_for(file)],
        }
        self.coordinator.max_adaptive_repairs = 0
        with patch.object(self.coordinator, "_validate_workflow", new=AsyncMock(return_value=validation)), patch.object(
            self.coordinator, "_supervisor_text", new=AsyncMock(return_value="Source ready; preview unavailable.")
        ):
            await self.coordinator._execute_workflow(
                task, steps=[], runs={}, context=self.packet, selected_profile=self.catalog.default,
                tools_by_name={}, execute_tool=self.execute, parent_trace=None, progress=None,
                hooks=self.hooks, initial_completed=completed,
            )
        self.workspaces.deliver.assert_awaited_once()
        self.assertEqual(self.workspaces.deliver.await_args.args[1]["snapshot"], file["snapshot"])
        self.execute.assert_not_awaited()
        current = self.store.get(task.task_id)
        self.assertEqual(current.status, "partial")
        self.assertEqual(current.result["validation"]["acceptance"], validation)
        self.assertTrue(current.result["deliveries"][0]["ok"])
        self.assertEqual(self.store.deliveries(task.task_id)[0]["key"], file["snapshot"])

    async def test_all_passed_files_skip_acceptance_repair_and_send_while_publish_remains_partial(self):
        for publish_state in ("failed", "partial"):
            with self.subTest(publish_state=publish_state):
                task = self.submit()
                file = artifact()
                completed = {
                    "validate": self.outcome(task, "validate", artifacts=[file]),
                    "publish": self.outcome(task, "publish", dependencies=("validate",), state=publish_state),
                }
                validation = {
                    "status": "failed", "summary": "Preview upload failed; source verified.",
                    "checks": [{**check_for(file), "step": "validate"}],
                    "artifacts": [{**review_for(file), "step": "validate"}],
                }
                self.workspaces.deliver.reset_mock()
                with patch.object(self.coordinator, "_validate_workflow", new=AsyncMock(return_value=validation)), patch.object(
                    self.coordinator, "_attempt_adaptive_repair", new=AsyncMock(return_value=(False, None))
                ) as repair, patch.object(
                    self.coordinator, "_supervisor_text", new=AsyncMock(return_value="Source delivered; preview unavailable.")
                ):
                    await self.coordinator._execute_workflow(
                        task, steps=[], runs={}, context=self.packet, selected_profile=self.catalog.default,
                        tools_by_name={}, execute_tool=self.execute, parent_trace=None, progress=None,
                        hooks=self.hooks, initial_completed=completed,
                    )
                repair.assert_not_awaited()
                self.workspaces.deliver.assert_awaited_once()
                self.assertEqual(self.workspaces.deliver.await_args.args[1]["snapshot"], file["snapshot"])
                current = self.store.get(task.task_id)
                self.assertEqual(current.status, "partial")
                self.assertTrue(current.result["deliveries"][0]["ok"])
                self.assertEqual(current.result["steps"]["publish"]["status"], publish_state)
                self.assertEqual(current.result["steps"]["validate"]["status"], "success")
        self.execute.assert_not_awaited()

    async def test_mixed_files_send_only_the_independently_accepted_snapshot(self):
        task = self.submit()
        good, bad_format, bad_content, missing, conflicting, wrong_hash, duplicated = (
            artifact(digit) for digit in "abcdef0"
        )
        files = [bad_format, bad_content, missing, conflicting, wrong_hash, duplicated, good]
        completed = {"build": self.outcome(task, artifacts=files)}
        reviews = [
            review_for(good), review_for(bad_format), review_for(bad_content, status="failed"),
            review_for(conflicting), review_for(conflicting, status="failed"),
            review_for(artifact("1")), review_for(duplicated), review_for(duplicated),
        ]
        verdicts = artifact_verdicts(
            [check_for(file, ok=file is not bad_format) for file in files],
            {"metadata": {"artifact_reviews": reviews}}, executed=True,
        )
        deliveries = await self.deliver(task, completed, {"status": "failed", "artifacts": verdicts})
        self.assertEqual(len(deliveries), len(files))
        self.assertEqual([item["handle"] for item in deliveries if item["ok"]], [good["handle"]])
        self.assertTrue(all(item["state"] == "validation_failed" for item in deliveries if not item["ok"]))
        self.workspaces.deliver.assert_awaited_once()
        self.assertEqual(self.workspaces.deliver.await_args.args[1]["snapshot"], good["snapshot"])
        self.execute.assert_not_awaited()
        self.assertEqual([item["key"] for item in self.store.deliveries(task.task_id)], [good["snapshot"]])

    async def test_same_named_passed_snapshots_are_delivered_and_recorded_separately(self):
        task = self.submit()
        first, second = artifact(), artifact("b")
        completed = {"build": self.outcome(task, artifacts=[first, second])}
        deliveries = await self.deliver(task, completed, {
            "status": "passed", "artifacts": [review_for(second), review_for(first)],
        })
        self.assertEqual(len(deliveries), 2)
        self.assertTrue(all(item["ok"] for item in deliveries))
        self.assertEqual(self.workspaces.deliver.await_count, 2)
        sent = [call.args[1] for call in self.workspaces.deliver.await_args_list]
        self.assertEqual([item["snapshot"] for item in sent], [first["snapshot"], second["snapshot"]])
        self.assertNotEqual(sent[0]["name"], sent[1]["name"])
        self.assertEqual({item["key"] for item in self.store.deliveries(task.task_id)},
                         {first["snapshot"], second["snapshot"]})

    async def test_rejected_snapshot_at_same_path_does_not_hide_passed_snapshot(self):
        for rejected_first in (True, False):
            with self.subTest(rejected_first=rejected_first):
                task = self.submit()
                rejected = artifact()
                passed = {**rejected, "snapshot": "b" * 64}
                files = [rejected, passed] if rejected_first else [passed, rejected]
                completed = {"validate": self.outcome(task, "validate", artifacts=files)}
                self.workspaces.deliver.reset_mock()
                deliveries = await self.deliver(task, completed, {
                    "status": "failed",
                    "artifacts": [review_for(rejected, status="failed"), review_for(passed)],
                })
                self.assertEqual(len(deliveries), 2)
                self.assertEqual(sum(item["ok"] is True for item in deliveries), 1)
                self.assertEqual([item["state"] for item in deliveries if not item["ok"]], ["validation_failed"])
                self.workspaces.deliver.assert_awaited_once()
                self.assertEqual(self.workspaces.deliver.await_args.args[1]["snapshot"], passed["snapshot"])
                self.assertEqual([item["key"] for item in self.store.deliveries(task.task_id)], [passed["snapshot"]])
        self.execute.assert_not_awaited()

    async def test_same_path_passed_snapshots_are_sent_once_per_hash(self):
        task = self.submit()
        first = artifact()
        second = {**first, "snapshot": "b" * 64}
        completed = {"validate": self.outcome(task, "validate", artifacts=[first, second, dict(first)])}
        deliveries = await self.deliver(task, completed, {
            "status": "passed", "artifacts": [review_for(first), review_for(second)],
        })
        self.assertEqual(len(deliveries), 2)
        self.assertTrue(all(item["ok"] for item in deliveries))
        self.assertEqual([call.args[1]["snapshot"] for call in self.workspaces.deliver.await_args_list],
                         [first["snapshot"], second["snapshot"]])
        self.assertEqual({item["key"] for item in self.store.deliveries(task.task_id)},
                         {first["snapshot"], second["snapshot"]})
        self.execute.assert_not_awaited()

    async def test_historical_path_receipt_cannot_skip_new_snapshot_after_revision_and_restart(self):
        task = self.submit()
        old = artifact()
        new = {**old, "snapshot": "b" * 64}
        self.store.begin_delivery(task.task_id, old["snapshot"], {"filename": old["name"]})
        self.store.finish_delivery(task.task_id, old["snapshot"], "acknowledged", {"ok": True})
        self.store.append_checkpoint(task.task_id, "artifact_delivery", {
            "sandbox_id": "saaaaaa", "path": "/workspace/source.zip",
            "filename": old["name"], "ok": True,
        })
        control = self.store.control(task.task_id)
        self.store.update_control(task.task_id, expected_version=control["version"], revision=2)
        completed = {"validate": self.outcome(task, "validate", artifacts=[new])}
        task, completed = self.reopen(task, completed)
        validation = {"status": "passed", "artifacts": [review_for(new)]}
        with patch.object(self.coordinator, "_validate_workflow", new=AsyncMock(return_value=validation)), patch.object(
            self.coordinator, "_supervisor_text", new=AsyncMock(return_value="Updated source delivered.")
        ):
            await self.coordinator._execute_workflow(
                task, steps=[], runs={}, context=self.packet, selected_profile=self.catalog.default,
                tools_by_name={}, execute_tool=self.execute, parent_trace=None, progress=None,
                hooks=self.hooks, initial_completed=completed,
            )
        self.workspaces.deliver.assert_awaited_once()
        self.assertEqual(self.workspaces.deliver.await_args.args[1]["snapshot"], new["snapshot"])
        deliveries = self.store.get(task.task_id).result["deliveries"]
        self.assertEqual(len(deliveries), 1)
        self.assertTrue(deliveries[0]["ok"])
        self.assertFalse(deliveries[0].get("already_delivered", False))
        self.assertEqual({(item["revision"], item["key"], item["state"])
                          for item in self.store.deliveries(task.task_id)}, {
            (1, old["snapshot"], "acknowledged"), (2, new["snapshot"], "acknowledged"),
        })
        self.execute.assert_not_awaited()

    async def test_cached_acceptance_after_restart_preserves_individual_file_verdicts(self):
        task = self.submit()
        first, second = artifact(), artifact("b")
        completed = {"build": self.outcome(task, artifacts=[first, second])}
        completed["build_alias"] = completed["build"]
        review_result = {
            "status": "partial", "summary": "Preview unavailable; one source archive is valid.",
            "unresolved": ["The preview service is unavailable."],
            "metadata": {"artifact_reviews": [review_for(second, status="failed"), review_for(first)]},
        }
        with patch.object(self.coordinator, "_run_step_reliably", new=self.reviewer(review_result)) as worker:
            fresh = await self.validate(task, completed)
        worker.assert_awaited_once()
        self.assertEqual(fresh["status"], "failed")
        self.assertEqual([item["status"] for item in fresh["artifacts"]], ["passed", "failed"])
        self.assertEqual(self.workspaces.validate.await_count, 2)
        self.assertEqual([item["artifact_key"] for item in fresh["checks"]], [first["snapshot"], second["snapshot"]])
        task, completed = self.reopen(task, completed)
        with patch.object(self.coordinator, "_run_step_reliably", new=AsyncMock()) as worker:
            cached = await self.validate(task, completed)
        worker.assert_not_awaited()
        self.assertEqual(cached, fresh)
        self.assertEqual(self.workspaces.validate.await_count, 4)
        deliveries = await self.deliver(task, completed, cached)
        self.assertEqual([item["handle"] for item in deliveries if item["ok"]], [first["handle"]])
        self.workspaces.deliver.assert_awaited_once()

    async def test_fresh_and_cached_success_claims_require_successful_acceptance_run_tool_evidence(self):
        for evidence in ("missing", "failed_command", "other_run", "started_only", "import_only"):
            with self.subTest(evidence=evidence):
                task = self.submit()
                file = artifact()
                completed = {"build": self.outcome(task, artifacts=[file])}
                review_result = {"status": "success", "metadata": {"artifact_reviews": [review_for(file)]}}
                with patch.object(self.coordinator, "_run_step_reliably", new=self.reviewer(review_result, evidence=evidence)):
                    fresh = await self.validate(task, completed)
                self.assertEqual(fresh["status"], "failed")
                self.assertEqual(fresh["artifacts"][0]["status"], "failed")
                # Historical cached success is not proof that the verifier ran a check.
                self.store.finish_run(fresh["run_id"], "succeeded", result=review_result)
                task, completed = self.reopen(task, completed)
                with patch.object(self.coordinator, "_run_step_reliably", new=AsyncMock()) as worker:
                    cached = await self.validate(task, completed)
                worker.assert_not_awaited()
                self.assertEqual(cached["run_id"], fresh["run_id"])
                self.assertEqual(cached["status"], "failed")
                self.assertEqual(cached["artifacts"][0]["status"], "failed")
                deliveries = await self.deliver(task, completed, cached)
                self.assertEqual(deliveries[0]["state"], "validation_failed")
                self.assertFalse(deliveries[0]["ok"])
                self.assertEqual(self.store.deliveries(task.task_id), [])
        self.workspaces.deliver.assert_not_awaited()
        self.execute.assert_not_awaited()

    async def test_cached_success_rechecks_snapshot_integrity_after_restart(self):
        task = self.submit()
        file = artifact()
        completed = {"build": self.outcome(task, artifacts=[file])}
        review_result = {"status": "success", "metadata": {"artifact_reviews": [review_for(file)]}}
        with patch.object(self.coordinator, "_run_step_reliably", new=self.reviewer(review_result)):
            fresh = await self.validate(task, completed)
        self.assertEqual(fresh["status"], "passed")
        self.assertEqual(fresh["artifacts"][0]["status"], "passed")
        task, completed = self.reopen(task, completed)
        self.workspaces.validate.return_value = {"ok": False, "error": "Snapshot checksum mismatch"}
        with patch.object(self.coordinator, "_run_step_reliably", new=AsyncMock()) as worker:
            cached = await self.validate(task, completed)
        worker.assert_not_awaited()
        self.assertEqual(self.workspaces.validate.await_count, 2)
        self.assertEqual(cached["run_id"], fresh["run_id"])
        self.assertEqual(cached["status"], "failed")
        self.assertEqual(cached["artifacts"][0]["status"], "failed")
        self.assertIn("checksum mismatch", cached["artifacts"][0]["reason"])
        deliveries = await self.deliver(task, completed, cached)
        self.assertFalse(deliveries[0]["ok"])
        self.workspaces.deliver.assert_not_awaited()

    async def test_failed_acceptance_never_completes_all_successful_steps(self):
        task = self.submit()
        outcomes = [self.outcome(task, "build"), self.outcome(task, "validate", dependencies=("build",))]
        for deliveries in ([], [{"ok": True}], [{"ok": False}]):
            with self.subTest(deliveries=deliveries):
                self.assertEqual(_settled_task_status(outcomes, deliveries, {"status": "failed"}), "partial")
        self.assertEqual(_settled_task_status(outcomes, [{"ok": True}], {"status": "passed"}), "completed")

    async def test_acceptance_repair_target_falls_back_to_failed_or_partial_publish_not_successful_validate(self):
        for publish_state in ("failed", "partial"):
            with self.subTest(publish_state=publish_state):
                task = self.submit()
                file = artifact()
                publish = self.outcome(task, "publish", dependencies=("validate",), state=publish_state)
                validate = self.outcome(task, "validate", artifacts=[file])
                completed = {"publish": publish, "validate": validate}
                validation = {
                    "status": "failed", "checks": [{**check_for(file), "step": "validate"}],
                    "artifacts": [{**review_for(file), "step": "validate"}],
                }
                self.assertIs(_acceptance_repair_target(completed, validation), publish)
                self.assertIsNone(_acceptance_repair_target({"validate": validate}, validation))

    async def test_acceptance_repair_target_prioritizes_bad_file_then_format_before_unfinished_publish(self):
        task = self.submit()
        build = self.outcome(task, "build", artifacts=[artifact()])
        validate = self.outcome(task, "validate", artifacts=[artifact("b")], dependencies=("build",))
        publish = self.outcome(task, "publish", dependencies=("validate",), state="failed")
        completed = {"build": build, "validate": validate, "publish": publish}
        validation = {
            "status": "failed",
            "artifacts": [{**review_for(artifact("b"), status="failed"), "step": "validate"}],
            "checks": [{**check_for(artifact(), ok=False), "step": "build"}],
        }
        self.assertIs(_acceptance_repair_target(completed, validation), validate)
        validation["artifacts"] = [{**review_for(artifact("b")), "step": "validate"}]
        self.assertIs(_acceptance_repair_target(completed, validation), build)
        validation["checks"] = [{**check_for(artifact()), "step": "build"}]
        self.assertIs(_acceptance_repair_target(completed, validation), publish)

    async def test_passed_acceptance_cannot_complete_mandatory_failure_without_matching_successful_repair(self):
        task = self.submit()
        validate = self.outcome(task, "validate")
        publish = self.outcome(task, "publish", dependencies=("validate",), state="failed")
        unrelated = self.outcome(task, "validate__repair_1")
        partial_repair = self.outcome(task, "publish__repair_1", state="partial")
        successful_repair = self.outcome(task, "publish__repair_2")
        deliveries = [{"ok": True}]
        validation = {"status": "passed"}
        for extra in ([], [unrelated], [partial_repair]):
            with self.subTest(extra=[item.step.key for item in extra]):
                self.assertEqual(_settled_task_status([validate, publish, *extra], deliveries, validation), "partial")
        self.assertEqual(_settled_task_status([validate, publish, successful_repair], deliveries, validation), "completed")
        skipped = self.outcome(task, "skipped_publish", state="skipped")
        self.assertEqual(_settled_task_status([validate, skipped], deliveries, validation), "partial")

    async def test_preview_upload_failure_is_not_promoted_to_completed_by_fresh_or_cached_acceptance(self):
        for state in ("partial", "failed"):
            with self.subTest(review_state=state):
                task = self.submit()
                file = artifact()
                completed = {
                    "validate": self.outcome(task, "validate", artifacts=[file]),
                    "publish": self.outcome(task, "publish", dependencies=("validate",), state="failed"),
                }
                review_result = {
                    "status": state, "summary": "Source verified; preview deployment unavailable.",
                    "unresolved": ["Failed to upload the preview deployment"],
                    "metadata": {"artifact_reviews": [review_for(file)]},
                }
                with patch.object(self.coordinator, "_run_step_reliably", new=self.reviewer(review_result)):
                    fresh = await self.validate(task, completed)
                self.assertEqual(fresh["status"], "failed")
                self.assertEqual(fresh["artifacts"][0]["status"], "passed")
                self.assertEqual(_settled_task_status(list(completed.values()), [{"ok": True}], fresh), "partial")
                task, completed = self.reopen(task, completed)
                with patch.object(self.coordinator, "_run_step_reliably", new=AsyncMock()) as worker:
                    cached = await self.validate(task, completed)
                worker.assert_not_awaited()
                self.assertEqual(cached["run_id"], fresh["run_id"])
                self.assertEqual(cached["status"], "failed")
                self.assertEqual(cached["artifacts"][0]["status"], "passed")
                deliveries = await self.deliver(task, completed, cached)
                self.assertTrue(deliveries[0]["ok"])
                self.assertEqual(_settled_task_status(list(completed.values()), deliveries, cached), "partial")

    async def test_delivery_selection_resolves_repair_alias_before_selecting_validated_successor(self):
        task = self.submit()
        self.store.set_task_state(task.task_id, "running", plan={
            "contract": {"delivery_required": True},
            "steps": [
                {"id": "build"}, {"id": "validate", "depends_on": ["build"]},
                {"id": "publish", "depends_on": ["validate"]},
            ],
        })
        task = self.store.get(task.task_id)
        build = self.outcome(task, "build", state="failed")
        repair = self.outcome(task, "build__repair_1", artifacts=[artifact()])
        validate = self.outcome(task, "validate", artifacts=[artifact("b")], dependencies=("build",))
        publish = self.outcome(task, "publish", dependencies=("validate",), state="failed")
        for order in ((build, repair, validate, publish), (publish, validate, repair, build)):
            with self.subTest(order=[item.step.key for item in order]):
                completed = {item.step.key: item for item in order}
                _apply_completed_repairs(completed)
                self.assertEqual(completed["build"].step.key, "build__repair_1")
                selected = _delivery_outcomes(task, completed)
                self.assertEqual([item.step.key for item in selected], ["validate"])

    async def test_delivery_selection_skips_draft_when_publish_leaf_has_no_files(self):
        task = self.submit()
        build = self.outcome(task, "build", artifacts=[artifact()])
        validate = self.outcome(task, "validate", artifacts=[artifact("b")], dependencies=("build",))
        publish = self.outcome(task, "publish", dependencies=("validate",))
        for order in ((build, validate, publish), (publish, validate, build)):
            with self.subTest(order=[item.step.key for item in order]):
                selected = _delivery_outcomes(task, {item.step.key: item for item in order})
                self.assertEqual([item.step.key for item in selected], ["validate"])

    async def test_delivery_selection_keeps_independent_branch_and_transitive_successor(self):
        task = self.submit()
        build = self.outcome(task, "build", artifacts=[artifact()])
        inspect = self.outcome(task, "inspect", dependencies=("build",))
        validate = self.outcome(task, "validate", artifacts=[artifact("b")], dependencies=("inspect",))
        independent = self.outcome(task, "independent", artifacts=[artifact("c")])
        publish = self.outcome(task, "publish", dependencies=("validate", "independent"))
        for order in ((build, inspect, validate, independent, publish),
                      (publish, independent, validate, inspect, build)):
            with self.subTest(order=[item.step.key for item in order]):
                selected = _delivery_outcomes(task, {item.step.key: item for item in order})
                self.assertEqual({item.step.key for item in selected}, {"validate", "independent"})
                self.assertEqual(len(selected), 2)


if __name__ == "__main__":
    unittest.main()
