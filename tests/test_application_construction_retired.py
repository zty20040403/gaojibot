"""Optional construction failures close allocations without disabling startup."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.plugins.ai_chat.application_construction import build_owned_context
from src.plugins.ai_chat.bridges import MatrixClient
from src.plugins.ai_chat.config import settings
from src.plugins.ai_chat.runtime import build_app_context
from tests.postgres_fixture import isolated_postgres_for_test


class RetiredConstructionTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_invalid_bluebubbles_method_closes_preceding_matrix_client(self):
        database=isolated_postgres_for_test(self)
        clients=[]
        def matrix(*args,**kwargs):
            client=MatrixClient(*args,**kwargs);clients.append(client);return client
        config=replace(settings,postgres_dsn=database.dsn,postgres_schema=database.schema,
            admin_secret_file='',ledger_enabled=True,outbox_enabled=True,
            matrix_enabled=True,matrix_homeserver='https://matrix.test.invalid',
            matrix_access_token='TEST_MATRIX_TOKEN',matrix_user_id='TEST_MATRIX_USER',
            imessage_enabled=True,imessage_base_url='https://imessage.test.invalid',
            imessage_password='TEST_IMESSAGE_PASSWORD',imessage_chat_guid='TEST_CHAT',
            imessage_send_method='TEST_INVALID_METHOD',
            mirror_routes_json=json.dumps([{'name':'TEST_MIRROR','endpoints':[
                {'platform':'matrix','id':'TEST_ROOM'}, {'platform':'imessage','id':'TEST_CHAT'}]}]),
            projects_enabled=False,sandbox_enabled=False,routines_enabled=False,group_wiki_enabled=False,
            semantic_enabled=False,historian_enabled=False,dream_enabled=False,
            media_enabled=False,archive_enabled=False,browser_enabled=False,rich_render_enabled=False,
            mcp_enabled=False,subagents_enabled=False,observability_enabled=False)
        with TemporaryDirectory() as directory,patch('src.plugins.ai_chat.runtime.MatrixClient',side_effect=matrix):
            root=Path(directory)
            context=await build_owned_context(build_app_context,config,state_dir=root/'state',
                project_root=root,logger=Mock(),historian_generator=AsyncMock(),dream_generator=AsyncMock(),
                evidence_provider=Mock(),onebot_enabled=False)
            try:
                self.assertEqual(len(clients),1)
                self.assertTrue(clients[0].client.is_closed)
                self.assertIsNone(context.bridge_manager)
                self.assertIsNone(context.mirror_state)
                self.assertFalse(context.database._pool.closed)
                context.message_ledger.list_scopes()
            finally:await context.shutdown()
            self.assertTrue(context.database._pool.closed)

    async def test_cancelled_retired_cleanup_rolls_back_all_still_owned_resources(self):
        entered=asyncio.Event();seen=[]
        class Retired:
            calls=0
            async def close(self):
                self.calls+=1
                if self.calls==1:
                    entered.set();await asyncio.Event().wait()
                seen.append('retired')
        class Owned:
            def close(self):seen.append('owned')
        def builder(*args,_construction,**kwargs):
            _construction.own(Owned())
            retired=_construction.own(Retired());_construction.retire(retired)
            return object()
        task=asyncio.create_task(build_owned_context(builder,logger=Mock()))
        await entered.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(seen,['retired','owned'])
