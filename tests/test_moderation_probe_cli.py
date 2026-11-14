from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch

from src.qq_official.credentials import Credentials,DEFAULT_API_BASE
from src.qq_official.probe_moderation_cli import _options,_private_text,argument_parser,async_main,main


@unittest.skipUnless(os.name=='posix','CLI explicitly requires Unix report storage')
class ModerationProbeCLITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.scope = {'app_id':'TEST_APP_ID','group':'TEST_GROUP','member':'TEST_MEMBER',
            'independent_test_account':True,'exclusive_moderation':True}
        self.write('id','TEST_APP_ID'); self.write('secret','TEST_APP_SECRET'); self.write('scope',json.dumps(self.scope))
        self.argv = ['--app-id-file',str(self.root/'id'),'--app-secret-file',str(self.root/'secret'),
            '--scope-file',str(self.root/'scope'),'--report',str(self.root/'report.json')]

    def write(self,name,content):
        path = self.root/name; path.write_text(content); path.chmod(0o600)

    async def test_default_is_read_and_only_fixed_account_environment_is_constructed(self):
        tokens = SimpleNamespace(credentials=Credentials('TEST_APP_ID','TEST_APP_SECRET'),base_url=DEFAULT_API_BASE,aclose=AsyncMock())
        with patch('src.qq_official.probe_moderation_cli.TokenManager',return_value=tokens) as constructor,\
            patch('src.qq_official.probe_moderation_cli.run_moderation_probe',new_callable=AsyncMock,return_value={'status':'completed'}) as runner:
            self.assertEqual(await async_main(self.argv),0)
        options = runner.call_args.args[0]
        self.assertEqual(options.checks,('read',)); self.assertEqual(options.group,'TEST_GROUP'); self.assertEqual(options.environment,'production')
        self.assertEqual(constructor.call_args.kwargs,{'base_url':DEFAULT_API_BASE}); tokens.aclose.assert_awaited_once()

    async def test_invalid_scope_or_batch_parameters_never_construct_transport(self):
        for scope in ({**self.scope,'app_id':'TEST_OTHER_APP'},{**self.scope,'exclusive_moderation':False},
            {**self.scope,'independent_test_account':False},{**self.scope,'extra':'TEST_VALUE'}):
            self.write('scope',json.dumps(scope))
            with patch('src.qq_official.probe_moderation_cli.TokenManager') as constructor,self.assertRaises(ValueError):
                await async_main(self.argv)
            constructor.assert_not_called()
        self.write('scope',json.dumps(self.scope))
        for extra in (['--checks','mute,mute'],['--checks','kick'],['--duration','301']):
            with patch('src.qq_official.probe_moderation_cli.TokenManager') as constructor,self.assertRaises(ValueError):
                await async_main(self.argv+extra)
            constructor.assert_not_called()

    def test_private_reader_refuses_symlink_broad_file_and_oversized_input(self):
        link = self.root/'link'; link.symlink_to(self.root/'id')
        with self.assertRaises(OSError):_private_text(link)
        (self.root/'id').chmod(0o640)
        with self.assertRaises(ValueError):_private_text(self.root/'id')
        self.write('large','X'*16385)
        with self.assertRaises(ValueError):_private_text(self.root/'large')
        self.root.chmod(0o755)
        with self.assertRaises(ValueError):_options(argument_parser().parse_args(self.argv))

    def test_native_acl_metadata_is_bound_to_fd_and_unknown_or_extended_acl_is_denied(self):
        for result in (SimpleNamespace(returncode=0,stdout='-rw-------+ TEST_USER TEST_VALUE\n 0: TEST_GRANT allow read\n'),
            SimpleNamespace(returncode=1,stdout='')):
            with patch('src.qq_official.probe_moderation_cli.sys.platform','darwin'),\
                patch('src.qq_official.probe_moderation_cli.subprocess.run',return_value=result) as inspect,self.assertRaises(ValueError):
                _private_text(self.root/'id')
            arguments = inspect.call_args
            self.assertEqual(arguments.args[0][:2],['/bin/ls','-ldeL'])
            self.assertEqual(arguments.args[0][-1],'/dev/fd/'+str(arguments.kwargs['pass_fds'][0]))

    def test_help_and_errors_do_not_print_scope_values_or_unsafe_exception_text(self):
        output = io.StringIO()
        with redirect_stdout(output),self.assertRaises(SystemExit) as raised:argument_parser().parse_args(['--help'])
        self.assertEqual(raised.exception.code,0)
        with redirect_stdout(output),patch('src.qq_official.probe_moderation_cli.async_main',new_callable=AsyncMock,
            side_effect=RuntimeError('TEST_SECRET_PATH_OR_TOKEN')):
            self.assertEqual(main(),1)
        self.assertNotIn('TEST_SECRET_PATH_OR_TOKEN',output.getvalue()); self.assertNotIn('TEST_MEMBER',output.getvalue())
