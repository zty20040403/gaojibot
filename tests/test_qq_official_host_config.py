"""Protected deployment settings and the real startup factory selection."""
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.plugins.ai_chat.adapters.qq_official.host_config import HostConfiguration, HostConfigurationError, ConfiguredHost
from src.plugins.ai_chat.platform_types import AccountKey
from src.plugins.ai_chat.runtime import AppContext


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
        # Native-only hosts intentionally allow an empty source list. The parser
        # still rejects non-list baselines; startup enforces managed-route needs.
        changes=[{'version':True},{'factory':'untrusted.module'},{'onebot_accounts':None},
                 {'onebot_accounts':self.raw['onebot_accounts']*2},
                 {'files':{'private_directory':'relative','native_routes':[]}},
                 {'host_services':[{'host_id':'all'}]}]
        for change in changes:
            with self.subTest(change=tuple(change)),self.assertRaises(HostConfigurationError):
                HostConfiguration.parse(json.dumps(self.raw|change))
        text=json.dumps(self.raw)[:-1]+',"version":1}'
        with self.assertRaises(HostConfigurationError):HostConfiguration.parse(text)


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
