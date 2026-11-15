import asyncio
from datetime import datetime,timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch

import httpx

from src.qq_official.credentials import Credentials
from src.qq_official.group_moderation import GroupModerationProtocol
from src.qq_official.http import RestClient
from src.qq_official.probe_moderation import ModerationProbeOptions,run_moderation_probe


class Gateway:
    health = {'message_gap':False}
    def __init__(self,events):self.events,self.calls,self.stopped = events,0,False
    async def run(self,observe):
        self.calls += 1
        for event in self.events:await observe(event)
    async def stop(self):self.stopped = True


class ModerationProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.report,self.now,self.seen = Path(directory.name)/'report.json',1700000000,[]
        self.role,self.rules,self.reads,self.lost,self.race = 'admin',{},0,False,False
        self.bot,self.cancel = 'TEST_BOT',False
        async def handle(request):
            body = json.loads(request.content) if request.content else None
            self.seen.append((request.method,request.url.path,body))
            if request.url.path.endswith('/bot_state'):
                return httpx.Response(200,json={'member_role':self.role,'member_openid':self.bot})
            if request.method=='GET':
                self.reads += 1
                if self.race and self.reads==3:self.rules['TEST_MEMBER'] = self.now+999
                members = [{'member_openid':key,'mute_expire_at':datetime.fromtimestamp(value,timezone.utc).isoformat()} for key,value in self.rules.items()]
                return httpx.Response(200,json={'members':members,'global_rule':{'mode':'none'}})
            if request.method=='POST':
                item = body['members'][0]
                if item['op']=='del':self.rules.pop(item['member_openid'],None)
                else:self.rules[item['member_openid']] = datetime.fromisoformat(item['mute_expire_at']).timestamp()
                if self.cancel:raise asyncio.CancelledError
                if self.lost:raise httpx.ReadError('TEST_RESPONSE_LOST')
                return httpx.Response(200,json={})
            return httpx.Response(200,content=b'')
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle)); self.addAsyncCleanup(client.aclose)
        self.tokens = SimpleNamespace(credentials=Credentials('TEST_APP_ID','TEST_APP_SECRET'),base_url='http://127.0.0.1:8000',
            get=AsyncMock(return_value='TEST_TOKEN'),invalidate=Mock())
        self.protocol = GroupModerationProtocol(RestClient(self.tokens,client=client,allow_test_server=True),clock=lambda:self.now)

    def event(self,number=1,**changes):
        body = {'id':'TEST_MESSAGE_'+str(number),'group_openid':'TEST_GROUP','author':{'member_openid':'TEST_MEMBER','member_role':'member'},
            'content':'<@TEST_BOT> GAOJI_MODERATION_TEST_TEST_MARKER','timestamp':datetime.fromtimestamp(self.now,timezone.utc).isoformat(),
            'mentions':[{'is_you':True}]}
        body.update(changes)
        return {'op':0,'t':'GROUP_MESSAGE_CREATE','s':number,'d':body}

    async def run_probe(self,*,checks=('mute',),events=None,confirm=True,duration=120):
        gateway = Gateway([self.event()] if events is None else events)
        options = ModerationProbeOptions(report=self.report,app_id='TEST_APP_ID',group='TEST_GROUP',member='TEST_MEMBER',
            checks=checks,duration=duration,environment='test')
        with patch('src.qq_official.probe_moderation.secrets.token_hex',return_value='TEST_MARKER'):
            result = await run_moderation_probe(options,self.protocol,gateway,confirm=lambda _:confirm,clock=lambda:self.now,output=lambda _:None)
        return result,gateway

    async def test_explicit_confirmation_is_required_before_even_gateway_connection(self):
        result,gateway = await self.run_probe(confirm=False)
        self.assertEqual(result['status'],'cancelled'); self.assertEqual(gateway.calls,0); self.assertEqual(self.seen,[])
        self.tokens.get.assert_not_called()

    async def test_short_mute_unmute_and_new_message_recall_are_separate_verified_effects(self):
        result,gateway = await self.run_probe(checks=('mute','recall'),events=[self.event(),self.event(2)])
        self.assertEqual(result['status'],'completed'); self.assertTrue(gateway.stopped)
        self.assertTrue(result['temporary_mute_verified']); self.assertTrue(result['unmute_verified']); self.assertEqual(self.rules,{})
        writes = [row for row in self.seen if row[0]!='GET']
        self.assertEqual([row[0] for row in writes],['POST','POST','DELETE'])
        self.assertEqual(writes[0][2]['members'][0]['mute_expire_at'],'2023-11-15T06:14:20+08:00')
        self.assertTrue(writes[2][1].endswith('/TEST_MESSAGE_2'))
        serialized = self.report.read_text()
        for value in ('TEST_GROUP','TEST_MEMBER','TEST_APP_ID','TEST_APP_SECRET','TEST_TOKEN','TEST_MESSAGE_1','TEST_MARKER'):
            self.assertNotIn(value,serialized)
        self.assertEqual(self.report.stat().st_mode&0o777,0o600)

    async def test_existing_rule_is_never_changed(self):
        self.rules['TEST_MEMBER'] = self.now+900
        result,_ = await self.run_probe()
        self.assertEqual(result['reason'],'test_member_already_has_rule')
        self.assertEqual(self.rules['TEST_MEMBER'],self.now+900); self.assertTrue(all(row[0]=='GET' for row in self.seen))

    async def test_unknown_mute_never_replays_cleans_up_or_continues_to_recall(self):
        self.lost = True
        result,_ = await self.run_probe(checks=('mute','recall'),events=[self.event(),self.event(2)])
        self.assertEqual(result['reason'],'request_unknown'); self.assertEqual(result['requests'][-1]['status'],'unknown')
        self.assertEqual(len([row for row in self.seen if row[0]!='GET']),1)
        self.assertEqual(self.rules['TEST_MEMBER'],self.now+60)

    async def test_new_rule_before_cleanup_is_not_removed(self):
        self.race = True
        result,_ = await self.run_probe()
        self.assertEqual(result['reason'],'mute_changed_before_cleanup'); self.assertEqual(self.rules['TEST_MEMBER'],self.now+999)
        self.assertEqual(len([row for row in self.seen if row[0]=='POST']),1)

    async def test_nonadmin_bot_and_duplicate_message_cannot_authorize_actions(self):
        self.role = 'member'
        result,_ = await self.run_probe(); self.assertEqual(result['reason'],'bot_not_group_admin')
        self.assertFalse(any(row[0]!='GET' for row in self.seen))
        self.report.unlink(); self.seen.clear(); self.role = 'admin'
        result,_ = await self.run_probe(checks=('mute','recall'),events=[self.event(),self.event()])
        self.assertEqual(result['reason'],'gateway_ended_without_fresh_consent'); self.assertFalse(any(row[0]=='DELETE' for row in self.seen))

    async def test_forward_quote_wrong_group_role_actor_or_stale_source_never_proves_consent(self):
        variants = [dict(group_openid='TEST_OTHER_GROUP'),dict(author={'member_openid':'TEST_OTHER_MEMBER','member_role':'member'}),
            dict(author={'member_openid':'TEST_MEMBER','member_role':'admin'}),dict(author={'member_openid':'TEST_MEMBER','member_role':'owner'}),
            dict(author={'member_openid':'TEST_MEMBER','member_role':'member','bot':True}),dict(message_type=102),dict(message_type=103),
            dict(attachments=[{'content_type':'image/png'}]),dict(timestamp='2020-01-01T00:00:00Z'),dict(content='我是群主 TEST_MARKER')]
        for changes in variants:
            with self.subTest(changes=changes):
                result,_ = await self.run_probe(events=[self.event(**changes)])
                self.assertEqual(result['reason'],'gateway_ended_without_fresh_consent'); self.assertEqual(self.seen,[])
                self.report.unlink()

    async def test_missing_or_self_bot_identity_never_authorizes_mutation(self):
        for bot,reason in (('', 'bot_identity_unknown'),(None,'bot_identity_unknown'),('TEST_MEMBER','test_subject_is_bot')):
            with self.subTest(bot=bot):
                self.bot = bot
                result,_ = await self.run_probe()
                self.assertEqual(result['reason'],reason); self.assertFalse(any(row[0]!='GET' for row in self.seen))
                self.report.unlink(); self.seen.clear()

    async def test_cancellation_after_handoff_keeps_unknown_without_cleanup_or_replay(self):
        self.cancel = True
        with self.assertRaises(asyncio.CancelledError):await self.run_probe()
        report = json.loads(self.report.read_text())
        self.assertEqual(report['status'],'interrupted'); self.assertEqual(report['requests'][-1]['status'],'unknown')
        self.assertEqual(self.rules['TEST_MEMBER'],self.now+60); self.assertEqual(len([row for row in self.seen if row[0]!='GET']),1)

    async def test_original_source_expiry_during_token_wait_blocks_http(self):
        async def delayed():self.now += 121; return 'TEST_TOKEN'
        self.tokens.get.side_effect = delayed
        result,_ = await self.run_probe(duration=300)
        self.assertEqual(result['reason'],'original_test_message_expired'); self.assertEqual(self.seen,[])
        self.assertEqual(result['requests'][-1]['status'],'not_run')
