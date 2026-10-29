"""One physical signed PUT, to an explicitly approved public TLS endpoint.

No QQ credential, cookie jar, proxy, redirect or retry. Host evidence and the
caller's durable source/stage permit authorize a call; a provider URL alone
cannot expand the configured hostname set.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import threading
from dataclasses import dataclass, field
import ipaddress
import re
import sys

import httpx2

from ...mcp_bridge.http_transport import HttpPolicy, PinnedHttpTransport, resolve_addresses
from .native_media import UploadPart, validate_part_url
from .native_upload import PutResponse, UploadGuardError
from .project_authority import RecipientEvidenceUnavailable

_PRIVATE_HTTP=ContextVar('qq_private_upload_http',default=False)
_FACTORY_LOCK=threading.Lock()


@contextmanager
def protected_http_logs():
    # Redact at LogRecord creation, before child or ancestor handlers see it.
    # A parent logger level alone cannot stop an explicitly DEBUG child. The
    # context flag affects this upload only, including transport close/errors.
    with _FACTORY_LOCK:
        previous=logging.getLogRecordFactory()
        if not getattr(previous,'_qq_private_http_factory',False):
            def factory(*args,**kwargs):
                record=previous(*args,**kwargs)
                if _PRIVATE_HTTP.get() and record.name.startswith(('httpcore2','httpx2')):
                    record.msg='protected_upload_transport'
                    record.args=()
                    record.exc_info=record.exc_text=record.stack_info=None
                return record
            factory._qq_private_http_factory=True
            logging.setLogRecordFactory(factory)
    marker=_PRIVATE_HTTP.set(True)
    try:yield
    finally:_PRIVATE_HTTP.reset(marker)


_HOST=re.compile(r'(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\Z')


@dataclass(frozen=True,repr=False)
class MultipartConfiguration:
    allowed_part_hosts: frozenset[str] = field(repr=False)
    max_parts: int = 128
    max_session_seconds: int = 600

    def __post_init__(self):
        if (type(self.allowed_part_hosts) is not frozenset or not 1<=len(self.allowed_part_hosts)<=16
                or type(self.max_parts) is not int or not 1<=self.max_parts<=128
                or type(self.max_session_seconds) is not int or not 1<=self.max_session_seconds<=600):
            raise ValueError('invalid_multipart_configuration')
        for host in self.allowed_part_hosts:
            if (type(host) is not str or not _HOST.fullmatch(host) or '..' in host or '.' not in host
                    or host.endswith(('.localhost','.local'))):
                raise ValueError('invalid_multipart_configuration')
            try:ipaddress.ip_address(host)
            except ValueError:continue
            raise ValueError('invalid_multipart_configuration')


class PinnedPartTransport:
    def __init__(self,configuration:MultipartConfiguration,*,resolver=resolve_addresses,transport_factory=None):
        if type(configuration) is not MultipartConfiguration:raise ValueError('invalid_multipart_configuration')
        self.configuration=configuration
        self.resolver,self.transport_factory=resolver,transport_factory

    async def put(self,part:UploadPart,content:bytes,*,timeout:float,before_dispatch,ensure_current):
        if (type(part) is not UploadPart or type(content) is not bytes or len(content)!=part.length
                or type(timeout) not in {int,float} or not 0<timeout<=30
                or not callable(before_dispatch) or not callable(ensure_current)):
            raise UploadGuardError()
        url=validate_part_url(part.presigned_url,allowed_hosts=self.configuration.allowed_part_hosts)
        # Reuse public DNS validation/pinning, not MCP's method/body limits.
        endpoint=httpx2.URL(url)
        pinned=PinnedHttpTransport(str(endpoint.copy_with(path="/",query=None)),HttpPolicy(),resolver=self.resolver,
            transport=self.transport_factory() if self.transport_factory else None)
        pinned.endpoint=endpoint
        response=None
        privacy=protected_http_logs()
        privacy.__enter__()
        try:
            async with asyncio.timeout(timeout):
                address=await pinned.address()
                request=httpx2.Request('PUT',endpoint.copy_with(host=address),content=content,
                    headers={'Host':endpoint.netloc.decode('ascii'),'Content-Type':'application/octet-stream',
                             'Content-Length':str(len(content)),'Accept-Encoding':'identity'},
                    extensions={'sni_hostname':endpoint.host,
                        'timeout':{'connect':min(5,timeout),'read':timeout,'write':timeout,'pool':min(2,timeout)}})
                await ensure_current()
                before_dispatch()
                response=await pinned.transport.handle_async_request(request)
                # Exact 200 only confirms a part. Do not consume/retain bodies,
                # signed URLs or remote diagnostics; 201/202/3xx stay unknown.
                return PutResponse(response.status_code)
        except (asyncio.CancelledError,RecipientEvidenceUnavailable):
            raise
        except Exception:
            raise UploadGuardError() from None
        finally:
            original=sys.exc_info()[1]
            cleanup_error=None
            try:
                if response is not None:await response.aclose()
            except BaseException as error:
                cleanup_error=error
            try:
                await pinned.aclose()
            except BaseException as error:
                if cleanup_error is None or isinstance(error,asyncio.CancelledError):
                    cleanup_error=error
            finally:
                privacy.__exit__(None,None,None)
            # Cleanup cannot erase cancellation/source control flow or expose a
            # signed transport exception. Both resources have been attempted.
            if isinstance(original,asyncio.CancelledError):
                pass
            elif isinstance(cleanup_error,asyncio.CancelledError):
                raise cleanup_error
            elif original is None and cleanup_error is not None:
                if not isinstance(cleanup_error,Exception):raise cleanup_error
                raise UploadGuardError() from None
