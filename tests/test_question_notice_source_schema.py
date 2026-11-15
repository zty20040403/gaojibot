"""Proposal-only SQL fences; no say tool, public delivery or live permission claim."""
import secrets
import psycopg

from src.plugins.ai_chat.platform_types import AccountKey,EndpointKey
from tests.question_notice_fixture import NoticeFixture


class QuestionNoticeSourceTests(NoticeFixture):
    def test_one_body_free_source_for_the_original_question_never_sends(self):
        row = self.notice()
        self.assertEqual((row['job_id'],row['lease_generation'],row['attempts']),(self.question['job_id'],1,1))
        self.assertEqual(row['bot_native_id'],'TEST_NOTICE_BOT')
        self.assertNotIn(self.question['question'],repr(row))
        self.assertEqual(self.query('SELECT * FROM delivery_plans'),[])
        self.assertEqual(self.query('SELECT * FROM deliveries'),[])
        with self.pg_error(psycopg.errors.UniqueViolation):self.notice()
        self.assertEqual(len(self.query('SELECT * FROM qq_official_question_notice_sources')),1)

    def test_bot_identity_cannot_be_claimed_from_a_human_actor(self):
        with self.pg_error(pattern='question_notice_original_source_required'):
            self.notice(bot_native_id=self.actor.native_user_id)

    def test_real_bot_worker_epoch_and_frozen_target_snapshots_are_required(self):
        for changes in (dict(bot_native_id=self.actor.native_user_id),dict(bot_identity_revision=2),dict(worker_id='TEST_OTHER_WORKER'),
            dict(account_holder_id='TEST_FORGED_HOLDER'),dict(account_epoch=self.token.epoch+1),dict(target_generation=2),
            dict(target_route_epoch=2),dict(target_scope_key=self.endpoint.scope.key),dict(target_stop_generation=1),
            dict(target_budget_generation=1),dict(capability_revision=999),dict(job_id=self.job.job_id)):
            with self.subTest(changes=changes),self.pg_error(pattern='question_notice_original_source_required'):self.notice(**changes)
        for changes in (dict(lease_generation=2),dict(attempts=2),dict(bot_identity_source='model'),dict(content_hmac='TEST_UNKEYED_BODY')):
            with self.subTest(changes=changes),self.pg_error():self.notice(**changes)

    def test_unknown_denied_or_changed_target_route_is_not_a_proactive_probe(self):
        for state in ('unknown','denied'):
            self.query('UPDATE qq_official_endpoints SET proactive_state=? WHERE endpoint_key=?',(state,self.target.key))
            with self.subTest(state=state),self.pg_error(pattern='question_notice_original_source_required'):self.notice()
        self.query("UPDATE qq_official_endpoints SET proactive_state='allowed',generation=generation+1 WHERE endpoint_key=?",(self.target.key,))
        with self.pg_error(pattern='question_notice_original_source_required'):self.notice()

    def test_closed_question_is_not_reauthorized_by_a_still_running_job(self):
        self.query("UPDATE qq_official_conversation_questions SET state='cancelled',reason_code='cancelled',finished_at=? WHERE question_id=?",
            (self.now,self.question['question_id']))
        with self.pg_error(pattern='question_notice_original_source_required'):self.notice()

    def test_actual_first_job_lease_must_still_be_current(self):
        self.now += self.jobs.lease_seconds
        with self.pg_error(pattern='question_notice_original_source_required'):self.notice()
        self.assertEqual(self.query('SELECT * FROM qq_official_question_notice_sources'),[])

    def test_intent_is_immutable_and_cannot_be_deleted_then_replayed(self):
        row = self.notice()
        for field,value in (('content_hmac',secrets.token_hex(32)),('created_at',self.now+1),('expires_at',self.now+31),
            ('worker_id','TEST_OTHER_WORKER'),('bot_native_id','TEST_OTHER_BOT'),('target_budget_generation',1)):
            with self.subTest(field=field),self.pg_error(pattern='immutable_question_notice_source'):
                self.query('UPDATE qq_official_question_notice_sources SET '+field+'=? WHERE notice_id=?',(value,row['notice_id']))
        with self.pg_error(pattern='immutable_question_notice_source'):
            self.query('DELETE FROM qq_official_question_notice_sources WHERE notice_id=?',(row['notice_id'],))
        self.assertEqual(self.query('SELECT * FROM qq_official_question_notice_sources'),[row])

    def test_owned_workspace_reset_reenables_the_statement_guard(self):
        self.notice()
        workspace = type(self).__dict__['_owned_postgres_fixture']
        workspace.reset()
        self.assertEqual(self.query('SELECT * FROM qq_official_question_notice_sources'),[])
        for mode,enabled in (('ENABLE ALWAYS','A'),('ENABLE REPLICA','R'),('DISABLE','D')):
            self.query(f'ALTER TABLE qq_official_question_notice_sources {mode} TRIGGER protect_question_notice_truncate')
            workspace.reset()
            self.assertEqual(self.query("SELECT tgenabled FROM pg_trigger WHERE tgrelid='qq_official_question_notice_sources'::regclass AND tgname='protect_question_notice_truncate'")[0]['tgenabled'],enabled)
        self.query('ALTER TABLE qq_official_question_notice_sources ENABLE TRIGGER protect_question_notice_truncate')
        with self.pg_error(pattern='immutable_question_notice_source'):
            self.query('TRUNCATE qq_official_question_notice_sources')

    def test_truncate_cannot_erase_the_once_only_proposal_and_replay_it(self):
        row = self.notice()
        with self.pg_error(pattern='immutable_question_notice_source'):
            self.query('TRUNCATE qq_official_question_notice_sources')
        self.assertEqual(self.query('SELECT * FROM qq_official_question_notice_sources'),[row])

    def test_other_real_first_job_cannot_borrow_the_original_question_id(self):
        other,_ = self.jobs.enqueue(kind=self.notice_job.kind,scope_key=self.notice_job.scope_key,
            payload={'question_id':self.question['question_id']},idempotency_key='TEST_OTHER_NOTICE_JOB_'+secrets.token_hex(16),
            max_attempts=1,resume_on_lease_loss=False,now=self.now)
        claimed = self.jobs.claim_due('TEST_OTHER_NOTICE_WORKER',kinds=(other.kind,),now=self.now)[0]
        self.assertEqual(claimed.job_id,other.job_id)
        self.assertEqual((claimed.lease_generation,claimed.attempts),(1,1))
        with self.pg_error(psycopg.errors.ForeignKeyViolation) as caught:
            self.notice(job_id=claimed.job_id,worker_id=claimed.lease_owner)
        self.assertEqual(caught.exception.__cause__.diag.constraint_name,'question_notice_target')

    def test_complete_account_and_physical_question_identity_cannot_be_aliased(self):
        other = EndpointKey(AccountKey(self.account.platform,self.account.app_id,'sandbox'),'group',self.target.native_conversation_id)
        self.provision(other)
        with self.pg_error():self.notice(account_key=other.account.key,target_endpoint_key=other.key,target_scope_key=other.scope.key)
        with self.pg_error():self.notice(target_endpoint_key=self.endpoint.key)
        definition = self.query("SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint WHERE conrelid='qq_official_question_notice_sources'::regclass AND conname='question_notice_target'")[0]['definition']
        self.assertIn('FOREIGN KEY (question_id, account_key, target_endpoint_key, job_id)',definition)

    def test_thirty_seconds_is_a_fixed_source_deadline_not_a_retry_window(self):
        with self.pg_error(psycopg.errors.CheckViolation):self.notice(expires_at=self.now+31)
        self.assertEqual(self.notice()['expires_at'],self.now+30)

    def test_zero_lifetime_is_not_a_valid_notice_window(self):
        with self.pg_error(psycopg.errors.CheckViolation):self.notice(expires_at=self.now)


class PrivateQuestionNoticeSourceTests(NoticeFixture):
    target_kind = 'private'

    def test_private_question_cannot_borrow_a_group_or_peer_send_identity(self):
        self.assertEqual(self.question['target_endpoint_key'],self.target.key)
        self.assertEqual(self.notice_job.attempts,1)
        with self.pg_error(pattern='question_notice_original_source_required'):self.notice()


class ShortQuestionNoticeSourceTests(NoticeFixture):
    source_seconds = 10

    def test_proposal_cannot_extend_the_original_source_task_deadline(self):
        with self.pg_error(pattern='question_notice_original_source_required'):self.notice(expires_at=self.now+11)
        self.assertEqual(self.notice()['expires_at'],self.question['deadline_at'])
