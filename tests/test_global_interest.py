import asyncio
from datetime import datetime, timezone
from email.utils import format_datetime
from html import escape
import os
from pathlib import Path
import select
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import ANY, AsyncMock, Mock, patch

import httpx

from src.plugins.ai_chat import global_interest as interest

NOW = 1770000000


def rss(items):
    entries = "".join(
        f"<item><title>{escape(title)}</title><link>{escape(url)}</link>"
        f"<pubDate>{format_datetime(datetime.fromtimestamp(date, timezone.utc))}</pubDate>"
        "<description>TEST_PRIVATE_CHAT</description><author>TEST_PERSON</author></item>"
        for title, url, date in items
    )
    return f"<rss version='2.0'><channel>{entries}</channel></rss>".encode()


def item(source, name="story", date=NOW - 60):
    path = f"/technology/{name}/" if source[0].startswith("nasa") else f"/loc/2026/02/{name}/"
    return ("A book about space", f"https://{source[2]}{path}", date)


def response(data):
    return httpx.Response(200, stream=httpx.ByteStream(data))


class InterestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.logger = Mock()

    async def test_daily_results_restart_expiry_and_no_private_input(self):
        calls = []
        def handler(request):
            calls.append(request)
            source = next(s for s in interest.SOURCES if str(request.url) == s[1])
            return response(rss([item(source)]))
        transport = httpx.MockTransport(handler)
        self.assertTrue(await interest.collect_daily(self.root, logger=self.logger, now=NOW, transport=transport))
        self.assertFalse(await interest.collect_daily(self.root, logger=self.logger, now=NOW + 60, transport=transport))
        self.assertEqual(len(calls), 2)
        for request in calls:
            self.assertEqual(request.method, "GET")
            self.assertNotIn("authorization", request.headers)
            self.assertNotIn("cookie", request.headers)
            self.assertEqual(request.content, b"")
            self.assertEqual(request.headers["accept-encoding"], "identity")
        candidates = interest.read_candidates(self.root, now=NOW)
        self.assertEqual(len(candidates), 2)
        self.assertTrue({"书籍", "太空"}.issubset(candidates[0]["tags"]))
        saved = (self.root / "global-interest" / "state.json").read_text()
        self.assertNotIn("TEST_PRIVATE_CHAT", saved)
        self.assertNotIn("TEST_PERSON", saved)
        self.assertEqual(interest.read_candidates(self.root, now=NOW + interest.CANDIDATE_TTL), [])
        self.assertTrue(await interest.collect_daily(self.root, logger=self.logger, now=NOW + 86400, transport=transport))
        self.assertFalse(await interest.collect_daily(self.root, logger=self.logger, now=NOW, transport=transport))
        self.assertEqual(len(calls), 4)

    async def test_partial_failure_retains_old_without_retry(self):
        def good(request):
            return response(rss([item(next(s for s in interest.SOURCES if str(request.url) == s[1]))]))
        await interest.collect_daily(self.root, logger=self.logger, now=NOW, transport=httpx.MockTransport(good))
        calls = []
        def bad(request):
            calls.append(request)
            if str(request.url) == interest.SOURCES[0][1]:
                raise httpx.ConnectError("TEST_FAILURE", request=request)
            return response(rss([]))
        transport = httpx.MockTransport(bad)
        await interest.collect_daily(self.root, logger=self.logger, now=NOW + 86400, transport=transport)
        await interest.collect_daily(self.root, logger=self.logger, now=NOW + 86401, transport=transport)
        self.assertEqual(len(calls), 2)
        self.assertEqual([c["source"] for c in interest.read_candidates(self.root, now=NOW + 86400)], [interest.SOURCES[0][0]])
        self.assertEqual(interest.read_candidates(self.root, now=NOW + interest.CANDIDATE_TTL), [])
        self.logger.warning.assert_called()

    async def test_limits_timeouts_and_redirects(self):
        class SlowStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"<rss>"
                await asyncio.sleep(1)
        with patch.object(interest, "SOURCE_TIMEOUT", 0.02):
            with self.assertRaises(TimeoutError):
                await interest._fetch(interest.SOURCES[0], NOW, httpx.MockTransport(
                    lambda req: httpx.Response(200, stream=SlowStream())))
        for result in (httpx.Response(302, headers={"location": "https://example.org/"}),
                       httpx.Response(200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(b"bad")),
                       response(b"x" * (interest.MAX_BYTES + 1))):
            with self.assertRaises((httpx.HTTPError, ValueError)):
                await interest._fetch(interest.SOURCES[0], NOW, httpx.MockTransport(lambda req: result))

    def test_parser_rejects_untrusted_links_and_bounds_items(self):
        source = interest.SOURCES[0]
        valid = item(source)
        links = ["https://example.org/private", "https://www.nasa.gov.evil.test/",
                 "https://user@www.nasa.gov/", "http://www.nasa.gov/", "https://www.nasa.gov/people/123/",
                 "https://www.nasa.gov/%70eople/123/", "https://www.nasa.gov/technology/%2e%2e/people/123/",
                 "https://www.nasa.gov/technology/%2570eople/123/"]
        bad = [("bad", url, NOW - 1) for url in links]
        bad += [item(source, "old", NOW - interest.MAX_AGE), item(source, "future", NOW + 1),
                ("<script>bad</script>", valid[1], NOW - 1)]
        parsed = interest._parse_feed(rss(bad + [valid, valid] + [item(source, str(i)) for i in range(20)]), source, NOW)
        self.assertEqual(len(parsed), interest.MAX_ITEMS_PER_SOURCE)
        self.assertEqual(len({row["url"] for row in parsed}), len(parsed))
        self.assertEqual(interest._parse_feed(rss(bad * 7 + [valid]), source, NOW), [])
        self.assertEqual(interest._link(valid[1] + "?tracking=secret#frag", source[2]), valid[1])
        self.assertEqual(interest._link("https://blogs.loc.gov/loc/about/", "blogs.loc.gov"), "")
        for data in (b'<!DOCTYPE rss [<!ENTITY x "x">]><rss/>', '<!DOCTYPE rss><rss/>'.encode('utf-16'), b"<html/>"):
            with self.assertRaises((ValueError, UnicodeError)):
                interest._parse_feed(data, source, NOW)

    async def test_lock_survives_killed_process_without_repeating(self):
        code = '''
import asyncio, logging, sys
from pathlib import Path
import httpx
from src.plugins.ai_chat.global_interest import collect_daily
async def handler(request):
 print("REQUEST", flush=True)
 await asyncio.sleep(100)
 return httpx.Response(500)
asyncio.run(collect_daily(Path(sys.argv[1]), logger=logging.getLogger(), now=int(sys.argv[2]), transport=httpx.MockTransport(handler)))
'''
        env = {"PYTHON_DOTENV_DISABLED": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.Popen([sys.executable, "-u", "-c", code, str(self.root), str(NOW)],
                                cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env)
        try:
            self.assertTrue(select.select([proc.stdout], [], [], 10)[0], "child did not start request")
            self.assertEqual(proc.stdout.readline().strip(), "REQUEST")
            calls = []
            transport = httpx.MockTransport(lambda req: calls.append(req) or httpx.Response(500))
            self.assertFalse(await interest.collect_daily(self.root, logger=self.logger, now=NOW, transport=transport))
            proc.kill()
            proc.wait(timeout=5)
            self.assertFalse(await interest.collect_daily(self.root, logger=self.logger, now=NOW, transport=transport))
            self.assertEqual(calls, [])
            restarted = subprocess.run([sys.executable, "-u", "-c", code, str(self.root), str(NOW)],
                                       cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, env=env, timeout=5)
            self.assertEqual(restarted.returncode, 0, restarted.stderr)
            self.assertNotIn("REQUEST", restarted.stdout)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            proc.stdout.close()
            proc.stderr.close()

    async def test_entrypoint_permissions_single_load_and_corruption(self):
        with (patch.object(sys, "argv", ["gaoji-interest", str(self.root)]),
              patch.object(interest, "collect_daily", new_callable=AsyncMock) as collect):
            await asyncio.to_thread(interest.main)
            collect.assert_awaited_once_with(self.root, logger=ANY)
        old_umask = os.umask(0o077)
        try:
            with patch.object(interest, "_load", wraps=interest._load) as load:
                await interest.collect_daily(self.root, logger=self.logger, now=NOW,
                    transport=httpx.MockTransport(lambda req: httpx.Response(500)))
                self.assertEqual(load.call_count, 1)
        finally:
            os.umask(old_umask)
        directory = self.root / "global-interest"
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o750)
        for name in ("state.json", "lock"):
            self.assertEqual(stat.S_IMODE((directory / name).stat().st_mode), 0o640)
        (directory / "state.json").write_text("broken")
        with self.assertRaises(ValueError):
            await interest.collect_daily(self.root, logger=self.logger, now=NOW,
                transport=httpx.MockTransport(lambda req: self.fail("unexpected request")))


if __name__ == "__main__":
    unittest.main()
