"""Real SQLite domain projection and guest-only CDP doubles; no real login."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch

from src.plugins.ai_chat import sandbox_browser_state as browser
from src.plugins.ai_chat.sandbox_tools_setup import files


class BrowserDomainTests(unittest.TestCase):
    def test_readonly_cookie_domains_ignore_values_and_expired_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            self.assertEqual(browser.domains(profile=profile),{'domains':[],'truncated':False})
            path = profile/'Default/Network/Cookies'; path.parent.mkdir(parents=True)
            connection = sqlite3.connect(path)
            connection.execute('CREATE TABLE cookies(host_key TEXT,value TEXT,encrypted_value BLOB,expires_utc INTEGER)')
            connection.executemany('INSERT INTO cookies VALUES (?,?,?,?)',[(name,'TEST_SECRET',b'TEST_SECRET',expires)
                for name,expires in (('.Test.Example',0),('test.example',0),('expired.example',1),('other.example',2**62))])
            connection.commit(); connection.close()
            digest = hashlib.sha256(path.read_bytes()).digest()
            result = browser.domains(profile=profile,now=1800000000)
            self.assertEqual(result,{'domains':['other.example','test.example'],'truncated':False})
            self.assertNotIn('TEST_SECRET',json.dumps(result))
            self.assertEqual(hashlib.sha256(path.read_bytes()).digest(),digest)
            connection = sqlite3.connect(path)
            connection.executemany('INSERT INTO cookies VALUES (?,?,?,?)',[(f'test-{i}.example','TEST_SECRET',b'',0) for i in range(210)])
            connection.commit(); connection.close()
            result = browser.domains(profile=profile)
            self.assertTrue(result['truncated']); self.assertEqual(len(result['domains']),200)

    def test_domains_are_hostnames_not_paths_urls_or_sql(self):
        self.assertEqual(browser.domain_name('.TEST.Example.'),'test.example')
        self.assertEqual(browser.domain_name('localhost'),'localhost')
        self.assertEqual(browser.domain_name('[::1]'),'::1')
        for bad in ('https://test.example','test.example/a','a.test; DROP TABLE cookies','x..example','*.example','com',False,'test@example'):
            with self.assertRaises(ValueError):
                browser.domain_name(bad)
        payload = next(item for item in files() if item['path'].endswith('/sandbox_browser_state.py'))
        self.assertEqual(payload['content'],Path(browser.__file__).read_text())

    def test_cli_failures_never_emit_browser_exception_values(self):
        output = io.StringIO()
        with patch.object(browser,'domains',side_effect=RuntimeError('TEST_SECRET_VALUE')),contextlib.redirect_stdout(output):
            self.assertEqual(browser.main(['domains']),1)
        self.assertEqual(json.loads(output.getvalue()),{'error':'browser_state_unavailable'})


class BrowserLogoutTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        self.cookies = [{'domain':'.test.example','value':'TEST_COOKIE'},
            {'domain':'sub.test.example','value':'TEST_SUB_COOKIE'}, {'domain':'.other.example','value':'TEST_OTHER_COOKIE'}]
        self.session = SimpleNamespace(send=AsyncMock(),detach=AsyncMock())
        self.control = SimpleNamespace(close=AsyncMock())
        self.target = SimpleNamespace(url='https://sub.test.example:8443/TEST_PRIVATE_PATH',close=AsyncMock())
        self.other = SimpleNamespace(url='https://other.example',close=AsyncMock())
        async def clear(*,domain):
            self.assertIsInstance(domain,re.Pattern)
            self.cookies = [item for item in self.cookies if domain.search(item['domain']) is None]
        return SimpleNamespace(storage_state=AsyncMock(return_value={'cookies':list(self.cookies),
            'origins':[{'origin':'https://sub.test.example:8443','localStorage':[{'name':'TEST','value':'TEST_STORAGE'}]},
                {'origin':'https://other.example','localStorage':[]}]}),
            new_page=AsyncMock(return_value=self.control),new_cdp_session=AsyncMock(return_value=self.session),
            pages=[self.target,self.other],clear_cookies=AsyncMock(side_effect=clear),cookies=AsyncMock(side_effect=lambda:list(self.cookies)))

    async def test_logout_closes_target_pages_and_clears_subdomains_without_leaking_values(self):
        context = self.context()
        result = await browser.logout('test.example',context=context)
        self.assertEqual(result,{'domain':'test.example','cleared':True})
        self.target.close.assert_awaited_once(); self.other.close.assert_not_awaited()
        origins = {call.args[1]['origin'] for call in self.session.send.await_args_list}
        self.assertIn('https://sub.test.example:8443',origins)
        self.assertEqual(origins,{'http://test.example','https://test.example','http://sub.test.example',
            'https://sub.test.example','https://sub.test.example:8443'})
        self.assertEqual(self.cookies,[{'domain':'.other.example','value':'TEST_OTHER_COOKIE'}])
        self.session.detach.assert_awaited_once(); self.control.close.assert_awaited_once()
        self.assertNotIn('TEST_',json.dumps(result))

    async def test_parent_cookie_requires_explicit_broader_domain_not_false_success(self):
        context = self.context()
        with self.assertRaises(browser.ParentDomainRequired) as raised:
            await browser.logout('sub.test.example',context=context)
        self.assertEqual(raised.exception.domains,['test.example'])
        context.new_page.assert_not_awaited(); context.clear_cookies.assert_not_awaited()

    async def test_guest_errors_and_remaining_cookies_never_report_success(self):
        context = self.context()
        self.session.send.side_effect = RuntimeError('TEST_CDP_FAILURE')
        with self.assertRaises(RuntimeError):
            await browser.logout('test.example',context=context)
        self.control.close.assert_awaited_once()
        context = self.context(); context.clear_cookies = AsyncMock()
        with self.assertRaisesRegex(RuntimeError,'browser_logout_incomplete'):
            await browser.logout('test.example',context=context)
        self.control.close.assert_awaited_once()
        context = self.context(); context.new_cdp_session.side_effect = RuntimeError('TEST_CONNECT')
        with self.assertRaises(RuntimeError):
            await browser.logout('test.example',context=context)
        self.control.close.assert_awaited_once()
