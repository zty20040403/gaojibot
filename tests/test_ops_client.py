"""Local transport doubles only: signed fixed calls, intent checks and cancellation."""
import asyncio
from copy import deepcopy
import hashlib
import hmac
import json
import unittest
from unittest.mock import AsyncMock,patch

import httpx

from src.plugins.ai_chat import ops_client as ops
from src.plugins.ai_chat.ops_plans import OpsConfiguration,fingerprint
from tests.test_ops_plans import configuration
from tests.test_ops_read import Body


class OpsClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = OpsConfiguration.parse(configuration())
        secret = patch.object(OpsConfiguration,'secret',return_value='TEST_SECRET_'*4)
        secret.start(); self.addCleanup(secret.stop)
        self.current = AsyncMock()
        self.client = ops.OpsClient(self.config,self.config.plan('restart-test'),{'unit':'TEST_A.service'},
            action_id='a'*32,deadline_at=1800003600,current=self.current,clock=lambda:1800000000)
        self.catalog = dict(version=2,hosts=['TEST_HOST'],backend='ssh',writes_require_approval=True,
            operations=[dict(name='exec.run',read_only=False,kind='job_submission',idempotency='required')])
        arguments = dict(op='exec.run',params=self.client.params,binding_hash='b'*64,upstream_idempotency='required')
        self.record = dict(operation_id='op_'+'c'*32,actor_id='TEST_OPERATOR',origin_scope=self.client.origin,
            idempotency_key=self.client.key,host_id='TEST_HOST',resource_ref='exec.run',resource_version=1,
            operation='ssh.execute',backend_ref='ssh-management-v1',status='awaiting_approval',arguments=arguments,deadline_at=1800001800,
            contract_hash=fingerprint(dict(actor='TEST_OPERATOR',origin=self.client.origin,arguments=arguments)))
        self.seen,self.bodies = [],[]
        self.handler = self.respond
        client = self.real_client = httpx.AsyncClient
        async def server(request):
            self.seen.append(request)
            signed = '\n'.join((request.method,request.url.path,request.headers['X-KC-Actor'],request.headers['X-KC-Origin'],
                request.headers['X-KC-Time'],hashlib.sha256(request.content).hexdigest())).encode()
            self.assertEqual(request.headers['Authorization'],'Bearer '+'TEST_SECRET_'*4)
            self.assertEqual(request.headers['X-KC-Signature'],hmac.new(('TEST_SECRET_'*4).encode(),signed,hashlib.sha256).hexdigest())
            response = self.handler(request)
            return await response if asyncio.iscoroutine(response) else response
        network = patch.object(ops.httpx,'AsyncClient',side_effect=lambda **kw:client(transport=httpx.MockTransport(server),**kw))
        network.start(); self.addCleanup(network.stop)

    def respond(self,request):
        if request.url.path.endswith('/catalog'):
            self.assertEqual(dict(request.url.params),{'operation':'exec.run'})
            value = self.catalog
        elif request.url.path.endswith('/call'):
            body = json.loads(request.content)
            self.assertEqual(body,dict(operation='exec.run',params=self.client.params,idempotency_key=self.client.key,deadline_at=self.client.deadline_at))
            value = dict(ok=True,executed=False,approval_required=True,operation=self.record)
        else:
            if request.url.path.endswith('/approve'):
                self.assertEqual(json.loads(request.content),dict(contract_hash=self.record['contract_hash'],resource_version=1))
                self.record['status'] = 'queued'
            value = self.record
        stream = Body(json.dumps(value).encode()); self.bodies.append(stream)
        return httpx.Response(200,stream=stream)

    async def test_catalog_prepare_approve_and_status_have_exact_signatures_and_contract(self):
        ref = await self.client.prepare()
        self.assertEqual(ref.operation_id,self.record['operation_id'])
        self.assertEqual((await self.client.approve(ref))['status'],'queued')
        self.record['status'] = 'succeeded'
        self.assertEqual((await self.client.status(ref))['status'],'succeeded')
        self.assertEqual([r.method for r in self.seen],['GET','POST','POST','GET'])
        self.assertTrue(all(body.closed for body in self.bodies)); self.assertGreaterEqual(self.current.await_count,12)

    async def test_actual_control_router_and_manager_accept_the_fixed_signed_contract(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        import time
        from prometheus_client import CollectorRegistry
        from src.cluster_control.api import create_app
        from src.cluster_control.ops_management import OpsManagementService
        from tests.test_ops_management import MemoryStore
        store = MemoryStore()
        backend = SimpleNamespace(backend_name='ssh',authorization_binding=lambda:{'test':'TEST_BINDING'},
            catalog=AsyncMock(return_value={'version':2,'operations':[dict(self.catalog['operations'][0],params_schema={'type':'object'})]}))
        manager = OpsManagementService(backend,store,hosts=('TEST_HOST',),actors=('TEST_OPERATOR',),host_helpers={'TEST_HOST':'/TEST_BIN/helper'})
        execution = SimpleNamespace(store=store,operation_status=lambda key,**kw:store.get_operation(key))
        with TemporaryDirectory() as folder:
            token = Path(folder)/'token'; token.write_text('TEST_SECRET_'*4); token.chmod(0o600)
            app = create_app(SimpleNamespace(metrics_registry=CollectorRegistry()),api_token_file=token,management=manager,execution=execution)
            self.client.clock = time.time; self.client.deadline_at = int(time.time())+90
            with patch.object(ops.httpx,'AsyncClient',side_effect=lambda **kw:self.real_client(transport=httpx.ASGITransport(app=app),**kw)):
                ref = await self.client.prepare()
                self.assertEqual(store.get_operation(ref.operation_id)['status'],'awaiting_approval')
                self.assertEqual(store.get_operation(ref.operation_id)['deadline_at'],self.client.deadline_at)
                self.assertEqual((await self.client.approve(ref))['status'],'queued')
                self.assertEqual((await self.client.status(ref))['status'],'queued')
            self.assertEqual(len(store.records),1)  # no runner or SSH backend is started

    async def test_invalid_catalog_never_posts(self):
        for change in ({'backend':'unknown'},{'writes_require_approval':False}, {'hosts':[]},
                {'operations':[dict(name='exec.run',read_only=True,kind='job_submission',idempotency='required')]}):
            original = deepcopy(self.catalog); self.catalog.update(change)
            with self.subTest(change=change),self.assertRaises(ops.OpsTransportError):await self.client.prepare()
            self.catalog = original
        self.assertTrue(all(request.method=='GET' for request in self.seen))

    async def test_changed_intent_or_remote_identity_is_never_approved(self):
        for change in ({'operation_id':'../TEST_OTHER'},{'actor_id':'TEST_OTHER'},{'origin_scope':'TEST_OTHER'},
                {'host_id':'TEST_OTHER'},{'contract_hash':'d'*64},{'resource_version':True},{'deadline_at':1800003601},
                {'arguments':dict(self.record['arguments'],params={})},{'status':'succeeded'}):
            original = deepcopy(self.record); self.record.update(change)
            with self.subTest(change=change),self.assertRaises(ops.OpsTransportError):await self.client.prepare()
            self.record = original
        self.assertFalse(any(request.url.path.endswith('/approve') for request in self.seen))

    async def test_status_cannot_switch_to_another_operation_with_the_same_parameters(self):
        ref = await self.client.prepare(); self.record['operation_id'] = 'op_'+'d'*32
        with self.assertRaisesRegex(ops.OpsTransportError,'contract_changed'):await self.client.status(ref)

    async def test_original_job_deadline_prevents_all_network_calls(self):
        self.client.clock = lambda:self.client.deadline_at
        with self.assertRaisesRegex(ops.OpsTransportError,'deadline_expired'):await self.client.prepare()
        self.assertEqual(self.seen,[])

    async def test_initial_authority_loss_never_opens_transport(self):
        self.current.side_effect = PermissionError('TEST_REVOKED')
        with self.assertRaises(PermissionError):await self.client.prepare()
        self.assertEqual(self.seen,[])

    async def test_revocation_closes_pending_approval_and_never_retries_the_write(self):
        ref = await self.client.prepare(); entered,closed = asyncio.Event(),asyncio.Event()
        async def waiting(request):
            entered.set()
            try:await asyncio.Future()
            finally:closed.set()
        self.handler = waiting
        pending = asyncio.create_task(self.client.approve(ref))
        await asyncio.wait_for(entered.wait(),1); self.current.side_effect = PermissionError('TEST_REVOKED')
        with self.assertRaises(PermissionError):await asyncio.wait_for(pending,2)
        self.assertTrue(closed.is_set()); self.assertEqual(len(self.seen),3)

    async def test_deadline_and_job_cancellation_close_the_stream(self):
        entered = asyncio.Event()
        class WaitingBody(Body):
            async def __aiter__(self):
                entered.set(); yield b'{'; await asyncio.Future()
        body = WaitingBody(b''); self.handler = lambda request:httpx.Response(200,stream=body)
        pending = asyncio.create_task(self.client.prepare()); await asyncio.wait_for(entered.wait(),1)
        self.current.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(pending,2)
        self.assertTrue(body.closed)
        self.current.side_effect = None; body = WaitingBody(b'')
        with patch.object(ops,'DEADLINE',0.02),self.assertRaisesRegex(ops.OpsTransportError,'interrupted'):
            await self.client.prepare()
        self.assertTrue(body.closed); self.assertEqual(len(self.seen),2)

    async def test_status_encoding_json_and_body_limits_do_not_leak_or_redirect(self):
        for status,headers,raw in ((302,{'location':'https://TEST_OTHER/'},b'TEST_SECRET'),(500,{},b'TEST_SECRET'),
                (200,{'content-encoding':'gzip'},b'TEST_SECRET'),(200,{},b'not JSON TEST_SECRET'),
                (200,{},b'{"value":NaN}'),(200,{},b'{"value":"\\ud800"}'),(200,{},b'x'*(ops.MAX_RESPONSE+1))):
            body = Body(raw); self.handler = lambda request:httpx.Response(status,headers=headers,stream=body)
            with self.assertRaises(ops.OpsTransportError) as error:await self.client.prepare()
            self.assertNotIn('TEST_SECRET',str(error.exception)); self.assertNotIn('example.test',str(error.exception))
            self.assertTrue(body.closed)
        self.assertEqual(len(self.seen),7)
