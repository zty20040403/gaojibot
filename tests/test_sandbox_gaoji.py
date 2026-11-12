"""Stdlib CLI over actual loopback HTTP; only the guest host address is mapped."""
from contextlib import redirect_stdout,redirect_stderr
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from io import StringIO
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from src.plugins.ai_chat import sandbox_gaoji as cli, sandbox_tools_setup

TOKEN = 'gvm_'+('TEST_'*9)[:43]


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass

    def do_POST(self):
        self.answer()

    def do_GET(self):
        self.answer()

    def answer(self):
        length = int(self.headers.get('Content-Length',0))
        data = json.loads(self.rfile.read(length)) if length else None
        self.server.seen.append((self.command,self.path,self.headers.get('Authorization'),data))
        if self.server.drop:
            self.close_connection = True
            return
        body = self.server.events if self.path.startswith('/vm/events') else json.dumps(self.server.value).encode()
        self.send_response(self.server.status)
        self.send_header('Content-Length',str(len(body)))
        self.send_header('X-Gaoji-Head','3')
        self.send_header('Location','http://example.invalid/TEST_REDIRECT')
        self.end_headers(); self.wfile.write(body)


class SandboxGaojiTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root/'token').write_text(TOKEN)
        (self.root/'connection.json').write_text('{"base_url":"http://10.0.2.2:18640"}')
        self.client = cli.Client(self.root)
        server = self.server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        server.seen,server.status,server.value,server.drop = [],202,{'state':'queued'},False
        server.events = b'\n{"id":4,"text":"TEST_EVENT"}\n{"id":5}\n'
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(thread.join,3); self.addCleanup(server.shutdown)
        original = cli.http.client.HTTPConnection
        def connection(host,port,**kwargs):
            self.assertEqual((host,port),('10.0.2.2',18640))
            return original('127.0.0.1',server.server_port,**kwargs)
        patcher = patch.object(cli.http.client,'HTTPConnection',side_effect=connection)
        patcher.start(); self.addCleanup(patcher.stop)

    def run_cli(self, *args):
        stdout,stderr = StringIO(),StringIO()
        with redirect_stdout(stdout),redirect_stderr(stderr):
            result = cli.main(args,client=self.client)
        return result,stdout.getvalue(),stderr.getvalue()

    def test_commands_are_thin_requests_and_credentials_are_reread_not_printed(self):
        result,out,error = self.run_cli('say','TEST_TEXT','--request-id','TEST_REQUEST','--program','TEST_UNIT')
        self.assertEqual(result,0); self.assertEqual(json.loads(out),{'state':'queued'})
        self.assertIn('request_id=TEST_REQUEST',error); self.assertNotIn(TOKEN,out+error)
        self.assertEqual(self.server.seen[-1],('POST','/vm/say','Bearer '+TOKEN,
            {'text':'TEST_TEXT','request_id':'TEST_REQUEST','program':'TEST_UNIT'}))
        replacement = 'gvm_'+('TEST_B_'*7)[:43]
        (self.root/'token').write_text(replacement)
        for command,args in (('wake',('TEST_WAKE',)),('send-file',('/work/report.txt','--filename','report.txt')),
                ('history',()),('units',()),('web-search',('TEST_QUERY',)),('web-fetch',('https://example.test',))):
            self.assertEqual(self.run_cli(command,*args)[0],0)
            self.assertEqual(self.server.seen[-1][1],'/vm/'+command)
            self.assertEqual(self.server.seen[-1][2],'Bearer '+replacement)
        history = next(row[3] for row in self.server.seen if row[1]=='/vm/history')
        self.assertEqual(history['to'],'#9223372036854775807')
        self.assertEqual(self.run_cli('status','--request-id','TEST_REQUEST')[0],0)
        self.assertEqual(self.server.seen[-1][:2],('GET','/vm/actions?request_id=TEST_REQUEST'))

    def test_http_denials_are_nonzero_and_writes_are_never_retried(self):
        self.server.status,self.server.value = 429,{'error':'program_rate_limited'}
        result,out,_ = self.run_cli('wake','TEST','--request-id','TEST_REQUEST')
        self.assertEqual(result,1); self.assertEqual(json.loads(out),self.server.value)
        self.server.drop = True
        result,out,error = self.run_cli('say','TEST','--request-id','TEST_UNKNOWN')
        self.assertEqual(result,1); self.assertIn('use status',error)
        self.assertEqual(len(self.server.seen),2)
        self.assertNotIn(TOKEN,out+error)

    def test_redirect_is_not_followed_and_invalid_configuration_never_connects(self):
        self.server.status = 302
        result,_,error = self.run_cli('history')
        self.assertEqual(result,1); self.assertIn('redirect refused',error)
        self.assertEqual(len(self.server.seen),1)
        for url in ('https://example.test','http://127.0.0.1:18640','http://10.0.2.2:0',
                'http://user@10.0.2.2:18640','http://10.0.2.2:18640/?token=TEST',{}):
            (self.root/'connection.json').write_text(json.dumps({'base_url':url}))
            result,out,error = self.run_cli('history')
            self.assertEqual(result,1); self.assertNotIn(TOKEN,out+error)
        self.assertEqual(len(self.server.seen),1)

    def test_events_preserve_heartbeats_and_complete_frames_but_eof_is_not_success(self):
        self.server.status = 200
        result,out,error = self.run_cli('events','--after','3')
        self.assertEqual(result,1); self.assertEqual(out,self.server.events.decode())
        self.assertIn('--after 5',error)
        self.assertEqual(self.server.seen[-1][1],'/vm/events?after=3')
        self.server.events = b'{"id":4}\n{"id":5'
        result,out,error = self.run_cli('events')
        self.assertEqual((result,out),(1,'{"id":4}\n'))
        self.assertIn('--after 4',error)
        self.server.events = b'{"id":2}\n'
        self.assertEqual(self.run_cli('events')[0],1)

    def test_cloud_init_and_upgrade_ship_the_same_dependency_free_cli(self):
        entry = next(item for item in sandbox_tools_setup.files() if item['path'].endswith('/sandbox_gaoji.py'))
        self.assertEqual(entry['content'],Path(cli.__file__).read_text())
        self.assertIn('exec /usr/bin/python3 /usr/local/share/gaoji/sandbox_gaoji.py',sandbox_tools_setup.INSTALL)
        standalone = subprocess.run([sys.executable,'-I',cli.__file__,'--help'],capture_output=True,timeout=5)
        self.assertEqual(standalone.returncode,0,standalone.stderr)
        self.assertIn(b'send-file',standalone.stdout)
