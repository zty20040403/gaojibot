"""Operations conversations' containers run on the operations network and get an SSH client config."""
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock

from src.plugins.ai_chat.container_sandbox import ContainerSandboxManager

OPS = 'qqo:conversation:' + '1' * 64
OTHER = 'qqo:conversation:' + '2' * 64


class ContainerOperationsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.manager = ContainerSandboxManager(root=directory.name, broker='/nonexistent',
            operations_owners=frozenset({OPS}), operations_domain='TEST.invalid')
        for sandbox_id, owner in (('s111111', OPS), ('s222222', OTHER)):
            path = self.manager.root / sandbox_id
            path.mkdir()
            (path / 'metadata.json').write_text(json.dumps({'id': sandbox_id, 'owner_hash': self.manager._owner_hash(owner)}))

    def test_operations_owner_is_classified_from_metadata(self):
        self.assertTrue(self.manager._operations_sandbox('s111111'))
        self.assertFalse(self.manager._operations_sandbox('s222222'))
        plain = ContainerSandboxManager(root=str(self.manager.root), broker='/nonexistent')
        self.assertFalse(plain._operations_sandbox('s111111'))

    async def test_ready_writes_ssh_config_only_for_operations_sandboxes(self):
        self.manager._guest_exec = AsyncMock(return_value=(0, b'', b''))
        await self.manager._conversation_event('ready', OTHER, 's222222')
        self.manager._guest_exec.assert_not_awaited()
        await self.manager._conversation_event('ready', OPS, 's111111')
        script = self.manager._guest_exec.await_args.args[2][1]
        self.assertIn('HostName %h.TEST.invalid', script)
        self.assertIn('User gaoji', script)
        self.assertIn('/etc/ssh/ssh_config.d/gaoji-operations.conf', script)
