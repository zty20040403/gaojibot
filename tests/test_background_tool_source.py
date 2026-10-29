"""Background identity has no fabricated inbound or permission evidence."""
from dataclasses import replace
from types import SimpleNamespace
import unittest

from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import (
    AccountKey, ActorKey, BackgroundToolSource, EndpointKey, InboundMessage,
)
from src.plugins.ai_chat.tools.state import ToolContext, TurnState, structured_tool_source


class BackgroundToolSourceTests(unittest.TestCase):
    def setUp(self):
        self.account=AccountKey('qq-official','TEST_BACKGROUND_APP','test')
        self.endpoint=EndpointKey(self.account,'group','TEST_GROUP')
        self.actor=ActorKey(self.account,'group_member','TEST_GROUP','TEST_OPAQUE_ACTOR')
        self.source=BackgroundToolSource(self.endpoint,self.actor,'subagent','task:7',2)
        self.platform=SimpleNamespace(account=self.account)

    def test_metadata_keeps_real_endpoint_and_opaque_actor_without_an_inbound(self):
        context=ToolContext(None,background_source=self.source,platform=self.platform)
        self.assertIs(structured_tool_source(context),self.source)
        self.assertEqual(context.scope,self.endpoint.scope)
        self.assertIsNone(context.inbound)
        self.assertIsNone(context.event)
        for name in ('native_message_id','reply_credential','occurred_at','mentions_bot'):
            self.assertFalse(hasattr(self.source,name))
        # A metadata object alone neither opens tools nor yields a grant.
        self.assertEqual(context.capabilities,frozenset())
        self.assertFalse(hasattr(self.source,'grant'))
        self.assertNotIn(self.actor.native_user_id,repr(self.source))

    def test_source_revision_reference_and_kind_are_bounded(self):
        for changes in ({'source_revision':0},{'source_revision':True},{'source_revision':2**63},
                        {'source_ref':''},{'source_ref':'x'*129},{'source_kind':'foreground'}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):replace(self.source,**changes)

    def test_account_and_actor_namespaces_must_match(self):
        other=AccountKey('qq-official','TEST_OTHER_APP','test')
        for actor in (ActorKey(other,'group_member','TEST_GROUP','TEST_OPAQUE_ACTOR'),
                      ActorKey(self.account,'group_member','TEST_OTHER_GROUP','TEST_OPAQUE_ACTOR'),
                      ActorKey(self.account,'bot','','TEST_BOT')):
            with self.subTest(actor=actor.identity_kind):
                with self.assertRaises(ValueError):replace(self.source,actor=actor)

    def test_context_cannot_combine_event_or_inbound_with_background(self):
        inbound=InboundMessage(self.endpoint,self.actor,None,'TEST_NATIVE','test','',
            MessageBody((TextNode(0,'test'),)),True,None,100,None)
        for changes in ({'inbound':inbound},{'event':object()}):
            with self.subTest(changes=tuple(changes)):
                with self.assertRaises(ValueError):ToolContext(None,background_source=self.source,**changes)
        with self.assertRaises(TypeError):ToolContext(None,background_source=SimpleNamespace())
        with self.assertRaises(ValueError):
            ToolContext(None,background_source=self.source,platform=SimpleNamespace(
                account=AccountKey('qq-official','TEST_OTHER_APP','test')))

    def test_checkpoint_codec_uses_actual_background_account(self):
        context=ToolContext(None,background_source=self.source)
        turn=TurnState(context,reply_checkpoint_codec=SimpleNamespace(platform='onebot-v11'))
        with self.assertRaises(ValueError):turn._checkpoint_codec()
        turn.reply_checkpoint_codec=SimpleNamespace(platform='qq-official')
        self.assertIs(turn._checkpoint_codec(),turn.reply_checkpoint_codec)


if __name__=='__main__':
    unittest.main()
