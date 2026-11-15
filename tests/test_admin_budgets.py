"""Real admin session/CSRF and exact receipts with independent budget versions."""
import asyncio
from dataclasses import replace
import unittest
from unittest.mock import patch

import httpx

from src.plugins.ai_chat.admin_api import budgets
from src.plugins.ai_chat.budget_views import project
from src.plugins.ai_chat.conversation_config import change
from src.plugins.ai_chat.model_billing import BillingSource,ModelBilling
from src.plugins.ai_chat.model_pricing import ModelPricing
from src.plugins.ai_chat.model_usage import TokenUsage
from src.plugins.ai_chat.platform_types import EndpointKey
from tests import test_admin_conversations as base
from tests.test_llm_gateway import profile


class AdminBudgetTests(unittest.IsolatedAsyncioTestCase):
    setUp = base.AdminConversationTests.setUp
    tx = base.AdminConversationTests.tx
    query = base.AdminConversationTests.query
    provision = base.AdminConversationTests.provision
    path = '/bot-admin/api/v1/conversations/budgets'

    async def asyncSetUp(self):
        self.now = base.ingress.NOW
        clock = patch.object(budgets.time,'time',side_effect=lambda:self.now)
        clock.start(); self.addCleanup(clock.stop)
        await base.AdminConversationTests.asyncSetUp(self)

    async def view(self):
        response = await self.client.get(self.path,headers={'X-Test-Login':'yes'})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    async def set_budget(self,value,version=0,**extra):
        return await self.client.post(self.path,headers={'If-Match':str(version)},
            json=dict(endpoint_key=self.endpoint.key,daily_budget_usd=value,**extra))

    def bill(self,*,priced=True,usage=TokenUsage(1000,50,600,0)):
        store = ModelBilling(self.database,clock=lambda:self.now)
        selected = replace(profile('TEST',api_key='TEST_SECRET'),pricing=ModelPricing('2','0.2','8') if priced else None)
        call = store.begin(BillingSource(self.endpoint,'turn'),selected)
        if usage is not None:store.finish(call,usage)

    async def test_read_is_side_effect_free_default_off_and_uses_budget_not_conversation_version(self):
        snapshot = await self.view()
        self.assertEqual((snapshot['resource'],snapshot['resource_version']),('budgets',0))
        self.assertIsNone(snapshot['items'][0]['daily_budget_usd']); self.assertEqual(snapshot['items'][0]['spent_usd'],'0')
        self.assertEqual(snapshot['items'][0]['notice_state'],'not_required')
        for table in ('conversation_settings','conversation_budget_days','admin_resource_versions','admin_audit_log'):
            self.assertFalse(self.query('SELECT * FROM '+table),table)
        response = await self.set_budget(None)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['resource_version'],1); self.assertIsNone(response.json()['daily_budget_usd'])
        self.assertEqual(response.json()['budget_generation'],0)
        self.assertEqual((await self.client.get('/bot-admin/api/v1/conversations')).json()['resource_version'],0)
        with self.tx() as cursor:change(cursor,self.endpoint,'daily_budget_usd','0.10',actor='TEST_NATIVE',now=self.now)
        stale = await self.set_budget('0',1)
        self.assertEqual(stale.status_code,409); self.assertEqual(stale.json()['detail']['resource'],'budgets')
        self.assertEqual(len(self.query('SELECT * FROM admin_audit_log')),2)
        self.assertEqual(self.app.state.auth_messages,[])

    async def test_actual_unknown_receipts_lower_bound_stop_and_notification_are_projected_atomically(self):
        self.bill(); self.bill(priced=False); self.bill(usage=None)
        self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,TRUE,7)',(self.endpoint.scope.key,))
        await self.view()
        response = await self.set_budget('0.001000')
        self.assertEqual(response.status_code,200,response.text)
        view = response.json()
        self.assertEqual((view['daily_budget_usd'],view['spent_usd']),('0.001','0.00132'))
        self.assertEqual((view['cost_is_lower_bound'],view['incomplete_calls'],view['pending_calls']),(True,2,1))
        self.assertEqual((view['blocked'],view['stopped'],view['notice_state']),(True,True,'pending'))
        self.assertEqual(self.query('SELECT stop_generation FROM conversation_settings')[0]['stop_generation'],7)
        self.assertEqual(view['budget_generation'],2)
        with self.tx() as cursor:
            tomorrow = project(cursor,(self.endpoint,),now=view['day_end'])[0]
        self.assertEqual((tomorrow['blocked'],tomorrow['spent_usd'],tomorrow['notice_state'],tomorrow['stopped']),
            (False,'0','not_required',True))
        self.assertEqual(len(self.query('SELECT * FROM conversation_budget_days')),1)
        self.notify.assert_not_called()

    async def test_history_alias_does_not_borrow_limit_or_receipts_in_admin_batch(self):
        self.bill()
        other = EndpointKey(self.account,'group','TEST_OTHER_BUDGET'); self.provision(other)
        self.query("UPDATE qq_official_endpoints SET binding_state='revoked',membership_state='absent' WHERE endpoint_key=?",(other.key,))
        self.query('UPDATE qq_official_endpoints SET canonical_scope_key=? WHERE endpoint_key=?',(other.scope.key,self.endpoint.key))
        with self.tx() as cursor:change(cursor,other,'daily_budget_usd','999.000001',actor='TEST_NATIVE',now=self.now)
        rows = {item['endpoint_key']:item for item in (await self.view())['items']}
        self.assertIsNone(rows[self.endpoint.key]['daily_budget_usd']); self.assertEqual(rows[self.endpoint.key]['spent_usd'],'0.00132')
        self.assertEqual(rows[other.key]['daily_budget_usd'],'999.000001'); self.assertEqual(rows[other.key]['spent_usd'],'0')

    async def test_parallel_same_version_has_one_winner_and_rollback_never_leaves_crossing(self):
        await self.view()
        original = budgets.change
        def fail(*args,**kwargs):
            original(*args,**kwargs)
            raise RuntimeError('TEST rollback')
        with patch.object(budgets,'change',side_effect=fail):
            self.assertEqual((await self.set_budget('0')).status_code,503)
        for table in ('conversation_settings','conversation_budget_days','admin_resource_versions','admin_audit_log'):
            self.assertFalse(self.query('SELECT * FROM '+table),table)
        results = await asyncio.gather(self.set_budget('0'),self.set_budget('1'))
        self.assertEqual(sorted(response.status_code for response in results),[200,409])
        self.assertEqual(len(self.query('SELECT * FROM admin_audit_log')),1)

    async def test_authenticated_csrf_and_strict_decimal_only_inputs_are_required(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='https://test') as anonymous:
            self.assertEqual((await anonymous.get(self.path)).status_code,401)
            self.assertEqual((await anonymous.post(self.path,headers={'Origin':'https://test'},json={})).status_code,401)
        await self.view()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='https://test',cookies=self.client.cookies) as no_csrf:
            response = await no_csrf.post(self.path,headers={'Origin':'https://test','If-Match':'0'},
                json={'endpoint_key':self.endpoint.key,'daily_budget_usd':'0'})
            self.assertEqual(response.status_code,403)
        self.assertEqual((await self.client.post(self.path,json={'endpoint_key':self.endpoint.key,'daily_budget_usd':'0'})).status_code,428)
        for value in (0,True,0.01,{},['0']):
            with self.subTest(value=value):self.assertEqual((await self.set_budget(value)).status_code,422)
        for value in ('NaN','1e2','-1',' 1','0.0000001','1000001'):
            with self.subTest(value=value):self.assertEqual((await self.set_budget(value)).status_code,400)
        self.assertEqual((await self.set_budget('1',authorized=True)).status_code,422)
        self.assertEqual((await self.client.post(self.path,headers={'If-Match':'0'},json={'endpoint_key':'TEST_FOREIGN','daily_budget_usd':'1'})).status_code,404)
        self.assertFalse(self.query('SELECT * FROM conversation_settings')); self.assertFalse(self.query('SELECT * FROM admin_audit_log'))
