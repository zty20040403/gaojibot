"""Protected host file -> application startup -> actual per-account wiki worker."""
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official.config import OfficialConfig
from src.plugins.ai_chat.adapters.qq_official.runtime import OfficialRuntime
from src.plugins.ai_chat.adapters.qq_official.wiki_service import OfficialWikiService
from src.plugins.ai_chat.context_store import ContextStore
from src.plugins.ai_chat.onebot_runtime import OneBotRuntime
from src.plugins.ai_chat.quota import UsageStore
from src.plugins.ai_chat.runtime import AppContext
from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker
from tests.qq_official_migration_journal_fixture import JournalFixture
from tests.test_qq_official_runtime import ControlledProtocol,loaded_config


class HostBusinessPGTests(JournalFixture,unittest.IsolatedAsyncioTestCase):
    async def test_protected_host_actual_runtime_registers_maintains_and_closes_wiki(self):
        await self.host_lifecycle(all_features=False)

    async def test_media_schedule_proactive_and_wiki_share_actual_epoch_and_close(self):
        await self.host_lifecycle(all_features=True)

    async def host_lifecycle(self, *, all_features):
        self.source_connection.close();self.account_lease.close()
        calls=DatabaseCalls();context_store=ContextStore(self.database,historian_managed=True)
        usage=UsageStore(self.database);self.addCleanup(usage.close)
        context=SimpleNamespace(database=self.database,delivery_store=self.deliveries,db_call=calls,
            mobile_authorization=SimpleNamespace(store=self.mapping_security),model_catalog=Mock(),
            sandbox_manager=None,fleet_client=None,job_worker=SimpleNamespace(),_closed=False,_closing=False,
            official_runtime=None,message_ledger=self.ledger,turn_journal=self.journal,context_store=context_store,
            logger=Mock(),usage_store=usage,settings=SimpleNamespace(system_prompt='TEST persona'),subagent_store=None,subagent_coordinator=None)
        onebot=object.__new__(OneBotRuntime);onebot.migration_service_factory=None
        onebot._handlers=SimpleNamespace(chat=SimpleNamespace(_conversation_scope=lambda event:self.source_scope))
        protocol=ControlledProtocol();registered=[];maintained=[];closed=[]
        old_register=DurableJobWorker.register;old_maintain=OfficialWikiService.maintain;old_close=OfficialWikiService.close
        def register(worker,kind,handler,**kw):
            registered.append((kind,handler));return old_register(worker,kind,handler,**kw)
        async def maintain(service):
            await old_maintain(service);maintained.append(service)
        def close(service):
            old_close(service);closed.append(service)
        def runtime_factory(config,database,**kw):
            return OfficialRuntime(config,database,**kw,protocol_factory=protocol,
                probe_seconds=0.02,turn_consumer_factory=lambda *_:AsyncMock())
        try:
            with TemporaryDirectory() as directory:
                data=dict(version=1,account=self.account.key,
                    onebot_accounts=[dict(account=self.source.account.key,deployment_digest='e'*64)],
                    native_background=dict(wiki=dict(enabled=True,profile='TEST_WIKI',hour=3,timezone='UTC',check_seconds=30)))
                if all_features:
                    data['media']=dict(asr=True,documents=True,forwards=True,emoji=True)
                    data['native_background'].update(
                        schedules=dict(enabled=True,policy_id='TEST_HOST_COST',revision=1,
                            authorization_until=self.now+3600,profile='TEST_SCHEDULE'),
                        proactive=dict(enabled=True,profile='TEST_PROACTIVE',percent=100,max_checks_per_hour=2))
                path=Path(directory)/'host.json';path.write_text(json.dumps(data))
                path.chmod(0o600)
                with patch.dict('os.environ',AI_QQ_MIGRATION_ENABLED='true',AI_QQ_MIGRATION_CONFIG_FILE=str(path)):
                    await onebot._start_migration_service(context)
                context.model_catalog.resolve.assert_called_with('TEST_WIKI')
                with patch.object(OfficialConfig,'from_env',return_value=OfficialConfig(enabled=True)),\
                     patch.object(OfficialConfig,'load',return_value=loaded_config(self.account.app_id,public=True)),\
                     patch('src.plugins.ai_chat.adapters.qq_official.runtime.OfficialRuntime',side_effect=runtime_factory),\
                     patch.object(DurableJobWorker,'register',register),\
                     patch.object(OfficialWikiService,'maintain',maintain),\
                     patch.object(OfficialWikiService,'close',close):
                    self.assertTrue(await AppContext.start_official_ingress(context,{}))
                    await protocol.next_ready()
                    actual=context.official_runtime;service=actual.background_service;wiki=service.wiki
                    self.assertIs(actual.native_commands,service)
                    self.assertIsInstance(wiki,OfficialWikiService)
                    self.assertIs(wiki.runtime,actual)
                    if all_features:
                        media=actual.media_service
                        self.assertIsNotNone(media)
                        self.assertIs(media.token,actual._token)
                        self.assertIs(service.domain.token,actual._token)
                        self.assertIs(service.proactive.token,actual._token)
                        self.assertIs(service.previous_guard,actual.migration_service.guard)
                        self.assertEqual(actual.outbox.source_guard,service.plan_guard)
                    self.assertTrue(any(kind==wiki.store.job_kind and handler.__self__ is wiki for kind,handler in registered))
                    for _ in range(100):
                        if maintained:break
                        await asyncio.sleep(0.01)
                    self.assertIn(wiki,maintained)
                    self.assertEqual(self.query('SELECT * FROM group_wiki_settings'),[])
                    self.assertEqual(self.query('SELECT * FROM deliveries'),[])
                    await actual.stop()
                    self.assertIn(wiki,closed)
                    self.assertIsNone(actual.native_commands)
                    self.assertIsNone(actual.background_service)
                    if all_features:
                        self.assertTrue(media.closed)
                        self.assertIsNone(actual.media_service)
                    self.assertEqual(len(protocol.closed),1)
        finally:
            if context.official_runtime is not None:await context.official_runtime.stop()
            if getattr(context,'migration_service',None) is not None:await context.migration_service.close()
            context_store.close();await calls.close()
