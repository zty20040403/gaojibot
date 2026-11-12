"""Program metadata never provides a human actor, owner role or host grant."""
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from tests import test_background_tool_source as fixture
from src.plugins.ai_chat.background_tools import BackgroundPlatform
from src.plugins.ai_chat.platform_types import InboundMessage, ProgramToolSource
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.tools import neutral_registry_for
from src.plugins.ai_chat.tools.state import ToolContext, TurnState, structured_tool_source


class ProgramToolSourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture.BackgroundToolSourceTests.setUp(self)
        self.background = self.source
        self.source = ProgramToolSource(self.endpoint,'TEST_PROGRAM','TEST_ACTION')
        async def call(fn,*args,**kwargs):
            return fn(*args,**kwargs)
        self.ledger = Mock()
        self.ledger.history.return_value = []
        app = SimpleNamespace(message_ledger=self.ledger,db_call=call)
        self.context = ToolContext(SimpleNamespace(context=app),program_source=self.source,
            platform=BackgroundPlatform(self.endpoint),capabilities=frozenset({'self','context','web','memory','host'}))

    async def test_read_catalog_cannot_inherit_a_foreground_actor_or_privileges(self):
        self.assertIs(structured_tool_source(self.context),self.source)
        self.assertEqual(self.context.scope,self.endpoint.scope)
        self.assertIsNone(self.context.inbound)
        for key in ('actor','platform_role','reply_credential','native_message_id','canonical_scope','grant'):
            self.assertFalse(hasattr(self.source,key))
        registry = neutral_registry_for(self.context)
        self.assertEqual({item['function']['name'] for item in registry.definitions(self.context)},
            {'history','web_search','web_fetch','use_skill','inspect_source'})
        turn = TurnState(self.context)
        for name in ('ops_read','agent','schedule_set','memory_write','send_file'):
            self.assertIsNone(await registry.execute(turn,name,{}))
        value = await registry.execute(turn,'history',{'query':'TEST','scope_key':'TEST_OTHER_SCOPE'})
        self.assertTrue(json.loads(value)['ok'])
        self.assertEqual(self.ledger.history.call_args.args[0],self.endpoint.scope)

    async def test_metadata_cannot_be_combined_with_human_or_legacy_context(self):
        inbound = InboundMessage(self.endpoint,self.actor,None,'TEST_NATIVE','TEST_DISPLAY','',
            MessageBody((TextNode(0,'TEST'),)),True,None,100,None)
        for fields in ({'event':object()},{'background_source':self.background},{'inbound':inbound}):
            with self.assertRaises(ValueError):
                replace(self.context,**fields)
        with self.assertRaises(TypeError):
            replace(self.context,program_source=SimpleNamespace(actor=self.actor))
        with self.assertRaises(ValueError):
            replace(self.context,platform=SimpleNamespace(account=replace(self.account,app_id='TEST_OTHER')))
        for fields in ({'source_ref':''},{'source_ref':'x'*129},{'program':'x'*81},{'endpoint':None}):
            with self.assertRaises(ValueError):
                replace(self.source,**fields)
