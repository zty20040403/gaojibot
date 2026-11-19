"""Pure candidate factory/registry and original-deadline tests; no PostgreSQL."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import httpx2

from src.plugins.ai_chat.adapters.qq_official.host_config import HostConfiguration, HostConfigurationError, ConfiguredHost
from src.plugins.ai_chat.adapters.qq_official.media_runtime import MediaConfig, _read_library_bytes
from src.plugins.ai_chat.adapters.qq_official.media_refs import MediaDenied
from src.plugins.ai_chat.ledger import CanonicalMessage
from src.plugins.ai_chat.message_ir import MessageBody, MediaNode
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey, InboundMessage, PlatformFeatures
from src.plugins.ai_chat.tools import neutral_registry_for
from src.plugins.ai_chat.tools.state import ToolContext, TurnState
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.adapters.qq_official.content_reader import PinnedContentReader, PinnedMediaTransport
from src.plugins.ai_chat.adapters.qq_official.codec import decode_message
from src.qq_official.events import parse_dispatch
from tests.test_qq_official_image_formats import png


class WiringTests(unittest.IsolatedAsyncioTestCase):
    def raw(self, kinds=()):
        return dict(version=1, account=self.account.key, onebot_accounts=[],
                    files={'private_directory':'/tmp/TEST_MEDIA', 'generation_enabled':False})

    def setUp(self):
        self.account = AccountKey('qq-official','TEST_IMAGE_CONFIG','test')
        self.endpoint = EndpointKey(self.account,'private','TEST_MEMBER')
        self.actor = ActorKey(self.account,'c2c_user','','TEST_MEMBER')
        self.now = 100
        self.source = CanonicalMessage(3,self.endpoint.scope.key,'TEST_SOURCE','TEST_MEMBER',8,'TEST',
            'inbound','chat',MessageBody((MediaNode(0,'audio',source='https://example.invalid/voice',source_type='qq-official.attachment',
                raw_data={'asr_refer_text':'TEST transcript','asr_source':'platform_external_content'}),)),
            '',100,None,None,self.actor.key,self.endpoint.key)
        self.row = dict(state='executing',deadline_at=150,canonical_message_id=3,journal_turn_id=4)
        self.token = SimpleNamespace(account_key=self.account.key)
        async def call(function,*args,**kwargs): return function(*args,**kwargs)
        @contextmanager
        def tx(): yield object()
        self.app = SimpleNamespace(settings=SimpleNamespace(video_whisper_model_path='',video_whisper_threads=2,media_max_vision_bytes=20*1024*1024),
            db_call=call,usage_store=None,vision_worker=None,media_library=None,
            message_ledger=SimpleNamespace(get_in_scope=lambda scope,canonical:self.source if canonical==3 else None))
        self.runtime = SimpleNamespace(_token=self.token,_stopping=False,_calls=call,
            deliveries=SimpleNamespace(transaction=tx),store=SimpleNamespace(clock=lambda:self.now),file_service=None)
        self.inbound = InboundMessage(self.endpoint,self.actor,None,'TEST_SOURCE','TEST','',self.source.body,False,None,100,None)
        self.platform = SimpleNamespace(account=self.account,inbound=self.inbound,features=PlatformFeatures(),
            execution=SimpleNamespace(token=self.token,current=lambda cursor,now:self.row))

    async def session(self, **flags):
        raw=self.raw(kinds=())
        raw['media']=flags
        config=HostConfiguration.parse(json.dumps(raw))
        host=ConfiguredHost(config,self.app,SimpleNamespace())
        service=host.media_factory(self.runtime,self.token)
        session=await service.foreground(self.inbound,self.platform)
        self.platform.media_session=session
        return session

    async def test_actual_config_factory_defaults_closed_and_account_exact(self):
        host=ConfiguredHost(HostConfiguration.parse(json.dumps(self.raw(kinds=()))),self.app,None)
        self.assertIsNone(host.media_factory(self.runtime,self.token))
        session=await self.session(asr=True,ocr=True,stickers=True)
        self.assertTrue(session.enabled('transcribe_voice'))
        self.assertFalse(session.enabled('view_image'))
        self.assertFalse(session.enabled('send_sticker'))
        session.service.token=SimpleNamespace(account_key='TEST_OTHER')
        with self.assertRaises(MediaDenied):await session.current()
        raw=self.raw(kinds=());raw['media']={'asr':True}
        host=ConfiguredHost(HostConfiguration.parse(json.dumps(raw)),self.app,None)
        with self.assertRaises(HostConfigurationError):
            host.media_factory(self.runtime,SimpleNamespace(account_key='TEST_OTHER'))

    async def test_real_base_registry_uses_current_canonical_without_event(self):
        await self.session(asr=True,emoji=True)
        context=ToolContext(SimpleNamespace(context=self.app),inbound=self.inbound,platform=self.platform)
        turn=TurnState(context)
        registry=neutral_registry_for(context)
        result=json.loads(await registry.execute(turn,'transcribe_voice',{}))
        self.assertTrue(result['ok']);self.assertEqual(result['text'],'TEST transcript')
        self.assertEqual(result['trust'],'external_content')
        self.assertIsNone(context.event)
        names={d['function']['name'] for d in registry.definitions(context)}
        self.assertNotIn('send_sticker',names)
        self.assertNotIn('view_image',names)

    async def test_deadline_is_original_not_refreshed_and_source_replacement_denied(self):
        session=await self.session(asr=True)
        self.now=150
        with self.assertRaises(MediaDenied):await session.voice({})
        self.now=110;self.row['deadline_at']=200
        with self.assertRaises(MediaDenied):await session.voice({})
        self.row['deadline_at']=150
        source=await session.source({}, {'audio'})
        self.source=replace(self.source,source_endpoint_key=EndpointKey(self.account,'private','OTHER').key)
        with self.assertRaises(MediaDenied):await session.resolver.require(source)

    async def test_unobserved_sticker_handle_has_zero_library_read(self):
        session=await self.session(stickers=True)
        with self.assertRaises(MediaDenied):await session.sticker({'media_handle':'media#1'},frozenset())

    async def test_bound_conversation_reads_and_imports_official_attachment_forms(self):
        scope=ConversationScope('onebot-v11','group','TEST_OLD_GROUP')
        self.inbound=replace(self.inbound,canonical_scope=scope)
        self.platform.inbound=self.inbound
        def get(actual,canonical):
            self.assertEqual(actual,scope)
            return self.source if canonical==3 else None
        self.app.message_ledger.get_in_scope=get
        manager=SimpleNamespace(ensure_default=AsyncMock(return_value={'sandbox_id':'TEST_VM'}),write_file=AsyncMock())
        self.app.sandbox_manager=manager
        session=await self.session(documents=True,ocr=True)
        session.frontend_vision=True
        turn=TurnState(ToolContext(SimpleNamespace(context=self.app),inbound=self.inbound,platform=self.platform))
        query='appid=TEST_APP&fileid=TEST_FILE%2FTEST%2B%3D&rkey=TEST_KEY&spec=0'
        urls=[(prefix+'multimedia.example.invalid/download?'+query,200,'inbound') for prefix in ('https://','//','')]
        urls.extend([('https://gchat.example.invalid/gchatpic_new/TEST_APP/TEST_FILE/0?term=2',200,'inbound'),
                     ('',200,'inbound'),(urls[0][0],403,'inbound'),('',200,'outbound')])
        async def dns(host,port):return ('93.184.216.34',)
        for mime,name,content in (('text/plain','TEST.txt',b'TEST text'),
                                  ('application/pdf','TEST.pdf',b'%PDF-1.7\nTEST_OPAQUE_PDF\n%%EOF'),
                                  ('image/png','TEST.png',png()),
                                  ('video/mp4','clip.mp4',b'TEST_VIDEO'*(300*1024))):
            for form,(url,status,direction) in enumerate(urls):
                with self.subTest(mime=mime,form=form):
                    decoded=decode_message(parse_dispatch({'op':0,'s':1,'t':'C2C_MESSAGE_CREATE','d':{
                        'id':'TEST_SOURCE','author':{'user_openid':'TEST_MEMBER'},'content':'',
                        'attachments':[{'url':url,'filename':name,'content_type':mime,'size':len(content) if url else 100*1024*1024+1}]}}),self.account,100)
                    self.source=replace(self.source,scope_key=scope.key,body=decoded.body,direction=direction)
                    if not url:self.assertNotIn('unsafe_media_url',decoded.evidence.warnings)
                    requests=[]
                    session.images.clear()
                    manager.write_file.reset_mock()
                    async def fetch(request):
                        requests.append(request)
                        return httpx2.Response(status,content=content if status==200 else b'TEST_RESPONSE_SECRET',headers={'content-type':mime})
                    reader=PinnedContentReader(max_bytes=100*1024*1024,transport_factory=lambda url:
                        PinnedMediaTransport(url,resolver=dns,max_response_bytes=100*1024*1024,
                            max_session_bytes=100*1024*1024,transport=httpx2.MockTransport(fetch)))
                    session.service.fetcher=session.service.file_fetcher=session.service.image_fetcher=reader
                    if not url or status!=200:
                        tools=('import_file_to_sandbox',) if mime=='video/mp4' else (
                            'import_file_to_sandbox','view_image' if mime=='image/png' else 'read_attachment')
                        for tool in tools:
                            result=json.loads(await neutral_registry_for(turn.context).execute(turn,tool,{'message_handle':'msg#3'}))
                            self.assertFalse(result['ok'])
                            expected=('media_reference_unavailable' if direction=='outbound' else
                                      'download_http_rejected' if url else 'media_download_url_missing')
                            self.assertEqual(result['code'],expected)
                            if url:self.assertEqual(result['http_status'],403);self.assertIn('不能确定是否过期',result['message'])
                            else:
                                self.assertIn('入站' if direction=='outbound' else '仍无法读取',result['message'])
                                self.assertFalse(requests)
                            self.assertNotIn('TEST_KEY',str(result));self.assertNotIn('TEST_RESPONSE_SECRET',str(result))
                        manager.write_file.assert_not_called()
                        continue
                    if mime=='text/plain':
                        self.assertEqual(json.loads(await neutral_registry_for(turn.context).execute(turn,'read_attachment',{}))['text'],'TEST text')
                    elif mime=='image/png':
                        self.assertTrue((await neutral_registry_for(turn.context).execute(turn,'view_image',{})).data_url.startswith('data:image/'))
                    result=json.loads(await neutral_registry_for(turn.context).execute(turn,'import_file_to_sandbox',{}))
                    self.assertEqual(result['path'],f'/work/chat/3-0-{name}')
                    self.assertEqual(manager.write_file.call_args.args[3],content)
                    self.assertTrue(manager.write_file.call_args.kwargs['allow_large'])
                    self.assertTrue(all(req.url.query==httpx2.URL(
                        'https://'+url.removeprefix('https://').removeprefix('//')).query for req in requests))
                    self.assertNotIn('TEST_KEY',str(result))

    def test_configuration_strict_and_local_fallback_dependency_gate(self):
        for bad in ({'asr':1},{'local_asr':True},{'url':'https://x'},{'max_download_bytes':2097153}):
            raw=self.raw(kinds=());raw['media']=bad
            self.assertIsNone(HostConfiguration.parse(json.dumps(raw)).media)
        with self.assertRaises(ValueError):MediaConfig(timeout_seconds=0)

    def test_library_bytes_cannot_escape_follow_symlink_or_ignore_digest(self):
        with TemporaryDirectory() as root:
            root=Path(root);content=b'TEST_PRIVATE_BYTES';path=root/'actual';path.write_bytes(content)
            digest=hashlib.sha256(content).hexdigest()
            self.assertEqual(_read_library_bytes(root,path,len(content),digest),content)
            link=root/'link';link.symlink_to(path)
            for target,sha in ((link,digest),(path,'0'*64),(root.parent/'outside',digest)):
                with self.assertRaises(MediaDenied):_read_library_bytes(root,target,len(content),sha)

    def test_complete_factory_import_does_not_import_onebot(self):
        code='''import importlib.abc,sys
class Block(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname=='nonebot' or fullname.startswith('nonebot.'):raise AssertionError(fullname)
sys.meta_path.insert(0,Block())
from src.plugins.ai_chat.adapters.qq_official.host_config import HostConfiguration
from src.plugins.ai_chat.adapters.qq_official.runtime import OfficialRuntime
from src.plugins.ai_chat.adapters.qq_official.media_runtime import OfficialMediaService
from src.plugins.ai_chat.tools import _base_registry
_base_registry()
assert not any(x=='nonebot' or x.startswith('nonebot.') for x in sys.modules)
'''
        result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,
                              env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
        self.assertEqual(result.returncode,0,result.stderr)
