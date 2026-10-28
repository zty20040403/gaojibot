"""Preparation journals cannot be discarded by schema rollback."""

import os
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config

from src.bot_storage.schema import HEAD_REVISION
from tests import test_project_task_admission as fixture
from tests.postgres_fixture import postgres_for_test


class TaskAdmissionMigrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ProjectTaskAdmissionTests.asyncSetUp
    setup_admission = fixture.ProjectTaskAdmissionTests.setup_admission
    prepare = fixture.ProjectTaskAdmissionTests.prepare

    def migration_environment(self):
        return patch.dict(os.environ, {"AI_POSTGRES_DSN": os.environ["TEST_POSTGRES_DSN"],
                                       "AI_POSTGRES_SCHEMA": postgres_for_test(self).schema})

    async def test_empty_upgrade_downgrade_round_trip(self):
        with self.migration_environment():
            command.downgrade(Config("alembic.ini"), "0058_project_task_release")
            command.upgrade(Config("alembic.ini"), "head")
        postgres_for_test(self).require_revision(HEAD_REVISION)
        self.prepare()

    async def test_journal_blocks_downgrade_without_losing_lock_or_version(self):
        run = self.prepare()
        with self.migration_environment(), self.assertRaisesRegex(RuntimeError, "preparation journals"):
            command.downgrade(Config("alembic.ini"), "0058_project_task_release")
        postgres_for_test(self).require_revision(HEAD_REVISION)
        self.assertEqual(self.projects.get(self.access, self.pid)["lifecycle_job_id"], run["job_id"])
