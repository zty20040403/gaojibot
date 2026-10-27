from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import nonebot
from alembic import command
from alembic.config import Config

nonebot.init()

from src.bot_storage import open_store_connection
from src.bot_storage.schema import HEAD_REVISION
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.project_blobs import ProjectFile, project_path
from src.plugins.ai_chat.projects import (
    ProjectAccess,
    ProjectConflict,
    ProjectQuotaExceeded,
    ProjectStore,
)
from src.plugins.ai_chat.quota import UsageStore
from src.plugins.ai_chat.routines import RoutineStore
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from src.plugins.ai_chat.workbench_auth import WorkbenchAuthStore
from tests.postgres_fixture import postgres_for_test


class ProjectStoreTests(unittest.TestCase):
    def setUp(self):
        self.db = postgres_for_test(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "objects"
        self.store = self.new_store()
        self.auth = self.owned(WorkbenchAuthStore(self.db))
        self.ledger = self.owned(MessageLedger(self.db))
        self.principal = self.ledger.ensure_principal_identity("onebot-v11", "201", "A")
        self.other = self.ledger.ensure_principal_identity("onebot-v11", "202", "B")
        self.scope = ConversationScope("onebot-v11", "group", "101", "201", "301")
        self.access = ProjectAccess(self.scope, self.principal, member=True)
        self.b = ProjectAccess(self.scope.with_actor("202"), self.other, member=True)
        self.now = int(time.time())
        _, self.sql = open_store_connection(self.db)
        self.addCleanup(self.sql.close)

    def owned(self, value):
        self.addCleanup(value.close)
        return value

    def new_store(self):
        return self.owned(ProjectStore(self.db, self.root))

    def create(self, **kwargs):
        return self.store.create(self.access, "Project", now=self.now, **kwargs)[
            "project_id"
        ]

    def snapshot(self, pid, value=b"first", **kwargs):
        return self.store.snapshot(
            self.access,
            pid,
            [ProjectFile("index.html", value)],
            expected_version=self.store.get(self.access, pid)["version"],
            now=self.now,
            **kwargs,
        )

    def test_group_members_view_but_only_editors_write_and_departure_revokes(self):
        pid = self.create()
        self.assertEqual(self.store.get(self.b, pid)["project_id"], pid)
        with self.assertRaises(PermissionError):
            self.store.update(self.b, pid, expected_version=1, title="no")
        self.store.set_member(
            self.access, pid, self.other, "editor", expected_version=1
        )
        self.store.update(self.b, pid, expected_version=2, title="Edited")
        with self.assertRaises(PermissionError):
            self.store.get(replace(self.b, member=False), pid)
        with self.assertRaises(PermissionError):
            self.store.set_member(
                self.b, pid, self.principal, "editor", expected_version=3
            )
        with self.assertRaises(PermissionError):
            self.store.update(self.b, pid, expected_version=3, quota_bytes=10000)

    def test_cross_group_even_admin_and_private_identity_are_denied(self):
        pid = self.create()
        another = replace(
            self.access,
            scope=replace(self.scope, native_conversation_id="102"),
            admin=True,
        )
        with self.assertRaises(PermissionError):
            self.store.get(another, pid)
        private = replace(
            self.access,
            scope=replace(self.scope, kind="private", native_conversation_id="201"),
        )
        private_id = self.store.create(private, "Private")["project_id"]
        with self.assertRaises(PermissionError):
            self.store.get(replace(private, principal_id=self.other), private_id)
        with self.assertRaises(PermissionError):
            self.store.create(replace(private, principal_id=self.other), "Spoofed")
        with self.assertRaises(PermissionError):
            self.store.get(replace(self.access, principal_id=self.other), pid)

    def test_concurrent_creates_never_exceed_scope_limit(self):
        stores = [self.new_store() for _ in range(8)]

        def create(item):
            index, store = item
            try:
                return store.create(self.access, f"Project {index}")["project_id"]
            except ProjectQuotaExceeded:
                return None

        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(create, enumerate(stores)))
        self.assertEqual(sum(result is not None for result in results), 5)
        self.assertEqual(len(self.store.list(self.access)), 5)

    def test_two_writers_same_revision_only_one_commits(self):
        pid = self.create()
        stores = [self.new_store(), self.new_store()]

        def write(item):
            index, store = item
            try:
                return store.snapshot(
                    self.access,
                    pid,
                    [ProjectFile("file", str(index).encode())],
                    expected_version=1,
                )
            except ProjectConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(write, enumerate(stores)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.store.versions(self.access, pid)), 1)

    def test_identical_snapshots_deduplicate_and_restore_creates_new_history(self):
        pid = self.create()
        first = self.snapshot(pid)
        self.assertEqual(self.snapshot(pid)["version_no"], 1)
        self.snapshot(pid, b"second")
        third = self.store.restore(self.access, pid, 1, expected_version=3)
        self.assertEqual(third["version_no"], 3)
        self.assertEqual(third["manifest_sha256"], first["manifest_sha256"])
        self.assertEqual(
            self.store.read_file(self.access, pid, 3, "index.html"), b"first"
        )
        self.assertEqual(
            self.sql.execute("SELECT count(*) FROM project_blobs").fetchone()[0], 4
        )
        self.assertEqual(
            [v["version_no"] for v in self.store.versions(self.access, pid)], [3, 2, 1]
        )

    def test_content_is_shared_across_projects_but_counted_per_project(self):
        a, b = self.create(), self.create()
        self.snapshot(a)
        once = self.store.usage(self.access)["total_bytes"]
        self.snapshot(b)
        self.assertEqual(
            self.sql.execute("SELECT count(*) FROM project_blobs").fetchone()[0], 2
        )
        self.assertEqual(self.store.usage(self.access)["total_bytes"], once * 2)
        self.assertEqual(
            self.new_store().read_file(self.access, b, 1, "index.html"), b"first"
        )

    def test_label_unchanged_content_without_duplicating_version(self):
        pid = self.create()
        self.snapshot(pid)
        saved = self.snapshot(pid, label="release")
        self.assertEqual((saved["version_no"], saved["label"]), (1, "release"))
        self.assertEqual(self.store.get(self.access, pid)["version"], 3)
        self.assertEqual(
            self.store.events(self.access, pid)[0]["kind"], "version_labelled"
        )

    def test_scope_quota_serializes_competing_project_writes(self):
        a, b = self.create(), self.create()
        self.store.set_scope_limits(
            replace(self.access, admin=True),
            quota_bytes=500,
            max_projects=5,
            expected_version=1,
        )
        stores = [self.new_store(), self.new_store()]

        def write(item):
            pid, store = item
            try:
                return store.snapshot(
                    self.access, pid, [ProjectFile("x", b"a" * 200)], expected_version=1
                )
            except ProjectQuotaExceeded:
                return None

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(write, zip((a, b), stores)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertLessEqual(self.store.usage(self.access)["total_bytes"], 500)

    def test_scope_quota_rejects_atomically_and_orphans_wait_for_grace(self):
        pid = self.create()
        self.store.set_scope_limits(
            replace(self.access, admin=True),
            quota_bytes=100,
            max_projects=5,
            expected_version=1,
        )
        with self.assertRaises(ProjectQuotaExceeded):
            self.snapshot(pid, b"x" * 200)
        self.assertEqual(self.store.versions(self.access, pid), [])
        self.assertEqual(
            self.sql.execute("SELECT count(*) FROM project_blobs").fetchone()[0], 0
        )
        self.assertTrue(list((self.root / "blobs").glob("*/*")))
        self.assertEqual(self.store.collect_blobs(now=self.now), [])
        self.assertGreater(len(self.store.collect_blobs(now=self.now + 86402)), 0)
        self.assertEqual(list((self.root / "blobs").glob("*/*")), [])

    def test_gc_preserves_shared_references_and_resets_grace_after_last_reference(self):
        a, b = self.create(), self.create()
        self.snapshot(a, b"shared")
        self.snapshot(b, b"shared")
        self.snapshot(a, b"new-a")
        digest = hashlib.sha256(b"shared").hexdigest()
        self.store.prune_versions(self.access, a, [], expected_version=3, now=self.now)
        self.assertNotIn(digest, self.store.collect_blobs(now=self.now + 90000))
        self.assertEqual(
            self.store.read_file(self.access, b, 1, "index.html"), b"shared"
        )
        self.snapshot(b, b"new-b")
        self.store.prune_versions(
            self.access, b, [], expected_version=3, now=self.now + 90000
        )
        self.assertEqual(self.store.collect_blobs(now=self.now + 90001), [])
        self.assertIn(digest, self.store.collect_blobs(now=self.now + 180000))

    def test_republishing_unreferenced_content_prevents_gc(self):
        pid = self.create()
        self.snapshot(pid)
        self.snapshot(pid, b"new")
        self.store.prune_versions(
            self.access, pid, [], expected_version=3, now=self.now
        )
        self.snapshot(pid)
        self.store.collect_blobs(now=self.now + 90000)
        self.assertEqual(
            self.store.read_file(self.access, pid, 3, "index.html"), b"first"
        )

    def test_interrupted_staging_uploads_only_removed_after_grace(self):
        old = self.root / "staging" / "upload-abcdefgh"
        old.write_bytes(b"interrupted")
        fresh = self.root / "staging" / "upload-ijklmnop"
        fresh.write_bytes(b"fresh")
        os.utime(old, (self.now - 90000, self.now - 90000))
        unknown = self.root / "staging" / "keep.txt"
        unknown.write_bytes(b"unknown")
        link = self.root / "staging" / "upload-qrstuvwx"
        link.symlink_to(unknown)
        self.store.collect_blobs(now=self.now)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(link.is_symlink())
        self.assertEqual(unknown.read_bytes(), b"unknown")

    def test_pinned_and_labelled_versions_and_latest_are_never_pruned(self):
        pid = self.create()
        self.snapshot(pid, label="release")
        self.snapshot(pid, b"pin")
        self.store.pin_version(self.access, pid, 2, pinned=True, expected_version=3)
        self.snapshot(pid, b"disposable")
        self.snapshot(pid, b"latest")
        removed = self.store.prune_versions(self.access, pid, [], expected_version=6)
        self.assertEqual(removed, [3])
        self.assertEqual(
            [v["version_no"] for v in self.store.versions(self.access, pid)], [4, 2, 1]
        )

    def test_scope_and_user_specific_focus_expires_without_polling_extending_it(self):
        a, b = self.create(), self.create()
        self.store.open(self.access, a, now=self.now)
        self.store.open(self.b, b, now=self.now)
        self.assertEqual(
            self.store.current(self.access, now=self.now + 100)["project_id"], a
        )
        self.assertEqual(
            self.store.current(self.b, now=self.now + 100)["project_id"], b
        )
        self.assertIsNone(self.store.current(self.access, now=self.now + 86400))

    def test_editing_refreshes_own_focus_not_other_members_focus(self):
        pid = self.create()
        self.store.open(self.b, pid, now=self.now)
        self.store.snapshot(
            self.access,
            pid,
            [ProjectFile("x", b"content")],
            expected_version=1,
            now=self.now + 86000,
        )
        self.assertIsNotNone(self.store.current(self.access, now=self.now + 86500))
        self.assertIsNone(self.store.current(self.b, now=self.now + 86500))

    def test_malicious_paths_modes_and_prefix_collisions_cannot_publish(self):
        pid = self.create()
        for path in (
            "../secret",
            "/etc/passwd",
            "a//b",
            "C:/secret",
            "a\\b",
            "NUL.txt",
            "a\x00b",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                project_path(path)
        for files in (
            [ProjectFile("x", b"a", 0o4755)],
            [ProjectFile("x", b"a"), ProjectFile("x/y", b"b")],
            [ProjectFile("a", b"1"), ProjectFile("A", b"2")],
        ):
            with self.assertRaises(ValueError):
                self.store.snapshot(self.access, pid, files, expected_version=1)
        self.assertEqual(self.store.versions(self.access, pid), [])

    def test_file_size_limit_integrity_and_symlink_guards(self):
        pid = self.create()
        with (
            patch("src.plugins.ai_chat.project_blobs.MAX_FILE_BYTES", 10),
            self.assertRaises(ValueError),
        ):
            self.snapshot(pid, b"x" * 11)
        self.snapshot(pid)
        digest = hashlib.sha256(b"first").hexdigest()
        path = self.store.blobs.path(digest)
        path.chmod(0o600)
        path.write_bytes(b"bad")
        with self.assertRaises(ValueError):
            self.store.read_file(self.access, pid, 1, "index.html")
        path.unlink()
        path.symlink_to(self.root / "outside")
        with self.assertRaises(OSError):
            self.store.read_file(self.access, pid, 1, "index.html")
        self.assertEqual(list((self.root / "staging").iterdir()), [])

    def test_login_credentials_are_hashed_one_use_expiring_and_revocable(self):
        login = self.auth.issue(self.access, now=self.now)
        row = dict(self.sql.execute("SELECT * FROM workbench_login_codes").fetchone())
        self.assertNotIn(login.token, str(row))
        self.assertNotIn(login.token, repr(login))
        session = self.auth.consume(login.token, now=self.now + 1)
        self.assertEqual(session.principal_id, self.principal)
        self.assertEqual(session.scope_key, self.scope.key)
        with self.assertRaises(PermissionError):
            self.auth.consume(login.token, now=self.now + 2)
        self.assertEqual(
            self.owned(WorkbenchAuthStore(self.db)).resolve(
                session.token, csrf_token=session.csrf_token, now=self.now + 2
            )["principal_id"],
            self.principal,
        )
        with self.assertRaises(PermissionError):
            self.auth.resolve(session.token, csrf_token="x" * 43, now=self.now + 2)
        with self.assertRaises(PermissionError):
            self.auth.resolve(login.token, now=self.now + 2)
        self.auth.revoke(session.token, now=self.now + 3)
        with self.assertRaises(PermissionError):
            self.auth.resolve(session.token, now=self.now + 4)
        expired = self.auth.issue(self.access, now=self.now)
        with self.assertRaises(PermissionError):
            self.auth.consume(expired.token, now=self.now + 600)

    def test_concurrent_login_consumers_only_one_wins(self):
        login = self.auth.issue(self.access)
        stores = [self.owned(WorkbenchAuthStore(self.db)) for _ in range(4)]

        def consume(store):
            try:
                return store.consume(login.token)
            except PermissionError:
                return None

        with ThreadPoolExecutor(max_workers=4) as workers:
            sessions = list(workers.map(consume, stores))
        self.assertEqual(sum(session is not None for session in sessions), 1)
        self.assertEqual(
            self.sql.execute("SELECT count(*) FROM workbench_sessions").fetchone()[0], 1
        )

    def test_new_login_link_revokes_previous_and_departure_cannot_issue(self):
        old = self.auth.issue(self.access)
        self.auth.issue(self.access)
        with self.assertRaises(PermissionError):
            self.auth.consume(old.token)
        with self.assertRaises(PermissionError):
            self.auth.issue(replace(self.access, member=False))

    def test_sessions_expire_and_revoking_principal_cannot_race_login(self):
        old = self.auth.consume(
            self.auth.issue(self.access, now=self.now).token, now=self.now
        )
        with self.assertRaises(PermissionError):
            self.auth.resolve(old.token, now=self.now + 7 * 86400)
        login = self.auth.issue(self.access, now=self.now)
        consumer = self.owned(WorkbenchAuthStore(self.db))
        revoker = self.owned(WorkbenchAuthStore(self.db))
        start = threading.Barrier(2)

        def consume():
            start.wait(timeout=5)
            try:
                return consumer.consume(login.token, now=self.now + 1)
            except PermissionError:
                return None

        def revoke():
            start.wait(timeout=5)
            revoker.revoke_principal(self.principal, now=self.now + 1)

        with ThreadPoolExecutor(max_workers=2) as workers:
            first, second = workers.submit(consume), workers.submit(revoke)
            session = first.result(timeout=10)
            second.result(timeout=10)
        self.assertEqual(
            self.sql.execute(
                "SELECT count(*) FROM workbench_sessions WHERE revoked_at IS NULL"
            ).fetchone()[0],
            0,
        )
        if session:
            with self.assertRaises(PermissionError):
                self.auth.resolve(session.token, now=self.now + 2)
        with self.assertRaises(PermissionError):
            self.auth.consume(login.token, now=self.now + 2)

    def test_pending_routine_uses_existing_queue_only_after_exact_confirmation(self):
        jobs = self.owned(DurableJobStore(self.db))
        store = self.owned(
            RoutineStore(
                self.db,
                jobs=jobs,
                outbox=self.owned(DeliveryStore(self.db, recover_interrupted=False)),
                usage=self.owned(UsageStore(self.db)),
            )
        )
        pid = self.create()
        draft = store.create_pending(
            self.scope,
            name="draft",
            prompt="Continue this project",
            project_id=pid,
            access=self.access,
            cron="* * * * *",
            now=self.now,
        )
        self.assertFalse(draft.enabled)
        self.assertEqual(draft.creator_principal_id, self.principal)
        self.assertEqual(store.materialize_due(now=self.now + 120), [])
        with self.assertRaises(PermissionError):
            store.confirm(
                self.b.scope, draft.routine_id, expected_version=1, now=self.now
            )
        with self.assertRaises(ValueError):
            store.confirm(
                self.scope,
                draft.routine_id,
                expected_version=2,
                access=self.access,
                now=self.now,
            )
        with self.assertRaises(ValueError):
            store.set(
                self.scope,
                name="draft",
                prompt=draft.prompt,
                cron=draft.cron,
                expected_version=1,
            )
        confirmed = store.confirm(
            self.scope,
            draft.routine_id,
            expected_version=1,
            access=self.access,
            now=self.now,
        )
        self.assertEqual(confirmed.state, "active")
        with self.assertRaisesRegex(ValueError, "confirmation/update"):
            store.set(
                self.scope,
                name=draft.name,
                prompt="Changed through legacy entry",
                cron=draft.cron,
                expected_version=confirmed.version,
            )
        self.assertEqual(len(store.materialize_due(now=confirmed.next_due_at)), 1)
        self.assertEqual(store.materialize_due(now=confirmed.next_due_at), [])

    def test_watch_definition_persists_without_accidental_cron_execution(self):
        store = self.owned(
            RoutineStore(
                self.db,
                jobs=self.owned(DurableJobStore(self.db)),
                outbox=self.owned(DeliveryStore(self.db, recover_interrupted=False)),
                usage=self.owned(UsageStore(self.db)),
            )
        )
        draft = store.create_pending(
            self.scope,
            name="watch",
            prompt="Tell me about changes",
            routine_kind="watch",
            spec={"source": "webpage", "url": "https://example.org/"},
            now=self.now,
        )
        confirmed = store.confirm(
            self.scope, draft.routine_id, expected_version=1, now=self.now
        )
        self.assertEqual(confirmed.spec, draft.spec)
        self.assertIsNotNone(
            self.sql.execute(
                "SELECT * FROM watch_state WHERE routine_id=?", (draft.routine_id,)
            ).fetchone()
        )
        self.assertEqual(store.materialize_due(now=self.now + 86400), [])

    def test_cancelled_draft_cannot_be_confirmed_or_reenabled_by_legacy_set(self):
        store = self.owned(
            RoutineStore(
                self.db,
                jobs=self.owned(DurableJobStore(self.db)),
                outbox=self.owned(DeliveryStore(self.db, recover_interrupted=False)),
                usage=self.owned(UsageStore(self.db)),
            )
        )
        draft = store.create_pending(
            self.scope, name="cancelled", prompt="Do not execute"
        )
        self.assertTrue(store.cancel(self.scope, draft.routine_id, expected_version=1))
        self.assertFalse(store.cancel(self.scope, draft.routine_id, expected_version=2))
        with self.assertRaises(ValueError):
            store.confirm(self.scope, draft.routine_id, expected_version=2)
        with self.assertRaises(ValueError):
            store.set(
                self.scope,
                name=draft.name,
                prompt=draft.prompt,
                cron=draft.cron,
                expected_version=2,
            )
        self.assertEqual(store.materialize_due(now=self.now + 86400), [])

    def test_scope_limit_applies_to_legacy_and_pending_routines_together(self):
        jobs = self.owned(DurableJobStore(self.db))
        outbox = self.owned(DeliveryStore(self.db, recover_interrupted=False))
        usage = self.owned(UsageStore(self.db))
        stores = [
            self.owned(RoutineStore(self.db, jobs=jobs, outbox=outbox, usage=usage))
            for _ in range(12)
        ]

        def create(item):
            index, store = item
            scope = self.scope.with_actor(str(1000 + index))
            try:
                if index % 2:
                    return store.create_pending(scope, name="draft", prompt="Test")
                return store.set(scope, name="active", prompt="Test", cron="0 9 * * *")
            except ValueError as error:
                self.assertIn("limit reached", str(error))
                return None

        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(create, enumerate(stores)))
        self.assertEqual(sum(result is not None for result in results), 10)


class ProjectMigrationTests(unittest.TestCase):
    def setUp(self):
        self.db = postgres_for_test(self)
        _, self.sql = open_store_connection(self.db)
        self.addCleanup(self.sql.close)

    def migrate(self, operation, revision):
        with patch.dict(
            os.environ, AI_POSTGRES_DSN=self.db.dsn, AI_POSTGRES_SCHEMA=self.db.schema
        ):
            operation(Config("alembic.ini"), revision)

    def test_upgrade_backfills_legacy_routines_and_empty_downgrade_is_reversible(self):
        ledger = MessageLedger(self.db)
        try:
            principal = ledger.ensure_principal_identity("onebot-v11", "201", "A")
        finally:
            ledger.close()
        self.migrate(command.downgrade, "0042_mcp_continuations")
        for name, enabled in (("active", True), ("paused", False)):
            self.sql.execute(
                """INSERT INTO agent_routines
                (scope_key,platform,kind,conversation_id,bot_id,creator_id,name,prompt,cron,timezone,
                 enabled,version,max_tokens,daily_tokens,daily_runs,next_due_at,created_at,updated_at)
                VALUES ('onebot-v11:group:101','onebot-v11','group','101','301','201',?,'Legacy',
                 '0 9 * * *','Asia/Shanghai',?,1,2000,4000,2,100,1,1)""",
                (name, enabled),
            )
        self.sql.commit()
        self.migrate(command.upgrade, HEAD_REVISION)
        self.migrate(command.upgrade, HEAD_REVISION)
        rows = self.sql.execute(
            "SELECT name,state,enabled,creator_principal_id FROM agent_routines ORDER BY name"
        ).fetchall()
        self.assertEqual(
            [
                tuple(
                    row[key]
                    for key in ("name", "state", "enabled", "creator_principal_id")
                )
                for row in rows
            ],
            [
                ("active", "active", True, principal),
                ("paused", "paused", False, principal),
            ],
        )
        self.migrate(command.downgrade, "0042_mcp_continuations")
        self.assertEqual(
            self.sql.execute("SELECT count(*) FROM agent_routines").fetchone()[0], 2
        )
        self.migrate(command.upgrade, HEAD_REVISION)
        self.db.require_revision(HEAD_REVISION)

    def test_downgrade_refuses_retained_data_without_partial_ddl(self):
        ledger = MessageLedger(self.db)
        try:
            principal = ledger.ensure_principal_identity("onebot-v11", "201", "A")
        finally:
            ledger.close()
        access = ProjectAccess(
            ConversationScope("onebot-v11", "group", "101", "201"),
            principal,
            member=True,
        )
        with tempfile.TemporaryDirectory() as temporary:
            store = ProjectStore(self.db, Path(temporary))
            try:
                project = store.create(access, "Retained")
                with self.assertRaisesRegex(RuntimeError, "Export retained"):
                    self.migrate(command.downgrade, "0042_mcp_continuations")
                self.db.require_revision(HEAD_REVISION)
                self.assertEqual(
                    store.get(access, project["project_id"])["title"], "Retained"
                )
                self.assertEqual(
                    self.sql.execute(
                        "SELECT count(*) FROM workbench_sessions"
                    ).fetchone()[0],
                    0,
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
