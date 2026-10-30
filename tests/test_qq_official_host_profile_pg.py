"""Actual issued execution/admin binding -> planner -> persisted task/profile/job."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official import execution
from src.plugins.ai_chat.adapters.qq_official.host_session import host_coordinator
from src.plugins.ai_chat.adapters.qq_official.subagent_actions import SubmissionBinding
from src.plugins.ai_chat.adapters.qq_official.subagent_runtime import assert_current, job_kind
from src.plugins.ai_chat.agent.execution import EntryDecision, TaskContract
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.model_catalog import ModelCatalog, ModelProfile
from src.plugins.ai_chat.platform_types import InboundMessage
from src.plugins.ai_chat.subagents import SubAgentStore
from src.plugins.ai_chat.tool_invocation import active_tool_call_id
from tests.qq_official_approval_fixture import ApprovalFixture


class OfficialHostProfilePostgresTests(ApprovalFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls=DatabaseCalls()
        self.tasks=SubAgentStore(self.database)
        self.addCleanup(self.tasks.close)
        self.profile=ModelProfile(name='test',provider='test',protocol='openai-chat',model='test',
            base_url='http://127.0.0.1',api_key_required=False)
        self.coordinator=host_coordinator(self.tasks,ModelCatalog({'test':self.profile},default_profile='test'),
            logger=Mock(),files_enabled=True)
        self.inbound=InboundMessage(self.endpoint,self.actor,None,self.handle.source_native_message_id,
            'TEST','',MessageBody((TextNode(0,'TEST objective'),)),True,None,self.now,None,
            route_epoch=self.handle.route_epoch)
        self.turn=SimpleNamespace(context=SimpleNamespace(inbound=self.inbound,
            agent_executor=SimpleNamespace(readiness=AsyncMock())))
        self.binding=SubmissionBinding(self.token,execution,self.handle,self.job,self.tx,self.calls,
            self.admission,self.coordinator,self.jobs,self.now+120,clock=lambda:self.now,
            host_admission=self.host_admission)

    async def asyncTearDown(self):
        await self.calls.close()

    def host_admission(self,cursor,handle):
        # Actual OwnerGrant + current C2C administrator binding, not a bool stub.
        admin=self.admins.resolve_admin(cursor,endpoint=handle.endpoint,actor=handle.actor)
        return admin.admin_account_id==self.account_a['account_id']

    async def submit_decision(self,role,tools,delivery):
        decision=EntryDecision('delegate','execution','TEST exact requirements','',
            TaskContract('TEST objective',('TEST result',),(),('TEST independent review',),delivery,
                ({'criterion_index':0,'kind':'evidence'},)),
            ({'id':'work','agent':role,'objective':'TEST objective','deliverable':'TEST result',
                'depends_on':[],'required_tools':tools},))
        raw={**decision.contract.as_payload(),'mode':decision.mode,'task_type':decision.task_type,
            'reason':decision.reason,'answer':'','steps':list(decision.steps)}
        actions=self.binding.actions_for(self.turn,SimpleNamespace(conversation_id='TEST_HOST_PROFILE'),
            self.profile,SimpleNamespace(turn_context='TEST'),None)
        mark=active_tool_call_id.set('TEST_PROFILE_'+role)
        try:
            with patch('src.plugins.ai_chat.tools.owned_sandbox.enabled',return_value=True), \
                    patch.object(self.coordinator,'_supervisor_json',new=AsyncMock(return_value=raw)):
                queued=await actions.delegate(role,'TEST objective')
        finally:
            active_tool_call_id.reset(mark)
        task=self.tasks.get(queued['task_id'])
        job=self.jobs.claim_due('TEST_PROFILE_WORKER',kinds=(job_kind(self.account),),now=self.now)[0]
        with self.tx() as cursor:
            binding,restored=assert_current(cursor,token=self.token,task_id=task.task_id,
                now=self.now,admission=self.admission,job=job)
        restored=self.tasks._task_row(restored)
        self.assertEqual(restored.task_id,task.task_id)
        self.assertEqual(restored.endpoint,self.endpoint)
        self.assertEqual(restored.requester_actor,self.actor)
        self.assertIsNone(restored.requester_user_id)
        self.assertEqual(binding['job_id'],job.job_id)
        return binding,restored

    async def test_actual_admin_file_submission_remains_file_after_persistent_restore(self):
        binding,task=await self.submit_decision('document',['sandbox_create','sandbox_write_file'],True)
        self.assertEqual(binding['tool_profile'],'official-files-v1')
        self.assertEqual(self.tasks.control(task.task_id)['dispatch']['tool_profile'],'official-files-v1')
        entry=self.tasks.checkpoints(task.task_id)[0]['state']['entry_decision']
        self.assertTrue(EntryDecision.from_payload(entry).contract.delivery_required)

    async def test_actual_admin_read_submission_is_explicit_host_profile(self):
        binding,task=await self.submit_decision('analyst',['host_inspect'],False)
        self.assertEqual(binding['tool_profile'],'official-host-v1')
        self.assertEqual(self.tasks.control(task.task_id)['dispatch']['tool_profile'],'official-host-v1')
        entry=self.tasks.checkpoints(task.task_id)[0]['state']['entry_decision']
        self.assertEqual(EntryDecision.from_payload(entry).steps[0]['required_tools'],['host_inspect'])
