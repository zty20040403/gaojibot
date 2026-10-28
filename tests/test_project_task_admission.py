"""Real PG, CAS and private file RPC; kernel/containers/QQ remain fixtures."""

import asyncio
from dataclasses import replace
import hashlib
import json
import time
import unittest
from unittest.mock import Mock, patch

import nonebot

nonebot.init()

from src.plugins.ai_chat.agent import ContextPacket
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.project_executor import ProjectLifecycleExecutor
from src.plugins.ai_chat.project_lifecycle import ProjectLifecycle, transition_plan
from src.plugins.ai_chat.project_task_admission import ProjectTaskAdmission
from src.plugins.ai_chat.project_turn_saves import ProjectTurnSaves
from src.plugins.ai_chat.projects import ProjectConflict
from src.plugins.ai_chat.subagents import SubAgentStore
from src.plugins.ai_chat.turn_journal import TurnJournal
from src.plugins.ai_chat.workers.durable_jobs import JobDeferred
from src.plugins.ai_chat.project_task_reconciliation import ProjectTaskReleases
from tests import test_project_restore as fixture
from tests import test_project_vm_executor as vm_fixture
from tests import test_project_autosave as autosave_fixture
from tests.project_turn_fixture import start_project_turn


class ProjectTaskAdmissionTests(unittest.IsolatedAsyncioTestCase):
    wake = fixture.OciRestoreTests.wake
    claim = fixture.OciRestoreTests.claim
    save = fixture.OciRestoreTests.save
    request = fixture.OciRestoreTests.request

    async def asyncSetUp(self):
        await fixture.OciRestoreTests.asyncSetUp(self)
        self.setup_admission()

    def setup_admission(self):
        self.tasks = SubAgentStore(self.projects.database)
        self.ledger = MessageLedger(self.projects.database)
        self.journal = TurnJournal(self.projects.database)
        for store in (self.tasks, self.ledger, self.journal):
            self.addCleanup(store.close)
        self.turn = start_project_turn(self.ledger, self.journal, self.scope)
        self.packet = ContextPacket(self.scope.key, self.scope.key + ":user:201", 201,
                                    self.turn.trigger_canonical_message_id, "Update the website",
                                    memory_context="private actor memory")
        self.dispatch = {"bot_id": "301", "event": {"user_id": 201, "message_id": 5}, "profile": "fixture"}
        self.key = hashlib.sha256(b"prepare-task").hexdigest()
        self.admission = ProjectTaskAdmission(self.lifecycle, self.tasks)
        self.lifecycle.task_admission = self.admission
        self.projects.open(self.access, self.pid)

    def prepare(self, **changes):
        options = dict(request_key=self.key, turn_id=self.turn.turn_id, packet=self.packet,
                       decision=None, dispatch=self.dispatch, max_parallelism=2, max_steps=4,
                       timeout_seconds=600, preflight=self.backend.preflight)
        options.update(changes)
        return self.admission.request(self.access, **options)

    def count(self, table):
        return self.sql.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def project(self):
        return self.projects.get(self.access, self.pid)

    async def test_unsaved_active_files_become_task_baseline_and_lock_in_one_commit(self):
        root = await self.wake()
        (root / "index.html").write_bytes(b"first")
        self.save()
        await self.executor(self.claim())
        (root / "index.html").write_bytes(b"latest unsaved edit")
        run = self.prepare()
        self.assertFalse(run["task_bound"])
        self.assertEqual(self.count("subagent_tasks"), 0)
        self.assertEqual(self.project()["lifecycle_job_id"], run["job_id"])
        with self.assertRaises(ProjectConflict):
            self.projects.update(self.access, self.pid, expected_version=self.project()["version"], title="racing edit")
        result = await self.executor(self.claim())
        task = self.tasks.get(result["task_id"])
        self.assertTrue(result["task_bound"])
        self.assertEqual((task.project_id, task.project_base_version, task.status), (self.pid, 2, "queued"))
        self.assertEqual(self.project()["locked_task_id"], task.task_id)
        self.assertIsNone(self.project()["lifecycle_job_id"])
        self.assertEqual(self.admission.bindings.read_baseline_file(self.access, task.task_id, "index.html")["content"],
                         b"latest unsaved edit")
        self.assertEqual(self.projects.read_file(self.access, self.pid, 1, "index.html"), b"first")
        self.assertIn(task.task_id, self.tasks.dispatchable_tasks())
        self.assertEqual(self.admission.status(self.access, self.key)["task_id"], task.task_id)

    async def test_new_project_gets_real_empty_snapshot_before_task_is_visible(self):
        self.prepare()
        self.assertEqual(self.count("subagent_tasks"), 0)
        result = await self.executor(self.claim())
        baseline = self.admission.bindings.baseline(self.access, result["task_id"])
        self.assertEqual(baseline["manifest"]["files"], [])
        self.assertEqual(self.project()["workspace_generation"], 1)
        self.assertEqual(self.project()["status"], "stopped")
        self.assertEqual(self.docker.serial, 1)

    async def test_request_replay_and_restart_ignore_changed_focus_and_finished_turn(self):
        root = await self.wake()
        (root / "note.txt").write_bytes(b"original project")
        run = self.prepare()
        other = self.projects.create(self.access, "Other")["project_id"]
        self.projects.open(self.access, other)
        self.journal.finish_turn(self.turn.turn_id, status="succeeded")
        replacement = ProjectLifecycle(self.projects, self.jobs)
        self.admission = ProjectTaskAdmission(replacement, self.tasks)
        replacement.task_admission = self.admission
        self.assertEqual(self.prepare()["job_id"], run["job_id"])
        executor = ProjectLifecycleExecutor(replacement, {"oci": self.backend}, resolve_access=lambda *_: self.access)
        result = await executor(self.claim())
        self.assertEqual(self.prepare()["task_id"], result["task_id"])
        self.assertEqual(self.count("subagent_tasks"), 1)
        self.assertEqual(self.projects.current(self.access)["project_id"], other)
        with self.assertRaises(ProjectConflict):
            self.prepare(packet=replace(self.packet, objective="changed request"))

    async def test_post_snapshot_transaction_failure_never_exposes_unlocked_workspace(self):
        root = await self.wake()
        (root / "note.txt").write_bytes(b"survives failed commit")
        self.prepare()
        job = self.claim()
        append = self.tasks.append_checkpoint

        def crash(task_id, phase, *args, **kwargs):
            append(task_id, phase, *args, **kwargs)
            if phase == "project_bound":
                raise RuntimeError("fixture commit interrupted")

        with patch.object(self.tasks, "append_checkpoint", side_effect=crash):
            with self.assertRaises(RuntimeError):
                await self.executor(job)
        self.assertEqual(self.count("subagent_tasks"), 0)
        self.assertEqual(self.count("project_task_bindings"), 0)
        self.assertEqual(self.project()["lifecycle_job_id"], job.job_id)
        self.assertIsNone(self.project()["locked_task_id"])
        self.assertEqual(self.jobs.get(job.job_id).status, "running")
        result = await self.executor(self.claim(now=int(time.time()) + 61))
        self.assertEqual(self.count("subagent_tasks"), 1)
        self.assertEqual(self.count("project_versions"), 1)
        self.assertEqual(self.project()["locked_task_id"], result["task_id"])

    async def test_concurrent_duplicate_requests_enqueue_one_preparation(self):
        runs = await asyncio.gather(*(asyncio.to_thread(self.prepare) for _ in range(4)))
        self.assertEqual(len({run["job_id"] for run in runs}), 1)
        self.assertEqual(self.count("durable_jobs"), 1)
        await self.executor(self.claim())
        self.assertEqual(self.count("subagent_tasks"), 1)

    async def test_current_turn_pending_save_joins_preparation_and_has_truthful_receipt(self):
        root = await self.wake()
        (root / "note.txt").write_bytes(b"current turn mutation")
        now = int(time.time())
        self.sql.execute("""INSERT INTO project_turn_saves
            (project_id,turn_id,actor_principal_id,created_at,updated_at) VALUES (?,?,?,?,?)""",
            (self.pid, self.turn.turn_id, self.access.principal_id, now, now))
        saves = ProjectTurnSaves(self.projects)
        run = self.prepare()
        before = saves.status(self.access, self.pid)[0]
        self.assertEqual(before["status"], "queued")
        self.assertFalse(before["saved"])
        self.assertEqual(self.sql.execute("SELECT job_id FROM project_turn_saves").fetchone()[0], run["job_id"])
        await self.executor(self.claim())
        self.assertTrue(saves.status(self.access, self.pid)[0]["saved"])
        self.assertEqual(saves.status(self.access, self.pid)[0]["version"], "v1")

    async def test_other_running_turn_save_is_not_silently_claimed(self):
        other = start_project_turn(self.ledger, self.journal, self.scope, message_id=6)
        now = int(time.time())
        self.sql.execute("""INSERT INTO project_turn_saves
            (project_id,turn_id,actor_principal_id,created_at,updated_at) VALUES (?,?,?,?,?)""",
            (self.pid, other.turn_id, self.access.principal_id, now, now))
        with self.assertRaisesRegex(ProjectConflict, "Another project turn"):
            self.prepare()
        self.assertIsNone(self.project()["lifecycle_job_id"])
        self.assertEqual(self.count("durable_jobs"), 0)

    async def test_missing_adapter_or_revoked_identity_prevents_effects_and_binding(self):
        run = self.prepare()
        job = self.claim()
        self.lifecycle.task_admission = None
        with self.assertRaises(JobDeferred):
            await self.executor(job)
        self.assertEqual(self.docker.serial, 0)
        self.assertEqual(self.project()["lifecycle_job_id"], run["job_id"])
        self.lifecycle.task_admission = self.admission
        self.executor.resolve_access = lambda *_: replace(self.access, member=False)
        with self.assertRaises(PermissionError):
            await self.executor(job)
        self.assertEqual(self.docker.serial, 0)
        self.assertEqual(self.count("subagent_tasks"), 0)

    async def test_preflight_and_context_validation_do_not_leave_partial_jobs(self):
        for options in (
            {"packet": replace(self.packet, requester_user_id=202)},
            {"packet": replace(self.packet, trigger_message_id=999999)},
            {"dispatch": {**self.dispatch, "bot_id": "302"}},
            {"dispatch": {**self.dispatch, "deadline": 99999999999}},
            {"timeout_seconds": 0}, {"max_steps": 0}, {"max_parallelism": True},
            {"preflight": Mock(side_effect=ValueError("unsupported backend"))},
        ):
            with self.subTest(options=options), self.assertRaises((ValueError, PermissionError)):
                self.prepare(**options)
        self.assertEqual(self.count("durable_jobs"), 0)
        self.assertIsNone(self.project()["lifecycle_job_id"])
        with self.assertRaises(ValueError):
            self.lifecycle.request(self.access, self.pid, "task_prepare", expected_version=1, request_key="not-host")

    async def test_public_journal_never_exposes_personal_context_or_dispatch(self):
        self.prepare()
        output = json.dumps(self.lifecycle.journal(self.access, self.pid))
        self.assertNotIn("private actor memory", output)
        self.assertNotIn("task_admission", output)
        self.assertNotIn('"dispatch"', output)
        self.assertNotIn('"reservation"', output)

    async def test_fresh_stopped_hibernated_archived_plans_preserve_restore_before_save(self):
        for backend in ("oci", "vm"):
            for state, generation in (("active", 1), ("stopped", 1), ("stopped", 0), ("hibernated", 1), ("archived", 1)):
                with self.subTest(backend=backend, state=state, generation=generation):
                    project = {**self.project(), "backend": backend, "status": state, "workspace_generation": generation}
                    _, plan = transition_plan(project, "task_prepare", {"version_no": 1, "manifest_sha256": "a" * 64},
                                              rebuild={"command": "npm ci"})
                    steps = plan["steps"]
                    self.assertEqual(steps[-2:], ["release_workspace", "verify_stopped"])
                    self.assertEqual(steps.count("reserve_workspace"), 1)
                    self.assertEqual(steps.count("snapshot_workspace"), 1)
                    if state == "archived" or generation == 0:
                        self.assertLess(steps.index("restore_version"), steps.index("snapshot_workspace"))
                    if state in {"hibernated", "archived"} or generation == 0:
                        self.assertLess(steps.index("rebuild_dependencies"), steps.index("snapshot_workspace"))

    async def test_saved_stopped_workspace_uses_same_generation_without_starting_container(self):
        root = await self.wake()
        (root / "note.txt").write_bytes(b"stopped source")
        self.request("stop")
        await self.executor(self.claim())
        before = len(self.docker.commands)
        self.prepare()
        await self.executor(self.claim())
        self.assertFalse(any(c[0] == "start" for c in self.docker.commands[before:]))
        self.assertEqual(self.project()["workspace_generation"], 1)
        self.assertEqual(self.count("project_versions"), 1)

    async def test_archived_project_restores_retained_bytes_before_admission(self):
        root = await self.wake()
        (root / "note.txt").write_bytes(b"archived source")
        for action in ("stop", "hibernate", "archive"):
            self.request(action)
            await self.executor(self.claim())
        self.prepare()
        result = await self.executor(self.claim())
        self.assertEqual(self.project()["workspace_generation"], 2)
        self.assertEqual(self.admission.bindings.read_baseline_file(self.access, result["task_id"], "note.txt")["content"],
                         b"archived source")

    async def test_deadline_starts_at_binding_not_at_initial_request(self):
        self.prepare()
        job = self.claim()
        issued = int(time.time())
        while (step := self.lifecycle.next_step(job)) is not None:
            evidence = await self.backend.execute(job, step, self.access)
            self.lifecycle.acknowledge(job, step, evidence)
        self.sql.execute("UPDATE durable_jobs SET lease_until=? WHERE job_id=?", (issued + 7200, job.job_id))
        result = self.lifecycle.finish(job, access=self.access, now=issued + 3600)
        self.assertEqual(self.tasks.control(result["task_id"])["dispatch"]["deadline"], issued + 3600 + 600 + 900)

    async def test_tampered_envelope_or_missing_access_cannot_bind_after_physical_save(self):
        self.prepare()
        job = self.claim()
        while (step := self.lifecycle.next_step(job)) is not None:
            evidence = await self.backend.execute(job, step, self.access)
            self.lifecycle.acknowledge(job, step, evidence)
        with self.assertRaises(PermissionError):
            self.lifecycle.finish(job)
        self.sql.execute("""UPDATE project_lifecycle_runs SET plan=jsonb_set(plan,
            '{task_admission,envelope,packet,objective}', '\"substituted\"'::jsonb) WHERE job_id=?""", (job.job_id,))
        with self.assertRaisesRegex(ProjectConflict, "envelope"):
            self.lifecycle.finish(job, access=self.access)
        self.assertEqual(self.count("subagent_tasks"), 0)
        self.assertEqual(self.project()["lifecycle_job_id"], job.job_id)

    async def test_lease_expiry_during_binding_rolls_back_task_and_lock_handoff(self):
        self.prepare()
        job = self.claim()
        original = self.admission._bind_saved

        def expire(cursor, *args, **kwargs):
            result = original(cursor, *args, **kwargs)
            cursor.execute("UPDATE durable_jobs SET lease_until=0 WHERE job_id=?", (job.job_id,))
            return result

        with patch.object(self.admission, "_bind_saved", side_effect=expire):
            with self.assertRaisesRegex(ProjectConflict, "lease was lost"):
                await self.executor(job)
        self.assertEqual(self.count("subagent_tasks"), 0)
        self.assertEqual(self.project()["lifecycle_job_id"], job.job_id)
        await self.executor(job)
        self.assertEqual(self.count("subagent_tasks"), 1)


class VmTaskPreparationTests(vm_fixture.VmExecutorFixture, unittest.IsolatedAsyncioTestCase):
    setup_admission = ProjectTaskAdmissionTests.setup_admission
    prepare = ProjectTaskAdmissionTests.prepare

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.setup_admission()

    async def test_fresh_vm_has_verified_empty_baseline(self):
        self.prepare()
        result = await self.executor(self.claim())
        self.assertEqual(self.project()["status"], "stopped")
        self.assertEqual(self.admission.bindings.baseline(self.access, result["task_id"])["manifest"]["files"], [])

    async def test_vm_prepare_cannot_release_or_verify_before_saving(self):
        self.prepare()
        _, run = self.lifecycle.context(self.claim())
        plan = run["plan"]
        for steps in (
            [step for step in plan["steps"] if step != "snapshot_workspace"],
            plan["steps"][:-2] + ["verify_stopped", "release_workspace"],
            plan["steps"][:-2] + ["release_workspace", "verify_active"],
        ):
            with self.subTest(steps=steps), self.assertRaises(ValueError):
                self.backend.preflight({**plan, "steps": steps})

    async def test_hibernated_vm_rebuilds_dependencies_and_saves_before_binding(self):
        await self.transition("wake")
        root = self.guests[self.project()["sandbox_id"]].workspace
        (root / "index.html").write_bytes(b"retained VM website")
        (root / "package.json").write_bytes(b'{"name":"fixture"}')
        (root / "package-lock.json").write_bytes(b'{"lockfileVersion":3}')
        (root / "node_modules/pkg").mkdir(parents=True)
        (root / "node_modules/pkg/data").write_bytes(b"dependency")
        self.projects.update(self.access, self.pid, expected_version=self.project()["version"], rebuild_command="npm ci")
        await self.transition("stop")
        await self.transition("hibernate")
        self.prepare()
        result = await self.executor(self.claim())
        self.assertEqual(self.project()["status"], "stopped")
        self.assertEqual(self.admission.bindings.read_baseline_file(self.access, result["task_id"], "index.html")["content"],
                         b"retained VM website")


class TaskPreparationAutosaveTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = autosave_fixture.ProjectAutosaveTests.asyncSetUp
    call = autosave_fixture.ProjectAutosaveTests.call
    write = autosave_fixture.ProjectAutosaveTests.write
    saves = autosave_fixture.ProjectAutosaveTests.saves
    finish = autosave_fixture.ProjectAutosaveTests.finish
    scan = autosave_fixture.ProjectAutosaveTests.scan
    run_saves = autosave_fixture.ProjectAutosaveTests.run_saves

    async def test_actual_tool_writes_save_then_resume_tracking_later_same_turn_writes(self):
        await self.write("before delegation")
        runtime = self.runtime
        tasks = SubAgentStore(runtime.store.database)
        self.addCleanup(tasks.close)
        admission = ProjectTaskAdmission(runtime.lifecycle, tasks)
        runtime.lifecycle.task_admission = admission
        run = admission.request(self.access,
            request_key=hashlib.sha256(b"real-tool-turn").hexdigest(), turn_id=self.turn.turn_id,
            packet=ContextPacket(self.scope.key, self.scope.key + ":user:201", 201,
                                 self.turn.trigger_canonical_message_id, "Continue editing"),
            decision=None, dispatch={"bot_id": "301"}, max_parallelism=2, max_steps=4,
            timeout_seconds=600, preflight=runtime.backends["oci"].preflight)
        await self.worker.run_once()
        self.assertTrue(self.saves()[0]["saved"])
        result = self.jobs.get(run["job_id"]).result
        task_id = result["task_id"]
        tasks.set_task_state(task_id, "failed", error="fixture stopped before any worker effect")
        ProjectTaskReleases(admission.bindings).release_failed(self.access, task_id)
        project = runtime.store.get(self.access, self.pid)
        runtime.lifecycle.request(self.access, self.pid, "wake", expected_version=project["version"], request_key="continue-turn")
        await self.worker.run_once()
        await self.write("after delegation")
        self.assertFalse(self.saves()[0]["saved"])
        self.assertEqual(self.saves()[0]["status"], "pending")
        self.finish()
        await self.scan()
        await self.run_saves()
        self.assertTrue(self.saves()[0]["saved"])
        self.assertEqual(runtime.store.read_file(self.access, self.pid, 2, "report.txt"), b"after delegation")
