import asyncio
import base64
from dataclasses import replace
from io import BytesIO
import json
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch
import wave

from aiohttp import web
from aiohttp.test_utils import TestServer
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.plugins.ai_chat import voice_call as voice
from src.plugins.ai_chat.adapters.qq_official.authority import OwnerAuthority
from src.plugins.ai_chat.adapters.qq_official.progress import ProgressEmitter
from src.plugins.ai_chat.application.turn_context import turn_window
from src.plugins.ai_chat.browser_takeover import TakeoverLinks
from src.plugins.ai_chat.config import Settings
from src.plugins.ai_chat.conversation_window import ConversationStore
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.llm_gateway import LLMGateway
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.model_catalog import ModelCatalog, ModelProfile
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey, InboundMessage, PlatformFeatures
from src.plugins.ai_chat.tools import neutral_registry_for
from src.plugins.ai_chat.tools.state import ToolContext, TurnState
from tests.postgres_fixture import postgres_for_test


ORIGIN = 'https://TEST.invalid'
SETTINGS = Settings.from_env({'AI_ADMIN_ORIGIN': ORIGIN, 'AI_VOICE_APP_ID': 'TEST_APP_ID',
                              'AI_VOICE_ACCESS_KEY': 'TEST_ACCESS_KEY', 'AI_SYSTEM_PROMPT': 'TEST_PERSONA'})
ACCOUNT = AccountKey('qq-official', 'TEST_QQ_APP', 'test')


def inbound(peer='TEST_OWNER', kind='private'):
    endpoint = EndpointKey(ACCOUNT, kind, peer)
    actor = (ActorKey(ACCOUNT, 'c2c_user', '', peer) if kind == 'private' else
             ActorKey(ACCOUNT, 'group_member', peer, 'TEST_MEMBER'))
    return InboundMessage(endpoint, actor, None, 'TEST_NATIVE', 'TEST_NAME', '',
                          MessageBody(()), False, None, 100, None)


def frame(event, data):
    session = b'TEST_SESSION'
    head = b'\x11\xb4\x00\x00' if event == 352 else b'\x11\x94\x10\x00'
    return head + struct.pack('>II', event, len(session)) + session + struct.pack('>I', len(data)) + data


class FrameTests(unittest.TestCase):
    def test_full_client_request_and_server_events(self):
        request = voice.tts_request('你好', 'TEST_VOICE')
        self.assertEqual(request[:4], bytes.fromhex('11 10 10 00'))
        self.assertEqual(int.from_bytes(request[4:8]), len(request[8:]))
        self.assertEqual(json.loads(request[8:]), {'user': {'uid': 'gaoji'}, 'req_params': {
            'text': '你好', 'speaker': 'TEST_VOICE', 'audio_params': {'format': 'pcm', 'sample_rate': 24000}}})
        for event, data in [(352, b'\x00\xff'), (152, b'{"status_code":20000000,"message":"ok"}')]:
            self.assertEqual(voice.tts_event(frame(event, data)), (event, data))
        with self.assertRaisesRegex(voice.VoiceError, '42.*TEST_ERROR'):
            voice.tts_event(bytes.fromhex('11 f0 00 00') + struct.pack('>II', 42, 10) + b'TEST_ERROR')


async def db_call(fn, *args, **kwargs):
    return fn(*args, **kwargs)


class LinkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.links = TakeoverLinks()
        store = MagicMock()
        store.authority = OwnerAuthority(account=ACCOUNT, owner_c2c_ids=('TEST_OWNER',))
        self.app = SimpleNamespace(settings=SETTINGS, official_runtime=SimpleNamespace(store=store),
                                   voice_calls=self.links, db_call=db_call)
        self.sender = Mock(spec=ProgressEmitter, say=AsyncMock(return_value={'ok': True}))

    def tool_context(self, source):
        platform = SimpleNamespace(account=ACCOUNT, features=PlatformFeatures(), progress=self.sender,
                                   progress_enabled=True, files=None)
        return ToolContext(SimpleNamespace(context=self.app), inbound=source, platform=platform)

    async def test_only_current_private_owner_with_credentials_and_https(self):
        for source, settings in [(inbound(), SETTINGS), (inbound('TEST_OTHER'), SETTINGS),
                (inbound('TEST_GROUP', 'group'), SETTINGS), (inbound(), replace(SETTINGS, voice_app_id='')),
                (inbound(), replace(SETTINGS, voice_access_key='')),
                (inbound(), replace(SETTINGS, admin_origin='http://TEST.invalid'))]:
            self.app.settings = settings
            ctx = self.tool_context(source)
            registry = neutral_registry_for(ctx)
            # Listing reads settings only; the owner check runs off the loop when the tool is used.
            listed = settings == SETTINGS and source.endpoint.kind == 'private'
            allowed = listed and source.endpoint == inbound().endpoint
            names = {tool['function']['name'] for tool in registry.definitions(ctx)}
            self.assertEqual('voice_call' in names, listed)
            result = json.loads(await registry.execute(TurnState(ctx), 'voice_call', {}))
            self.assertEqual(result['ok'], allowed)
        self.sender.say.assert_awaited_once()
        link, = self.sender.say.await_args.args
        url, token = link.split('#')
        self.assertEqual(url, ORIGIN + '/voice-call')
        self.assertEqual(self.links.claim(token)[0], inbound())
        self.assertIsNone(self.links.claim(token))
        token = self.links.issue(inbound())
        with patch.object(asyncio.get_running_loop(), 'time', return_value=self.links.pending[token][-1]):
            self.assertIsNone(self.links.claim(token))
        self.assertNotIn('TEST_ACCESS_KEY', repr(SETTINGS))
        self.assertEqual((SETTINGS.voice_speaker, SETTINGS.voice_profile),
                         ('zh_female_vv_uranus_bigtts', 'gemini-3.8-flash-high'))


class CallTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.source = inbound()
        profile = ModelProfile(SETTINGS.voice_profile, 'openai', 'openai-chat', 'TEST_MODEL',
                               'https://TEST.invalid', 'TEST_MODEL_KEY', reasoning_effort='high')
        self.provider = Mock(create_completion=AsyncMock(), close=AsyncMock())
        self.app = SimpleNamespace(settings=SETTINGS, voice_calls=TakeoverLinks(), db_call=db_call,
            model_preferences=Mock(get_scope_persona=Mock(return_value='TEST_CUSTOM_PERSONA')),
            conversations=Mock(tail_after=Mock(return_value=0)), message_ledger=Mock(history=Mock(return_value=[])),
            model_catalog=ModelCatalog({profile.name: profile}, default_profile=profile.name),
            llm_gateway=LLMGateway({'openai-chat': self.provider}))
        self.addAsyncCleanup(self.app.llm_gateway.close)
        self.api = FastAPI()
        voice.register_voice_call(self.api, self.app)
        self.received, self.sent = asyncio.Queue(), asyncio.Queue()
        self.requests = []
        self.tts_closed = asyncio.Event()

        async def synth(request):
            ws = web.WebSocketResponse()
            ws.headers['X-Tt-Logid'] = 'TEST_LOG_ID'
            await ws.prepare(request)
            packet = await ws.receive_bytes()
            text = json.loads(packet[8:])['req_params']['text']
            self.requests.append((text, request.headers))
            await ws.send_bytes(frame(350, b'{}'))
            if text == '嗯……':
                await ws.send_bytes(frame(352, b'\x11\x00'))
            else:
                await ws.send_bytes(frame(352, b'\x22\x00'))
                if text == 'TEST_INTERRUPTED_TTS':
                    await ws.receive()  # Cancellation must close the upstream socket.
                    self.tts_closed.set()
                    return ws
                await ws.send_bytes(frame(352, b'\x33\x00'))
            await ws.send_bytes(frame(351, b'{}'))
            # The real service ends a successful session with {} (checked live); failures carry a status.
            finished = {'status_code': 42, 'message': text} if text == 'TEST_ERROR' else {}
            await ws.send_bytes(frame(152, json.dumps(finished).encode()))
            return ws

        tts = web.Application()
        tts.router.add_get('/stream', synth)
        server = TestServer(tts)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        self.enterContext(patch.object(voice, 'TTS_URL', str(server.make_url('/stream').with_scheme('ws'))))

    def connect(self, token, origin=ORIGIN):
        scope = {'type': 'websocket', 'path': '/voice-call/live', 'headers': [(b'origin', origin.encode())],
                 'query_string': b'', 'scheme': 'wss'}
        self.received.put_nowait({'type': 'websocket.connect'})
        self.send(text=json.dumps({'token': token}))
        return asyncio.create_task(self.api(scope, self.received.get, self.sent.put))

    def send(self, **fields):
        self.received.put_nowait({'type': 'websocket.receive', **fields})

    async def receive(self):
        return await asyncio.wait_for(self.sent.get(), 3)

    async def open(self):
        task = self.connect(self.app.voice_calls.issue(self.source))
        self.assertEqual((await self.receive())['type'], 'websocket.accept')
        self.assertEqual((await self.receive())['bytes'], b'\x11\x00')
        return task

    @staticmethod
    def response(text):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])

    async def test_call_context_cancellation_and_transcript_visible_in_next_turn(self):
        db = postgres_for_test(self)
        ledger = self.app.message_ledger = MessageLedger(db)
        self.addCleanup(ledger.close)
        self.app.conversations = ConversationStore(db)
        self.addCleanup(self.app.conversations.close)
        ledger.record_message(self.source.scope, native_message_id='TEST_PRIOR', body=MessageBody((TextNode(0, 'TEST_HISTORY'),)),
            source_endpoint=self.source.endpoint, actor=self.source.actor, sender_native_user_id=self.source.actor.native_user_id,
            sender_display=self.source.sender_display)
        thinking, cancelled = asyncio.Event(), asyncio.Event()
        replies = iter(['TEST_FIRST', None, 'TEST_INTERRUPTED_TTS', 'TEST_LAST'])
        heard = {1: '一句', 2: '二句', 3: '三句', 4: '四句'}

        async def model(profile, **request):
            content = request['messages'][-1]['content']
            if request['messages'][0]['content'].startswith('逐字转写'):
                self.assertEqual(len(content), 1)
                with wave.open(BytesIO(base64.b64decode(content[0]['input_audio']['data']))) as wav:
                    return self.response(heard[wav.readframes(1)[0]])
            text = next(replies)
            if text is None:
                thinking.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()
            return self.response(text)

        self.provider.create_completion.side_effect = model
        with self.assertLogs(voice.LOG, level='INFO') as logs:
            task = await self.open()
        self.assertIn('TEST_LOG_ID', logs.output[0])
        async with asyncio.timeout(5):
            self.send(bytes=b'\x01\x00')
            self.assertEqual(json.loads((await self.receive())['text']), {'type': 'reply', 'turn': 1})
            self.assertEqual([(await self.receive())['bytes'] for _ in range(2)], [b'\x22\x00', b'\x33\x00'])
            self.assertEqual(json.loads((await self.receive())['text']), {'type': 'done', 'turn': 1})
            self.send(bytes=b'\x02\x00')
            await thinking.wait()
            self.send(text='{"type":"interrupt"}')
            await cancelled.wait()
            self.send(bytes=b'\x03\x00')
            self.assertEqual(json.loads((await self.receive())['text']), {'type': 'reply', 'turn': 3})
            self.assertEqual((await self.receive())['bytes'], b'\x22\x00')
            self.send(bytes=b'\x04\x00')  # New utterance also cancels the unfinished TTS.
            await self.tts_closed.wait()
            self.assertEqual(json.loads((await self.receive())['text']), {'type': 'reply', 'turn': 4})
            self.assertEqual([(await self.receive())['bytes'] for _ in range(2)], [b'\x22\x00', b'\x33\x00'])
            self.assertEqual(json.loads((await self.receive())['text']), {'type': 'done', 'turn': 4})
            self.received.put_nowait({'type': 'websocket.disconnect', 'code': 1000})
            await task

        calls = self.provider.create_completion.await_args_list
        self.assertEqual(len(calls), 8)  # four replies, then one transcription per utterance
        for call in calls:
            self.assertEqual(call.kwargs['reasoning_effort'], 'high')
            self.assertNotIn('tools', call.kwargs)
        messages = calls[3].kwargs['messages']
        self.assertIn('TEST_CUSTOM_PERSONA', messages[0]['content'])
        self.assertIn('TEST_HISTORY', messages[1]['content'])
        self.assertEqual([m['content'] for m in messages if m['role'] == 'assistant'], ['TEST_FIRST'])
        parts = [call.kwargs['messages'][-1]['content'][0] for call in calls[4:]]
        self.assertEqual(parts, [m['content'][0] for m in messages[2:] if m['role'] == 'user'])
        for i, part in enumerate(parts, 1):
            self.assertEqual((part['type'], part['input_audio']['format']), ('input_audio', 'wav'))
            with wave.open(BytesIO(base64.b64decode(part['input_audio']['data']))) as wav:
                self.assertEqual(wav.getparams()[:4], (1, 2, 16000, 1))
                self.assertEqual(wav.readframes(1), bytes([i, 0]))
        self.assertEqual([text for text, _ in self.requests], ['嗯……', 'TEST_FIRST', 'TEST_INTERRUPTED_TTS', 'TEST_LAST'])
        for _, headers in self.requests:
            self.assertEqual([headers['X-Api-' + name] for name in ('App-Id', 'Access-Key', 'Resource-Id')],
                             ['TEST_APP_ID', 'TEST_ACCESS_KEY', 'seed-tts-2.0'])
        self.assertEqual(len({headers['X-Api-Request-Id'] for _, headers in self.requests}), 4)
        prior, transcript = ledger.history(self.source.scope)
        self.assertEqual((transcript.direction, transcript.message_kind), ('system', 'system'))
        self.assertRegex(transcript.rendered_text, r'^\[语音通话 \d\d:\d\d–\d\d:\d\d\]\n你：一句\ngaoji：TEST_FIRST\n你：二句\n你：三句\n你：四句\ngaoji：TEST_LAST$')
        tail, _, _, _ = turn_window(self.app, replace(self.source, native_message_id='TEST_NEXT'), calls[0].args[0])
        self.assertIn(transcript.rendered_text, tail)
        self.app.model_preferences.get_scope_persona.assert_called_once_with(self.source.endpoint.scope)

    async def test_interrupt_during_queued_playback_drops_already_streamed_text(self):
        self.provider.create_completion.side_effect = [self.response('TEST_QUEUED'), self.response('TEST_SPOKEN')]
        task = await self.open()
        self.send(bytes=b'\x01\x00')
        for _ in range(3):  # reply marker, then two chunks
            await self.receive()
        self.assertEqual(json.loads((await self.receive())['text']), {'type': 'done', 'turn': 1})
        self.send(text='{"type":"interrupt"}')
        self.received.put_nowait({'type': 'websocket.disconnect', 'code': 1000})
        await asyncio.wait_for(task, 2)
        body = self.app.message_ledger.record_message.call_args.kwargs['body']
        self.assertEqual(body.nodes[0].text.splitlines()[1:], ['你：TEST_SPOKEN'])

    async def test_one_failed_utterance_does_not_hide_the_others(self):
        call = voice.VoiceCall(self.app, self.source)
        call.turns = [{'role': 'user', 'content': [voice.audio_part(b'\1\0')]},
                      {'role': 'assistant', 'content': 'TEST_REPLY'},
                      {'role': 'user', 'content': [voice.audio_part(b'\2\0')]}]

        async def model(profile, **request):
            part = request['messages'][-1]['content'][0]
            with wave.open(BytesIO(base64.b64decode(part['input_audio']['data']))) as wav:
                return self.response('TEST_ONE\n' if wav.readframes(1)[0] == 1 else '')
        self.provider.create_completion.side_effect = model
        with self.assertLogs(voice.LOG, level='ERROR'):
            await call.record_transcript()
        self.assertEqual(self.provider.create_completion.await_count, 2)
        record = self.app.message_ledger.record_message
        record.assert_called_once()
        self.assertEqual(record.call_args.kwargs['body'].nodes[0].text.splitlines()[1:],
                         ['你：TEST_ONE', 'gaoji：TEST_REPLY', '你：（转写失败）'])

    async def test_origin_link_replay_and_page_headers(self):
        with TestClient(self.api) as client:
            page = client.get('/voice-call')
            self.assertEqual(page.status_code, 200)
            self.assertIn('wss://TEST.invalid', page.headers['content-security-policy'])
            self.assertEqual(page.headers['permissions-policy'], 'microphone=(self)')
        token = self.app.voice_calls.issue(self.source)
        await self.connect(token, 'https://TEST_OTHER.invalid')
        self.assertEqual((await self.receive())['code'], 1008)
        self.received = asyncio.Queue()
        task = self.connect(token)
        await self.receive()
        self.assertIn('bytes', await self.receive())
        self.received.put_nowait({'type': 'websocket.disconnect', 'code': 1000})
        await task
        await self.connect(token)
        await self.receive()
        self.assertEqual((await self.receive())['code'], 4403)
        self.provider.create_completion.assert_not_awaited()
        self.app.message_ledger.record_message.assert_not_called()

    async def test_deadline_closes_before_transcription(self):
        self.assertEqual(voice.CALL_SECONDS, 900)
        cancelled, finish_transcript = asyncio.Event(), asyncio.Event()
        async def model(profile, *, messages, **kwargs):
            if len(self.provider.create_completion.await_args_list) == 1:
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()
            await finish_transcript.wait()
            return self.response('TEST_TRANSCRIPT')
        self.provider.create_completion.side_effect = model
        with patch.object(voice, 'CALL_SECONDS', .35), self.assertLogs(voice.LOG, level='INFO'):
            task = await self.open()
            self.send(bytes=b'\x00\x00')
            self.assertEqual((await self.receive())['code'], 4408)
            self.assertTrue(cancelled.is_set())
            finish_transcript.set()
            await asyncio.wait_for(task, 2)
        self.app.message_ledger.record_message.assert_called_once()

    async def test_tts_failure_ends_call_without_waiting_for_another_utterance(self):
        self.provider.create_completion.side_effect = [self.response('TEST_ERROR'), self.response('TEST_TRANSCRIPT')]
        task = await self.open()
        self.send(bytes=b'\x01\x00')
        await self.receive()  # reply marker and two chunks precede the failed SessionFinished
        await self.receive()
        await self.receive()
        error = json.loads((await self.receive())['text'])
        self.assertEqual(error['type'], 'error')
        self.assertIn('42：TEST_ERROR', error['message'])
        self.assertEqual((await self.receive())['code'], 1011)
        await task
        body = self.app.message_ledger.record_message.call_args.kwargs['body']
        self.assertNotIn('TEST_ERROR', body.nodes[0].text)
