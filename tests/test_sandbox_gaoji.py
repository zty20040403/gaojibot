"""Stdlib CLI over unified /job/tool HTTP; only the guest host address is mapped."""
from contextlib import redirect_stdout, redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from src.plugins.ai_chat import sandbox_gaoji as cli

TOKEN = 'gvm_' + ('TEST_' * 9)[:43]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.answer()

    def do_GET(self):
        self.answer()

    def answer(self):
        length = int(self.headers.get('Content-Length', 0))
        data = json.loads(self.rfile.read(length)) if length else None
        self.server.seen.append((self.command, self.path, self.headers.get('Authorization'), data))
        if self.server.drop:
            self.close_connection = True
            return
        if self.path == '/job/tool' and self.server.status < 400:
            line = json.dumps({
                'type': 'result',
                'ok': self.server.tool_ok,
                'content': [{'type': 'text', 'text': json.dumps(self.server.value)}],
                **({'error': 'tool_denied'} if not self.server.tool_ok else {}),
            }).encode()
            body = b'{"type":"heartbeat"}\n' + line + b'\n'
        elif self.path == '/job/tools' and self.server.status < 400:
            body = json.dumps({'tools': [{'name': 'history', 'description': 'search'}]}).encode()
        else:
            body = json.dumps(self.server.value).encode()
        self.send_response(self.server.status)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Location', 'http://example.invalid/TEST_REDIRECT')
        self.end_headers()
        self.wfile.write(body)


class SandboxGaojiTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'token').write_text(TOKEN)
        (self.root / 'connection.json').write_text('{"base_url":"http://10.0.2.2:18640"}')
        self.client = cli.Client(self.root, use_env=False)
        server = self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.seen, server.status, server.value, server.drop, server.tool_ok = [], 200, {'state': 'queued'}, False, True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.shutdown)
        original = cli.http.client.HTTPConnection

        def connection(host, port, **kwargs):
            self.assertEqual((host, port), ('10.0.2.2', 18640))
            return original('127.0.0.1', server.server_port, **kwargs)

        patcher = patch.object(cli.http.client, 'HTTPConnection', side_effect=connection)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *args):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = cli.main(args, client=self.client)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_commands_all_route_through_unified_job_tool_and_reread_credentials(self):
        result, out, error = self.run_cli('say', 'TEST_TEXT', '--request-id', 'TEST_REQUEST')
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(out), {'state': 'queued'})
        self.assertIn('request_id=TEST_REQUEST', error)
        self.assertNotIn(TOKEN, out + error)
        self.assertEqual(self.server.seen[-1], ('POST', '/job/tool', 'Bearer ' + TOKEN, {
            'name': 'say',
            'arguments': {'text': 'TEST_TEXT', 'request_id': 'TEST_REQUEST'},
        }))

        replacement = 'gvm_' + ('TEST_B_' * 7)[:43]
        (self.root / 'token').write_text(replacement)
        for command, args, expected_tool in (
            ('send-file', ('/work/report.txt', '--filename', 'report.txt'), 'send_file'),
            ('send-image', ('/work/plot.png',), 'send_image'),
            ('history', (), 'history'),
            ('web-search', ('TEST_QUERY',), 'web_search'),
            ('web-fetch', ('https://example.test',), 'web_fetch'),
            ('use-skill', ('browser',), 'use_skill'),
            ('status', ('--request-id', 'TEST_REQUEST'), 'action_status'),
            ('agent', ('ask', 'TEST_QUESTION'), 'agent_ask'),
            ('github', ('/repos/test/repo/issues',), 'github'),
        ):
            self.assertEqual(self.run_cli(command, *args)[0], 0)
            self.assertEqual(self.server.seen[-1][1], '/job/tool')
            self.assertEqual(self.server.seen[-1][2], 'Bearer ' + replacement)
            self.assertEqual(self.server.seen[-1][3]['name'], expected_tool)

        history = next(row[3]['arguments'] for row in self.server.seen if row[3] and row[3].get('name') == 'history')
        self.assertEqual(history['to'], '#9223372036854775807')
        self.assertEqual(self.run_cli('tools')[0], 0)
        self.assertEqual(self.server.seen[-1][:2], ('GET', '/job/tools'))

    def test_http_denials_are_nonzero_and_writes_are_never_retried(self):
        self.server.status, self.server.value = 429, {'error': 'program_rate_limited'}
        result, out, _ = self.run_cli('say', 'TEST', '--request-id', 'TEST_REQUEST')
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(out), self.server.value)
        self.server.drop = True
        result, out, error = self.run_cli('say', 'TEST', '--request-id', 'TEST_UNKNOWN')
        self.assertEqual(result, 1)
        self.assertIn('use status', error)
        self.assertEqual(len(self.server.seen), 2)
        self.assertNotIn(TOKEN, out + error)

    def test_redirect_is_not_followed_and_invalid_configuration_never_connects(self):
        self.server.status = 302
        result, _, error = self.run_cli('history')
        self.assertEqual(result, 1)
        self.assertIn('redirect refused', error)
        self.assertEqual(len(self.server.seen), 1)
        for url in (
            'https://example.test', 'http://127.0.0.1:18640', 'http://10.0.2.2:0',
            'http://user@10.0.2.2:18640', 'http://10.0.2.2:18640/?token=TEST', {},
        ):
            (self.root / 'connection.json').write_text(json.dumps({'base_url': url}))
            result, out, error = self.run_cli('history')
            self.assertEqual(result, 1)
            self.assertNotIn(TOKEN, out + error)
        self.assertEqual(len(self.server.seen), 1)

    def test_container_ships_dependency_free_cli(self):
        tools_nix = (Path(__file__).parents[1] / 'nix/guest/tools.nix').read_text()
        self.assertIn('"sandbox_gaoji.py"', tools_nix)
        self.assertIn('(wrapper "gaoji" "${share}/share/gaoji/sandbox_gaoji.py")', tools_nix)
        standalone = subprocess.run([sys.executable, '-I', cli.__file__, '--help'], capture_output=True, timeout=5)
        self.assertEqual(standalone.returncode, 0, standalone.stderr)
        self.assertIn(b'send-file', standalone.stdout)
        self.assertIn(b'agent', standalone.stdout)
        self.assertIn(b'github', standalone.stdout)
