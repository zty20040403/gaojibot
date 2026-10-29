"""No-PG regressions: real planner contract/catalog with synthetic host ports."""
import json
import tempfile
import time
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from src.bot_security.store import SecurityError
from src.plugins.ai_chat.adapters.qq_official import subagent_actions as actions
from src.plugins.ai_chat.adapters.qq_official import subagent_runtime
from src.plugins.ai_chat.adapters.qq_official.host_authority import AllowedService
from src.plugins.ai_chat.adapters.qq_official.host_session import host_coordinator
from src.plugins.ai_chat.adapters.qq_official.host_tools import HOST_READ_TOOLS, HOST_TOOLS, OfficialHostTools
from src.plugins.ai_chat.agent.execution import EntryDecision, TaskContract
from src.plugins.ai_chat.fleet_client import FleetControlClient
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.model_catalog import ModelCatalog, ModelProfile
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey, InboundMessage, BackgroundToolSource
from src.plugins.ai_chat.tool_invocation import active_tool_call_id
from src.plugins.ai_chat.tools.state import ToolContext, TurnState


def identity():
    account = AccountKey('qq-official', 'TEST_HOST_PARITY', 'test')
    endpoint = EndpointKey(account, 'group', 'TEST_GROUP')
    return endpoint, ActorKey(account, 'group_member', endpoint.native_conversation_id, 'TEST_ACTOR')


async def calls(function, *args, **kwargs):
    return function(*args, **kwargs)


@contextmanager
def transaction():
    yield Mock()


class TaskProfileSelectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        endpoint, actor = identity()
        self.handle = SimpleNamespace(endpoint=endpoint, actor=actor, source_native_message_id='TEST_MSG',
            scope_key=endpoint.scope.key, route_epoch=1, canonical_message_id=123, execution_id='TEST_EXECUTION')
        inbound = InboundMessage(endpoint, actor, None, 'TEST_MSG', 'TEST', '',
            MessageBody((TextNode('text', 'TEST task'),)), True, None, 1000, None, route_epoch=1)
        self.turn = SimpleNamespace(context=SimpleNamespace(inbound=inbound, agent_executor=SimpleNamespace(readiness=AsyncMock())))
        self.profile = ModelProfile(name='test', provider='test', protocol='openai-chat', model='test',
            base_url='http://127.0.0.1', api_key_required=False)
        self.coordinator = host_coordinator(Mock(), ModelCatalog({'test': self.profile}, default_profile='test'),
            logger=Mock(), files_enabled=True)
        self.admission = Mock(return_value=True)
        self.binding = actions.SubmissionBinding(object(), SimpleNamespace(assert_execution_current=Mock(return_value={'state':'executing'})),
            self.handle, object(), transaction, calls, object(), self.coordinator, object(),
            1600, clock=lambda:1000, host_admission=self.admission)
        self.submitted = []

    async def queue(self, tools, *, role='analyst', files=True, delivery=False, host=True):
        self.admission.return_value = host
        decision = EntryDecision('delegate', 'execution', 'TEST required tools', '',
            TaskContract('TEST objective', ('TEST result',), (), ('TEST independently check content',), delivery,
                ({'criterion_index':0, 'kind':'evidence'},)),
            ({'id':'work', 'agent':role, 'objective':'TEST objective', 'deliverable':'TEST result',
                'depends_on':[], 'required_tools':list(tools)},))
        async def plan(*args, **kwargs):
            return {**decision.contract.as_payload(), 'mode': decision.mode, 'task_type': decision.task_type,
                'reason': decision.reason, 'answer': '', 'steps': list(decision.steps)}
        def submit(cursor, **kwargs):
            self.submitted.append(kwargs)
            return SimpleNamespace(task_id=7, handle='task#7')
        api = self.binding.actions_for(self.turn, SimpleNamespace(conversation_id='TEST_CONVERSATION'),
            self.profile, SimpleNamespace(turn_context='TEST'), None)
        token = active_tool_call_id.set('TEST_CALL')
        try:
            with patch('src.plugins.ai_chat.tools.owned_sandbox.enabled', return_value=files), \
                    patch.object(self.coordinator, '_supervisor_json', side_effect=plan), \
                    patch.object(actions, 'submit', side_effect=submit):
                await api.delegate(role, 'TEST objective')
        finally:
            active_tool_call_id.reset(token)
        return self.submitted[-1]

    async def test_admitted_admin_file_decision_keeps_file_profile(self):
        result = await self.queue(['sandbox_create', 'sandbox_write_file'], role='document', delivery=True)
        self.assertEqual(result['tool_profile'], 'official-files-v1')
        self.assertIsNone(result['host_admission'])
        self.assertNotIn('operation_prepare', result['decision'].steps[0]['required_tools'])
        self.assertIn('不提供宿主', result['packet'].constraints[0])

    async def test_admitted_admin_plain_task_does_not_gain_host_profile(self):
        result = await self.queue([])
        self.assertEqual(result['tool_profile'], 'official-files-v1')
        self.assertIsNone(result['host_admission'])

    async def test_real_read_and_write_requirements_select_host_profile(self):
        for tool in ('host_inspect', 'operation_prepare'):
            with self.subTest(tool=tool):
                result = await self.queue([tool])
                self.assertEqual(result['tool_profile'], 'official-host-v1')
                self.assertIs(result['host_admission'], self.admission)
        self.assertIs(subagent_runtime.HOST_TOOLS, HOST_TOOLS)

    async def test_host_request_still_requires_current_admission(self):
        with self.assertRaisesRegex(ValueError, 'official_host_profile_not_admitted'):
            await self.queue(['host_inspect'], host=False)
        self.assertFalse(self.submitted)

    async def test_mixed_file_and_host_contract_is_not_silently_downgraded(self):
        with self.assertRaisesRegex(ValueError, 'official_host_file_profile_conflict'):
            await self.queue(['host_inspect'], delivery=True)
        self.assertFalse(self.submitted)

    async def test_without_file_capability_plain_task_stays_text(self):
        result = await self.queue([], files=False)
        self.assertEqual(result['tool_profile'], 'official-text-v1')
        self.assertIsNone(result['host_admission'])


class OfficialFleetReadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        endpoint, actor = identity()
        self.source = SimpleNamespace(endpoint=endpoint, actor=actor, task_id=17, revision=1, scope=endpoint.scope)
        self.live = True
        self.http_calls = []
        self.revoke_on_reply = False
        self.app = SimpleNamespace(db_call=calls, local_model=None,
            model_catalog=SimpleNamespace(profiles=[]), logger=Mock())
        self.authority = SimpleNamespace(services={('testhost','test.service'):AllowedService('testhost','test.service','TEST_BACKEND')},
            _identity=lambda cursor, source:({'username':'TEST_ADMIN'},None), origin=lambda source:'TEST_TRUSTED_ORIGIN')
        def guard(task_id):
            if not self.live or task_id != 17:
                raise SecurityError('TEST source revoked')
            return self.source
        self.bridge = SimpleNamespace(guard=guard, authority=self.authority, transaction_factory=transaction)
        self.temp = tempfile.TemporaryDirectory(prefix='qq-host-read-')
        self.addCleanup(self.temp.cleanup)
        token = Path(self.temp.name)/'token'
        token.write_text('TEST_SYNTHETIC_FLEET_TOKEN_1234567890')
        self.client = FleetControlClient('http://test-fleet.invalid', token)
        await self.client._client.aclose()
        self.client._client = httpx.AsyncClient(transport=httpx.MockTransport(self.respond))
        self.addAsyncCleanup(self.client.close)
        self.tools = OfficialHostTools(bridge=self.bridge, client=self.client, source=self.source)
        metadata = BackgroundToolSource(endpoint, actor, 'subagent', 'task:17', 1)
        context = ToolContext(SimpleNamespace(context=self.app), capabilities=frozenset({'host'}),
            background_source=metadata, platform=SimpleNamespace(account=endpoint.account))
        self.turn = TurnState(context)

    def respond(self, request):
        self.http_calls.append(request)
        if self.revoke_on_reply:
            self.live=False
        now = int(time.time())
        path = request.url.path
        if path == '/v1/fleet':
            payload = {'status':'fresh','expires_at':now+30,
                'inventory':[{'host_id':host} for host in ('testhost','privatehost')],
                'data':{'hosts':[{'host':host,'agent':{'state':'reachable'}} for host in ('testhost','privatehost')]},
                'failed_units':{'status':'fresh','data':{'hosts':[{'host':'testhost','state':'available',
                    'units':[{'unit':'test.service'},{'unit':'private.service'}]}]}}}
        elif path == '/v1/workers':
            payload={'items':[{'worker_id':'test-worker','host_id':'testhost'},
                {'worker_id':'private-worker','host_id':'privatehost'}]}
        elif path == '/v1/resource-policies':
            payload={'items':[]}
        else:
            payload={'status':'fresh','data':{'host':'testhost','facts':{'kernel':'TEST_KERNEL'}}, 'value':'TEST_READ_FACT'}
        return httpx.Response(200,json=payload)

    async def invoke(self, name, arguments):
        return json.loads(await self.tools.run(name,self.turn,arguments))

    async def test_service_logs_use_exact_get_existing_route_and_trusted_principal(self):
        result=await self.invoke('service_logs',{'host_id':'testhost','unit':'test.service','lines':900,'since_seconds':999999})
        self.assertEqual(result['value'],'TEST_READ_FACT')
        request=self.http_calls[0]
        self.assertEqual(request.method,'GET')
        self.assertEqual(str(request.url), 'http://test-fleet.invalid/v1/hosts/testhost/units/test.service/logs?lines=200&since_seconds=86400')
        self.assertEqual(request.headers['X-KC-Actor'],'admin:TEST_ADMIN')
        self.assertEqual(request.headers['X-KC-Origin'],'TEST_TRUSTED_ORIGIN')
        self.assertIsNone(self.turn.context.event)

    async def test_existing_fleet_projection_excludes_unconfigured_hosts_and_units(self):
        result=await self.invoke('fleet_overview',{})
        self.assertEqual([item['host_id'] for item in result['hosts']],['testhost'])
        encoded=json.dumps(result)
        self.assertNotIn('privatehost',encoded)
        self.assertNotIn('private.service',encoded)
        self.assertNotIn('private-worker',encoded)
        self.assertEqual(len(self.http_calls),3)
        self.assertTrue(all(request.method=='GET' for request in self.http_calls))

    async def test_wrong_target_and_model_supplied_identity_never_reach_http(self):
        for args in ({'host_id':'privatehost','unit':'test.service'},
                     {'host_id':'testhost','unit':'private.service'},
                     {'host_id':'testhost','unit':'test.service','actor':'admin:OTHER'}):
            result=await self.invoke('service_inspect',args)
            self.assertFalse(result['ok'])
        self.assertFalse(self.http_calls)

    async def test_revocation_before_or_during_read_withholds_result(self):
        self.live=False
        result=await self.invoke('service_inspect',{'host_id':'testhost','unit':'test.service'})
        self.assertFalse(result['ok']);self.assertFalse(self.http_calls)
        self.live=True;self.revoke_on_reply=True
        result=await self.invoke('service_inspect',{'host_id':'testhost','unit':'test.service'})
        self.assertFalse(result['ok']);self.assertEqual(len(self.http_calls),1)
        self.assertNotIn('TEST_READ_FACT',json.dumps(result))

    async def test_catalog_exposes_reads_only_on_exact_source_and_keeps_mutation_set(self):
        registry=self.tools.registry(self.turn.context)
        names={item['function']['name'] for item in registry.definitions(self.turn.context)}
        self.assertTrue(HOST_READ_TOOLS <= names)
        self.assertTrue({'operation_prepare','operation_status','operation_approve'} <= names)
        self.assertFalse({'ops_call','host_reboot','cluster_job_submit'} & names)
        wrong=replace(self.turn.context,background_source=replace(self.turn.context.background_source,source_revision=2))
        self.assertFalse(HOST_READ_TOOLS & {item['function']['name'] for item in registry.definitions(wrong)})
