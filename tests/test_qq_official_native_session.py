"""Private session evidence and DNS-pinned PUT; no remote platform access."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

import httpx2

from src.plugins.ai_chat.adapters.qq_official.native_media import fingerprint,parse_prepare,UploadPart
from src.plugins.ai_chat.adapters.qq_official.native_part_transport import MultipartConfiguration,PinnedPartTransport
from src.plugins.ai_chat.adapters.qq_official.native_session_vault import NativeSessionVault,NativeSessionBinding,NativeSessionReceipt
from src.plugins.ai_chat.adapters.qq_official.native_upload import UploadGuardError,UploadSourceLost
from src.plugins.ai_chat.agent.control import LeaseLost
from src.plugins.ai_chat.adapters.qq_official.native_vault import NativeUploadBinding,NativeVaultError
from src.plugins.ai_chat.platform_types import AccountKey,EndpointKey


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.now=1000;self.directory=Path(self.temp.name)/'session'
        self.config=MultipartConfiguration(frozenset({'uploads.example.test'}))
        endpoint=EndpointKey(AccountKey('qq-official','TEST_APP','test'),'group','TEST_GROUP')
        artifact=fingerprint([b'TEST_PDF_BYTES'],filename='fixture.pdf',file_type=4)
        upload=NativeUploadBinding('a'*32,'b'*32,endpoint,artifact,'application/pdf')
        self.binding=NativeSessionBinding(upload,1100,self.config)
        body=dict(upload_id='TEST_SESSION_SECRET',block_size=str(artifact.size),
            parts=[dict(index=0,block_size=str(artifact.size),presigned_url='https://uploads.example.test/part?key=TEST_SECRET')],
            upload_config=dict(concurrency=1,retry_timeout=60,retry_delay=1))
        self.prepared=parse_prepare(200,body,endpoint=endpoint,artifact=artifact,allowed_hosts=self.config.allowed_part_hosts)
        self.receipt=NativeSessionReceipt(self.prepared,1000,1060)
        self.vault=NativeSessionVault(self.directory,clock=lambda:self.now);self.addCleanup(self.vault.close)

    def test_immutable_restart_recovery_matches_full_binding_and_original_expiry(self):
        ref=self.vault.save(self.binding,self.receipt)
        self.assertEqual(self.vault.save(self.binding,self.receipt),ref)
        self.assertEqual(self.directory.stat().st_mode&0o777,0o700)
        self.assertEqual((self.directory/(ref.credential_ref+'.json')).stat().st_mode&0o777,0o600)
        self.vault.close();self.vault=NativeSessionVault(self.directory,clock=lambda:self.now);self.addCleanup(self.vault.close)
        self.assertEqual(self.vault.read(ref,self.binding),self.receipt)
        self.assertEqual(self.vault.recover_stage(self.binding),(ref,self.receipt))
        self.assertNotIn('TEST_SECRET',repr(ref)+repr(self.receipt)+repr(self.vault))
        self.assertNotIn('TEST_GROUP',repr(self.binding))
        with self.assertRaises(NativeVaultError):self.vault.save(self.binding,replace(self.receipt,reusable_until=1059))
        for changed in (replace(self.binding,deadline_at=1099),replace(self.binding,upload=replace(self.binding.upload,
                stage_attempt_id='c'*32)),replace(self.binding,configuration=replace(self.config,max_parts=127))):
            with self.assertRaises((NativeVaultError,FileNotFoundError)):self.vault.read(ref,changed)

    def test_expiry_and_mutation_never_renew_or_reconstruct(self):
        ref=self.vault.save(self.binding,self.receipt)
        self.now=1060
        with self.assertRaisesRegex(NativeVaultError,'expired'):self.vault.read(ref,self.binding)
        with self.assertRaises(NativeVaultError):self.vault.save(self.binding,replace(self.receipt,received_at=1060,reusable_until=1100))
        self.assertEqual(self.vault.collect_expired()['removed'],1)
        # DB immutable stage/session record, not absence of this file, prevents
        # a new physical prepare. The private vault grants no such permission.
        self.assertEqual(list(self.directory.iterdir()),[])

    def test_corruption_permissions_and_wrong_namespace_fail_closed(self):
        ref=self.vault.save(self.binding,self.receipt);path=self.directory/(ref.credential_ref+'.json')
        path.chmod(0o644)
        with self.assertRaises(NativeVaultError):self.vault.read(ref,self.binding)
        path.chmod(0o600);data=json.loads(path.read_text());data['receipt']['prepared']['upload_id']='CHANGED'
        path.write_text(json.dumps(data))
        with self.assertRaises(NativeVaultError):self.vault.read(ref,self.binding)

    def test_conservative_session_and_part_limits(self):
        for receipt in (replace(self.receipt,reusable_until=1101),replace(self.receipt,received_at=True),
                        replace(self.receipt,prepared=replace(self.prepared,endpoint=EndpointKey(self.binding.upload.endpoint.account,'group','OTHER')))):
            with self.assertRaises(NativeVaultError):self.vault.save(self.binding,receipt)
        for hosts in (frozenset(),frozenset({'127.0.0.1'}),frozenset({'*.qq.com'}),frozenset({'test.local'})):
            with self.assertRaises(ValueError):MultipartConfiguration(hosts)


async def permitted():pass


class PartTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_pinned_put_preserves_signed_target_no_credentials_and_consumes_once(self):
        calls=[];dispatched=[]
        async def resolve(host,port):
            self.assertEqual((host,port),('uploads.example.test',443));return ('93.184.216.34',)
        async def handle(request):
            self.assertEqual(len(dispatched),1)
            calls.append(request)
            self.assertEqual(request.method,'PUT');self.assertEqual(request.url.host,'93.184.216.34')
            self.assertEqual(request.url.query,b'key=TEST%2FSECRET')
            self.assertEqual(request.headers['host'],'uploads.example.test')
            self.assertEqual(request.extensions['sni_hostname'],'uploads.example.test')
            self.assertNotIn('authorization',request.headers);self.assertNotIn('cookie',request.headers)
            self.assertEqual(await request.aread(),b'TEST')
            return httpx2.Response(200)
        transport=PinnedPartTransport(MultipartConfiguration(frozenset({'uploads.example.test'})),
            resolver=resolve,transport_factory=lambda:httpx2.MockTransport(handle))
        result=await transport.put(UploadPart(0,0,4,'https://uploads.example.test/p?key=TEST%2FSECRET'),b'TEST',
            timeout=3,ensure_current=permitted,before_dispatch=lambda:dispatched.append(True))
        self.assertEqual(result.status,200);self.assertEqual(len(calls),1)

    async def test_private_dns_and_guard_failure_do_not_invoke_transport(self):
        calls=[]
        async def private(*_):return ('127.0.0.1',)
        async def public(*_):return ('93.184.216.34',)
        async def handle(request):calls.append(request);return httpx2.Response(200)
        config=MultipartConfiguration(frozenset({'uploads.example.test'}))
        part=UploadPart(0,0,4,'https://uploads.example.test/p?s=TEST')
        def denied():raise PermissionError('TEST_SECRET_NOT_LOGGED')
        for resolver,guard in ((private,lambda:None),(public,denied)):
            transport=PinnedPartTransport(config,resolver=resolver,transport_factory=lambda:httpx2.MockTransport(handle))
            with self.assertRaisesRegex(UploadGuardError,'^upload_authority_unavailable$'):
                await transport.put(part,b'TEST',timeout=3,ensure_current=permitted,before_dispatch=guard)
        self.assertEqual(calls,[])

    async def test_redirect_is_one_response_not_followed_and_unknown_status_preserved(self):
        async def public(*_):return ('93.184.216.34',)
        calls=[]
        async def handle(request):calls.append(request);return httpx2.Response(302,headers={'Location':'https://evil.example'})
        transport=PinnedPartTransport(MultipartConfiguration(frozenset({'uploads.example.test'})),
            resolver=public,transport_factory=lambda:httpx2.MockTransport(handle))
        result=await transport.put(UploadPart(0,0,4,'https://uploads.example.test/p'),b'TEST',
            timeout=3,ensure_current=permitted,before_dispatch=lambda:None)
        self.assertEqual(result.status,302);self.assertEqual(len(calls),1)

    async def test_cancel_after_dispatch_does_not_retry(self):
        async def public(*_):return ('93.184.216.34',)
        seen=asyncio.Event();calls=[]
        async def handle(request):
            calls.append(request);seen.set();await asyncio.Event().wait()
        transport=PinnedPartTransport(MultipartConfiguration(frozenset({'uploads.example.test'})),
            resolver=public,transport_factory=lambda:httpx2.MockTransport(handle))
        task=asyncio.create_task(transport.put(UploadPart(0,0,4,'https://uploads.example.test/p'),b'TEST',
            timeout=3,ensure_current=permitted,before_dispatch=lambda:None))
        await seen.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(len(calls),1)

    async def test_failing_close_preserves_cancellation_and_has_fixed_success_error(self):
        seen=asyncio.Event();closed=[]
        async def public(*_):return ('93.184.216.34',)
        class Transport(httpx2.AsyncBaseTransport):
            wait=True
            async def handle_async_request(self,request):
                seen.set()
                if self.wait:await asyncio.Event().wait()
                return httpx2.Response(200)
            async def aclose(self):
                closed.append(True);raise OSError('TEST_SIGNED_URL_PRIVATE')
        inner=Transport()
        transport=PinnedPartTransport(MultipartConfiguration(frozenset({'uploads.example.test'})),
            resolver=public,transport_factory=lambda:inner)
        part=UploadPart(0,0,4,'https://uploads.example.test/p')
        task=asyncio.create_task(transport.put(part,b'TEST',timeout=3,ensure_current=permitted,before_dispatch=lambda:None))
        await seen.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        inner.wait=False
        with self.assertRaisesRegex(UploadGuardError,'^upload_authority_unavailable$'):
            await transport.put(part,b'TEST',timeout=3,ensure_current=permitted,before_dispatch=lambda:None)
        self.assertEqual(len(closed),2)

    async def test_dns_barrier_revocation_and_typed_freshness_are_checked_after_resolution(self):
        from types import SimpleNamespace
        from src.plugins.ai_chat.adapters.qq_official.native_upload import OfficialMediaUploader,UploadIntent,issue_upload_permit
        from src.plugins.ai_chat.adapters.qq_official.project_authority import RecipientEvidenceUnavailable
        endpoint=EndpointKey(AccountKey('qq-official','TEST_APP','test'),'group','TEST_TARGET')
        fp=fingerprint([b'TEST'],filename='fixture.pdf',file_type=4)
        prepared=parse_prepare(200,dict(upload_id='TEST',block_size='4',parts=[dict(index=0,block_size='4',
            presigned_url='https://uploads.example.test/p')]),endpoint=endpoint,artifact=fp,allowed_hosts={'uploads.example.test'})
        for failure,reason in ((UploadGuardError(),'part_upload_unavailable'),
                               (RecipientEvidenceUnavailable(),'recipient_evidence_unavailable'),
                               (LeaseLost('TEST_PRIVATE'), 'upload_source_unavailable')):
            current=True;calls=[];checks=[]
            async def resolver(*_):
                nonlocal current
                current=False;await asyncio.sleep(0);return ('93.184.216.34',)
            async def source(*_):
                checks.append(current)
                if not current:raise failure
            async def network(request):calls.append(request);return httpx2.Response(200)
            async def begin(stage):return issue_upload_permit(stage,stage_attempt_id='a'*32,check_current=lambda:None)
            transport=PinnedPartTransport(MultipartConfiguration(frozenset({'uploads.example.test'})),
                resolver=resolver,transport_factory=lambda:httpx2.MockTransport(network))
            rest=SimpleNamespace(base_url='http://127.0.0.1',allow_test_server=True,
                token_manager=SimpleNamespace(credentials=SimpleNamespace(app_id='TEST_APP',is_test=True)))
            uploader=OfficialMediaUploader(rest,ensure_current=source,before_network=begin,clock=lambda:1000,
                allowed_part_hosts={'uploads.example.test'},part_transport=transport)
            if isinstance(failure,LeaseLost):
                with self.assertRaises(UploadSourceLost) as caught:
                    await uploader.put_part(UploadIntent('b'*32,endpoint,fp,1100),prepared,0,b'TEST')
                self.assertNotIn('TEST_PRIVATE',str(caught.exception))
                result=caught.exception.result
            else:
                result=await uploader.put_part(UploadIntent('b'*32,endpoint,fp,1100),prepared,0,b'TEST')
            self.assertEqual((result.disposition,result.category),('proven_unsent',reason))
            self.assertEqual(calls,[]);self.assertFalse(checks[-1])

    async def test_real_httpcore_debug_response_headers_are_redacted_only_in_upload_context(self):
        import io
        import logging
        import httpcore2
        output=io.StringIO();handler=logging.StreamHandler(output)
        logger=logging.getLogger('httpcore2.http11');previous=logger.level;logger.setLevel(logging.DEBUG);logger.addHandler(handler)
        async def resolver(*_):return ('93.184.216.34',)
        def factory():
            transport=httpx2.AsyncHTTPTransport(trust_env=False,retries=0)
            transport._pool=httpcore2.AsyncConnectionPool(network_backend=httpcore2.AsyncMockBackend([
                b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\nLocation: https://uploads.example.test/p?token=TEST_SECRET_SENTINEL\r\n\r\n']))
            return transport
        try:
            transport=PinnedPartTransport(MultipartConfiguration(frozenset({'uploads.example.test'})),
                resolver=resolver,transport_factory=factory)
            await transport.put(UploadPart(0,0,4,'https://uploads.example.test/p?token=TEST_SECRET_SENTINEL'),b'TEST',
                timeout=3,ensure_current=permitted,before_dispatch=lambda:None)
            self.assertNotIn('TEST_SECRET_SENTINEL',output.getvalue())
            self.assertIn('protected_upload_transport',output.getvalue())
            logger.debug('outside-context')
            self.assertIn('outside-context',output.getvalue())
        finally:logger.removeHandler(handler);logger.setLevel(previous)


class ResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_close_failure_still_closes_both_existing_vaults(self):
        from types import SimpleNamespace
        from src.plugins.ai_chat.adapters.qq_official.file_services import FileServices
        seen=[]
        def fail():seen.append('session');raise OSError('TEST_PRIVATE')
        async def calls(fn):return fn()
        instance=SimpleNamespace(_maintenance_lock=asyncio.Lock(),calls=calls,
            session_vault=SimpleNamespace(close=fail),
            native_vault=SimpleNamespace(close=lambda:seen.append('native')),
            link_vault=SimpleNamespace(close=lambda:seen.append('link')))
        with self.assertRaises(OSError):await FileServices._close_resources(instance)
        self.assertEqual(seen,['session','native','link'])

    async def test_cancel_while_persisting_source_loss_is_not_swallowed(self):
        from types import SimpleNamespace
        from src.plugins.ai_chat.adapters.qq_official.native_chunk_dispatch import MultipartDispatcher
        from src.plugins.ai_chat.adapters.qq_official.native_media import NativeLimits
        from src.plugins.ai_chat.adapters.qq_official.native_upload import StageResult
        entered=asyncio.Event()
        async def database(*_):entered.set();await asyncio.Event().wait()
        parent=SimpleNamespace(limits=NativeLimits(),_db=database,_settle_stage=None)
        dispatcher=MultipartDispatcher(parent,MultipartConfiguration(frozenset({'uploads.example.test'})),None,None)
        async def run(*_):
            raise UploadSourceLost(None,StageResult('upload_put','proven_unsent','a'*32))
        dispatcher._run=run
        task=asyncio.create_task(dispatcher(None,None,None))
        await entered.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task


class HostPolicyTests(unittest.TestCase):
    def fixture(self):
        account=AccountKey('qq-official','TEST_APP','test')
        endpoint=EndpointKey(account,'group','TEST_GROUP')
        return dict(version=1,account=account.key,onebot_accounts=[],files=dict(
            private_directory='/tmp/TEST_NEVER_OPENED_MULTIPART',native_routes=[dict(
                endpoint=endpoint.key,endpoint_generation=1,route_epoch=2,capability_revision=3,
                max_bytes=8_000_000,verified_at=100,expires_at=200,contract='TEST_OLD_CONTRACT',file_type=4)]))

    def test_explicit_policy_never_upgrades_old_evidence_or_default_file_limit(self):
        from src.plugins.ai_chat.adapters.qq_official.host_config import HostConfiguration
        from src.plugins.ai_chat.adapters.qq_official.file_services import FileServiceConfig
        raw=self.fixture()
        raw['files']['multipart']=dict(allowed_part_hosts=['uploads.example.test'])
        config=HostConfiguration.parse(json.dumps(raw))
        self.assertIs(config.native_routes[0].multipart,False)
        self.assertEqual(config.native_routes[0].contract,'TEST_OLD_CONTRACT')
        self.assertEqual(config.native_routes[0].expires_at,200)
        self.assertEqual(FileServiceConfig(manager=None,**config.files).max_file_bytes,20*1024*1024)
        raw['files']['native_routes'][0]['multipart']=True
        self.assertIs(HostConfiguration.parse(json.dumps(raw)).native_routes[0].multipart,True)

    def test_multipart_image_bad_types_and_unsigned_transport_configuration_are_rejected(self):
        from src.plugins.ai_chat.adapters.qq_official.host_config import HostConfiguration,HostConfigurationError
        import copy
        raw=self.fixture()
        cases=[]
        for value in (True,'yes',{},dict(allowed_part_hosts=['127.0.0.1']),
                      dict(allowed_part_hosts=['uploads.example.test']*2),
                      dict(allowed_part_hosts=['uploads.example.test'],max_parts=True),
                      dict(allowed_part_hosts=['uploads.example.test'],max_session_seconds=601),
                      dict(allowed_part_hosts=['uploads.example.test'],transport_factory='anything')):
            changed=copy.deepcopy(raw);changed['files']['multipart']=value;cases.append(changed)
        for kind,flag in ((1,True),(4,1),(4,'true')):
            changed=copy.deepcopy(raw);changed['files']['native_routes'][0].update(file_type=kind,multipart=flag);cases.append(changed)
        for changed in cases:
            with self.assertRaises(HostConfigurationError):HostConfiguration.parse(json.dumps(changed))
