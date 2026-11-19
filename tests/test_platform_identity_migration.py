"""Actual PostgreSQL upgrades preserve legacy principal and source identities."""
import os
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from src.bot_storage.disposable import IsolatedPostgres
from tests.test_platform_identity_storage import inbound, record
from src.bot_storage.schema import HEAD_REVISION
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.message_ir import MessageBody, TextNode, body_to_json


class PlatformIdentityMigrationTests(unittest.TestCase):
    def setUp(self):
        fixture = IsolatedPostgres(os.environ["TEST_POSTGRES_DSN"], revision="0072_qq_simple_binding")
        self.addCleanup(fixture.close)
        self.db = fixture.database
        self.ledger = MessageLedger(self.db)
        self.addCleanup(self.ledger.close)

    def migrate(self, method, revision):
        with patch.dict(os.environ, {"AI_POSTGRES_DSN": os.environ["TEST_POSTGRES_DSN"], "AI_POSTGRES_SCHEMA": self.db.schema}):
            method(Config("alembic.ini"), revision)

    def seed_legacy_message(self, scope):
        with self.ledger._transaction() as cursor:
            conversation = cursor.execute('''INSERT INTO conversations
                (scope_key,platform,kind,native_conversation_id,created_at,updated_at)
                VALUES (?,?,?,?,1,1) RETURNING conversation_id''',
                (scope.key, scope.platform, scope.kind, scope.native_conversation_id)).fetchone()[0]
            cursor.execute('INSERT INTO conversation_visibility(conversation_id) VALUES (?)', (conversation,))
            principal = cursor.execute("INSERT INTO principals(display_name,created_at,updated_at) VALUES ('Alice',1,1) RETURNING principal_id").fetchone()[0]
            identity = cursor.execute('''INSERT INTO principal_identities
                (principal_id,platform,native_user_id,display_name,last_seen_at)
                VALUES (?,'onebot-v11','7','Alice',1) RETURNING identity_id''', (principal,)).fetchone()[0]
            message = cursor.execute('''INSERT INTO messages
                (conversation_id,native_message_id,sender_identity_id,sender_principal_id,
                 sender_native_user_id,sender_display,direction,body_json,rendered_text,occurred_at)
                VALUES (?,'old',?,?,'7','Alice','inbound',?,'old row',1) RETURNING canonical_message_id''',
                (conversation, identity, principal, body_to_json(MessageBody((TextNode(0, 'old row'),))))).fetchone()[0]
        return message, principal

    def test_empty_upgrade_roundtrip(self):
        self.migrate(command.upgrade, HEAD_REVISION)
        self.migrate(command.upgrade, HEAD_REVISION)
        self.db.require_revision(HEAD_REVISION)
        self.assertIsNotNone(record(self.ledger, inbound()).sender_principal_id)

    def test_existing_rows_keep_ids_and_old_uniqueness_after_upgrade(self):
        scope = ConversationScope("onebot-v11", "group", "123")
        message_id, principal_id = self.seed_legacy_message(scope)
        self.migrate(command.upgrade, HEAD_REVISION)
        old = self.ledger.get_in_scope(scope, message_id)
        self.assertEqual(old.sender_principal_id, principal_id)
        self.assertEqual(old.body, MessageBody((TextNode(0, "old row"),)))
        self.assertEqual(self.ledger.ensure_principal_identity("onebot-v11", "7", "Alice"), principal_id)
        new = record(self.ledger, inbound(user="7", scope=scope, message="old"))
        self.assertNotEqual(new.canonical_message_id, message_id)
        self.assertNotEqual(new.sender_principal_id, principal_id)
        self.assertEqual(self.ledger.canonical_id_for_native(scope, "old"), message_id)

    def test_downgrade_refuses_to_discard_live_namespaces(self):
        message_id, _ = self.seed_legacy_message(ConversationScope("onebot-v11", "group", "123"))
        self.migrate(command.upgrade, "0077_retired_features")
        with self.assertRaisesRegex(RuntimeError, "retired_feature_data_requires_backup_restore"):
            self.migrate(command.downgrade, "0076_schedules")
        self.db.require_revision("0077_retired_features")
        with self.ledger._transaction() as cursor:
            self.assertEqual(cursor.execute('SELECT canonical_message_id FROM messages WHERE canonical_message_id=?',
                                            (message_id,)).fetchone()[0], message_id)
