"""Exercise search through a leased PostgreSQL job; no network providers."""
import asyncio
from dataclasses import replace
import json
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official.background_agents import OfficialBackgroundAgents
from src.plugins.ai_chat.config import settings
from src.plugins.ai_chat.model_catalog import ModelCatalog, ModelProfile
from src.plugins.ai_chat.tools import sources
from src.plugins.ai_chat.web_search import SearchResult
from tests.qq_official_turn_fixture import ExecutionFixture


class JobSearchBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        f = self.fixture = ExecutionFixture('runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        calls = DatabaseCalls()
        self.addAsyncCleanup(calls.close)
        profile = ModelProfile('TEST_MODEL', 'test', 'openai-chat', 'TEST_MODEL', 'https://example.invalid', api_key_required=False)
        app = SimpleNamespace(settings=replace(settings, search_enabled=True), logger=logging.getLogger(__name__),
            db_call=calls, sandbox_manager=AsyncMock(), message_ledger=f.ledger, model_catalog=ModelCatalog({'TEST_MODEL': profile}, default_profile='TEST_MODEL'),
            model_preferences=SimpleNamespace(get_explicit=lambda _: None))
        runtime = SimpleNamespace(store=SimpleNamespace(transaction=f.tx, admitted=f.admission, clock=lambda: f.now),
                                  jobs=f.jobs, _calls=calls)
        self.service = OfficialBackgroundAgents(runtime, f.token, app)
        self.addAsyncCleanup(self.service.close)
        source = dict(account_key=f.account.key, endpoint_key=f.endpoint.key, actor_key=f.actor.key,
            scope_key=f.endpoint.scope.key, source_execution_id=None, canonical_message_id=f.message.canonical_message_id,
            native_message_id='TEST_INPUT', endpoint_generation=1, route_epoch=1)
        with f.tx() as cursor:
            self.row = self.service.store.submit(cursor, kind=self.service.kind, key='TEST', source=source,
                task='TEST task', title='TEST title', profile='TEST_MODEL', independent=True, workdir='/work/TEST')
        self.job = f.jobs.claim_due('TEST_WORKER', kinds=(self.service.kind,), now=f.now)[0]
        self.service.store.start(self.job.job_id)

    async def test_takeover_preserves_progress_and_rejects_old_writer(self):
        async def call(session):
            return json.loads(await session.execute_tool('web_search', {'queries': ['TEST_PRIVATE_QUERY']}))
        async def provider(*args, all_results, **kwargs):
            all_results.append(SearchResult('TEST', 'https://example.test/TEST_PRIVATE_PATH?key=TEST_SECRET', ''))
            return all_results
        old = await self.service.session(self.row, self.job, AsyncMock())
        with patch.object(sources, 'search_web', side_effect=provider) as search:
            self.assertIn('新链接 1 条', (await call(old))['progress'])
            self.fixture.query('UPDATE durable_jobs SET lease_generation=lease_generation+1 WHERE job_id=?', (self.job.job_id,))
            with self.assertRaises(asyncio.CancelledError):
                await call(old)
            resumed = await self.service.session(self.row, replace(self.job, lease_generation=self.job.lease_generation+1), AsyncMock())
            for _ in range(3):
                self.assertIn('新链接 0 条', (await call(resumed))['progress'])
            self.assertEqual((await call(resumed))['note'], sources.STOP_SEARCH)
            self.assertEqual(search.await_count, 4)
        data = self.fixture.query('SELECT payload_json FROM durable_jobs WHERE job_id=?', (self.job.job_id,))[0]['payload_json']
        payload = json.loads(data)
        self.assertEqual(payload['workdir'], '/work/TEST')
        self.assertEqual(payload['search_budget']['count'], 4)
        self.assertNotIn('TEST_PRIVATE', data)
        self.assertNotIn('TEST_SECRET', data)
        self.assertRegex(payload['search_budget']['seen'][0], r'^[0-9a-f]{64}$')
