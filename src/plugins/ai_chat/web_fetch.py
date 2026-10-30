"""Read one public web page as plain text, so search results can be verified."""

from __future__ import annotations

import asyncio
import codecs
from dataclasses import dataclass
from html.parser import HTMLParser
import ipaddress
import re
import socket

import httpx

MAX_PAGE_BYTES = 3 * 1024 * 1024
MAX_REDIRECTS = 5
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
_SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe", "canvas"}
_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer", "table", "ul", "ol", "pre",
    "blockquote", "dd", "dt", "hr",
}
_META_CHARSET = re.compile(rb"""charset=["']?([A-Za-z0-9_-]+)""", re.IGNORECASE)


class FetchError(RuntimeError):
    """A failure whose message is safe to show the model."""


@dataclass(frozen=True)
class FetchedPage:
    url: str
    title: str
    text: str


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: list[str] = []
        self.parts: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in _SKIP_TAGS:
            self._skip = max(self._skip - 1, 0)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title.append(data)
        elif not self._skip:
            self.parts.append(data)


async def public_address(host: str, port: int) -> str:
    """Resolve once and refuse anything but public addresses (no LAN, Tailscale or metadata)."""
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            async with asyncio.timeout(5):
                rows = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (OSError, TimeoutError) as exc:
            raise FetchError("域名解析失败。") from exc
        addresses = sorted({str(row[4][0]) for row in rows})
    if not addresses:
        raise FetchError("域名解析失败。")
    for value in addresses:
        address = ipaddress.ip_address(value.split("%", 1)[0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if not address.is_global:
            raise FetchError("只能读取公网网页。")
    return addresses[0]


def html_to_text(html: str) -> tuple[str, str]:
    parser = _TextParser()
    parser.feed(html)
    parser.close()
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    kept: list[str] = []
    for line in lines:
        if line and (not kept or kept[-1] != line):
            kept.append(line)
    return " ".join("".join(parser.title).split()), "\n".join(kept)


def _decode(body: bytes, content_type: str) -> str:
    match = _META_CHARSET.search(content_type.encode("latin-1", "ignore")) or _META_CHARSET.search(body[:4096])
    encoding = match.group(1).decode("ascii").lower() if match else "utf-8"
    if encoding in {"gb2312", "gbk"}:
        encoding = "gb18030"
    try:
        codecs.lookup(encoding)
    except LookupError:
        encoding = "utf-8"
    return body.decode(encoding, errors="replace")


async def fetch_page(
    url: str,
    *,
    timeout_seconds: float = 15,
    resolver=public_address,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FetchedPage:
    try:
        current = httpx.URL(url.strip())
    except httpx.InvalidURL as exc:
        raise FetchError("网址格式不对。") from exc
    async with httpx.AsyncClient(
        timeout=timeout_seconds, follow_redirects=False, trust_env=False, headers=_HEADERS,
        transport=transport,
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            if current.scheme not in {"http", "https"} or not current.host:
                raise FetchError("只支持 http/https 网址。")
            https = current.scheme == "https"
            address = await resolver(current.host, current.port or (443 if https else 80))
            # Connect to the address that was checked, so DNS cannot swap in a private one.
            request = client.build_request(
                "GET", current.copy_with(host=address),
                headers={"Host": current.netloc.decode("ascii")},
                extensions={"sni_hostname": current.host} if https else {},
            )
            response = await client.send(request, stream=True)
            try:
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    if not location:
                        raise FetchError("网页跳转缺少目标地址。")
                    current = current.join(location)
                    continue
                if response.status_code >= 400:
                    raise FetchError(f"网页返回 HTTP {response.status_code}。")
                content_type = response.headers.get("content-type", "").lower()
                kind = content_type.split(";", 1)[0].strip()
                if kind and not (kind.startswith("text/") or kind in {
                    "application/xhtml+xml", "application/json", "application/xml",
                }):
                    raise FetchError(f"这是 {kind} 文件，不是网页；需要时下载到沙盒里处理。")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_PAGE_BYTES:
                        break
            finally:
                await response.aclose()
            text = _decode(bytes(body[:MAX_PAGE_BYTES]), content_type)
            if kind in {"", "text/html", "application/xhtml+xml"}:
                title, text = html_to_text(text)
            else:
                title = ""
            return FetchedPage(str(current), title, text)
    raise FetchError("网页跳转次数过多。")
