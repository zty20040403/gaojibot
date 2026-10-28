import unittest

from tests.qq_official_outbox_fixture import OutboxFixture

from src.plugins.ai_chat import turn_journal as draft


class DurableJournalTests(OutboxFixture):
    def setUp(self):
        super().setUp()
        self.journal = draft.TurnJournal(self.database)
        self.addCleanup(self.journal.close)

    def begin(self, cursor=None):
        return self.journal.start_turn(self.endpoint.scope, trigger_canonical_message_id=None,
            objective="TEST_DURABLE", provider="TEST", model="TEST", started_at=self.now,
            transaction_cursor=cursor)

    def test_caller_transaction_contains_start_and_finish(self):
        with self.tx() as cursor:
            turn = self.begin(cursor)
            self.assertEqual(cursor.execute("SELECT status FROM agent_turns WHERE turn_id=?",
                                            (turn.turn_id,)).fetchone()["status"], "running")
            self.assertEqual(self.query("SELECT turn_id FROM agent_turns"), [])
            self.journal.finish_turn(turn.turn_id, status="succeeded", final_text="TEST_RESULT",
                                     finished_at=self.now + 1, transaction_cursor=cursor)
        rows = self.query("SELECT status,final_text FROM agent_turns")
        self.assertEqual([(row["status"],row["final_text"]) for row in rows], [("succeeded","TEST_RESULT")])

    def test_execution_association_failure_rolls_back_journal_start(self):
        with self.assertRaisesRegex(RuntimeError, "TEST_EXECUTION_ASSOCIATION"):
            with self.tx() as cursor:
                self.begin(cursor)
                raise RuntimeError("TEST_EXECUTION_ASSOCIATION")
        self.assertEqual(self.query("SELECT turn_id FROM agent_turns"), [])
        self.assertEqual(self.begin().turn_ordinal, 1)


if __name__ == "__main__":
    unittest.main()
