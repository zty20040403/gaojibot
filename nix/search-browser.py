"""Public search over the systemd Unix socket. The browser is never exposed over CDP."""
import asyncio
import socket
import sys

from aiohttp import web
from sandbox_search import search


def application(connect):
    slots = asyncio.Semaphore(4)

    async def lookup(request):
        if slots.locked():
            raise web.HTTPServiceUnavailable()
        async with slots, asyncio.timeout(9):
            try:
                query = (await request.read()).decode('utf-8')
            except UnicodeDecodeError:
                raise web.HTTPBadRequest() from None
            if not query.strip() or len(query) > 1500 or '\x00' in query:
                raise web.HTTPBadRequest()
            for engine in ('bing', 'baidu'):
                try:
                    async with asyncio.timeout(4):
                        reply = await search(engine, query, connect, documents_only=True)
                except TimeoutError:
                    continue
                if reply['status'] == 'ok':
                    return web.json_response(reply['results'])
                if engine == 'baidu' and reply['status'] == 'empty':
                    return web.json_response([])
            raise web.HTTPBadGateway()

    app = web.Application(client_max_size=6001)
    app.router.add_post('/search', lookup)
    return app


async def serve(executable):
    from playwright.async_api import async_playwright

    with socket.socket(fileno=3) as listener:
        if listener.family != socket.AF_UNIX:
            raise RuntimeError('expected a Unix search socket')
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=executable, chromium_sandbox=True,
                args=['--disable-background-networking', '--disable-extensions'])
            disconnected = asyncio.Event()
            browser.on('disconnected', lambda _: disconnected.set())
            async def connect():
                return browser
            runner = web.AppRunner(application(connect), access_log=None, handler_cancellation=True, keepalive_timeout=2)
            await runner.setup()
            try:
                await web.SockSite(runner, listener, backlog=16).start()
                await disconnected.wait()
                raise RuntimeError('search browser disconnected')
            finally:
                await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(serve(sys.argv[1]))
