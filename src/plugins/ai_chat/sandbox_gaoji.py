"""Dependency-free container CLI; all tools route through the host's unified /job/tool."""
import argparse
from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import sys
from urllib.parse import urlsplit

MAX_RESPONSE = 4 * 1024 * 1024
MAX_REQUEST = 256 * 1024


class ClientError(RuntimeError):
    pass


def _valid_host_url(raw_url: str) -> tuple[str, int]:
    url = urlsplit(raw_url)
    port = url.port if url.port is not None else 80
    if (url.scheme != 'http' or url.hostname != '10.0.2.2'
            or url.username is not None or url.password is not None
            or url.path not in {'', '/'} or url.query or url.fragment
            or not 1 <= port <= 65535):
        raise ValueError()
    return url.hostname, port


class Client:
    def __init__(self, directory=Path('/run/gaoji'), *, use_env=True):
        self.directory = directory
        self.use_env = use_env

    def is_job_token(self) -> bool:
        return bool(self.use_env and os.environ.get('GAOJI_JOB_TOKEN') and os.environ.get('GAOJI_HOST_URL'))

    def credentials(self):
        try:
            if self.is_job_token():
                token = os.environ['GAOJI_JOB_TOKEN'].strip()
                if not token or len(token) > 256 or any(ch.isspace() for ch in token):
                    raise ValueError()
                host, port = _valid_host_url(os.environ['GAOJI_HOST_URL'].strip())
                return host, port, token

            def read(name, cap):
                with (self.directory / name).open('rb') as stream:
                    content = stream.read(cap + 1)
                if len(content) > cap:
                    raise ValueError()
                return content.decode('utf-8')

            token = read('token', 128).strip()
            config = json.loads(read('connection.json', 1024))
            if set(config) != {'base_url'} or re.fullmatch(r'gvm_[A-Za-z0-9_-]{43}', token) is None:
                raise ValueError()
            host, port = _valid_host_url(config['base_url'])
            return host, port, token
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise ClientError('VM credentials unavailable; the host must initialize this boot') from None

    @contextmanager
    def open(self, method, route, data=None, *, timeout=120):
        host, port, token = self.credentials()  # Re-read for every request/boot.
        body = None if data is None else json.dumps(data, ensure_ascii=False).encode()
        if body is not None and len(body) > MAX_REQUEST:
            raise ClientError('request exceeds size limit')
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            connection.request(method, route, body, {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise ClientError('redirect refused; VM credentials are never forwarded')
            yield response
        except ClientError:
            raise
        except (OSError, http.client.HTTPException):
            raise ClientError('connection failed; do not repeat writes with a new request_id; use status') from None
        finally:
            connection.close()

    def request(self, method, route, data=None, *, timeout=120):
        with self.open(method, route, data, timeout=timeout) as response:
            content = response.read(MAX_RESPONSE + 1)
            if len(content) > MAX_RESPONSE:
                raise ClientError('response exceeds limit')
            try:
                value = json.loads(content)
            except (ValueError, UnicodeError):
                raise ClientError('invalid JSON response; use status for writes') from None
            return response.status, value

    def list_tools(self):
        status, value = self.request('GET', '/job/tools')
        if not 200 <= status < 300 or not isinstance(value, dict) or not isinstance(value.get('tools'), list):
            return status, value
        return status, value

    def call_tool(self, name: str, arguments: dict, *, timeout=180):
        with self.open('POST', '/job/tool', {'name': name, 'arguments': arguments}, timeout=timeout) as response:
            if response.status >= 400:
                raw = response.read(MAX_RESPONSE + 1)
                try:
                    return response.status, json.loads(raw)
                except (ValueError, UnicodeError):
                    raise ClientError('invalid JSON response; use status for writes') from None
            total = 0
            while True:
                line = response.fp.readline(MAX_RESPONSE + 1)
                if not line:
                    raise ClientError('host closed /job/tool stream without a result; use status for writes')
                total += len(line)
                if total > MAX_RESPONSE:
                    raise ClientError('response exceeds limit')
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    event = json.loads(stripped)
                except (ValueError, UnicodeError):
                    raise ClientError('invalid NDJSON line from /job/tool') from None
                if event.get('type') == 'heartbeat':
                    continue
                if event.get('type') == 'result':
                    if not event.get('ok'):
                        return 502, {'ok': False, 'error': event.get('error') or 'tool_failed'}
                    items = event.get('content') or []
                    texts = [c.get('text', '') for c in items if isinstance(c, dict) and c.get('type') == 'text']
                    joined = '\n'.join(t for t in texts if t != '')
                    try:
                        parsed = json.loads(joined)
                    except (ValueError, TypeError):
                        parsed = joined
                    if isinstance(parsed, dict) and parsed.get('ok') is False:
                        return 502, parsed
                    if isinstance(parsed, dict) and parsed.get('state') in {'failed', 'cancelled', 'ambiguous'}:
                        return 502, parsed
                    return 200, parsed


def _read_json_arg(raw: str | None) -> dict:
    if raw is None or raw == '':
        return {}
    text = sys.stdin.read(MAX_REQUEST) if raw == '-' else raw
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ClientError('JSON arguments must be an object')
    return value


def parser():
    result = argparse.ArgumentParser(prog='gaoji', description='Unified container tool client via host /job/tool.')
    commands = result.add_subparsers(dest='command', required=True)

    commands.add_parser('tools', help='list host tools available to this container')
    call_cmd = commands.add_parser('call', help='invoke any host tool by name with JSON arguments')
    call_cmd.add_argument('tool_name')
    call_cmd.add_argument('json_args', nargs='?', default='{}', help='JSON object, or - for stdin')

    for name in ('say', 'send-file', 'send-image'):
        command = commands.add_parser(name)
        command.add_argument('path' if name != 'say' else 'text', help='workspace file' if name != 'say' else 'text, or - for stdin')
        command.add_argument('--request-id', help='reuse for retries of this exact action')
        if name != 'say':
            command.add_argument('--filename')

    history = commands.add_parser('history')
    history.add_argument('positional_query', nargs='?')
    for name in ('query', 'who', 'from', 'to', 'around'):
        history.add_argument('--' + name)
    history.add_argument('--limit', type=int, default=50)

    search = commands.add_parser('web-search')
    search.add_argument('query')
    search.add_argument('--freshness', choices=('auto', 'day', 'week', 'month', 'year'), default='auto')

    fetch = commands.add_parser('web-fetch')
    fetch.add_argument('url')
    fetch.add_argument('--offset', type=int, default=0)

    skill = commands.add_parser('use-skill')
    skill.add_argument('name')

    status = commands.add_parser('status').add_mutually_exclusive_group(required=True)
    status.add_argument('--request-id')
    status.add_argument('--action-id')

    agent = commands.add_parser('agent')
    agent_sub = agent.add_subparsers(dest='agent_command', required=True)
    ask = agent_sub.add_parser('ask')
    ask.add_argument('question', help='question for foreground chat, or - for stdin')
    ask.add_argument('--job-id', type=int)

    gh = commands.add_parser('github')
    gh.add_argument('path', help='API path starting with /, e.g. /repos/owner/repo/issues')
    gh.add_argument('--method', default='GET', choices=('GET', 'POST', 'PUT', 'PATCH', 'DELETE'))
    gh.add_argument('--query', help='JSON object of query parameters')
    gh.add_argument('--body', help='JSON object body, or - for stdin')
    return result


def main(argv=None, *, client=None):
    args = vars(parser().parse_args(argv))
    command = args.pop('command')
    client = client or Client()
    try:
        if command == 'tools':
            status, value = client.list_tools()
        elif command == 'call':
            status, value = client.call_tool(args['tool_name'].replace('-', '_'), _read_json_arg(args['json_args']))
        elif command == 'status':
            payload = {k: v for k, v in args.items() if v is not None}
            status, value = client.call_tool('action_status', payload)
        elif command == 'agent':
            question = sys.stdin.read(4000) if args['question'] == '-' else args['question']
            job_dir = os.environ.get('GAOJI_JOB_DIR', '').strip()
            asked_file = Path(job_dir) / 'asked' if job_dir and Path(job_dir).is_dir() else None
            if asked_file is not None:
                asked_file.write_text(question, encoding='utf-8')
            payload = {'question': question}
            job_id = args.get('job_id') or (int(os.environ['GAOJI_JOB_ID']) if os.environ.get('GAOJI_JOB_ID', '').isdigit() else None)
            if job_id is not None:
                payload['job_id'] = job_id
            status, value = client.call_tool('agent_ask', payload)
            if not 200 <= status < 300 and asked_file is not None:
                asked_file.unlink(missing_ok=True)
        elif command == 'github':
            payload = {'method': args['method'], 'path': args['path']}
            if args.get('query'):
                payload['query'] = _read_json_arg(args['query'])
            if args.get('body'):
                payload['body'] = _read_json_arg(args['body'])
            status, value = client.call_tool('github', payload)
        else:
            pos_q = args.pop('positional_query', None)
            data = {key: value for key, value in args.items() if value is not None}
            if pos_q and 'query' not in data:
                data['query'] = pos_q
            if command in {'say', 'send-file', 'send-image'}:
                data.setdefault('request_id', secrets.token_hex(16))
                print('request_id=' + data['request_id'], file=sys.stderr, flush=True)
                if data.get('text') == '-':
                    data['text'] = sys.stdin.read(1801)
                if 'text' in data and len(data['text']) > 1800:
                    raise ClientError('text exceeds action limit')
            if command == 'history' and not any(data.get(key) for key in ('query', 'who', 'from', 'to', 'around')):
                data['to'] = '#9223372036854775807'
            status, value = client.call_tool(command.replace('-', '_'), data)
        if isinstance(value, str):
            print(value, flush=True)
        else:
            print(json.dumps(value, ensure_ascii=False), flush=True)
        return 0 if 200 <= status < 300 else 1
    except (ClientError, BrokenPipeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
