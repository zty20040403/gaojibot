"""Dependency-free VM CLI; authority comes only from the host's VM routes."""
import argparse
from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import sys
from urllib.parse import urlencode, urlsplit, quote

MAX_RESPONSE = 1024*1024


class ClientError(RuntimeError):
    pass


class Client:
    def __init__(self, directory=Path('/run/gaoji')):
        self.directory = directory

    def credentials(self):
        try:
            def read(name, cap):
                with (self.directory/name).open('rb') as stream:
                    content = stream.read(cap+1)
                if len(content)>cap:
                    raise ValueError()
                return content.decode('utf-8')
            token = read('token',128).strip()
            config = json.loads(read('connection.json',1024))
            url = urlsplit(config['base_url'])
            port = url.port if url.port is not None else 80
            if (set(config)!={'base_url'} or re.fullmatch(r'gvm_[A-Za-z0-9_-]{43}',token) is None
                    or url.scheme!='http' or url.hostname!='10.0.2.2' or url.username is not None
                    or url.password is not None or url.path not in {'','/'} or url.query or url.fragment
                    or not 1<=port<=65535):
                raise ValueError()
            return url.hostname,port,token
        except (OSError,ValueError,TypeError,KeyError,AttributeError):
            raise ClientError('VM credentials unavailable; the host must initialize this boot') from None

    @contextmanager
    def open(self, method, route, data=None):
        host,port,token = self.credentials() # Re-read for every request/boot.
        body = None if data is None else json.dumps(data,ensure_ascii=False).encode()
        if body is not None and len(body)>16384:
            raise ClientError('request exceeds 16 KiB')
        connection = http.client.HTTPConnection(host,port,timeout=120)
        try:
            connection.request(method,route,body,{'Authorization':'Bearer '+token,'Content-Type':'application/json'})
            response = connection.getresponse()
            if 300<=response.status<400:
                raise ClientError('redirect refused; VM credentials are never forwarded')
            yield response
        except (OSError,http.client.HTTPException):
            raise ClientError('connection failed; do not repeat writes with a new request_id; use status') from None
        finally:
            connection.close()

    def request(self, method, route, data=None):
        with self.open(method,route,data) as response:
            content = response.read(MAX_RESPONSE+1)
            if len(content)>MAX_RESPONSE:
                raise ClientError('response exceeds 1 MiB')
            try:
                value = json.loads(content)
            except (ValueError,UnicodeError):
                raise ClientError('invalid JSON response; use status for writes') from None
            return response.status,value

    def events(self, after=None):
        if after is not None and not 0<=after<2**63:
            raise ClientError('after must be a nonnegative message ID')
        route = '/vm/events'+('?' + urlencode({'after':after}) if after is not None else '')
        cursor = after
        try:
            with self.open('GET',route) as response:
                if response.status!=200:
                    raise ClientError('event subscription failed: HTTP '+str(response.status))
                try:
                    head = int(response.getheader('X-Gaoji-Head'))
                    if not 0<=head<2**63:
                        raise ValueError()
                except (ValueError,TypeError):
                    raise ClientError('event stream has no valid starting cursor') from None
                if cursor is None:
                    cursor = head
                while True:
                    line = response.readline(MAX_RESPONSE+1)
                    if not line or len(line)>MAX_RESPONSE or not line.endswith(b'\n'):
                        raise ClientError('event stream ended or frame is incomplete/oversized')
                    try:
                        text = line.decode('utf-8')
                        if text.strip():
                            message = json.loads(text)
                            identity = message['id']
                            if type(identity) is not int or not cursor<identity<2**63:
                                raise ValueError()
                        else:
                            identity = cursor
                    except (ValueError,KeyError,TypeError):
                        raise ClientError('invalid event frame') from None
                    yield text
                    cursor = identity # Advance only after the complete line was consumed.
        except ClientError as error:
            raise ClientError(str(error)+(f'; resume with --after {cursor}' if cursor is not None else '')) from None


def parser():
    result = argparse.ArgumentParser(prog='gaoji',description='Conversation-scoped VM client; no human/owner authority.')
    commands = result.add_subparsers(dest='command',required=True)
    for name in ('say','wake','send-file'):
        command = commands.add_parser(name)
        command.add_argument('path' if name=='send-file' else 'text',help='workspace file' if name=='send-file' else 'text, or - for stdin')
        command.add_argument('--program',default=os.environ.get('GAOJI_PROGRAM','manual'))
        command.add_argument('--request-id',help='reuse for retries of this exact action')
        if name=='send-file':
            command.add_argument('--filename')
    commands.add_parser('events').add_argument('--after',type=int)
    commands.add_parser('units',help='sync registered units through the host, not a guest sudo shell')
    command = commands.add_parser('history')
    for name in ('query','who','from','to','around'):
        command.add_argument('--'+name)
    command.add_argument('--limit',type=int,default=50)
    command = commands.add_parser('web-search'); command.add_argument('query')
    command.add_argument('--freshness',choices=('auto','day','week','month','year'),default='auto')
    command = commands.add_parser('web-fetch'); command.add_argument('url'); command.add_argument('--offset',type=int,default=0)
    command = commands.add_parser('status').add_mutually_exclusive_group(required=True)
    command.add_argument('--request-id'); command.add_argument('--action-id')
    return result


def main(argv=None, *, client=None):
    args = vars(parser().parse_args(argv))
    command = args.pop('command')
    client = client or Client()
    try:
        if command=='events':
            for line in client.events(args['after']):
                print(line,end='',flush=True)
            return 0
        if command=='status':
            route = '/vm/actions/'+quote(args['action_id'],safe='') if args['action_id'] else '/vm/actions?'+urlencode({'request_id':args['request_id']})
            status,value = client.request('GET',route)
        else:
            data = {key:value for key,value in args.items() if value is not None}
            if command in {'say','wake','send-file'}:
                data.setdefault('request_id',secrets.token_hex(16))
                print('request_id='+data['request_id'],file=sys.stderr,flush=True)
                if data.get('text')=='-':
                    data['text'] = sys.stdin.read(4001)
                if 'text' in data and len(data['text'])>(1800 if command=='say' else 4000):
                    raise ClientError('text exceeds action limit')
            if command=='history' and not any(data.get(key) for key in ('query','who','from','to','around')):
                data['to'] = '#9223372036854775807' # Latest records, not a VM-local timezone guess.
            status,value = client.request('POST','/vm/'+command,data)
        print(json.dumps(value,ensure_ascii=False),flush=True)
        return 0 if 200<=status<300 else 1
    except (ClientError,BrokenPipeError) as error:
        print(str(error),file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__=='__main__':
    raise SystemExit(main())
