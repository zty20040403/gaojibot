from copy import deepcopy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from test_desktop_backups import Fixture, FakeController, CHECKSUM, IMAGE, DEPENDENCY
from src.local_runtime.desktop import DesktopController, initialize_seed
from src.local_runtime.files import read_json, write_json
from src.local_runtime.official_configuration import official_settings
from src.local_runtime.instances import InstanceStore


class OfficialActivationTests(Fixture):
    def setUp(self):
        super().setUp()
        settings=deepcopy(self.config['settings'])
        settings.update(platforms={'onebot':False,'qq_official':True},official=official_settings(),console_admin_username='localadmin')
        settings['official']['exclusive_deployment_confirmed']=True
        settings['features']={key:False for key in settings['features']}
        self.config=self.store.update(self.identifier,settings,expected_revision=2,
            official_credentials={'app_id':'TEST_ACTIVATE_APP','app_secret':'TEST_ACTIVATE_SECRET'})
        (self.root/'jobs').mkdir();self.job='9'*32
        write_json(self.root/'jobs'/(self.job+'.json'),{'instance_id':self.identifier,'checksum':CHECKSUM,
            'runtime_image':IMAGE,'docker':'/test/docker','docker_host':'unix:///test/engine'})
        release=self.root/'releases'/CHECKSUM;release.mkdir(parents=True)
        write_json(release/'images.lock.json',{'postgres':DEPENDENCY,'napcat':DEPENDENCY})
        write_json(self.directory/'config/control.json',{'port':12345,'token':'t'*43})
        self.c=FakeController(self.job,self.root);self.c.running={'database'}
        self.c.napcat=Mock();self.c.napcat.status.side_effect=AssertionError('official path queried legacy login')
        self.c.runtime_probe=Mock();self.c.sandbox.pause=Mock()
        self.c.operation={'id':'a'*32,'kind':'activate','revision':self.config['revision'],'status':'queued',
            'administrator':{'username':'localadmin','password':'TEST_PASSWORD_12345'}}
        self.c.persist()

    def test_actual_activation_uses_local_admin_only_and_real_health_shape(self):
        original=self.c.compose;observed=[]
        def compose(arguments,**kwargs):
            observed.append((arguments,kwargs))
            if arguments[0]=='up' and 'bot' in arguments:
                write_json(self.directory/'state/desktop-health.json',{'updated_at':int(time.time()),
                    'official':{'healthy':True,'lease_current':True,'gateway_ready':True}})
            return original(arguments,**kwargs)
        with patch.object(self.c,'compose',compose):self.c.execute()
        self.assertEqual(self.c.operation['status'],'completed',self.c.operation)
        self.c.napcat.status.assert_not_called();self.c.napcat.configure.assert_not_called()
        self.assertFalse(any('napcat' in args for args,_ in observed))
        admin=[kw['stdin'] for args,kw in observed if 'ensure-admin' in args]
        self.assertEqual(admin,[{'username':'localadmin','password':'TEST_PASSWORD_12345','identity_kind':'local_admin'}])
        self.assertNotIn('TEST_PASSWORD',str(self.c.public_operation()))
        self.assertEqual(self.c.status()['official_connection'],'connected')

    def test_real_desktop_initialize_skips_qr_pull_and_start(self):
        self.c.install_path=self.root/'desktop-install.json'
        write_json(self.c.install_path,{'status':'queued','stage':'database'})
        self.c.install_compose=Mock();self.c.resume=Mock();self.c.job=self.job
        DesktopController.initialize(self.c)
        self.assertEqual(read_json(self.c.install_path)['status'],'awaiting_setup')
        arguments=[item.args[0] for item in self.c.install_compose.call_args_list]
        self.assertEqual(arguments[0],['pull','database'])
        self.assertFalse(any('napcat' in row for row in arguments))
        self.c.napcat.status.assert_not_called()


class OfficialSeedTests(unittest.TestCase):
    def test_actual_seed_pins_official_platform_and_replay_cannot_rechoose(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);payload={'job':'a'*32,'checksum':'b'*64,'token':'T'*43,'bot_port':28080,
                'platforms':{'onebot':False,'qq_official':True}}
            with patch('src.local_runtime.desktop.ROOT',root),patch('src.local_runtime.desktop.SEED',root/'desktop-seed.json'),patch('src.local_runtime.desktop.INSTALL',root/'desktop-install.json'),patch('src.local_runtime.desktop.os.chown'):
                initialize_seed(payload);initialize_seed(payload)
                store=InstanceStore(root/'data');rows=store.list()
                self.assertEqual(len(rows),1);self.assertEqual(rows[0]['settings']['platforms'],payload['platforms'])
                with self.assertRaises(ValueError):initialize_seed(payload|{'platforms':{'onebot':True,'qq_official':False}})
                self.assertEqual(len(store.list()),1)
