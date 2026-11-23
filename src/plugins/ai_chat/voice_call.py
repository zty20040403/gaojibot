"""Owner-private audio calls. Only the text transcript is saved to chat history."""
import asyncio
import base64
from contextlib import aclosing
from datetime import datetime
from io import BytesIO
import json
import logging
from pathlib import Path
import struct
from uuid import uuid4
import wave

import aiohttp
from fastapi import WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from .llm_gateway import LLMError, completion_profile_scope
from .message_ir import MessageBody, TextNode
from .prompt import PERSONA
from .timeline import is_noise, render_line

TTS_URL = 'wss://openspeech.bytedance.com/api/v3/tts/unidirectional/stream'
CALL_SECONDS = 15 * 60
LOG = logging.getLogger(__name__)


class VoiceError(ValueError):
    """A call failure whose message can be shown to the caller."""


def tts_request(text, speaker):
    body = json.dumps({'user': {'uid': 'gaoji'}, 'req_params': {
        'text': text, 'speaker': speaker, 'audio_params': {'format': 'pcm', 'sample_rate': 24000},
    }}, ensure_ascii=False).encode()
    # V3 unidirectional: FullClientRequest, NoSeq, JSON (no event or session id).
    return b'\x11\x10\x10\x00' + struct.pack('>I', len(body)) + body


def tts_event(frame):
    kind = frame[1] >> 4
    if kind == 0xf:
        code, size = struct.unpack_from('>II', frame, 4)
        raise VoiceError(f'豆包语音错误 {code}：{frame[12:12 + size].decode()}')
    if frame[1] not in (0x94, 0xb4):
        raise VoiceError('豆包语音返回了未知的数据帧')
    event, session_size = struct.unpack_from('>II', frame, 4)
    offset = 12 + session_size
    size, = struct.unpack_from('>I', frame, offset)
    payload = frame[offset + 4:]
    if len(payload) != size:
        raise VoiceError('豆包语音数据帧不完整')
    return event, payload


async def synthesize(settings, text):
    headers = {'X-Api-App-Id': settings.voice_app_id, 'X-Api-Access-Key': settings.voice_access_key,
               'X-Api-Resource-Id': 'seed-tts-2.0', 'X-Api-Request-Id': str(uuid4())}
    async with aiohttp.ClientSession() as client, client.ws_connect(TTS_URL, headers=headers) as ws:
        LOG.info('Doubao TTS X-Tt-Logid: %s', ws._response.headers.get('X-Tt-Logid'))
        await ws.send_bytes(tts_request(text, settings.voice_speaker))
        heard = False
        async for message in ws:
            event, payload = tts_event(message.data)
            if event == 352 and payload:
                heard = True
                yield payload
            elif event == 152:
                result = json.loads(payload)
                if result['status_code'] != 20000000:
                    raise VoiceError(f"豆包语音错误 {result['status_code']}：{result['message']}")
                if not heard:
                    raise VoiceError('豆包语音没有返回音频')
                return
        raise VoiceError('豆包语音连接提前断开')


def audio_part(pcm):
    buffer = BytesIO()
    with wave.open(buffer, 'wb') as wav:
        wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        wav.writeframes(pcm)
    return {'type': 'input_audio', 'input_audio': {
        'data': base64.b64encode(buffer.getvalue()).decode('ascii'), 'format': 'wav'}}


def call_context(app, source):
    persona = app.model_preferences.get_scope_persona(source.endpoint.scope) or app.settings.system_prompt or PERSONA
    prompt = persona + '\n\n这是实时语音通话，用户每次发来的音频就是对你说的话。用简短的中文口语直接回答，不用 Markdown、列表或 emoji。'
    messages = [{'role': 'system', 'content': prompt}]
    recent = app.message_ledger.history(source.scope, after_id=app.conversations.tail_after(source.scope.key),
                                        newest=True, limit=20)
    history = '\n'.join(render_line(row) for row in recent if not is_noise(row))
    if history:
        messages.append({'role': 'user', 'content': '[通话前的私聊记录，仅作背景]\n' + history})
    return messages


class VoiceCall:
    def __init__(self, app, source):
        self.app, self.source = app, source
        self.profile = app.model_catalog.resolve(app.settings.voice_profile)
        if self.profile.protocol != 'openai-chat':
            raise VoiceError('语音通话需要支持音频输入的 OpenAI Chat 模型配置')
        self.started = datetime.now()
        self.turns = []  # In-call user audio and completed assistant replies, in order.

    async def complete(self, messages):
        with completion_profile_scope(frozenset({self.profile.name})):
            result = await self.app.llm_gateway.create_completion(self.profile, messages=messages, stream=False)
        text = result.choices[0].message.content
        if not text or not text.strip():
            raise VoiceError('通话模型没有返回文字')
        return text.strip()

    async def greet(self, ws):
        clip = b''.join([chunk async for chunk in synthesize(self.app.settings, '嗯……')])
        await ws.send_bytes(clip)

    async def reply(self, ws, context, turn):
        text = await self.complete(context + self.turns)
        await ws.send_json({'type': 'reply', 'turn': turn})
        async with aclosing(synthesize(self.app.settings, text)) as chunks:
            async for chunk in chunks:
                await ws.send_bytes(chunk)
        await ws.send_json({'type': 'done', 'turn': turn})
        self.turns.append({'role': 'assistant', 'content': text})

    async def listen(self, ws):
        context = await self.app.db_call(call_context, self.app, self.source)
        incoming = asyncio.create_task(ws.receive())
        reply = asyncio.create_task(self.greet(ws))
        turn = 0
        try:
            while True:
                done, _ = await asyncio.wait([incoming, reply] if reply else [incoming],
                                             return_when=asyncio.FIRST_COMPLETED)
                if reply in done:
                    reply.result()  # Surface synthesis/model errors even if the caller is silent.
                    reply = None
                if incoming not in done:
                    continue
                event = incoming.result()
                if event['type'] == 'websocket.disconnect':
                    return
                if reply and not turn:
                    raise VoiceError('请等待语音通话就绪')
                if reply:
                    reply.cancel()
                    await asyncio.gather(reply, return_exceptions=True)
                    reply = None
                if event.get('bytes') is not None:
                    turn += 1
                    self.turns.append({'role': 'user', 'content': [audio_part(event['bytes'])]})
                    reply = asyncio.create_task(self.reply(ws, context, turn))
                elif json.loads(event['text']) == {'type': 'interrupt'}:
                    # Synthesis can already be done while playback is still queued on the phone.
                    if self.turns and self.turns[-1]['role'] == 'assistant':
                        self.turns.pop()
                else:
                    raise VoiceError('未知通话操作')
                incoming = asyncio.create_task(ws.receive())
        finally:
            tasks = [incoming, reply] if reply else [incoming]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def record_transcript(self):
        if not self.turns:
            return
        ended = datetime.now()
        parts = [msg['content'][0] for msg in self.turns if msg['role'] == 'user']
        lines = [f'[语音通话 {self.started:%H:%M}–{ended:%H:%M}]']
        try:
            transcription = await self.complete([
                {'role': 'system', 'content': '按顺序转写每段用户语音，一段一行，只输出原话，不要编号、角色、解释或空行。听不清的一段写「听不清」。'},
                {'role': 'user', 'content': parts},
            ])
            spoken = [line.strip() for line in transcription.splitlines() if line.strip()]
            if len(spoken) != len(parts):
                raise VoiceError('转写行数与语音段数不符')
        except (LLMError, VoiceError) as exc:
            LOG.error('Voice transcription failed: %s', type(exc).__name__)
            spoken = ['（转写失败）'] * len(parts)
        utterances = iter(spoken)
        for msg in self.turns:
            lines.append('你：' + next(utterances) if msg['role'] == 'user' else 'gaoji：' + msg['content'])
        source = self.source
        await self.app.db_call(self.app.message_ledger.record_message, source.scope,
            native_message_id='voice-call:' + uuid4().hex, actor=source.actor, source_endpoint=source.endpoint,
            sender_native_user_id=source.actor.native_user_id, sender_display=source.sender_display,
            body=MessageBody((TextNode(0, '\n'.join(lines)),)), direction='system', message_kind='system')


def register_voice_call(app, context):
    @app.get('/voice-call', include_in_schema=False)
    async def page():
        return FileResponse(Path(__file__).with_suffix('.html'), headers={
            'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer', 'Permissions-Policy': 'microphone=(self)',
            'Content-Security-Policy': "default-src 'none'; script-src 'unsafe-inline' blob:; worker-src blob:; "
                                       f"style-src 'unsafe-inline'; connect-src {context.settings.admin_origin.replace('https:', 'wss:', 1)}; "
                                       "frame-ancestors 'none'",
        })

    @app.websocket('/voice-call/live')
    async def live(ws: WebSocket):
        if ws.headers.get('origin') != context.settings.admin_origin:
            await ws.close(code=1008)
            return
        await ws.accept()
        call = None
        try:
            async with asyncio.timeout(CALL_SECONDS):
                async with asyncio.timeout(5):
                    hello = await ws.receive_json()
                entry = context.voice_calls.claim(hello['token'])
                if not entry:
                    await ws.close(code=4403, reason='链接已使用或失效')
                    return
                source, _ = entry
                call = VoiceCall(context, source)
                await call.listen(ws)
        except WebSocketDisconnect:
            pass
        except TimeoutError:
            await ws.close(code=4408, reason='语音通话超时')
        except VoiceError as exc:
            await ws.send_json({'type': 'error', 'message': str(exc)})
            await ws.close(code=1011, reason='语音通话出错')
        except Exception as exc:
            LOG.error('Voice call failed: %s', type(exc).__name__)
            await ws.close(code=1011, reason='语音通话连接或处理失败')
        finally:
            if call is not None:
                await call.record_transcript()
