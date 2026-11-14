"""Real PG origin/consent checks; target gets only verified untrusted data."""
from dataclasses import replace
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official import turn_source
from src.plugins.ai_chat.adapters.qq_official.conversation_control import change
from src.plugins.ai_chat.adapters.qq_official.link_ingress import LinkIngress
from src.plugins.ai_chat.adapters.qq_official.question_authority import QuestionAuthority
from src.plugins.ai_chat.adapters.qq_official.turn_platform import ExecutionBinding
from src.plugins.ai_chat.platform_types import ActorKey
from tests import test_link_authority as authority_fixture
from tests.conversation_question_fixture import QuestionFixture


class QuestionAuthorityTests(QuestionFixture):
    link_approver = 'TEST_QUESTION_LINK_APPROVER'

    def setUp(self):
        super().setUp()
        self.ingress = LinkIngress(self.auth,admission=self.admission); self.authority = QuestionAuthority(self.ingress,admission=self.admission)
        self.binding = ExecutionBinding(turn_source,self.token,self.handle,self.job,self.admission)

    def capture(self,*,binding=None,reference=None,question='TEST_UNTRUSTED_QUESTION'):
        with self.tx() as cursor:return self.authority.capture(cursor,self.token,binding or self.binding,reference or self.edge['link_id'],question)

    def row(self):return self.insert_question(**self.capture())

    def current(self,row):
        with self.tx() as cursor:return self.authority.current(cursor,self.token,row)

    def control(self,endpoint,action):
        with self.tx() as cursor:change(cursor,endpoint,action,now=self.now,actor=self.actor.key,authorized=True)

    def test_regular_caller_proof_and_stable_request_give_target_no_person_capability(self):
        with self.tx() as cursor:self.assertEqual(list(self.auth._credentials(cursor,self.endpoint,self.actor)),[])
        first = self.capture(); self.now += 1; second = self.capture()
        self.assertEqual(first['request_hmac'],second['request_hmac']); row = self.insert_question(**first)
        self.assertEqual(self.current(row),self.target)
        with self.tx() as cursor:data = self.authority.target_input(cursor,self.token,row)
        self.assertEqual(set(data),{'question','source_label'}); self.assertNotIn(self.actor.native_user_id,str(data))
        self.assertNotIn(self.endpoint.native_conversation_id,str(data)); self.assertNotIn('owner',str(data))
        self.result(self.handle); self.now += 701; self.assertEqual(self.current(row),self.target)

    def test_unsigned_schema_rows_are_not_host_issued_capabilities(self):
        row = self.insert_question()
        with self.assertRaisesRegex(PermissionError,'question_source_unavailable'):self.current(row)

    def test_forged_binding_actor_or_physical_scope_never_captures(self):
        actor = ActorKey(self.account,'group_member',self.endpoint.native_conversation_id,'TEST_FORGED_QUESTION_ACTOR')
        for handle in (replace(self.handle,actor=actor),replace(self.handle,endpoint=self.target),replace(self.handle,scope_key='TEST_OTHER_HISTORY')):
            binding = replace(self.binding,handle=handle)
            with self.subTest(handle=handle.execution_id),self.assertRaisesRegex(PermissionError,'question_source_unavailable'):self.capture(binding=binding)
        with self.assertRaisesRegex(PermissionError,'question_link_unavailable'):self.capture(reference='f'*32)
        for value in ('',None,{'role':'owner'},'x'*8001):
            with self.subTest(kind=type(value).__name__),self.assertRaisesRegex(ValueError,'question_request_invalid'):self.capture(question=value)

    def test_hmac_changes_never_allow_query_or_target_evidence(self):
        row = self.row()
        for key,value in (('question','TEST_CHANGED'),('source_actor_key','TEST_FORGED_ACTOR'),('link_id','f'*32)):
            with self.subTest(key=key),self.assertRaisesRegex(PermissionError,'question_source_unavailable'):self.current(row|{key:value})
        with self.assertRaisesRegex(PermissionError,'question_source_unavailable'),self.tx() as cursor:
            self.authority.target_input(cursor,self.token,row|dict(request_hmac='a'*64))

    def stop_case(self,endpoint,reason):
        row = self.row(); self.control(endpoint,'stop')
        with self.assertRaisesRegex(PermissionError,reason):self.current(row)
        self.control(endpoint,'start')
        with self.assertRaisesRegex(PermissionError,reason):self.current(row)

    def test_source_stop_start_never_resumes_original_request(self):
        self.stop_case(self.endpoint,'question_source_unavailable')

    def test_target_stop_start_never_resumes_original_request(self):
        self.stop_case(self.target,'question_target_stopped')

    def test_target_budget_restore_never_authorizes_old_generation(self):
        row = self.row()
        self.query('''INSERT INTO conversation_settings(scope_key,daily_budget_usd,budget_generation) VALUES (?,0,1)
            ON CONFLICT(scope_key) DO UPDATE SET daily_budget_usd=0,budget_generation=conversation_settings.budget_generation+1''',(self.target.scope.key,))
        with self.assertRaisesRegex(PermissionError,'conversation_daily_budget_exceeded'):self.current(row)
        self.query('UPDATE conversation_settings SET daily_budget_usd=NULL,budget_generation=budget_generation+1 WHERE scope_key=?',(self.target.scope.key,))
        with self.assertRaisesRegex(PermissionError,'conversation_daily_budget_replaced'):self.current(row)
        self.assertEqual(self.capture()['target_budget_generation'],2)

    def test_repaired_caller_snapshots_cannot_replace_frozen_budget_or_stop_evidence(self):
        row = self.row(); self.control(self.target,'stop'); self.control(self.target,'start')
        with self.assertRaisesRegex(PermissionError,'question_source_unavailable'):
            self.current(row|dict(target_stop_generation=row['target_stop_generation']+1))
        for key,value in (('created_at',self.now+1),('deadline_at',row['deadline_at']+1),('target_budget_generation',3)):
            with self.subTest(key=key),self.assertRaisesRegex(PermissionError,'question_source_unavailable'):self.current(row|{key:value})

    def test_cancelled_store_result_cannot_be_replaced_by_live_caller_copy(self):
        row = self.row()
        self.query("UPDATE qq_official_conversation_questions SET state='cancelled',reason_code='restart',finished_at=? WHERE question_id=?",(self.now,row['question_id']))
        with self.assertRaisesRegex(PermissionError,'question_source_unavailable'):self.current(row)
        closed = self.query('SELECT * FROM qq_official_conversation_questions WHERE question_id=?',(row['question_id'],))[0]
        with self.assertRaisesRegex(PermissionError,'question_closed'):self.current(closed)

    def test_demotion_regrant_and_route_or_lease_changes_never_restore_request(self):
        row = self.row(); self.now += 1
        with self.tx() as cursor:self.auth.observe(cursor,self.token,authority_fixture.LinkAuthorityTests.message(self,endpoint=self.target,author={'member_openid':self.link_approver,'member_role':'member'}))
        with self.assertRaisesRegex(PermissionError,'question_link_unavailable'):self.current(row)
        self.now += 1
        with self.tx() as cursor:self.auth.observe(cursor,self.token,authority_fixture.LinkAuthorityTests.message(self,endpoint=self.target,author={'member_openid':self.link_approver,'member_role':'admin'}))
        with self.assertRaisesRegex(PermissionError,'question_link_unavailable'):self.current(row)
        self.query('UPDATE qq_official_endpoints SET route_epoch=route_epoch+1 WHERE endpoint_key=?',(self.target.key,))
        with self.assertRaisesRegex(PermissionError,'question_link_unavailable'):self.current(row)
        self.account_lease.close(); self.token = self.lease().acquire()
        with self.assertRaisesRegex(PermissionError,'question_source_unavailable'):self.current(row)

    def test_deadline_crossing_during_permission_lookup_cannot_capture_or_return(self):
        original = self.ingress.management.current
        def crosses_capture(*args):
            result = original(*args); self.now = self.deadline; return result
        with patch.object(self.ingress.management,'current',side_effect=crosses_capture),self.assertRaisesRegex(PermissionError,'question_source_expired'):
            self.capture()
        self.now = self.execution['created_at']; row = self.row()
        def crosses_return(*args):
            result = original(*args); self.now = row['deadline_at']; return result
        with patch.object(self.ingress.management,'current',side_effect=crosses_return),self.assertRaisesRegex(PermissionError,'question_source_expired'):
            self.current(row)

    def test_source_admission_revocation_and_closed_task_deny_before_evidence(self):
        row = self.row(); self.admitted = False
        with self.assertRaisesRegex(PermissionError,'question_source_unavailable'):self.current(row)
        self.admitted = True
        self.query("UPDATE qq_official_turn_executions SET state='cancelled' WHERE execution_id=?",(self.handle.execution_id,))
        with self.assertRaisesRegex(PermissionError,'question_source_unavailable'),self.tx() as cursor:self.authority.target_input(cursor,self.token,row)
