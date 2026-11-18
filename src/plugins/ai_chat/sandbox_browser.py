"""Import as gaoji_browser inside a conversation VM; no host credentials."""
import asyncio
import os
from pathlib import Path
import shutil
import subprocess

_driver = _browser = None


async def page():
    from playwright.async_api import async_playwright
    global _driver, _browser
    try:
        Path('/run/gaoji-browser.used').touch()
    except OSError:
        pass
    if _browser is not None and not _browser.is_connected():
        _browser = None
        if _driver is not None:
            try:
                await _driver.stop()
            except Exception:
                pass
            _driver = None
    if _browser is None:
        _driver = await async_playwright().start()
        try:
            _browser = await _driver.chromium.connect_over_cdp('http://127.0.0.1:9222', timeout=2000)
        except Exception:
            if os.geteuid() == 0 and Path('/run/systemd/system').is_dir() and shutil.which('systemctl'):
                proc = await asyncio.create_subprocess_exec(
                    'systemctl', 'start', 'gaoji-browser.service',
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                )
                _, err = await proc.communicate()
                if proc.returncode == 0:
                    _browser = await _driver.chromium.connect_over_cdp('http://127.0.0.1:9222', timeout=5000)
                else:
                    raise RuntimeError('Chromium 服务启动失败：' + err.decode(errors='replace').strip()) from None
            else:
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


async def wait_for_login(check, *, timeout=180, interval=3):
    """A site-specific boolean check; neither QR disappearance nor cookies suffice."""
    if (type(timeout) not in (int,float) or not 0<timeout<=180
            or type(interval) not in (int,float) or not 0<interval<=10):
        raise ValueError('登录检查最多180秒，间隔必须大于0且不超过10秒。')
    deadline = asyncio.timeout(timeout)
    try:
        async with deadline:
            while True:
                authenticated = await check()
                if type(authenticated) is not bool:
                    raise TypeError('登录检查必须返回真实登录标志的布尔结果。')
                if authenticated:
                    return True
                await asyncio.sleep(interval)
    except TimeoutError:
        if not deadline.expired():
            raise
        return False


async def snapshot(current):
    tree = await current.locator('body').aria_snapshot()
    numbered = '\n'.join(f'{i}: {line}' for i, line in enumerate(tree.splitlines(), 1))
    return f'{await current.title()}\n{current.url}\n{numbered}'
