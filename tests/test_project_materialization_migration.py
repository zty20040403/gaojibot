"""Run Alembic checks separately because it reconfigures global logging."""

import unittest

from alembic import command
import nonebot

nonebot.init()

from src.bot_storage import DatabaseError
from src.bot_storage.schema import HEAD_REVISION
from src.plugins.ai_chat.project_lifecycle import ProjectLifecycle
from src.plugins.ai_chat.project_materialization import ProjectMaterializations
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from tests import test_project_publication_migration as fixture


class MaterializationMigrationTests(unittest.TestCase):
    setUp = fixture.ProjectPublicationMigrationTests.setUp
    owned = fixture.ProjectPublicationMigrationTests.owned
    new_store = fixture.ProjectPublicationMigrationTests.new_store
    create = fixture.ProjectPublicationMigrationTests.create
    snapshot = fixture.ProjectPublicationMigrationTests.snapshot
    prepare = fixture.ProjectPublicationMigrationTests.prepare
    count = fixture.ProjectPublicationMigrationTests.count
    accepted = fixture.ProjectPublicationMigrationTests.accepted
    event = fixture.ProjectPublicationMigrationTests.event
    publish = fixture.ProjectPublicationMigrationTests.publish
    migrate = fixture.ProjectPublicationMigrationTests.migrate

    def request(self):
        lifecycle = ProjectLifecycle(self.store, self.owned(DurableJobStore(self.db)))
        return ProjectMaterializations(self.publisher, lifecycle).request(
            self.access, self.task.task_id, preflight=lambda plan: None)

    def test_empty_roundtrip_preserves_publication_and_can_queue_after_upgrade(self):
        self.accepted()
        self.publish()
        self.migrate(command.downgrade, "0056_project_publish")
        self.db.require_revision("0056_project_publish")
        self.assertEqual(self.store.read_file(self.access, self.pid, 2, "index.html"), b"updated")
        self.migrate(command.upgrade, HEAD_REVISION)
        self.migrate(command.upgrade, HEAD_REVISION)
        self.request()
        self.assertEqual(self.count("project_task_materializations"), 1)

    def test_inflight_receipt_blocks_downgrade_and_lifecycle_deletion(self):
        self.accepted()
        self.publish()
        run = self.request()
        with self.assertRaisesRegex(RuntimeError, "Export project task materializations"):
            self.migrate(command.downgrade, "0056_project_publish")
        self.db.require_revision(HEAD_REVISION)
        with self.assertRaises(DatabaseError), self.tasks._transaction() as cursor:
            cursor.execute("DELETE FROM project_lifecycle_runs WHERE job_id=?", (run["job_id"],))
        self.assertEqual(self.request()["job_id"], run["job_id"])


if __name__ == "__main__":
    unittest.main()
