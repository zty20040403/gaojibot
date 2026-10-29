"""Real loopback listener + TLS proxy; never QQ or public-network requests."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import aiohttp
import httpx2

from src.plugins.ai_chat.adapters.qq_official.download_runtime import (
    CONTRACT, DownloadConfiguration, DownloadError, DownloadHost, LinkRouteEvidence, probe_https,
)
from src.plugins.ai_chat.adapters.qq_official.file_links import ArtifactRef, FileCredentialVault, FileLinkPublisher, VerifiedStream
from src.plugins.ai_chat.adapters.qq_official.host_config import HostConfiguration, ConfiguredHost, HostConfigurationError
from src.plugins.ai_chat.mcp_bridge.http_transport import HttpPolicy, PinnedHttpTransport
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey
from tests.qq_official_download_fixture import TLSProxy, ORIGIN, free_port
from tests.test_qq_official_file_links import MemoryStore


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.account=AccountKey('qq-official','TEST_DOWNLOAD','test')
        self.endpoint=EndpointKey(self.account,'group','TEST_GROUP')
        self.now=1800000000;self.mono=1.0;self.port=free_port()
        self.evidence=LinkRouteEvidence(self.endpoint,1,1,1,self.now,self.now+100,CONTRACT)
        self.config=DownloadConfiguration(ORIGIN,'127.0.0.1',self.port,(self.evidence,))
        self.proxy=await TLSProxy(self.temp.name,self.port).start()
        self.host=DownloadHost(self.config,self.account,probe=self.proxy.probe,
            clock=lambda:self.now,monotonic=lambda:self.mono)
        self.vault=FileCredentialVault(Path(self.temp.name)/'vault')
        self.store=MemoryStore();self.body=b'%PDF-1.4\nTEST_DOWNLOAD\n%%EOF\n'
        self.artifact=ArtifactRef('test-artifact',hashlib.sha256(self.body).hexdigest(),len(self.body),'application/pdf','report.pdf')
        self.publisher=FileLinkPublisher(store=self.store,vault=self.vault,open_artifact=self.reader,origin=ORIGIN,clock=lambda:self.now)
        self.current=True
        self.service=SimpleNamespace(_local=lambda:self.current,token=SimpleNamespace(account_key=self.account.key),
            config=SimpleNamespace(link_origin=ORIGIN),download_publisher=self.publisher)

    @asynccontextmanager
    async def reader(self,ref):
        async def blocks():yield self.body
        yield VerifiedStream(ref.sha256,ref.size,blocks())

    async def asyncTearDown(self):
        await self.host.close();await self.proxy.close();self.vault.close()

    async def ready(self):
        await self.host.start()
        # Let the real startup probe finish; then perform an explicit fresh probe.
        await asyncio.sleep(0.02)
        self.assertTrue(await self.host.check())
        self.host.attach(self.service)

    async def test_protected_configuration_closed_until_listener_and_real_tls_proof(self):
        raw=dict(version=1,account=self.account.key,onebot_accounts=[],files=dict(private_directory='/tmp/TEST_ONLY',
            native_routes=[],download=dict(origin=ORIGIN,listen_host='127.0.0.1',listen_port=self.port,
                routes=[dict(endpoint=self.endpoint.key,endpoint_generation=1,route_epoch=1,capability_revision=1,
                    verified_at=self.now,expires_at=self.now+100,contract=CONTRACT)])))
        parsed=HostConfiguration.parse(json.dumps(raw))
        self.assertEqual(parsed.download,self.config)
        with self.assertRaisesRegex(HostConfigurationError,'independent_origin'):
            ConfiguredHost(parsed,SimpleNamespace(settings=SimpleNamespace(admin_origin=ORIGIN+':443')),None)
        self.assertFalse(self.host.reachable());self.assertIsNone(self.host.publisher())
        await self.ready();self.assertTrue(self.host.reachable())
        self.assertNotIn(self.endpoint.native_conversation_id,repr(parsed.download))
        self.assertNotIn(ORIGIN,repr(parsed.download))

    async def test_invalid_origin_listener_or_evidence_cannot_be_configured(self):
        for value in ('http://download.example.test','https://u:p@download.example.test','https://download.example.test/a',
                      ORIGIN+'?secret=x',ORIGIN+'#x','https://127.0.0.1',ORIGIN+'/'):
            with self.subTest(value=value),self.assertRaises(ValueError):replace(self.config,origin=value)
        for value in ('0.0.0.0','localhost','192.168.1.1'):
            with self.assertRaises(ValueError):replace(self.config,listen_host=value)
        for value in (0,80,True,65536):
            with self.assertRaises(ValueError):replace(self.config,listen_port=value)
        for change in ({'expires_at':self.now},{'capability_revision':True},{'verified_at':-1}):
            with self.assertRaises(ValueError):replace(self.evidence,**change)
        with self.assertRaises(ValueError):replace(self.config,routes=(self.evidence,self.evidence))
        foreign=replace(self.evidence,endpoint=EndpointKey(AccountKey('qq-official','OTHER','test'),'group','TEST_GROUP'))
        with self.assertRaises(DownloadError):DownloadHost(replace(self.config,routes=(foreign,)),self.account)

    async def test_probe_failure_redirect_wrong_proof_tls_and_private_dns_never_ready(self):
        await self.ready()
        for status in (302,500):
            self.proxy.status=status
            self.assertFalse(await self.host.check());self.assertFalse(self.host.reachable())
        self.proxy.status=200;self.proxy.wrong=True
        self.assertFalse(await self.host.check());self.proxy.wrong=False
        with self.assertRaises(DownloadError):
            await probe_https(ORIGIN+'/health/download','x'*64,
                transport_factory=lambda url:self.proxy.transport(url,trusted=False))
        class Never(httpx2.AsyncBaseTransport):
            async def handle_async_request(self,request):raise AssertionError('SSRF connector invoked')
        async def local(*_):return ('127.0.0.1',)
        with self.assertRaises(DownloadError):
            await probe_https(ORIGIN+'/health/download','x'*64,
                transport_factory=lambda url:PinnedHttpTransport(url,HttpPolicy(),resolver=local,transport=Never()))

    async def test_actual_get_head_range_no_session_and_original_expiry(self):
        await self.ready()
        item=await self.publisher.publish(self.artifact,request_key='a'*64,expires_at=self.now+60)
        status,headers,body=await self.proxy.request('GET',item.url,headers={'Cookie':'console=does_not_authorize'})
        self.assertEqual((status,body),(200,self.body));self.assertNotIn('set-cookie',headers)
        self.assertIn('attachment;',headers['content-disposition'])
        self.assertEqual(self.store.rows[item.publication_ref].served_complete_count,1)
        status,_,body=await self.proxy.request('HEAD',item.url)
        self.assertEqual((status,body),(200,b''));self.assertEqual(self.store.rows[item.publication_ref].served_complete_count,1)
        self.assertEqual((await self.proxy.request('GET',item.url,{'Range':'bytes=0-2'}))[0],416)
        self.now+=60
        self.assertEqual((await self.proxy.request('GET',item.url))[0],404)
        self.assertEqual(self.store.rows[item.publication_ref].state,'expired')

    async def test_probe_expiry_does_not_reissue_publication_and_epoch_change_restores_same_token(self):
        await self.ready()
        item=await self.publisher.publish(self.artifact,request_key='b'*64,expires_at=self.now+60)
        self.mono+=61;self.assertFalse(self.host.reachable())
        self.assertEqual((await self.proxy.request('GET',item.url))[0],200)
        self.current=False
        self.assertEqual((await self.proxy.request('GET',item.url))[0],503)
        replacement=SimpleNamespace(_local=lambda:True,token=self.service.token,config=self.service.config,
            download_publisher=FileLinkPublisher(store=self.store,vault=self.vault,open_artifact=self.reader,origin=ORIGIN,clock=lambda:self.now))
        self.host.attach(replacement)
        restored=await replacement.download_publisher.restore(item.credential_ref)
        self.assertEqual(restored,item);self.assertEqual(len(self.store.rows),1)
        self.assertEqual((await self.proxy.request('GET',item.url))[0],200)
        await self.publisher.revoke(item.publication_ref)
        self.assertEqual((await self.proxy.request('GET',item.url))[0],404)

    async def test_listener_unavailable_fixed_error_and_shutdown_closes_port(self):
        await self.ready()
        other=DownloadHost(self.config,self.account,probe=self.proxy.probe)
        with self.assertRaisesRegex(DownloadError,'^download_listener_unavailable$'):await other.start()
        await other.close();await self.host.close();self.assertFalse(self.host.reachable())
        async with aiohttp.ClientSession() as client:
            with self.assertRaises(aiohttp.ClientConnectorError):await client.get(f'http://127.0.0.1:{self.port}/health/download')
