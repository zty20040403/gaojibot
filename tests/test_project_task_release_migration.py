"""Run schema downgrade checks separately from tests asserting runtime logs."""

import unittest

from alembic import command

from src.bot_storage import DatabaseError
from src.bot_storage.schema import HEAD_REVISION
from tests import test_project_task_reconciliation as fixture
from tests import test_project_task_migration as migration_fixture


class ProjectTaskReleaseMigrationTests(fixture.ReleaseFixture, unittest.TestCase):
    setUp = fixture.ReleaseFixture.setup_release
    migrate = migration_fixture.ProjectTaskMigrationTests.migrate

    def test_empty_roundtrip_preserves_task_ownership_and_version(self):
        self.migrate(command.downgrade, "0057_project_materialize")
        self.db.require_revision("0057_project_materialize")
        self.assertEqual(self.store.get(self.access, self.pid)["locked_task_id"], self.task.task_id)
        self.assertEqual(self.store.read_file(self.access, self.pid, 1, "index.html"), b"first")
        self.migrate(command.upgrade, HEAD_REVISION)
        self.migrate(command.upgrade, HEAD_REVISION)
        self.release()
        self.assertEqual(self.count("project_task_releases"), 1)

    def test_release_receipt_blocks_destructive_downgrade_and_binding_deletion(self):
        receipt = self.release()
        with self.assertRaisesRegex(RuntimeError, "Export project task releases"):
            self.migrate(command.downgrade, "0057_project_materialize")
        self.db.require_revision(HEAD_REVISION)
        with self.assertRaises(DatabaseError), self.tasks._transaction() as cursor:
            cursor.execute("DELETE FROM project_task_bindings WHERE task_id=?", (self.task.task_id,))
        self.assertEqual(self.release(), receipt)


if __name__ == "__main__":
    unittest.main()
