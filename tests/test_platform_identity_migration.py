"""Actual PostgreSQL upgrades preserve legacy principal and source identities."""
import os
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from tests.postgres_fixture import postgres_for_test
from tests.test_platform_identity_storage import inbound, record
from src.bot_storage.schema import HEAD_REVISION
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.message_ir import MessageBody, TextNode


class PlatformIdentityMigrationTests(unittest.TestCase):
    def setUp(self):
        self.db = postgres_for_test(self)
        self.ledger = MessageLedger(self.db)
        self.addCleanup(self.ledger.close)

    def migrate(self, method, revision):
        with patch.dict(os.environ, {"AI_POSTGRES_DSN": os.environ["TEST_POSTGRES_DSN"], "AI_POSTGRES_SCHEMA": self.db.schema}):
            method(Config("alembic.ini"), revision)

    def test_empty_upgrade_roundtrip(self):
        self.migrate(command.downgrade, "0060_project_quota_jobs")
        self.migrate(command.upgrade, HEAD_REVISION)
        self.migrate(command.upgrade, HEAD_REVISION)
        self.db.require_revision(HEAD_REVISION)
        self.assertIsNotNone(record(self.ledger, inbound()).sender_principal_id)

    def test_existing_rows_keep_ids_and_old_uniqueness_after_upgrade(self):
        scope = ConversationScope("onebot-v11", "group", "123")
        old = self.ledger.record_message(scope, native_message_id="old", sender_native_user_id="7", sender_display="Alice", body=MessageBody((TextNode(0, "old row"),)))
        self.migrate(command.downgrade, "0060_project_quota_jobs")
        self.db.require_revision("0060_project_quota_jobs")
        self.migrate(command.upgrade, HEAD_REVISION)
        reread = self.ledger.get_in_scope(scope, old.canonical_message_id)
        self.assertEqual(reread, old)
        self.assertEqual(self.ledger.ensure_principal_identity("onebot-v11", "7", "Alice"), old.sender_principal_id)
        new = record(self.ledger, inbound(user="7", scope=scope, message="old"))
        self.assertNotEqual(new.canonical_message_id, old.canonical_message_id)
        self.assertNotEqual(new.sender_principal_id, old.sender_principal_id)
        self.assertEqual(self.ledger.canonical_id_for_native(scope, "old"), old.canonical_message_id)

    def test_downgrade_refuses_to_discard_live_namespaces(self):
        message = record(self.ledger, inbound())
        with self.assertRaisesRegex(RuntimeError, "Export structured"):
            self.migrate(command.downgrade, "0060_project_quota_jobs")
        self.db.require_revision(HEAD_REVISION)
        self.assertEqual(self.ledger.get_in_scope(inbound().scope, message.canonical_message_id), message)
