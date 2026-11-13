"""Recipe parsing never executes commands or opens a deployment connection."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from dataclasses import replace
import json
import unittest

from src.plugins.ai_chat.ops_plans import OpsConfiguration,OpsPlanError
from src.plugins.ai_chat.adapters.qq_official.host_config import HostConfiguration
from src.plugins.ai_chat.platform_types import AccountKey


def configuration():
    return dict(base_url='https://control.example.test',secret_file='/TEST_PRIVATE_TOKEN',actor='TEST_OPERATOR',plans={
        'restart-test':dict(about='TEST 重启允许的服务',host='TEST_HOST',argv=['/TEST_BIN/systemctl','restart','{unit}'],
            parameters={'unit':{'enum':['TEST_A.service','TEST B.service']}},timeout_seconds=60),
        'deploy-test':dict(about='TEST 部署指定提交',host='TEST_HOST',argv=['/TEST_BIN/deploy','{revision}','{count}'],
            parameters={'revision':{'format':'git_revision'},'count':{'minimum':1,'maximum':3}},timeout_seconds=900)})


class OpsPlanTests(unittest.TestCase):
    def test_typed_arguments_are_individual_argv_not_shell_interpolation(self):
        config = OpsConfiguration.parse(configuration())
        self.assertEqual(config.plan('restart-test').render({'unit':'TEST B.service'}),{
            'host':'TEST_HOST','profile':'operator','command':{'argv':['/TEST_BIN/systemctl','restart','TEST B.service']},'timeout_seconds':60})
        self.assertEqual(config.plan('deploy-test').render({'revision':'a'*40,'count':2})['command']['argv'],
            ['/TEST_BIN/deploy','a'*40,'2'])
        self.assertEqual(config.plan('deploy-test').schema()['additionalProperties'],False)
        for arguments in ({},{'unit':'TEST_A.service; rm -rf /'},{'unit':'TEST_A.service','host':'TEST_OTHER'},
                {'unit':['TEST_A.service']},{'unit':'TEST_A.service','argv':['/TEST_OTHER']}):
            with self.assertRaises(OpsPlanError):
                config.plan('restart-test').render(arguments)
        for arguments in ({'revision':'HEAD','count':1},{'revision':'a'*40,'count':True},{'revision':'a'*40,'count':4}):
            with self.assertRaises(OpsPlanError):
                config.plan('deploy-test').render(arguments)
        with self.assertRaises(OpsPlanError):
            config.plan('not-configured')

    def test_catalog_and_representations_do_not_expose_control_address_or_command(self):
        config = OpsConfiguration.parse(configuration())
        public = json.dumps(config.catalog(),ensure_ascii=False)+repr(config)+repr(config.plans)
        for private in ('control.example.test','TEST_PRIVATE_TOKEN','/TEST_BIN','TEST_OPERATOR','TEST_HOST'):
            self.assertNotIn(private,public)
        self.assertIn('restart-test',public); self.assertIn('TEST_A.service',public)

    def test_configuration_rejects_free_form_arguments_dynamic_executables_and_partial_placeholders(self):
        for change in ({'argv':['{unit}']},{'argv':['systemctl','{unit}']},{'argv':['/TEST_BIN/control','--unit={unit}']},
                {'argv':['/TEST_BIN/control','{other}']},{'parameters':{'unit':{'type':'string'}}},
                {'parameters':{'unit':{'pattern':'.*'}}},{'parameters':{'unit':{'enum':[]}}},
                {'parameters':{'unit':{'enum':['TEST_\u202e']}}},{'about':'TEST_\ud800'},
                {'parameters':{'unit':{'minimum':True,'maximum':3}}},{'timeout_seconds':0},
                {'timeout_seconds':True},{'argv':['/TEST_BIN/control','fixed']},{'host':'{unit}'},
                {'env':{'TOKEN':'TEST_SECRET'}}):
            raw = configuration(); raw['plans']['restart-test'].update(change)
            with self.subTest(change=change),self.assertRaises(OpsPlanError):
                OpsConfiguration.parse(raw)

    def test_transport_address_is_static_and_credentials_are_private(self):
        for change in ({'base_url':'http://remote.example'},{'base_url':'https://user:TEST_TOKEN@remote.example'},
                {'base_url':'https://remote.example/?secret=TEST_TOKEN'},{'base_url':'https://remote.example/a/../b'},
                {'secret_file':'relative'},{'actor':'TEST\nINJECTION'},{'plans':{}},{'token':'TEST_SECRET'}):
            with self.subTest(change=change),self.assertRaises(OpsPlanError) as error:
                OpsConfiguration.parse(configuration()|change)
            self.assertNotIn('TEST_TOKEN',str(error.exception))
        with TemporaryDirectory() as directory:
            path = Path(directory)/'token'; path.write_text('TEST_SECRET_'*4); path.chmod(0o600)
            config = OpsConfiguration.parse(configuration()|dict(secret_file=str(path)))
            secret = config.secret(); original = config.binding(config.plan('restart-test'),secret)
            changed = deepcopy(configuration()); changed['plans']['restart-test']['argv'][1] = 'stop'
            self.assertNotEqual(original,OpsConfiguration.parse(changed).binding(OpsConfiguration.parse(changed).plan('restart-test'),secret))
            self.assertNotEqual(original,config.binding(config.plan('restart-test'),'TEST_NEW_SECRET_'*3))
            self.assertNotEqual(original,replace(config,base_url='https://other.example').binding(config.plan('restart-test'),secret))
            self.assertNotEqual(original,replace(config,actor='TEST_OTHER').binding(config.plan('restart-test'),secret))
            path.chmod(0o644)
            with self.assertRaises(OpsPlanError):
                config.secret()

    def test_host_configuration_defaults_off_and_bad_plans_do_not_disable_other_features(self):
        base = dict(account=AccountKey('qq-official','TEST_APP','test').key,version=1,media={'asr':True})
        self.assertIsNone(HostConfiguration.parse(json.dumps(base)).operations)
        self.assertIsNotNone(HostConfiguration.parse(json.dumps(base|dict(operations=configuration()))).operations)
        with self.assertLogs('src.plugins.ai_chat.adapters.qq_official.host_config',level='WARNING') as logs:
            result = HostConfiguration.parse(json.dumps(base|dict(operations={'token':'TEST_MUST_NOT_LOG'})))
        self.assertIsNone(result.operations); self.assertTrue(result.media.asr)
        self.assertNotIn('TEST_MUST_NOT_LOG',str(logs.output))
