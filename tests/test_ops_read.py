from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import unittest

import httpx
from src.plugins.ai_chat.ops_read import owner_allowed, read
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey


class OpsReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_denied_and_host_request_only_uses_get(self):
        account = AccountKey('qq-official', 'TEST_APP', 'test')
        group = SimpleNamespace(endpoint=EndpointKey(account, 'group', 'TEST_GROUP'))
        self.assertFalse(owner_allowed(SimpleNamespace(), group))
        with TemporaryDirectory() as directory:
            token = Path(directory) / 'token'
            token.write_text('TEST_PRIVATE_TOKEN')
            seen = []
            def server(request):
                seen.append(request)
                return httpx.Response(200, json={'cpu': 10})
            async def calls(fn, *args):
                return fn(*args)
            app = SimpleNamespace(db_call=calls, settings=SimpleNamespace(
                ops_read_base_url='https://cluster.example.test', ops_read_token_file=str(token)))
            inbound = SimpleNamespace(endpoint=EndpointKey(account, 'private', 'TEST_OWNER'),
                actor=ActorKey(account, 'c2c_user', '', 'TEST_OWNER'))
            client = httpx.AsyncClient
            with patch('src.plugins.ai_chat.ops_read.owner_allowed', return_value=True), \
                 patch('src.plugins.ai_chat.ops_read.httpx.AsyncClient', side_effect=lambda **kw: client(transport=httpx.MockTransport(server), **kw)):
                result = await read(app, inbound, {'action': 'resources', 'host': 'TEST_HOST'})
                self.assertNotIn('TEST_PRIVATE_TOKEN', result)
                with self.assertRaises(ValueError):
                    await read(app, inbound, {'action': 'restart', 'host': 'TEST_HOST'})
            self.assertEqual([request.method for request in seen], ['GET'])
            self.assertEqual(seen[0].headers['Authorization'], 'Bearer TEST_PRIVATE_TOKEN')
            with patch('src.plugins.ai_chat.ops_read.owner_allowed', return_value=False):
                with self.assertRaises(PermissionError):
                    await read(app, inbound, {'action': 'overview'})
