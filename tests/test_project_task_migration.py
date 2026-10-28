"""Run separately from runtime suites: Alembic changes global logging state."""

import os
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config

from src.bot_storage import DatabaseError
from src.bot_storage.schema import HEAD_REVISION
from tests import test_project_tasks as fixture


class ProjectTaskMigrationTests(unittest.TestCase):
    setUp = fixture.ProjectTaskTests.setUp
    owned = fixture.ProjectTaskTests.owned
    new_store = fixture.ProjectTaskTests.new_store
    create = fixture.ProjectTaskTests.create
    snapshot = fixture.ProjectTaskTests.snapshot
    prepare = fixture.ProjectTaskTests.prepare

    def migrate(self, method, target):
        with patch.dict(os.environ, {"AI_POSTGRES_DSN": os.environ["TEST_POSTGRES_DSN"],
                                     "AI_POSTGRES_SCHEMA": self.db.schema}):
            method(Config("alembic.ini"), target)

    def test_empty_upgrade_downgrade_preserves_existing_project_and_version(self):
        self.migrate(command.downgrade, "0054_project_browser")
        self.db.require_revision("0054_project_browser")
        self.assertEqual(self.store.read_file(self.access, self.pid, 1, "index.html"), b"first")
        self.migrate(command.upgrade, HEAD_REVISION)
        self.migrate(command.upgrade, HEAD_REVISION)
        self.assertEqual(self.prepare().project_base_version, 1)

    def test_bound_task_prevents_downgrade_and_baseline_deletion(self):
        task = self.prepare()
        with self.assertRaisesRegex(RuntimeError, "Export project task"):
            self.migrate(command.downgrade, "0054_project_browser")
        self.db.require_revision(HEAD_REVISION)
        with self.assertRaises(DatabaseError), self.tasks._transaction() as cursor:
            cursor.execute("DELETE FROM project_versions WHERE project_id=?", (self.pid,))
        self.assertEqual(self.binding.read_baseline_file(self.access, task.task_id, "index.html")["content"], b"first")


if __name__ == "__main__":
    unittest.main()
