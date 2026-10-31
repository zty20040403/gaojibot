"""Import as gaoji_browser inside a conversation VM; no host credentials."""
import asyncio
from pathlib import Path
import shutil
import subprocess

from playwright.async_api import async_playwright

_driver = _browser = None


async def page():
    global _driver, _browser
    if _browser is None:
        _driver = await async_playwright().start()
        try:
            _browser = await _driver.chromium.connect_over_cdp('http://127.0.0.1:9222', timeout=2000)
        except Exception:
            profile = Path('/work/.gaoji/chromium')
            profile.mkdir(parents=True, exist_ok=True)
            executable = shutil.which('chromium') or shutil.which('chromium-browser')
            if executable is None:
                raise RuntimeError('Chromium 未安装，请安装 chromium 软件包。') from None
            log = (profile.parent / 'chromium.log').open('ab')
            subprocess.Popen([executable, '--headless', '--no-sandbox', '--disable-dev-shm-usage',
                '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=9222',
                '--user-data-dir=' + str(profile), 'about:blank'], stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, start_new_session=True)
            log.close()
            for attempt in range(20):
                await asyncio.sleep(.25)
                try:
                    _browser = await _driver.chromium.connect_over_cdp('http://127.0.0.1:9222', timeout=1000)
                    break
                except Exception:
                    if attempt == 19:
                        raise
    context = _browser.contexts[0]
    return context.pages[-1] if context.pages else await context.new_page()


async def snapshot(current):
    tree = await current.locator('body').aria_snapshot()
    numbered = '\n'.join(f'{i}: {line}' for i, line in enumerate(tree.splitlines(), 1))
    return f'{await current.title()}\n{current.url}\n{numbered}'
