from __future__ import annotations

from tests.postgres_fixture import postgres_for_test

import unittest
from unittest.mock import Mock, patch

from tests.test_subagent_v2 import profile
from src.plugins.ai_chat.model_catalog import ModelCatalog
from src.plugins.ai_chat.subagents import SubAgentCoordinator, SubAgentStore, TaskStep


class AdaptiveRepairCountTests(unittest.TestCase):
    def setUp(self):
        self.store = SubAgentStore(postgres_for_test(self))
        self.catalog = ModelCatalog({"test": profile("test")}, default_profile="test")
        self.task = self.new_task()
        self.run = self.store.runs(self.task.task_id)[0]

    def tearDown(self):
        self.store.close()

    def new_task(self):
        task = self.store.create_task(scope_key="group:1", conversation_id="group:1:user:2",
            requester_user_id=2, trigger_message_id=None, objective="inspect",
            max_parallelism=3, max_steps=5)
        self.store.create_run(task.task_id, TaskStep("inspect", "operator", "inspect", "report"),
            allowed_tools=[], model_profile="test")
        return task

    def checkpoint(self, phase, *, task=None, now=None):
        return self.store.append_checkpoint((task or self.task).task_id, phase, {}, now=now)

    def fill_page(self, *, task=None):
        for _ in range(205):
            self.checkpoint("tool_progress", task=task)

    def revise(self, *, task=None):
        task = task or self.task
        control = self.store.control(task.task_id)
        if not control["dispatch"]:
            control = self.store.update_control(task.task_id, expected_version=control["version"],
                dispatch={"bot_id": "123"})
        self.store.set_task_state(task.task_id, "partial")
        coordinator = SubAgentCoordinator(self.store, self.catalog, logger=Mock())
        return coordinator.revise(task.task_id, scope_key=task.scope_key,
            requester_user_id=task.requester_user_id, instruction="inspect again",
            step_keys=["inspect"], expected_version=control["version"])

    def count(self, task=None):
        return self.store.current_revision_adaptive_repair_count((task or self.task).task_id)

    def reopen(self):
        self.store.close()
        self.store = SubAgentStore(postgres_for_test(self))

    def test_initial_revision_counts_only_planned_repairs_without_control_row(self):
        self.assertEqual(self.count(), 0)
        self.checkpoint("adaptive_repair_planned")
        for phase in ("adaptive_repair_completed", "adaptive_repair_planned_extra",
                      "process_interrupted", "workflow_completed"):
            self.checkpoint(phase)
        self.store.append_event(self.task.task_id, "repair.declined", {})
        self.store.append_event(self.task.task_id, "repair.planning_failed", {})
        self.assertEqual(self.store.control(self.task.task_id)["version"], 0)
        self.assertEqual(self.count(), 1)

    def test_initial_revision_repairs_beyond_first_page_are_counted(self):
        self.fill_page()
        self.checkpoint("adaptive_repair_planned")
        self.checkpoint("adaptive_repair_planned")
        self.assertEqual(len(self.store.checkpoints(self.task.task_id)), 200)
        with patch.object(self.store, "checkpoints", side_effect=AssertionError("No checkpoint pages")), \
                patch.object(self.store, "revision_checkpoints", side_effect=AssertionError("Use scoped SQL")):
            self.assertEqual(self.count(), 2)

    def test_latest_revision_excludes_old_repairs_and_counts_current_after_page_limit(self):
        self.checkpoint("adaptive_repair_planned")
        self.checkpoint("adaptive_repair_planned")
        self.fill_page()
        self.assertEqual(self.revise()["revision"], 2)
        self.assertFalse(any(item["phase"] == "revision_requested"
                             for item in self.store.checkpoints(self.task.task_id)))
        self.assertEqual(self.count(), 0)
        self.fill_page()
        # Sequence, not wall-clock ordering, determines revision membership.
        self.checkpoint("adaptive_repair_planned", now=1)
        self.checkpoint("adaptive_repair_completed", now=1)
        self.checkpoint("adaptive_repair_planned", now=1)
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.revise()["revision"], 3)
        self.assertEqual(self.count(), 0)
        self.checkpoint("adaptive_repair_planned")
        self.assertEqual(self.count(), 1)

    def test_restart_preserves_reserved_repairs_and_only_revision_resets_count(self):
        self.fill_page()
        self.checkpoint("adaptive_repair_planned")
        self.revise()
        self.checkpoint("adaptive_repair_planned")
        self.store.interrupt_task(self.task.task_id)
        self.reopen()
        self.assertEqual(self.store.control(self.task.task_id)["revision"], 2)
        self.assertEqual(self.count(), 1)
        self.checkpoint("adaptive_repair_completed")
        self.checkpoint("adaptive_repair_planned")
        self.reopen()
        self.assertEqual(self.count(), 2)
        self.revise()
        self.reopen()
        self.assertEqual(self.store.control(self.task.task_id)["revision"], 3)
        self.assertEqual(self.count(), 0)

    def test_task_counts_and_revision_boundaries_are_independent(self):
        other = self.new_task()
        self.checkpoint("adaptive_repair_planned")
        self.fill_page(task=other)
        self.checkpoint("adaptive_repair_planned", task=other)
        self.revise(task=other)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.count(other), 0)
        self.checkpoint("adaptive_repair_planned", task=other)
        self.checkpoint("adaptive_repair_planned", task=other)
        self.revise()
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.count(other), 2)

    def test_repeated_counts_are_read_only_and_control_version_does_not_reset_them(self):
        self.checkpoint("adaptive_repair_planned")
        self.store.update_control(self.task.task_id, expected_version=0, dispatch={"bot_id": "123"})
        self.store.update_control(self.task.task_id, expected_version=1, policy={"mode": "auto"})
        changed = Mock()
        self.store.set_change_listener(changed)
        with (
            patch.object(
                self.store._connection, "execute", wraps=self.store._connection.execute
            ) as queries,
            patch.object(
                self.store._connection,
                "cursor",
                side_effect=AssertionError("Read path opened a transaction"),
            ),
        ):
            for _ in range(3):
                self.assertEqual(self.count(), 1)
        self.assertEqual(queries.call_count, 3)
        self.assertTrue(
            all(
                call.args[0].lstrip().upper().startswith("SELECT")
                for call in queries.call_args_list
            )
        )
        changed.assert_not_called()

    def test_latest_run_checkpoint_returns_full_state_after_page_limit(self):
        task_id, run_id, phase = self.task.task_id, self.run.run_id, "report_correction"
        self.assertIsNone(self.store.latest_run_checkpoint(task_id, run_id, phase))
        self.store.append_checkpoint(task_id, phase, {}, run_id=run_id)
        self.assertEqual(self.store.latest_run_checkpoint(task_id, run_id, phase), {})
        self.fill_page()
        original = {"status": "partial", "findings": [{"evidence_refs": ["evidence#1"]}]}
        started = {"fingerprint": "a" * 64, "status": "started", "original": original}
        self.store.append_checkpoint(task_id, phase, started, run_id=run_id, now=1000)
        completed = {**started, "status": "completed", "corrected": {"status": "success", "score": 0}}
        self.store.append_checkpoint(task_id, phase, completed, run_id=run_id, now=1)
        with patch.object(self.store, "checkpoints", side_effect=AssertionError("No checkpoint pages")), \
                patch.object(self.store, "revision_checkpoints", side_effect=AssertionError("Use scoped SQL")):
            self.assertEqual(self.store.latest_run_checkpoint(task_id, run_id, phase), completed)

    def test_latest_run_checkpoint_is_scoped_to_task_run_and_phase(self):
        task_id, run_id, phase = self.task.task_id, self.run.run_id, "report_correction"
        other = self.new_task()
        other_run = self.store.runs(other.task_id)[0]
        sibling = self.store.create_run(task_id, TaskStep("sibling", "analyst", "review", "report"),
            allowed_tools=[], model_profile="test")
        for owner, run, value in ((task_id, run_id, "wanted"), (task_id, sibling.run_id, "sibling"),
                                  (other.task_id, other_run.run_id, "other")):
            self.store.append_checkpoint(owner, phase, {"fingerprint": value}, run_id=run)
        self.store.append_checkpoint(task_id, "other_phase", {"fingerprint": "wrong_phase"}, run_id=run_id)
        self.checkpoint(phase)
        self.assertEqual(self.store.latest_run_checkpoint(task_id, run_id, phase), {"fingerprint": "wanted"})
        self.assertEqual(self.store.latest_run_checkpoint(task_id, sibling.run_id, phase), {"fingerprint": "sibling"})
        self.assertEqual(self.store.latest_run_checkpoint(other.task_id, other_run.run_id, phase), {"fingerprint": "other"})
        self.assertIsNone(self.store.latest_run_checkpoint(other.task_id, run_id, phase))
        self.assertIsNone(self.store.latest_run_checkpoint(task_id, other_run.run_id, phase))
        self.assertIsNone(self.store.latest_run_checkpoint(task_id, run_id, "missing_phase"))
        self.fill_page(task=other)
        self.revise(task=other)
        self.assertEqual(self.store.latest_run_checkpoint(task_id, run_id, phase), {"fingerprint": "wanted"})
        self.assertIsNone(self.store.latest_run_checkpoint(other.task_id, other_run.run_id, phase))

    def test_latest_run_checkpoint_excludes_previous_revisions_after_page_limit(self):
        task_id, run_id, phase = self.task.task_id, self.run.run_id, "report_correction"
        self.store.append_checkpoint(task_id, phase, {"status": "completed"}, run_id=run_id)
        self.fill_page()
        self.revise()
        self.assertIsNone(self.store.latest_run_checkpoint(task_id, run_id, phase))
        self.fill_page()
        state = {"fingerprint": "new", "status": "started"}
        self.store.append_checkpoint(task_id, phase, state, run_id=run_id, now=1)
        self.assertEqual(self.store.latest_run_checkpoint(task_id, run_id, phase), state)
        self.revise()
        self.assertIsNone(self.store.latest_run_checkpoint(task_id, run_id, phase))

    def test_latest_run_checkpoint_survives_restart_without_mutation(self):
        task_id, run_id, phase = self.task.task_id, self.run.run_id, "report_correction"
        self.revise()
        self.fill_page()
        for status in ("started", "failed", "completed"):
            with self.subTest(status=status):
                state = {"fingerprint": "stable", "status": status, "original": {"summary": "original"},
                         "corrected": None if status != "completed" else {"summary": "corrected"}}
                self.store.append_checkpoint(task_id, phase, state, run_id=run_id)
                self.reopen()
                changed = Mock()
                self.store.set_change_listener(changed)
                with (
                    patch.object(
                        self.store._connection,
                        "execute",
                        wraps=self.store._connection.execute,
                    ) as queries,
                    patch.object(
                        self.store._connection,
                        "cursor",
                        side_effect=AssertionError("Read path opened a transaction"),
                    ),
                ):
                    self.assertEqual(
                        self.store.latest_run_checkpoint(task_id, run_id, phase), state
                    )
                self.assertEqual(queries.call_count, 1)
                self.assertTrue(
                    queries.call_args.args[0].lstrip().upper().startswith("SELECT")
                )
                changed.assert_not_called()
