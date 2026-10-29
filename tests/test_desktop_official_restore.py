from copy import deepcopy
import json

from test_desktop_backups import Fixture, FakeController, FakeMaintenance, CHECKSUM, IMAGE, DEPENDENCY
from src.local_runtime.files import write_json, read_json
from src.local_runtime.backups import validate_snapshot, verify_archive
from src.local_runtime.official_configuration import official_settings, backup_bundle
from src.local_runtime.runtime import runtime_environment
from src.local_runtime.validation import InstanceError


class OfficialRestoreTests(Fixture):
    def setUp(self):
        super().setUp()
        self.settings = deepcopy(self.config['settings'])
        self.settings.update(platforms={'onebot': False, 'qq_official': True}, official=official_settings())
        self.settings['official']['exclusive_deployment_confirmed'] = True
        self.config = self.store.update(self.identifier, self.settings, expected_revision=2,
            official_credentials={'app_id': 'TEST_SOURCE_APP', 'app_secret': 'TEST_SOURCE_SECRET'})
        self.config, self.private = self.store.snapshot(self.identifier)
        self.original_ref = self.config['official_credential_ref']
        self.original_bundle = backup_bundle(self.directory, self.original_ref)
        (self.root / 'jobs').mkdir()
        self.job = '9' * 32
        write_json(self.root / 'jobs' / (self.job + '.json'), {'instance_id': self.identifier, 'checksum': CHECKSUM,
            'runtime_image': IMAGE, 'docker': '/test/docker', 'docker_host': 'unix:///test/engine'})
        release = self.root / 'releases' / CHECKSUM
        release.mkdir(parents=True)
        write_json(release / 'images.lock.json', {'postgres': DEPENDENCY, 'napcat': DEPENDENCY})
        write_json(self.directory / 'config/control.json', {'port': 12345, 'token': 't' * 43})
        self.c = FakeController(self.job, self.root)
        self.c.running = {'database', 'bot'}
        self.c.render(True)
        write_json(self.c.desired_path, {'bot_enabled': True, 'revision': self.config['revision']})

    def operation(self, kind, ident, **extra):
        self.c.operation = {'id': ident, 'kind': kind, 'revision': self.store.read(self.identifier)['revision'],
            'status': 'queued', **extra}
        self.c.persist()
        return FakeMaintenance(self.c)

    def change_account(self):
        self.store.update(self.identifier, self.settings, expected_revision=self.store.read(self.identifier)['revision'],
            official_credentials={'app_id': 'TEST_TARGET_APP', 'app_secret': 'TEST_TARGET_SECRET'})
        self.c.render(True)

    def test_real_archive_restore_preserves_original_pepper_account_and_stops_all_writers(self):
        (self.directory / 'state/test-pending-and-unknown.json').write_text('{"pending":1,"unknown":1}')
        self.operation('backup', '1' * 32).run()
        self.assertEqual(self.c.operation['status'], 'completed', self.c.operation)
        record = self.backups.check('1' * 32, CHECKSUM)
        archive = self.backups.path('1' * 32) / 'snapshot.tar'
        snapshot = verify_archive(archive, expected_checksum=CHECKSUM, max_bytes=record['bytes'])['snapshot']
        self.assertEqual(snapshot['official_bundle'], self.original_bundle)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        self.change_account()
        self.c.calls.clear()
        self.operation('restore', '2' * 32, backup_id='1' * 32).run()
        self.assertEqual(self.c.operation['status'], 'completed', self.c.operation)
        current = self.store.read(self.identifier)
        self.assertNotEqual(current['official_credential_ref'], self.original_ref)
        self.assertEqual(backup_bundle(self.directory, current['official_credential_ref']), self.original_bundle)
        self.assertEqual(backup_bundle(self.directory, self.original_ref), self.original_bundle)
        self.assertFalse(current['settings']['official']['exclusive_deployment_confirmed'])
        self.assertFalse(read_json(self.c.desired_path)['bot_enabled'])
        self.assertNotIn('bot', self.c.running)
        self.assertNotIn('napcat', self.c.running)
        self.assertFalse(any(call[0] == 'up' and 'bot' in call for call in self.c.calls))
        self.assertEqual(json.loads((self.directory / 'state/test-pending-and-unknown.json').read_text()), {'pending': 1, 'unknown': 1})
        public = json.dumps([self.store.public_status(self.identifier), self.backups.list(), self.c.public_operation()])
        for secret in (self.original_bundle['app_id'], self.original_bundle['app_secret'], self.original_bundle['pepper'], 'TEST_TARGET_SECRET'):
            self.assertNotIn(secret, public)

    def test_missing_original_bundle_blocks_start_and_replacement_credentials_persistently(self):
        # A valid old snapshot can contain official DB state without the later bundle extension.
        config = dict(self.config); config.pop('official_credential_ref')
        self.backups.publish('1' * 32, config, self.private, self.dump, CHECKSUM, database_bytes=4096)
        self.change_account()
        self.operation('restore', '2' * 32, backup_id='1' * 32).run()
        self.assertEqual(self.c.operation['status'], 'completed', self.c.operation)
        current, private = self.store.snapshot(self.identifier)
        self.assertTrue(current['official_recovery_required'])
        self.assertNotIn('official_credential_ref', current)
        self.assertFalse(read_json(self.c.desired_path)['bot_enabled'])
        with self.assertRaises(InstanceError): runtime_environment(current, private)
        with self.assertRaises(InstanceError):
            self.store.update(self.identifier, self.settings, expected_revision=current['revision'],
                official_credentials={'app_id': 'TEST_SOURCE_APP', 'app_secret': 'TEST_SOURCE_SECRET'})
        self.assertTrue(self.store.read(self.identifier)['official_recovery_required'])
        self.assertNotIn('bot', self.c.running)

    def test_bundle_tampering_and_original_secret_loss_fail_closed(self):
        self.operation('backup', '1' * 32).run()
        record = self.backups.check('1' * 32, CHECKSUM)
        value = verify_archive(self.backups.path('1' * 32) / 'snapshot.tar', expected_checksum=CHECKSUM, max_bytes=record['bytes'])['snapshot']
        for changes in ({'pepper': ''}, {'account': 'TEST_OTHER'}, {'environment': 'sandbox'}, {'unexpected': 'value'}):
            mutated = deepcopy(value); mutated['official_bundle'].update(changes)
            with self.subTest(keys=tuple(changes)), self.assertRaises(InstanceError): validate_snapshot(mutated)
        (self.directory / 'config/official-credentials' / self.original_ref / 'bind-pepper').unlink()
        with self.assertRaises(InstanceError): self.backups.publish('3' * 32, self.config, self.private, self.dump, CHECKSUM, database_bytes=4096)
        self.assertFalse((self.backups.path('3' * 32) / 'snapshot.tar').exists())
