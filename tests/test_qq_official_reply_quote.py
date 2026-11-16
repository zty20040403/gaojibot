"""A reply quotes the asker only on its first message and only after others spoke."""
import unittest

from src.plugins.ai_chat.adapters.qq_official.send_store import quote_needed


class FakeCursor:
    def __init__(self, *, earlier_part=False, others_spoke=False):
        self.earlier_part, self.others_spoke, self.queries = earlier_part, others_spoke, []

    def execute(self, sql, params):
        self.queries.append(sql)
        return self

    def fetchone(self):
        sql = self.queries[-1]
        found = self.earlier_part if 'FROM deliveries' in sql else self.others_spoke
        return {'?column?': 1} if found else None


def delivery(**changes):
    row = {'delivery_id': 10, 'turn_id': 3, 'reply_to_native_message_id': 'TEST_NATIVE',
           'source_canonical_message_id': 7}
    row.update(changes)
    return row


class ReplyQuoteTests(unittest.TestCase):
    def test_no_quote_when_nobody_else_spoke(self):
        self.assertFalse(quote_needed(FakeCursor(), delivery()))

    def test_first_message_quotes_after_others_spoke(self):
        self.assertTrue(quote_needed(FakeCursor(others_spoke=True), delivery()))

    def test_later_messages_of_the_same_reply_never_quote(self):
        self.assertFalse(quote_needed(FakeCursor(earlier_part=True, others_spoke=True), delivery()))

    def test_a_message_without_a_source_never_quotes(self):
        cursor = FakeCursor(others_spoke=True)
        self.assertFalse(quote_needed(cursor, delivery(reply_to_native_message_id='')))
        self.assertFalse(quote_needed(cursor, delivery(source_canonical_message_id=None)))
        self.assertEqual(cursor.queries, [])

    def test_a_delivery_outside_a_turn_checks_only_the_conversation(self):
        cursor = FakeCursor(others_spoke=True)
        self.assertTrue(quote_needed(cursor, delivery(turn_id=None)))
        self.assertTrue(all('FROM deliveries' not in sql for sql in cursor.queries))


if __name__ == '__main__':
    unittest.main()
