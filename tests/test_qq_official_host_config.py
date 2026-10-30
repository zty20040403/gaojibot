"""Optional host features are isolated; unreadable config does not stop ingress."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official.host_config import HostConfiguration
from src.plugins.ai_chat.platform_types import AccountKey


class HostConfigTests(unittest.TestCase):
    def setUp(self):
        self.account=AccountKey('qq-official','TEST_HOST_CONFIG','test')
        self.raw=dict(version=1,account=self.account.key)

    def test_disabled_config_performs_no_file_read(self):
        with patch('src.plugins.ai_chat.adapters.qq_official.host_config._read_secret',side_effect=AssertionError('read')):
            self.assertIsNone(HostConfiguration.from_env({'AI_QQ_MIGRATION_CONFIG_FILE':'/unreadable'}))

    def test_private_file_loads_and_world_readable_file_disables_host(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'host.json'
            path.write_text(json.dumps(self.raw));path.chmod(0o640)
            env=dict(AI_QQ_OFFICIAL_ENABLED='true',AI_QQ_OFFICIAL_HOST_CONFIG_FILE=str(path))
            self.assertEqual(HostConfiguration.from_env(env).account,self.account)
            path.chmod(0o644)
            with self.assertLogs('src.plugins.ai_chat.adapters.qq_official.host_config',level='WARNING') as logs:
                self.assertIsNone(HostConfiguration.from_env(env))
            self.assertNotIn(directory,str(logs.output))

    def test_bad_optional_features_leave_media_and_files_available(self):
        raw=self.raw|dict(files={'private_directory':'/tmp/TEST_FILES','generation_enabled':True},
            host_services=[{'invalid':'TEST'}],media={'asr':True},
            native_background={'schedules':{'enabled':True}})
        with self.assertLogs('src.plugins.ai_chat.adapters.qq_official.host_config',level='WARNING'):
            config=HostConfiguration.parse(json.dumps(raw))
        self.assertTrue(config.media.asr)
        self.assertTrue(config.files['generation_enabled'])
        self.assertIsNone(config.native_schedule)
        self.assertEqual(config.host_services,())

    def test_legacy_proofs_and_migration_settings_are_ignored(self):
        raw=self.raw|dict(onebot_accounts=None,deadlines='obsolete',
            files={'private_directory':'/tmp/TEST_FILES','native_routes':'obsolete','download':'obsolete'})
        self.assertEqual(HostConfiguration.parse(json.dumps(raw)).files,{'private_directory':Path('/tmp/TEST_FILES')})
