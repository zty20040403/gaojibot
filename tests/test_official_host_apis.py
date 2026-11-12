"""Official job authority, actual PostgreSQL stop fences and relay binding."""
from functools import partial
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import httpx
import httpx2

from src.plugins.ai_chat.adapters.qq_official.background_agents import OfficialBackgroundAgents
from src.plugins.ai_chat.adapters.qq_official.host_config import HostConfiguration
from src.plugins.ai_chat.host_apis import HostAPI
from src.plugins.ai_chat.host_api_transport import PinnedAPITransport,forward
from tests import test_background_video as video_fixture
from tests.test_codex_gateway import free_port
from tests.test_host_api_transport import Stream


class OfficialHostAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await video_fixture.BackgroundVideoTests.asyncSetUp(self)
        self.app.database = self.f.database
        self.app.settings = replace(self.app.settings,background_gateway_port=free_port())
        api = HostAPI.parse('test',dict(base_url='https://api.example.test',secret_file='/TEST_PRIVATE',
            headers={'Authorization':'Bearer {secret}'}))
        self.app.official_host = SimpleNamespace(config=HostConfiguration(self.f.account,apis={'test':api}))
        self.service = OfficialBackgroundAgents(self.service.runtime,self.f.token,self.app)
        self.addAsyncCleanup(self.service.close)
        row = self.service.store.get(self.job.job_id)
        self.session = await self.service.session(row,self.job,AsyncMock())
        self.seen = []
        def wire(request):
            self.seen.append(request)
            return httpx2.Response(200,stream=Stream(b'TEST accepted'))
        self.service.api_proxy.transport = partial(forward,transport_factory=lambda url:PinnedAPITransport(url,
            resolver=AsyncMock(return_value=('8.8.8.8',)),transport=httpx2.MockTransport(wire)))
        await self.service.gateway.start()
        self.token,_ = self.service.gateway.open(source=row,current=self.session.current)
        self.client = httpx.AsyncClient(trust_env=False,headers={'Authorization':'Bearer '+self.token})
        self.addAsyncCleanup(self.client.aclose)
        self.url = f'http://127.0.0.1:{self.service.gateway.port}/api/test/repos'

    async def test_stop_then_start_never_revalidates_an_old_job_token(self):
        from unittest.mock import patch
        with patch('src.plugins.ai_chat.host_apis._read_secret',return_value='TEST_HOST_KEY'):
            self.assertEqual((await self.client.post(self.url)).status_code,200)
            with self.f.tx() as cursor:
                cursor.execute('''INSERT INTO conversation_settings(scope_key,stopped,stop_generation)
                    VALUES (?,TRUE,1)''',(self.f.endpoint.scope.key,))
            self.assertEqual((await self.client.post(self.url)).status_code,403)
            with self.f.tx() as cursor:
                cursor.execute('UPDATE conversation_settings SET stopped=FALSE')
            self.assertEqual((await self.client.post(self.url)).status_code,403)
        self.assertEqual(len(self.seen),1)

    async def test_persisted_job_cancel_is_checked_without_waiting_for_token_revocation(self):
        self.assertIsNotNone(self.service.api_proxy)
        self.assertIn('/api/test',self.session.prompt)
        self.assertNotIn('TEST_PRIVATE',self.session.prompt)
        self.service.store.cancel(self.job.job_id,self.f.endpoint.key,self.f.actor.key)
        response = await self.client.post(self.url)
        self.assertEqual(response.status_code,403,response.text)
        self.assertEqual(self.seen,[])
