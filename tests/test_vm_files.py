"""VM-only file HTTP + bounded local guest files + persistent real file outbox."""
import asyncio
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from tests import test_vm_program_http as fixture
from tests.qq_official_outbox_fixture import OutboxFixture
from tests.qq_official_file_http_fixture import FileMediaFake
from src.plugins.ai_chat.adapters.qq_official.file_services import FileServices,FileServiceConfig
from src.sandbox_runtime.artifact_export import bounded_regular_file


class VMFileHTTPTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    ensure_current = fixture.VMProgramHTTPTests.ensure_current
    make_sender = fixture.VMProgramHTTPTests.make_sender
    fast_sleep = fixture.VMProgramHTTPTests.fast_sleep

    async def asyncSetUp(self):
        fake = FileMediaFake(); fake.clock = lambda:self.now
        with patch('tests.test_vm_program_http.FakePlatform',return_value=fake):
            await fixture.VMProgramHTTPTests.asyncSetUp(self)
        self.runtime = self.service.runtime
        self.runtime.deliveries,self.runtime.outbox = self.deliveries,self.outbox
        self.runtime._token,self.runtime._stopping = self.token,False
        self.files = self.runtime.file_service = FileServices(self.runtime,self.token,self.rest,
            FileServiceConfig(self.root/'private-files',self.manager,generation_enabled=True))
        self.addAsyncCleanup(self.files.close)
        self.work = self.root/'work'; self.work.mkdir()
        self.manager.max_file_bytes = 65536
        async def export(owner,sid,path,*,file_only,max_bytes):
            self.assertEqual((owner,sid,file_only),(self.owner,'s123456',True))
            return await asyncio.to_thread(bounded_regular_file,self.work,path,max_bytes),False
        self.manager.export_artifact = AsyncMock(side_effect=export)
        (self.work/'report.txt').write_bytes(b'TEST_ORIGINAL')

    async def post(self, **changes):
        return await self.client.post(self.base+'/vm/send-file',json=dict(path='/work/report.txt',request_id='TEST_FILE',
            program='TEST_PROGRAM')|changes)

    async def test_file_is_queued_once_and_actual_delivery_is_reported_separately(self):
        self.query('INSERT INTO conversation_settings(scope_key,program_say_limit) VALUES (?,1)',(self.endpoint.scope.key,))
        response = await self.post()
        self.assertEqual((response.status_code,response.json()['state']),(202,'queued'),response.text)
        action_id = response.json()['action_id']
        self.assertNotIn('token_hash',response.text); self.assertNotIn('payload_json',response.text)
        self.assertEqual(self.fake.received,[])
        (self.work/'report.txt').write_bytes(b'TEST_CHANGED_AFTER_CAPTURE')
        self.assertEqual((await self.post()).json(),response.json())
        self.manager.export_artifact.assert_awaited_once()
        rows = self.query('SELECT * FROM qq_official_file_artifacts')
        self.assertEqual(len(rows),1)
        outcome = await self.files.dispatcher(SimpleNamespace(payload={'artifact_id':rows[0]['artifact_id']}))
        self.assertEqual(outcome['status'],'committed')
        state = await self.client.get(self.base+'/vm/actions/'+action_id)
        self.assertEqual(state.json()['state'],'completed')
        self.assertEqual(self.fake.uploaded[('groups',self.endpoint.native_conversation_id)],b'TEST_ORIGINAL')
        self.assertEqual((await self.post()).status_code,200)
        self.assertEqual(len(self.fake.received),1)
        say = await self.client.post(self.base+'/vm/say',json={'text':'TEST','request_id':'TEST_NEXT'})
        self.assertEqual(say.status_code,429) # send-file shares the say bucket.
        self.assertEqual((await self.post(path='different.txt')).status_code,409)

    async def test_only_bounded_regular_files_from_this_workspace_are_accepted(self):
        outside = self.root/'TEST_PRIVATE'; outside.write_bytes(b'TEST_OUTSIDE')
        (self.work/'link').symlink_to(outside)
        (self.work/'dirlink').symlink_to(self.root,target_is_directory=True)
        os.mkfifo(self.work/'fifo')
        (self.work/'large').write_bytes(b'X'*65537)
        for path,status in (('/etc/passwd',400),('../TEST_PRIVATE',400),('/work/../TEST_PRIVATE',400),
                ('link',502),('dirlink/TEST_PRIVATE',502),('fifo',502),('large',502),('missing',502)):
            with self.subTest(path=path):
                response = await self.post(path=path,request_id='TEST_'+str(status)+'_'+str(len(self.query('SELECT * FROM qq_official_program_actions'))))
                self.assertEqual(response.status_code,status,response.text)
                self.assertNotIn('TEST_OUTSIDE',response.text)
        self.assertEqual((await self.post(endpoint_key='TEST_OTHER')).status_code,400)
        self.assertEqual((await self.post(filename='../bad')).status_code,400)
        token,_ = self.service.gateway.open(source={'job_id':1},current=AsyncMock())
        response = await self.client.post(self.base+'/vm/send-file',headers={'Authorization':'Bearer '+token},json={})
        self.assertEqual(response.status_code,401)
        self.assertEqual(self.query('SELECT * FROM qq_official_file_artifacts'),[])
        self.assertEqual(self.fake.received,[])

    async def test_stop_cancels_export_and_start_does_not_replay_the_reserved_path(self):
        entered,cancelled = asyncio.Event(),asyncio.Event()
        async def export(*args,**kwargs):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        self.manager.export_artifact.side_effect = export
        pending = asyncio.create_task(self.post())
        try:
            await asyncio.wait_for(entered.wait(),3)
            self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,TRUE,1)',(self.endpoint.scope.key,))
            response = await asyncio.wait_for(pending,3)
            self.assertEqual(response.status_code,403,response.text)
            await asyncio.wait_for(cancelled.wait(),3)
            self.assertEqual(self.query('SELECT state FROM qq_official_program_actions'),[{'state':'failed'}])
            self.query('UPDATE conversation_settings SET stopped=FALSE')
            self.assertEqual((await self.post()).status_code,403)
            self.assertEqual(self.query('SELECT * FROM qq_official_file_artifacts'),[])
            self.assertEqual(self.lifecycle.files.active,{})
        finally:
            pending.cancel(); await asyncio.gather(pending,return_exceptions=True)

    async def test_export_limit_and_program_disable_cancel_real_work_without_more_exports(self):
        entered,cancelled = asyncio.Event(),[]
        calls = 0
        async def export(*args,**kwargs):
            nonlocal calls
            calls += 1
            if calls==2:
                entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.append(True)
        self.manager.export_artifact.side_effect = export
        pending = [asyncio.create_task(self.post(request_id='TEST_'+str(i))) for i in range(2)]
        try:
            await asyncio.wait_for(entered.wait(),3)
            excess = await self.post(request_id='TEST_EXCESS')
            self.assertEqual((excess.status_code,excess.json()['error']),(429,'program_file_exports_busy'))
            self.query('INSERT INTO program_controls VALUES (?,?,FALSE,?)',(self.endpoint.scope.key,'TEST_PROGRAM',self.now))
            responses = await asyncio.wait_for(asyncio.gather(*pending),3)
            self.assertEqual([item.status_code for item in responses],[403,403])
            self.assertEqual(len(cancelled),2)
            self.assertEqual(self.lifecycle.files.active,{})
            self.assertEqual(self.query('SELECT * FROM qq_official_file_artifacts'),[])
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending,return_exceptions=True)

    async def test_slow_body_cannot_borrow_the_generation_after_stop_start(self):
        authenticated,release = asyncio.Event(),asyncio.Event()
        original = self.lifecycle.api.authorize
        async def authorize(*args,**kwargs):
            result = await original(*args,**kwargs)
            authenticated.set()
            return result
        async def body():
            yield b'{"request_id":"TEST_OLD",'
            await release.wait()
            yield b'"path":"/work/report.txt"}'
        with patch.object(self.lifecycle.api,'authorize',side_effect=authorize):
            pending = asyncio.create_task(self.client.post(self.base+'/vm/send-file',content=body()))
            try:
                await asyncio.wait_for(authenticated.wait(),3)
                self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,FALSE,1)',(self.endpoint.scope.key,))
                release.set()
                response = await asyncio.wait_for(pending,3)
                self.assertEqual((response.status_code,response.json()['error']),(403,'conversation_stopped_or_replaced'))
            finally:
                release.set(); pending.cancel(); await asyncio.gather(pending,return_exceptions=True)
        self.manager.export_artifact.assert_not_awaited()
        self.assertEqual(self.query('SELECT * FROM qq_official_program_actions'),[])

    async def test_restart_does_not_reexport_a_previous_lease_unfinished_path(self):
        current = self.lifecycle.store.authenticate(self.token_value)
        action,_ = self.lifecycle.actions.reserve(current,generation=0,kind='send_file',program='TEST_PROGRAM',
            request_id='TEST_INTERRUPTED',payload={'path':'report.txt','filename':'report.txt'})
        self.query('UPDATE qq_official_program_actions SET account_epoch=account_epoch-1 WHERE action_id=?',(action['action_id'],))
        await self.calls(self.lifecycle.files.recover)
        self.assertEqual(self.lifecycle.actions.get(current,action['action_id'])['state'],'ambiguous')
        self.manager.export_artifact.assert_not_awaited()

    async def test_timeout_settles_failure_and_retry_does_not_export_changed_bytes(self):
        async def slow(*args,**kwargs):
            await asyncio.Future()
        self.manager.export_artifact.side_effect = slow
        with patch('src.plugins.ai_chat.adapters.qq_official.program_files.EXPORT_SECONDS',0.05):
            self.assertEqual((await self.post()).status_code,502)
        self.assertEqual((await self.post()).json()['state'],'failed')
        self.manager.export_artifact.assert_awaited_once()
        self.assertEqual(self.lifecycle.files.active,{})
