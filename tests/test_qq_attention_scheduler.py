"""Real PostgreSQL attention cursor, source authority and recovery under a test clock."""
import asyncio
from datetime import datetime, timezone
import os
import unittest
from unittest.mock import AsyncMock,patch

from alembic import command
from alembic.config import Config
from tests import test_qq_official_turn_endtoend as base
from tests import test_qq_official_runtime as fixture
from tests.test_qq_official_runtime_sender import verified_ready_platform
from src.plugins.ai_chat.conversation_config import change
from src.plugins.ai_chat.platform_types import EndpointKey
from src.plugins.ai_chat.adapters.qq_official.native_background_factory import background_factory,ProactiveConfiguration
from src.plugins.ai_chat.adapters.qq_official.proactive_ingress import ProactiveIngress,record_group_activity
from src.plugins.ai_chat.adapters.qq_official.migration_types import MigrationDenied
from src.plugins.ai_chat.proactive import ProactiveDecision


class AttentionSchedulerTests(base.OfficialTurnFixture, unittest.IsolatedAsyncioTestCase):
    async def setup_group(self,fake):
        app=self.application()
        runtime=self.runtime(loaded=fixture.loaded_config(fake.app_id,base=fake.base_url),
            application=app,ledger=app.message_ledger,journal=app.turn_journal,
            background_factory=background_factory(proactive_configuration=ProactiveConfiguration(enabled=True),proactive_profile='test'))
        self.assertTrue(await runtime.start())
        await asyncio.wait_for(fake.wait_ready(),5)
        await self.wait_db("SELECT bot_native_id FROM qq_official_accounts WHERE bot_identity_source='ready'")
        await fake.emit('GROUP_AT_MESSAGE_CREATE',{'id':'TEST_SETUP','content':'setup',
            'timestamp':datetime.now(timezone.utc).isoformat(),'group_openid':'TEST_GROUP',
            'author':{'member_openid':'TEST_MEMBER_A'},
            'mentions':[{'member_openid':'TEST_BOT_MENTION','is_you':True,'bot':True}]})
        await self.wait_db("SELECT inbox_id FROM qq_official_inbound_events WHERE native_message_id='TEST_SETUP'")
        ep=EndpointKey(runtime.loaded.account,'group','TEST_GROUP')
        with runtime.store.transaction() as c:
            c.execute("UPDATE qq_official_endpoints SET binding_state='active' WHERE endpoint_key=?",(ep.key,))
        proactive=runtime.background_service.proactive
        manual_glance=proactive.glance
        proactive.glance=AsyncMock(return_value=None)  # only disable the runtime's worker
        now=[int(runtime.store.clock())]
        proactive.clock=lambda:now[0]
        return runtime,proactive,manual_glance,ep,now

    async def emit(self,fake,native,content,*,author='TEST_MEMBER_A',mention=False):
        data={'id':native,'content':content,'timestamp':datetime.now(timezone.utc).isoformat(),
            'group_openid':'TEST_GROUP','author':{'member_openid':author,'username':author}}
        if mention:data['mentions']=[{'member_openid':'TEST_BOT_MENTION','is_you':True,'bot':True}]
        await fake.emit('GROUP_AT_MESSAGE_CREATE' if mention else 'GROUP_MESSAGE_CREATE',data)
        return (await self.wait_db('''SELECT inbox_id,canonical_message_id FROM qq_official_inbound_events
            WHERE native_message_id=? AND status='processed' ''',(native,)))[0]

    @staticmethod
    def fresh_ingress(p,now,scorer):
        return ProactiveIngress(token=p.token,transaction=p.tx,admission=p.admission,outbox=p.outbox,
            scorer=scorer,clock=lambda:now[0],database_calls=p.calls)

    async def test_model_reasons_and_failures_reach_real_observation_transitions(self):
        async with verified_ready_platform() as fake:
            runtime,p,manual,ep,now=await self.setup_group(fake)
            p.wake_turn=AsyncMock()
            p.media=None
            cases = [
                ({'action':'speak','reason':'TEST wake','classification_reason':'可以提供具体帮助'}, 'queued', '可以提供具体帮助'),
                ({'action':'speak','reason':'TEST wake','classification_reason':'可以纠正错误'}, 'discarded', '可以纠正错误'),
                ({'action':'silent','classification_reason':'没有具体帮助可提供'}, 'discarded', '没有具体帮助可提供'),
                (ValueError('TEST_PRIVATE_FAILURE'), 'discarded', None),
            ]
            verdicts=iter(x[0] for x in cases)
            async def classify(*args,**kwargs):
                value=next(verdicts)
                if isinstance(value,Exception):raise value
                if value.get('classification_reason')=='没有具体帮助可提供':
                    with runtime.store.transaction() as c:
                        change(c,ep,'proactive_enabled',False,actor='TEST_ADMIN',now=now[0])
                        change(c,ep,'proactive_enabled',True,actor='TEST_ADMIN',now=now[0])
                return value
            with patch('src.plugins.ai_chat.application.shared_proactive.ask_deepseek_json', side_effect=classify) as model:
                for i,(_,state,reason) in enumerate(cases):
                    now[0]=int(runtime.store.clock())
                    event=await self.emit(fake,f'TEST_REASON_{i}','TEST group conversation question')
                    now[0]+=4
                    with runtime.store.transaction() as c:
                        c.execute('UPDATE qq_official_endpoints SET attention_next_at=? WHERE endpoint_key=?',(now[0],ep.key))
                    if i == 1:
                        with patch.object(p, 'send_policy', AsyncMock(side_effect=PermissionError('TEST_PRIVATE_FAILURE'))):
                            await manual()
                    else:
                        await manual()
                    row=self.query('SELECT state,decision_reason FROM qq_official_proactive_observations WHERE inbox_id=?',(event['inbox_id'],))[0]
                    self.assertEqual((row['state'],row['decision_reason']),(state,reason))
            self.assertIn('不抄聊天原文、人名、ID，也不输出思维链',model.await_args_list[0].args[0])
            p.wake_turn.assert_awaited_once()
            schema = self.database.schema
            await runtime.stop()
            with patch.dict(os.environ, {'AI_POSTGRES_DSN': os.environ['TEST_POSTGRES_DSN'], 'AI_POSTGRES_SCHEMA': schema}):
                with self.assertRaisesRegex(RuntimeError, 'attention_reasons_require_backup_restore_before_downgrade'):
                    command.downgrade(Config('alembic.ini'), '0120_retire_context_feedback')

    async def test_burst_multi_author_scopes_last_twenty_and_keeps_trailing_non_candidate(self):
        async with verified_ready_platform() as fake:
            runtime,p,manual,ep,now=await self.setup_group(fake)
            seen=[]
            async def scorer(**kw):
                seen.append(kw)
                return ProactiveDecision('silent','')
            p.scorer=scorer
            await fake.emit('GROUP_AT_MESSAGE_CREATE',{'id':'TEST_OTHER_SETUP','content':'setup',
                'timestamp':datetime.now(timezone.utc).isoformat(),'group_openid':'TEST_OTHER_GROUP',
                'author':{'member_openid':'TEST_MEMBER_A'},
                'mentions':[{'member_openid':'TEST_BOT_MENTION','is_you':True,'bot':True}]})
            await self.wait_db("SELECT inbox_id FROM qq_official_inbound_events WHERE native_message_id='TEST_OTHER_SETUP'")
            other=EndpointKey(runtime.loaded.account,'group','TEST_OTHER_GROUP')
            with runtime.store.transaction() as c:
                c.execute("UPDATE qq_official_endpoints SET binding_state='active' WHERE endpoint_key=?",(other.key,))
            for i in range(25):
                await fake.emit('GROUP_MESSAGE_CREATE',{'id':f'TEST_OTHER_{i}',
                    'content':f'other group {i} question',
                    'timestamp':datetime.now(timezone.utc).isoformat(),'group_openid':'TEST_OTHER_GROUP',
                    'author':{'member_openid':'TEST_MEMBER_A'}})
                await self.wait_db('''SELECT inbox_id FROM qq_official_inbound_events
                    WHERE native_message_id=? AND status='processed' ''',(f'TEST_OTHER_{i}',))
                await self.emit(fake,f'TEST_M{i}',f'topic {i:02d} question',
                    author='TEST_MEMBER_A' if i%2 else 'TEST_MEMBER_B')
            await self.emit(fake,'TEST_TRAILING','hi',author='TEST_MEMBER_A')
            with runtime.store.transaction() as c:
                c.execute('''UPDATE qq_official_endpoints SET attention_cursor=attention_end,
                    attention_next_at=NULL WHERE endpoint_key=?''',(other.key,))
            pending=self.query('SELECT attention_cursor,attention_end FROM qq_official_endpoints WHERE endpoint_key=?',(ep.key,))[0]
            # Canonical IDs are global: 25 interleaved other-group messages
            # mean ID subtraction by 20 would lose half this batch.
            self.assertGreater(pending['attention_end']-pending['attention_cursor'],20)
            now[0]+=4
            await manual()
            self.assertEqual(len(seen),1)
            lines=seen[0]['text'].splitlines()
            self.assertEqual(len(lines),20)
            self.assertIn('topic 06 question',lines[0])
            self.assertIn('topic 24 question',lines[-2])
            self.assertIn('hi',lines[-1])
            self.assertIn('TEST_MEMBER_B',lines[0])
            self.assertIn('TEST_MEMBER_A',lines[1])
            self.assertIn('TEST_MEMBER_A',lines[-1])
            self.assertIn('TEST_MEMBER_B',seen[0]['actor'].key)
            self.assertEqual(len(seen[0]['history']),6)
            self.assertEqual((await manual()),None)
            self.assertEqual(len(self.query('SELECT * FROM qq_official_proactive_observations')),1)
            self.assertEqual(self.query('SELECT attention_cursor FROM qq_official_endpoints WHERE endpoint_key=?',
                (ep.key,))[0]['attention_cursor'],pending['attention_end'])
            await runtime.stop()

    async def test_cold_pending_and_claimed_unknown_decision_not_replayed(self):
        async with verified_ready_platform() as fake:
            runtime,p,manual,ep,now=await self.setup_group(fake)
            await self.emit(fake,'TEST_COLD','cold topic')
            scorer=AsyncMock(return_value=ProactiveDecision('silent',''))
            recovered=self.fresh_ingress(p,now,scorer)
            now[0]+=4
            await recovered.glance()  # pending work survives reconstruction
            self.assertEqual(scorer.await_count,1)
            await self.emit(fake,'TEST_UNKNOWN','new topic')
            # Crash after the atomic claim and before any model decision.
            now[0]=int(runtime.store.clock())+4
            source=recovered.claim_due()
            self.assertIsNotNone(source)
            self.assertEqual(self.query('SELECT state FROM qq_official_proactive_observations WHERE inbox_id=?',
                (source.inbox_id,))[0]['state'],'scoring')
            restarted=self.fresh_ingress(p,now,scorer)
            await restarted.glance()
            self.assertEqual(scorer.await_count,1)
            await runtime.stop()

    async def test_sparse_expiry_never_scores_and_lull_shortens_next_look(self):
        async with verified_ready_platform() as fake:
            runtime,p,manual,ep,now=await self.setup_group(fake)
            p.scorer=AsyncMock(return_value=ProactiveDecision('silent',''))
            with runtime.store.transaction() as c:
                c.execute('UPDATE qq_official_endpoints SET attention_activity_at=? WHERE endpoint_key=?',
                    (now[0]-100,ep.key))
            await self.emit(fake,'TEST_SPARSE','sparse topic')
            row=self.query('SELECT attention_next_at FROM qq_official_endpoints WHERE endpoint_key=?',(ep.key,))[0]
            self.assertLessEqual(row['attention_next_at']-now[0],21)  # before 30s source expiry
            now[0]=row['attention_next_at']+31
            await manual()
            p.scorer.assert_not_awaited()
            self.assertEqual(len(self.query('SELECT * FROM qq_official_proactive_observations')),0)
            now[0]=int(runtime.store.clock())
            with runtime.store.transaction() as c:
                c.execute('UPDATE qq_official_endpoints SET attention_activity_at=? WHERE endpoint_key=?',
                    (now[0]-601,ep.key))
            await self.emit(fake,'TEST_AFTER_LULL','new topic')
            row=self.query('SELECT attention_next_at FROM qq_official_endpoints WHERE endpoint_key=?',(ep.key,))[0]
            self.assertLessEqual(row['attention_next_at']-now[0],4)
            now[0]+=4
            await manual()
            self.assertEqual(p.scorer.await_count,1)
            await runtime.stop()

    async def test_missing_history_floor_never_releases_imported_source(self):
        async with verified_ready_platform() as fake:
            runtime,p,manual,ep,now=await self.setup_group(fake)
            await self.emit(fake,'TEST_MISSING_FLOOR','private imported topic')
            self.assertEqual(len(self.query('UPDATE conversations SET min_canonical_message_id=NULL WHERE scope_key=? RETURNING conversation_id',
                (ep.scope.key,))), 1)
            now[0]+=4
            self.assertIsNone(p.claim_due())
            self.assertEqual(self.query('SELECT * FROM qq_official_proactive_observations'),[])
            await runtime.stop()

    async def test_unverified_identity_does_not_consume_recovered_range(self):
        async with verified_ready_platform() as fake:
            runtime,p,manual,ep,now=await self.setup_group(fake)
            await self.emit(fake,'TEST_IDENTITY','waiting group question')
            now[0]+=4
            with patch('src.plugins.ai_chat.adapters.qq_official.proactive_ingress.snapshot',return_value=None):
                self.assertIsNone(p.claim_due())
            row=self.query('SELECT attention_cursor,attention_end FROM qq_official_endpoints WHERE endpoint_key=?',
                (ep.key,))[0]
            self.assertLess(row['attention_cursor'],row['attention_end'])
            self.assertIsNotNone(p.claim_due())
            await runtime.stop()

    async def test_new_activity_does_not_postpone_overdue_pending_glance(self):
        async with verified_ready_platform() as fake:
            runtime,p,manual,ep,now=await self.setup_group(fake)
            await self.emit(fake,'TEST_DUE_1','first group question')
            with runtime.store.transaction() as c:
                c.execute('UPDATE qq_official_endpoints SET attention_next_at=? WHERE endpoint_key=?',
                    (now[0]-1,ep.key))
            await self.emit(fake,'TEST_DUE_2','second group question')
            state=self.query('SELECT attention_next_at FROM qq_official_endpoints WHERE endpoint_key=?',
                (ep.key,))[0]
            self.assertEqual(state['attention_next_at'],now[0]-1)
            claimed=p.claim_due()
            self.assertIsNotNone(claimed)
            self.assertEqual(len(claimed.batch),2)
            # The claimed group is idle; its future hint must not strand a new
            # source behind that hint, even if the old due time has passed.
            with runtime.store.transaction() as c:
                c.execute('UPDATE qq_official_endpoints SET attention_next_at=? WHERE endpoint_key=?',
                    (now[0]+400,ep.key))
            await self.emit(fake,'TEST_DUE_3','third group question')
            fresh=self.query('SELECT attention_next_at FROM qq_official_endpoints WHERE endpoint_key=?',
                (ep.key,))[0]['attention_next_at']
            self.assertLessEqual(fresh-now[0],20)
            await runtime.stop()

    async def test_direct_mention_suppresses_claimed_source_without_rewinding_newer_pending(self):
        async with verified_ready_platform() as fake:
            runtime,p,manual,ep,now=await self.setup_group(fake)
            old=await self.emit(fake,'TEST_OLD','old topic')
            now[0]+=4
            source=p.claim_due()
            self.assertIsNotNone(source)
            await self.emit(fake,'TEST_DIRECT','@bot answer',mention=True)
            with self.assertRaises(MigrationDenied):p._current(source)
            state=self.query('SELECT attention_cursor,attention_end,attention_next_at FROM qq_official_endpoints WHERE endpoint_key=?',
                (ep.key,))[0]
            self.assertEqual(state['attention_cursor'],state['attention_end'])
            self.assertIsNone(state['attention_next_at'])
            newer=await self.emit(fake,'TEST_NEWER','newer topic')
            with runtime.store.transaction() as c:
                record_group_activity(c,ep,old['canonical_message_id'],int(runtime.store.clock()),direct=True)
            state=self.query('''SELECT attention_cursor,attention_end,attention_next_at,attention_suppressed_through
                FROM qq_official_endpoints WHERE endpoint_key=?''',(ep.key,))[0]
            self.assertLess(state['attention_cursor'],state['attention_end'])
            self.assertEqual(state['attention_end'],newer['canonical_message_id'])
            self.assertIsNotNone(state['attention_next_at'])
            self.assertGreaterEqual(state['attention_suppressed_through'],old['canonical_message_id'])
            await runtime.stop()
