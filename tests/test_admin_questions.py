"""Real PG audit records/session and physical filters, no model/QQ effects."""
from types import SimpleNamespace
import unittest

from fastapi import FastAPI
import httpx

from src.bot_security.passwords import hash_password
from src.plugins.ai_chat.admin import AdminServices
from src.plugins.ai_chat.admin_api.realtime import _changed_database_resources
from src.plugins.ai_chat.adapters.qq_official.link_commands import parse_link_command
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey
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
        self.assertEqual((item['question'],item['answer'],item['consumed_at'],item['delivery_state']),
            (row['question'],'TEST_PRIVATE_AUDIT_ANSWER',self.now,'not_tracked'))
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

    async def test_realtime_maps_private_changes_to_metadata_only_resource(self):
        for table in ('qq_official_accounts','qq_official_link_challenges','qq_official_conversation_links','qq_official_conversation_questions'):
            self.assertIn('questions',_changed_database_resources({table:(0,0,0)},{table:(1,0,0)}))
        self.assertEqual((await self.client.post(self.path+'/links',json={})).status_code,405)
