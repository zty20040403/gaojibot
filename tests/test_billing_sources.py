"""Frozen attribution from real principal/claimed-program rows, no guessed owner."""
from dataclasses import replace
import json
import unittest

from src.bot_storage import DatabaseError
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody,TextNode
from src.plugins.ai_chat.model_billing import BillingSource,ModelBilling
from src.plugins.ai_chat.model_usage import TokenUsage
from src.plugins.ai_chat.adapters.qq_official.background_commands import BackgroundCommands
from src.plugins.ai_chat.adapters.qq_official.program_execution import ProgramExecution
from src.plugins.ai_chat.adapters.qq_official.program_actions import ProgramActions
from src.plugins.ai_chat.adapters.qq_official.program_wakes import ProgramWakes
from src.plugins.ai_chat.adapters.qq_official.vm_sessions import VMSessions
from tests.test_vm_sessions import BOOT
from tests import test_model_billing as billing_fixture
from tests import test_program_wakes as program_fixture
from tests.qq_official_outbox_fixture import OutboxFixture
from tests.test_llm_gateway import profile


class HumanBillingSourcesTests(unittest.TestCase):
    setUp = billing_fixture.ModelBillingTests.setUp
    row = billing_fixture.ModelBillingTests.row

    def test_person_snapshot_is_namespaced_and_survives_later_renaming(self):
        ledger = MessageLedger(self.store.database); self.addCleanup(ledger.close)
        def record(endpoint,actor,display,native):
            return ledger.record_message(endpoint.scope,native_message_id=native,sender_native_user_id=actor.native_user_id,
                sender_display=display,body=MessageBody((TextNode(0,'TEST'),)),occurred_at=self.now,actor=actor,source_endpoint=endpoint)
        original = record(self.endpoint,self.actor,'TEST_ORIGINAL','TEST_SOURCE')
        foreign = replace(self.actor,identity_namespace=self.other.native_conversation_id)
        record(self.other,foreign,'TEST_FOREIGN','TEST_FOREIGN_SOURCE')
        call = self.store.begin(self.source,self.profile)
        saved = json.loads(self.row(call)['source_metadata_json'])
        self.assertEqual(saved,dict(person=original.sender_principal_id,display='TEST_ORIGINAL',programs=[],commands=[]))
        record(self.endpoint,self.actor,'TEST_RENAMED','TEST_LATER_SOURCE')
        self.store.finish(call,TokenUsage(10,2,0,0))
        self.assertEqual(json.loads(self.row(call)['source_metadata_json']),saved)
        self.assertNotIn(self.actor.native_user_id,json.dumps(saved))
        with self.assertRaises(DatabaseError),self.store.transaction() as cursor:
            cursor.execute("UPDATE model_usage_calls SET source_metadata_json='{}' WHERE call_id=?",(call,))

    def test_missing_principal_is_explicit_and_pending_metadata_cannot_be_reassigned(self):
        call = self.store.begin(self.source,self.profile)
        saved = json.loads(self.row(call)['source_metadata_json'])
        self.assertEqual(saved,dict(person=None,display=None,programs=[],commands=[]))
        with self.assertRaises(DatabaseError),self.store.transaction() as cursor:
            cursor.execute("UPDATE model_usage_calls SET source_metadata_json='{}' WHERE call_id=?",(call,))
        self.assertIsNone(self.row(call)['finished_at'])


class ProgramBillingSourcesTests(OutboxFixture):
    submit = program_fixture.ProgramWakeTests.submit
    limit = program_fixture.ProgramWakeTests.limit

    def setUp(self):
        super().setUp()
        self.sessions = VMSessions(self.tx,self.token,private_admission=lambda *_:True,clock=lambda:self.now)
        self.current = self.sessions.issue(self.endpoint,'s123456',BOOT,expected_route=(1,1)).source
        self.actions = ProgramActions(self.sessions)
        self.wakes = ProgramWakes(self.actions)

    def batch(self):
        self.limit(1); self.submit(0)
        first,_ = self.submit(1,program='TEST_A')
        self.submit(2,program='TEST_B'); self.submit(3,program='TEST_A')
        self.now = (self.now//3600+1)*3600
        run = ProgramExecution(self.wakes).claim(first['wake_id'])
        self.assertIsNotNone(run)
        return run

    def begin(self,run):
        store = ModelBilling(self.database,clock=lambda:self.now)
        call = store.begin(BillingSource(self.endpoint,'program_wake',program_wake_id=run['wake_id']),profile('test'))
        saved = json.loads(self.query('SELECT source_metadata_json FROM model_usage_calls WHERE call_id=?',(call,))[0]['source_metadata_json'])
        return call,saved

    def test_coalesced_programs_are_one_joint_origin_without_human_or_reply_name(self):
        run = self.batch()
        reply,_ = self.actions.reserve(self.current,generation=0,program='TEST_REPLY',request_id='TEST_REPLY',kind='say',payload={'text':'TEST'})
        self.query('UPDATE qq_official_program_actions SET wake_id=? WHERE action_id=?',(run['wake_id'],reply['action_id']))
        call,saved = self.begin(run)
        self.assertEqual(saved,dict(person=None,display=None,programs=['TEST_A','TEST_B'],commands=[]))
        self.assertEqual(len(self.query('SELECT * FROM model_usage_calls')),1)
        # Current source records can change later; the already-started receipt
        # must still identify its original participants, not the latest name.
        self.query("UPDATE qq_official_program_actions SET program_name='TEST_RENAMED' WHERE wake_id=?",(run['wake_id'],))
        self.assertEqual(json.loads(self.query('SELECT source_metadata_json FROM model_usage_calls WHERE call_id=?',(call,))[0]['source_metadata_json']),saved)

    def test_command_completion_origin_is_not_charged_as_a_resident_program(self):
        run = self.batch()
        command,_ = BackgroundCommands(self.sessions).reserve(self.current,generation=(0,0),program='TEST_A',
            request_id='TEST_CMD',argv=['printf','TEST'])
        # Extraction is not command authorization: the production current()
        # fence separately requires the command's confirmed completion.
        self.query('UPDATE qq_official_program_actions SET source_command_id=? WHERE action_id=?',
            (command['command_id'],json.loads(run['input_ids_json'])[0]))
        _,saved = self.begin(run)
        self.assertEqual(saved['commands'],[command['command_id']])
        self.assertIsNone(saved['person'])
        self.assertNotIn(self.current.token_hash,json.dumps(saved))
