"""Startup candidate: no fake OneBot identity or SDK in official-only mode."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.plugins.ai_chat.platform_mode import PlatformMode, PlatformModeError
from src.plugins.ai_chat.adapters.qq_official.host_config import HostConfiguration, HostConfigurationError
from src.plugins.ai_chat.application_workers import start_background_tasks


class PlatformModeTests(unittest.TestCase):
    def test_default_and_dual_preserve_onebot(self):
        self.assertEqual(PlatformMode.from_env({}),PlatformMode(True,False))
        self.assertEqual(PlatformMode.from_env({'AI_QQ_OFFICIAL_ENABLED':'true'}),PlatformMode(True,True))
        self.assertEqual(PlatformMode.from_env({'AI_ONEBOT_ENABLED':'false','AI_QQ_OFFICIAL_ENABLED':'true'}),PlatformMode(False,True))

    def test_invalid_and_empty_mode_fail_before_transport(self):
        for env in ({'AI_ONEBOT_ENABLED':'false'},{'AI_ONEBOT_ENABLED':'typo'}, {'AI_QQ_OFFICIAL_ENABLED':'typo'}):
            with self.subTest(env=env), self.assertRaises(PlatformModeError):PlatformMode.from_env(env)

    def test_full_product_asgi_import_has_no_nonebot(self):
        code='''
import sys, importlib.abc
class Block(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, path=None, target=None):
  if fullname=='nonebot' or fullname.startswith('nonebot.'):
   raise RuntimeError('unexpected SDK import')
sys.meta_path.insert(0,Block())
from src.plugins.ai_chat.application_runtime import create_application
app=create_application(env={'AI_ONEBOT_ENABLED':'false','AI_QQ_OFFICIAL_ENABLED':'true'})
assert app.state.gaoji_runtime.onebot_enabled is False
assert not any(n=='nonebot' or n.startswith('nonebot.') for n in sys.modules)
import os, types
os.environ.update(AI_ONEBOT_ENABLED='false',AI_QQ_OFFICIAL_ENABLED='true')
seen=[]
sys.modules['uvicorn']=types.SimpleNamespace(run=lambda app,**kwargs:seen.append((app,kwargs)))
from src.plugins.ai_chat.platform_entrypoint import serve
serve()
assert len(seen)==1 and seen[0][1]['access_log'] is False
assert not any(n=='nonebot' or n.startswith('nonebot.') for n in sys.modules)
'''
        result=subprocess.run([sys.executable,'-c',code],cwd=Path(__file__).resolve().parents[1],
            env={**os.environ,'PYTHON_DOTENV_DISABLED':'1','PYTHONDONTWRITEBYTECODE':'1'},capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_dual_desktop_selects_only_shared_writer(self):
        from src.plugins.ai_chat.platform_entrypoint import serve
        from src.plugins.ai_chat import application_diagnostics
        with patch.dict(os.environ,{'AI_ONEBOT_ENABLED':'true','AI_QQ_OFFICIAL_ENABLED':'true'}), \
            patch('nonebot.init'),patch('nonebot.get_driver'), \
            patch('nonebot.load_plugin',return_value=object()),patch('nonebot.run'), \
            patch('src.local_runtime.runtime_health.install_health_writer') as legacy:
            serve(health_writer=True)
            self.assertTrue(application_diagnostics._desktop_requested)
        legacy.assert_not_called()
        application_diagnostics.configure_desktop_health(False)

    def test_project_dependency_is_explicit_before_storage_or_credentials(self):
        from dataclasses import replace
        from src.plugins.ai_chat.config import settings
        from src.plugins.ai_chat.application_runtime import OfficialApplicationRuntime
        with patch('src.plugins.ai_chat.application_runtime.build_app_context') as build:
            with self.assertRaisesRegex(PlatformModeError,'official_projects_require_subagents'):
                OfficialApplicationRuntime(replace(settings,projects_enabled=True,subagents_enabled=False),
                    state_dir='/TEST',cache_dir='/TEST',project_root='/TEST',
                    env={'AI_ONEBOT_ENABLED':'false','AI_QQ_OFFICIAL_ENABLED':'true'})
        build.assert_not_called()

    def test_native_host_path_is_dormant_when_official_disabled(self):
        with patch('src.plugins.ai_chat.adapters.qq_official.host_config._read_secret') as read:
            self.assertIsNone(HostConfiguration.from_env({'AI_QQ_OFFICIAL_HOST_CONFIG_FILE':'/unread'}))
        read.assert_not_called()

    def test_empty_baselines_are_not_fake_accounts(self):
        from src.plugins.ai_chat.platform_types import AccountKey
        raw={'version':1,'account':AccountKey('qq-official','TEST_APP','production').key,'onebot_accounts':[]}
        config=HostConfiguration.parse(json.dumps(raw))
        self.assertEqual(config.onebot_accounts,())
        with TemporaryDirectory() as directory:
            path=Path(directory)/'host.json';path.touch(mode=0o600);path.write_text(json.dumps(raw))
            self.assertEqual(HostConfiguration.from_env({'AI_QQ_OFFICIAL_ENABLED':'true',
                'AI_QQ_OFFICIAL_HOST_CONFIG_FILE':str(path)}).onebot_accounts,())
            with patch('src.plugins.ai_chat.adapters.qq_official.host_config._read_secret') as read:
                with self.assertRaises(HostConfigurationError):HostConfiguration.from_env({
                    'AI_QQ_OFFICIAL_ENABLED':'true','AI_QQ_OFFICIAL_HOST_CONFIG_FILE':str(path),
                    'AI_QQ_MIGRATION_ENABLED':'true','AI_QQ_MIGRATION_CONFIG_FILE':'/different'})
            read.assert_not_called()


class CommonWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_common_workers_never_start_legacy_delivery_approval_or_vision(self):
        starts=[]
        names=('project_runtime','mcp_runtime','turn_journal','proactive_learning','subagent_coordinator','local_model',
            'job_store','media_library','semantic_recall','job_worker','cold_archive','bridge_manager','historian_service','dream_service')
        context=SimpleNamespace(**dict.fromkeys(names),settings=SimpleNamespace(observability_enabled=False,
            alertmanager_url='',alert_notify_group_id=0),logger=Mock(),background_tasks=SimpleNamespace(start=lambda n,f:starts.append(n)),
            mobile_authorization=SimpleNamespace(run_forever=AsyncMock()),vision_worker=SimpleNamespace(run_forever=AsyncMock()),
            reminder_store=object(),delivery_store=object())
        await start_background_tasks(context,maintenance=SimpleNamespace())
        self.assertEqual(starts,[])

    async def test_project_ready_hook_precedes_shared_job_worker(self):
        sequence=[]
        async def start():sequence.append('project')
        async def configure(project):sequence.append('configure')
        async def jobs():pass
        names=('mcp_runtime','turn_journal','proactive_learning','subagent_coordinator','local_model',
            'job_store','media_library','semantic_recall','cold_archive','bridge_manager','historian_service','dream_service',
            'mobile_authorization','vision_worker','reminder_store','delivery_store')
        context=SimpleNamespace(**dict.fromkeys(names),settings=SimpleNamespace(observability_enabled=False,
            alertmanager_url='',alert_notify_group_id=0),logger=Mock(),
            background_tasks=SimpleNamespace(start=lambda n,f:sequence.append(n)),
            project_runtime=SimpleNamespace(start=start),migration_service=SimpleNamespace(configure_project_runtime=configure),
            job_worker=SimpleNamespace(run_forever=jobs,registered_kinds=()))
        await start_background_tasks(context,maintenance=SimpleNamespace())
        self.assertEqual(sequence,['project','configure','durable-jobs'])

    async def test_asgi_lifespan_owns_exact_start_close(self):
        from src.plugins.ai_chat.application_runtime import create_application
        runtime=SimpleNamespace(start=AsyncMock(),close=AsyncMock())
        app=create_application(env={'AI_ONEBOT_ENABLED':'false','AI_QQ_OFFICIAL_ENABLED':'true'},runtime=runtime)
        async with app.router.lifespan_context(app):runtime.start.assert_awaited_once_with(app)
        runtime.close.assert_awaited_once()
        await asyncio.sleep(0)
