"""Fixture helpers only: real leases/storage and the declared schema head.

No test methods are inherited from execution/artifact test cases. No ad-hoc DDL,
external imports, deployment credentials, or platform I/O are used here.
"""
from pathlib import Path
import hashlib
import tempfile

from tests.qq_official_outbox_fixture import OutboxFixture
from src.plugins.ai_chat.adapters.qq_official.ingest import turn_dispatch_key, turn_dispatch_lane
from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
from src.plugins.ai_chat.adapters.qq_official.execution import begin_execution, capture_result, claim_execution
from src.plugins.ai_chat.adapters.qq_official.file_artifacts import ArtifactStore, foreground_source
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from src.plugins.ai_chat.turn_journal import TurnJournal
from src.plugins.ai_chat.project_blobs import ProjectBlobStore

PDF = b'%PDF-1.4\n% TEST controlled fixture\n%%EOF\n'
KEY = hashlib.sha256(b'TEST_TOOL_CALL').hexdigest()


class ExecutionFixture(OutboxFixture):
    def setUp(self):
        super().setUp()
        self.ledger, self.journal, self.jobs = MessageLedger(self.database), TurnJournal(self.database), DurableJobStore(self.database)
        for store in (self.ledger, self.journal, self.jobs):
            self.addCleanup(store.close)
        self.message = self.ledger.record_message(self.endpoint.scope, native_message_id="TEST_INPUT",
            sender_native_user_id=self.actor.native_user_id, sender_display="test",
            body=MessageBody((TextNode(0, "TEST question"),)), occurred_at=self.now - 5,
            actor=self.actor, source_endpoint=self.endpoint)
        self.inbox = "TEST_INBOX"
        self.kind = account_job_kind("turn", self.account.key)
        with self.tx() as cursor:
            cursor.execute("""INSERT INTO qq_official_inbound_events
                (inbox_id,account_key,endpoint_key,endpoint_generation,route_epoch,event_type,native_message_id,
                 status,echo_state,trigger_requested,canonical_message_id,received_at,created_at,updated_at,turn_text,turn_text_version)
                VALUES (?,?,?,1,1,'GROUP_AT_MESSAGE_CREATE','TEST_INPUT','processed','not_echo',TRUE,?,?,?,?,'TEST question',1)""",
                (self.inbox, self.account.key, self.endpoint.key, self.message.canonical_message_id,
                 self.now - 3, self.now - 3, self.now - 3))
            pending, _ = self.jobs.enqueue(kind=self.kind, payload={"inbox_id": self.inbox},
                scope_key=turn_dispatch_lane(self.account.key, self.endpoint.key),
                idempotency_key=turn_dispatch_key(self.account.key, self.endpoint.key, self.inbox),
                now=self.now, transaction_cursor=cursor)
            cursor.execute("UPDATE qq_official_inbound_events SET dispatch_job_id=? WHERE inbox_id=?", (pending.job_id, self.inbox))
        self.job = self.jobs.claim_due("TEST_WORKER", kinds=(self.kind,), now=self.now)[0]
        self.deadline = self.now + 3600
        self.admitted = True

    def admission(self, *_):
        return self.admitted

    def args(self, handle):
        return dict(token=self.token, handle=handle, job=self.job, now=self.now, admission=self.admission)

    def claim(self, purpose="final", kind="chat"):
        with self.tx() as cursor:
            return claim_execution(cursor, token=self.token, job=self.job, now=self.now,
                deadline_at=self.deadline, admission=self.admission, result_purpose=purpose, execution_kind=kind)

    def begin(self, handle):
        turn = self.journal.start_turn(self.endpoint.scope, trigger_canonical_message_id=self.message.canonical_message_id,
            objective="test", provider="test", model="test", started_at=self.now)
        with self.tx() as cursor:
            begin_execution(cursor, **self.args(handle), journal_turn_id=turn.turn_id)
        return turn.turn_id

    def result(self, handle, turn_id=None, text="TEST final"):
        body = MessageBody((TextNode(0, text),))
        with self.tx() as cursor:
            capture_result(cursor, **self.args(handle), body=body, turn_id=turn_id, status="succeeded")
        return body

    def reclaim_job(self):
        self.now += 301
        self.job = self.jobs.claim_due("TEST_NEW_WORKER", kinds=(self.kind,), now=self.now)[0]


class FileFixture(ExecutionFixture):
    def setUp(self):
        super().setUp()
        for table in ('qq_official_file_artifacts', 'qq_official_file_deliveries',
                      'qq_official_file_publications', 'qq_official_generation_capacity'):
            row = self.query('SELECT to_regclass(?) AS name', (self.database.schema + '.' + table,))[0]
            if row['name'] is None:
                raise RuntimeError('official_file_schema_not_migrated')
        temp = tempfile.TemporaryDirectory(prefix='qq-file-test-')
        self.addCleanup(temp.cleanup)
        self.blobs = ProjectBlobStore(Path(temp.name) / 'private-artifacts')
        self.files = ArtifactStore(transaction=self.tx, jobs=self.jobs, blobs=self.blobs, clock=lambda:self.now)
        self.handle = self.claim().handle
        self.begin(self.handle)
        self.source = self.issue()

    def issue(self):
        with self.tx() as cursor:
            return foreground_source(cursor, token=self.token, handle=self.handle,
                job=self.job, admission=self.admission, now=self.now, pending_deadline_at=self.now + 86400)

    def put(self, **kwargs):
        return self.files.register(self.source, content=PDF, filename='report.pdf', logical_request_key=KEY, **kwargs)
