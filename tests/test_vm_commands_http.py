"""CLI/HTTP -> persistent command -> VM hop -> model -> actual FakePlatform receipt."""
import asyncio
from contextlib import redirect_stdout,redirect_stderr
from io import StringIO
import json
import unittest
from unittest.mock import AsyncMock,patch

from src.plugins.ai_chat import sandbox_gaoji as cli
from src.plugins.ai_chat.adapters.qq_official.conversation_control import change
from src.plugins.ai_chat.platform_types import EndpointKey
from tests import test_vm_wake_http as fixture
from tests.test_command_execution import report
from tests.qq_official_outbox_fixture import OutboxFixture


class VMCommandHTTPTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    ensure_current = fixture.VMWakeHTTPTests.ensure_current
    make_sender = fixture.VMWakeHTTPTests.make_sender
    fast_sleep = fixture.VMWakeHTTPTests.fast_sleep
    close_workers = fixture.VMWakeHTTPTests.close_workers
    wait_state = fixture.VMWakeHTTPTests.wait_state

    async def asyncSetUp(self):
        await fixture.VMWakeHTTPTests.asyncSetUp(self)
        self.commands = self.lifecycle.commands
        self.requests,self.reports = [],{}
        self.final_immediately = False
        original = self.manager._guest_exec
        async def guest(sid,path,args,*,input_bytes=None,timeout=60):
            if args!=['-c',self.commands.script]:
                return await original(sid,path,args,input_bytes=input_bytes,timeout=timeout)
            data = json.loads(input_bytes); self.requests.append(data); key = data['command_id']
            if data['op']=='start':
                self.reports[key] = report(state='completed',stopped=True,exit_code=0,elapsed_seconds=9) if self.final_immediately else report()
            if data['op']=='stop':
                self.reports[key] = report(state='cancelled',stopped=True,error='command_cancelled')
            return 0,json.dumps(self.reports.get(key,report(state='ambiguous'))).encode(),b''
        self.manager._guest_exec = AsyncMock(side_effect=guest)

    async def post(self, **changes):
        return await self.client.post(self.base+'/vm/commands',json=dict(argv=['printf','TEST_RESULT'],request_id='TEST_COMMAND',program='TEST_PROGRAM')|changes)

    async def state(self, key, expected):
        async with asyncio.timeout(8):
            while True:
                response = await self.client.get(self.base+'/vm/commands/'+key)
                self.assertEqual(response.status_code,200,response.text)
                if response.json()['state']==expected:
                    return response.json()
                await asyncio.sleep(.02)

    async def test_cli_to_completion_queues_one_model_and_keeps_real_delivery_receipt(self):
        self.final_immediately = True
        client = cli.Client(self.guest)
        connection = cli.http.client.HTTPConnection
        def connect(host,port,**kwargs):
            self.assertEqual((host,port),('10.0.2.2',self.service.gateway.port))
            return connection('127.0.0.1',port,**kwargs)
        def command():
            out,err = StringIO(),StringIO()
            with redirect_stdout(out),redirect_stderr(err):
                result = cli.main(['run-bg','--request-id','TEST_CLI','--','printf','TEST_RESULT'],client=client)
            return result,json.loads(out.getvalue()),err.getvalue()
        with patch.object(cli.http.client,'HTTPConnection',side_effect=connect):
            code,row,error = await asyncio.to_thread(command)
            self.assertEqual(code,0); self.assertIn('request_id=TEST_CLI',error)
            key = row['command_id']
            row = await self.state(key,'completed')
            async with asyncio.timeout(5):
                while row['notification_action_id'] is None:
                    await asyncio.sleep(.02)
                    row = (await self.client.get(self.base+'/vm/commands/'+key)).json()
            await self.wait_state(row['notification_action_id'],'completed')
            status,latest = await asyncio.to_thread(client.request,'GET','/vm/commands?request_id=TEST_CLI')
        self.assertEqual((status,latest['notification']['state']),(200,'completed'))
        self.assertEqual(len(self.prompts),1); self.assertIn('9 秒',self.prompts[0])
        self.assertEqual(len(self.fake.received),1)
        self.assertNotIn('argv_json',latest); self.assertNotIn('token_hash',latest)
        repeated = await self.post(request_id='TEST_CLI',program='manual')
        self.assertEqual(repeated.json()['command_id'],key)
        self.assertEqual(sum(row['op']=='start' for row in self.requests),1)

    async def test_command_success_does_not_hide_denied_qq_notification(self):
        self.final_immediately = True
        self.fake.proactive_allowed[('group',self.endpoint.native_conversation_id)] = False
        key = (await self.post()).json()['command_id']
        async with asyncio.timeout(8):
            while True:
                row = (await self.client.get(self.base+'/vm/commands/'+key)).json()
                if row.get('notification',{}).get('state')=='failed':
                    break
                await asyncio.sleep(.02)
        self.assertEqual((row['state'],row['exit_code']),('completed',0))
        self.assertEqual(row['notification']['error_code'],'proactive_denied')
        self.assertFalse(self.query("SELECT * FROM messages WHERE direction='outbound'"))

    async def test_task_credentials_scope_injection_size_and_conflicting_retry_are_refused(self):
        task_token,_ = self.service.gateway.open(source={'job_id':1},current=AsyncMock())
        response = await self.client.post(self.base+'/vm/commands',headers={'Authorization':'Bearer '+task_token},json={})
        self.assertEqual(response.status_code,401)
        self.assertEqual((await self.post(actor_key='TEST_OWNER')).status_code,400)
        self.assertEqual((await self.client.post(self.base+'/vm/commands',content=b'X'*32769)).status_code,413)
        key = (await self.post()).json()['command_id']
        self.assertEqual((await self.post(argv=['false'])).status_code,409)
        self.assertEqual((await self.post()).json()['command_id'],key)
        other = EndpointKey(self.account,'group','TEST_OTHER'); self.provision(other)
        current = self.lifecycle.store.issue(other,'s654321',self.current_boot(),expected_route=(1,1)).source
        row,_ = self.commands.commands.reserve(current,generation=(0,0),program='TEST',request_id='TEST_OTHER',argv=['true'])
        for route in ('/vm/commands/'+row['command_id'],'/vm/commands?request_id=TEST_OTHER'):
            self.assertEqual((await self.client.get(self.base+route)).status_code,404)
        self.assertEqual((await self.client.post(self.base+'/vm/commands/'+row['command_id']+'/stop',json={})).status_code,404)

    def current_boot(self):
        return self.lifecycle.store.authenticate(self.token_value).boot_id

    async def test_stop_and_status_work_while_conversation_is_stopped(self):
        key = (await self.post()).json()['command_id']
        await self.state(key,'running')
        with self.tx() as cursor:
            change(cursor,self.endpoint,'stop',now=self.now,actor='TEST_ADMIN',authorized=True)
        self.assertEqual((await self.post(request_id='TEST_REFUSED')).status_code,403)
        response = await self.client.post(self.base+'/vm/commands/'+key+'/stop',json={})
        self.assertEqual(response.status_code,202,response.text)
        await self.state(key,'cancelled')
        self.assertEqual(self.prompts,[]); self.assertEqual(self.fake.received,[])

    async def test_kill_fences_body_authenticated_before_command_generation_changes(self):
        authenticated,release = asyncio.Event(),asyncio.Event()
        original = self.lifecycle.api.authorize
        async def authorize(*args,**kwargs):
            scope = await original(*args,**kwargs); authenticated.set(); return scope
        async def body():
            yield b'{"request_id":"TEST_SLOW",'
            await release.wait()
            yield b'"argv":["true"]}'
        with patch.object(self.lifecycle.api,'authorize',side_effect=authorize):
            pending = asyncio.create_task(self.client.post(self.base+'/vm/commands',content=body()))
            try:
                await asyncio.wait_for(authenticated.wait(),3)
                with self.tx() as cursor:
                    change(cursor,self.endpoint,'kill',now=self.now,actor='TEST_ADMIN',authorized=True)
                release.set(); result = await asyncio.wait_for(pending,3)
                self.assertEqual((result.status_code,result.json()['error']),(403,'background_command_generation_changed'))
            finally:
                release.set(); pending.cancel(); await asyncio.gather(pending,return_exceptions=True)
        self.assertEqual(self.query('SELECT * FROM qq_official_background_commands'),[])
