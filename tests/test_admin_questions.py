"""Real PG audit records/session and physical filters, no model/QQ effects."""
from types import SimpleNamespace
import unittest

from unittest.mock import patch

from fastapi import FastAPI
import httpx

from src.bot_storage import DatabaseCalls
from src.bot_security.passwords import hash_password
from src.plugins.ai_chat.admin import AdminServices
from src.plugins.ai_chat.admin_api.realtime import _changed_database_resources
from src.plugins.ai_chat.adapters.qq_official import turn_source
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.adapters.qq_official.link_commands import parse_link_command
from src.plugins.ai_chat.adapters.qq_official.link_ingress import LinkIngress
from src.plugins.ai_chat.adapters.qq_official.question_authority import QuestionAuthority
from src.plugins.ai_chat.adapters.qq_official.question_notices import QuestionNotices
from src.plugins.ai_chat.adapters.qq_official.questions import Questions
from src.plugins.ai_chat.adapters.qq_official.send_store import OfficialSendStore
from src.plugins.ai_chat.adapters.qq_official.sender import OfficialSender
from src.plugins.ai_chat.adapters.qq_official.turn_platform import ExecutionBinding
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey
from src.qq_official.credentials import Credentials
from src.qq_official.http import RestClient
from src.qq_official.testing.fake_platform import FakePlatform
from src.qq_official.token import TokenManager
from tests import test_qq_official_sender as sender_fixture
from tests.admin_session_fixture import ApprovedClient, PASSWORD, register_admin
from tests.conversation_question_fixture import QuestionFixture
from tests import test_link_authority as authority_fixture


class AdminQuestionTests(QuestionFixture,unittest.IsolatedAsyncioTestCase):
    path = '/bot-admin/api/v1/conversation-questions'

    async def asyncSetUp(self):
        self.app = FastAPI()
        register_admin(self.app,AdminServices(version='TEST',started_at=1,database=self.database,
            settings=SimpleNamespace(admin_origin='https://test')),test_case=self)
        self.client = ApprovedClient(transport=httpx.ASGITransport(app=self.app),base_url='https://test')
        self.addAsyncCleanup(self.client.aclose)
        response = await self.client.get(self.path,headers={'X-Test-Login':'yes'})
        self.assertEqual(response.status_code,200,response.text)

    async def get(self,suffix='',**params):
        response = await self.client.get(self.path+suffix,params={'account_key':self.account.key,**params})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.headers['cache-control'],'no-store')
        return response.json()

    def alternate(self):
        self.now += 1; target = EndpointKey(self.account,'group','TEST_UNRELATED_TARGET'); self.provision(target)
        def command(endpoint,text):
            message = authority_fixture.LinkAuthorityTests.message(self,endpoint=endpoint,content=text)
            with self.tx() as cursor:
                self.auth.observe(cursor,self.token,message)
                return self.reducer.apply(cursor,self.token,message,parse_link_command(text))
        issued = command(self.endpoint,'/link')
        with self.tx() as cursor:self.reducer.record_issue(cursor,self.token,issued.challenge_id,outcome='committed',native_id='TEST_AUDIT_FIXTURE_ACCEPTED',received_at=self.now)
        linked = command(target,'/link '+issued.code)
        self.edge = dict(self.query('SELECT * FROM qq_official_conversation_links WHERE link_id=?',(linked.link_id,))[0])
        return target

    async def test_body_is_opt_in_and_reads_never_consume_close_or_send(self):
        row = self.insert_question()
        self.query("UPDATE qq_official_conversation_questions SET state='running',started_at=? WHERE question_id=?",(self.now,row['question_id']))
        self.query("UPDATE qq_official_conversation_questions SET state='answered',answer='TEST_PRIVATE_AUDIT_ANSWER',finished_at=? WHERE question_id=?",(self.now,row['question_id']))
        self.query('UPDATE qq_official_conversation_questions SET consumed_at=? WHERE question_id=?',(self.now,row['question_id']))
        tables = ('qq_official_conversation_questions','durable_jobs','qq_official_conversation_links','delivery_plans','deliveries','messages','admin_audit_log')
        before = {table:self.query('SELECT * FROM '+table) for table in tables}
        listing = await self.get('/records')
        self.assertNotIn(row['question'],repr(listing)); self.assertNotIn('TEST_PRIVATE_AUDIT_ANSWER',repr(listing))
        item = (await self.get('/records/'+row['question_id']))['item']
        self.assertEqual((item['question'],item['answer'],item['consumed_at'],item['delivery_state'],item['notice']),
            (row['question'],'TEST_PRIVATE_AUDIT_ANSWER',self.now,'not_tracked',None))
        self.assertNotIn('request_hmac',repr(item)); self.assertNotIn('source_actor_key',repr(item))
        self.now += 8000
        self.assertEqual((await self.get('/records/'+row['question_id']))['item'],item)
        link = (await self.get('/links'))['items'][0]
        self.assertEqual((link['state'],link['receipt_recorded'],link['authorization']),('active',True,'not_checked'))
        self.assertEqual({table:self.query('SELECT * FROM '+table) for table in tables},before)
        self.assertEqual(self.app.state.auth_messages,[])

    async def test_scope_filters_run_before_limit_without_history_alias_authorization(self):
        original = self.insert_question(); target = self.target; other = self.alternate()
        for _ in range(3):self.insert_question()
        self.query("UPDATE qq_official_endpoints SET canonical_scope_key=?,binding_state='revoked',membership_state='absent' WHERE endpoint_key=?",(target.scope.key,other.key))
        records = await self.get('/records',endpoint_key=target.key,limit=1)
        self.assertEqual([item['question_id'] for item in records['items']],[original['question_id']])
        self.assertIsNone(records['next_cursor'])
        links = await self.get('/links',endpoint_key=target.key,limit=1)
        self.assertEqual([item['target']['endpoint_key'] for item in links['items']],[target.key])
        self.assertIsNone(links['next_cursor'])

    async def test_account_environment_and_detail_selection_are_exact(self):
        row = self.insert_question(); foreign = AccountKey(self.account.platform,self.account.app_id,'production')
        endpoint = EndpointKey(foreign,'group',self.target.native_conversation_id); self.provision(endpoint)
        for suffix in ('/links','/records'):
            self.assertEqual((await self.get(suffix,account_key=foreign.key))['items'],[])
            response = await self.client.get(self.path+suffix,params={'account_key':self.account.key,'endpoint_key':endpoint.key})
            self.assertEqual(response.status_code,404)
        response = await self.client.get(self.path+'/records/'+row['question_id'],params={'account_key':foreign.key})
        self.assertEqual(response.status_code,404); self.assertNotIn(row['question'],response.text)
        response = await self.client.get(self.path+'/links',params={'account_key':'TEST_BAD_ACCOUNT'})
        self.assertEqual(response.status_code,404)
        first = await self.get(limit=1); second = await self.get(limit=1,after=first['next_cursor'])
        self.assertEqual({a['account_key'] for a in first['accounts']+second['accounts']},{foreign.key,self.account.key})

    async def test_fixed_job_id_pages_ignore_later_backdated_insert_and_bind_cursor(self):
        rows = [self.insert_question() for _ in range(3)]
        first = await self.get('/records',limit=1)
        self.now += 1
        late = self.insert_question(created_at=self.now-1,deadline_at=self.now-1+7200)
        second = await self.get('/records',limit=1,after=first['next_cursor'])
        third = await self.get('/records',limit=1,after=second['next_cursor'])
        items = first['items']+second['items']+third['items']
        self.assertEqual([item['question_id'] for item in items],[row['question_id'] for row in reversed(rows)])
        self.assertNotIn(late['question_id'],repr(items)); self.assertIsNone(third['next_cursor'])
        self.assertEqual(first['snapshot_job_id'],third['snapshot_job_id'])
        for suffix,params in (('/links',{'after':first['next_cursor']}),('/records',{'after':'TEST_BAD_CURSOR'}),
                ('/records',{'after':first['next_cursor'],'endpoint_key':self.target.key})):
            response = await self.client.get(self.path+suffix,params={'account_key':self.account.key,**params})
            self.assertEqual(response.status_code,422,response.text)

    async def test_live_connection_seek_pages_preserve_direction_and_revoked_fact(self):
        original = self.edge; other = self.alternate()
        self.query("UPDATE qq_official_conversation_links SET state='revoked',generation=generation+1,revoked_at=? WHERE link_id=?",(self.now,original['link_id']))
        first = await self.get('/links',limit=1); second = await self.get('/links',limit=1,after=first['next_cursor'])
        self.assertEqual(first['items'][0]['target']['endpoint_key'],other.key)
        self.assertEqual((second['items'][0]['link_id'],second['items'][0]['state'],second['items'][0]['generation']),
            (original['link_id'],'revoked',2))
        self.assertIsNone(second['next_cursor'])

    async def test_anonymous_member_and_approved_non_admin_never_read_private_records(self):
        row = self.insert_question()
        paths = ('','/links','/records','/records/'+row['question_id'])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='https://test') as client:
            for alias in (self.path,self.path.replace('/api/v1/','/api/')):
                for suffix in paths:
                    response = await client.get(alias+suffix)
                    self.assertEqual(response.status_code,401,response.text)
            store = self.app.state.mobile.store
            admin = store.account_for_qq('123456789')
            store.apply_account_change(admin,{'action':'create','username':'test_viewer','password_hash':hash_password(PASSWORD),'role':'member','qq_id':None})
            login = await client.post('/bot-admin/api/v1/auth/login',headers={'Origin':'https://test'},json={'username':'test_viewer','password':PASSWORD})
            self.assertEqual(login.status_code,200,login.text)
            for suffix in paths:
                response = await client.get(self.path+suffix,params={'account_key':self.account.key})
                self.assertEqual(response.status_code,403,response.text)
                self.assertNotIn(row['question'],response.text)

    async def test_public_notice_fact_stays_metadata_only_and_distinct_from_private_consume(self):
        calls = DatabaseCalls(); deliveries = DeliveryStore(self.database); self.addCleanup(deliveries.close)
        outbox = OfficialSendStore(deliveries,admission=self.admission,pepper=b'TEST_AUDIT_NOTICE_PEPPER_123456789',clock=lambda:self.now)
        binding = ExecutionBinding(turn_source,self.token,self.handle,self.job,self.admission)
        questions = Questions(QuestionAuthority(LinkIngress(self.auth,admission=self.admission),admission=self.admission),self.jobs)
        notices = QuestionNotices(questions,outbox); outbox.question_notice_authority = notices
        with self.tx() as cursor:
            observe_ready(cursor,self.token,{'d':{'user':{'id':'TEST_AUDIT_BOT','bot':True}}},self.now)
            cursor.execute("UPDATE qq_official_endpoints SET proactive_state='allowed' WHERE endpoint_key=?",(self.target.key,))
            committed_q,_ = questions.submit(cursor,self.token,binding,self.edge['link_id'],'TEST_PUBLIC_NOTICE_Q1')
            late_q,_ = questions.submit(cursor,self.token,binding,self.edge['link_id'],'TEST_PUBLIC_NOTICE_Q2')
        jobs = self.jobs.claim_due('TEST_AUDIT_WORKER',kinds=(account_job_kind('question',self.account.key),),now=self.now,limit=2)
        with self.tx() as cursor:
            for job in jobs:questions.claim(cursor,self.token,job)
        jobs_by_qid = {job.payload['question_id']:job for job in jobs}
        with self.tx() as cursor:
            s1,_,ref1 = notices.prepare(cursor,self.token,jobs_by_qid[committed_q['question_id']],'TEST_PUBLIC_AUDIT_NOTICE_ONE')
        fake = FakePlatform(clock=lambda:self.now); fake.app_id = self.account.app_id; await fake.start(); self.addAsyncCleanup(fake.close)
        fake.proactive_allowed[('group',self.target.native_conversation_id)] = True
        tokens = TokenManager(Credentials(self.account.app_id,fake.app_secret),fake.base_url,allow_test_server=True); self.addAsyncCleanup(tokens.aclose)
        rest = RestClient(tokens,allow_test_server=True); self.addAsyncCleanup(rest.aclose)
        async def ensure_current():
            def check():
                with self.tx() as cursor:assert_epoch(cursor,self.token)
            await calls(check)
        sender = OfficialSender(self.account,self.token,outbox,rest,self.ledger,database_calls=calls,
            ensure_current=ensure_current,wall_clock=lambda:self.now,poll_seconds=0.01,
            sleep=sender_fixture.OfficialSenderTests.fast_sleep)
        self.addAsyncCleanup(sender.stop); self.addAsyncCleanup(calls.close)
        await sender.send_delivery(ref1.delivery_ids[0])
        with self.tx() as cursor:
            s2,_,ref2 = notices.prepare(cursor,self.token,jobs_by_qid[late_q['question_id']],'TEST_PUBLIC_AUDIT_NOTICE_TWO')
        original_send = rest.send_message
        async def cancel_after_accept(*args,**kwargs):
            response = await original_send(*args,**kwargs)
            with self.tx() as cursor:notices.close(cursor,self.token,s2['notice_id'],reason='cancelled')
            return response
        with patch.object(rest,'send_message',side_effect=cancel_after_accept):
            await sender.send_delivery(ref2.delivery_ids[0])
        tables = ('qq_official_conversation_questions','qq_official_question_notice_sources','qq_official_question_notices',
            'delivery_plans','deliveries','messages')
        before = {table:self.query('SELECT * FROM '+table) for table in tables}
        listing = await self.get('/records')
        self.assertNotIn('TEST_PUBLIC_AUDIT_NOTICE',repr(listing))
        by_qid = {item['question_id']:item for item in listing['items']}
        first,second = by_qid[committed_q['question_id']],by_qid[late_q['question_id']]
        self.assertIsNotNone(first['notice']); self.assertIsNotNone(second['notice'])
        self.assertEqual((first['delivery_state'],first['consumed_at'],first['notice']['notice_id'],first['notice']['state'],
            first['notice']['delivery_state'],first['notice']['receipt_recorded'],first['notice']['bot_native_id'],first['notice']['bot_identity_source']),
            ('not_tracked',None,s1['notice_id'],'committed','committed',True,'TEST_AUDIT_BOT','ready'))
        self.assertEqual((second['delivery_state'],second['notice']['notice_id'],second['notice']['state'],
            second['notice']['delivery_state'],second['notice']['receipt_recorded']),
            ('not_tracked',s2['notice_id'],'cancelled','committed',True))
        for item in (first,second,(await self.get('/records/'+committed_q['question_id']))['item']):
            for forbidden in ('content_hmac','bot_actor_key','plan_id','receipt_id','delivery_receipt_hash'):
                self.assertNotIn(forbidden,item['notice'])
        self.assertEqual({table:self.query('SELECT * FROM '+table) for table in tables},before)

    async def test_realtime_maps_private_changes_to_metadata_only_resource(self):
        for table in ('qq_official_accounts','qq_official_link_challenges','qq_official_conversation_links',
                'qq_official_conversation_questions','qq_official_question_notice_sources','qq_official_question_notices'):
            self.assertIn('questions',_changed_database_resources({table:(0,0,0)},{table:(1,0,0)}))
        self.assertEqual((await self.client.post(self.path+'/links',json={})).status_code,405)
