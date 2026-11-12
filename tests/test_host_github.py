"""Only a public committer identity enters a VM; no real GitHub calls."""
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch

from src.plugins.ai_chat.adapters.qq_official.host_config import HostConfiguration
from src.plugins.ai_chat.host_github import GitHubIdentity
from src.plugins.ai_chat.platform_types import AccountKey
from src.plugins.ai_chat.sandbox import SandboxError
from src.plugins.ai_chat.vm_sandbox import VmSandboxManager


class GitHubIdentityTests(unittest.TestCase):
    def test_only_public_identity_and_noreply_email_are_written(self):
        identity = GitHubIdentity.parse({'login':'TEST-BOT','user_id':12345})
        with TemporaryDirectory() as directory:
            path = Path(directory)/'TEST_gitconfig'
            env = dict(os.environ,GIT_CONFIG_SYSTEM=str(path))
            subprocess.run(['/bin/sh','-c',identity.script],env=env,check=True,capture_output=True)
            self.assertEqual(subprocess.check_output(['git','config','--system','user.email'],env=env).decode().strip(),
                '12345+TEST-BOT@users.noreply.github.com')
            text = path.read_text()
            self.assertIn('name = TEST-BOT',text)
            self.assertNotIn('credential',text)
            self.assertNotIn('Authorization',text)

    def test_identity_rejects_secrets_shell_fragments_and_unexpected_fields(self):
        for raw in ({'login':'TEST; echo bad','user_id':1},{'login':'TEST\nINJECT','user_id':1},
                {'login':'TEST','user_id':True},{'login':'TEST','user_id':0},
                {'login':'TEST','user_id':1,'token':'TEST_SECRET'}, {'login':'TEST','email':'TEST_PRIVATE'}):
            with self.assertRaisesRegex(ValueError,'^invalid_github_identity$'):
                GitHubIdentity.parse(raw)

    def test_protected_host_configuration_wires_only_vm_public_identity(self):
        account = AccountKey('qq-official','TEST_GITHUB','test')
        raw = dict(account=account.key,github={'login':'TEST-BOT','user_id':12345})
        config = HostConfiguration.parse(json.dumps(raw))
        context = SimpleNamespace(database=object(),delivery_store=SimpleNamespace(transaction=object()),
            db_call=object(),sandbox_manager=SimpleNamespace(backend='vm'))
        with patch('src.plugins.ai_chat.adapters.qq_official.host_config.HostServices'):
            config.build(context,None)
        self.assertEqual(context.sandbox_manager.github_identity,config.github)
        raw['github']['token'] = 'TEST_SECRET'
        with self.assertLogs('src.plugins.ai_chat.adapters.qq_official.host_config',level='WARNING') as logs:
            self.assertIsNone(HostConfiguration.parse(json.dumps(raw)).github)
        self.assertNotIn('TEST_SECRET',str(logs.output))


class GitHubVMTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.manager = VmSandboxManager(image='/TEST_base.qcow2',root=directory.name)
        self.manager.github_identity = GitHubIdentity.parse({'login':'TEST-BOT','user_id':12345})
        self.manager._metadata = AsyncMock(return_value={'id':'s123456'})
        self.manager._all_metadata = AsyncMock(return_value=[])
        self.manager._defined = AsyncMock(return_value=True)
        self.manager._running = AsyncMock(return_value=False)
        self.manager._virsh,self.manager._wait_guest = AsyncMock(),AsyncMock()
        self.manager._save_metadata = Mock()
        self.manager._guest_exec = AsyncMock(return_value=(0,b'',b''))

    async def test_boot_and_already_running_vm_receive_identity_before_work(self):
        for running in (False,True):
            self.manager._running.return_value = running
            self.manager._guest_exec.reset_mock()
            await self.manager.start_owned('qqo:conversation:TEST_GROUP','s123456')
            call = self.manager._guest_exec.await_args
            self.assertIn('users.noreply.github.com',call.args[2][1])
            self.assertEqual(call.kwargs['timeout'],30)
        self.manager._guest_exec.reset_mock()
        await self.manager.start_owned('TEST_TEMP_OWNER','s123456')
        self.manager._guest_exec.assert_not_awaited()

    async def test_failed_identity_setup_is_not_silently_ignored(self):
        self.manager._guest_exec.return_value = (1,b'',b'TEST_PRIVATE_GUEST_ERROR')
        with self.assertRaisesRegex(SandboxError,'Git 提交身份配置失败') as error:
            await self.manager.start_owned('qqo:conversation:TEST_GROUP','s123456')
        self.assertNotIn('TEST_PRIVATE',str(error.exception))

    async def test_first_creation_also_applies_identity_after_cloud_init(self):
        self.manager.image = Path(self.manager.root)/'TEST_base.qcow2'
        self.manager.image.touch()
        self.manager._checked = AsyncMock()
        self.manager._configure_github = AsyncMock()
        owner = 'qqo:conversation:TEST_GROUP'
        result = await self.manager.create(owner)
        self.manager._configure_github.assert_awaited_once_with(owner,result['sandbox_id'])
