"""Actual Journal approval + shared official tool loop + revoked-cache fences."""
from dataclasses import replace
import html
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch

from src.plugins.ai_chat.adapters.qq_official.migration_history import HistoryUnavailable
from src.plugins.ai_chat.adapters.qq_official.migration_history_service import MigrationHistoryService
from src.plugins.ai_chat.adapters.qq_official.migration_history_clear import clear_public_history,lock_history_revocation
from src.plugins.ai_chat.agent.control import RouteAuthorityLost,RouteFence,active_route_fence
from src.plugins.ai_chat.application.platform_chat import PlatformChatService
from src.plugins.ai_chat.llm_gateway import LLMGateway,LLMConnectionError
from src.plugins.ai_chat.message_ir import MessageBody,TextNode
from src.plugins.ai_chat.model_catalog import ModelCatalog
from src.plugins.ai_chat.platform_types import InboundMessage,PlatformFeatures,OutboundCapabilities
from tests.qq_official_migration_journal_fixture import JournalFixture
from tests import test_qq_official_turn_endtoend as turn_fixture
from tests.test_shared_agent_turn import completion
from tests.test_model_lease_fence import profile


class MigrationHistoryServiceTests(JournalFixture,unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.history_source=object()
        self.reader_allowed=True
        self.history=MigrationHistoryService(self.migration,token=self.token,clock=lambda:self.now,
            reader_guard=lambda c,endpoint,source,now:self.reader_allowed and endpoint==self.endpoint and source is self.history_source)
        self.ledger.public_history_clear=lambda cursor,scope:clear_public_history(cursor,scope,lock_routes=lock_history_revocation)
        self.first=self.source_message('OLD_FIRST','PUBLIC original history')
        self.second=self.source_message('OLD_SECOND','PUBLIC next history')

    def source_message(self,native,text):
        return self.ledger.record_message(self.source_scope,native_message_id=native,
            sender_native_user_id=self.source_actor.native_user_id,sender_display='Original author',
            body=MessageBody((TextNode(0,text),)),occurred_at=self.now-10)

    def prepared(self):
        initial=self.plan()
        with self.tx() as cursor:
            plan,preview=self.history.prepare(cursor,migration_id=initial.migration_id,authorization=self.authorization,now=self.now)
        return initial,plan,preview

    def approved(self):
        initial,plan,preview=self.prepared()
        self.switch(plan)
        original=self.migration.complete
        def complete(cursor,**kwargs):
            result=original(cursor,**kwargs)
            self.history.activate(cursor,migration_id=plan.migration_id)
            return result
        with patch.object(self.migration,'complete',side_effect=complete):
            JournalFixture.complete(self,plan)
        return initial,plan,preview

    def session(self):
        return self.history.session(endpoint=self.endpoint,source=self.history_source,transaction=self.tx)

    def test_history_digest_is_in_review_and_only_completed_can_activate(self):
        initial,plan,preview=self.prepared()
        self.assertNotEqual(initial.preview_digest,plan.preview_digest)
        with self.assertRaises(Exception):self.freeze(initial)
        with self.tx() as cursor:
            with self.assertRaises(HistoryUnavailable):self.history.activate(cursor,migration_id=plan.migration_id)
        with self.assertRaises(HistoryUnavailable):self.session().prompt()
        stored=json.loads(self.query('SELECT preview_json FROM migration_journals')[0][0])
        self.assertEqual(stored['public_history']['manifest_digest'],preview.manifest_digest)

    def test_issued_capture_never_merges_author_or_native_lookup(self):
        _,_,preview=self.approved();session=self.session()
        text=session.prompt()
        self.assertIn('Original author',text);self.assertIn('PUBLIC original history',text)
        self.assertIn(self.source_scope.key,text)
        rows=session.search('original')
        self.assertEqual(rows[0]['original_user_id'],self.source_actor.native_user_id)
        self.assertTrue(rows[0]['handle'].startswith('migration:'+preview.grant_id+':'))
        self.assertEqual(session.expand(rows[0]['handle'])['text'],'PUBLIC original history')
        self.assertIsNone(self.ledger.canonical_id_for_native(self.endpoint.scope,'OLD_FIRST',endpoint=self.endpoint))
        self.assertIsNone(session.expand('msg#'+str(self.first.canonical_message_id)))
        with self.tx() as cursor:
            with self.assertRaises(HistoryUnavailable):self.history.require_current(cursor,replace(session._captured))

    def test_clear_after_capture_revokes_prompt_tools_and_provider_without_retry(self):
        self.approved();session=self.session();session.prompt()
        self.ledger.hide_history(self.source_scope)
        for call in (session.prompt,lambda:session.search('PUBLIC'),lambda:session.expand('migration:any')):
            with self.assertRaises(HistoryUnavailable):call()
        self.assertEqual('revoked',self.query('SELECT state FROM qq_migration_public_history_grants')[0][0])

    def test_default_reader_and_wrong_source_are_denied(self):
        self.approved()
        deny=MigrationHistoryService(self.migration,token=self.token,clock=lambda:self.now)
        for service,source in ((deny,self.history_source),(self.history,object())):
            with self.tx() as cursor:
                with self.assertRaises(HistoryUnavailable):service.capture(cursor,endpoint=self.endpoint,source=source)

    def test_clear_after_lease_loss_and_bot_departure_still_revokes(self):
        self.approved();self.account_lease.close()
        self.query("UPDATE qq_official_endpoints SET membership_state='absent',binding_state='revoked'")
        self.query('UPDATE qq_official_accounts SET enabled=FALSE')
        self.ledger.hide_history(self.endpoint.scope)
        self.assertEqual('revoked',self.query('SELECT state FROM qq_migration_public_history_grants')[0][0])

    def test_oversized_view_rejects_with_fixed_error_without_exposing_content(self):
        for i in range(25):self.source_message('BIG_'+str(i),'PRIVATE_TEST_SENTINEL'+'x'*12000)
        self.approved()
        with self.assertRaisesRegex(HistoryUnavailable,'^history_view_too_large$'):
            self.session().prompt()

    async def test_real_gateway_retry_cannot_reuse_revoked_view(self):
        self.approved();session=self.session();session.prompt()
        primary,backup=profile(),profile('backup')
        async def fail(*args,**kwargs):
            self.ledger.hide_history(self.source_scope)
            raise LLMConnectionError('TEST provider transport failure')
        provider=SimpleNamespace(create_completion=AsyncMock(side_effect=fail))
        gateway=LLMGateway({'openai-chat':provider},catalog=ModelCatalog({p.name:p for p in (primary,backup)},default_profile='primary'))
        with session.guard_scope(),self.assertRaises(RouteAuthorityLost):
            await gateway.create_completion(primary,messages=[{'role':'user','content':session.prompt()}])
        provider.create_completion.assert_awaited_once()

    async def test_shared_official_loop_reads_context_port_and_rechecks_next_rpc(self):
        self.approved()
        self.logger=Mock()
        app=turn_fixture.OfficialTurnEndToEndTests.application(self)
        app.semantic_recall=None
        inbound=InboundMessage(self.endpoint,self.actor,'TEST_EVENT','TEST_NEW_MESSAGE','Current author','member',
            MessageBody((TextNode(0,'检索旧群PUBLIC记录'),)),True,None,self.now,None)
        platform=SimpleNamespace(account=self.account,features=PlatformFeatures(),capabilities=OutboundCapabilities())
        service=PlatformChatService(app,scope_enabled=lambda _:True,public_history_factory=lambda i,p:self.session())
        async def model(**kwargs):
            messages=kwargs['messages']
            if not any(item['role']=='tool' for item in messages):
                return completion(tool='context_search',arguments={'query':'PUBLIC'})
            text='\n'.join(str(item.get('content','')) for item in messages)
            self.assertIn('Original author',text);self.assertIn('migrated_public_history',text)
            self.assertIn('PUBLIC original history',text)
            receipts=[json.loads(html.unescape(item['content'].split('\n',1)[1].rsplit('\n</untrusted',1)[0]))
                      for item in messages if item['role']=='tool']
            if len(receipts)==1:
                self.assertIn('migrated_public_history',receipts[0],repr(receipts[0]))
                records=receipts[0]['migrated_public_history']
                self.assertTrue(records);self.assertEqual(records[0]['original_author'],'Original author')
                answer=completion(tool='context_expand',arguments={'target':records[0]['handle']})
                answer.choices[0].message.tool_calls[0].id='TEST_EXPAND_CALL'
                return answer
            self.assertEqual(receipts[-1]['record']['text'],'PUBLIC original history')
            return completion('TEST history answer')
        with patch('src.plugins.ai_chat.agent_loop._create_completion',side_effect=model) as create:
            answer=await service.ask(inbound,platform,'检索旧群PUBLIC记录')
        self.assertEqual(answer,'TEST history answer');self.assertEqual(create.await_count,3)
        # The same actual shared loop must stop before a second physical model
        # call when a tool's await boundary observes a revoked history grant.
        async def revoke_after_first(**kwargs):
            self.ledger.hide_history(self.source_scope)
            return completion(tool='use_skill',arguments={'name':'TEST_NOT_FOUND'})
        with patch('src.plugins.ai_chat.agent_loop._create_completion',side_effect=revoke_after_first) as create:
            with self.assertRaises(RouteAuthorityLost):await service.ask(inbound,platform,'检查技能')
        create.assert_awaited_once()
        self.assertIsNone(active_route_fence.get())

    def test_session_fence_preserves_parent_route_fence(self):
        self.approved();session=self.session();session.prompt()
        called=[]
        parent=RouteFence(lambda:called.append(1) or False)
        token=active_route_fence.set(parent)
        try:
            with session.guard_scope(),self.assertRaises(RouteAuthorityLost):
                active_route_fence.get().assert_current()
            self.assertIs(active_route_fence.get(),parent)
        finally:
            active_route_fence.reset(token)
        self.assertEqual(called,[1])
