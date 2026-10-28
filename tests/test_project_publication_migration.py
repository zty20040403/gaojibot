"""Publication migrations run separately from runtime logging assertions."""

import unittest

from alembic import command

from src.bot_storage import DatabaseError
from src.bot_storage.schema import HEAD_REVISION
from tests import test_project_publication as fixture
from tests import test_project_task_migration as migration_fixture


class ProjectPublicationMigrationTests(unittest.TestCase):
    setUp = fixture.ProjectPublicationTests.setUp
    owned = fixture.ProjectPublicationTests.owned
    new_store = fixture.ProjectPublicationTests.new_store
    create = fixture.ProjectPublicationTests.create
    snapshot = fixture.ProjectPublicationTests.snapshot
    prepare = fixture.ProjectPublicationTests.prepare
    count = fixture.ProjectPublicationTests.count
    accepted = fixture.ProjectPublicationTests.accepted
    event = fixture.ProjectPublicationTests.event
    publish = fixture.ProjectPublicationTests.publish
    migrate = migration_fixture.ProjectTaskMigrationTests.migrate

    def test_empty_publication_table_roundtrip_preserves_bound_task(self):
        self.migrate(command.downgrade, "0055_project_tasks")
        self.db.require_revision("0055_project_tasks")
        self.assertEqual(self.binding.baseline(self.access, self.task.task_id)["version_no"], 1)
        self.migrate(command.upgrade, HEAD_REVISION)
        self.migrate(command.upgrade, HEAD_REVISION)
        self.accepted()
        self.assertEqual(self.publish()["version_no"], 2)

    def test_publication_receipt_blocks_destructive_downgrade_and_version_deletion(self):
        self.accepted()
        self.publish()
        with self.assertRaisesRegex(RuntimeError, "Export project task publications"):
            self.migrate(command.downgrade, "0055_project_tasks")
        self.db.require_revision(HEAD_REVISION)
        with self.assertRaises(DatabaseError), self.tasks._transaction() as cursor:
            cursor.execute("DELETE FROM project_versions WHERE project_id=? AND version_no=2", (self.pid,))
        self.assertEqual(self.store.read_file(self.access, self.pid, 2, "index.html"), b"updated")


if __name__ == "__main__":
    unittest.main()
