"""Capture action generation before guest I/O and keep read scopes independent."""
import asyncio
import json
import unittest

from starlette.exceptions import HTTPException

from tests import test_vm_credentials as fixture
from tests.qq_official_outbox_fixture import OutboxFixture
from src.plugins.ai_chat.adapters.qq_official.program_actions import ProgramActions
from src.plugins.ai_chat.adapters.qq_official.vm_api import VMAPIAuthority


class VMActionAuthTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    prepare = fixture.VMCredentialTests.prepare
    token_file = fixture.VMCredentialTests.token_file

    async def asyncSetUp(self):
        await fixture.VMCredentialTests.asyncSetUp(self)
        self.current = await self.prepare()
        self.value = self.token_file()
        self.actions = ProgramActions(self.store)
        self.authority = VMAPIAuthority(self.broker,actions=self.actions)
        self.addAsyncCleanup(self.authority.close)

    async def test_stop_start_during_guest_authentication_rejects_old_request(self):
        original = self.manager._guest_exec.side_effect
        async def guest(*args,**kwargs):
            if json.loads(kwargs['input_bytes'])['op']=='inspect':
                self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,FALSE,1) ON CONFLICT DO NOTHING',
                    (self.endpoint.scope.key,))
            return await original(*args,**kwargs)
        self.manager._guest_exec.side_effect = guest
        with self.assertRaises(HTTPException) as caught:
            await self.authority.authorize(self.value,write=True)
        self.assertEqual((caught.exception.status_code,caught.exception.detail),(403,'conversation_stopped_or_replaced'))
        fresh = await self.authority.authorize(self.value,write=True)
        self.assertEqual((fresh.identity,fresh.generation),(self.current,1))

    async def test_stop_cancels_action_scope_but_keeps_api_reads_and_new_generation_separate(self):
        read = await self.authority.authorize(self.value)
        write = await self.authority.authorize(self.value,write=True)
        self.assertIsNot(read,write)
        self.assertIsNone(read.generation)
        self.assertEqual(write.generation,0)
        async def work(scope):
            try:
                await asyncio.Event().wait()
            finally:
                scope.requests.discard(asyncio.current_task())
        reading,writing = asyncio.create_task(work(read)),asyncio.create_task(work(write))
        read.requests.add(reading); write.requests.add(writing)
        self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,TRUE,1)',(self.endpoint.scope.key,))
        result = await asyncio.wait_for(asyncio.gather(writing,return_exceptions=True),3)
        self.assertIsInstance(result[0],asyncio.CancelledError)
        self.assertFalse(reading.done())
        self.assertEqual(write.denial,(403,'conversation_stopped_or_replaced'))
        await read.current()
        count = self.manager._guest_exec.await_count
        with self.assertRaises(HTTPException):
            await self.authority.authorize(self.value,write=True)
        self.assertEqual(self.manager._guest_exec.await_count,count)
        self.query('UPDATE conversation_settings SET stopped=FALSE')
        fresh = await self.authority.authorize(self.value,write=True)
        self.assertIsNot(write,fresh)
        self.assertEqual(fresh.generation,1)
        self.assertEqual(len(self.authority.scopes),2)

    async def test_action_authentication_is_unavailable_without_action_authority(self):
        authority = VMAPIAuthority(self.broker)
        self.addAsyncCleanup(authority.close)
        with self.assertRaises(PermissionError):
            await authority.authorize(self.value,write=True)
        self.assertEqual(authority.scopes,{})
