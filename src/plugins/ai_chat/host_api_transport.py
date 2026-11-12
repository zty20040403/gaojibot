"""Credential-bearing HTTP to one fixed public origin; never a general proxy."""
import asyncio
from contextlib import asynccontextmanager
import ipaddress
import json
import re
from urllib.parse import parse_qsl, urlencode, quote, quote_plus

import httpx2

from .host_apis import ApiDenied,HOP_HEADERS,METHODS,canonical_path
from .pinned_http import HttpPolicy,PinnedHttpTransport,resolve_addresses

MAX_BODY = 64*1024*1024
TIMEOUT = {'connect':10,'read':60,'write':60,'pool':5}
_NAT64 = (ipaddress.ip_network('64:ff9b::/96'),ipaddress.ip_network('64:ff9b:1::/48'))


class PinnedAPITransport(PinnedHttpTransport):
    def __init__(self, url, *, resolver=resolve_addresses, transport=None):
        async def public_addresses(host,port):
            addresses = await resolver(host,port)
            for value in addresses:
                address = ipaddress.ip_address(value)
                if isinstance(address,ipaddress.IPv6Address) and (
                        address.sixtofour or address.teredo or any(address in network for network in _NAT64)):
                    raise ApiDenied('api_address_denied')
            return addresses
        self.current = None
        super().__init__(url,HttpPolicy(),resolver=public_addresses,transport=transport)
        if self.endpoint.scheme!='https':
            raise ApiDenied('api_https_required')

    async def handle_async_request(self, request):
        if request.url!=self.endpoint or request.method not in METHODS:
            raise ApiDenied('api_target_mismatch')
        body = request.content
        if len(body)>MAX_BODY:
            raise ApiDenied('api_request_too_large')
        headers = httpx2.Headers(request.headers)
        headers['Host'] = self.endpoint.netloc.decode('ascii')
        headers['Accept-Encoding'] = 'identity'
        address = await self.address()
        if self.current is not None:
            try:
                await self.current()
            except PermissionError:
                raise ApiDenied('api_caller_revoked') from None
        pinned = httpx2.Request(request.method,self.endpoint.copy_with(host=address),
            headers=headers,content=body,extensions={'timeout':TIMEOUT,'sni_hostname':self.endpoint.host})
        return await self.transport.handle_async_request(pinned)


def forwarded_headers(headers, *, response=False):
    headers = httpx2.Headers(headers)
    nominated = {name.strip().lower() for name in headers.get('connection','').split(',')}
    denied = HOP_HEADERS|nominated|({'set-cookie'} if response else {'authorization','cookie',
        'x-http-method','x-http-method-override','x-method-override'})
    return [(key,value) for key,value in headers.multi_items() if key.lower() not in denied]


class ApiResponse:
    def __init__(self, upstream, secret):
        self.upstream,self.status_code = upstream,upstream.status_code
        variants = (secret,quote(secret,safe=''),quote_plus(secret,safe=''),json.dumps(secret)[1:-1])
        self.patterns = {self.percent_case(value.replace('/','\\/').encode()) for value in variants}
        self.patterns.update(self.percent_case(value.encode()) for value in variants)
        self.headers = forwarded_headers(upstream.headers,response=True)
        if upstream.headers.get('content-encoding','identity').lower()!='identity':
            raise ApiDenied('api_response_encoding_denied')
        if any(self.reflected(value.encode()) for _,value in self.headers):
            raise ApiDenied('api_response_contains_credential')

    @staticmethod
    def percent_case(data):
        return re.sub(br'%[0-9a-f]{2}',lambda match:match[0].upper(),data,flags=re.I)

    def reflected(self, data):
        normalized = self.percent_case(data)
        return any(pattern in normalized for pattern in self.patterns)

    async def chunks(self):
        # Keep enough look-behind to reject accidental credential reflection even
        # across chunk boundaries. Successful responses remain byte-for-byte exact.
        tail = b''
        keep = max(map(len,self.patterns))-1
        try:
            async for chunk in self.upstream.aiter_raw():
                tail += chunk
                if self.reflected(tail):
                    raise ApiDenied('api_response_contains_credential')
                if len(tail)>keep:
                    count = len(tail)-keep
                    yield tail[:count]
                    tail = tail[count:]
            if tail:
                yield tail
        except (OSError,TimeoutError,httpx2.HTTPError):
            raise ApiDenied('api_stream_unavailable') from None


@asynccontextmanager
async def forward(api, *, method, raw_path, query, headers, body, transport_factory=PinnedAPITransport, current=None):
    """The caller owns authentication, quota reservation and final audit."""
    path = canonical_path(raw_path)
    if not api.permits(method,path):
        raise ApiDenied('api_operation_denied')
    if len(body)>MAX_BODY or len(query)>8192:
        raise ApiDenied('api_request_too_large')
    secret = await asyncio.to_thread(api.secret)
    fixed_query = {key:value.replace('{secret}',secret) for key,value in api.query.items()}
    try:
        params = [(key,value) for key,value in parse_qsl(query.decode('ascii'),keep_blank_values=True,max_num_fields=1000)
            if key.lower() not in {key.lower() for key in fixed_query}]
        params.extend(fixed_query.items())
        base = httpx2.URL(api.base_url)
        url = base.copy_with(path=base.path.rstrip('/')+path,query=urlencode(params).encode())
        outgoing = httpx2.Headers(forwarded_headers(headers))
        outgoing.update({key:value.replace('{secret}',secret) for key,value in api.headers.items()})
        transport = transport_factory(str(url))
        transport.current = current
    except (ValueError,UnicodeError):
        raise ApiDenied('invalid_api_request') from None
    upstream = None
    try:
        # Direct transport avoids an HTTP client's URL log and cookie jar; there
        # is no redirect-following layer. The timeout also bounds header drips.
        async with asyncio.timeout(60):
            upstream = await transport.handle_async_request(httpx2.Request(method,url,headers=outgoing,content=body))
        yield ApiResponse(upstream,secret)
    except ApiDenied:
        raise
    except (OSError,ValueError,TimeoutError,httpx2.HTTPError):
        raise ApiDenied('api_upstream_unavailable') from None
    finally:
        if upstream is not None:
            await upstream.aclose()
        await transport.aclose()
