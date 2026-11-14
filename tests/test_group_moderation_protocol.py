import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock

import httpx

from src.qq_official.credentials import Credentials
from src.qq_official.errors import QQAPIError
from src.qq_official.group_moderation import GroupModerationProtocol
from src.qq_official.http import RestClient,scoped_request_guard


class GroupModerationProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.seen,self.status,self.body = [],200,{}
        def handle(request):
            self.seen.append((request.method,request.url.path,json.loads(request.content) if request.content else None))
            if isinstance(self.body,Exception):raise self.body
            return httpx.Response(self.status,content=self.body) if isinstance(self.body,bytes) else httpx.Response(self.status,json=self.body)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(client.aclose)
        self.tokens = SimpleNamespace(credentials=Credentials('TEST_APP_ID','TEST_APP_SECRET'),base_url='http://127.0.0.1:8000',
            get=AsyncMock(return_value='TEST_TOKEN'),invalidate=Mock())
        self.rest = RestClient(self.tokens,client=client,allow_test_server=True)
        self.protocol = GroupModerationProtocol(self.rest,clock=lambda:1700000000)

    async def test_exact_routes_single_subject_and_rfc3339_expiry(self):
        for action in ('bot_state','mutes'):
            self.assertEqual((await self.protocol.request('TEST_GROUP',action)).status,200)
        for action in ('mute','update_mute'):
            await self.protocol.request('TEST_GROUP',action,member='TEST_MEMBER',seconds=60)
        await self.protocol.request('TEST_GROUP','unmute',member='TEST_MEMBER')
        self.body = b''
        self.assertEqual((await self.protocol.request('TEST_GROUP','recall',message='TEST_MESSAGE')).body,{})
        self.assertEqual([row[:2] for row in self.seen],[
            ('GET','/v2/groups/TEST_GROUP/bot_state'),('GET','/v2/groups/TEST_GROUP/restrict_chat_setting'),
            ('POST','/v2/groups/TEST_GROUP/restrict_chat_setting'),('POST','/v2/groups/TEST_GROUP/restrict_chat_setting'),
            ('POST','/v2/groups/TEST_GROUP/restrict_chat_setting'),('DELETE','/v2/groups/TEST_GROUP/messages/TEST_MESSAGE')])
        self.assertEqual(self.seen[2][2],{'members':[{'op':'add','member_openid':'TEST_MEMBER','mute_expire_at':'2023-11-15T06:14:20+08:00'}]})
        self.assertEqual(self.seen[3][2]['members'][0]['op'],'update')
        self.assertEqual(self.seen[4][2],{'members':[{'op':'del','member_openid':'TEST_MEMBER','mute_expire_at':''}]})

    async def test_invalid_paths_actions_and_arguments_fail_before_token_or_network(self):
        cases = [
            ('TEST_GROUP/other','mutes',{}),('TEST_GROUP','kick',{}),('TEST_GROUP','mutes',{'member':'TEST_MEMBER'}),
            ('TEST_GROUP','mute',{'member':'TEST_MEMBER','seconds':True}),('TEST_GROUP','mute',{'member':'TEST_MEMBER','seconds':0}),
            ('TEST_GROUP','mute',{'member':'TEST_MEMBER','seconds':2592001}),('TEST_GROUP','mute',{'member':'TEST_MEMBER','seconds':60,'message':'TEST_MESSAGE'}),
            ('TEST_GROUP','unmute',{'member':'TEST_MEMBER','seconds':0}),('TEST_GROUP','mute',{'member':'TEST_MEMBER/other','seconds':60}),
            ('TEST_GROUP','recall',{'message':'TEST_MESSAGE%2Fother'}),('TEST_GROUP','recall',{'message':'TEST_MESSAGE','member':'TEST_MEMBER'}),
            ('TEST_GROUP','recall',{}),(None,'mutes',{})]
        for group,action,arguments in cases:
            with self.subTest(action=action,arguments=arguments),self.assertRaises(ValueError):
                await self.protocol.request(group,action,**arguments)
        self.tokens.get.assert_not_called()
        self.assertEqual(self.seen,[])

    async def test_empty_response_does_not_relax_other_requests_or_error_statuses(self):
        self.body = b''
        for status in (200,204,401,429,500):
            self.status = status
            for action,arguments in (('mute',{'member':'TEST_MEMBER','seconds':60}),('bot_state',{})):
                with self.subTest(status=status,action=action),self.assertRaises(QQAPIError):
                    await self.protocol.request('TEST_GROUP',action,**arguments)
            if status!=200:
                with self.subTest(status=status,action='recall'),self.assertRaises(QQAPIError):
                    await self.protocol.request('TEST_GROUP','recall',message='TEST_MESSAGE')
        self.status = 200
        with self.assertRaisesRegex(QQAPIError,'invalid_response'):
            await self.rest.send_message('group','TEST_GROUP',{'content':'TEST_MESSAGE'})

    async def test_rejection_body_and_ambiguous_transport_are_not_retried(self):
        for status,body,accepted in ((200,{'err_code':40011030},False),(401,{},False),(429,{},False),
            (200,httpx.ReadError('TEST_READ_ERROR'),None),(200,b'not json',None),(200,{'err_code':'not numeric'},None)):
            self.status,self.body = status,body
            for action,arguments in (('mute',{'member':'TEST_MEMBER','seconds':60}),('recall',{'message':'TEST_MESSAGE'})):
                before = len(self.seen)
                with self.subTest(status=status,action=action),self.assertRaises(QQAPIError) as raised:
                    await self.protocol.request('TEST_GROUP',action,**arguments)
                self.assertIs(raised.exception.accepted,accepted)
                self.assertEqual(len(self.seen),before+1)
        self.assertTrue(self.tokens.invalidate.called)

    async def test_guard_after_token_wait_stops_original_mutation(self):
        elapsed = 0
        async def token():
            nonlocal elapsed
            elapsed = 61
            return 'TEST_TOKEN'
        self.tokens.get.side_effect = token
        async def guard(_stage):
            if elapsed>60:raise PermissionError('TEST_ORIGINAL_DEADLINE')
        with scoped_request_guard(guard,before_headers=True),self.assertRaisesRegex(PermissionError,'TEST_ORIGINAL_DEADLINE'):
            await self.protocol.request('TEST_GROUP','mute',member='TEST_MEMBER',seconds=60)
        self.assertEqual(self.seen,[])
