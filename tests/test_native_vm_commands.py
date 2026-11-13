"""Exercise the actual consumer factory, not a substituted command callback."""
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official import turns
from src.plugins.ai_chat.application.commands import CommandResult
from tests import test_qq_official_turn_consumer as base


class NativeVMCommandTests(unittest.IsolatedAsyncioTestCase):
    setUp = base.ConsumerTests.setUp
    asyncSetUp = base.ConsumerTests.asyncSetUp
    asyncTearDown = base.ConsumerTests.asyncTearDown

    async def exercise(self, text, *, role='', owner=False, denied=False):
        private = self.f.endpoint.kind=='private'
        raw = {'op':0,'t':'C2C_MESSAGE_CREATE' if private else 'GROUP_AT_MESSAGE_CREATE','d':{'id':'TEST_INPUT','content':text,
            'author':{'user_openid' if private else 'member_openid':self.f.actor.native_user_id,'member_role':role},
            'msg_elements':[{'author':{'member_role':'owner'},'content':'TEST untrusted grant'}]}}
        if not private:
            raw['d']['group_openid'] = self.f.endpoint.native_conversation_id
        self.f.query('UPDATE qq_official_inbound_events SET payload_json=?,payload_expires_at=? WHERE inbox_id=?',
            (json.dumps(raw),self.f.now+86400,self.f.inbox))
        invoked = []
        async def command(inbound,parsed,*,authorize,key=None):
            invoked.append(parsed.name)
            with self.f.tx() as cursor:
                if denied:
                    with self.assertRaises(PermissionError):
                        authorize(cursor)
                    return CommandResult('denied','TEST denied native command')
                row = authorize(cursor)
                if parsed.name=='logout':
                    self.assertEqual(key,row['execution_id'])
                    if role=='admin' and not owner:
                        cursor.execute('UPDATE qq_official_inbound_events SET payload_json=NULL WHERE inbox_id=?',(self.f.inbox,))
                        with self.assertRaises(PermissionError):
                            authorize(cursor)
                        cursor.execute('UPDATE qq_official_inbound_events SET payload_json=? WHERE inbox_id=?',(json.dumps(raw),self.f.inbox))
                self.assertEqual(row['state'],'executing')
                original = self.f.now
                self.f.now = row['deadline_at']
                try:
                    with self.assertRaises(turns.LifecycleError):
                        authorize(cursor)
                finally:
                    self.f.now = original
                cursor.execute('UPDATE qq_official_endpoints SET route_epoch=route_epoch+1 WHERE endpoint_key=?',(inbound.endpoint.key,))
                with self.assertRaises(turns.LifecycleError):
                    authorize(cursor)
                cursor.execute('UPDATE qq_official_endpoints SET route_epoch=route_epoch-1 WHERE endpoint_key=?',(inbound.endpoint.key,))
                authorize(cursor)
            self.assertEqual(parsed.name,text[1:].split()[0])
            return CommandResult('ok','TEST verified native command')
        failures = []
        async def guarded(*args,**kwargs):
            try:
                return await command(*args,**kwargs)
            except BaseException as error:
                failures.append(error)
                raise
        provider = SimpleNamespace(command=guarded)
        app = SimpleNamespace(running_tasks=self.tasks,model_preferences=object(),reasoning_preferences=object(),usage_store=None)
        runtime = SimpleNamespace(agent_service=SimpleNamespace(vm_lifecycle=SimpleNamespace(status=provider,browser=provider)),
            store=SimpleNamespace(admitted=self.f.admission,clock=lambda:self.f.now,
                authority=SimpleNamespace(owner_for_actor=lambda *_:object() if owner else None)),deliveries=self.deliveries,
            outbox=self.outbox,journal=self.journal,_calls=self.calls,sender=None,_token=self.f.token,
            _gateway=SimpleNamespace(state='ready'),
            loaded=SimpleNamespace(account=self.f.account,credentials=object(),delivery=SimpleNamespace(turn_policy=lambda:None)))
        with patch.object(turns,'PlatformChatService'),patch.object(turns,'build_platform_chat_orchestrator',return_value=self.orchestrator):
            consumer = turns.build_turn_consumer(runtime,self.f.token,app)
        self.f.query('UPDATE qq_official_inbound_events SET turn_text=?,turn_text_version=1 WHERE inbox_id=?',(text,self.f.inbox))
        self.assertEqual(await consumer(self.f.job),{'status':'handed_off'})
        if failures:
            raise failures[0]
        manifest = self.f.query('SELECT manifest_json FROM delivery_plans')[0]['manifest_json']
        self.assertIn('TEST denied native command' if denied else 'TEST verified native command',manifest)
        self.agent.assert_not_awaited()
        self.assertEqual((await consumer(self.f.job))['status'],'handed_off')
        self.assertEqual(invoked,[text[1:].split()[0]])

    async def test_units_uses_real_command_lifecycle_authority(self):
        await self.exercise('/units')

    async def test_logins_uses_real_command_lifecycle_authority(self):
        await self.exercise('/logins')

    async def test_logout_group_admin_uses_original_transport_role(self):
        await self.exercise('/logout test.example',role='admin')

    async def test_logout_member_and_forwarded_owner_have_no_write_authority(self):
        await self.exercise('/logout test.example',role='member',denied=True)

    async def test_logout_host_owner_does_not_need_fabricated_transport_admin_role(self):
        await self.exercise('/logout test.example',role='member',owner=True)


class PrivateFixture(base.fixture.ExecutionFixture):
    endpoint_kind = 'private'


class PrivateNativeVMCommandTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = NativeVMCommandTests.asyncSetUp
    asyncTearDown = NativeVMCommandTests.asyncTearDown
    exercise = NativeVMCommandTests.exercise

    def setUp(self):
        with patch.object(base.fixture,'ExecutionFixture',PrivateFixture):
            base.ConsumerTests.setUp(self)

    async def test_private_peer_does_not_require_owner_or_group_admin(self):
        await self.exercise('/logout test.example')
