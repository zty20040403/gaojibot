"""Synthetic clips only: real ffmpeg preparation and metadata-only host checks."""
from dataclasses import replace
from fractions import Fraction
import io
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
    def runner(self, root, source, **arguments):
        runner = Path(sandbox_video.__file__).with_name('sandbox_runner.js')
        response = subprocess.run(['node',str(runner),'--video',json.dumps(
            dict(work=str(root),path='/work/'+source.name,**arguments))],capture_output=True,timeout=30)
        return response.returncode, json.loads(response.stdout)

    def test_real_clips_keep_video_packets_and_sound_in_both_guest_tools(self):
        cases = [('normal','1280x720:r=15:d=3',True), ('short','32x32:r=25:d=0.08',False),
                 ('short_audio','32x32:r=25:d=0.08',True), ('one_pixel','2x2:r=1:d=1,format=rgb24,crop=1:1',False)]
        with TemporaryDirectory() as temporary, patch.object(sandbox_video,'WORK_ROOT',Path(temporary)):
            root = Path(temporary)
            for name, picture, sound in cases:
                source = root / ('TEST source '+name+('.mp4' if name=='normal' else '.mkv'))
                subprocess.run(['ffmpeg','-v','error','-nostdin','-f','lavfi','-i','color=c=blue:s='+picture,
                    *(['-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration='+('3' if name=='normal' else '0.08'),
                       '-c:a','aac','-ac','2'] if sound else []), '-c:v','libx264rgb' if name=='one_pixel' else 'libx264',str(source)],
                    check=True,capture_output=True,timeout=30)
                code, result = self.runner(root,source,**({'start':0,'end':3} if name=='normal' else {}))
                self.assertEqual(code,0,result)
                self.assertTrue(result['ok'],result)
                self.assertEqual(result['value']['seconds'],3 if name=='normal' else 1)
                outputs = [root/result['value']['path'].removeprefix('/work/'), Path(sandbox_video.prepare(source)['path'])]
                for output in outputs:
                    with self.subTest(clip=name,output=output.name):
                        info = json.loads(subprocess.run(['ffprobe','-v','error','-count_packets','-show_streams','-of','json',str(output)],
                            capture_output=True,check=True,timeout=20).stdout)
                        video = next(stream for stream in info['streams'] if stream['codec_type']=='video')
                        self.assertGreater(int(video['nb_read_packets']),0)
                        self.assertLessEqual(max(video['width'],video['height']),640)
                        self.assertEqual((video['width']%2,video['height']%2),(0,0))
                        self.assertEqual(Fraction(video['avg_frame_rate']),1)
                        if sound:
                            audio = next(stream for stream in info['streams'] if stream['codec_type']=='audio')
                            self.assertEqual(audio['channels'],1)
                            self.assertGreater(int(audio['nb_read_packets']),0)
                        self.assertLessEqual(output.stat().st_size,sandbox_video.MAX_BYTES)
                        self.assertAlmostEqual(duration(output.read_bytes()),3 if name=='normal' else 1,delta=0.1)
                with self.assertRaises(ValueError):
                    sandbox_video.prepare(source,output=source)
                code, result = self.runner(root,source,start=0,end=601)
                self.assertEqual((code,result['error']['code']),(1,'too_long'))

    def test_empty_video_track_fails_safely_without_publishing_output(self):
        with TemporaryDirectory() as temporary, patch.object(sandbox_video,'WORK_ROOT',Path(temporary)):
            root = Path(temporary); source = root/'TEST_SECRET.mkv'
            subprocess.run(['ffmpeg','-v','error','-nostdin','-f','lavfi','-i','color=s=32x32:r=25:d=1',
                '-f','lavfi','-i','sine=duration=1','-vf','select=0','-c:v','libx264','-c:a','aac',str(source)],
                capture_output=True,check=True,timeout=30)
            with self.assertRaisesRegex(ValueError,'video_stream_has_no_packets'):
                sandbox_video.prepare(source)
            error = io.StringIO()
            with patch('sys.argv',['gaoji-video',str(source)]), patch('sys.stderr',error), self.assertRaises(SystemExit) as stopped:
                sandbox_video.main()
            self.assertEqual(stopped.exception.code,1)
            self.assertIn('video_stream_has_no_packets',error.getvalue())
            self.assertNotIn('TEST_SECRET',error.getvalue())
            code, result = self.runner(root,source)
            self.assertEqual(code,1)
            self.assertIn('no video',result['error']['message'])
            self.assertNotIn('TEST_SECRET',json.dumps(result))
            self.assertFalse(list(root.rglob('*.mp4')))

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
            with patch.object(subprocess,'run',return_value=SimpleNamespace(stdout=b'{"format":{"duration":"601"}}')) as run:
                with self.assertRaisesRegex(ValueError,'duration_limit'):
                    sandbox_video.prepare(source)
                run.assert_called_once()
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
