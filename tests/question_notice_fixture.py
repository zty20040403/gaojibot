"""Real first queue claim and synthetic consent; no live QQ/notice delivery."""
from contextlib import contextmanager
import secrets
import psycopg

from src.bot_storage import DatabaseError
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
from tests.conversation_question_fixture import QuestionFixture


class NoticeFixture(QuestionFixture):
    def setUp(self):
        super().setUp()
        self.question = self.insert_question(deadline_at=min(self.now+7200,self.deadline))
        self.notice_job = self.jobs.claim_due('TEST_NOTICE_WORKER',kinds=(account_job_kind('question',self.account.key),),now=self.now)[0]
        with self.tx() as cursor:
            observe_ready(cursor,self.token,{'d':{'user':{'id':'TEST_NOTICE_BOT','bot':True}}},self.now)
            cursor.execute("UPDATE qq_official_endpoints SET proactive_state='allowed' WHERE endpoint_key=?",(self.target.key,))
            cursor.execute("UPDATE qq_official_conversation_questions SET state='running',started_at=? WHERE question_id=?",(self.now,self.question['question_id']))

    @contextmanager
    def pg_error(self,kind=psycopg.Error,pattern=None):
        with self.assertRaises(DatabaseError) as caught:yield caught
        self.assertIsInstance(caught.exception.__cause__,kind)
        if pattern:self.assertRegex(str(caught.exception.__cause__),pattern)

    def notice(self,**changes):
        q = self.question
        account = self.query('SELECT * FROM qq_official_accounts WHERE account_key=?',(self.account.key,))[0]
        endpoint = self.query('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?',(self.target.key,))[0]
        row = dict(notice_id='QN-'+secrets.token_hex(16),question_id=q['question_id'],account_key=self.account.key,
            account_epoch=q['account_epoch'],account_holder_id=self.token.holder_id,target_endpoint_key=q['target_endpoint_key'],
            target_scope_key=q['target_scope_key'],target_generation=q['target_generation'],target_route_epoch=q['target_route_epoch'],
            target_stop_generation=q['target_stop_generation'],target_budget_generation=q['target_budget_generation'],
            capability_revision=endpoint['capability_revision'],job_id=q['job_id'],worker_id=self.notice_job.lease_owner,
            lease_generation=self.notice_job.lease_generation,attempts=self.notice_job.attempts,
            bot_native_id=account['bot_native_id'],bot_identity_source=account['bot_identity_source'],bot_identity_revision=account['bot_identity_revision'],
            content_hmac=secrets.token_hex(32),created_at=self.now,expires_at=min(self.now+30,q['deadline_at']))
        row.update(changes)
        with self.tx() as cursor:
            cursor.execute('INSERT INTO qq_official_question_notice_sources('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
        return dict(self.query('SELECT * FROM qq_official_question_notice_sources WHERE notice_id=?',(row['notice_id'],))[0])
