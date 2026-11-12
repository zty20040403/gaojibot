"""F1 image lookup uses the real scoped PostgreSQL ledger, never display names."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official.media_runtime import MediaSession
from src.plugins.ai_chat.adapters.qq_official.media_refs import MediaDenied
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MediaNode, MessageBody, TextNode
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey
from tests.postgres_fixture import postgres_for_test


class ImageSelectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = postgres_for_test(self, reset=True)
        self.ledger = MessageLedger(self.db)
        self.addCleanup(self.ledger.close)
        self.calls = DatabaseCalls()
        self.addAsyncCleanup(self.calls.close)
        self.endpoint = EndpointKey(AccountKey('qq-official', 'TEST_APP', 'test'), 'group', 'TEST_GROUP')
        self.actor = ActorKey(self.endpoint.account, 'group_member', 'TEST_GROUP', 'TEST_MEMBER')
        self.counter = 0
        self.session = MediaSession(SimpleNamespace(app=SimpleNamespace(message_ledger=self.ledger),
            runtime=SimpleNamespace(_calls=self.calls)),
            SimpleNamespace(endpoint=self.endpoint, actor=self.actor, scope=self.endpoint.scope), None)
        self.session.current = AsyncMock(side_effect=lambda: {'canonical_message_id': self.current})

    def say(self, *, image=False, actor=None, endpoint=None):
        self.counter += 1
        endpoint, actor = endpoint or self.endpoint, actor or self.actor
        node = MediaNode(0, 'image', source='https://example.invalid/TEST_IMAGE') if image else TextNode(0, 'TEST text')
        row = self.ledger.record_message(endpoint.scope, native_message_id=f'TEST_{self.counter}',
            sender_native_user_id=actor.native_user_id, sender_display='TEST same display',
            actor=actor, source_endpoint=endpoint, body=MessageBody((node,)))
        self.current = row.canonical_message_id
        return self.current

    async def selected(self, arguments=None):
        return (await self.session.image_source(arguments or {})).ref.canonical_id

    async def test_current_then_same_actor_then_tail(self):
        own = self.say(image=True)
        self.assertEqual(await self.selected(), own)
        other = ActorKey(self.endpoint.account, 'group_member', 'TEST_GROUP', 'TEST_OTHER')
        latest = self.say(image=True, actor=other)
        self.say()
        self.assertEqual(await self.selected(), own)
        # The tenth newer message pushes our own image outside the ten-message window.
        for _ in range(9):
            self.say()
        self.assertEqual(await self.selected(), latest)
        self.assertEqual(await self.selected({'message_handle': f'msg#{own}'}), own)

    async def test_explicit_text_or_foreign_handle_does_not_fall_back(self):
        self.say(image=True)
        text = self.say()
        endpoint = EndpointKey(self.endpoint.account, 'group', 'TEST_FOREIGN')
        actor = ActorKey(endpoint.account, 'group_member', endpoint.native_conversation_id, 'TEST_MEMBER')
        foreign = self.say(image=True, endpoint=endpoint, actor=actor)
        self.current = text
        for handle in (f'msg#{text}', f'msg#{foreign}', 'msg#0', 'https://example.invalid'):
            with self.subTest(handle=handle), self.assertRaises(MediaDenied):
                await self.selected({'message_handle': handle})

    async def test_no_image_gives_actionable_error_and_hidden_rows_are_excluded(self):
        image = self.say(image=True)
        self.say()
        with self.ledger._transaction() as cursor:
            cursor.execute('UPDATE conversation_visibility SET min_canonical_message_id=?', (image + 1,))
        with self.assertRaisesRegex(MediaDenied, 'message_handle'):
            await self.selected()
