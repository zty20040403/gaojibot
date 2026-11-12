"""Host declarations are strict and paths cannot change origin or evade allow."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from src.plugins.ai_chat.host_apis import ApiDenied,HostAPI,canonical_path,parse_apis
from src.plugins.ai_chat.adapters.qq_official.host_config import HostConfiguration
from src.plugins.ai_chat.platform_types import AccountKey


class HostAPITests(unittest.TestCase):
    def spec(self, **changes):
        return dict(base_url='https://api.example.test/v1',secret_file='/TEST_PRIVATE_KEY',
            headers={'Authorization':'Bearer {secret}','User-Agent':'TEST_BOT'},
            allow=['GET /repos','POST /repos/TEST_BOT'],per_hour=10,about='TEST public API') | changes

    def test_parse_does_not_read_secrets_and_no_protected_fields_in_repr(self):
        with patch('src.plugins.ai_chat.host_apis._read_secret',side_effect=AssertionError('read')):
            api = parse_apis({'test-api':self.spec()})['test-api']
        self.assertEqual(api.headers['authorization'],'Bearer {secret}')
        self.assertEqual(api.per_hour,10)
        self.assertNotIn('TEST_PRIVATE',repr(api))
        self.assertNotIn('api.example',repr(api))

    def test_allow_uses_method_and_segment_boundary_not_string_prefix(self):
        api = HostAPI.parse('test',self.spec())
        for method,path in [('GET','/repos'),('GET','/repos/TEST_BOT/repo.git'),('POST','/repos/TEST_BOT/issue')]:
            self.assertTrue(api.permits(method,path),(method,path))
        for method,path in [('POST','/repos/TEST_OTHER'),('GET','/repos-private'),('CONNECT','/repos'),
                ('GET','/repos/../TEST_ADMIN'),('GET','/repos/%252e%252e/TEST_ADMIN')]:
            self.assertFalse(api.permits(method,path),(method,path))
        self.assertTrue(HostAPI.parse('test',self.spec(allow=None)).permits('DELETE','/TEST'))
        self.assertFalse(HostAPI.parse('test',self.spec(allow=[])).permits('GET','/TEST'))
        self.assertTrue(HostAPI.parse('test',self.spec(allow=['* /'])).permits('PATCH','/TEST'))

    def test_encoded_delimiters_dot_segments_and_controls_are_rejected(self):
        for path in (b'//TEST',b'/../TEST',b'/./TEST',b'/x/../../TEST',b'/x//TEST',
                b'/x/%2e%2e/TEST',b'/x/%2FTEST',b'/x/%5cTEST',b'/x/%252E/TEST',b'/x/%255cTEST',
                b'/x\\TEST',b'/x/%00',b'/x/%0a',b'/x/%7f',b'/x/%GG',b'/x/TEST?',b'/x/..;/',b'/x/TEST#'):
            with self.subTest(path=path),self.assertRaisesRegex(ApiDenied,'invalid_api_path'):
                canonical_path(path)
        self.assertEqual(canonical_path(b'/repos/TEST.git'),'/repos/TEST.git')
        self.assertEqual(canonical_path('/TEST/中文'.encode()),'/TEST/中文')
        self.assertEqual(canonical_path(b'/TEST/%41'),'/TEST/A')

    def test_bad_configuration_fails_closed_without_value_in_diagnostic(self):
        for changes in ({'base_url':'http://api.example.test'}, {'base_url':'https://TEST_SECRET@example.test'},
                {'base_url':'https://api.example.test/?key=TEST_SECRET'}, {'base_url':'https://api.example.test/../TEST'},
                {'base_url':'https://api.example.test/%252e'}, {'secret_file':'relative'}, {'per_hour':True},
                {'per_hour':-1}, {'allow':['TRACE /']}, {'allow':['GET /repo/*']}, {'allow':['GET /../']},
                {'headers':{'Host':'TEST','Authorization':'Bearer {secret}'}},
                {'headers':{'Authorization':'{secret}','authorization':'TEST'}}, {'headers':{'X-Test':'TEST\r\n{secret}'}},
                {'headers':{}}, {'unknown':'TEST_SECRET'}):
            with self.subTest(changes=changes),self.assertRaises(ApiDenied) as error:
                HostAPI.parse('test',self.spec(**changes))
            self.assertNotIn('TEST_SECRET',str(error.exception))
        for name in ('TEST','../test','test_api','test/api',''):
            with self.assertRaises(ApiDenied):
                parse_apis({name:self.spec()})

    def test_secret_is_private_and_errors_do_not_disclose_path_or_value(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/'TEST_SECRET_FILE'
            path.write_text('TEST_HOST_SECRET\n'); path.chmod(0o600)
            api = HostAPI.parse('test',self.spec(secret_file=str(path)))
            self.assertEqual(api.secret(),'TEST_HOST_SECRET')
            for mode,value in ((0o644,'TEST_SECRET'),(0o600,'TEST\nSECRET'),(0o600,''),(0o600,'X'*16385)):
                path.chmod(mode); path.write_text(value)
                with self.assertRaisesRegex(ApiDenied,'api_credential_unavailable') as error:
                    api.secret()
                self.assertNotIn(directory,str(error.exception))

    def test_invalid_apis_do_not_enable_partial_proxy_or_disable_other_features(self):
        raw = dict(account=AccountKey('qq-official','TEST_API_CONFIG','test').key,media={'asr':True},
            apis={'good':self.spec(),'bad':self.spec(headers={})})
        with self.assertLogs('src.plugins.ai_chat.adapters.qq_official.host_config',level='WARNING'):
            config = HostConfiguration.parse(json.dumps(raw))
        self.assertEqual(config.apis,{})
        self.assertTrue(config.media.asr)
        raw['apis'].pop('bad')
        self.assertEqual(set(HostConfiguration.parse(json.dumps(raw)).apis),{'good'})
