"""SIGKILL a real QQ turn between a durable tool effect and its return to the loop."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import re
import signal
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiohttp import web

from src.bot_storage import PostgresDatabase
from src.bot_storage.disposable import validate_test_database
from tests import test_qq_official_runtime as runtime_fixture
from tests import test_qq_official_turn_endtoend as turn_fixture
from tests.test_qq_official_runtime_sender import verified_ready_platform

ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'TEST_CHECKPOINT_SOURCE'
APP = 'TEST_CHECKPOINT_APP'
FIRST = 'TEST_CHECKPOINT_PROGRESS_ONE'
SECOND = 'TEST_CHECKPOINT_PROGRESS_TWO'
FINAL = 'TEST_CHECKPOINT_FINAL'
IDS = ('TEST_CHECKPOINT_CALL_ONE', 'TEST_CHECKPOINT_CALL_TWO')


async def child(base, schema, phase):
    # The child must never connect to an inherited production DSN.
    dsn = os.environ['TEST_POSTGRES_DSN']
    validate_test_database(dsn)
    if not re.fullmatch(r'gaoji_test_\d+_[0-9a-f]{12}', schema):
        raise ValueError('invalid test schema')
    database = PostgresDatabase(dsn, schema=schema)
    case = turn_fixture.OfficialTurnEndToEndTests('test_command_uses_existing_router_and_never_calls_model')
    case.database, case.logger = database, logging.getLogger('TEST_CHECKPOINT_CHILD')
    app = case.application()
    from src.plugins.ai_chat.adapters.qq_official import turns
    from src.plugins.ai_chat.adapters.qq_official.progress import ProgressEmitter
    from src.plugins.ai_chat.tool_invocation import active_tool_call_id

    calls = 0

    async def completion(**kwargs):
        nonlocal calls
        calls += 1
        results = [m for m in kwargs['messages'] if m.get('role') == 'tool']
        if phase == 'first' and calls == 1 and not results:
            batch = [SimpleNamespace(id=call_id, type='function', function=SimpleNamespace(
                name='say', arguments=json.dumps({'text': text})))
                for call_id, text in zip(IDS, (FIRST, SECOND))]
            return turn_fixture.model_response('', batch, kwargs)
        # Recovery must finish the original pending batch, not start a fresh turn
        # or obtain a new batch from the model.
        if phase != 'second' or [m.get('tool_call_id') for m in results[-2:]] != list(IDS):
            print('TEST unexpected tool results:', repr(results), file=sys.stderr, flush=True)
            raise AssertionError('TEST checkpoint transcript did not restore both tool results')
        return turn_fixture.model_response(FINAL, [], kwargs)

    original_say = ProgressEmitter.say

    async def blocked_say(emitter, text):
        result = await original_say(emitter, text)
        if phase == 'first' and text == FIRST:
            assert active_tool_call_id.get() == IDS[0]
            assert result == {'ok': True, 'status': 'enqueued'}, result
            print('TEST_EFFECT_PERSISTED', flush=True)
            await asyncio.Event().wait()  # Parent sends SIGKILL; this call never returns.
        return result

    with patch.object(turns, 'TURN_LEASE_SECONDS', 2), \
            patch.object(ProgressEmitter, 'say', new=blocked_say), \
            patch('src.plugins.ai_chat.agent_loop._create_completion', new=completion):
        runtime = case.runtime(loaded=runtime_fixture.loaded_config(APP, base=base),
            application=app, ledger=app.message_ledger, journal=app.turn_journal)
        if not await runtime.start():
            raise AssertionError('TEST runtime did not start')
        try:
            await asyncio.to_thread(sys.stdin.readline)
        finally:
            await runtime.stop()
            await app.db_call.close()
            for store in (app.message_ledger, app.turn_journal, app.conversations, app.usage_store):
                store.close()
            database.close()


class TurnCheckpointRecoveryTests(unittest.IsolatedAsyncioTestCase):
    setUp = runtime_fixture.OfficialRuntimeTests.setUp
    query = runtime_fixture.OfficialRuntimeTests.query
    wait_db = runtime_fixture.OfficialRuntimeTests.wait_db
    emit = turn_fixture.OfficialTurnEndToEndTests.emit

    async def test_sigkill_replays_pending_batch_on_same_journal_once(self):
        processes = []
        with TemporaryDirectory(prefix='TEST_checkpoint_') as directory:
            logs = []

            async def start(fake, phase):
                log = open(Path(directory) / (phase + '.log'), 'w+')
                logs.append(log)
                process = await asyncio.create_subprocess_exec(
                    sys.executable, '-m', 'tests.test_turn_checkpoint_recovery', '--child',
                    fake.base_url, self.database.schema, phase, cwd=str(ROOT),
                    env={**os.environ, 'TMPDIR': directory}, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=log)
                processes.append(process)
                return process

            def diagnostics():
                for log in logs:
                    log.flush()
                    log.seek(0)
                return {'children': [(p.pid, p.returncode) for p in processes],
                    'logs': [log.read()[-12000:] for log in logs],
                    'executions': self.query('SELECT execution_id,state,reason_code,journal_turn_id,plan_id FROM qq_official_turn_executions'),
                    'turns': self.query('SELECT turn_id,status,final_text FROM agent_turns'),
                    'events': self.query('SELECT loop_sequence,tool_name,state,result_json FROM turn_journal_events ORDER BY event_id'),
                    'deliveries': self.query('SELECT purpose,status,body_json FROM deliveries')}

            try:
                async with verified_ready_platform() as fake:
                    fake.app_id = APP
                    first = await start(fake, 'first')
                    self.assertIn(await asyncio.wait_for(fake.wait_ready(), 20), (2, 6))
                    await self.emit(fake, native=SOURCE, content='TEST recover a two-step task')
                    marker = await asyncio.wait_for(first.stdout.readline(), 25)
                    self.assertEqual(marker, b'TEST_EFFECT_PERSISTED\n', diagnostics())
                    await self.wait_db("SELECT delivery_id FROM deliveries WHERE purpose='progress' AND status='committed'", timeout=20)
                    self.assertEqual(len(self.query('SELECT * FROM delivery_receipts WHERE applied_at IS NOT NULL')), 1)
                    before = self.query('SELECT execution_id,journal_turn_id,state,job_lease_generation FROM qq_official_turn_executions')
                    self.assertEqual(len(before), 1, diagnostics())
                    self.assertEqual(before[0]['state'], 'executing')
                    journal = before[0]['journal_turn_id']
                    self.assertIsNotNone(journal)
                    self.assertEqual(self.query('SELECT status FROM agent_turns WHERE turn_id=?', (journal,)), [{'status': 'running'}])
                    self.assertEqual(len(fake.received), 1)
                    self.assertEqual(len(self.query("SELECT * FROM delivery_plans WHERE purpose='progress'")), 1)
                    self.assertEqual(self.query("SELECT state FROM turn_journal_events WHERE turn_id=? AND tool_name='say'", (journal,)), [{'state': 'started'}])
                    self.assertIsNone(first.returncode)
                    first.send_signal(signal.SIGKILL)
                    self.assertEqual(await asyncio.wait_for(first.wait(), 10), -signal.SIGKILL)

                    waiting, release = asyncio.Event(), asyncio.Event()
                    send_frame = web.WebSocketResponse.send_json

                    async def hold_ready(socket, frame, *args, **kwargs):
                        if frame.get('t') in {'READY', 'RESUMED'}:
                            waiting.set()
                            await release.wait()
                        return await send_frame(socket, frame, *args, **kwargs)

                    with patch.object(web.WebSocketResponse, 'send_json', hold_ready):
                        second = await start(fake, 'second')
                        try:
                            await asyncio.wait_for(waiting.wait(), 20)
                            self.assertEqual(before, self.query('SELECT execution_id,journal_turn_id,state,job_lease_generation FROM qq_official_turn_executions'))
                            self.assertEqual(len(fake.received), 1)
                        finally:
                            release.set()
                        self.assertIn(await asyncio.wait_for(fake.wait_ready(), 20), (2, 6))
                    try:
                        outcome = await self.wait_db("""SELECT x.state,x.reason_code,d.status FROM qq_official_turn_executions x
                            LEFT JOIN deliveries d ON d.plan_id=x.plan_id AND d.purpose='final'
                            WHERE x.state IN ('cancelled','needs_attention') OR d.status='committed'""", timeout=40)
                    except TimeoutError:
                        self.fail('TEST recovery did not commit final delivery: ' + repr(diagnostics()))
                    self.assertEqual((outcome[0]['state'], outcome[0]['status']), ('handed_off', 'committed'), diagnostics())
                    try:
                        await self.wait_db("SELECT delivery_id FROM deliveries WHERE status='committed'",
                                           predicate=lambda rows: len(rows) >= 3, timeout=20)
                    except TimeoutError:
                        self.fail('TEST recovery did not commit all tool effects: ' + repr(diagnostics()))
                    after = self.query('SELECT execution_id,journal_turn_id,state,plan_id,job_lease_generation,checkpoint_json FROM qq_official_turn_executions')
                    self.assertGreater(after[0]['job_lease_generation'], before[0]['job_lease_generation'])
                    self.assertIsNone(after[0]['checkpoint_json'])
                    self.assertEqual(len(after), 1, diagnostics())
                    self.assertEqual((after[0]['execution_id'], after[0]['journal_turn_id']),
                                     (before[0]['execution_id'], journal))
                    self.assertEqual(self.query('SELECT turn_id,status,final_text FROM agent_turns'),
                                     [{'turn_id': journal, 'status': 'succeeded', 'final_text': FINAL}], diagnostics())
                    deliveries = self.query('SELECT delivery_id,plan_id,purpose,status,native_message_id FROM deliveries ORDER BY delivery_id')
                    self.assertEqual([d['purpose'] for d in deliveries], ['progress', 'progress', 'final'])
                    self.assertTrue(all(d['status'] == 'committed' for d in deliveries))
                    self.assertEqual(deliveries[-1]['plan_id'], after[0]['plan_id'])
                    receipts = self.query('SELECT native_message_id,applied_at FROM delivery_receipts ORDER BY native_message_id')
                    self.assertEqual(len(receipts), 3)
                    self.assertTrue(all(r['applied_at'] is not None for r in receipts))
                    self.assertEqual({r['native_message_id'] for r in receipts}, {d['native_message_id'] for d in deliveries})
                    texts = [r['payload'].get('markdown', {}).get('content', r['payload'].get('content')) for r in fake.received]
                    self.assertEqual(texts[0], FIRST)
                    self.assertCountEqual(texts, [FIRST, SECOND, FINAL])
                    self.assertCountEqual([p['purpose'] for p in self.query('SELECT purpose FROM delivery_plans')],
                                          ['progress', 'progress', 'final'])
                    self.assertEqual(len(self.query('SELECT * FROM turn_send_links WHERE turn_id=?', (journal,))), 1)
                    second.stdin.write(b'stop\n')
                    await second.stdin.drain()
                    self.assertEqual(await asyncio.wait_for(second.wait(), 20), 0, diagnostics())
            finally:
                for process in processes:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
                for log in logs:
                    log.close()


if __name__ == '__main__':
    if len(sys.argv) == 5 and sys.argv[1] == '--child':
        asyncio.run(child(*sys.argv[2:]))
    else:
        unittest.main()
