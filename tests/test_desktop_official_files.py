from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.local_runtime.official_files import file_settings, host_file_configuration, PRIVATE_DIRECTORY, NATIVE_CONTRACT, DOWNLOAD_CONTRACT
from src.local_runtime.official_configuration import official_settings, materialize_runtime, stage_credentials
from src.local_runtime.instances import InstanceStore
from src.local_runtime.runtime import compose_document
from src.local_runtime.desktop import desktop_compose
from src.local_runtime.validation import InstanceError
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey


class FileConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.account=AccountKey('qq-official','TEST_FILE_CONFIG','production')
        self.endpoint=EndpointKey(self.account,'group','TEST_GROUP')
        self.value=file_settings();self.value['enabled']=True
        self.value['download']=dict(origin='https://downloads.example.com',listen_host='127.0.0.1',listen_port=18081,routes=[])

    def route(self,*,native=False,endpoint=None):
        value=dict(endpoint=(endpoint or self.endpoint).key,endpoint_generation=2,route_epoch=3,capability_revision=4,
                   verified_at=100,expires_at=500,contract=NATIVE_CONTRACT if native else DOWNLOAD_CONTRACT)
        if native:value.update(file_type=4,max_bytes=3500000)
        return value

    def test_default_off_empty_routes_allow_protected_pickup_but_no_route_evidence(self):
        self.assertIsNone(host_file_configuration(None,account=self.account))
        value=host_file_configuration(self.value,account=self.account)
        self.assertEqual(value['private_directory'],PRIVATE_DIRECTORY)
        self.assertEqual(value['download']['routes'],[])
        self.assertEqual(value['native_routes'],[])
        self.assertFalse(value['generation_enabled'])
        self.assertNotIn('reachable',value)

    def test_exact_evidence_preserved_no_timestamp_replacement_and_no_input_mutation(self):
        self.value['native_routes']=[self.route(native=True)]
        self.value['download']['routes']=[self.route()]
        before=deepcopy(self.value)
        host=host_file_configuration(self.value,account=self.account)
        self.assertEqual(host['download']['routes'][0],self.route())
        self.assertEqual(host['native_routes'][0],self.route(native=True))
        host['download']['routes'][0]['verified_at']=0
        self.assertEqual(self.value,before)

    def test_cross_account_environment_and_duplicate_endpoint_rejected(self):
        for other in (AccountKey('qq-official','TEST_OTHER','production'),AccountKey('qq-official','TEST_FILE_CONFIG','sandbox')):
            value=deepcopy(self.value);value['download']['routes']=[self.route(endpoint=EndpointKey(other,'group','TEST_GROUP'))]
            with self.assertRaises(InstanceError):host_file_configuration(value,account=self.account)
        self.value['download']['routes']=[self.route(),self.route()]
        with self.assertRaises(InstanceError):file_settings(self.value)

    def test_routes_strict_contract_integer_and_opaque_endpoint(self):
        base=self.route()
        for key,value in [('verified_at',True),('expires_at',100),('route_epoch',0),('capability_revision',float('nan')),('contract','future-contract'),('endpoint','../private')]:
            candidate=deepcopy(self.value);candidate['download']['routes']=[base|{key:value}]
            with self.subTest(key=key),self.assertRaises(InstanceError):file_settings(candidate)
        candidate=deepcopy(self.value);candidate['download']['routes']=[base|{'file_info':'TEST_HANDLE'}]
        with self.assertRaises(InstanceError):file_settings(candidate)
        slash=EndpointKey(self.account,'group','opaque/组:原值')
        candidate['download']['routes']=[self.route(endpoint=slash)]
        self.assertEqual(file_settings(candidate)['download']['routes'][0]['endpoint'],slash.key)

    def test_origin_and_listener_never_widen_to_host_or_public_interface(self):
        for origin in ('http://example.com','https://user:pass@example.com','https://example.com/','https://example.com/path','https://example.com?token=private','https://127.0.0.1','https://[::1]','https://localhost','https://example.com:8443'):
            value=deepcopy(self.value);value['download']['origin']=origin
            with self.subTest(origin=origin),self.assertRaises(InstanceError):file_settings(value)
        for field,value in [('listen_host','0.0.0.0'),('listen_host','localhost'),('listen_port',True),('listen_port',8080),('listen_port',65536)]:
            candidate=deepcopy(self.value);candidate['download'][field]=value
            with self.subTest(field=field,value=value),self.assertRaises(InstanceError):file_settings(candidate)
        for extra in ('private_directory','token','reachable','raw_url'):
            candidate=deepcopy(self.value);candidate[extra]='TEST_PRIVATE'
            with self.assertRaises(InstanceError):file_settings(candidate)

    def test_actual_private_runtime_materialization_revision_and_compose_mount(self):
        with TemporaryDirectory() as temp:
            root=Path(temp);root.chmod(0o700);(root/'config').mkdir(mode=0o700);runtime=root/'runtime-config';runtime.mkdir(mode=0o700)
            ref=stage_credentials(root,environment='production',previous=None,supplied=dict(app_id=self.account.app_id,app_secret='TEST_FILE_SECRET'))
            name=materialize_runtime(root,reference=ref,revision=1,environment='production',target_root=runtime,files=self.value)
            host=json.loads((runtime/name/'host.json').read_text())
            self.assertEqual(host['files']['private_directory'],'/data/state/qq-official-files')
            self.assertEqual(host['account'],self.account.key)
            self.assertEqual((runtime/name/'host.json').stat().st_mode&0o777,0o600)
            self.assertEqual(materialize_runtime(root,reference=ref,revision=1,environment='production',target_root=runtime,files=self.value),name)
            changed=deepcopy(self.value);changed['download']['origin']='https://second.example.com'
            with self.assertRaises(InstanceError):materialize_runtime(root,reference=ref,revision=1,environment='production',target_root=runtime,files=changed)
            new=materialize_runtime(root,reference=ref,revision=2,environment='production',target_root=runtime,files=changed)
            self.assertNotEqual(name,new)
            self.assertEqual(json.loads((runtime/name/'host.json').read_text()),host)

    def test_actual_settings_save_refuses_account_rotation_with_old_route(self):
        with TemporaryDirectory() as temp:
            store=InstanceStore(Path(temp));config=store.create('TEST');settings=deepcopy(config['settings'])
            settings.update(platforms={'onebot':False,'qq_official':True},official=official_settings())
            self.value['download']['routes']=[self.route()];settings['official']['files']=self.value
            saved=store.update(config['id'],settings,expected_revision=1,official_credentials={'app_id':self.account.app_id,'app_secret':'TEST_SECRET'})
            with self.assertRaises(InstanceError):store.update(config['id'],settings,expected_revision=2,official_credentials={'app_id':'TEST_OTHER','app_secret':'TEST_SECRET'})
            self.assertEqual(store.read(config['id'])['revision'],2)
            image='example.invalid/image@sha256:'+'b'*64;images=dict(napcat=image,postgres=image)
            spec=compose_document(saved,store.directory(config['id']),runtime_image='gaoji-app-runtime:'+'a'*64,images=images)
            self.assertNotIn('napcat',spec['services'])
            self.assertEqual([m for m in spec['services']['bot']['volumes'] if m['target']=='/data/state'][0]['source'],str(store.directory(config['id'])/'state'))
            self.assertEqual(len(spec['services']['bot']['ports']),1)  # No fake loopback port publish.
            deploy=dict(job='c'*32,runtime_image='gaoji-app-runtime:'+'a'*64,volume='gaoji-desktop-'+'c'*32+'-data',network='TEST_NETWORK')
            desktop=desktop_compose(saved,store.directory(config['id']),deploy,images,root=Path(temp))
            mount=next(m for m in desktop['services']['bot']['volumes'] if m['target']=='/data/state')
            self.assertEqual(mount['type'],'volume');self.assertTrue(mount['volume']['subpath'].endswith('/state'))

    def test_host_file_size_limit_rejects_without_truncating_evidence(self):
        self.value['download']['routes']=[self.route(endpoint=EndpointKey(self.account,'group','g'+str(i)+'x'*200)) for i in range(60)]
        self.assertEqual(len(file_settings(self.value)['download']['routes']),60)
        with self.assertRaises(InstanceError):host_file_configuration(self.value,account=self.account)

    def test_materialized_file_config_parses_with_actual_download_host_contract(self):
        from src.plugins.ai_chat.adapters.qq_official.host_config import HostConfiguration
        with TemporaryDirectory() as temp:
            root=Path(temp);root.chmod(0o700);(root/'config').mkdir(mode=0o700);target=root/'runtime-config';target.mkdir(mode=0o700)
            self.value['native_routes']=[self.route(native=True)]
            self.value['download']['routes']=[self.route()]
            reference=stage_credentials(root,environment='production',previous=None,supplied={'app_id':self.account.app_id,'app_secret':'TEST_SECRET'})
            name=materialize_runtime(root,reference=reference,revision=1,environment='production',target_root=target,files=self.value)
            config=HostConfiguration.from_env({'AI_QQ_OFFICIAL_ENABLED':'true','AI_QQ_OFFICIAL_HOST_CONFIG_FILE':str(target/name/'host.json')})
            self.assertEqual(config.account,self.account)
            self.assertEqual(config.files['private_directory'],Path(PRIVATE_DIRECTORY))
            self.assertNotIn('native_routes',config.files)
            self.assertNotIn('download',config.files)
