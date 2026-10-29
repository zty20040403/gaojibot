"""Managed late self echoes settle one actual physical attempt after freeze."""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from nonebot.adapters.onebot.v11 import Message

from src.plugins.ai_chat.adapters.onebot_echo import settle_self_echo
from src.plugins.ai_chat.adapters.onebot_migration_runtime import OneBotMigrationRuntime, SourceJournal
from src.plugins.ai_chat.adapters.onebot_route import PhysicalCall, TransportObservation
from src.plugins.ai_chat.adapters.onebot_route_context import active_source_route, source_context
from src.plugins.ai_chat.adapters.qq_official.migration_types import MigrationDenied
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from tests.qq_official_migration_journal_fixture import JournalFixture
from tests.test_routine_tools import event


class MigrationEchoTests(JournalFixture):
    def setUp(self):
        super().setUp()
        self.sources=SourceJournal(self.routes,self.tx,clock=lambda:self.now)
        self.holder=SimpleNamespace(account=self.source.account,runtime_id=self.baseline.runtime_id)
        self.boundary=OneBotMigrationRuntime(sources=self.sources,baselines={self.source.account.key:self.holder},
            canonical_scope=lambda event:self.source_scope)
        def effect(cursor,proof,**kw):
            self.sources.require(cursor,proof,**{k:v for k,v in kw.items() if k!='now'})
        self.ports.effect=effect
        self.deliveries.source_recorder=self.boundary.capture_delivery
        self.bot=SimpleNamespace(self_id=self.source.account.app_id)

    def queued(self,key='TEST_ECHO',*,dispatch=True):
        with self.tx() as cursor:
            source=self.sources.create(cursor,baseline=self.holder,endpoint=self.source,scope=self.source_scope,
                actor=self.source_actor,kind='handler',ref=key,revision=1,fingerprint='f'*64,deadline=self.now+100)
            proof=self.sources.claim(cursor,source,baseline=self.holder)
        with source_context(proof,lambda:self.sources.current(proof)):
            delivery,_=self.deliveries.enqueue(idempotency_key=key,source_scope_key=self.source_scope.key,
                target_scope=self.source_scope,body=MessageBody((TextNode(0,'TEST echo'),)),now=self.now)
        self.sources.finish(proof)
        if dispatch:
            self.deliveries.claim_due(now=self.now)
            async def send():
                async with self.boundary.outgoing_context('delivery',delivery.delivery_id,self.bot):
                    permit=self.boundary._begin(PhysicalCall('send_group_msg',self.bot.self_id,
                        group_id=self.source.native_conversation_id),active_source_route.get())
                    self.boundary._observe(permit,TransportObservation(False,error_kind='TEST timeout'))
            asyncio.run(send())
            self.assertTrue(self.deliveries.mark_ambiguous(delivery.delivery_id,'TEST timeout',now=self.now))
        return delivery

    def echo(self,**changes):
        return event().model_copy(update=dict(self_id=int(self.bot.self_id),user_id=int(self.bot.self_id),
            group_id=int(self.source.native_conversation_id),message_id=998877,time=self.now,
            message=Message('TEST echo'),original_message=Message('TEST echo'))|changes)

    def test_actual_unknown_dispatch_settles_after_freeze_and_replays_once(self):
        delivery=self.queued()
        prior=self.query('SELECT * FROM delivery_attempts WHERE delivery_id=?',(delivery.delivery_id,))[0]
        self.assertEqual((prior['state'],prior['finished_at']),('ambiguous',self.now))
        self.freeze(self.plan())
        self.now+=1
        with patch.object(self.routes,'assert_active_route',side_effect=AssertionError('no new authority')):
            for _ in range(2):
                self.assertEqual(settle_self_echo(self.boundary,self.deliveries,self.bot,self.echo()),delivery.delivery_id)
        row=self.deliveries.get(delivery.delivery_id)
        self.assertEqual((row.status,row.native_message_id),('committed','998877'))
        self.assertEqual(self.query('SELECT state FROM migration_dispatch_activities')[0][0],'accepted')
        attempts=self.query('SELECT * FROM delivery_attempts WHERE delivery_id=?',(delivery.delivery_id,))
        self.assertEqual(len(attempts),1)
        self.assertEqual((attempts[0]['attempt_id'],attempts[0]['state'],attempts[0]['finished_at']),
            (prior['attempt_id'],'committed',prior['finished_at']))
        observations=self.query("SELECT * FROM migration_activity_observations WHERE receipt_json::jsonb->>'status'='accepted'")
        self.assertEqual(len(observations),1)
        self.assertEqual(self.query("SELECT count(*) FROM migration_activity_observations WHERE receipt_json::jsonb->>'status'='unknown'")[0][0],1)
        self.assertEqual(self.deliveries.claim_due(now=self.now),[])
        self.assertFalse(self.deliveries.mark_ambiguous(delivery.delivery_id,'TEST stale timeout',now=self.now))
        self.assertEqual(self.deliveries.get(delivery.delivery_id).status,'committed')
        self.assertIsNone(active_source_route.get())

    def test_unattempted_foreign_author_wrong_target_and_changed_content_do_not_settle(self):
        delivery=self.queued(dispatch=False)
        self.assertIsNone(settle_self_echo(self.boundary,self.deliveries,self.bot,self.echo()))
        self.assertEqual(self.deliveries.get(delivery.delivery_id).status,'pending')
        for changes in ({'self_id':999},{'user_id':999}):
            with self.assertRaises(MigrationDenied):settle_self_echo(self.boundary,self.deliveries,self.bot,self.echo(**changes))
        self.assertIsNone(settle_self_echo(self.boundary,self.deliveries,self.bot,self.echo(group_id=999)))
        self.assertIsNone(settle_self_echo(self.boundary,self.deliveries,self.bot,self.echo(original_message=Message('OTHER'))))

    def test_two_matching_unknown_sends_are_ambiguous(self):
        first=self.queued('TEST_FIRST')
        second=self.queued('TEST_SECOND')
        self.assertIsNone(settle_self_echo(self.boundary,self.deliveries,self.bot,self.echo()))
        self.assertEqual(self.deliveries.get(first.delivery_id).status,'ambiguous')
        self.assertEqual(self.deliveries.get(second.delivery_id).status,'ambiguous')

    def test_preprocessor_returns_before_approval_and_handlers(self):
        from nonebot.exception import IgnoredException
        from src.plugins.ai_chat.onebot_runtime import OneBotRuntime
        from unittest.mock import AsyncMock
        delivery=self.queued()
        self.freeze(self.plan())
        async def call(function,*args):return function(*args)
        runtime=SimpleNamespace(ready=True,migration_routes=self.boundary,
            context=SimpleNamespace(db_call=call,delivery_store=self.deliveries,mobile_authorization=object()))
        with patch('src.plugins.ai_chat.onebot_runtime.handle_approval_event',new_callable=AsyncMock) as approval:
            with self.assertRaises(IgnoredException):asyncio.run(OneBotRuntime.preprocess(runtime,self.bot,self.echo()))
            approval.assert_not_awaited()
        self.assertEqual(self.deliveries.get(delivery.delivery_id).status,'committed')
