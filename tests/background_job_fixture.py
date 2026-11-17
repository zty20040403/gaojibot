"""A running private owner background job with real leases, but no live model or VM."""
from src.plugins.ai_chat.background_jobs import BackgroundJobs
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody,TextNode
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from tests.qq_official_outbox_fixture import OutboxFixture


class BackgroundJobFixture(OutboxFixture):
    endpoint_kind = 'private'

    def setUp(self):
        super().setUp()
        ledger = MessageLedger(self.database); self.addCleanup(ledger.close)
        self.queue = DurableJobStore(self.database); self.addCleanup(self.queue.close)
        message = ledger.record_message(self.endpoint.scope,native_message_id='TEST_SOURCE',sender_native_user_id=self.actor.native_user_id,
            sender_display='TEST_OWNER',body=MessageBody((TextNode(0,'TEST request'),)),occurred_at=self.now,actor=self.actor,source_endpoint=self.endpoint)
        source = dict(account_key=self.account.key,endpoint_key=self.endpoint.key,actor_key=self.actor.key,
            scope_key=self.endpoint.scope.key,source_execution_id=None,canonical_message_id=message.canonical_message_id,
            native_message_id='TEST_SOURCE',endpoint_generation=1,route_epoch=1)
        self.jobs = BackgroundJobs(self.queue,clock=lambda:self.now)
        with self.tx() as cursor:
            row = self.jobs.submit(cursor,kind='TEST_AGENT',key='TEST_JOB',source=source,task='TEST',title='TEST',profile='TEST_MODEL')
        self.job = self.queue.claim_due('TEST_WORKER',kinds=('TEST_AGENT',),now=self.now)[0]
        self.jobs.start(row['job_id'])
        self.query('UPDATE durable_jobs SET lease_until=? WHERE job_id=?',(self.now+3600,self.job.job_id))
