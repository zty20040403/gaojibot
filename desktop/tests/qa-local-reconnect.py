#!/usr/bin/env python3
"""Synthetic authenticated controller for the Docker reconnect contract test."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path

marker = Path('/var/lib/gaoji-app/qa-marker')
if not marker.exists():
    marker.write_bytes(os.urandom(32))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        if self.headers.get('Authorization') != 'Bearer ' + os.environ['QA_TOKEN']:
            self.send_error(401)
            return
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        if self.path == '/versions':
            data = {'checksum': os.environ['QA_CHECKSUM'], 'revision': 1}
        elif self.path == '/status':
            data = {'desktop_installation': {'status': 'awaiting_setup', 'stage': 'ready'},
                    'operation': None, 'qa_marker': marker.read_bytes().hex()}
        else:
            self.send_error(404)
            return
        payload = json.dumps({'data': data}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


HTTPServer(('0.0.0.0', 8090), Handler).serve_forever()
