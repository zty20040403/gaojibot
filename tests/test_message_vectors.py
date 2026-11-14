from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official.conversation_control import change
from src.plugins.ai_chat.adapters.qq_official.lease import LeaseLost
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import CardNode,MediaNode,MessageBody,TextNode,body_to_json
from src.plugins.ai_chat.message_vectors import MessageVectorJobs
from src.plugins.ai_chat.platform_types import AccountKey,ActorKey,EndpointKey
from src.plugins.ai_chat.semantic_recall import PgVectorBackend
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from tests.qq_official_outbox_fixture import OutboxFixture


class MessageVectorTests(OutboxFixture):
    def setUp(self):
        super().setUp()
        self.ledger = MessageLedger(self.database); self.addCleanup(self.ledger.close)
        self.jobs = DurableJobStore(self.database); self.addCleanup(self.jobs.close)
        self.embedder = SimpleNamespace(base_url='https://embedding.example.invalid/v1',model='TEST_EMBED',dimensions=1024)
        self.backend = PgVectorBackend(self.database.dsn,dimensions=1024,schema=self.database.schema)
        self.catalog = self.make_catalog()
        self.vector = [1]+[0]*1023

    def make_catalog(self):
        return MessageVectorJobs(self.database,self.jobs,self.token,self.backend,self.embedder,admission=lambda *_:True,clock=lambda:self.now)

    def message(self,*,endpoint=None,scope=None,body=None,kind='chat',label='TEST_SOURCE'):
        endpoint = endpoint or self.endpoint
        actor = (ActorKey(endpoint.account,'group_member',endpoint.native_conversation_id,'TEST_MEMBER') if endpoint.kind=='group'
            else ActorKey(endpoint.account,'c2c_user','',endpoint.native_conversation_id))
        return self.ledger.record_message(scope or endpoint.scope,native_message_id=label,sender_native_user_id=actor.native_user_id,
            sender_display='TEST_PERSON',actor=actor,source_endpoint=endpoint,body=body or MessageBody((TextNode(0,'TEST slides recommendation'),)),
            occurred_at=self.now,message_kind=kind)

    def claim(self):
        jobs = self.jobs.claim_due('TEST_VECTOR_WORKER',limit=32,kinds=(self.catalog.kind,),now=self.now)
        self.assertEqual(len(jobs),1)
        return jobs[0]

    def succeed(self,job):
        self.assertTrue(self.jobs.mark_succeeded(job.job_id,job.lease_owner,now=self.now,lease_generation=job.lease_generation))

    def test_persistent_hash_jobs_are_idempotent_and_cache_is_not_history_authority(self):
        message = self.message()
        self.assertEqual(len(self.catalog.enqueue(self.endpoint)),1)
        self.assertEqual(self.catalog.enqueue(self.endpoint),[])
        job = self.claim()
        endpoint,document = self.catalog.current(job)
        self.assertEqual(endpoint,self.endpoint); self.assertEqual(document.source_handle,'msg#'+str(message.canonical_message_id))
        self.assertNotIn('TEST slides',str(job.payload))
        self.catalog.save(job,document,self.vector); self.succeed(job)
        self.catalog = self.make_catalog()
        self.assertEqual(self.catalog.enqueue(self.endpoint),[])
        self.assertEqual(len(self.query('SELECT * FROM semantic_documents')),1)
        self.query('UPDATE messages SET rendered_text=? WHERE canonical_message_id=?',('TEST_CORRUPTED_CACHE',message.canonical_message_id))
        self.assertEqual(self.catalog.enqueue(self.endpoint),[])

    def test_hidden_or_changed_canonical_ir_is_denied_and_new_content_is_enqueued(self):
        message = self.message(); self.catalog.enqueue(self.endpoint); job = self.claim()
        _,document = self.catalog.current(job)
        self.query('UPDATE messages SET body_json=? WHERE canonical_message_id=?',
            (body_to_json(MessageBody((TextNode(0,'TEST new content'),))),message.canonical_message_id))
        with self.assertRaises(PermissionError):self.catalog.save(job,document,self.vector)
        self.assertEqual(self.query('SELECT * FROM semantic_documents'),[])
        self.assertEqual(len(self.catalog.enqueue(self.endpoint)),1)
        self.query('UPDATE conversation_visibility SET min_canonical_message_id=?',(message.canonical_message_id+1,))
        with self.assertRaises(PermissionError):self.catalog.current(job)
        self.assertEqual(self.catalog.enqueue(self.endpoint),[])

    def test_stop_start_and_route_replacement_never_reauthorize_old_task(self):
        self.message(); self.catalog.enqueue(self.endpoint); job = self.claim()
        with self.tx() as cursor:
            change(cursor,self.endpoint,'stop',now=self.now,actor=self.actor.key,authorized=True)
            change(cursor,self.endpoint,'start',now=self.now+1,actor=self.actor.key,authorized=True)
        with self.assertRaises(PermissionError):self.catalog.current(job)
        self.assertEqual(len(self.catalog.enqueue(self.endpoint)),1)  # Fresh backfill, not the old task.
        self.query('UPDATE qq_official_endpoints SET route_epoch=route_epoch+1 WHERE endpoint_key=?',(self.endpoint.key,))
        with self.assertRaises(PermissionError):self.catalog.current(job)

    def test_private_admission_and_actual_account_are_rechecked(self):
        private = EndpointKey(self.account,'private','TEST_PRIVATE_VECTOR'); self.provision(private)
        self.message(endpoint=private); self.catalog.enqueue(private); job = self.claim()
        self.catalog.admission = lambda *_:False
        with self.assertRaises(PermissionError):self.catalog.current(job)
        with self.assertRaises(PermissionError):self.catalog.enqueue(private)
        other = EndpointKey(AccountKey('qq-official','TEST_OTHER_ACCOUNT','test'),'group','TEST_GROUP')
        self.provision(other)
        with self.assertRaises(PermissionError):self.catalog.enqueue(other)
        self.assertNotIn(other,self.catalog.endpoints())

    def test_alias_backfill_uses_current_physical_reader_and_canonical_history(self):
        message = self.message(); self.catalog.enqueue(self.endpoint); old = self.claim()
        reader = EndpointKey(self.account,'group','TEST_NEW_READER'); self.provision(reader)
        unused = EndpointKey(self.account,'group','TEST_UNUSED_HISTORY').scope
        self.query('UPDATE qq_official_endpoints SET canonical_scope_key=?,route_epoch=route_epoch+1 WHERE endpoint_key=?',
            (unused.key,self.endpoint.key))
        self.query('UPDATE qq_official_endpoints SET canonical_scope_key=?,route_epoch=route_epoch+1 WHERE endpoint_key=?',
            (self.endpoint.scope.key,reader.key))
        with self.assertRaises(PermissionError):self.catalog.current(old)
        self.assertEqual(len(self.catalog.enqueue(reader)),1)
        current = self.claim(); endpoint,document = self.catalog.current(current)
        self.assertEqual(endpoint,reader); self.assertEqual(document.scope_key,message.scope_key)
        self.catalog.save(current,document,self.vector)

    def test_service_identity_change_overwrites_metadata_even_when_text_and_model_match(self):
        self.message(); self.catalog.enqueue(self.endpoint); job = self.claim()
        _,document = self.catalog.current(job); self.catalog.save(job,document,self.vector); self.succeed(job)
        self.embedder.base_url = 'https://other-embedding.example.invalid/v1'
        self.catalog = self.make_catalog(); self.assertEqual(len(self.catalog.enqueue(self.endpoint)),1)
        job = self.claim(); _,changed = self.catalog.current(job)
        self.assertEqual(document.content_hash,changed.content_hash); self.assertNotEqual(document.metadata,changed.metadata)
        self.catalog.save(job,changed,[0,1]+[0]*1022)
        row = self.query('SELECT metadata_json,embedding::text FROM semantic_documents')[0]
        self.assertEqual(row['metadata_json'],changed.metadata); self.assertTrue(row['embedding'].startswith('[0,1,'))

    def test_failed_expired_and_forged_payloads_do_not_resume_under_old_authorization(self):
        self.message(); self.catalog.enqueue(self.endpoint); job = self.claim()
        forged = replace(job,payload={**job.payload,'revision':'TEST_FORGED'})
        with self.assertRaises(PermissionError):self.catalog.current(forged)
        self.now += 301; self.jobs.recover_expired_leases(now=self.now)
        self.assertEqual(self.jobs.claim_due('TEST_NEW_WORKER',kinds=(self.catalog.kind,),now=self.now),[])
        with self.assertRaises(PermissionError):self.catalog.current(job)
        self.assertEqual(self.catalog.enqueue(self.endpoint),[])

    def test_account_lease_replacement_and_model_change_invalidate_old_task(self):
        self.message(); self.catalog.enqueue(self.endpoint); job = self.claim()
        self.embedder.model = 'TEST_CHANGED_MODEL'
        with self.assertRaises(PermissionError):self.catalog.current(job)
        self.embedder.model = 'TEST_EMBED'
        self.account_lease.close(); self.account_lease = self.lease(); self.token = self.account_lease.acquire()
        with self.assertRaises(LeaseLost):self.catalog.current(job)
        self.catalog = self.make_catalog(); self.assertEqual(len(self.catalog.enqueue(self.endpoint)),1)

    def test_backfill_pages_and_account_queue_are_bounded_before_content_hash_work(self):
        messages = [self.message(label='TEST_PAGE_'+str(index)) for index in range(130)]
        for _ in range(4):self.assertEqual(len(self.catalog.enqueue(self.endpoint,limit=32)),32)
        self.assertEqual(self.catalog.enqueue(self.endpoint),[])
        self.assertEqual(self.query("SELECT count(*) AS n FROM durable_jobs WHERE status='pending'")[0]['n'],128)
        self.query("UPDATE durable_jobs SET status='failed',finished_at=?",(self.now,))
        self.assertEqual(self.catalog.enqueue(self.endpoint,limit=32),[])
        before = self.catalog.page_before[self.endpoint.key]
        self.assertEqual(before,messages[2].canonical_message_id)
        old = self.catalog.enqueue(self.endpoint,limit=32,before=before)
        self.assertEqual({job.payload['message_id'] for job in old},{message.canonical_message_id for message in messages[:2]})

    def test_wrong_dimension_configuration_is_rejected_before_a_paid_request(self):
        self.embedder.dimensions = 1536; self.backend.dimensions = 1536
        with self.assertRaisesRegex(ValueError,'semantic_dimensions_mismatch'):self.make_catalog()

    def test_cards_are_real_content_while_commands_and_tool_noise_do_not_embed(self):
        self.message(body=MessageBody((CardNode(0,title='TEST presentation app',url='https://example.invalid/TEST_SLIDES'),)))
        self.message(kind='command',label='TEST_COMMAND')
        self.message(body=MessageBody((TextNode(0,'⚙ TEST_TOOL_TRACE'),)),label='TEST_NOISE')
        self.assertEqual(len(self.catalog.enqueue(self.endpoint)),2)
        jobs = self.jobs.claim_due('TEST_VECTOR_WORKER',limit=32,kinds=(self.catalog.kind,),now=self.now)
        documents = [self.catalog.current(job)[1] for job in jobs]
        self.assertEqual(sum(bool(document.content) for document in documents),1)
        self.assertIn('TEST presentation app',next(document.content for document in documents if document.content))

    def test_atomic_vector_write_rolls_back_if_outer_transaction_fails(self):
        self.message(); self.catalog.enqueue(self.endpoint); job = self.claim()
        _,document = self.catalog.current(job)
        original = self.backend.upsert
        def fail(*args,**kwargs):original(*args,**kwargs); raise RuntimeError('TEST_ROLLBACK')
        with patch.object(self.backend,'upsert',side_effect=fail),self.assertRaisesRegex(RuntimeError,'TEST_ROLLBACK'):
            self.catalog.save(job,document,self.vector)
        self.assertEqual(self.query('SELECT * FROM semantic_documents'),[])

    def test_media_only_waits_for_caption_and_caption_updates_invalidate_old_vectors(self):
        message = self.message(body=MessageBody((MediaNode(0,'image',source='https://example.invalid/TEST_IMAGE?secret=TEST_SECRET'),)))
        self.catalog.enqueue(self.endpoint); job = self.claim(); _,empty = self.catalog.current(job)
        self.assertEqual(empty.content,''); self.catalog.save(job,empty,[]); self.succeed(job)
        with self.tx() as cursor:
            media = cursor.execute('''INSERT INTO media_blobs(sha256,mime_type,byte_size,storage_path,first_seen_at,last_seen_at)
                VALUES (?,'image/png',10,'TEST_IMAGE',?,?) RETURNING media_id''',('a'*64,self.now,self.now)).fetchone()['media_id']
            cursor.execute('''INSERT INTO media_analysis(media_id,vision_profile,vision_model,summary,description,extracted_text,
                is_sticker,contains_private_info,safety,raw_response_json,content_hash,analyzed_at)
                VALUES (?,'TEST_PROFILE','TEST_MODEL','TEST slideshow tool','','',0,0,'safe','{}','TEST_HASH',?)''',(media,self.now))
            cursor.execute('''INSERT INTO message_media(scope_key,native_message_id,sender_native_user_id,canonical_message_id,segment_index,media_kind,
                source_type,source_url,media_id,fetch_status,created_at,updated_at) VALUES (?,?,?,?,0,'image','official','',?,'ready',?,?)''',
                (message.scope_key,message.native_message_id,message.sender_native_user_id,message.canonical_message_id,media,self.now,self.now))
        self.assertEqual(len(self.catalog.enqueue(self.endpoint)),1)
        job = self.claim(); _,caption = self.catalog.current(job)
        self.assertIn('TEST slideshow tool',caption.content); self.assertNotIn('TEST_SECRET',caption.content)
        self.catalog.save(job,caption,self.vector); self.succeed(job)
        self.query('UPDATE media_analysis SET summary=? WHERE media_id=?',('TEST corrected image caption',media))
        self.assertEqual(len(self.catalog.enqueue(self.endpoint)),1)
