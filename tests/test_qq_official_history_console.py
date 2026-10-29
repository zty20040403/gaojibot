"""Real protected console history review and atomic completion/activation."""
import json
import unittest
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official.migration_history_service import MigrationHistoryService
from src.plugins.ai_chat.message_ir import MessageBody,TextNode
from tests import test_qq_official_migration_console as console_fixture
from tests.qq_official_migration_journal_fixture import JournalFixture


class MigrationHistoryConsoleTests(JournalFixture,unittest.IsolatedAsyncioTestCase):
    def plan_request(self):
        # This suite migrates only public history. Executable task items now
        # require the actual domain activation exercised by its own suite.
        return {**console_fixture.MigrationConsoleTests.plan_request(self), 'items': []}
    create=console_fixture.MigrationConsoleTests.create
    asyncTearDown=console_fixture.MigrationConsoleTests.asyncTearDown

    async def asyncSetUp(self):
        await console_fixture.MigrationConsoleTests.asyncSetUp(self)
        self.service.history=MigrationHistoryService(self.migration,token=self.token,clock=lambda:self.now)
        self.ledger.record_message(self.source_scope,native_message_id='PUBLIC_CONSOLE_SOURCE',
            sender_native_user_id=self.source_actor.native_user_id,sender_display='Original author',
            body=MessageBody((TextNode(0,'Approved public source'),)),occurred_at=self.now)

    async def history_preview(self):
        plan=await self.create()
        response=await self.client.post(self.path+'/'+plan['migration_id']+'/history-preview',
            json=dict(preview_digest=plan['preview_digest'],chapter_ids=[],public_note_ids=[]))
        self.assertEqual(response.status_code,200,response.text)
        updated=response.json();self.assertNotEqual(updated['preview_digest'],plan['preview_digest'])
        return plan,updated

    async def ready(self):
        initial,plan=await self.history_preview()
        for action in ('freeze','drain','switch','verify'):
            response=await self.client.post(self.path+'/'+plan['migration_id']+'/actions',
                json=dict(action=action,preview_digest=plan['preview_digest']))
            self.assertEqual(response.status_code,200,response.text)
        nonce=self.query('SELECT verification_nonce FROM migration_journals')[0][0]
        # Build genuine registered M3 receipts with the existing host fixture.
        # The console already began verification; leave final completion to API.
        with patch.object(self.migration,'begin_verification',return_value=nonce), \
             patch.object(self.migration,'complete',return_value=None):
            JournalFixture.complete(self,type('Plan',(),plan)())
        return plan

    async def test_history_preview_changes_digest_and_old_review_cannot_freeze(self):
        initial,plan=await self.history_preview()
        response=await self.client.post(self.path+'/'+plan['migration_id']+'/actions',
            json=dict(action='freeze',preview_digest=initial['preview_digest']))
        self.assertEqual(response.status_code,409)
        row=self.query('SELECT preview_json FROM migration_journals')[0][0]
        self.assertIn('public_history',json.loads(row))
        self.assertEqual('prepared',self.query('SELECT state FROM qq_migration_public_history_grants')[0][0])

    async def test_actual_console_complete_activates_reviewed_history_same_transaction(self):
        plan=await self.ready()
        response=await self.client.post(self.path+'/'+plan['migration_id']+'/actions',
            json=dict(action='complete',preview_digest=plan['preview_digest']))
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual('completed',self.query('SELECT phase FROM migration_routes')[0][0])
        self.assertEqual('active',self.query('SELECT state FROM qq_migration_public_history_grants')[0][0])

    async def test_activation_failure_rolls_back_complete_but_preserves_real_receipts(self):
        plan=await self.ready()
        self.ledger.hide_history(self.source_scope)
        response=await self.client.post(self.path+'/'+plan['migration_id']+'/actions',
            json=dict(action='complete',preview_digest=plan['preview_digest']))
        self.assertEqual(response.status_code,409)
        self.assertEqual('verifying',self.query('SELECT phase FROM migration_routes')[0][0])
        self.assertEqual('prepared',self.query('SELECT state FROM qq_migration_public_history_grants')[0][0])
        self.assertEqual(2,self.query('SELECT count(*) FROM migration_verification_receipts')[0][0])
        self.assertEqual(0,self.query("SELECT count(*) FROM migration_control_audit WHERE action='complete'")[0][0])
