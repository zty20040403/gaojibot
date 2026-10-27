"""Actual PostgreSQL scheduling/retention; simulated backend, no host mutations."""

import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import nonebot

nonebot.init()

from src.plugins.ai_chat.project_blobs import ProjectFile
from src.plugins.ai_chat.project_executor import ProjectBackendUnavailable
from src.plugins.ai_chat.project_maintenance import ProjectMaintenance
from src.plugins.ai_chat.project_policy import DAY, ProjectPolicy
from src.plugins.ai_chat.projects import ProjectConflict
from src.plugins.ai_chat.subagents import SubAgentStore
from tests import test_project_lifecycle as fixture


class ProjectMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    owned = fixture.ProjectLifecycleTests.owned
    project = fixture.ProjectLifecycleTests.project
    request = fixture.ProjectLifecycleTests.request
    claim = fixture.ProjectLifecycleTests.claim
    evidence = fixture.ProjectLifecycleTests.evidence
    complete = fixture.ProjectLifecycleTests.complete

    def setUp(self):
        fixture.ProjectLifecycleTests.setUp(self)
        self.backend = SimpleNamespace(
            maintenance_ready=AsyncMock(return_value=True), preflight=Mock()
        )
        self.maintenance = self.scheduler()

    def scheduler(self, **kwargs):
        return ProjectMaintenance(
            self.lifecycle,
            {"oci": self.backend},
            resolve_access=lambda project, actor: self.access,
            **{"enabled": True, **kwargs},
        )

    def active(self):
        self.request()
        self.complete(self.claim())

    def snapshots(self, count=15):
        for i in range(count):
            self.projects.snapshot(
                self.access,
                self.pid,
                [ProjectFile("source.txt", f"version-{i}".encode())],
                expected_version=self.project()["version"],
                now=self.now,
            )

    def task(self):
        tasks = self.owned(SubAgentStore(self.db))
        task = tasks.create_task(
            scope_key=self.scope.key,
            conversation_id="101",
            requester_user_id=201,
            trigger_message_id=None,
            objective="deliver a file",
            max_parallelism=1,
            max_steps=1,
        )
        self.sql.execute(
            "UPDATE subagent_tasks SET project_id=? WHERE task_id=?",
            (self.pid, task.task_id),
        )
        return task

    async def test_default_disabled_and_loop_stops_without_scanning(self):
        scheduler = self.scheduler(enabled=False)
        self.assertEqual(await scheduler.run_once(), [])
        with patch.object(scheduler, "run_once") as scan:
            stop = asyncio.Event()
            stop.set()
            await scheduler.run(stop)
            scan.assert_not_called()
        self.backend.maintenance_ready.assert_not_awaited()

    async def test_due_project_is_queued_once_across_scheduler_restart(self):
        self.active()
        expected = self.project()["last_active_at"]
        self.assertEqual(
            (await self.maintenance.run_once(now=self.now + 1800))[0]["action"], "stop"
        )
        self.assertEqual(await self.scheduler().run_once(now=self.now + 1801), [])
        journal = self.lifecycle.journal(self.access, self.pid)[0]
        self.assertEqual(
            journal["plan"]["trigger"], {"kind": "idle", "last_active_at": expected}
        )
        self.backend.preflight.assert_called_once()
        self.assertEqual(
            self.project()["status"], "active"
        )  # Enqueued, not falsely completed.

    async def test_automatic_snapshots_do_not_reset_idle_or_conversation_focus(self):
        self.active()
        focus = self.sql.execute("SELECT updated_at FROM project_focus").fetchone()[0]
        for offset, action, state in (
            (1800, "stop", "stopped"),
            (7 * DAY, "hibernate", "hibernated"),
            (60 * DAY, "archive", "archived"),
        ):
            result = await self.maintenance.run_once(now=self.now + offset)
            self.assertEqual(result[0]["action"], action)
            job = self.claim(now=self.now + offset)
            self.complete(job, now=self.now + offset)
            self.assertEqual(self.project()["status"], state)
            self.assertEqual(self.project()["last_active_at"], self.now)
            self.assertEqual(
                self.sql.execute("SELECT updated_at FROM project_focus").fetchone()[0],
                focus,
            )
        self.now += 61 * DAY
        self.request("wake", "manual-resume")
        self.complete(self.claim())
        self.assertEqual(self.project()["last_active_at"], self.now)
        self.assertIsNone(
            (await self.maintenance.run_once(now=self.now + 10))[0]["action"]
        )

    async def test_user_open_between_scan_and_admission_prevents_idle_stop(self):
        self.active()
        request = self.lifecycle.request

        def racing(*args, **kwargs):
            self.projects.open(self.access, self.pid, now=self.now + 1800)
            return request(*args, **kwargs)

        with patch.object(self.lifecycle, "request", side_effect=racing):
            result = await self.maintenance.run_once(now=self.now + 1800)
        self.assertEqual(result[0]["skipped"], "ProjectConflict")
        self.assertIsNone(self.project()["lifecycle_job_id"])
        self.assertEqual(self.project()["last_active_at"], self.now + 1800)

    async def test_pending_maintenance_blocks_open_but_status_remains_readable(self):
        self.active()
        await self.maintenance.run_once(now=self.now + 1800)
        with self.assertRaises(ProjectConflict):
            self.projects.open(self.access, self.pid, now=self.now + 1801)
        self.assertEqual(self.project()["last_active_at"], self.now)

    async def test_failed_backend_preflight_rolls_back_queue_and_lock(self):
        self.active()
        self.backend.preflight.side_effect = ProjectBackendUnavailable(
            "fixture missing adapter"
        )
        result = await self.maintenance.run_once(now=self.now + 1800)
        self.assertEqual(result[0]["skipped"], "ProjectBackendUnavailable")
        self.assertIsNone(self.project()["lifecycle_job_id"])
        self.assertEqual(len(self.lifecycle.journal(self.access, self.pid)), 1)

    async def test_missing_backend_busy_and_unverified_workspace_leave_versions_alone(
        self,
    ):
        self.snapshots()
        self.active()
        self.backend.maintenance_ready.return_value = False
        result = await self.maintenance.run_once(now=self.now + 1800)
        self.assertEqual(result[0]["skipped"], "ProjectConflict")
        self.assertEqual(len(self.projects.versions(self.access, self.pid)), 15)
        self.maintenance.backends.clear()
        result = await self.maintenance.run_once(now=self.now + 1800)
        self.assertEqual(result[0]["skipped"], "ProjectBackendUnavailable")
        self.assertIsNone(self.project()["lifecycle_job_id"])

    async def test_retention_uses_full_history_not_the_ui_page_and_preserves_labels(
        self,
    ):
        self.snapshots(210)
        self.projects.pin_version(
            self.access,
            self.pid,
            2,
            pinned=True,
            expected_version=self.project()["version"],
        )
        self.sql.execute(
            "UPDATE project_versions SET label='release' WHERE project_id=? AND version_no=3",
            (self.pid,),
        )
        result = await self.maintenance.run_once(now=self.now + DAY)
        self.assertEqual(result[0]["pruned"], 197)
        self.assertEqual(
            {v["version_no"] for v in self.projects.versions(self.access, self.pid)},
            {2, 3, *range(200, 211)},
        )
        self.assertEqual(self.project()["last_active_at"], self.now)
        self.assertEqual(
            (await self.maintenance.run_once(now=self.now + DAY))[0]["pruned"], 0
        )
        self.assertEqual(
            self.sql.execute(
                "SELECT count(*) FROM project_events WHERE kind='versions_pruned'"
            ).fetchone()[0],
            1,
        )

    async def test_unfinished_task_and_missing_delivery_block_retention_and_lifecycle(
        self,
    ):
        self.snapshots()
        self.active()
        task = self.task()
        for status in ("queued", "failed", "completed"):
            self.sql.execute(
                "UPDATE subagent_tasks SET status=?,plan_json=? WHERE task_id=?",
                (
                    status,
                    json.dumps({"contract": {"delivery_required": True}}),
                    task.task_id,
                ),
            )
            result = await self.maintenance.run_once(now=self.now + 1800)
            self.assertEqual(result[0]["skipped"], "ProjectConflict")
            self.assertEqual(len(self.projects.versions(self.access, self.pid)), 15)
            self.assertIsNone(self.project()["lifecycle_job_id"])

    async def test_referenced_task_baseline_is_never_pruned(self):
        self.snapshots()
        task = self.task()
        self.sql.execute(
            "UPDATE subagent_tasks SET status='completed',project_base_version=1 WHERE task_id=?",
            (task.task_id,),
        )
        await self.maintenance.run_once(now=self.now + DAY)
        self.assertIn(
            1, {v["version_no"] for v in self.projects.versions(self.access, self.pid)}
        )

    async def test_new_task_arriving_during_probe_blocks_pruning_in_the_store(self):
        self.snapshots()

        async def race(project):
            self.task()
            return True

        self.backend.maintenance_ready.side_effect = race
        result = await self.maintenance.run_once(now=self.now + DAY)
        self.assertEqual(result[0]["skipped"], "ProjectConflict")
        self.assertEqual(len(self.projects.versions(self.access, self.pid)), 15)

    async def test_retention_failure_rolls_back_versions_and_audit_together(self):
        self.snapshots()
        with patch.object(
            self.projects, "_event", side_effect=RuntimeError("fixture crash")
        ):
            result = await self.maintenance.run_once(now=self.now + DAY)
        self.assertEqual(result[0]["failed"], "RuntimeError")
        self.assertEqual(len(self.projects.versions(self.access, self.pid)), 15)
        self.assertEqual(
            self.sql.execute(
                "SELECT count(*) FROM project_events WHERE kind='versions_pruned'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            (await self.maintenance.run_once(now=self.now + DAY))[0]["pruned"], 4
        )

    async def test_revoked_wrong_owner_and_cross_scope_identity_are_denied(self):
        self.active()
        for access in (
            replace(self.access, member=False),
            replace(self.b, admin=True),
            replace(
                self.access, scope=replace(self.scope, native_conversation_id="999")
            ),
        ):
            self.maintenance.resolve_access = lambda project, actor: access
            result = await self.maintenance.run_once(now=self.now + 1800)
            self.assertEqual(result[0]["skipped"], "PermissionError")
            self.assertIsNone(self.project()["lifecycle_job_id"])

    async def test_membership_is_rechecked_after_helper_probe(self):
        self.active()
        identities = iter((self.access, replace(self.access, member=False)))
        self.maintenance.resolve_access = lambda project, actor: next(identities)
        self.assertEqual(
            (await self.maintenance.run_once(now=self.now + 1800))[0]["skipped"],
            "PermissionError",
        )

    async def test_bounded_round_robin_and_per_project_failure_isolation(self):
        other = self.projects.create(self.access, "other")["project_id"]
        scheduler = self.scheduler(batch_size=1)
        scheduler.resolve_access = Mock(
            side_effect=[RuntimeError("fixture"), self.access, self.access]
        )
        first = await scheduler.run_once(now=self.now)
        second = await scheduler.run_once(now=self.now)
        self.assertEqual(first[0]["failed"], "RuntimeError")
        self.assertEqual(second[0]["project_id"], other)
        self.assertEqual(await scheduler.run_once(now=self.now), [])
        self.assertEqual(
            (await scheduler.run_once(now=self.now))[0]["project_id"], self.pid
        )

    async def test_concurrent_schedulers_cannot_duplicate_admission(self):
        self.active()
        await asyncio.gather(
            self.maintenance.run_once(now=self.now + 1800),
            self.scheduler().run_once(now=self.now + 1800),
        )
        self.assertEqual(
            self.sql.execute(
                "SELECT count(*) FROM project_lifecycle_runs WHERE action='stop'"
            ).fetchone()[0],
            1,
        )

    async def test_cancelled_probe_does_not_schedule_or_prune(self):
        self.snapshots()
        self.backend.maintenance_ready.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.maintenance.run_once(now=self.now + DAY)
        self.assertEqual(len(self.projects.versions(self.access, self.pid)), 15)
        self.assertIsNone(self.project()["lifecycle_job_id"])

    async def test_nonautomatic_snapshot_still_refreshes_actual_activity(self):
        self.snapshots(1)
        self.projects.snapshot(
            self.access,
            self.pid,
            [ProjectFile("source.txt", b"version-0")],
            expected_version=self.project()["version"],
            now=self.now + 10,
        )
        self.assertEqual(self.project()["last_active_at"], self.now + 10)

    async def test_unreferenced_objects_are_collected_only_after_the_grace_period(self):
        self.snapshots()
        digest = self.projects.versions(self.access, self.pid)[-1]["manifest_sha256"]
        await self.maintenance.run_once(now=self.now + DAY)
        self.assertEqual(self.maintenance.gc_status["deleted"], 0)
        self.projects.blobs.read(digest)
        await self.maintenance.run_once(now=self.now + 2 * DAY)
        self.assertGreater(self.maintenance.gc_status["deleted"], 0)
        self.assertIsNone(
            self.sql.execute(
                "SELECT 1 FROM project_blobs WHERE sha256=?", (digest,)
            ).fetchone()
        )
        for version in self.projects.versions(self.access, self.pid):
            self.projects.blobs.read(version["manifest_sha256"])

    async def test_preview_baseline_survives_policy_even_after_preview_expiry(self):
        self.snapshots()
        self.sql.execute(
            """INSERT INTO project_previews(preview_id,project_id,version_no,root,expires_at,created_at)
            VALUES (?,?,1,'fixture',?,?)""",
            ("fixture-preview-with-a-long-id", self.pid, self.now + 1, self.now),
        )
        await self.maintenance.run_once(now=self.now + DAY)
        self.assertIn(
            1,
            {
                row["version_no"]
                for row in self.projects.versions(self.access, self.pid)
            },
        )

    async def test_automatic_request_needs_preflight_and_rechecks_pinning(self):
        self.active()
        with self.assertRaises(ValueError):
            self.lifecycle.request(
                self.access,
                self.pid,
                "stop",
                expected_version=self.project()["version"],
                request_key="invalid",
                idle_policy=ProjectPolicy(),
            )
        self.sql.execute(
            "UPDATE projects SET status='hibernated',pinned=true WHERE project_id=?",
            (self.pid,),
        )
        result = await self.maintenance.run_once(now=self.now + 60 * DAY)
        self.assertIsNone(result[0]["action"])
        self.assertIsNone(self.project()["lifecycle_job_id"])
