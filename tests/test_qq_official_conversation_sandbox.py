import asyncio
import json
import tempfile
import unittest
from unittest.mock import AsyncMock

from tests.test_owned_sandbox import Binding, Delivery, FakeManager
from src.plugins.ai_chat.application.owned_sandbox import OwnedSandboxExecutor
from src.plugins.ai_chat.vm_sandbox import VmSandboxManager


class ConversationSandboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_next_turn_and_concurrent_create_reuse_files(self):
        manager = FakeManager()
        def executor(source):
            return OwnedSandboxExecutor(manager=manager, binding=Binding(source), delivery=Delivery(), clock=lambda: 100)
        first, second = executor('first'), executor('next')
        created = await asyncio.gather(first.execute('sandbox_create', {}), second.execute('sandbox_create', {}))
        sid = json.loads(created[0])['sandbox_id']
        self.assertEqual(json.loads(created[1])['sandbox_id'], sid)
        await first.execute('sandbox_write_file', {'sandbox_id': sid, 'path': '/work/note.txt', 'content': 'retained'})
        await first.retain_task_sandboxes()
        result = json.loads(await second.execute('sandbox_read_file', {'sandbox_id': sid, 'path': '/work/note.txt'}))
        self.assertEqual(result['content'], 'retained')
        self.assertEqual(len(manager.items), 1)
        self.assertFalse(any(call[0] == 'stop' for call in manager.calls))
        manager.fail = 'write'
        self.assertFalse(json.loads(await second.execute('sandbox_write_file', {'sandbox_id': sid, 'path': 'retry.txt', 'content': 'x'}))['ok'])
        manager.fail = None
        self.assertTrue(json.loads(await second.execute('sandbox_write_file', {'sandbox_id': sid, 'path': 'retry.txt', 'content': 'x'}))['ok'])

    async def test_idle_stops_then_aged_workspace_is_removed(self):
        with tempfile.TemporaryDirectory() as root:
            manager = VmSandboxManager(image='/base.qcow2', root=root)
            item = {'id': 's123456', 'owner_ref': 'qqo:conversation:test', 'last_used': 100, 'created_at': 0}
            manager._all_metadata = AsyncMock(return_value=[item])
            manager._metadata = AsyncMock(return_value=item)
            manager.stop_owned, manager.destroy = AsyncMock(), AsyncMock()
            await manager.maintain_conversations(now=200)
            manager.stop_owned.assert_not_awaited()
            await manager.maintain_conversations(now=7400)
            manager.stop_owned.assert_awaited_once()
            manager.destroy.assert_not_awaited()
            await manager.maintain_conversations(now=31*86400)
            manager.destroy.assert_awaited_once()

    def test_prepared_conversation_image_does_not_reinstall_packages(self):
        raw = json.loads(VmSandboxManager._cloud_config(conversation=True).split('\n', 1)[1])
        prepared = json.loads(VmSandboxManager._cloud_config(conversation=True, prepared=True).split('\n', 1)[1])
        self.assertIn('python3-pip', raw['packages'])
        self.assertNotIn('packages', prepared)
        self.assertFalse(prepared['package_update'])
        self.assertIn(['ln', '-sfn', '/workspace', '/work'], prepared['runcmd'])
