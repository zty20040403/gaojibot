"""Program files use the existing real HTTP/FakePlatform outbox and receipts."""
import asyncio
import hashlib
import json
from types import SimpleNamespace
import unittest

from tests.qq_official_file_fixture import FileFixture
from tests import test_qq_official_simple_files as fixture
from tests.test_vm_sessions import BOOT
from src.plugins.ai_chat.adapters.qq_official.file_upload import FileUploadError
from src.plugins.ai_chat.adapters.qq_official.program_actions import ProgramActions
from src.plugins.ai_chat.adapters.qq_official.program_file_source import issue
from src.plugins.ai_chat.adapters.qq_official.vm_sessions import VMSessions


class ProgramFileDeliveryTests(FileFixture,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixture.FileDeliveryTests.asyncSetUp(self)
        self.sessions = VMSessions(self.tx,self.token,private_admission=self.admission,clock=lambda:self.now)
        self.current = self.sessions.issue(self.endpoint,'s123456',BOOT,expected_route=(1,1)).source
        self.actions = ProgramActions(self.sessions)
        self.action,_ = self.actions.reserve(self.current,generation=0,kind='send_file',program='TEST_PROGRAM',
            request_id='TEST_FILE',payload={'path':'report.txt','filename':'report.txt'})
        with self.tx() as cursor:
            self.source = issue(cursor,self.actions,self.action['action_id'])
        self.record = await self.calls(self.service.artifacts.register,self.source,content=b'TEST_FILE',
            filename='report.txt',logical_request_key=hashlib.sha256(b'TEST_PROGRAM_FILE').hexdigest())

    def admission(self, _cursor, _endpoint, actor):
        return self.admitted and actor.identity_kind!='bot'

    async def send(self):
        return await self.service.dispatcher.send_artifact(self.record.artifact_id)

    def action_state(self):
        return self.actions.get(self.current,self.action['action_id'])

    async def test_program_file_delivers_once_as_bot_and_records_actual_program_receipt(self):
        self.assertEqual((await self.send()).status,'committed')
        self.assertEqual(self.action_state()['state'],'completed')
        self.assertEqual(len(self.fake.media_calls),1)
        self.assertEqual(self.fake.uploaded[('groups',self.endpoint.native_conversation_id)],b'TEST_FILE')
        wire = self.fake.received[-1]['payload']
        self.assertEqual(wire['msg_type'],7); self.assertNotIn('msg_id',wire)
        self.assertEqual(self.query('SELECT purpose FROM delivery_plans'),[{'purpose':'unsolicited'}])
        outbound = self.query("SELECT * FROM messages WHERE direction='outbound'")[0]
        self.assertEqual(json.loads(outbound['raw_event_json'])['by'],'program')
        self.assertEqual(json.loads(outbound['raw_event_json'])['program_action_id'],self.action['action_id'])
        self.assertIn('report.txt',outbound['body_json'])
        self.assertEqual(self.query('SELECT * FROM turn_send_links'),[])
        self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,TRUE,1)',(self.current.scope_key,))
        self.assertEqual((await self.send()).status,'committed')
        self.assertEqual(len(self.fake.received),1)

    async def test_program_file_platform_denial_does_not_fall_back_to_human_window(self):
        self.fake.proactive_allowed[('group',self.endpoint.native_conversation_id)] = False
        self.window()
        with self.assertRaisesRegex(FileUploadError,'proactive_denied'):
            await self.send()
        self.assertEqual(self.action_state()['error_code'],'proactive_denied')
        self.assertEqual(self.query('SELECT send_mode FROM delivery_attempts'),[{'send_mode':'proactive'}])
        self.assertEqual(self.fake.received,[])

    async def test_stop_cancels_waiting_upload_and_records_failure_without_chat_notification(self):
        self.fake.files_release = asyncio.Event()
        job = SimpleNamespace(payload={'artifact_id':self.record.artifact_id})
        pending = asyncio.create_task(self.service.dispatcher(job))
        try:
            await asyncio.wait_for(self.fake.files_seen.wait(),3)
            self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,TRUE,1)',(self.current.scope_key,))
            result = await asyncio.wait_for(pending,3)
            self.assertEqual(result['status'],'failed')
            self.assertEqual(self.action_state()['state'],'failed')
            self.assertEqual(self.fake.received,[])
            self.assertEqual(self.query('SELECT * FROM delivery_plans'),[])
            async with asyncio.timeout(3):
                while not self.fake.files_transport.is_closing():
                    await asyncio.sleep(0.01)
        finally:
            self.fake.files_release.set()
            await asyncio.gather(pending,return_exceptions=True)

    async def test_lost_message_response_stays_unknown_and_is_not_resent(self):
        self.fake.drop_next_response = True
        for _ in range(2):
            with self.assertRaisesRegex(FileUploadError,'可能没发出去'):
                await self.send()
            self.assertEqual(self.action_state()['state'],'ambiguous')
        self.assertEqual(len(self.fake.received),1)
        self.assertEqual(len(self.fake.media_calls),1)
        self.assertEqual(self.query("SELECT * FROM messages WHERE direction='outbound'"),[])

    async def test_outbox_requires_the_exact_persisted_file_handoff_not_just_program_id(self):
        from src.plugins.ai_chat.adapters.qq_official.file_upload import UploadedFile
        with self.tx() as cursor:
            item = dict(cursor.execute('SELECT * FROM qq_official_file_artifacts').fetchone())
            ref = self.outbox.enqueue_file(self.token,artifact=item,uploaded=UploadedFile('TEST_FILE_INFO',self.now+60),
                transaction_cursor=cursor)
        outcome = await self.service.runtime.sender.send_delivery(ref.delivery_ids[0])
        self.assertEqual((outcome.status,outcome.reason),('failed','program_plan_mismatch'))
        self.assertEqual(self.fake.received,[])

    async def test_replaced_vm_is_rejected_before_upload_but_source_metadata_cannot_grant_it(self):
        self.sessions.revoke_vm('s123456')
        with self.assertRaises(PermissionError):
            await self.send()
        self.assertEqual(self.fake.media_calls,[])
        self.assertEqual(self.fake.received,[])
