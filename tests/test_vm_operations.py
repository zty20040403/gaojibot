"""Operations conversations' VMs use the operations daemon and get an SSH client config."""
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock

from src.plugins.ai_chat.vm_sandbox import VmSandboxManager

OPS = 'qqo:conversation:' + '1' * 64
OTHER = 'qqo:conversation:' + '2' * 64
SOCKET = '/TEST/vm-ops-runtime/libvirt/virtqemud-sock'


class VMOperationsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.manager = VmSandboxManager(image='/TEST_base.qcow2', root=directory.name,
            operations_owners=frozenset({OPS}), operations_socket=SOCKET, operations_domain='TEST.invalid')
        for sandbox_id, owner in (('s111111', OPS), ('s222222', OTHER)):
            path = self.manager.root / sandbox_id
            path.mkdir()
            (path / 'metadata.json').write_text(json.dumps({'id': sandbox_id, 'owner_hash': self.manager._owner_hash(owner)}))

    def test_virsh_for_an_operations_vm_goes_to_its_daemon(self):
        session = ('virsh', '-c', 'qemu:///session')
        ops_uri = ('virsh', '-c', 'qemu:///session?socket=' + SOCKET)
        self.assertEqual(self.manager._daemon_args(session + ('start', 'gaoji-vm-s111111')), ops_uri + ('start', 'gaoji-vm-s111111'))
        define = str(self.manager.root / 's111111' / 'domain.xml')
        self.assertEqual(self.manager._daemon_args(session + ('define', define)), ops_uri + ('define', define))
        for args in (session + ('start', 'gaoji-vm-s222222'), session + ('list',), ('qemu-img', 'info', 'x')):
            self.assertEqual(self.manager._daemon_args(args), args)

    def test_no_operations_configured_changes_nothing(self):
        plain = VmSandboxManager(image='/TEST_base.qcow2', root=str(self.manager.root))
        args = ('virsh', '-c', 'qemu:///session', 'start', 'gaoji-vm-s111111')
        self.assertEqual(plain._daemon_args(args), args)

    async def test_ready_writes_ssh_config_only_for_operations_vms(self):
        self.manager._guest_exec = AsyncMock(return_value=(0, b'', b''))
        await self.manager._conversation_event('ready', OTHER, 's222222')
        self.manager._guest_exec.assert_not_awaited()
        await self.manager._conversation_event('ready', OPS, 's111111')
        script = self.manager._guest_exec.await_args.args[2][1]
        self.assertIn('HostName %h.TEST.invalid', script)
        self.assertIn('User gaoji', script)
        self.assertIn('/etc/ssh/ssh_config.d/gaoji-operations.conf', script)
