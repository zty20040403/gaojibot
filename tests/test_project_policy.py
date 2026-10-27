from dataclasses import replace
from datetime import datetime, timezone
import unittest

import nonebot

nonebot.init()

from src.plugins.ai_chat.project_policy import DAY, ProjectPolicy


class ProjectPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = ProjectPolicy()
        self.project = {
            "status": "active",
            "last_active_at": 100,
            "workspace_generation": 1,
            "lifecycle_job_id": None,
            "locked_task_id": None,
            "pinned": False,
        }

    def test_thresholds_use_actual_activity_not_state_change_time(self):
        for state, action, interval in (
            ("active", "stop", 1800),
            ("stopped", "hibernate", 7 * DAY),
            ("hibernated", "archive", 60 * DAY),
        ):
            project = {
                **self.project,
                "status": state,
                "state_changed_at": 100 + interval - 1,
            }
            self.assertIsNone(self.policy.idle_action(project, now=99 + interval))
            self.assertEqual(
                self.policy.idle_action(project, now=100 + interval), action
            )

    def test_busy_unmaterialized_future_and_terminal_projects_are_skipped(self):
        for change in (
            {"workspace_generation": 0},
            {"lifecycle_job_id": 5},
            {"locked_task_id": 9},
            {"last_active_at": 999999999},
            {"status": "trash"},
            {"status": "purged"},
            {"status": "archived"},
        ):
            self.assertIsNone(
                self.policy.idle_action({**self.project, **change}, now=100 * DAY)
            )

    def test_pinning_only_exempts_archive_and_configuration_is_validated(self):
        project = {**self.project, "pinned": True}
        self.assertEqual(self.policy.idle_action(project, now=100 * DAY), "stop")
        self.assertIsNone(
            self.policy.idle_action({**project, "status": "hibernated"}, now=100 * DAY)
        )
        for values in (
            {"keep_recent": 0},
            {"keep_recent": True},
            {"idle_stop_seconds": -1},
            {"hibernate_seconds": 2},
            {"archive_seconds": 7 * DAY},
        ):
            with self.assertRaises(ValueError):
                replace(self.policy, **values)

    @staticmethod
    def version(number, when, **kwargs):
        return {
            "version_no": number,
            "created_at": when,
            "pinned": False,
            "label": "",
            **kwargs,
        }

    def test_recent_allowance_excludes_permanent_versions_and_keeps_daily_representative(
        self,
    ):
        now = 200 * DAY + 3600
        rows = [self.version(i, now - 30 + i) for i in range(1, 16)]
        rows += [
            self.version(16, now, label="release"),
            self.version(17, now, pinned=True),
        ]
        self.assertEqual(self.policy.retain_versions(rows, now=now), set(range(5, 18)))
        self.assertEqual(
            self.policy.retain_versions(reversed(rows), now=now), set(range(5, 18))
        )

    def test_utc_daily_weekly_buckets_and_future_clock_protection(self):
        def stamp(value):
            return int(
                datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()
            )

        now = stamp("2026-02-15T12:00:00")
        rows = [
            self.version(1, stamp("2025-12-29T10:00:00")),
            self.version(
                2, stamp("2026-01-03T10:00:00")
            ),  # Same ISO week across years.
            self.version(3, stamp("2026-01-06T10:00:00")),
            self.version(4, stamp("2026-01-20T10:00:00")),
            self.version(5, stamp("2026-01-20T23:59:59")),
            self.version(6, stamp("2026-01-21T00:00:00")),
            self.version(7, now + 100),
            self.version(8, now + 50),
            self.version(
                9, stamp("2025-12-28T10:00:00")
            ),  # Latest by version despite clock change.
        ]
        self.assertEqual(
            replace(self.policy, keep_recent=1).retain_versions(rows, now=now),
            {2, 3, 5, 6, 7, 8, 9},
        )

    def test_archived_projects_keep_latest_permanent_and_future_versions(self):
        rows = [self.version(i, 100) for i in range(1, 6)]
        rows[0]["label"] = "initial"
        rows[1]["pinned"] = True
        rows[2]["created_at"] = 999
        self.assertEqual(
            self.policy.retain_versions(rows, now=200, archived=True), {1, 2, 3, 5}
        )
        self.assertEqual(self.policy.retain_versions([], now=200), set())
