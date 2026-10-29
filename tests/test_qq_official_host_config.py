"""Protected deployment settings and the real startup factory selection."""
import asyncio
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official.host_config import HostConfiguration, HostConfigurationError, ConfiguredHost
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey
from src.plugins.ai_chat.runtime import AppContext
from src.plugins.ai_chat.onebot_runtime import OneBotRuntime
from tests.qq_official_migration_journal_fixture import JournalFixture


class HostConfigTests(unittest.TestCase):
    def setUp(self):
        self.account=AccountKey('qq-official','TEST_HOST_CONFIG','test')
        self.source=AccountKey('onebot-v11','12345','production')
        self.raw=dict(version=1,account=self.account.key,
            onebot_accounts=[dict(account=self.source.key,deployment_digest='a'*64)])

    def test_disabled_config_performs_no_file_read(self):
        with patch('src.plugins.ai_chat.adapters.qq_official.host_config._read_secret',side_effect=AssertionError('read')):
            self.assertIsNone(HostConfiguration.from_env({'AI_QQ_MIGRATION_CONFIG_FILE':'/unreadable'}))

    def test_private_file_and_exact_namespaces_are_required(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'host.json'
            path.write_text(json.dumps(self.raw));path.chmod(0o600)
            env=dict(AI_QQ_MIGRATION_ENABLED='true',AI_QQ_MIGRATION_CONFIG_FILE=str(path))
            loaded=HostConfiguration.from_env(env)
            self.assertEqual(loaded.account,self.account)
            self.assertEqual(loaded.onebot_accounts[0].account,self.source)
            self.assertNotIn('TEST_',repr(loaded))
            path.chmod(0o644)
            with self.assertRaises(HostConfigurationError) as failure:HostConfiguration.from_env(env)
            self.assertNotIn(directory,str(failure.exception))
            path.chmod(0o600)
            link=Path(directory)/'link';link.symlink_to(path)
            with self.assertRaises(HostConfigurationError):HostConfiguration.from_env({**env,'AI_QQ_MIGRATION_CONFIG_FILE':str(link)})

    def test_unknown_duplicate_dynamic_or_relative_config_is_rejected(self):
        changes=[{'version':True},{'factory':'untrusted.module'},{'onebot_accounts':[]},
                 {'onebot_accounts':self.raw['onebot_accounts']*2},
                 {'files':{'private_directory':'relative','native_routes':[]}},
                 {'host_services':[{'host_id':'all'}]}]
        for change in changes:
            with self.subTest(change=tuple(change)),self.assertRaises(HostConfigurationError):
                HostConfiguration.parse(json.dumps(self.raw|change))
        text=json.dumps(self.raw)[:-1]+',"version":1}'
        with self.assertRaises(HostConfigurationError):HostConfiguration.parse(text)

    def test_native_evidence_retains_original_version_and_absolute_deadline(self):
        endpoint=EndpointKey(self.account,'group','TEST_GROUP')
        raw=copy.deepcopy(self.raw)
        raw['files']=dict(private_directory='/tmp/TEST_NOT_CREATED',generation_enabled=True,native_routes=[dict(
            endpoint=endpoint.key,endpoint_generation=2,route_epoch=3,capability_revision=4,
            max_bytes=1000,verified_at=100,expires_at=101,contract='TEST_OLD_CONTRACT',file_type=4)])
        config=HostConfiguration.parse(json.dumps(raw))
        self.assertEqual(config.native_routes[0].expires_at,101)
        self.assertEqual(config.native_routes[0].endpoint_generation,2)
        self.assertEqual(config.native_routes[0].contract,'TEST_OLD_CONTRACT')
        self.assertEqual(config.native_routes[0].file_type,4)
        for field in ('contract','file_type'):
            missing=copy.deepcopy(raw);missing['files']['native_routes'][0].pop(field)
            with self.subTest(missing=field),self.assertRaises(HostConfigurationError):
                HostConfiguration.parse(json.dumps(missing))
        for invalid_type in (True,4.0,0,'4'):
            invalid=copy.deepcopy(raw);invalid['files']['native_routes'][0]['file_type']=invalid_type
            with self.subTest(file_type=invalid_type),self.assertRaises(HostConfigurationError):
                HostConfiguration.parse(json.dumps(invalid))
        raw['files']['native_routes'][0]['endpoint']=EndpointKey(self.source,'group','123').key
        with self.assertRaises(HostConfigurationError):HostConfiguration.parse(json.dumps(raw))

    def test_wrong_epoch_account_cannot_construct_any_services(self):
        config=HostConfiguration.parse(json.dumps(self.raw))
        migration=SimpleNamespace(official_factory=Mock())
        host=ConfiguredHost(config,SimpleNamespace(),migration)
        with self.assertRaises(HostConfigurationError):
            host.migration_factory(object(),SimpleNamespace(account_key=self.source.key))
        migration.official_factory.assert_not_called()

    def test_actual_application_startup_passes_all_host_factories(self):
        host=SimpleNamespace(migration_factory=object(),files_factory=object(),approval_factory=object(),
            media_factory=object(),background_factory=object())
        context=SimpleNamespace(_closed=False,_closing=False,official_runtime=None,database=object(),
            message_ledger=object(),logger=Mock(),official_host=host)
        official=SimpleNamespace(start=AsyncMock(return_value=True),stop=AsyncMock())
        with patch('src.plugins.ai_chat.adapters.qq_official.runtime.OfficialRuntime',return_value=official) as factory:
            self.assertTrue(asyncio.run(AppContext.start_official_ingress(context,{'AI_QQ_OFFICIAL_ENABLED':'true'})))
        self.assertIs(factory.call_args.kwargs['migration_factory'],host.migration_factory)
        self.assertIs(factory.call_args.kwargs['file_services_factory'],host.files_factory)
        self.assertIs(factory.call_args.kwargs['approval_factory'],host.approval_factory)
        self.assertIs(factory.call_args.kwargs['media_factory'],host.media_factory)
        self.assertIs(factory.call_args.kwargs['background_factory'],host.background_factory)


class HostConfiguredStartupTests(JournalFixture,unittest.IsolatedAsyncioTestCase):
    async def test_real_startup_reads_protected_file_and_installs_one_baseline_and_control(self):
        self.source_connection.close()
        calls=DatabaseCalls()
        context=SimpleNamespace(database=self.database,delivery_store=self.deliveries,db_call=calls,
            mobile_authorization=SimpleNamespace(store=self.mapping_security),model_catalog=Mock(),
            sandbox_manager=None,fleet_client=None,job_worker=SimpleNamespace())
        runtime=object.__new__(OneBotRuntime)
        runtime.migration_service_factory=None
        runtime._handlers=SimpleNamespace(chat=SimpleNamespace(_conversation_scope=lambda event:self.source_scope))
        with TemporaryDirectory() as directory:
            path=Path(directory)/'host.json'
            path.write_text(json.dumps(dict(version=1,account=self.account.key,
                onebot_accounts=[dict(account=self.source.account.key,deployment_digest='e'*64)])))
            path.chmod(0o600)
            try:
                with patch.dict('os.environ',AI_QQ_MIGRATION_ENABLED='true',AI_QQ_MIGRATION_CONFIG_FILE=str(path)):
                    await runtime._start_migration_service(context)
                self.assertIs(context.migration_service,runtime.migration_routes)
                self.assertIs(context.official_host.migration,context.migration_service)
                self.assertEqual(context.job_worker.source_context,context.migration_service.job_context)
                self.assertIs(context.migration_service.admin_service(),context.migration_service.console)
                rows=self.query('SELECT * FROM migration_runtime_baselines WHERE account_key=?',(self.source.account.key,))
                self.assertEqual(rows[0]['deployment_digest'],'e'*64)
                self.assertEqual(rows[0]['state'],'ready')
                self.assertEqual(self.query('SELECT * FROM migration_journals'),[])
            finally:
                if getattr(context,'migration_service',None) is not None:await context.migration_service.close()
                await calls.close()
