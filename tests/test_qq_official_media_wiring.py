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
        self.app = SimpleNamespace(settings=SimpleNamespace(video_whisper_model_path='',video_whisper_threads=2),
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
        self.assertFalse(session.enabled('read_image_text'))
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
        self.assertNotIn('read_image_text',names)

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

    async def test_bound_conversation_imports_large_attachment_into_its_vm(self):
        scope=ConversationScope('onebot-v11','group','TEST_OLD_GROUP')
        self.source=replace(self.source,scope_key=scope.key,body=MessageBody((MediaNode(
            0,'video',source='https://example.invalid/video',name='clip.mp4'),)))
        self.inbound=replace(self.inbound,canonical_scope=scope,body=self.source.body)
        self.platform.inbound=self.inbound
        def get(actual,canonical):
            self.assertEqual(actual,scope)
            return self.source if canonical==3 else None
        self.app.message_ledger.get_in_scope=get
        manager=SimpleNamespace(ensure_default=AsyncMock(return_value={'sandbox_id':'TEST_VM'}),write_file=AsyncMock())
        self.app.sandbox_manager=manager
        session=await self.session(documents=True)
        content=b'TEST_VIDEO'*(300*1024)
        async def dns(host,port):return ('93.184.216.34',)
        session.service.file_fetcher=PinnedContentReader(max_bytes=100*1024*1024,transport_factory=lambda url:
            PinnedMediaTransport(url,resolver=dns,max_response_bytes=100*1024*1024,max_session_bytes=100*1024*1024,
                transport=httpx2.MockTransport(lambda request:httpx2.Response(200,content=content,headers={'content-type':'video/mp4'}))))
        result=await session.import_file({})
        self.assertEqual(result['path'],'/work/chat/3-0-clip.mp4')
        self.assertEqual(manager.write_file.call_args.args[3],content)
        self.assertTrue(manager.write_file.call_args.kwargs['allow_large'])

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
