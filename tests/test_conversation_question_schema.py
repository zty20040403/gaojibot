"""Private question schema fences; no ask tool, target model or live QQ claim."""
from contextlib import contextmanager
import secrets
import psycopg

from src.bot_storage import DatabaseError
from src.plugins.ai_chat.platform_types import AccountKey,ActorKey,EndpointKey
from tests.conversation_question_fixture import QuestionFixture


class ConversationQuestionSchemaTests(QuestionFixture):
    @contextmanager
    def pg_error(self,kind=psycopg.Error,*,pattern=None):
        with self.assertRaises(DatabaseError) as caught:yield caught
        self.assertIsInstance(caught.exception.__cause__,kind)
        if pattern:self.assertRegex(str(caught.exception.__cause__),pattern)

    def test_original_source_private_body_and_queue_only_opaque_id(self):
        row = self.insert_question()
        self.assertEqual((row['state'],row['deadline_at']-row['created_at']),('queued',7200))
        for table in ('messages','qq_official_inbound_events','durable_jobs'):
            self.assertNotIn(row['question'],repr(self.query('SELECT * FROM '+table)))
        job = self.query('SELECT * FROM durable_jobs WHERE job_id=?',(row['job_id'],))[0]
        self.assertEqual((job['max_attempts'],job['resume_on_lease_loss']),(1,0))
        with self.pg_error(psycopg.errors.UniqueViolation):self.insert_question(request_hmac=row['request_hmac'])

    def test_full_account_source_actor_and_direction_are_composite_foreign_keys(self):
        outsider = EndpointKey(self.account,'group','TEST_QUESTION_OUTSIDER'); self.provision(outsider)
        actor = ActorKey(self.account,'group_member',self.endpoint.native_conversation_id,'TEST_OTHER_QUESTION_ACTOR')
        account = AccountKey(self.account.platform,self.account.app_id,'sandbox'); self.provision(EndpointKey(account,'group',self.endpoint.native_conversation_id))
        self.lease(account).acquire()
        for changes,constraint in ((dict(source_actor_key=actor.key),'question_source_execution'),
            (dict(target_endpoint_key=outsider.key),'question_link_identity'),(dict(account_key=account.key),'question_link_identity')):
            with self.subTest(changes=changes):
                with self.pg_error(psycopg.errors.ForeignKeyViolation) as caught:self.insert_question(**changes)
                self.assertEqual(constraint,caught.exception.__cause__.diag.constraint_name)

    def test_receipt_active_link_original_execution_and_queue_proofs_required(self):
        for changes in (dict(link_generation=2),dict(source_scope_key='TEST_FOREIGN_SCOPE'),dict(source_route_epoch=2),
            dict(target_generation=2),dict(account_epoch=self.token.epoch+1),dict(source_execution_revision=2),dict(source_deadline_at=self.deadline+1)):
            with self.subTest(changes=changes),self.pg_error(pattern='question_original_source_required'):self.insert_question(**changes)
        for changes in (dict(max_attempts=2),dict(resume_on_lease_loss=True),dict(scope_key='TEST_OTHER_QUEUE'),
            dict(kind='TEST_OTHER_KIND'),dict(payload={'question':'TEST_DURABLE_BODY'})):
            with self.subTest(changes=changes),self.pg_error(pattern='question_original_source_required'):self.insert_question(queue_changes=changes)
        self.query("UPDATE qq_official_conversation_links SET state='revoked',generation=generation+1,revoked_at=? WHERE link_id=?",(self.now,self.edge['link_id']))
        with self.pg_error(pattern='question_original_source_required'):self.insert_question()

    def test_closed_original_turn_never_admits_question(self):
        self.query("UPDATE qq_official_turn_executions SET state='cancelled' WHERE execution_id=?",(self.handle.execution_id,))
        with self.pg_error(pattern='question_original_source_required'):self.insert_question()

    def test_original_deadline_two_hours_and_single_terminal_consumption(self):
        for changes in (dict(deadline_at=self.now+7201),dict(deadline_at=self.now),dict(question=' '*9),dict(question='x'*8001)):
            with self.subTest(changes=changes),self.pg_error(psycopg.errors.CheckViolation):self.insert_question(**changes)
        row = self.insert_question(); key = row['question_id']
        with self.pg_error(pattern='immutable_conversation_question'):
            self.query("UPDATE qq_official_conversation_questions SET state='answered',answer='TEST_ANSWER',started_at=?,finished_at=? WHERE question_id=?",(self.now,self.now,key))
        self.query("UPDATE qq_official_conversation_questions SET state='running',started_at=? WHERE question_id=?",(self.now,key))
        self.query("UPDATE qq_official_conversation_questions SET state='answered',answer='TEST_ANSWER',finished_at=? WHERE question_id=?",(self.now+1,key))
        self.query('UPDATE qq_official_conversation_questions SET consumed_at=? WHERE question_id=?',(self.now+2,key))
        for statement,args in (("UPDATE qq_official_conversation_questions SET state='running',finished_at=NULL,answer='' WHERE question_id=?",(key,)),
            ("UPDATE qq_official_conversation_questions SET answer='TEST_REPLACED' WHERE question_id=?",(key,)),
            ('UPDATE qq_official_conversation_questions SET consumed_at=? WHERE question_id=?',(self.now+3,key))):
            with self.pg_error(pattern='immutable_conversation_question'):self.query(statement,args)

    def test_metadata_is_immutable_and_cancel_cannot_resurrect_or_be_deleted(self):
        row = self.insert_question(); key = row['question_id']
        for field,value in (('source_stop_generation',1),('target_budget_generation',1),('question','TEST_CHANGED'),
            ('deadline_at',self.now+7199),('source_actor_key','TEST_FORGED_ACTOR'),('link_id',secrets.token_hex(16))):
            with self.subTest(field=field),self.pg_error(pattern='immutable_conversation_question'):
                self.query('UPDATE qq_official_conversation_questions SET '+field+'=? WHERE question_id=?',(value,key))
        self.query("UPDATE qq_official_conversation_questions SET state='cancelled',reason_code='restart',finished_at=? WHERE question_id=?",(self.now,key))
        for statement in ("UPDATE qq_official_conversation_questions SET state='queued',finished_at=NULL,reason_code='' WHERE question_id=?",
            'DELETE FROM qq_official_conversation_questions WHERE question_id=?'):
            with self.pg_error(pattern='immutable_conversation_question'):self.query(statement,(key,))


class ShortSourceQuestionSchemaTests(QuestionFixture):
    source_seconds = 300

    def test_wait_cannot_extend_an_original_task_shorter_than_two_hours(self):
        self.assertEqual(self.insert_question(deadline_at=self.now+300)['deadline_at'],self.now+300)
        with self.assertRaises(DatabaseError) as caught:self.insert_question(deadline_at=self.now+301)
        self.assertIsInstance(caught.exception.__cause__,psycopg.errors.CheckViolation)
