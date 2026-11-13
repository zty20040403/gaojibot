"""Exercise the actual consumer factory, not a substituted command callback."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official import turns
from src.plugins.ai_chat.application.commands import CommandResult
from tests import test_qq_official_turn_consumer as base


class NativeVMCommandTests(unittest.IsolatedAsyncioTestCase):
    setUp = base.ConsumerTests.setUp
    asyncSetUp = base.ConsumerTests.asyncSetUp
    asyncTearDown = base.ConsumerTests.asyncTearDown

    async def exercise(self, text):
        async def command(inbound,parsed,*,authorize):
            with self.f.tx() as cursor:
                row = authorize(cursor)
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
            self.assertEqual(parsed.name,text[1:])
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
            store=SimpleNamespace(admitted=self.f.admission,clock=lambda:self.f.now),deliveries=self.deliveries,
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
        self.assertIn('TEST verified native command',manifest)
        self.agent.assert_not_awaited()

    async def test_units_uses_real_command_lifecycle_authority(self):
        await self.exercise('/units')

    async def test_logins_uses_real_command_lifecycle_authority(self):
        await self.exercise('/logins')
