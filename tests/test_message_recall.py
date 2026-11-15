from types import SimpleNamespace

from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody,TextNode,body_to_json
from src.plugins.ai_chat.message_recall import merge_hits,message_hits
from src.plugins.ai_chat.message_vectors import MessageVectorJobs
from src.plugins.ai_chat.platform_types import EndpointKey
from src.plugins.ai_chat.semantic_recall import PgVectorBackend,SemanticDocument
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from tests.qq_official_outbox_fixture import OutboxFixture
from tests import test_message_vectors as base


class MessageRecallTests(OutboxFixture):
    message = base.MessageVectorTests.message

    def setUp(self):
        super().setUp()
        self.ledger = MessageLedger(self.database); self.addCleanup(self.ledger.close)
        self.jobs = DurableJobStore(self.database); self.addCleanup(self.jobs.close)
        self.embedder = SimpleNamespace(base_url='https://embedding.example.invalid/v1',model='TEST_EMBED',dimensions=1024)
        self.backend = PgVectorBackend(self.database.dsn,dimensions=1024,schema=self.database.schema)
        self.catalog = MessageVectorJobs(self.database,self.jobs,self.token,self.backend,self.embedder,
            admission=lambda *_:True,clock=lambda:self.now)
        self.count = 0

    def index(self,text,vector,*,endpoint=None,who='TEST_PERSON'):
        self.count += 1; endpoint = endpoint or self.endpoint
        message = self.message(endpoint=endpoint,label='TEST_RECALL_'+str(self.count),body=MessageBody((TextNode(0,text),)))
        self.query('UPDATE messages SET sender_display=? WHERE canonical_message_id=?',(who,message.canonical_message_id))
        self.catalog.enqueue(endpoint)
        jobs = self.jobs.claim_due('TEST_RECALL_WORKER',kinds=(self.catalog.kind,),now=self.now)
        self.assertEqual(len(jobs),1); job = jobs[0]
        _,document = self.catalog.current(job); self.catalog.save(job,document,vector)
        self.assertTrue(self.jobs.mark_succeeded(job.job_id,job.lease_owner,now=self.now,lease_generation=job.lease_generation))
        return self.ledger.visible_messages_by_ids(endpoint.scope,[message.canonical_message_id])[0]

    def search(self,vector=None,**kwargs):
        with self.tx() as cursor:
            return message_hits(cursor,self.endpoint.scope,vector or [1]+[0]*1023,model=self.embedder.model,
                identity=self.catalog.identity,**kwargs)

    def test_semantic_ranks_paraphrase_and_combined_keyword_result_only_once(self):
        target = self.index('TEST SlidesForge builds presentations',[1]+[0]*1023)
        other = self.index('TEST unrelated weekend trip',[0,1]+[0]*1022)
        hits = self.search(limit=1)
        self.assertEqual([item.canonical_message_id for item in hits],[target.canonical_message_id])
        merged = merge_hits([other,target,target],[target,other],limit=10)
        self.assertEqual(len(merged),2)
        self.assertEqual(len({item.canonical_message_id for item in merged}),2)

    def test_person_and_range_filter_before_vector_limit_not_after_top_hits(self):
        target = self.index('TEST target speaker text',[0,1]+[0]*1022,who='TEST_AL_ICE')
        self.index('TEST nearer wrong speaker',[1]+[0]*1023,who='TEST_ALXICE')
        self.index('TEST nearer outside range',[1]+[0]*1023,who='TEST_AL_ICE')
        result = self.search(who='AL_ICE',after_id=target.canonical_message_id-1,
            before_id=target.canonical_message_id+1,limit=1)
        self.assertEqual([item.canonical_message_id for item in result],[target.canonical_message_id])
        self.assertEqual(self.search(who='%',limit=1),[])

    def test_other_scope_sticker_and_unknown_source_metadata_do_not_enter_candidates(self):
        target = self.index('TEST local evidence',[0,1]+[0]*1022)
        other = EndpointKey(self.account,'group','TEST_OTHER_RECALL'); self.provision(other)
        self.index('TEST secret other conversation',[1]+[0]*1023,endpoint=other)
        self.backend.upsert(SemanticDocument(self.endpoint.scope.key,'sticker','msg#'+str(target.canonical_message_id),
            'TEST sticker metadata',{'embedding_identity':self.catalog.identity}),[1]+[0]*1023,model=self.embedder.model)
        self.assertEqual([item.canonical_message_id for item in self.search()],[target.canonical_message_id])
        self.query("UPDATE semantic_documents SET metadata_json='{}' WHERE source_type='message'")
        self.assertEqual(self.search(),[])

    def test_stale_ir_model_identity_hidden_and_command_rows_never_return_cached_text(self):
        target = self.index('TEST old evidence',[1]+[0]*1023)
        self.query('UPDATE messages SET body_json=? WHERE canonical_message_id=?',
            (body_to_json(MessageBody((TextNode(0,'TEST edited canonical evidence'),))),target.canonical_message_id))
        self.assertEqual(self.search(),[])
        self.assertIn('TEST old evidence',self.query('SELECT content FROM semantic_documents')[0]['content'])
        self.query('UPDATE messages SET body_json=? WHERE canonical_message_id=?',
            (body_to_json(MessageBody((TextNode(0,'TEST old evidence'),))),target.canonical_message_id))
        self.assertEqual(len(self.search()),1)
        self.query("UPDATE semantic_documents SET metadata_json=jsonb_set(metadata_json,'{embedding_identity}','\"TEST_CHANGED_SPACE\"')")
        self.assertEqual(self.search(),[])
        self.query("UPDATE semantic_documents SET metadata_json=jsonb_set(metadata_json,'{embedding_identity}',?::jsonb)",
            ('"'+self.catalog.identity+'"',))
        self.query("UPDATE messages SET message_kind='command'")
        self.assertEqual(self.search(),[])
        self.query("UPDATE messages SET message_kind='chat'")
        self.query('UPDATE conversation_visibility SET min_canonical_message_id=?',(target.canonical_message_id+1,))
        self.assertEqual(self.search(),[])

    def test_wrong_model_and_zero_vector_are_explicit_failures_or_no_candidates(self):
        self.index('TEST visible evidence',[1]+[0]*1023)
        self.embedder.model = 'TEST_OTHER_MODEL'
        self.assertEqual(self.search(),[])
        with self.assertRaisesRegex(ValueError,'semantic_query_vector_zero'):self.search([0]*1024)
