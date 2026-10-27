from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import nonebot

nonebot.init()

from src.plugins.ai_chat.agent import ContextPacket
from src.plugins.ai_chat.agent.background import SubAgentDispatcher
from src.plugins.ai_chat.agent.control import JobFence, LeaseLost, active_job_fence
from src.plugins.ai_chat.agent.execution import EntryDecision
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.model_catalog import ModelCatalog
from src.plugins.ai_chat.quota import UsageStore
from src.plugins.ai_chat.routines import RoutineLeaseLost, RoutineStore
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from src.plugins.ai_chat.subagents import SubAgentCoordinator, SubAgentStore, TaskStep
from tests.postgres_fixture import postgres_for_test
from tests.test_subagent_v2 import decision, profile


class RoutineChildrenTests(unittest.TestCase):
    def setUp(self):
        self.db = postgres_for_test(self)
        self.jobs = self.owned(DurableJobStore(self.db))
        self.outbox = self.owned(DeliveryStore(self.db, recover_interrupted=False))
        self.usage = self.owned(UsageStore(self.db))
        self.store = self.new_store()
        self.children = self.owned(SubAgentStore(self.db))
        self.scope = ConversationScope("onebot-v11", "group", "101", "201", "301")
        self.routine = self.store.set(self.scope, name="report", prompt="Build a report",
            cron="* * * * *", now=int(time.time()) - 65)
        self.store.materialize_due()
        self.job = self.jobs.claim_due("parent")[0]
        self.run = self.store.begin_attempt(self.job)
        self.packet = ContextPacket(self.scope.key, "group:101:user:201", 201, -1, "Build a report")
        self.entry = EntryDecision.parse(decision("workflow"))
        selected = profile("qwen-local")
        self.coordinator = SubAgentCoordinator(self.children,
            ModelCatalog({selected.name: selected}, default_profile=selected.name), logger=Mock())
        self.dispatcher = SubAgentDispatcher(SimpleNamespace(context=SimpleNamespace(
            subagent_store=self.children, job_store=self.jobs, subagent_coordinator=self.coordinator,
            logger=Mock())))

    def owned(self, value):
        self.addCleanup(value.close)
        return value

    def new_store(self):
        return self.owned(RoutineStore(self.db, jobs=self.jobs, outbox=self.outbox, usage=self.usage))

    def prepare(self, *, store=None, job=None, attempt_id=None, **changes):
        args = dict(subagents=self.children, intent={"tool": "run_subagents", "goal": "Build a report"},
            packet=self.packet, decision=self.entry, dispatch={"bot_id": "301", "profile": "qwen-local"},
            max_parallelism=2, max_steps=4)
        args.update(changes)
        return (store or self.store).prepare_child(job or self.job, attempt_id or self.run.attempt_id, **args)

    def counts(self):
        return tuple(self.store._connection.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
            for table in ("subagent_tasks", "subagent_controls", "subagent_checkpoints", "routine_children"))

    def test_notifications_only_see_committed_parent_and_complete_child(self):
        observed = []
        self.children.set_change_listener(lambda task_id: observed.append((
            self.children.routine_parent(task_id)["run_id"],
            self.children.dispatchable_tasks(), len(self.children.checkpoints(task_id)))))
        task = self.prepare()
        self.assertEqual(observed, [(self.run.run_id, [], 1)])
        self.assertEqual(self.children.control(task.task_id)["dispatch"]["deadline"], self.run.deadline_at)
        self.assertEqual(self.counts(), (1, 1, 1, 1))

    def test_link_failure_rolls_back_task_checkpoint_and_envelope(self):
        prepare = self.children.prepare_task
        def fail(*args, **kwargs):
            prepare(*args, **kwargs)
            raise RuntimeError("cannot store child ownership")
        observer = Mock()
        self.children.set_change_listener(observer)
        with patch.object(self.children, "prepare_task", side_effect=fail), self.assertRaises(RuntimeError):
            self.prepare()
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        observer.assert_not_called()

    def test_reopened_store_and_new_attempt_reuse_exact_child_checkpoint(self):
        first = self.prepare()
        self.jobs.defer(self.job, delay_seconds=0)
        next_job = self.jobs.claim_due("replacement")[0]
        reopened = self.new_store()
        next_run = reopened.begin_attempt(next_job)
        second = self.prepare(store=reopened, job=next_job, attempt_id=next_run.attempt_id,
            packet=replace(self.packet, supporting_context="new context must not overwrite saved context"))
        self.assertEqual(first.task_id, second.task_id)
        self.assertEqual(self.children.checkpoints(first.task_id)[0]["state"]["context_packet"], self.packet.as_payload())
        self.assertEqual(self.counts(), (1, 1, 1, 1))

    def test_concurrent_prepare_across_store_connections_is_idempotent(self):
        stores = [self.new_store() for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            tasks = list(pool.map(lambda store: self.prepare(store=store), stores))
        self.assertEqual(len({task.task_id for task in tasks}), 1)
        self.assertEqual(self.counts(), (1, 1, 1, 1))

    def test_scope_creator_and_bot_cannot_be_changed_even_on_reuse(self):
        self.prepare()
        for changes in ({"packet": replace(self.packet, scope_key="onebot-v11:group:999")},
                        {"packet": replace(self.packet, requester_user_id=999)},
                        {"dispatch": {"bot_id": "999"}}):
            with self.subTest(changes=changes), self.assertRaises(PermissionError):
                self.prepare(**changes)
        self.assertEqual(self.counts(), (1, 1, 1, 1))

    def test_old_attempt_and_lease_cannot_create_children(self):
        next_run = self.store.begin_attempt(self.job)
        with self.assertRaises(RoutineLeaseLost):
            self.prepare()
        self.jobs.defer(self.job, delay_seconds=0)
        with self.assertRaises(RoutineLeaseLost):
            self.prepare(attempt_id=next_run.attempt_id)
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_expired_deadline_blocks_children(self):
        self.store._connection.execute("UPDATE routine_runs SET deadline_at=? WHERE run_id=?",
            (int(time.time()) - 1, self.run.run_id))
        with self.assertRaises(RoutineLeaseLost):
            self.prepare()
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_standalone_dispatcher_never_executes_or_finalizes_owned_children(self):
        task = self.prepare()
        self.assertIsNone(self.dispatcher.enqueue(task.task_id))
        job = SimpleNamespace(payload={"task_id": task.task_id})
        self.assertEqual(asyncio.run(self.dispatcher.execute(job)), {"state": "owned_by_routine"})
        self.children.set_task_state(task.task_id, "completed", result={"answer": "private child result"})
        self.assertEqual(self.children.finalizable_tasks(), [])
        self.assertIsNone(self.dispatcher.enqueue_final(task.task_id))
        self.assertFalse(self.outbox.recent())

    def test_child_resume_and_revise_require_the_actual_parent_job_fence(self):
        task = self.prepare()
        with self.assertRaises(PermissionError):
            self.children.require_routine_owner(task.task_id)
        with self.assertRaises(PermissionError):
            self.coordinator.revise(task.task_id, scope_key=self.scope.key, requester_user_id=201,
                instruction="new work", step_keys=[], expected_version=1)
        token = active_job_fence.set(JobFence(self.job.job_id, self.job.lease_owner, self.job.attempts, lambda: True))
        try:
            self.children.require_routine_owner(task.task_id)
            self.store.cancel(self.scope, self.routine.routine_id, expected_version=1)
            with self.assertRaises(LeaseLost):
                self.children.require_routine_owner(task.task_id)
        finally:
            active_job_fence.reset(token)

    def test_parent_cancellation_settles_children_without_repeating_or_deleting_work(self):
        task = self.prepare()
        done = self.children.create_run(task.task_id, TaskStep("done", "analyst", "done", "report"),
                                        allowed_tools=[], model_profile="qwen-local")
        waiting = self.children.create_run(task.task_id, TaskStep("pending", "analyst", "wait", "report"),
                                           allowed_tools=[], model_profile="qwen-local")
        self.children.finish_run(done.run_id, "succeeded", result={"summary": "saved result"})
        self.children.finish_run(waiting.run_id, "waiting_external", result={"remote_job": "job-1"})
        self.children.set_task_state(task.task_id, "waiting_external", result={"draft": "retained"})
        self.assertEqual(self.children.reconcile_routine_parents(), 0)
        self.store.cancel(self.scope, self.routine.routine_id, expected_version=1)
        self.assertEqual(self.children.reconcile_routine_parents(), 1)
        self.assertEqual(self.children.reconcile_routine_parents(), 0)
        finished = self.children.get(task.task_id)
        self.assertEqual(finished.status, "cancelled")
        self.assertEqual(finished.result, {"draft": "retained"})
        self.assertIn("远程操作需另行核对", finished.last_error)
        runs = self.children.runs(task.task_id)
        self.assertEqual([run.status for run in runs], ["succeeded", "cancelled"])
        self.assertEqual(runs[1].result, {"remote_job": "job-1"})
        self.children.interrupt_task(task.task_id)
        self.assertEqual(self.children.get(task.task_id).status, "cancelled")

    def test_expired_parent_only_settles_unfinished_children(self):
        completed = self.prepare(intent={"goal": "completed"})
        unfinished = self.prepare(intent={"goal": "unfinished"})
        self.children.set_task_state(completed.task_id, "completed", result={"answer": "done"})
        self.children.set_task_state(unfinished.task_id, "interrupted")
        self.store._connection.execute("UPDATE routine_runs SET deadline_at=? WHERE run_id=?",
            (int(time.time()) - 1, self.run.run_id))
        self.store.begin_attempt(self.job)
        self.assertEqual(self.children.reconcile_routine_parents(), 1)
        self.assertEqual(self.children.get(completed.task_id).status, "completed")
        self.assertEqual(self.children.get(unfinished.task_id).status, "failed")
        self.assertFalse(self.children.finalizable_tasks())
        self.assertFalse(self.outbox.recent())


if __name__ == "__main__":
    unittest.main()
