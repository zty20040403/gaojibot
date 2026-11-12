"""Actual NDJSON sockets, reconnect cursors and revocation; no live QQ/VM."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from tests import test_vm_reads as read_fixture, test_program_events as event_fixture
from tests.qq_official_outbox_fixture import OutboxFixture


class VMEventHTTPTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    record = event_fixture.ProgramEventTests.record

    async def asyncSetUp(self):
        await read_fixture.VMReadTests.asyncSetUp(self)
        self.ledger = self.app.message_ledger
        self.url = self.base+'/vm/events'
        self.streams = self.lifecycle.http.stream
        patcher = patch('src.plugins.ai_chat.adapters.qq_official.program_stream.POLL_SECONDS',0.01)
        patcher.start(); self.addCleanup(patcher.stop)

    async def line(self, lines):
        return await asyncio.wait_for(anext(lines),3)

    async def released(self):
        async with asyncio.timeout(3):
            while self.streams.active:
                await asyncio.sleep(0.01)

    async def test_backfill_crosses_pages_and_reconnect_has_no_gaps_or_duplicates(self):
        originals = [await self.calls(self.record,f'TEST_{i}') for i in range(105)]
        async with self.client.stream('GET',self.url,params={'after':0}) as response:
            self.assertEqual(response.status_code,200)
            self.assertEqual(int(response.headers['x-gaoji-head']),originals[-1].canonical_message_id)
            lines = response.aiter_lines()
            seen = [json.loads(await self.line(lines))['id'] for _ in originals]
            self.assertEqual(seen,[row.canonical_message_id for row in originals])
            fresh = await self.calls(self.record,'TEST_LIVE')
            self.assertEqual(json.loads(await self.line(lines))['id'],fresh.canonical_message_id)
        await self.released()
        between = await self.calls(self.record,'TEST_DISCONNECTED')
        async with self.client.stream('GET',self.url,params={'after':fresh.canonical_message_id}) as response:
            self.assertEqual(json.loads(await self.line(response.aiter_lines()))['id'],between.canonical_message_id)
        await self.released()

    async def test_new_subscription_starts_at_head_heartbeats_and_reads_while_stopped(self):
        old = await self.calls(self.record,'TEST_OLD')
        with patch('src.plugins.ai_chat.adapters.qq_official.program_stream.HEARTBEAT_SECONDS',0.05):
            async with self.client.stream('GET',self.url) as response:
                self.assertEqual(int(response.headers['x-gaoji-head']),old.canonical_message_id)
                lines = response.aiter_lines()
                self.assertEqual(await self.line(lines),'')
                self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,TRUE,1)',(self.endpoint.scope.key,))
                new = await self.calls(self.record,'TEST_AFTER_STOP')
                value = await self.line(lines)
                while not value:
                    value = await self.line(lines)
                self.assertEqual(json.loads(value)['id'],new.canonical_message_id)
        await self.released()

    async def test_cursor_validation_vm_only_identity_and_subscription_limit(self):
        for suffix in ('?after=-1','?after=1&after=2','?scope=TEST_OTHER','?after=9223372036854775808'):
            response = await self.client.get(self.url+suffix)
            self.assertEqual(response.status_code,400,response.text)
        self.assertEqual((await self.client.get(self.url,params={'after':1})).status_code,409)
        token,_ = self.service.gateway.open(source={'job_id':1},current=AsyncMock())
        self.assertEqual((await self.client.get(self.url,headers={'Authorization':'Bearer '+token})).status_code,401)
        async with self.client.stream('GET',self.url) as first,self.client.stream('GET',self.url) as second:
            self.assertEqual((first.status_code,second.status_code),(200,200))
            excess = await self.client.get(self.url)
            self.assertEqual((excess.status_code,excess.json()['error']),(429,'program_event_streams_busy'))
        await self.released()

    async def test_rotation_breaks_existing_stream_instead_of_returning_successful_eof(self):
        await self.calls(self.record,'TEST_EXISTING')
        async with self.client.stream('GET',self.url,params={'after':0}) as response:
            lines = response.aiter_lines()
            self.assertEqual(json.loads(await self.line(lines))['text'],'TEST_EXISTING')
            self.boot.write_text('00000000-0000-4000-8000-000000000002')
            await self.lifecycle.ready(self.owner,'s123456')
            with self.assertRaises(httpx.RemoteProtocolError):
                await self.line(lines)
        await self.released()
