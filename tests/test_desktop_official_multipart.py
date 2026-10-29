"""Controller imports reach the real protected host loader without network/DB."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from src.local_runtime.instances import InstanceStore
from src.local_runtime.official_configuration import materialize_runtime, official_settings
from src.local_runtime.official_files import NATIVE_CONTRACT, file_settings, host_file_configuration
from src.local_runtime.validation import InstanceError
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey


class OfficialMultipartConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.account = AccountKey('qq-official', 'TEST_MULTIPART_CONFIG', 'production')
        self.endpoint = EndpointKey(self.account, 'group', 'TEST_GROUP')
        self.files = file_settings()
        self.files['enabled'] = True
        self.route = dict(endpoint=self.endpoint.key, endpoint_generation=2, route_epoch=3,
                          capability_revision=4, verified_at=100, expires_at=500,
                          contract=NATIVE_CONTRACT, file_type=4, max_bytes=20 * 1024 * 1024)
        self.policy = dict(allowed_part_hosts=['upload.example.test'])

    @contextmanager
    def saved_host(self, files):
        from src.plugins.ai_chat.adapters.qq_official.host_config import HostConfiguration
        with TemporaryDirectory() as temporary:
            store = InstanceStore(Path(temporary))
            instance = store.create('TEST_MULTIPART')
            settings = deepcopy(instance['settings'])
            settings.update(platforms={'onebot': False, 'qq_official': True}, official=official_settings())
            settings['official']['files'] = files
            saved = store.update(instance['id'], settings, expected_revision=1,
                                 official_credentials={'app_id': self.account.app_id, 'app_secret': 'TEST_SECRET'})
            # Read the persisted config, not the caller's request object.
            saved = store.read(saved['id'])
            directory = store.directory(saved['id'])
            target = directory / 'runtime-config'
            target.mkdir(mode=0o700, exist_ok=True)
            name = materialize_runtime(directory, reference=saved['official_credential_ref'],
                                       revision=saved['revision'], environment='production', target_root=target,
                                       files=saved['settings']['official']['files'])
            path = target / name / 'host.json'
            config = HostConfiguration.from_env({'AI_QQ_OFFICIAL_ENABLED': 'true',
                                                'AI_QQ_OFFICIAL_HOST_CONFIG_FILE': str(path)})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            yield config, json.loads(path.read_text()), saved, store

    def test_legacy_proof_roundtrip_stays_small_only_without_config(self):
        self.files['native_routes'] = [self.route]
        before = deepcopy(self.files)
        with self.saved_host(self.files) as (config, raw, saved, _):
            self.assertFalse(config.native_routes[0].multipart)
            self.assertNotIn('multipart', config.files)
            self.assertNotIn('multipart_configuration', config.files)
            self.assertNotIn('multipart', raw['files']['native_routes'][0])
            self.assertEqual(saved['settings']['official']['files'], before)
        self.assertEqual(self.files, before)

    def test_host_policy_does_not_upgrade_old_or_explicit_false_target_proof(self):
        for explicit in (False, True):
            with self.subTest(explicit_false=explicit):
                route = self.route | ({'multipart': False} if explicit else {})
                self.files.update(native_routes=[route], multipart=self.policy)
                with self.saved_host(self.files) as (config, raw, _, _):
                    self.assertFalse(config.native_routes[0].multipart)
                    policy = config.files['multipart_configuration']
                    self.assertEqual(policy.allowed_part_hosts, frozenset({'upload.example.test'}))
                    self.assertEqual((policy.max_parts, policy.max_session_seconds), (128, 600))
                    self.assertEqual(raw['files']['native_routes'][0], route)

    def test_explicit_multipart_proof_reaches_host_with_original_namespace_and_limits(self):
        route = self.route | {'multipart': True}
        policy = self.policy | {'max_parts': 7, 'max_session_seconds': 111}
        self.files.update(native_routes=[route], multipart=policy)
        before = deepcopy(self.files)
        with self.saved_host(self.files) as (config, raw, saved, _):
            evidence = config.native_routes[0]
            self.assertEqual(evidence.endpoint, self.endpoint)
            self.assertTrue(evidence.multipart)
            self.assertEqual((evidence.endpoint_generation, evidence.route_epoch, evidence.capability_revision), (2, 3, 4))
            self.assertEqual((evidence.verified_at, evidence.expires_at, evidence.max_bytes), (100, 500, 20 * 1024 * 1024))
            self.assertEqual(config.files['multipart_configuration'].max_parts, 7)
            self.assertEqual(config.files['multipart_configuration'].max_session_seconds, 111)
            self.assertEqual(raw['files']['native_routes'][0], route)
            self.assertEqual(raw['files']['multipart'], policy)
            self.assertEqual(saved['settings']['official']['files'], before)
        self.assertEqual(self.files, before)

    def test_proof_alone_does_not_install_upload_host_policy(self):
        self.files['native_routes'] = [self.route | {'multipart': True}]
        with self.saved_host(self.files) as (config, raw, _, _):
            self.assertTrue(config.native_routes[0].multipart)
            self.assertNotIn('multipart_configuration', config.files)
            self.assertNotIn('multipart', raw['files'])

    def test_multipart_strict_bool_and_file_type_and_size_bounds(self):
        for value in (None, 0, 1, 'true', [], {}):
            with self.subTest(value=value):
                self.files['native_routes'] = [self.route | {'multipart': value}]
                with self.assertRaises(InstanceError):
                    file_settings(self.files)
        for change in ({'file_type': 1, 'multipart': True}, {'max_bytes': 20 * 1024 * 1024 + 1},
                       {'max_bytes': True}, {'file_type': True}):
            with self.subTest(change=change):
                self.files['native_routes'] = [self.route | {'multipart': True} | change]
                with self.assertRaises(InstanceError):
                    file_settings(self.files)
        self.files['native_routes'] = [self.route | {'file_type': 1, 'multipart': False, 'max_bytes': 3500000}]
        self.assertFalse(file_settings(self.files)['native_routes'][0]['multipart'])

    def test_only_explicit_unique_bounded_ascii_dns_hosts_are_accepted(self):
        bad = ['', 'localhost', 'localhost.localdomain', 'host.local', 'host.localhost', '*.example.test', 'UPLOAD.example.test',
               'https://upload.example.test', 'upload.example.test:443', 'upload.example.test/path',
               'upload.example.test.', 'upload..example.test', 'u_pload.example.test', '-upload.example.test',
               'upload-.example.test', '上载.example.test', '127.0.0.1', '127.1', '8.8.8.8', '::1', '[::1]',
               'upload.example.test\n', 'x' * 64 + '.test', True, None, {}]
        for host in bad:
            with self.subTest(host=host):
                self.files['multipart'] = {'allowed_part_hosts': [host]}
                with self.assertRaises(InstanceError):
                    file_settings(self.files)
        for hosts in ([], ['upload.example.test'] * 2, [f'u{i}.example.test' for i in range(17)], 'upload.example.test'):
            with self.subTest(hosts=hosts):
                self.files['multipart'] = {'allowed_part_hosts': hosts}
                with self.assertRaises(InstanceError):
                    file_settings(self.files)
        self.files['multipart'] = {'allowed_part_hosts': [f'u{i}.example.test' for i in range(16)]}
        self.assertEqual(len(file_settings(self.files)['multipart']['allowed_part_hosts']), 16)

    def test_policy_rejects_unknown_fields_null_and_bool_limits(self):
        for policy in (None, {}, self.policy | {'transport': 'callback'}, self.policy | {'verified': True}):
            self.files['multipart'] = policy
            with self.subTest(policy=policy), self.assertRaises(InstanceError):
                file_settings(self.files)
        for field, maximum in [('max_parts', 128), ('max_session_seconds', 600)]:
            for value in (0, maximum + 1, True, 1.0, '1', None):
                self.files['multipart'] = self.policy | {field: value}
                with self.subTest(field=field, value=value), self.assertRaises(InstanceError):
                    file_settings(self.files)
            for value in (1, maximum):
                self.files['multipart'] = self.policy | {field: value}
                self.assertEqual(file_settings(self.files)['multipart'][field], value)

    def test_multipart_never_changes_download_evidence_schema_or_account_check(self):
        self.files['download'] = dict(origin='https://downloads.example.test', listen_host='127.0.0.1',
                                     listen_port=18081, routes=[{key: value for key, value in self.route.items()
                                     if key not in {'file_type', 'max_bytes'}} | {'multipart': True}])
        with self.assertRaises(InstanceError):
            file_settings(self.files)
        self.files['download'] = None
        other = EndpointKey(AccountKey('qq-official', 'TEST_OTHER', 'production'), 'group', 'TEST_GROUP')
        self.files.update(native_routes=[self.route | {'endpoint': other.key, 'multipart': True}], multipart=self.policy)
        with self.assertRaises(InstanceError):
            host_file_configuration(self.files, account=self.account)

    def test_invalid_import_does_not_publish_new_instance_revision(self):
        self.files.update(native_routes=[self.route], multipart=self.policy)
        with self.saved_host(self.files) as (_, _, saved, store):
            changed = deepcopy(saved['settings'])
            changed['official']['files']['multipart']['allowed_part_hosts'] = ['*.example.test']
            with self.assertRaises(InstanceError):
                store.update(saved['id'], changed, expected_revision=saved['revision'])
            self.assertEqual(store.read(saved['id']), saved)

    def test_root_file_link_ttl_survives_composed_multipart_host(self):
        from src.plugins.ai_chat.adapters.qq_official.delivery_configuration import DeliveryConfiguration
        from src.plugins.ai_chat.adapters.qq_official.file_services import FileServiceConfig
        from src.plugins.ai_chat.adapters.qq_official.host_config import ConfiguredHost
        self.files.update(native_routes=[self.route | {'multipart': True}], multipart=self.policy)
        with self.saved_host(self.files) as (config, _, _, _):
            delivery = DeliveryConfiguration.from_env({'AI_QQ_OFFICIAL_FILE_LINK_TTL_SECONDS': '120'})
            host = ConfiguredHost(config, SimpleNamespace(), None, file_link_ttl_seconds=delivery.file_link_ttl_seconds)
            runtime_config = FileServiceConfig(manager=None, **config.files, link_ttl_seconds=host.file_link_ttl_seconds)
            self.assertEqual(runtime_config.link_ttl_seconds, 120)
            self.assertEqual(runtime_config.max_file_bytes, 20 * 1024 * 1024)
            self.assertIs(runtime_config.multipart_configuration, config.files['multipart_configuration'])
            self.assertTrue(host.evidence[(self.endpoint.key, 4)].multipart)
