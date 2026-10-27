"""Real PostgreSQL admission; helper observations are explicit fixtures, not I/O."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import os
import threading
import unittest
from unittest.mock import patch

import nonebot
from alembic import command
from alembic.config import Config

nonebot.init()

from src.bot_storage import DatabaseError
from src.bot_storage.schema import HEAD_REVISION
from src.plugins.ai_chat.project_blobs import ProjectFile
from src.plugins.ai_chat.project_io import ProjectIOStore
from src.plugins.ai_chat.project_lifecycle import ProjectLifecycle
from src.plugins.ai_chat.project_usage import ProjectQuotaRejected, ProjectUsageStore
from src.plugins.ai_chat.projects import (
    ProjectConflict,
    ProjectQuotaExceeded,
    ProjectStore,
)
from tests import test_project_lifecycle as lifecycle_fixture
from tests.test_project_usage import UsageFixture


class ProjectIOTests(UsageFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.lifecycle.request(
            self.access,
            self.pid,
            "wake",
            request_key="fixture-wake",
            expected_version=self.project()["version"],
            now=self.now,
        )
        job = self.jobs.claim_due(
            "fixture", kinds=("project.lifecycle",), now=self.now
        )[0]
        while step := self.lifecycle.next_step(job, now=self.now):
            self.lifecycle.acknowledge(
                job,
                step,
                lifecycle_fixture.ProjectLifecycleTests.evidence(self, step),
                now=self.now,
            )
        self.lifecycle.finish(job, now=self.now)
        self.io = ProjectIOStore(self.projects, self.usage)
        self.record(self.sample(100))

    def admit(
        self,
        tool="sandbox_write_file",
        key="write-1",
        *,
        access=None,
        pid=None,
        io=None,
        **kwargs,
    ):
        return (io or self.io).admit(
            access or self.access,
            pid or self.pid,
            tool,
            request_key=hashlib.sha256(key.encode()).hexdigest(),
            arguments_sha256=hashlib.sha256(b"fixture arguments").hexdigest(),
            expected_version=(io or self.io).projects.get(
                access or self.access, pid or self.pid
            )["version"],
            now=self.now,
            **kwargs,
        )

    def observation(self, grant):
        row = self.sql.execute(
            "SELECT * FROM project_io WHERE operation_id=?", (grant.operation_id,)
        ).fetchone()
        return {
            "sandbox_id": row["sandbox_id"],
            "project_id": row["project_id"],
            "generation": row["generation"],
            "fence": row["fence"],
            "pending": False,
            "quiescent": True,
        }

    def test_reserved_dispatched_receipted_write_invalidates_usage_not_files(self):
        version = self.project()["version"]
        grant = self.admit(expected_bytes=10)
        self.assertEqual(self.io.receipt(self.access, grant)["state"], "reserved")
        target = self.io.dispatch(self.access, grant, now=self.now)
        self.assertEqual(target["owner"], self.scope.key)
        self.assertEqual(target["sandbox_id"], "fixture-sandbox")
        self.assertEqual(self.project()["version"], version)
        self.io.complete(
            grant,
            {"ok": True, "bytes_written": 10},
            self.observation(grant),
            now=self.now,
        )
        self.assertEqual(
            self.io.receipt(self.access, grant)["result"]["bytes_written"], 10
        )
        self.assertEqual(self.project()["version"], version + 1)
        self.assertFalse(
            self.usage.status(self.access, self.pid, now=self.now)["complete"]
        )
        self.assertEqual(
            self.usage.status(self.access, self.pid, now=self.now)[
                "last_known_used_bytes"
            ],
            100,
        )

    def test_read_viewer_admission_and_no_write_rights(self):
        self.projects.open(self.b, self.pid, now=self.now)
        with self.assertRaises(PermissionError):
            self.admit(access=self.b)
        before = self.project()["version"]
        grant = self.admit("sandbox_read_file", access=self.b)
        self.io.dispatch(self.b, grant)
        self.io.complete(
            grant, {"ok": True, "content": "safe"}, self.observation(grant)
        )
        self.assertEqual(self.project()["version"], before)
        self.assertTrue(
            self.usage.status(self.access, self.pid, now=self.now)["complete"]
        )
        with self.assertRaises(PermissionError):
            self.io.receipt(self.access, grant)

    def test_reserved_operation_blocks_maintenance_snapshot_and_metadata_edit(self):
        grant = self.admit("sandbox_read_file")
        for action in (
            lambda: self.lifecycle.request(
                self.access,
                self.pid,
                "stop",
                request_key="stop",
                expected_version=self.project()["version"],
                now=self.now,
            ),
            lambda: self.projects.update(
                self.access,
                self.pid,
                expected_version=self.project()["version"],
                title="changed",
            ),
            lambda: self.projects.update(
                self.access,
                self.pid,
                expected_version=self.project()["version"],
                quota_bytes=10000,
            ),
            lambda: self.projects.snapshot(
                self.access,
                self.pid,
                [ProjectFile("x", b"x")],
                expected_version=self.project()["version"],
            ),
        ):
            with self.assertRaises(ProjectConflict):
                action()
        self.io.abort(grant)
        self.lifecycle.request(
            self.access,
            self.pid,
            "stop",
            request_key="stop",
            expected_version=self.project()["version"],
            now=self.now,
        )
        with self.assertRaises(ProjectConflict):
            self.admit("sandbox_read_file", key="after-stop")

    def test_duplicate_dispatch_never_reruns_and_completion_replay_does_not_unlock_new_work(
        self,
    ):
        grant = self.admit("sandbox_read_file")
        self.assertEqual(grant, self.admit("sandbox_read_file"))
        self.io.dispatch(self.access, grant)
        with self.assertRaises(ProjectConflict):
            self.io.dispatch(self.access, grant)
        evidence = self.observation(grant)
        result = {"ok": True}
        self.io.complete(grant, result, evidence)
        fresh = self.admit("sandbox_read_file", key="new")
        self.io.complete(grant, result, evidence)
        self.assertEqual(self.io.receipt(self.access, fresh)["state"], "reserved")
        with self.assertRaises(ProjectConflict):
            self.io.complete(grant, {"ok": False}, evidence)
        with self.assertRaises(ProjectConflict):
            self.admit("sandbox_exec")

    def test_process_reopen_retains_uncertain_lock_even_after_long_time(self):
        grant = self.admit()
        self.io.dispatch(self.access, grant)
        store = self.owned(ProjectStore(self.db, self.root))
        recovered = ProjectIOStore(store, ProjectUsageStore(store))
        self.assertEqual(recovered.receipt(self.access, grant)["state"], "dispatched")
        recovered.uncertain(grant, now=self.now + 7 * 86400)
        self.assertEqual(
            recovered.receipt(self.access, grant)["state"], "needs_attention"
        )
        with self.assertRaises(ProjectConflict):
            ProjectLifecycle(store, self.jobs).request(
                self.access,
                self.pid,
                "stop",
                request_key="later",
                expected_version=self.project()["version"],
                now=self.now + 30 * 86400,
            )
        with self.assertRaises(ProjectConflict):
            recovered.abort(grant)
        recovered.complete(
            grant, {"ok": False, "error": "command failed"}, self.observation(grant)
        )
        self.assertEqual(recovered.receipt(self.access, grant)["state"], "completed")

    def test_incomplete_or_foreign_observation_never_releases_lock(self):
        grant = self.admit()
        self.io.dispatch(self.access, grant)
        evidence = self.observation(grant)
        for changed in (
            {"pending": True},
            {"quiescent": False},
            {"generation": evidence["generation"] + 1},
            {"project_id": self.pid + 1},
            {"sandbox_id": "other"},
            {"fence": evidence["fence"] + 1},
            {"quiescent": 1},
        ):
            with self.assertRaises(ProjectConflict):
                self.io.complete(grant, {"ok": True}, {**evidence, **changed})
            self.assertEqual(self.io.receipt(self.access, grant)["state"], "dispatched")

    def test_revocation_before_dispatch_and_forged_grant_are_rejected(self):
        self.projects.set_member(
            self.access,
            self.pid,
            self.other,
            "editor",
            expected_version=self.project()["version"],
        )
        self.projects.open(self.b, self.pid, now=self.now)
        grant = self.admit(access=self.b)
        self.projects.set_member(
            self.access,
            self.pid,
            self.other,
            None,
            expected_version=self.project()["version"],
        )
        with self.assertRaises(PermissionError):
            self.io.dispatch(self.b, grant)
        with self.assertRaises(PermissionError):
            self.io.dispatch(replace(self.b, member=False), grant)
        with self.assertRaises(PermissionError):
            self.io.abort(replace(grant, token="0" * 32))
        self.io.abort(grant)
        with self.assertRaises(PermissionError):
            self.io.dispatch(self.b, grant)

    def test_scope_account_private_and_actor_boundaries(self):
        grant = self.admit("sandbox_read_file")
        for access in (
            replace(
                self.access,
                scope=replace(self.scope, native_conversation_id="102"),
                admin=True,
            ),
            replace(self.access, scope=replace(self.scope, bot_native_user_id="302")),
            replace(
                self.access,
                scope=replace(self.scope, kind="private", native_conversation_id="201"),
            ),
            self.b,
        ):
            with self.assertRaises(PermissionError):
                self.io.dispatch(access, grant)
            with self.assertRaises(PermissionError):
                self.io.receipt(access, grant)

    def test_focus_expiration_and_changed_workspace_do_not_dispatch(self):
        self.projects.open(self.access, self.pid, now=self.now - 86400)
        with self.assertRaises(ProjectConflict):
            self.admit("sandbox_read_file")
        self.projects.open(self.access, self.pid, now=self.now)
        grant = self.admit("sandbox_read_file")
        with self.projects._transaction() as cursor:
            cursor.execute(
                "UPDATE projects SET workspace_generation=workspace_generation+1 WHERE project_id=?",
                (self.pid,),
            )
        with self.assertRaises(ProjectConflict):
            self.io.dispatch(self.access, grant)

    def test_unmeasured_and_full_capacity_block_writes_but_allow_read(self):
        with self.projects._transaction() as cursor:
            cursor.execute(
                "UPDATE project_usage SET complete=FALSE WHERE project_id=?",
                (self.pid,),
            )
        with self.assertRaises(ProjectQuotaRejected):
            self.admit()
        grant = self.admit("sandbox_read_file")
        self.io.abort(grant)
        self.record(self.sample(self.project()["quota_bytes"]))
        with self.assertRaises(ProjectQuotaRejected):
            self.admit(key="full-write")
        self.admit("sandbox_read_file", key="full-read")

    def test_dependency_threshold_and_shell_reserves_remaining_capacity(self):
        quota = self.project()["quota_bytes"]
        self.record(self.sample(quota * 96 // 100))
        with self.assertRaises(ProjectQuotaRejected) as error:
            self.admit("sandbox_exec", installing_dependencies=True)
        self.assertEqual(error.exception.code, "project_dependency_limit")
        grant = self.admit("sandbox_exec")
        reserved = self.sql.execute(
            "SELECT reserved_bytes FROM project_io WHERE operation_id=?",
            (grant.operation_id,),
        ).fetchone()[0]
        self.assertEqual(reserved, quota - self.project()["used_bytes"])

    def test_concurrent_admission_is_exclusive_across_store_connections(self):
        stores = [self.owned(ProjectStore(self.db, self.root)) for _ in range(2)]
        gate = threading.Barrier(2)

        def reserve(index):
            io = ProjectIOStore(stores[index], ProjectUsageStore(stores[index]))
            gate.wait(timeout=5)
            try:
                return self.admit("sandbox_read_file", key=f"concurrent-{index}", io=io)
            except ProjectConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, range(2)))
        self.assertEqual(sum(item is not None for item in results), 1)

    def test_database_itself_rejects_two_active_rows(self):
        self.admit()
        with self.assertRaises(DatabaseError), self.projects._transaction() as cursor:
            cursor.execute(
                """INSERT INTO project_io(project_id,actor_principal_id,request_key,token,tool_name,
                arguments_sha256,sandbox_id,generation,fence,mutating,reserved_bytes,created_at,updated_at)
                SELECT project_id,actor_principal_id,?,token,tool_name,arguments_sha256,sandbox_id,
                generation,fence,mutating,reserved_bytes,created_at,updated_at FROM project_io WHERE project_id=?""",
                ("f" * 64, self.pid),
            )

    def test_lifecycle_and_io_cannot_both_win_across_connections(self):
        peer = self.owned(ProjectStore(self.db, self.root))
        lifecycle = ProjectLifecycle(peer, self.jobs)
        barrier = threading.Barrier(2)
        revision = self.project()["version"]

        def compete(index):
            barrier.wait(timeout=5)
            try:
                if index:
                    return lifecycle.request(
                        self.access,
                        self.pid,
                        "stop",
                        request_key="race-stop",
                        expected_version=revision,
                        now=self.now,
                    )
                return self.admit("sandbox_read_file")
            except ProjectConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(compete, range(2)))
        self.assertEqual(sum(value is not None for value in results), 1)

    def test_completion_holds_scope_lock_until_usage_is_invalidated(self):
        grant = self.admit()
        self.io.dispatch(self.access, grant)
        evidence = self.observation(grant)
        peer = self.owned(ProjectStore(self.db, self.root))
        completing = ProjectIOStore(peer, ProjectUsageStore(peer))
        owned = completing._owned

        def observe_lock(cursor, value):
            result = owned(cursor, value)
            with self.projects._transaction() as observer:
                acquired = observer.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(?,0))",
                    ("project-scope:" + self.scope.key,),
                ).fetchone()[0]
                self.assertFalse(acquired)
            return result

        with patch.object(completing, "_owned", side_effect=observe_lock):
            completing.complete(grant, {"ok": True}, evidence, now=self.now)
        with self.projects._transaction() as cursor:
            self.assertTrue(
                cursor.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(?,0))",
                    ("project-scope:" + self.scope.key,),
                ).fetchone()[0]
            )
        with self.assertRaises(ProjectQuotaRejected):
            self.admit(key="must-remeasure")

    def test_operation_receipts_block_downgrade_even_when_aborted(self):
        grant = self.admit()
        self.io.abort(grant)
        with patch.dict(
            os.environ,
            {
                "AI_POSTGRES_DSN": self.db.dsn,
                "AI_POSTGRES_SCHEMA": self.db.schema,
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "operation receipts"):
                command.downgrade(Config("alembic.ini"), "0048_project_creation_key")
        self.db.require_revision(HEAD_REVISION)
        self.assertEqual(self.io.receipt(self.access, grant)["state"], "aborted")

    def test_empty_io_table_can_downgrade_and_upgrade(self):
        with patch.dict(
            os.environ,
            {
                "AI_POSTGRES_DSN": self.db.dsn,
                "AI_POSTGRES_SCHEMA": self.db.schema,
            },
        ):
            command.downgrade(Config("alembic.ini"), "0048_project_creation_key")
            self.db.require_revision("0048_project_creation_key")
            command.upgrade(Config("alembic.ini"), "head")
        self.db.require_revision(HEAD_REVISION)
        self.admit()

    def test_unbound_legacy_project_does_not_grant_io(self):
        grant = self.admit("sandbox_read_file")
        with self.projects._transaction() as cursor:
            cursor.execute(
                "UPDATE projects SET bot_native_user_id='' WHERE project_id=?",
                (self.pid,),
            )
        unbound = replace(self.access, scope=replace(self.scope, bot_native_user_id=""))
        for operation in (
            lambda: self.admit("sandbox_read_file", access=unbound),
            lambda: self.io.dispatch(unbound, grant),
            lambda: self.io.receipt(unbound, grant),
        ):
            with self.assertRaises(PermissionError):
                operation()

    def test_operation_identity_and_tool_allowlist_reject_changed_or_host_commands(
        self,
    ):
        self.admit("sandbox_read_file")
        with self.assertRaises(ProjectConflict):
            self.io.admit(
                self.access,
                self.pid,
                "sandbox_read_file",
                request_key=hashlib.sha256(b"write-1").hexdigest(),
                arguments_sha256="0" * 64,
                expected_version=self.project()["version"],
                now=self.now,
            )
        for tool in (
            "browser_navigate",
            "exec_host",
            "sandbox_destroy",
            "project_delete",
        ):
            with self.assertRaises(ValueError):
                self.admit(tool)

    def test_reservation_cannot_complete_without_dispatch(self):
        grant = self.admit()
        with self.assertRaises(ProjectConflict):
            self.io.complete(grant, {"ok": True}, self.observation(grant))
        self.io.abort(grant)
        self.io.abort(grant)
        self.assertEqual(self.io.receipt(self.access, grant)["state"], "aborted")
        with self.assertRaises(ProjectConflict):
            self.io.dispatch(self.access, grant)

    def test_scope_reservations_include_concurrent_other_project_writes(self):
        other = self.projects.create(self.access, "Peer", now=self.now)["project_id"]
        self.lifecycle.request(
            self.access,
            other,
            "wake",
            request_key="peer-wake",
            expected_version=1,
            now=self.now,
        )
        job = self.jobs.claim_due(
            "fixture", kinds=("project.lifecycle",), now=self.now
        )[0]
        while step := self.lifecycle.next_step(job, now=self.now):
            evidence = lifecycle_fixture.ProjectLifecycleTests.evidence(self, step)
            self.lifecycle.acknowledge(job, step, evidence, now=self.now)
        self.lifecycle.finish(job, now=self.now)
        self.record(self.sample(100, pid=other), pid=other)
        with self.projects._transaction() as cursor:
            cursor.execute(
                "UPDATE project_scope_limits SET quota_bytes=1000 WHERE scope_key=?",
                (self.scope.key,),
            )
        self.projects.open(self.access, self.pid, now=self.now)
        first = self.admit(expected_bytes=600)
        self.projects.open(self.access, other, now=self.now)
        with self.assertRaises(ProjectQuotaRejected) as error:
            self.admit(pid=other, key="peer", expected_bytes=600)
        self.assertEqual(error.exception.code, "project_scope_quota_full")
        self.io.abort(first)
        self.admit(pid=other, key="peer", expected_bytes=600)

    def test_snapshot_and_quota_shrink_cannot_spend_peer_reservation(self):
        grant = self.admit(expected_bytes=600)
        other = self.projects.create(self.access, "Peer", now=self.now)["project_id"]
        admin = replace(self.access, admin=True)
        with self.assertRaises(ProjectQuotaExceeded):
            self.projects.set_scope_limits(
                admin,
                quota_bytes=650,
                max_projects=5,
                expected_version=1,
            )
        self.projects.set_scope_limits(
            admin,
            quota_bytes=1000,
            max_projects=5,
            expected_version=1,
        )
        with self.assertRaises(ProjectQuotaExceeded):
            self.projects.snapshot(
                self.access,
                other,
                [ProjectFile("notes", b"x" * 500)],
                expected_version=1,
            )
        self.assertEqual(self.projects.versions(self.access, other), [])
        self.io.abort(grant)
        self.projects.snapshot(
            self.access,
            other,
            [ProjectFile("notes", b"x" * 500)],
            expected_version=1,
        )
        self.assertEqual(len(self.projects.versions(self.access, other)), 1)

    def test_completed_write_keeps_capacity_until_successful_remeasurement(self):
        grant = self.admit(expected_bytes=600)
        self.io.dispatch(self.access, grant)
        self.io.complete(grant, {"ok": True}, self.observation(grant), now=self.now)
        peer = self.projects.create(self.access, "Peer", now=self.now)["project_id"]
        self.projects.set_scope_limits(
            replace(self.access, admin=True),
            quota_bytes=1000,
            max_projects=5,
            expected_version=1,
        )
        self.usage.record(
            self.access,
            self.pid,
            None,
            expected_version=self.project()["version"],
            now=self.now,
            error="TimeoutError",
        )
        with self.assertRaises(ProjectQuotaExceeded):
            self.projects.snapshot(
                self.access,
                peer,
                [ProjectFile("notes", b"x" * 500)],
                expected_version=1,
            )
        with self.projects._transaction() as cursor:
            self.assertEqual(
                self.projects._scope_reserved_bytes(cursor, self.scope.key), 600
            )
        self.record(self.sample(110))
        with self.projects._transaction() as cursor:
            self.assertEqual(
                self.projects._scope_reserved_bytes(cursor, self.scope.key), 0
            )
        self.projects.snapshot(
            self.access,
            peer,
            [ProjectFile("notes", b"x" * 500)],
            expected_version=1,
        )
