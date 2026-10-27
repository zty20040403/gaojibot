"""Actual PostgreSQL accounting and Outbox; no production or model calls."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import nonebot

nonebot.init()

from src.project_capacity import quota_rejection
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.observability import BotTelemetry
from src.plugins.ai_chat.project_blobs import ProjectFile
from src.plugins.ai_chat.project_usage import (
    ProjectQuotaRejected,
    ProjectUsageSampler,
    ProjectUsageStore,
)
from src.plugins.ai_chat.projects import ProjectConflict, ProjectStore
from tests import test_project_lifecycle as fixture


class UsageFixture:
    owned = fixture.ProjectLifecycleTests.owned
    project = fixture.ProjectLifecycleTests.project

    def setUp(self):
        fixture.ProjectLifecycleTests.setUp(self)
        self.deliveries = self.owned(DeliveryStore(self.db, recover_interrupted=False))
        self.telemetry = BotTelemetry()
        self.usage = ProjectUsageStore(
            self.projects, self.deliveries, telemetry=self.telemetry
        )

    def sample(self, used=0, *, pid=None, **changes):
        project = self.projects.get(self.access, self.pid if pid is None else pid)
        return {
            "project_id": project["project_id"],
            "generation": project["workspace_generation"],
            "fence": project["lifecycle_fence"],
            "quota_bytes": project["quota_bytes"],
            "used_bytes": used,
            "rebuildable_bytes": 0,
            "quota_mode": "hard",
            "sampled_at": self.now,
            "complete": True,
            **changes,
        }

    def record(self, sample=None, *, now=None, pid=None):
        pid = self.pid if pid is None else pid
        return self.usage.record(
            self.access,
            pid,
            self.sample() if sample is None else sample,
            expected_version=self.projects.get(self.access, pid)["version"],
            now=self.now if now is None else now,
        )

    def rejected(self, code, **kwargs):
        with self.assertRaises(ProjectQuotaRejected) as raised:
            self.usage.require(self.access, self.pid, now=self.now, **kwargs)
        self.assertEqual(raised.exception.code, code)


class ProjectUsageStoreTests(UsageFixture, unittest.TestCase):
    def test_updates_usage_not_configuration_or_actual_activity(self):
        before = self.project()
        focus = self.sql.execute("SELECT updated_at FROM project_focus").fetchone()[0]
        self.record(self.sample(500, rebuildable_bytes=100))
        after = self.project()
        self.assertEqual((after["used_bytes"], after["rebuildable_bytes"]), (500, 100))
        for field in ("version", "last_active_at", "lifecycle_fence"):
            self.assertEqual(after[field], before[field])
        self.assertEqual(
            self.sql.execute("SELECT updated_at FROM project_focus").fetchone()[0],
            focus,
        )
        self.usage.require(self.access, self.pid, now=self.now)

    def test_failures_preserve_last_measurement_but_never_admit_unknown(self):
        self.rejected("project_usage_unknown")
        self.record(self.sample(500))
        self.usage.record(
            self.access,
            self.pid,
            None,
            expected_version=1,
            now=self.now,
            error="TimeoutError",
        )
        self.assertEqual(self.project()["used_bytes"], 500)
        self.rejected("project_usage_unknown")
        status = self.usage.status(self.access, self.pid, now=self.now)
        self.assertIsNone(status["used_bytes"])
        self.assertEqual(status["last_known_used_bytes"], 500)
        self.assertEqual(status["error"], "TimeoutError")

    def test_stale_and_invalid_samples_do_not_replace_good_measurement(self):
        self.record(self.sample(500))
        for changes in (
            {"project_id": self.pid + 1},
            {"generation": 2},
            {"fence": -1},
            {"fence": 1},
            {"sampled_at": self.now - 91},
            {"sampled_at": self.now + 6},
            {"sampled_at": self.now - 1},
            {"used_bytes": -1},
            {"used_bytes": True},
            {"used_bytes": self.project()["quota_bytes"] + 1},
            {"complete": False},
            {"rebuildable_bytes": 501},
            {"quota_mode": "unknown"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ProjectConflict):
                self.record(self.sample(500, **changes))
        self.assertEqual(self.project()["used_bytes"], 500)
        self.usage.require(self.access, self.pid, now=self.now)

    def test_95_denies_dependencies_but_not_small_source_edit_and_100_denies_all(self):
        quota = self.project()["quota_bytes"]
        self.record(self.sample((quota * 95 + 99) // 100))
        self.rejected("project_dependency_limit", installing_dependencies=True)
        self.usage.require(self.access, self.pid, now=self.now, expected_bytes=100)
        self.rejected("project_quota_full", expected_bytes=quota)
        self.record(self.sample(quota))
        self.rejected("project_quota_full")
        self.assertIn(
            b"gaoji_project_quota_rejections_total 3.0", self.telemetry.render()
        )

    def test_thresholds_do_not_round_early(self):
        self.assertIsNone(quota_rejection(949, 1000, installing_dependencies=True))
        self.assertEqual(
            quota_rejection(950, 1000, installing_dependencies=True),
            "project_dependency_limit",
        )
        self.assertEqual(quota_rejection(1000, 1000), "project_quota_full")
        self.assertEqual(quota_rejection(1001, 1000), "project_quota_full")
        for args in ((True, 1000), (-1, 1000), (0, 0)):
            with self.assertRaises(ValueError):
                quota_rejection(*args)

    def test_expired_sample_changed_quota_or_generation_requires_remeasure(self):
        self.record()
        with self.assertRaises(ProjectQuotaRejected):
            self.usage.require(self.access, self.pid, now=self.now + 91)
        for column in ("quota_bytes", "workspace_generation", "lifecycle_fence"):
            self.sql.execute(
                f"UPDATE projects SET {column}={column}+1 WHERE project_id=?",
                (self.pid,),
            )
            self.rejected("project_usage_unknown")
            self.record()
            self.usage.require(self.access, self.pid, now=self.now)

    def test_unknown_peer_and_scope_total_including_versions_are_checked(self):
        self.record(self.sample(200))
        peer = self.projects.create(self.access, "peer")["project_id"]
        self.rejected("project_scope_usage_unknown")
        self.record(self.sample(300, pid=peer), pid=peer)
        self.projects.snapshot(
            self.access,
            self.pid,
            [ProjectFile("notes", b"retained" * 100)],
            expected_version=1,
        )
        cursor = self.sql.cursor()
        try:
            used = self.projects._scope_bytes(cursor, self.scope.key)
        finally:
            cursor.close()
        self.sql.execute(
            "UPDATE project_scope_limits SET quota_bytes=? WHERE scope_key=?",
            (used + 10, self.scope.key),
        )
        self.usage.require(self.access, self.pid, now=self.now, expected_bytes=9)
        self.rejected("project_scope_quota_full", expected_bytes=10)

    def test_alert_is_once_per_conversation_utc_day_even_across_restart_and_projects(
        self,
    ):
        sample = self.sample(self.project()["quota_bytes"] * 8 // 10 + 1)
        self.record(sample)
        self.usage = ProjectUsageStore(self.projects, self.deliveries)
        self.record(sample)
        peer = self.projects.create(self.access, "another full project")["project_id"]
        self.record(self.sample(sample["used_bytes"], pid=peer), pid=peer)
        self.assertEqual(
            self.sql.execute("SELECT count(*) FROM deliveries").fetchone()[0], 1
        )
        tomorrow = self.now + 86400
        self.record({**sample, "sampled_at": tomorrow}, now=tomorrow)
        rows = self.sql.execute(
            "SELECT target_kind,target_native_conversation_id,status FROM deliveries"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(
            all(
                (
                    row["target_kind"],
                    row["target_native_conversation_id"],
                    row["status"],
                )
                == ("group", "101", "pending")
                for row in rows
            )
        )
        self.assertEqual(
            self.sql.execute(
                "SELECT count(*) FROM project_events WHERE kind='quota_warning_enqueued'"
            ).fetchone()[0],
            2,
        )

    def test_alert_and_accounting_rollback_together(self):
        with patch.object(
            self.projects, "_event", side_effect=RuntimeError("fixture rollback")
        ):
            with self.assertRaises(RuntimeError):
                self.record(self.sample(self.project()["quota_bytes"]))
        self.assertIsNone(self.project()["sampled_at"])
        self.assertEqual(
            self.sql.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0
        )
        self.assertEqual(
            self.sql.execute("SELECT count(*) FROM project_usage").fetchone()[0], 0
        )

    def test_concurrent_samplers_only_enqueue_one_warning(self):
        stores = [self.owned(ProjectStore(self.db, self.root)) for _ in range(3)]
        sample = self.sample(self.project()["quota_bytes"])
        with ThreadPoolExecutor(max_workers=3) as workers:
            list(
                workers.map(
                    lambda store: ProjectUsageStore(store, self.deliveries).record(
                        self.access, self.pid, sample, expected_version=1, now=self.now
                    ),
                    stores,
                )
            )
        self.assertEqual(
            self.sql.execute("SELECT count(*) FROM deliveries").fetchone()[0], 1
        )

    def test_foreign_scope_membership_and_stale_version_cannot_record_or_read(self):
        for access in (
            self.b,
            replace(self.access, member=False),
            replace(
                self.access,
                scope=replace(self.scope, native_conversation_id="999"),
                admin=True,
            ),
        ):
            with self.subTest(access=access), self.assertRaises(PermissionError):
                self.usage.record(
                    access, self.pid, self.sample(), expected_version=1, now=self.now
                )
        foreign = replace(
            self.access,
            scope=replace(self.scope, native_conversation_id="999"),
            admin=True,
        )
        with self.assertRaises(PermissionError):
            self.usage.status(foreign, self.pid, now=self.now)
        with self.assertRaises(ProjectConflict):
            self.usage.record(
                self.access, self.pid, self.sample(), expected_version=2, now=self.now
            )

    def test_metrics_keep_unmeasured_distinct_and_deduplicate_shared_blobs(self):
        self.record(self.sample(1000, rebuildable_bytes=500))
        peer = self.projects.create(self.access, "second")["project_id"]
        for pid in (self.pid, peer):
            self.projects.snapshot(
                self.access,
                pid,
                [ProjectFile("a.txt", b"same content")],
                expected_version=1,
            )
        expected = self.sql.execute(
            "SELECT sum(byte_size) FROM project_blobs"
        ).fetchone()[0]
        metrics = self.usage.metrics(now=self.now)
        self.assertEqual(
            metrics["bytes"],
            {"workspace": 1000, "rebuildable": 500, "versions": expected},
        )
        self.assertEqual(metrics["unknown"], 1)
        self.assertEqual(metrics["rebuildable_unknown"], 1)
        self.assertEqual(metrics["states"], {"stopped": 2})
        self.record(self.sample(400, rebuildable_bytes=None, pid=peer), pid=peer)
        metrics = self.usage.metrics(now=self.now)
        self.assertEqual((metrics["unknown"], metrics["rebuildable_unknown"]), (0, 1))
        self.assertIsNone(
            self.usage.status(self.access, peer, now=self.now)["rebuildable_bytes"]
        )
        self.telemetry.update_project_gauges(metrics)
        rendered = self.telemetry.render().decode()
        self.assertIn('gaoji_project_bytes{kind="workspace"} 1400.0', rendered)
        self.assertIn("gaoji_project_rebuildable_unknown 1.0", rendered)
        self.assertNotIn("project_id=", rendered)


class ProjectUsageSamplerTests(UsageFixture, unittest.IsolatedAsyncioTestCase):
    def scheduler(self, **kwargs):
        self.backend = SimpleNamespace(
            sample_usage=AsyncMock(return_value=self.sample(123))
        )
        return ProjectUsageSampler(
            self.usage,
            {"oci": self.backend},
            resolve_access=lambda project, actor: self.access,
            **{"enabled": True, **kwargs},
        )

    async def test_default_disabled_and_stopped_loop_does_not_sample(self):
        worker = self.scheduler(enabled=False)
        self.assertEqual(await worker.run_once(), [])
        stop = asyncio.Event()
        stop.set()
        await worker.run(stop)
        self.backend.sample_usage.assert_not_awaited()

    async def test_real_store_roundtrip_and_failures_isolated_by_project(self):
        peer = self.projects.create(self.access, "peer")["project_id"]
        worker = self.scheduler()
        self.backend.sample_usage.side_effect = [
            TimeoutError("secret must not appear"),
            self.sample(321, pid=peer),
        ]
        result = await worker.run_once()
        self.assertEqual(
            result,
            [
                {"project_id": self.pid, "error": "TimeoutError"},
                {"project_id": peer, "sampled": True},
            ],
        )
        rows = self.sql.execute(
            "SELECT complete,error FROM project_usage ORDER BY project_id"
        ).fetchall()
        self.assertEqual(
            [(row["complete"], row["error"]) for row in rows],
            [(False, "TimeoutError"), (True, "")],
        )
        self.assertEqual(self.projects.get(self.access, peer)["used_bytes"], 321)

    async def test_membership_revalidated_after_helper_and_never_fabricated(self):
        worker = self.scheduler()
        worker.resolve_access = AsyncMock(
            side_effect=[self.access, replace(self.access, member=False)]
        )
        self.assertEqual((await worker.run_once())[0]["error"], "PermissionError")
        self.assertIsNone(self.project()["sampled_at"])
        self.assertEqual(worker.resolve_access.await_count, 2)

    async def test_lifecycle_race_cannot_overwrite_usage(self):
        worker = self.scheduler()

        async def raced(project):
            self.sql.execute(
                "UPDATE projects SET version=version+1 WHERE project_id=?", (self.pid,)
            )
            return self.sample(123)

        self.backend.sample_usage.side_effect = raced
        self.assertEqual((await worker.run_once())[0]["error"], "ProjectConflict")
        self.assertIsNone(self.project()["sampled_at"])

    async def test_cancellation_is_not_swallowed_or_marked_as_zero(self):
        worker = self.scheduler()
        self.backend.sample_usage.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await worker.run_once()
        self.assertIsNone(self.project()["sampled_at"])

    async def test_scan_failure_loop_survives_without_logging_raw_payload(self):
        worker = self.scheduler()
        stop = asyncio.Event()

        async def failure():
            stop.set()
            raise OSError("private response")

        with (
            patch.object(worker, "run_once", side_effect=failure),
            patch("src.plugins.ai_chat.project_usage.logger.warning") as warning,
        ):
            await worker.run(stop)
        warning.assert_called_once_with("Project usage sampling failed: %s", "OSError")
        self.assertNotIn("private response", str(warning.call_args))

    async def test_bounded_pages_rotate(self):
        peer = self.projects.create(self.access, "peer")["project_id"]
        worker = self.scheduler(batch_size=1)
        self.backend.sample_usage.side_effect = lambda project: self.sample(
            123, pid=project["project_id"]
        )
        ids = []
        for _ in range(4):
            ids += [row["project_id"] for row in await worker.run_once()]
        self.assertEqual(ids, [self.pid, peer, self.pid])
