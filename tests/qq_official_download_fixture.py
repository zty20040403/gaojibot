"""Loopback-only TLS reverse proxy. Test trust/address injection is never config."""
from datetime import datetime, timedelta, timezone
import ipaddress
from pathlib import Path
import socket
import ssl

import aiohttp
from aiohttp import web
import httpx2
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from src.plugins.ai_chat.mcp_bridge.http_transport import HttpPolicy, PinnedHttpTransport
from src.plugins.ai_chat.adapters.qq_official.download_runtime import probe_https

ORIGIN='https://download.example.test'


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        return sock.getsockname()[1]


class TLSProxy:
    def __init__(self,directory,backend_port):
        self.directory,self.backend_port=Path(directory),backend_port
        self.status=200;self.wrong=False;self.requests=[]
        self.runner=self.client=None

    async def start(self):
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'download.example.test')])
        cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc)-timedelta(days=1))
            .not_valid_after(datetime.now(timezone.utc)+timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('download.example.test'),
                x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False)
            .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True).sign(key,hashes.SHA256()))
        cert_path=self.directory/'test-ca.pem';key_path=self.directory/'test-key.pem'
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()));key_path.chmod(0o600)
        server=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);server.load_cert_chain(cert_path,key_path)
        self.trust=ssl.create_default_context(cafile=str(cert_path))
        self.client=aiohttp.ClientSession(trust_env=False)
        app=web.Application(client_max_size=10_000_000)
        async def forward(request):
            self.requests.append((request.method,request.path))
            if self.status!=200:return web.Response(status=self.status,headers={'Location':ORIGIN})
            if self.wrong and request.path=='/health/download':return web.Response(body=b'x'*64,content_type='text/plain')
            headers={k:v for k,v in request.headers.items() if k.lower() in {'x-gaoji-probe','range','if-range'}}
            async with self.client.request(request.method,f'http://127.0.0.1:{self.backend_port}'+request.path,
                    headers=headers,data=await request.read(),allow_redirects=False) as response:
                body=await response.read()
                return web.Response(status=response.status,body=body,headers={k:v for k,v in response.headers.items()
                    if k.lower() not in {'transfer-encoding','connection','date','server'}})
        app.router.add_route('*','/{tail:.*}',forward)
        self.runner=web.AppRunner(app,access_log=None);await self.runner.setup()
        sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen();sock.setblocking(False)
        self.port=sock.getsockname()[1]
        await web.SockSite(self.runner,sock,ssl_context=server).start()
        return self

    def transport(self,url,*,trusted=True):
        inner=httpx2.AsyncHTTPTransport(verify=self.trust if trusted else True,trust_env=False,retries=0)
        port=self.port
        class PortTransport(httpx2.AsyncBaseTransport):
            async def handle_async_request(self,request):
                return await inner.handle_async_request(httpx2.Request(request.method,
                    request.url.copy_with(port=port),headers=request.headers,stream=request.stream,extensions=request.extensions))
            async def aclose(self):await inner.aclose()
        async def resolver(host,port):
            if host!='download.example.test':raise AssertionError('nonloopback test URL')
            return ('127.0.0.1',)
        return PinnedHttpTransport(url,HttpPolicy(private_endpoints=frozenset({url})),
            resolver=resolver,transport=PortTransport())

    async def probe(self,url,nonce):
        return await probe_https(url,nonce,transport_factory=self.transport)

    async def request(self,method,url,headers=None):
        # Keep the public-origin TLS/SNI contract while mapping only this fake
        # test origin to loopback. Not an operator-configurable production port.
        transport=self.transport(url)
        try:
            # Existing pinned transport permits GET but not HEAD; this client
            # path tests actual proxy HEAD through aiohttp's trusted CA instead.
            if method=='HEAD':
                async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=self.trust)) as client:
                    async with client.head(f'https://127.0.0.1:{self.port}'+httpx2.URL(url).path,
                            headers={'Host':'download.example.test',**(headers or {})}) as response:
                        return response.status,dict(response.headers),await response.read()
            response=await transport.handle_async_request(httpx2.Request(method,url,headers=headers))
            try:return response.status_code,dict(response.headers),await response.aread()
            finally:await response.aclose()
        finally:await transport.aclose()

    async def close(self):
        if self.runner:await self.runner.cleanup()
        if self.client:await self.client.close()
