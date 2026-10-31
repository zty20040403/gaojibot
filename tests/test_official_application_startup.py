"""Official application startup: no OneBot SDK, explicit lifespan and workers."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.plugins.ai_chat.adapters.qq_official.host_config import HostConfiguration
from src.plugins.ai_chat.application_workers import start_background_tasks


class StartupTests(unittest.TestCase):
    def test_full_product_asgi_import_has_no_nonebot(self):
        code='''
import sys, importlib.abc
class Block(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, path=None, target=None):
  if fullname=='nonebot' or fullname.startswith('nonebot.'):
   raise RuntimeError('unexpected SDK import')
sys.meta_path.insert(0,Block())
from src.plugins.ai_chat.application_runtime import create_application
create_application(env={'AI_QQ_OFFICIAL_ENABLED':'true'})
import types
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

    def test_native_host_path_is_dormant_when_official_disabled(self):
        with patch('src.plugins.ai_chat.adapters.qq_official.host_config._read_secret') as read:
            self.assertIsNone(HostConfiguration.from_env({'AI_QQ_OFFICIAL_HOST_CONFIG_FILE':'/unread'}))
        read.assert_not_called()

    def test_old_configuration_fields_are_ignored(self):
        from src.plugins.ai_chat.platform_types import AccountKey
        raw={'version':1,'account':AccountKey('qq-official','TEST_APP','production').key,'onebot_accounts':[],
             'native_background':{'schedules':{'enabled':True},'proactive':{'enabled':False}}}
        config=HostConfiguration.parse(json.dumps(raw))
        self.assertEqual(config.account.app_id,'TEST_APP')
        self.assertFalse(config.proactive.enabled)


class CommonWorkerTests(unittest.IsolatedAsyncioTestCase):
    def context(self, starts, **overrides):
        names=('turn_journal','proactive_learning','local_model','job_store','media_library','semantic_recall',
               'job_worker','cold_archive','conversations')
        values={**dict.fromkeys(names),**overrides}
        return SimpleNamespace(**values,settings=SimpleNamespace(observability_enabled=False),logger=Mock(),
            background_tasks=SimpleNamespace(start=lambda n,f:starts.append(n)))

    async def test_nothing_starts_without_services(self):
        starts=[]
        await start_background_tasks(self.context(starts),maintenance=SimpleNamespace())
        self.assertEqual(starts,[])

    async def test_durable_jobs_start_when_configured(self):
        starts=[]
        async def jobs():pass
        context=self.context(starts,job_worker=SimpleNamespace(run_forever=jobs,registered_kinds=()))
        await start_background_tasks(context,maintenance=SimpleNamespace())
        self.assertEqual(starts,['durable-jobs'])

    async def test_asgi_lifespan_owns_exact_start_close(self):
        from src.plugins.ai_chat.application_runtime import create_application
        runtime=SimpleNamespace(start=AsyncMock(),close=AsyncMock())
        app=create_application(env={'AI_QQ_OFFICIAL_ENABLED':'true'},runtime=runtime)
        async with app.router.lifespan_context(app):runtime.start.assert_awaited_once_with(app)
        runtime.close.assert_awaited_once()
        await asyncio.sleep(0)


if __name__ == "__main__":
    unittest.main()
