"""Real PostgreSQL queue/journal tests, not native backend acceptance."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import nonebot
from alembic import command
from alembic.config import Config

nonebot.init()

from src.bot_storage import open_store_connection
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.project_blobs import ProjectFile
from src.plugins.ai_chat.project_lifecycle import KIND, ProjectLifecycle
from src.plugins.ai_chat.projects import ProjectAccess, ProjectConflict, ProjectStore
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from src.plugins.ai_chat.subagents import SubAgentStore
from tests.postgres_fixture import postgres_for_test


class ProjectLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.db = postgres_for_test(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "objects"
        self.projects = self.owned(ProjectStore(self.db, self.root))
        self.jobs = self.owned(DurableJobStore(self.db, lease_seconds=10))
        ledger = self.owned(MessageLedger(self.db))
        self.principal = ledger.ensure_principal_identity("onebot-v11", "201", "A")
        self.other = ledger.ensure_principal_identity("onebot-v11", "202", "B")
        self.scope = ConversationScope("onebot-v11", "group", "101", "201", "301")
        self.access = ProjectAccess(self.scope, self.principal, member=True)
        self.b = ProjectAccess(self.scope.with_actor("202"), self.other, member=True)
        self.now = int(time.time())
        _, self.sql = open_store_connection(self.db)
        self.addCleanup(self.sql.close)
        self.lifecycle = ProjectLifecycle(self.projects, self.jobs)
        self.pid = self.projects.create(self.access, "Retained project", now=self.now)[
            "project_id"
        ]

    def owned(self, value):
        self.addCleanup(value.close)
        return value

    def project(self):
        return self.projects.get(self.access, self.pid)

    def request(self, action="wake", key="wake-1", **kwargs):
        return self.lifecycle.request(
            self.access,
            self.pid,
            action,
            request_key=key,
            expected_version=self.project()["version"],
            now=self.now,
            **kwargs,
        )

    def claim(self, owner="first", now=None):
        return self.jobs.claim_due(
            owner, kinds=(KIND,), now=self.now if now is None else now
        )[0]

    def evidence(self, step):
        evidence = {
            key: step["arguments"][key] for key in ("project_id", "generation", "fence")
        }
        evidence.update(
            verified=True, observation="Fixture observation; not a physical host result"
        )
        if step["operation"].startswith("verify_"):
            evidence["state"] = step["operation"].removeprefix("verify_")
            if evidence["state"] in {"active", "stopped"}:
                evidence["sandbox_id"] = "fixture-sandbox"
        if step["operation"] == "snapshot_workspace":
            version = self.projects.versions(self.access, self.pid)[0]
            evidence["snapshot"] = {
                key: version[key] for key in ("version_no", "manifest_sha256")
            }
        if step["operation"] == "prune_rebuildable":
            evidence["snapshot"] = step["arguments"]["snapshot"]
        return evidence

    def complete(self, job, now=None):
        now = self.now if now is None else now
        while step := self.lifecycle.next_step(job, now=now):
            if step["operation"] == "snapshot_workspace":
                self.projects.snapshot(
                    self.access,
                    self.pid,
                    [ProjectFile("source.txt", b"retained")],
                    expected_version=self.project()["version"],
                    lifecycle_job=job,
                    now=now,
                )
            self.lifecycle.acknowledge(job, step, self.evidence(step), now=now)
        return self.lifecycle.finish(job, now=now)

    def advance_to(self, job, operation):
        while step := self.lifecycle.next_step(job, now=self.now):
            if step["operation"] == operation:
                return step
            self.lifecycle.acknowledge(job, step, self.evidence(step), now=self.now)
        self.fail(f"Missing planned operation: {operation}")

    def test_queue_and_project_lock_are_atomic_and_same_request_replays(self):
        run = self.request()
        self.assertEqual(self.request()["job_id"], run["job_id"])
        self.assertEqual(self.project()["status"], "stopped")
        self.assertEqual(self.project()["workspace_generation"], 1)
        self.assertEqual(self.project()["lifecycle_job_id"], run["job_id"])
        self.assertEqual(
            self.sql.execute("SELECT count(*) FROM durable_jobs").fetchone()[0], 1
        )
        with self.assertRaises(ProjectConflict):
            self.request("stop", "wake-1")
        with self.assertRaises(ProjectConflict):
            self.request(key="other")
        with self.assertRaises(ProjectConflict):
            self.projects.update(
                self.access, self.pid, expected_version=2, title="racing writer"
            )
        with self.assertRaises(ProjectConflict):
            self.projects.snapshot(self.access, self.pid, [], expected_version=2)

    def test_enqueue_failure_rolls_back_queue_and_generation(self):
        with patch.object(self.projects, "_event", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.request()
        self.assertEqual(self.project()["workspace_generation"], 0)
        self.assertIsNone(self.project()["lifecycle_job_id"])
        self.assertEqual(
            self.sql.execute("SELECT count(*) FROM durable_jobs").fetchone()[0], 0
        )
        self.assertEqual(
            self.sql.execute("SELECT count(*) FROM project_lifecycle_runs").fetchone()[
                0
            ],
            0,
        )

    def test_concurrent_requests_select_one_job(self):
        stores = [self.owned(ProjectStore(self.db, self.root)) for _ in range(4)]

        def request(store):
            return ProjectLifecycle(store, self.jobs).request(
                self.access,
                self.pid,
                "wake",
                request_key="same",
                expected_version=1,
                now=self.now,
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(request, stores))
        self.assertEqual(len({row["job_id"] for row in rows}), 1)

    def test_scope_membership_and_editor_enforcement(self):
        for access in (
            self.b,
            replace(self.access, member=False),
            replace(
                self.access,
                scope=replace(self.scope, native_conversation_id="999"),
                admin=True,
            ),
        ):
            with self.assertRaises(PermissionError):
                self.lifecycle.request(
                    access, self.pid, "wake", expected_version=1, request_key="denied"
                )
        self.request()
        with self.assertRaises(ProjectConflict):
            self.lifecycle.request(
                self.b, self.pid, "wake", expected_version=2, request_key="wake-1"
            )
        with self.assertRaises(PermissionError):
            self.lifecycle.journal(
                replace(
                    self.access, scope=replace(self.scope, native_conversation_id="999")
                ),
                self.pid,
            )

    def test_takeover_reuses_physical_fence_but_replaces_uncertain_receipt(self):
        self.request()
        old = self.claim()
        first = self.lifecycle.next_step(old, now=self.now)
        self.assertEqual(self.lifecycle.next_step(old, now=self.now), first)
        replacement = ProjectLifecycle(
            self.owned(ProjectStore(self.db, self.root)), self.jobs
        )
        new = self.claim("second", self.now + 11)
        resumed = replacement.next_step(new, now=self.now + 11)
        self.assertEqual(resumed["fence"], first["fence"])
        self.assertEqual(resumed["arguments"], first["arguments"])
        self.assertNotEqual(resumed["request_id"], first["request_id"])
        for work in (
            lambda: replacement.next_step(old, now=self.now + 11),
            lambda: replacement.acknowledge(
                old, first, self.evidence(first), now=self.now + 11
            ),
            lambda: replacement.acknowledge(
                new, first, self.evidence(first), now=self.now + 11
            ),
        ):
            with self.assertRaises(ProjectConflict):
                work()
        replacement.acknowledge(new, resumed, self.evidence(resumed), now=self.now + 11)
        next_step = replacement.next_step(new, now=self.now + 11)
        self.assertEqual(next_step["fence"], first["fence"] + 1)

    def test_expired_or_forged_lease_cannot_snapshot_or_settle(self):
        self.request()
        job = self.claim()
        for bad, now in (
            (job, self.now + 10),
            (replace(job, lease_owner="imposter"), self.now),
            (replace(job, scope_key="other"), self.now),
            (replace(job, lease_generation=job.lease_generation + 1), self.now),
            (
                replace(job, payload={"project_id": self.pid, "injected": True}),
                self.now,
            ),
        ):
            with self.assertRaises(ProjectConflict):
                self.lifecycle.next_step(bad, now=now)
            with self.assertRaises(ProjectConflict):
                self.projects.snapshot(
                    self.access,
                    self.pid,
                    [],
                    expected_version=2,
                    lifecycle_job=bad,
                    now=now,
                )

    def test_snapshot_checks_expiry_again_before_commit(self):
        self.request()
        self.complete(self.claim())
        self.request("stop", "stop")
        job = self.claim()
        self.advance_to(job, "snapshot_workspace")
        version = self.project()["version"]
        with patch(
            "src.plugins.ai_chat.projects.time.time",
            side_effect=[self.now, self.now, self.now + 11],
        ):
            with self.assertRaises(ProjectConflict):
                self.projects.snapshot(
                    self.access,
                    self.pid,
                    [ProjectFile("file", b"payload")],
                    expected_version=version,
                    lifecycle_job=job,
                )
        self.assertEqual(self.projects.versions(self.access, self.pid), [])

    def test_lease_does_not_allow_snapshot_outside_its_pending_step(self):
        self.request()
        job = self.claim()
        self.lifecycle.next_step(job, now=self.now)
        with self.assertRaises(ProjectConflict):
            self.projects.snapshot(
                self.access,
                self.pid,
                [],
                expected_version=self.project()["version"],
                lifecycle_job=job,
                now=self.now,
            )

    def test_evidence_is_bound_and_immutable_and_exit_code_is_not_enough(self):
        self.request()
        job = self.claim()
        step = self.lifecycle.next_step(job, now=self.now)
        good = self.evidence(step)
        for evidence in (
            {"exit_code": 0},
            {**good, "generation": 9},
            {**good, "fence": True},
            {**good, "observation": ""},
        ):
            with self.assertRaises((ValueError, ProjectConflict)):
                self.lifecycle.acknowledge(job, step, evidence, now=self.now)
        with self.assertRaises(ProjectConflict):
            self.lifecycle.finish(job, now=self.now)
        self.lifecycle.acknowledge(job, step, good, now=self.now)
        self.lifecycle.acknowledge(job, step, good, now=self.now)
        with self.assertRaises(ProjectConflict):
            self.lifecycle.acknowledge(
                job, step, {**good, "observation": "changed"}, now=self.now
            )

    def test_complete_updates_state_and_queue_in_same_transaction(self):
        self.request()
        job = self.claim()
        result = self.complete(job)
        self.assertEqual(result["state"], "active")
        self.assertEqual(self.project()["sandbox_id"], "fixture-sandbox")
        self.assertIsNone(self.project()["lifecycle_job_id"])
        self.assertEqual(
            self.sql.execute(
                "SELECT status FROM durable_jobs WHERE job_id=?", (job.job_id,)
            ).fetchone()[0],
            "succeeded",
        )
        self.assertEqual(self.request()["status"], "completed")
        with self.assertRaises(ProjectConflict):
            self.lifecycle.next_step(job, now=self.now)

    def test_archive_restore_keeps_retained_data_and_advances_incarnation(self):
        for action in ("wake", "stop", "hibernate", "archive"):
            self.request(action, action)
            self.complete(self.claim())
        old_fence = self.project()["lifecycle_fence"]
        self.assertEqual(self.project()["status"], "archived")
        self.assertEqual(
            self.projects.read_file(self.access, self.pid, 1, "source.txt"), b"retained"
        )
        self.projects.update(
            self.access,
            self.pid,
            expected_version=self.project()["version"],
            rebuild_command="npm ci",
        )
        run = self.request("wake", "restore")
        self.assertEqual(run["generation"], 2)
        self.assertEqual(
            run["plan"]["steps"],
            [
                "create_volume",
                "reserve_workspace",
                "mount_volume",
                "restore_version",
                "start_workspace",
                "release_workspace",
                "verify_active",
            ],
        )
        step = self.lifecycle.next_step(self.claim(), now=self.now)
        self.assertEqual(step["fence"], old_fence + 1)
        self.assertEqual(step["arguments"]["baseline"]["version_no"], 1)

    def test_snapshot_evidence_must_reference_a_real_published_version(self):
        self.request()
        self.complete(self.claim())
        self.request("stop", "stop")
        job = self.claim()
        snapshot = self.advance_to(job, "snapshot_workspace")
        bad = {
            key: snapshot["arguments"][key]
            for key in ("project_id", "generation", "fence")
        }
        bad.update(verified=True, observation="Fixture only")
        bad["snapshot"] = {"version_no": 1, "manifest_sha256": "a" * 64}
        with self.assertRaises(ProjectConflict):
            self.lifecycle.acknowledge(job, snapshot, bad, now=self.now)

    def test_failed_operation_keeps_project_locked_and_retry_identity(self):
        self.request()
        job = self.claim()
        step = self.lifecycle.next_step(job, now=self.now)
        self.lifecycle.needs_attention(job, now=self.now)
        self.assertEqual(
            self.lifecycle.journal(self.access, self.pid)[0]["status"],
            "needs_attention",
        )
        with self.assertRaises(ProjectConflict):
            self.request(key="bypass")
        self.assertEqual(self.lifecycle.next_step(job, now=self.now), step)

    def test_invalid_transitions_pinning_and_missing_archive_version_are_refused(self):
        with self.assertRaises(ProjectConflict):
            self.request("archive")
        for action in ("wake", "stop", "hibernate"):
            self.request(action, action)
            self.complete(self.claim())
        self.projects.update(
            replace(self.access, admin=True),
            self.pid,
            expected_version=self.project()["version"],
            pinned=True,
        )
        with self.assertRaises(ProjectConflict):
            self.request("archive", "pinned")
        self.sql.execute(
            "UPDATE projects SET status='archived' WHERE project_id=?", (self.pid,)
        )
        self.sql.execute(
            "DELETE FROM project_version_blobs WHERE project_id=?", (self.pid,)
        )
        self.sql.execute("DELETE FROM project_versions WHERE project_id=?", (self.pid,))
        with self.assertRaises(ProjectConflict):
            self.request("wake", "missing-baseline")

    def test_task_and_missing_or_unconfirmed_delivery_block_reclamation(self):
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
        with self.assertRaises(ProjectConflict):
            self.request()
        self.sql.execute(
            "UPDATE subagent_tasks SET status='completed',plan_json=? WHERE task_id=?",
            (json.dumps({"contract": {"delivery_required": True}}), task.task_id),
        )
        with self.assertRaises(ProjectConflict):
            self.request()
        self.sql.execute(
            "UPDATE subagent_tasks SET plan_json='{}' WHERE task_id=?", (task.task_id,)
        )
        self.sql.execute(
            "INSERT INTO subagent_deliveries(task_id,revision,delivery_key,state,payload_json,updated_at) VALUES (?,1,'file','unknown','{}',?)",
            (task.task_id, self.now),
        )
        with self.assertRaises(ProjectConflict):
            self.request()
        self.sql.execute(
            "UPDATE subagent_deliveries SET state='acknowledged' WHERE task_id=?",
            (task.task_id,),
        )
        self.request()

    def test_new_task_detected_before_dispatch_prevents_reclamation(self):
        self.request()
        job = self.claim()
        tasks = self.owned(SubAgentStore(self.db))
        task = tasks.create_task(
            scope_key=self.scope.key,
            conversation_id="101",
            requester_user_id=201,
            trigger_message_id=None,
            objective="fixture",
            max_parallelism=1,
            max_steps=1,
        )
        self.sql.execute(
            "UPDATE subagent_tasks SET project_id=? WHERE task_id=?",
            (self.pid, task.task_id),
        )
        with self.assertRaises(ProjectConflict):
            self.lifecycle.next_step(job, now=self.now)

    def test_retained_journal_prevents_destructive_schema_downgrade(self):
        self.request()
        config = Config()
        config.set_main_option(
            "script_location", str(Path(__file__).resolve().parents[1] / "migrations")
        )
        with patch.dict(
            os.environ,
            {
                "AI_POSTGRES_DSN": os.environ["TEST_POSTGRES_DSN"],
                "AI_POSTGRES_SCHEMA": self.db.schema,
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "retained project lifecycle"):
                command.downgrade(config, "0043_project_workspace")
        self.assertEqual(len(self.lifecycle.journal(self.access, self.pid)), 1)
        self.assertIsNotNone(self.project()["lifecycle_job_id"])
