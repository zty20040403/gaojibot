"""Synthetic committed consent + real turn/queue metadata; not real QQ proof."""
import hashlib
import secrets
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
from src.plugins.ai_chat.adapters.qq_official.link_authority import LinkAuthority
from src.plugins.ai_chat.adapters.qq_official.link_commands import parse_link_command
from src.plugins.ai_chat.adapters.qq_official.linking import LinkReducer
from src.plugins.ai_chat.platform_types import AccountKey,EndpointKey
from tests import test_link_authority as authority_fixture
from tests.qq_official_turn_fixture import ExecutionFixture


class QuestionFixture(ExecutionFixture):
    def setUp(self):
        super().setUp()
        self.auth = LinkAuthority(b'TEST_QUESTION_AUTHORITY_PEPPER_1234567',clock=lambda:self.now)
        self.reducer = LinkReducer(self.auth); self.target = EndpointKey(self.account,'group','TEST_QUESTION_TARGET'); self.provision(self.target)
        def command(text,endpoint):
            message = authority_fixture.LinkAuthorityTests.message(self,endpoint=endpoint,content=text)
            with self.tx() as cursor:
                self.auth.observe(cursor,self.token,message)
                return self.reducer.apply(cursor,self.token,message,parse_link_command(text))
        with patch('src.plugins.ai_chat.adapters.qq_official.linking.secrets.randbelow',return_value=849231):issued = command('/link',self.endpoint)
        with self.tx() as cursor:self.reducer.record_issue(cursor,self.token,issued.challenge_id,outcome='committed',native_id='TEST_QUESTION_SCHEMA_ACCEPTED',received_at=self.now)
        linked = command('/link '+issued.code,self.target)
        self.edge = dict(self.query('SELECT * FROM qq_official_conversation_links WHERE link_id=?',(linked.link_id,))[0])
        self.deadline = self.now+getattr(self,'source_seconds',10000); self.handle = self.claim().handle; self.begin(self.handle)
        self.execution = dict(self.query('SELECT * FROM qq_official_turn_executions WHERE execution_id=?',(self.handle.execution_id,))[0])

    def insert_question(self,*,queue_changes=None,**changes):
        row = dict(question_id='CQ-'+secrets.token_hex(16),account_key=self.account.key,account_epoch=self.token.epoch,
            link_id=self.edge['link_id'],link_generation=self.edge['generation'],source_actor_key=self.actor.key,
            source_execution_id=self.handle.execution_id,source_execution_revision=self.handle.execution_revision,
            source_stop_generation=0,target_stop_generation=0,target_budget_generation=0,request_hmac=secrets.token_hex(32),
            question='TEST_PRIVATE_QUESTION_ONLY',created_at=self.now,deadline_at=self.now+7200,source_deadline_at=self.deadline)
        for key in ('source_endpoint_key','target_endpoint_key','source_scope_key','target_scope_key','source_generation',
            'source_route_epoch','target_generation','target_route_epoch'):row[key] = self.edge[key]
        row.update(changes)
        options = dict(kind=account_job_kind('question',AccountKey.parse(row['account_key']).key),scope_key='qqq:'+hashlib.sha256(row['target_endpoint_key'].encode()).hexdigest(),
            payload={'question_id':row['question_id']},max_attempts=1,resume_on_lease_loss=False)
        options.update(queue_changes or {})
        with self.tx() as cursor:
            job,_ = self.jobs.enqueue(**options,idempotency_key='TEST_QUESTION_'+secrets.token_hex(16),now=self.now,transaction_cursor=cursor)
            row['job_id'] = job.job_id
            cursor.execute('INSERT INTO qq_official_conversation_questions('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
        return dict(self.query('SELECT * FROM qq_official_conversation_questions WHERE question_id=?',(row['question_id'],))[0])
