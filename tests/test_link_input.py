import asyncio
import json
import unittest
from unittest.mock import AsyncMock,patch

from src.qq_official.events import parse_dispatch
from src.plugins.ai_chat.adapters.qq_official.commands import route_command
from src.plugins.ai_chat.adapters.qq_official.input_normalization import normalize_input
from src.plugins.ai_chat.adapters.qq_official.link_commands import parse_link_command
from src.plugins.ai_chat.adapters.qq_official.ingest import process_inbox
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.application.commands import CommandServices
from src.plugins.ai_chat.ledger import MessageLedger
from tests.test_qq_official_commands import inbound
from tests.test_qq_official_input_normalization import frame
from tests import test_qq_official_inbox as fixture


class LinkCommandTests(unittest.TestCase):
    def test_fixed_independent_namespace_ascii_codes_and_safe_repr(self):
        reference = 'LC-'+'a'*32
        for text,action in (('/link','create'),(' !LINK 723891','confirm'),('/link list','list'),
                ('/link cancel '+reference,'cancel'),('/link status '+reference,'status'),('/unlink '+'b'*32,'revoke')):
            result = parse_link_command(text); self.assertEqual(result.action,action); self.assertTrue(result.valid)
            self.assertNotIn('723891',repr(result))
        for text in ('/link １２３４５６','/link 12345','/link submit TEST 723891','/unlink LC-'+'a'*32,
                '/link status BC-'+'a'*32,'/link '+('723891 '*100),'/link cancel '+reference+' extra'):
            result = parse_link_command(text); self.assertFalse(result.valid)
            self.assertEqual((result.reference,result.code),('',''))
        for text in ('/bind 723891','/linking 723891','text /link 723891',None):self.assertIsNone(parse_link_command(text))

    def test_structural_self_mention_only_and_embedded_link_withheld(self):
        valid = normalize_input(parse_dispatch(frame('<@!TEST_BOT> /link 723891',[{'id':'TEST_BOT','is_you':True}])))
        self.assertEqual((valid.kind,valid.link_command.action,valid.link_command.code),('link_control','confirm','723891'))
        self.assertIsNone(valid.command); self.assertIsNone(valid.input_text); self.assertNotIn('723891',repr(valid))
        for text,mentions in (('<@!TEST_BOT> /link 723891',[]),('quoted /link 723891',[]),
                ('<@TEST_BOT> /link 723891',[{'id':'TEST_BOT','is_you':1}]),('text /unlink '+'a'*32,[])):
            value = normalize_input(parse_dispatch(frame(text,mentions)))
            self.assertEqual((value.kind,value.reason),('quarantine','input_sensitive_link'))
            self.assertIsNone(value.input_text); self.assertIsNone(value.link_command)

    def test_generic_command_router_cannot_execute_link_or_retain_code(self):
        async def check():
            for text in ('/link 723891','!unlink '+'a'*32):
                before = AsyncMock(side_effect=AssertionError('must not checkpoint a link code'))
                admit = AsyncMock(side_effect=AssertionError('must not call shared router'))
                result = await route_command(text,inbound(),services=CommandServices(),admission=admit,before_effect=before)
                self.assertEqual(result.kind,'control'); before.assert_not_called(); admit.assert_not_called()
        asyncio.run(check())


class LinkIngressPrivacyTests(fixture.OfficialInboxTests):
    def test_unavailable_link_service_never_falls_through_to_binding_or_history(self):
        self.activate_group()
        for private,content,mentions in ((True,'/link 723891',[]),(False,'<@TEST_BOT> /link 723891',[{'id':'TEST_BOT','is_you':True}])):
            with patch.object(self.binding,'apply',side_effect=AssertionError('link is not binding')):
                result = self.accept(private=private,content=content,mentions=mentions)
            self.assertEqual((result.disposition,result.reason),('quarantined','link_control_unavailable'))
            row = self.query('SELECT * FROM qq_official_inbound_events WHERE inbox_id=?',(result.inbox_id,))[0]
            self.assertIsNone(row['payload_json']); self.assertIsNone(row['turn_text']); self.assertIsNone(row['control_action'])
            self.assertEqual(self.accept(private=private,native=row['native_message_id'],content=content,mentions=mentions).disposition,'duplicate')
        self.assertEqual((self.count('messages'),self.count('durable_jobs'),self.count('qq_official_reply_windows')),(0,0,0))
        self.assertNotIn('723891',self.persisted_text())

    def test_fake_prefix_link_is_body_free_and_allocates_no_work(self):
        self.activate_group()
        result = self.accept(content='<@TEST_BOT> /link 723891')
        self.assertEqual((result.disposition,result.reason),('quarantined','input_sensitive_link'))
        self.assertNotIn('723891',self.persisted_text())
        self.assertEqual((self.count('messages'),self.count('durable_jobs')),(0,0))

    def test_old_misclassification_erases_link_before_canonical_ingest(self):
        self.activate_group(); result = self.accept(content='TEST_ORDINARY_INPUT')
        row = self.query('SELECT * FROM qq_official_inbound_events WHERE inbox_id=?',(result.inbox_id,))[0]
        source = json.loads(row['payload_json']); source['d']['content'] = '/link 723891'
        ledger = MessageLedger(self.database); self.addCleanup(ledger.close)
        with self.tx() as cursor:
            cursor.execute('UPDATE qq_official_inbound_events SET payload_json=? WHERE inbox_id=?',(json.dumps(source),result.inbox_id))
            outcome = process_inbox(cursor,inbox_id=result.inbox_id,lease_token=self.token,ledger=ledger,jobs=self.jobs,
                epoch_guard=assert_epoch,admission=self.store.admitted,now=self.now)
        self.assertEqual(outcome.status,'quarantined'); self.assertNotIn('723891',self.persisted_text())
        self.assertEqual(self.count('messages'),0)


for name in dir(fixture.OfficialInboxTests):
    if name.startswith('test_') and name not in LinkIngressPrivacyTests.__dict__:setattr(LinkIngressPrivacyTests,name,None)
