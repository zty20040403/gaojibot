"""Real PostgreSQL project/task ownership; no production, models or QQ."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import threading
import unittest
from unittest.mock import Mock, patch

import nonebot

nonebot.init()

from src.plugins.ai_chat.agent import ContextPacket
from src.plugins.ai_chat.project_lifecycle import ProjectLifecycle
from src.plugins.ai_chat.project_tasks import ProjectTaskStore
from src.plugins.ai_chat.projects import ProjectConflict
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from src.plugins.ai_chat.subagents import SubAgentStore
from tests import test_project_store as store_fixture
from tests import test_project_file_executor as file_fixture


def key(value):
    return hashlib.sha256(value.encode()).hexdigest()


def task_packet(access):
    return ContextPacket(access.scope.key, access.scope.key + ":user:201", 201, 401, "Update the project")


class ProjectTaskTests(unittest.TestCase):
    owned = store_fixture.ProjectStoreTests.owned
    new_store = store_fixture.ProjectStoreTests.new_store
    create = store_fixture.ProjectStoreTests.create
    snapshot = store_fixture.ProjectStoreTests.snapshot

    def setUp(self):
        store_fixture.ProjectStoreTests.setUp(self)
        self.tasks = self.owned(SubAgentStore(self.db))
        self.binding = ProjectTaskStore(self.store, self.tasks)
        self.pid = self.create()
        self.snapshot(self.pid)
        self.store.open(self.access, self.pid)
        self.packet = task_packet(self.access)
        self.dispatch = {"bot_id": "301", "event": {"user_id": 201}, "profile": "fixture"}

    def prepare(self, *, binding=None, access=None, **changes):
        arguments = {"expected_version": self.store.get(self.access, self.pid)["version"],
                     "base_version": 1, "request_key": key("first"), "packet": self.packet,
                     "decision": None, "dispatch": self.dispatch, "max_parallelism": 2, "max_steps": 4}
        arguments.update(changes)
        return (binding or self.binding).prepare(access or self.access, self.pid, **arguments)

    def count(self, table):
        return self.sql.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def test_task_lock_baseline_and_recovery_envelope_commit_together(self):
        seen = []
        def observer(tid):
            task = self.tasks.get(tid)
            seen.append((task.project_id, task.project_base_version, task.status,
                         self.store.get(self.access, self.pid)["locked_task_id"],
                         len(self.tasks.checkpoints(tid))))
        self.tasks.set_change_listener(observer)
        task = self.prepare()
        self.assertEqual(seen, [(self.pid, 1, "queued", task.task_id, 2)])
        self.assertEqual(self.tasks.control(task.task_id)["dispatch"], self.dispatch)
        self.assertEqual(self.binding.baseline(self.access, task.task_id)["version_no"], 1)
        project = self.store.get(self.access, self.pid)
        with self.assertRaisesRegex(ProjectConflict, "locked by task"):
            self.store.update(self.access, self.pid, expected_version=project["version"], title="no")
        with self.assertRaises(ProjectConflict):
            self.snapshot(self.pid, b"overwritten")
        lifecycle = ProjectLifecycle(self.store, self.owned(DurableJobStore(self.db)))
        with self.assertRaises(ProjectConflict):
            lifecycle.request(self.access, self.pid, "wake", expected_version=project["version"], request_key="wake")

    def test_checkpoint_failure_leaves_neither_task_nor_project_lock(self):
        observer = Mock()
        self.tasks.set_change_listener(observer)
        before = self.store.get(self.access, self.pid)
        append = self.tasks.append_checkpoint
        def crash(task_id, phase, *args, **kwargs):
            append(task_id, phase, *args, **kwargs)
            if phase == "project_bound":
                raise RuntimeError("lost transaction before commit")
        with patch.object(self.tasks, "append_checkpoint", side_effect=crash), self.assertRaises(RuntimeError):
            self.prepare()
        for table in ("subagent_tasks", "project_task_bindings", "subagent_controls", "subagent_checkpoints", "subagent_events"):
            self.assertEqual(self.count(table), 0, table)
        self.assertEqual(self.store.get(self.access, self.pid), before)
        observer.assert_not_called()

    def test_replay_after_focus_switch_expiry_and_store_reopen_returns_original(self):
        task = self.prepare()
        other = self.create()
        self.store.open(self.access, other)
        reopened = ProjectTaskStore(self.new_store(), self.owned(SubAgentStore(self.db)))
        repeated = self.prepare(binding=reopened, now=self.now + 90000)
        self.assertEqual(repeated.task_id, task.task_id)
        self.assertEqual(self.count("subagent_tasks"), 1)
        self.assertEqual(reopened.read_baseline_file(self.access, task.task_id, "index.html")["content"], b"first")
        with self.assertRaises(ProjectConflict):
            self.prepare(binding=reopened, packet=replace(self.packet, objective="different intent"))
        with self.assertRaises(ProjectConflict):
            self.prepare(binding=reopened, dispatch={**self.dispatch, "profile": "different"})

    def test_new_task_rejects_expired_or_changed_focus_and_stale_revision(self):
        with self.assertRaises(ProjectConflict):
            self.prepare(now=self.now + 90000)
        with self.assertRaises(ProjectConflict):
            self.prepare(expected_version=1)
        self.store.open(self.access, self.create())
        with self.assertRaises(ProjectConflict):
            self.prepare()
        self.assertEqual(self.count("subagent_tasks"), 0)

    def test_no_saved_baseline_or_nonlatest_version_is_not_silently_substituted(self):
        self.snapshot(self.pid, b"second")
        with self.assertRaises(ProjectConflict):
            self.prepare()
        self.assertEqual(self.prepare(base_version=2).project_base_version, 2)

    def test_viewer_departure_other_group_other_bot_and_spoofed_packet_are_denied(self):
        for access in (self.b, replace(self.access, member=False),
                       replace(self.access, scope=replace(self.scope, native_conversation_id="102")),
                       replace(self.access, scope=replace(self.scope, bot_native_user_id="302"))):
            with self.subTest(access=access), self.assertRaises(PermissionError):
                self.prepare(access=access)
        for packet in (replace(self.packet, requester_user_id=202), replace(self.packet, scope_key="group:other")):
            with self.assertRaises(PermissionError):
                self.prepare(packet=packet)
        self.assertEqual(self.count("subagent_tasks"), 0)
        task = self.prepare()
        for access in (self.b, replace(self.access, member=False),
                       replace(self.b, admin=True),
                       replace(self.access, scope=replace(self.scope, bot_native_user_id="302"))):
            with self.subTest(access=access), self.assertRaises(PermissionError):
                self.binding.read_baseline_file(access, task.task_id, "index.html")

    def test_editor_revocation_blocks_replay_and_read(self):
        version = self.store.get(self.access, self.pid)["version"]
        self.store.set_member(self.access, self.pid, self.other, "editor", expected_version=version)
        self.store.open(self.b, self.pid)
        packet = replace(self.packet, requester_user_id=202)
        task = self.prepare(access=self.b, packet=packet, dispatch={**self.dispatch, "event": {"user_id": 202}})
        self.sql.execute("DELETE FROM project_members WHERE project_id=? AND principal_id=?", (self.pid, self.other))
        with self.assertRaises(PermissionError):
            self.prepare(access=self.b, packet=packet, dispatch={**self.dispatch, "event": {"user_id": 202}})
        with self.assertRaises(PermissionError):
            self.binding.baseline(self.b, task.task_id)

    def test_running_or_unverified_workspace_cannot_be_admitted_from_old_snapshot(self):
        for status, generation in (("active", 1), ("stopped", 1), ("hibernated", 1), ("archived", 1)):
            self.sql.execute("UPDATE projects SET status=?,workspace_generation=? WHERE project_id=?", (status, generation, self.pid))
            with self.subTest(status=status), self.assertRaises(ProjectConflict):
                self.prepare()
        self.assertEqual(self.count("subagent_tasks"), 0)

    def test_baseline_read_detects_missing_corrupt_and_unlisted_files(self):
        task = self.prepare()
        for path in ("../secret", "/workspace/index.html"):
            with self.assertRaises(ValueError):
                self.binding.read_baseline_file(self.access, task.task_id, path)
        with self.assertRaises(FileNotFoundError):
            self.binding.read_baseline_file(self.access, task.task_id, "secret")
        item = self.binding.baseline(self.access, task.task_id)["manifest"]["files"][0]
        path = self.store.blobs.path(item["sha256"])
        path.chmod(0o600)
        path.write_bytes(b"wrong")
        with self.assertRaises(ValueError):
            self.binding.read_baseline_file(self.access, task.task_id, "index.html")
        path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.binding.read_baseline_file(self.access, task.task_id, "index.html")

    def test_cancel_and_lost_generation_deny_read_without_releasing_project(self):
        task = self.prepare()
        self.sql.execute("UPDATE subagent_tasks SET cancel_requested=TRUE WHERE task_id=?", (task.task_id,))
        with self.assertRaises(ProjectConflict):
            self.binding.baseline(self.access, task.task_id)
        self.assertEqual(self.store.get(self.access, self.pid)["locked_task_id"], task.task_id)
        self.sql.execute("UPDATE subagent_tasks SET cancel_requested=FALSE WHERE task_id=?", (task.task_id,))
        self.sql.execute("UPDATE projects SET workspace_generation=1 WHERE project_id=?", (self.pid,))
        with self.assertRaises(ProjectConflict):
            self.binding.baseline(self.access, task.task_id)

    def test_failed_task_does_not_implicitly_unlock_or_erase_baseline(self):
        task = self.prepare()
        self.tasks.set_task_state(task.task_id, "failed", error="fixture")
        reopened = ProjectTaskStore(self.new_store(), self.owned(SubAgentStore(self.db)))
        self.assertEqual(self.store.get(self.access, self.pid)["locked_task_id"], task.task_id)
        with self.assertRaises(ProjectConflict):
            reopened.baseline(self.access, task.task_id)
        self.assertEqual(self.store.read_file(self.access, self.pid, 1, "index.html"), b"first")

    def test_distinct_concurrent_requests_cannot_take_the_same_project(self):
        self.race(duplicate=False)

    def test_same_concurrent_request_returns_one_task(self):
        self.race(duplicate=True)

    def race(self, *, duplicate):
        bindings = [ProjectTaskStore(self.new_store(), self.owned(SubAgentStore(self.db))) for _ in range(2)]
        revision = self.store.get(self.access, self.pid)["version"]
        barrier = threading.Barrier(2)
        def submit(i):
            barrier.wait(timeout=5)
            try:
                return self.prepare(binding=bindings[i], expected_version=revision,
                                    request_key=key("same" if duplicate else str(i))).task_id
            except ProjectConflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, range(2)))
        self.assertEqual(sum(v is not None for v in results), 2 if duplicate else 1)
        self.assertEqual(len(set(v for v in results if v is not None)), 1)
        self.assertEqual(self.count("subagent_tasks"), 1)
        self.assertEqual(self.count("project_task_bindings"), 1)


class ProjectTaskLifecycleTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = file_fixture.ProjectFileExecutorTests.asyncSetUp
    request = file_fixture.ProjectFileExecutorTests.request
    claim = file_fixture.ProjectFileExecutorTests.claim
    wake = file_fixture.ProjectFileExecutorTests.wake

    async def setup_task(self):
        self.tasks = SubAgentStore(self.projects.database)
        self.addCleanup(self.tasks.close)
        self.bindings = ProjectTaskStore(self.projects, self.tasks)
        self.projects.open(self.access, self.pid)
        root = await self.wake()
        (root / "index.html").write_bytes(b"latest workspace, not an old version")
        self.request("stop")
        await self.executor(self.claim())

    def prepare(self):
        return self.bindings.prepare(self.access, self.pid,
            expected_version=self.projects.get(self.access, self.pid)["version"], base_version=1,
            request_key=key("lifecycle"), packet=task_packet(self.access), decision=None,
            dispatch={"bot_id": "301"}, max_parallelism=2, max_steps=4)

    async def test_verified_snapshot_and_stop_supply_actual_files_to_task(self):
        await self.setup_task()
        task = self.prepare()
        self.assertEqual(self.bindings.read_baseline_file(self.access, task.task_id, "index.html")["content"],
                         b"latest workspace, not an old version")
        self.assertEqual(self.projects.get(self.access, self.pid)["locked_task_id"], task.task_id)

    async def test_changed_final_receipt_cannot_authorize_old_baseline(self):
        await self.setup_task()
        self.sql.execute("UPDATE projects SET lifecycle_fence=lifecycle_fence+1 WHERE project_id=?", (self.pid,))
        with self.assertRaises(ProjectConflict):
            self.prepare()
        self.assertIsNone(self.projects.get(self.access, self.pid)["locked_task_id"])


if __name__ == "__main__":
    unittest.main()
