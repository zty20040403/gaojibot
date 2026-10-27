from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.postgres_fixture import postgres_for_test
from tests.test_subagent_v2 import decision, profile
from src.plugins.ai_chat.agent import ContextPacket
from src.plugins.ai_chat.agent.execution import EntryDecision
from src.plugins.ai_chat.model_catalog import ModelCatalog
from src.plugins.ai_chat.subagents import SubAgentCoordinator, SubAgentStore


class SubAgentPreparationTests(unittest.TestCase):
    def setUp(self):
        self.store = SubAgentStore(postgres_for_test(self))
        self.addCleanup(self.store.close)
        self.packet = ContextPacket("group:1", "group:1:user:2", 2, 3, "test project")
        self.entry = EntryDecision.parse(decision("workflow"))
        selected = profile("qwen-local")
        self.coordinator = SubAgentCoordinator(self.store,
            ModelCatalog({selected.name: selected}, default_profile=selected.name), logger=Mock())
        self.enqueue = Mock()
        self.coordinator.dispatcher = SimpleNamespace(enqueue=self.enqueue)

    def submit(self):
        return self.coordinator.submit(packet=self.packet, decision=self.entry,
            dispatch={"bot_id": "123", "event": {"user_id": 2}, "profile": "qwen-local"})

    def counts(self, cursor=None):
        connection = cursor if cursor is not None else self.store._connection
        return tuple(connection.execute(f"SELECT count(*) AS n FROM {name}").fetchone()["n"]
            for name in ("subagent_tasks", "subagent_controls", "subagent_checkpoints", "subagent_events"))

    def test_notification_and_dispatch_only_see_complete_preparation(self):
        observed = []
        def observe(task_id):
            observed.append((self.store.get(task_id).status, self.store.control(task_id)["version"],
                             len(self.store.checkpoints(task_id))))
        self.store.set_change_listener(observe)
        task = self.submit()
        self.assertEqual(observed, [("queued", 1, 1)])
        self.enqueue.assert_called_once_with(task.task_id)
        self.assertEqual(self.store.control(task.task_id)["dispatch"]["bot_id"], "123")
        self.assertEqual(self.store.checkpoints(task.task_id)[0]["state"]["context_packet"], self.packet.as_payload())
        self.assertEqual(self.counts(), (1, 1, 1, 3))

    def test_checkpoint_failure_rolls_back_task_events_and_checkpoint(self):
        append = self.store.append_checkpoint
        def crash(*args, **kwargs):
            append(*args, **kwargs)
            raise RuntimeError("process failed before dispatch envelope")
        with patch.object(self.store, "append_checkpoint", side_effect=crash), self.assertRaises(RuntimeError):
            self.submit()
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.enqueue.assert_not_called()

    def test_invalid_dispatch_rolls_back_already_prepared_context(self):
        dispatch = {}
        dispatch["invalid"] = dispatch
        with self.assertRaises(ValueError):
            self.coordinator.submit(packet=self.packet, decision=self.entry, dispatch=dispatch)
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.enqueue.assert_not_called()

    def test_parent_transaction_can_atomically_attach_ownership_before_commit(self):
        observer = Mock()
        self.store.set_change_listener(observer)
        with self.assertRaises(RuntimeError):
            with self.store._transaction() as cursor:
                self.store.prepare_task(packet=self.packet, decision=self.entry,
                    dispatch={}, max_parallelism=2, max_steps=4, transaction_cursor=cursor)
                self.assertEqual(self.counts(cursor), (1, 1, 1, 3))
                self.assertEqual(self.counts(), (0, 0, 0, 0))
                raise RuntimeError("parent ownership could not be saved")
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        observer.assert_not_called()

    def test_failed_enqueue_keeps_a_recoverable_task_for_reconciliation(self):
        self.enqueue.side_effect = RuntimeError("job queue unavailable")
        with self.assertRaises(RuntimeError):
            self.submit()
        self.assertEqual(self.counts(), (1, 1, 1, 3))
        task_id = self.store.dispatchable_tasks()[0]
        self.assertEqual(self.store.get(task_id).status, "queued")
        self.assertEqual(self.store.checkpoints(task_id)[0]["state"]["entry_decision"], self.entry.as_payload())


if __name__ == "__main__":
    unittest.main()
