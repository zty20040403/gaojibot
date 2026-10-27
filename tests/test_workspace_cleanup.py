from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import nonebot

os.environ.setdefault("AI_SUBAGENTS_ENABLED", "false")
nonebot.init()

from src.plugins.ai_chat.agent.workspace_cleanup import prune_task_workspaces, schedule_cleanup
from src.plugins.ai_chat.agent.workspaces import prune_acknowledged_artifacts


class WorkspaceCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.digest = "a" * 64
        self.artifact = self.root / "1" / self.digest
        self.artifact.parent.mkdir()
        self.artifact.write_bytes(b"delivered zip")
        self.task = SimpleNamespace(task_id=1, conversation_id="group:10:user:20", status="completed", finished_at=100)
        self.store = Mock()
        self.store.get.return_value = self.task
        self.store.control.return_value = {"revision": 1}
        self.store.runs.return_value = [SimpleNamespace(step_key="code")]
        self.coordinator = Mock(store=self.store)
        self.coordinator._artifact_retention_state.return_value = (True, (self.digest,))
        self.manager = Mock(reclaim_stopped=AsyncMock())
        self.owner = "group:10:user:20:task#1/code"
        self.marker = self.root / ".workspace-cleanup" / "1.json"
        self.schedule()

    def schedule(self, *, now=100, seconds=3600):
        with patch("src.plugins.ai_chat.agent.workspace_cleanup.time.time", return_value=now):
            schedule_cleanup(self.root, task_id=1, revision=1, finished_at=100,
                sandboxes=[{"owner": self.owner, "sandbox_id": "sabc123"}],
                snapshots=(self.digest,), retention_seconds=seconds)

    async def prune(self, now=3700):
        return await prune_task_workspaces(self.root, self.manager, self.coordinator, now=now)

    async def test_delivered_workspace_and_snapshot_expire_together_in_one_hour(self):
        self.assertEqual(await self.prune(3699), (0, 0))
        self.manager.reclaim_stopped.assert_not_awaited()
        self.assertTrue(self.artifact.exists())
        self.assertEqual(await self.prune(), (1, 0))
        call = self.manager.reclaim_stopped.await_args
        self.assertEqual(call.args, (self.owner, "sabc123"))
        self.assertTrue(call.kwargs["eligible"]())
        self.assertFalse(self.artifact.exists())
        self.assertFalse(self.marker.exists())
        self.store.append_event.assert_called_once()

    async def test_repeated_finalization_cannot_extend_deadline(self):
        self.schedule(now=3600, seconds=7 * 86400)
        self.assertEqual(json.loads(self.marker.read_text())["delete_after"], 3700)
        self.assertEqual(await self.prune(), (1, 0))

    async def test_zero_retention_reclaims_on_next_sweep(self):
        self.schedule(seconds=0)
        self.assertEqual(await self.prune(100), (1, 0))

    async def test_active_failed_partial_and_cancelled_tasks_are_not_deleted(self):
        for status in ("running", "queued", "failed", "partial", "cancelled"):
            with self.subTest(status=status):
                self.schedule()
                self.task.status = status
                self.assertEqual(await self.prune(), (0, 0))
                self.assertTrue(self.artifact.exists())
        self.manager.reclaim_stopped.assert_not_awaited()

    async def test_revised_task_invalidates_old_snapshot_deadline(self):
        retention = self.root / ".retention" / "1" / f"{self.digest}.json"
        retention.parent.mkdir(parents=True)
        retention.write_text(json.dumps({"task_id": 1, "sha256": self.digest, "acknowledged_at": 100, "delete_after": 3700}))
        self.store.control.return_value = {"revision": 2}
        self.assertEqual(await self.prune(), (0, 0))
        self.assertFalse(retention.exists())
        prune_acknowledged_artifacts(self.root, now=4000)
        self.assertTrue(self.artifact.exists())
        self.manager.reclaim_stopped.assert_not_awaited()

    async def test_unconfirmed_delivery_is_not_deleted(self):
        self.coordinator._artifact_retention_state.return_value = (False, ())
        self.assertEqual(await self.prune(), (0, 0))
        self.manager.reclaim_stopped.assert_not_awaited()
        self.assertTrue(self.artifact.exists())

    async def test_cleanup_failure_preserves_ticket_and_snapshot_for_retry(self):
        self.manager.reclaim_stopped.side_effect = RuntimeError("Docker unavailable")
        self.assertEqual(await self.prune(), (0, 1))
        self.assertTrue(self.marker.exists())
        self.assertTrue(self.artifact.exists())
        self.manager.reclaim_stopped.side_effect = None
        self.assertEqual(await self.prune(), (1, 0))

    async def test_ticket_cannot_delete_another_users_workspace(self):
        payload = json.loads(self.marker.read_text())
        payload["sandboxes"][0]["owner"] = "group:999:user:999:task#1/code"
        self.marker.write_text(json.dumps(payload))
        self.assertEqual(await self.prune(), (0, 1))
        self.manager.reclaim_stopped.assert_not_awaited()
        self.assertTrue(self.artifact.exists())

    async def test_legacy_seven_day_snapshot_ticket_obeys_one_hour_cap(self):
        self.marker.unlink()
        retention = self.root / ".retention" / "1" / f"{self.digest}.json"
        retention.parent.mkdir(parents=True)
        retention.write_text(json.dumps({"task_id": 1, "sha256": self.digest,
            "acknowledged_at": 100, "delete_after": 100 + 7 * 86400}))
        self.assertEqual(prune_acknowledged_artifacts(self.root, now=3699), (0, 0))
        self.assertEqual(prune_acknowledged_artifacts(self.root, now=3700), (1, 0))
