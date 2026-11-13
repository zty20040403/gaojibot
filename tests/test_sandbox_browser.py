"""Bounded guest login checks; no site credentials or real external login."""
import asyncio
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock

from src.plugins.ai_chat import sandbox_browser as browser
from src.plugins.ai_chat import sandbox_tools_setup as setup


class BrowserLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_requires_boolean_site_evidence(self):
        check = AsyncMock(side_effect=[False,False,True])
        self.assertTrue(await browser.wait_for_login(check,timeout=1,interval=.001))
        self.assertEqual(check.await_count,3)
        for value in ({'cookie':'TEST_SECRET'},'TEST redirect',None,1):
            with self.assertRaises(TypeError):
                await browser.wait_for_login(AsyncMock(return_value=value))

    async def test_hung_check_is_cancelled_and_timeout_does_not_claim_success(self):
        stopped = asyncio.Event()
        async def hung():
            try:
                await asyncio.Future()
            finally:
                stopped.set()
        self.assertFalse(await browser.wait_for_login(hung,timeout=.02))
        self.assertTrue(stopped.is_set())
        self.assertFalse(await browser.wait_for_login(AsyncMock(return_value=False),timeout=.02,interval=.001))

    async def test_site_errors_and_external_cancellation_are_not_hidden_as_timeout(self):
        for error in (TimeoutError('TEST website blocked'),RuntimeError('TEST browser closed'),asyncio.CancelledError()):
            with self.assertRaises(type(error)):
                await browser.wait_for_login(AsyncMock(side_effect=error))
        for timeout in (0,181,True,float('nan'),float('inf')):
            with self.assertRaises(ValueError):
                await browser.wait_for_login(AsyncMock(),timeout=timeout)
        for interval in (0,-1,11,True,float('nan')):
            with self.assertRaises(ValueError):
                await browser.wait_for_login(AsyncMock(),interval=interval)

    def test_prepared_vm_imports_new_browser_payload_without_reinstalling_packages(self):
        files = {item['path']:item for item in setup.files()}
        wrapper = files['/usr/local/bin/gaoji-python']
        self.assertEqual(wrapper['permissions'],'0755')
        self.assertEqual(wrapper['content'],setup.PYTHON_RUN)
        self.assertIn('PYTHONPATH="/usr/local/share/gaoji',wrapper['content'])
        self.assertIn(setup.PYTHON_RUN,setup.INSTALL)
        payload = files['/usr/local/share/gaoji/gaoji_browser.py']['content']
        self.assertEqual(payload,Path(browser.__file__).read_text())
        self.assertIn('async def wait_for_login',payload)
        self.assertNotIn('shutil.copy',setup.INSTALL)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fresh,stale = root/'fresh',root/'stale'
            fresh.mkdir(); stale.mkdir()
            (fresh/'gaoji_browser.py').write_text(payload)
            (stale/'gaoji_browser.py').write_text("raise RuntimeError('TEST stale image module')")
            script = root/'gaoji-python'
            script.write_text(setup.PYTHON_RUN.replace('/usr/local/share/gaoji',str(fresh))
                .replace('/opt/gaoji-tools/bin/python',shlex.quote(sys.executable)))
            result = subprocess.run(['/bin/sh',str(script),'-c','import gaoji_browser; print(callable(gaoji_browser.wait_for_login))'],
                env={'PYTHONPATH':str(stale)},check=True,capture_output=True,text=True,timeout=10)
            self.assertEqual(result.stdout.strip(),'True')

    def test_skill_requires_real_delivery_and_never_exports_credentials(self):
        skill = (Path(__file__).parents[1]/'skills/browser.md').read_text()
        for text in ('个人账号只在私聊','这个登录全群可用','提醒撤回','最多三分钟','wait_for_login',
                'gaoji send-file','gaoji status','不换 ID 盲重试','storage_state()','cookie/本地存储的值不导出'):
            self.assertIn(text,skill)
