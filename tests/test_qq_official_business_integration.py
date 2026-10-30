"""Actual M2 ingress + M4 lifecycle + M3 manifests, official wiki worker guards."""
from datetime import datetime,timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock

from tests.test_qq_official_migration_services import MigrationServicesFixture
from tests.qq_official_migration_project_fixture import install_project_owner
from src.plugins.ai_chat.adapters.qq_official import turn_source as execution
from src.plugins.ai_chat.adapters.qq_official.ingest import process_inbox
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.turns import OfficialTurnConsumer
from src.plugins.ai_chat.adapters.qq_official.turn_platform import TransportState
from src.plugins.ai_chat.adapters.qq_official.business_commands import build_business_services
from src.plugins.ai_chat.adapters.qq_official.wiki_service import OfficialWikiService
from src.plugins.ai_chat.application.commands import CommandServices
from src.plugins.ai_chat.context_store import ContextStore
from src.plugins.ai_chat.group_wiki import GroupWikiStore,WikiLeaseLost,WIKI_JOB_KIND
from src.plugins.ai_chat.long_term_memory import LongTermMemoryStore
from src.plugins.ai_chat.message_ir import MessageBody,TextNode,body_from_json
from src.plugins.ai_chat.platform_types import ActorKey


class BusinessCommandPGTests(MigrationServicesFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp();install_project_owner(self)
        self.collection=True
        self.context=ContextStore(self.database,historian_managed=True);self.addCleanup(self.context.close)
        temp=TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.memory_path=Path(temp.name)/'memory.json'
        self.memory=LongTermMemoryStore(self.memory_path)
        self.app=SimpleNamespace(long_term_memory=self.memory,message_ledger=self.ledger,context_store=self.context,
            logger=Mock(),settings=SimpleNamespace(group_wiki_hour=0,group_wiki_timezone='UTC',group_wiki_check_seconds=30))
        self.generated=AsyncMock(return_value={'items':[]})
        self.wiki=OfficialWikiService(runtime=self.runtime,token=self.token,app=self.app,
            collection_allowed=lambda *_:self.collection,generate=self.generated)
        self.addCleanup(self.wiki.close)
        self.runtime.native_commands=SimpleNamespace(bind=lambda **kwargs:{'wiki':self.wiki.commands_for(
            life=kwargs['life'],inbound=kwargs['inbound'])})
        self.agent=AsyncMock(side_effect=AssertionError('business command must not invoke chat model'))
        self.consumer=OfficialTurnConsumer(api=execution,token=self.token,store=self.store,outbox=self.outbox,
            journal=self.journal,database_calls=self.calls,orchestrator=self.agent,command_services=CommandServices(),
            business_services=build_business_services(self.runtime,self.app),
            transport_state=lambda:TransportState(self.account,True,True),clock=lambda:self.now)
        with self.tx() as c:
            observe_ready(c,self.token,{'op':0,'t':'READY','d':dict(session_id='TEST_ASSEMBLY',user=dict(id='TEST_REAL_BOT',bot=True))},self.now)

    async def command(self,text,*,actor=None):
        actor=actor or self.actor;self.sequence+=1;native='TEST_BUSINESS_'+str(self.sequence)
        result=self.store.accept({'op':0,'t':'GROUP_AT_MESSAGE_CREATE','s':self.sequence,
            'd':dict(id=native,group_openid=self.endpoint.native_conversation_id,author=dict(member_openid=actor.native_user_id),
                content=text,timestamp=datetime.fromtimestamp(self.now,timezone.utc).isoformat())},
            token=self.token,session_id='TEST_ASSEMBLY')
        with self.tx() as c:
            observed=process_inbox(c,inbox_id=result.inbox_id,lease_token=self.token,ledger=self.ledger,jobs=self.jobs,
                epoch_guard=assert_epoch,admission=self.store.admitted,now=self.now)
        jobs=self.jobs.claim_due('TEST_BUSINESS',kinds=(account_job_kind('turn',self.account.key),),now=self.now)
        self.assertEqual(len(jobs),1);job=jobs[0]
        self.assertEqual((await self.consumer(job))['status'],'handed_off')
        self.assertEqual((await self.consumer(job))['status'],'handed_off')
        self.jobs.mark_succeeded(job.job_id,job.lease_owner,lease_generation=job.lease_generation,now=self.now)
        row=self.query('SELECT * FROM qq_official_turn_executions WHERE canonical_message_id=?',(observed.canonical_message_id,))[0]
        body=body_from_json(row['result_body_json']);text=''.join(node.text for node in body.nodes)
        self.agent.assert_not_awaited()
        return text,row,job

    def chapter(self):
        message=self.ledger.record_message(self.endpoint.scope,native_message_id='TEST_CHAPTER',
            sender_native_user_id=self.actor.native_user_id,sender_display='TEST owner',
            body=MessageBody((TextNode(0,'We chose Python for the project'),)),occurred_at=self.now,
            actor=self.actor,source_endpoint=self.endpoint)
        candidate=self.context.capture_candidate(self.ledger,self.endpoint.scope,settled=True)
        self.context.publish_generated(candidate,('Python','Python','Python'))
        return message

    async def test_wiki_config_command_account_worker_and_replay_keep_one_snapshot(self):
        text,config,job=await self.command('/群百科 开启')
        self.assertIn('已开启',text)
        self.assertEqual(self.query('SELECT source_execution_id FROM qq_official_wiki_configurations')[0][0],config['execution_id'])
        self.assertEqual(self.query('SELECT bot_id FROM group_wiki_settings')[0][0],'TEST_REAL_BOT')
        self.assertEqual(self.query('SELECT count(*) FROM qq_official_wiki_configurations')[0][0],1)
        legacy=GroupWikiStore(self.database,ledger=self.ledger,context=self.context,jobs=self.jobs);self.addCleanup(legacy.close)
        self.assertEqual(legacy.enabled_groups(),[])
        message=self.chapter()
        self.assertEqual(await self.wiki.worker.schedule_once(now=self.now),1)
        self.assertEqual(self.jobs.claim_due('TEST_LEGACY',kinds=(WIKI_JOB_KIND,),now=self.now),[])
        wiki_job=self.jobs.claim_due('TEST_WIKI',kinds=(self.wiki.store.job_kind,),now=self.now)[0]
        self.generated.return_value={'items':[dict(category='decision',title='Language',content='Use Python',evidence_ids=[message.canonical_message_id])]}
        result=await self.wiki.handle_job(wiki_job)
        self.assertEqual(result['status'],'published');self.generated.assert_awaited_once()
        self.assertEqual((await self.wiki.handle_job(wiki_job))['status'],'published');self.generated.assert_awaited_once()
        page=self.wiki.store.page(self.endpoint.scope)
        self.assertEqual(len(page['items']),1)
        self.assertEqual(self.query('SELECT count(*) FROM group_wiki_publications')[0][0],1)
        # Background publication changes only the existing wiki page, not QQ.
        self.assertEqual(self.query('SELECT count(*) FROM delivery_plans')[0][0],1)

    async def test_unknown_collection_or_missing_owner_cannot_enable(self):
        self.collection=False
        text,_,_=await self.command('/群百科 开启');self.assertIn('不可用',text)
        self.assertEqual(self.query('SELECT * FROM qq_official_wiki_configurations'),[])
        self.collection=True
        other=ActorKey(self.account,'group_member',self.endpoint.native_conversation_id,'TEST_NONOWNER')
        text,_,_=await self.command('/群百科 开启',actor=other);self.assertIn('不可用',text)
        self.assertEqual(self.query('SELECT * FROM qq_official_wiki_configurations'),[])

    async def test_revoke_during_generation_cannot_publish_old_worker(self):
        await self.command('/群百科 开启');self.chapter()
        self.wiki.store.enqueue(self.endpoint.scope,slot='TEST_SLOT',now=self.now)
        job=self.jobs.claim_due('TEST_WIKI',kinds=(self.wiki.store.job_kind,),now=self.now)[0]
        async def generate(payload):
            self.query("UPDATE qq_official_owner_actor_links SET state='revoked',revision=revision+1 WHERE endpoint_key=?",(self.endpoint.key,))
            return {'items':[]}
        self.generated.side_effect=generate
        self.assertEqual((await self.wiki.handle_job(job))['status'],'superseded')
        self.assertEqual(self.query('SELECT * FROM group_wiki_publications'),[])
        self.assertEqual(self.query('SELECT count(*) FROM group_wiki_generations')[0][0],1)

    async def test_disable_and_reenable_config_cannot_reuse_old_job_source(self):
        await self.command('/群百科 开启');self.chapter()
        self.wiki.store.enqueue(self.endpoint.scope,slot='TEST_OLD',now=self.now)
        old=self.jobs.claim_due('TEST_OLD',kinds=(self.wiki.store.job_kind,),now=self.now)[0]
        await self.command('/群百科 关闭');await self.command('/群百科 开启')
        with self.assertRaises(WikiLeaseLost):self.wiki.store.prepare_generation(old)
        self.assertEqual(self.query('SELECT version FROM group_wiki_settings')[0][0],3)
        self.assertEqual(len(self.query('SELECT * FROM qq_official_wiki_configurations')),3)
        self.assertFalse(self.generated.called)

    async def test_memory_actual_command_audit_clear_and_durable_replay(self):
        text,row,job=await self.command('/memory 添加 喜欢绿色')
        self.assertIn('记',text)
        memories=self.memory.list_entries([self.actor.key]);self.assertEqual(len(memories),1)
        self.assertEqual(memories[0].creator_actor_key,self.actor.key)
        self.assertIsNone(memories[0].creator_user_id)
        self.assertEqual(len(self.memory.list_entries([self.actor.key])),1)
        text,_,_=await self.command('/memory 清空');self.assertIn('清空',text)
        self.assertEqual(self.memory.list_entries([self.actor.key]),[])
        reopened=LongTermMemoryStore(self.memory_path)
        self.assertEqual(reopened.list_entries([self.actor.key]),[])

    async def test_real_non_at_collection_evidence_is_endpoint_and_epoch_bound(self):
        from src.plugins.ai_chat.adapters.qq_official.wiki_configuration import WikiCollectionEvidence
        self.sequence+=1
        result=self.store.accept({'op':0,'t':'GROUP_MESSAGE_CREATE','s':self.sequence,
            'd':dict(id='TEST_COLLECTION_OBSERVATION',group_openid=self.endpoint.native_conversation_id,
                author=dict(member_openid=self.actor.native_user_id),content='An ordinary group observation',
                timestamp=datetime.fromtimestamp(self.now,timezone.utc).isoformat())},
            token=self.token,session_id='TEST_ASSEMBLY')
        with self.tx() as c:
            process_inbox(c,inbox_id=result.inbox_id,lease_token=self.token,ledger=self.ledger,jobs=self.jobs,
                epoch_guard=assert_epoch,admission=self.store.admitted,now=self.now)
            row=c.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?',(self.endpoint.key,)).fetchone()
            evidence=WikiCollectionEvidence(self.endpoint,row['generation'],row['route_epoch'],result.inbox_id,self.now,self.now+60)
            self.assertTrue(evidence.current(c,self.endpoint,self.now))
            self.assertFalse(evidence.current(c,self.endpoint,self.now+60))
            c.execute('UPDATE qq_official_endpoints SET route_epoch=route_epoch+1 WHERE endpoint_key=?',(self.endpoint.key,))
            self.assertFalse(evidence.current(c,self.endpoint,self.now))
