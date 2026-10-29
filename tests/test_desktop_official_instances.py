from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from src.local_runtime.instances import InstanceStore,ConfigurationConflict
from src.local_runtime.files import write_json
from src.local_runtime.official_configuration import official_settings,read_credentials
from src.local_runtime.runtime import runtime_environment,compose_document
from src.local_runtime.desktop import desktop_compose


class OfficialInstancesTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=InstanceStore(self.root)
        self.config=self.store.create('TEST');self.id=self.config['id']
        self.settings=deepcopy(self.config['settings'])
        self.settings.update(platforms=dict(onebot=False,qq_official=True),official=official_settings(),
            console_admin_username='admin',default_model='test',models=[dict(id='test',model='TEST',base_url='https://example.invalid/v1')])
        self.settings['official']['exclusive_deployment_confirmed']=True
        self.pair={'app_id':'TEST_NATIVE_APP','app_secret':'TEST_NATIVE_SECRET'}

    def save(self,revision=1,credentials=None):
        return self.store.update(self.id,self.settings,expected_revision=revision,model_keys={'test':'TEST_MODEL_KEY'},
            official_credentials=self.pair if credentials is None else credentials)

    def test_actual_revision_save_snapshot_environment_and_public_non_disclosure(self):
        saved=self.save();config,private=self.store.snapshot(self.id)
        self.assertEqual(saved['revision'],2)
        self.assertIn('official_reference',private)
        result=runtime_environment(config,private)
        self.assertEqual(result['AI_ONEBOT_ENABLED'],'false')
        self.assertEqual(result['AI_QQ_OFFICIAL_ENABLED'],'true')
        self.assertEqual(result['AI_ADMIN_USER_IDS'],'')
        self.assertEqual(result['SUPERUSERS'],'[]')
        self.assertNotIn('TEST_NATIVE',json.dumps(result))
        public=self.store.public_status(self.id);encoded=json.dumps(public)
        for value in ('TEST_NATIVE_APP','TEST_NATIVE_SECRET','TEST_MODEL_KEY','official_credential_ref'):
            self.assertNotIn(value,encoded)
        self.assertTrue(public['official_configuration']['credentials_saved'])
        self.assertEqual(public['official_configuration']['global_exclusivity'],'unknown')

    def test_crash_before_instance_commit_keeps_exact_previous_bundle(self):
        first=self.save();original=write_json
        def fail(path,value,**kw):
            if path.name=='instance.json':raise OSError('TEST_CRASH')
            return original(path,value,**kw)
        with patch('src.local_runtime.instances.write_json',fail),self.assertRaises(OSError):
            self.save(2,self.pair|{'app_secret':'TEST_NEW_SECRET'})
        current=self.store.read(self.id)
        self.assertEqual(current['revision'],first['revision'])
        self.assertEqual(current['official_credential_ref'],first['official_credential_ref'])
        self.assertEqual(read_credentials(self.store.directory(self.id),current['official_credential_ref']).credentials.app_secret,'TEST_NATIVE_SECRET')
        with self.assertRaises(ConfigurationConflict):self.save(1)

    def test_same_app_other_local_instance_risk_does_not_claim_global_exclusivity(self):
        self.save();other=self.store.create('TEST_OTHER')
        self.store.update(other['id'],self.settings,expected_revision=1,official_credentials=self.pair)
        status=self.store.public_status(self.id)['official_configuration']
        self.assertEqual(status['same_app_local_instances'],1)
        self.assertEqual(status['global_exclusivity'],'unknown')

    def test_official_only_server_and_named_volume_compose_never_defines_napcat(self):
        config=self.save();directory=self.store.directory(self.id)
        images={'postgres':'example.invalid/postgres@sha256:'+'a'*64,'napcat':'example.invalid/napcat@sha256:'+'b'*64}
        spec=compose_document(config,directory,runtime_image='gaoji-app-runtime:'+'c'*64,images=images)
        self.assertNotIn('napcat',spec['services'])
        self.assertEqual(spec['services']['bot']['user'],'10001:10001')
        selected=[x for x in spec['services']['bot']['volumes'] if '/official-' in x['target']]
        self.assertEqual(len(selected),1);self.assertTrue(selected[0]['read_only'])
        deploy=dict(job='d'*32,runtime_image='gaoji-app-runtime:'+'c'*64,volume='gaoji-desktop-'+'d'*32+'-data',network='TEST_NETWORK')
        desktop=desktop_compose(config,directory,deploy,images,root=self.root)
        self.assertNotIn('napcat',desktop['services'])
        mount=[x for x in desktop['services']['bot']['volumes'] if x['target']=='/run/gaoji']
        self.assertEqual(len(mount),1);self.assertTrue(mount[0]['read_only']);self.assertEqual(mount[0]['type'],'volume')
        self.assertTrue(mount[0]['volume']['subpath'].endswith('/runtime-config'))
        self.assertNotIn('TEST_NATIVE',json.dumps(desktop))

    def test_dual_still_requires_real_legacy_identity_and_preserves_napcat(self):
        self.settings['platforms']['onebot']=True;self.save()
        from src.local_runtime.validation import InstanceError
        with self.assertRaises(InstanceError):runtime_environment(*self.store.snapshot(self.id))
        self.settings.update(bot_qq='12345678',admin_qq='23456789');self.save(2)
        config,private=self.store.snapshot(self.id)
        result=runtime_environment(config,private)
        self.assertEqual((result['AI_ONEBOT_ENABLED'],result['AI_QQ_OFFICIAL_ENABLED']),('true','true'))
        image='example.invalid/image@sha256:'+'a'*64
        self.assertIn('napcat',compose_document(config,self.store.directory(self.id),runtime_image='gaoji-app-runtime:'+'c'*64,images={'postgres':image,'napcat':image})['services'])
