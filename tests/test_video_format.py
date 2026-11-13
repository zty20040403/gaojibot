"""Synthetic clips only: real ffmpeg preparation and metadata-only host checks."""
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
import subprocess
import json
import httpx
from openai import AsyncOpenAI
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.plugins.ai_chat import sandbox_video
from src.plugins.ai_chat.video_format import duration
from src.plugins.ai_chat.model_catalog import ModelCapabilities, ModelProfile
from src.plugins.ai_chat.model_budget import ModelCallBudget, model_budget_scope
from src.plugins.ai_chat.llm_gateway import LLMGateway, LLMConfigError, LLMUnavailableError, _profile_supports_request


def box(kind, body):
    return (len(body)+8).to_bytes(4,'big') + kind + body


def metadata_clip(seconds=3, track_seconds=None, version=0):
    def header(value):
        return (b'\0'*12 + (1000).to_bytes(4,'big') + int(value*1000).to_bytes(4,'big') if version == 0 else
                b'\1'+b'\0'*19+(1000).to_bytes(4,'big')+int(value*1000).to_bytes(8,'big'))
    movie = box(b'mvhd',header(seconds))
    track = box(b'trak',box(b'mdia',box(b'mdhd',header(seconds if track_seconds is None else track_seconds))))
    return box(b'ftyp',b'isom\0\0\0\0isom') + box(b'moov',movie+track) + box(b'mdat',b'TEST')


class VideoFormatTests(unittest.TestCase):
    def test_real_clip_is_shrunk_in_guest_helper_with_sound_preserved(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'TEST source.mkv'
            subprocess.run(['ffmpeg','-v','error','-nostdin','-f','lavfi','-i','color=c=blue:s=1280x720:r=15',
                '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','3','-c:v','libx264','-c:a','aac',
                '-ac','2',str(source)],check=True,capture_output=True,timeout=30)
            with patch.object(sandbox_video,'WORK_ROOT',root):
                result = sandbox_video.prepare(str(source))
            output = Path(result['path'])
            info = sandbox_video.probe(output)
            video = next(stream for stream in info['streams'] if stream['codec_type']=='video')
            audio = next(stream for stream in info['streams'] if stream['codec_type']=='audio')
            self.assertLessEqual(max(video['width'],video['height']),640)
            self.assertEqual(Fraction(video['avg_frame_rate']),1)
            self.assertEqual(audio['channels'],1)
            self.assertLessEqual(result['byte_size'],sandbox_video.MAX_BYTES)
            self.assertAlmostEqual(duration(output.read_bytes()),3,delta=0.1)
            with patch.object(sandbox_video,'WORK_ROOT',root), self.assertRaises(ValueError):
                sandbox_video.prepare(str(source),output=str(source))

    def test_host_rejects_long_tracks_bad_boxes_and_unsupported_duration_headers(self):
        for version in (0,1):
            self.assertEqual(duration(metadata_clip(600,version=version)),600)
            for content in (metadata_clip(601,version=version),metadata_clip(3,track_seconds=601,version=version)):
                with self.assertRaisesRegex(ValueError,'duration_limit'):
                    duration(content)
        for content in (b'TEST not MP4',metadata_clip()[:-1],b'\0\0\0\1ftyp',
                        box(b'free',b'')*2049,metadata_clip(0)):
            with self.subTest(content=content[:24]), self.assertRaises(ValueError):
                duration(content)

    def test_helper_rejects_long_input_before_conversion_and_paths_outside_work(self):
        with TemporaryDirectory() as temporary, patch.object(sandbox_video,'WORK_ROOT',Path(temporary)):
            source = Path(temporary)/'TEST.mp4'; source.write_bytes(b'TEST')
            with patch.object(sandbox_video,'probe',return_value={'format':{'duration':'601'}}), self.assertRaisesRegex(ValueError,'duration_limit'):
                sandbox_video.prepare(source)
            with self.assertRaises((ValueError,FileNotFoundError)):
                sandbox_video.prepare('/etc/passwd')


class VideoModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_sdk_preserves_video_data_url_over_http(self):
        requests = []
        def transport(request):
            requests.append(request)
            return httpx.Response(200,json={'id':'TEST','created':0,'object':'chat.completion','model':'TEST_MODEL',
                'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'TEST sound and picture'}}],
                'usage':{'prompt_tokens':12,'completion_tokens':8,'total_tokens':20}})
        client = AsyncOpenAI(api_key='TEST_KEY',base_url='https://example.invalid/v1',
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
        self.addAsyncCleanup(client.close)
        selected = ModelProfile('test','openai','openai-chat','TEST_MODEL','https://example.invalid/v1',
            api_key='TEST_KEY',capabilities=ModelCapabilities(video=True))
        gateway = LLMGateway(fallback_enabled=False)
        self.addAsyncCleanup(gateway.close)
        with patch('src.plugins.ai_chat.llm_gateway.OpenAIChatProvider._client',return_value=client):
            result = await gateway.create_completion(selected,messages=[{'role':'user','content':[
                {'type':'video_url','video_url':{'url':'data:video/mp4;base64,TEST'}}]}],stream=False)
        self.assertEqual(result.choices[0].message.content,'TEST sound and picture')
        self.assertEqual(requests[0].url.path,'/v1/chat/completions')
        self.assertEqual(json.loads(requests[0].content)['messages'][0]['content'][0]['video_url']['url'],'data:video/mp4;base64,TEST')

    async def test_only_explicit_chat_video_profiles_accept_video_and_reserve_opaque_input(self):
        calls = []
        class Provider:
            async def create_completion(self,profile,**kwargs):
                calls.append(kwargs)
                return SimpleNamespace(choices=[],usage=SimpleNamespace(prompt_tokens=12,completion_tokens=8))
        profile = ModelProfile('test','openai','openai-chat','TEST_MODEL','https://example.invalid',
            api_key_required=False,max_input_tokens=4096,max_output_tokens=256,
            capabilities=ModelCapabilities.from_mapping('openai-chat',{'video':True}))
        gateway = LLMGateway(providers={'openai-chat':Provider(),'anthropic-messages':Provider()},fallback_enabled=False)
        request = {'messages':[{'role':'user','content':[{'type':'video_url','video_url':{'url':'data:video/mp4;base64,'+'A'*100000}}]}]}
        for bad in (replace(profile,capabilities=ModelCapabilities()),replace(profile,protocol='anthropic-messages')):
            self.assertFalse(_profile_supports_request(bad,request))
            with self.assertRaises((LLMConfigError,LLMUnavailableError)):
                await gateway.create_completion(bad,**request)
        self.assertFalse(calls)
        budget = ModelCallBudget(8192,max_requests=1)
        with model_budget_scope(budget):
            await gateway.create_completion(profile,**request)
        self.assertEqual(len(calls),1)
        self.assertEqual(budget.charged_tokens,20)
