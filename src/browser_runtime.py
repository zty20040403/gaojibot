"""Portable persistent browser sessions, independent of NoneBot and host services."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import re
import shutil
import socket
import stat
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class BrowserUnavailable(RuntimeError):
    pass


class BrowserPolicyError(RuntimeError):
    pass


@dataclass
class BrowserSession:
    context: Any
    page: Any
    touched_at: float
    lock: asyncio.Lock
    users: int = 0
    retiring: bool = False


class BrowserManager:
    """Scoped Playwright sessions; a project worker may bind one exact owner.

    Profile admission and cache flags are not filesystem quotas. The project
    runtime must still provide its quota volume and stop the browser before
    taking a snapshot or releasing that volume.
    """

    def __init__(
        self,
        profile_root: str | Path,
        *,
        timeout_seconds: int = 30,
        max_sessions: int = 3,
        idle_seconds: int = 1800,
        executable_path: str = "",
        allow_private_network: bool = False,
        bound_owner: str = "",
        max_profile_bytes: int = 256 * 1024**2,
        cache_bytes: int = 32 * 1024**2,
    ) -> None:
        if (
            type(max_profile_bytes) is not int
            or max_profile_bytes <= 0
            or type(cache_bytes) is not int
            or not 0 < cache_bytes <= max_profile_bytes
            or not isinstance(bound_owner, str)
        ):
            raise ValueError("Invalid browser profile limits or owner")
        self.profile_root = Path(profile_root)
        self.profile_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _directory(self.profile_root)
        self.bound_owner = bound_owner
        self.max_profile_bytes, self.cache_bytes = max_profile_bytes, cache_bytes
        self.timeout_ms = min(max(int(timeout_seconds), 5), 120) * 1000
        self.max_sessions = 1 if bound_owner else min(max(int(max_sessions), 1), 10)
        self.idle_seconds = max(int(idle_seconds), 60)
        self.executable_path = executable_path.strip()
        self.allow_private_network = bool(allow_private_network)
        self._playwright: Any = None
        self._sessions: dict[str, BrowserSession] = {}
        self._manager_lock: asyncio.Lock | None = None
        self._dns_cache: dict[str, tuple[float, bool]] = {}
        self._closed = False

    async def close(self) -> None:
        async with self._lock():
            self._closed = True
            errors = []
            for key in list(self._sessions):
                try:
                    await self._close_locked(key)
                except Exception as exc:
                    errors.append(exc)
            if errors:
                raise BrowserUnavailable(
                    "Browser shutdown is not confirmed"
                ) from errors[0]
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None

    async def _close_locked(self, key: str) -> bool:
        session = self._sessions.get(key)
        if session is None:
            return False
        session.retiring = True
        async with session.lock:
            async with asyncio.timeout(self.timeout_ms / 1000):
                await session.context.close()
        # Keep an uncertain close in the inventory: it is not safe to delete
        # the profile, reuse it, or report a drained project on a lost reply.
        self._sessions.pop(key)
        return True

    async def close_idle(self) -> int:
        cutoff = time.monotonic() - self.idle_seconds
        async with self._lock():
            stale = [
                key
                for key, session in self._sessions.items()
                if not session.users and session.touched_at < cutoff
            ]
            for key in stale:
                await self._close_locked(key)
        return len(stale)

    async def close_session(self, owner: str) -> bool:
        key = self._key(owner)
        async with self._lock():
            return await self._close_locked(key)

    async def clear_profile(self, owner: str) -> bool:
        """Close and delete only this owner's persistent browser state."""
        key = self._key(owner)
        profile = self._profile(key)
        async with self._lock():
            closed = await self._close_locked(key)
            existed = profile.exists()
            if existed or profile.is_symlink():
                _directory(profile)
                await asyncio.to_thread(shutil.rmtree, profile)
        return closed or existed

    def stats(self) -> dict[str, int]:
        profiles = (
            int(self.profile_root.is_dir())
            if self.bound_owner
            else sum(
                1
                for item in self.profile_root.iterdir()
                if item.is_dir() and not item.is_symlink()
            )
        )
        return {
            "active_sessions": len(self._sessions),
            "closing_sessions": sum(
                session.retiring for session in self._sessions.values()
            ),
            "persistent_profiles": profiles,
            "max_sessions": self.max_sessions,
        }

    async def navigate(self, owner: str, url: str) -> dict[str, object]:
        await self._assert_url_allowed(url)
        async with self._use(owner, create=True) as session:
            response = await session.page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            )
            session.touched_at = time.monotonic()
            return await self._snapshot_locked(
                session,
                status=(response.status if response is not None else None),
            )

    async def snapshot(self, owner: str) -> dict[str, object]:
        async with self._use(owner) as session:
            session.touched_at = time.monotonic()
            return await self._snapshot_locked(session)

    async def click(self, owner: str, ref: str) -> dict[str, object]:
        selector = _ref_selector(ref)
        async with self._use(owner) as session:
            await session.page.locator(selector).first.click(timeout=self.timeout_ms)
            await session.page.wait_for_timeout(300)
            session.touched_at = time.monotonic()
            return await self._snapshot_locked(session)

    async def type_text(
        self,
        owner: str,
        ref: str,
        text: str,
        *,
        submit: bool = False,
    ) -> dict[str, object]:
        selector = _ref_selector(ref)
        async with self._use(owner) as session:
            locator = session.page.locator(selector).first
            await locator.fill(str(text)[:10000], timeout=self.timeout_ms)
            if submit:
                await locator.press("Enter")
                await session.page.wait_for_timeout(300)
            session.touched_at = time.monotonic()
            return await self._snapshot_locked(session)

    async def press_key(self, owner: str, key: str) -> dict[str, object]:
        allowed = {
            "Enter",
            "Escape",
            "Tab",
            "ArrowUp",
            "ArrowDown",
            "ArrowLeft",
            "ArrowRight",
            "PageUp",
            "PageDown",
            "Home",
            "End",
        }
        if key not in allowed:
            raise BrowserPolicyError("unsupported browser key")
        async with self._use(owner) as session:
            await session.page.keyboard.press(key)
            await session.page.wait_for_timeout(200)
            session.touched_at = time.monotonic()
            return await self._snapshot_locked(session)

    async def scroll(self, owner: str, amount: int) -> dict[str, object]:
        bounded = min(max(int(amount), -5000), 5000)
        async with self._use(owner) as session:
            await session.page.evaluate("y => window.scrollBy(0, y)", bounded)
            await session.page.wait_for_timeout(200)
            session.touched_at = time.monotonic()
            return await self._snapshot_locked(session)

    async def wait_for(
        self, owner: str, text: str, timeout_seconds: int
    ) -> dict[str, object]:
        timeout_ms = min(max(int(timeout_seconds), 1), 60) * 1000
        async with self._use(owner) as session:
            await session.page.get_by_text(str(text)[:500], exact=False).first.wait_for(
                state="visible", timeout=timeout_ms
            )
            session.touched_at = time.monotonic()
            return await self._snapshot_locked(session)

    def _key(self, owner):
        if (
            not isinstance(owner, str)
            or not owner
            or (self.bound_owner and owner != self.bound_owner)
        ):
            raise BrowserPolicyError("Browser profile belongs to another owner")
        return _owner_key(owner)

    def _profile(self, key):
        return self.profile_root if self.bound_owner else self.profile_root / key

    @asynccontextmanager
    async def _use(self, owner, *, create=False):
        key = self._key(owner)
        async with self._lock():
            if self._closed:
                raise BrowserUnavailable("Browser manager is shutting down")
            session = self._sessions.get(key)
            if session is None and create:
                session = await self._open_locked(key)
            if session is None:
                raise BrowserUnavailable("no page is open; call browser_navigate first")
            if session.retiring:
                raise BrowserUnavailable("Browser close is still unconfirmed")
            session.users += 1
        try:
            async with session.lock:
                if session.retiring:
                    raise BrowserUnavailable("Browser session is closing")
                yield session
        finally:
            session.touched_at = time.monotonic()
            session.users -= 1

    async def _open_locked(self, key):
        cutoff = time.monotonic() - self.idle_seconds
        for stale, session in list(self._sessions.items()):
            if not session.users and session.touched_at < cutoff:
                await self._close_locked(stale)
        if len(self._sessions) >= self.max_sessions:
            idle = [
                (key, session)
                for key, session in self._sessions.items()
                if not session.users
            ]
            if not idle:
                raise BrowserUnavailable(
                    "All browser sessions are busy; retry after one finishes"
                )
            oldest_key, _ = min(idle, key=lambda item: item[1].touched_at)
            await self._close_locked(oldest_key)
        profile = self._profile(key)
        _directory(self.profile_root, create=True)
        _directory(profile, create=True)
        await asyncio.to_thread(_profile_size, profile, self.max_profile_bytes)
        await self._ensure_playwright()
        kwargs: dict[str, object] = {
            "user_data_dir": str(profile),
            "headless": True,
            "viewport": {"width": 1280, "height": 900},
            "locale": "zh-CN",
            "service_workers": "block",
            "accept_downloads": False,
            "args": [
                f"--disk-cache-size={self.cache_bytes}",
                f"--media-cache-size={self.cache_bytes}",
            ],
        }
        if self.executable_path:
            kwargs["executable_path"] = self.executable_path
        context = await self._playwright.chromium.launch_persistent_context(**kwargs)
        session = BrowserSession(context, None, time.monotonic(), asyncio.Lock())
        self._sessions[key] = session
        try:
            context.set_default_timeout(self.timeout_ms)
            await context.route("**/*", self._route_guard)
            session.page = (
                context.pages[0] if context.pages else await context.new_page()
            )
            return session
        except BaseException:
            await self._close_locked(key)
            raise

    def _lock(self) -> asyncio.Lock:
        if self._manager_lock is None:
            self._manager_lock = asyncio.Lock()
        return self._manager_lock

    async def _ensure_playwright(self) -> None:
        if self._playwright is not None:
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserUnavailable(
                "Playwright is not installed; install dependencies and run "
                "`playwright install chromium`"
            ) from exc
        self._playwright = await async_playwright().start()

    async def _route_guard(self, route: Any, request: Any) -> None:
        try:
            await self._assert_url_allowed(request.url)
        except BrowserPolicyError:
            await route.abort("blockedbyclient")
        else:
            await route.continue_()

    async def _assert_url_allowed(self, url: str) -> None:
        parsed = urlparse(str(url))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise BrowserPolicyError("only public http/https URLs are allowed")
        if self.allow_private_network:
            return
        host = parsed.hostname.rstrip(".").casefold()
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            raise BrowserPolicyError("private-network browser access is disabled")
        cached = self._dns_cache.get(host)
        if cached is not None and cached[0] > time.monotonic():
            if not cached[1]:
                raise BrowserPolicyError("URL resolves to a private network")
        try:
            direct_ip = ipaddress.ip_address(host)
            public = direct_ip.is_global
        except ValueError:
            loop = asyncio.get_running_loop()
            try:
                records = await loop.getaddrinfo(
                    host,
                    parsed.port or (443 if parsed.scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
            except OSError as exc:
                raise BrowserPolicyError(f"browser DNS lookup failed: {exc}") from exc
            addresses = {item[4][0] for item in records}
            public = bool(addresses) and all(
                ipaddress.ip_address(address).is_global for address in addresses
            )
        if not public:
            self._dns_cache[host] = (time.monotonic() + 60, False)
            raise BrowserPolicyError("URL resolves to a private network")
        self._dns_cache.pop(host, None)

    async def _snapshot_locked(
        self,
        session: BrowserSession,
        *,
        status: int | None = None,
    ) -> dict[str, object]:
        page = session.page
        elements = await page.evaluate(
            """
            () => {
              const nodes = [...document.querySelectorAll(
                'a,button,input,textarea,select,[role="button"],[contenteditable="true"]'
              )].filter(el => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.visibility !== 'hidden';
              }).slice(0, 80);
              return nodes.map((el, index) => {
                const ref = `b${index + 1}`;
                el.setAttribute('data-bot-ref', ref);
                const text = (el.innerText || el.value || el.getAttribute('aria-label') ||
                  el.getAttribute('placeholder') || el.title || '').trim().slice(0, 200);
                return {ref, tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '', text};
              });
            }
            """
        )
        visible = await page.locator("body").inner_text(timeout=self.timeout_ms)
        return {
            "url": page.url,
            "title": (await page.title())[:300],
            "status": status,
            "text": _compact_text(visible, 12000),
            "elements": elements,
        }


def _ref_selector(ref: str) -> str:
    value = str(ref).strip()
    if re.fullmatch(r"b[1-9][0-9]*", value) is None:
        raise BrowserPolicyError("invalid browser element reference")
    return f'[data-bot-ref="{value}"]'


def _directory(path, *, create=False):
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise BrowserPolicyError("Browser profile must be a real directory")


def _profile_size(path, maximum):
    """Bound launch-time accounting without following profile-controlled links."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root = os.open(path, flags)
    total, count = 0, 0
    deadline = time.monotonic() + 5
    device = os.fstat(root).st_dev

    def walk(fd, depth=0):
        nonlocal total, count
        if depth > 64:
            raise BrowserPolicyError("Browser profile nesting is too deep")
        before = os.fstat(fd)
        with os.scandir(fd) as entries:
            for entry in entries:
                count += 1
                if count > 100000 or time.monotonic() > deadline:
                    raise BrowserUnavailable(
                        "Browser profile accounting exceeded its budget"
                    )
                info = entry.stat(follow_symlinks=False)
                if info.st_dev != device:
                    raise BrowserPolicyError(
                        "Browser profile crossed a filesystem boundary"
                    )
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(entry.name, flags, dir_fd=fd)
                    try:
                        opened = os.fstat(child)
                        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                            raise BrowserPolicyError(
                                "Browser profile directory changed"
                            )
                        walk(child, depth + 1)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    total += max(info.st_size, getattr(info, "st_blocks", 0) * 512)
                elif (
                    depth == 0
                    and stat.S_ISLNK(info.st_mode)
                    and entry.name
                    in {
                        "SingletonLock",
                        "SingletonCookie",
                        "SingletonSocket",
                    }
                ):
                    # Chromium owns these process locks. Never follow/remove
                    # them here or mistake them for reusable profile files.
                    total += info.st_size
                else:
                    raise BrowserPolicyError("Browser profile contains an unsafe file")
                if total > maximum:
                    raise BrowserUnavailable(
                        "Browser profile exceeds its startup size limit"
                    )
        after = os.fstat(fd)
        if (before.st_ino, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_ino,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise BrowserPolicyError("Browser profile changed during accounting")

    try:
        walk(root)
        current = path.lstat()
        opened = os.fstat(root)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise BrowserPolicyError("Browser profile was replaced")
        return total
    finally:
        os.close(root)


def _owner_key(owner: str) -> str:
    return hashlib.sha256(str(owner).encode("utf-8")).hexdigest()[:24]


def _compact_text(text: str, limit: int) -> str:
    value = re.sub(r"[ \t]+", " ", str(text))
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    return value if len(value) <= limit else value[:limit].rstrip() + "\n[truncated]"
