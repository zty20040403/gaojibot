"""Bounded, credential-free reads using the existing pinned HTTP transport.

No automatic redirect, HTML execution, extraction of archives, auth headers or
MCP session exists here. URLs originate only from an exact scoped ledger node.
"""
import asyncio
from dataclasses import dataclass, field
import json
from urllib.parse import urlsplit

import httpx2

from src.plugins.ai_chat.message_ir import CardNode, MediaNode, TextNode
from src.plugins.ai_chat.pinned_http import (
    HttpPolicy, MAX_RESPONSE_BYTES, McpTransportDenied, PinnedHttpTransport,
)
from .media_refs import ExternalText, MediaDenied, bounded_text, text_link


@dataclass(frozen=True)
class DownloadedContent:
    content: bytes = field(repr=False)
    declared_mime: str


class PinnedMediaTransport(PinnedHttpTransport):
    """Reuse exact-request pin/SSRF/stream checks, allowing signed URL query.

    MCP config deliberately forbids queries. This media-only constructor
    validates the complete content URL, then preserves its query verbatim;
    MCP configuration and its credential resolution remain unchanged.
    """
    def __init__(self, url, **kwargs):
        if (not isinstance(url, str) or not 1 <= len(url) <= 8192
                or any(ord(c) <= 32 or ord(c) == 127 for c in url)):
            raise MediaDenied('invalid_media_url')
        try:
            parsed = urlsplit(url)
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.fragment):
                raise ValueError
            _ = parsed.port
            endpoint = httpx2.URL(url)
            super().__init__(str(endpoint.copy_with(query=None)), HttpPolicy(), **kwargs)
            self.endpoint = endpoint
        except (TypeError, ValueError):
            raise MediaDenied('invalid_media_url') from None


class PinnedContentReader:
    def __init__(self, *, transport_factory=None, max_bytes=MAX_RESPONSE_BYTES, timeout=15):
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_RESPONSE_BYTES:
            raise MediaDenied('invalid_download_limit')
        if type(timeout) is not int or not 1 <= timeout <= 30:
            raise MediaDenied('invalid_download_timeout')
        self.transport_factory = transport_factory or (
            lambda url: PinnedMediaTransport(url))
        self.max_bytes, self.timeout = max_bytes, timeout

    async def fetch(self, source, *, resolver, audio=False):
        await resolver.require(source)
        node = source.node
        if isinstance(node, TextNode):
            url = text_link(node)
            if url is None:
                raise MediaDenied('unambiguous_message_link_required')
        elif isinstance(node, CardNode):
            url = node.url
        elif isinstance(node, MediaNode):
            url = node.raw_data.get('voice_wav_url') if audio else None
            url = url or node.source
            declared_size = node.raw_data.get('size')
            if declared_size is not None and (type(declared_size) is not int
                    or not 0 <= declared_size <= self.max_bytes):
                raise MediaDenied('download_size_limit')
        else:
            raise MediaDenied('media_reference_unavailable')
        response, transport = None, None
        try:
            async with asyncio.timeout(self.timeout):
                transport = self.transport_factory(url)
                await resolver.require(source)
                # Low-level transport intentionally avoids AsyncClient URL INFO
                # logging. Never share the QQ REST client or cookie jar.
                response = await transport.handle_async_request(httpx2.Request(
                    'GET', url, headers={'Accept-Encoding': 'identity'}))
                if response.status_code != 200:
                    raise MediaDenied('download_http_rejected')
                encoding = response.headers.get('content-encoding', 'identity').lower()
                if encoding != 'identity':
                    raise MediaDenied('download_encoding_rejected')
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    await resolver.require(source)
                    if len(data) + len(chunk) > self.max_bytes:
                        raise MediaDenied('download_size_limit')
                    data.extend(chunk)
                if not data:
                    raise MediaDenied('download_empty')
                await resolver.require(source)
                mime = response.headers.get('content-type', '').split(';', 1)[0].lower()
                return DownloadedContent(bytes(data), mime[:128])
        except MediaDenied:
            raise
        except (ValueError, OSError, TimeoutError, httpx2.HTTPError, McpTransportDenied):
            raise MediaDenied('download_unavailable') from None
        finally:
            if response is not None:
                await response.aclose()
            if transport is not None:
                await transport.aclose()


def read_text_document(download):
    """First slice: strict UTF-8 text/JSON only. No HTML/PDF parser on host.

    PDF/ZIP remain opaque until an already configured bounded project/sandbox
    reader provides independent validation. Header/extension cannot enable it.
    """
    if download.declared_mime not in {'text/plain', 'text/markdown', 'application/json'}:
        raise MediaDenied('document_reader_unavailable')
    try:
        text = download.content.decode('utf-8')
        if download.declared_mime == 'application/json':
            json.loads(text)
    except (ValueError, RecursionError):
        raise MediaDenied('document_content_invalid') from None
    text, truncated = bounded_text(text)
    return ExternalText('attachment_text', text, truncated)
