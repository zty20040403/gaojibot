"""Public guest search; metrics go to the guest journal, not host fallback logs."""
import asyncio
import json
import logging
import re
import sys
import syslog
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

# host, path, query parameter, card, link, title, snippet
ENGINES = {
    'bing': ('www.bing.com', '/search', 'q', 'li.b_algo', 'h2 a', 'h2', '.b_caption p'),
    'baidu': ('www.baidu.com', '/s', 'wd', '.result, .c-container', 'h3 a', 'h3', '.c-abstract, .content-right_8Zs40'),
    'bilibili': ('search.bilibili.com', '/all', 'keyword', '.bili-video-card', 'a:has(h3), h3 a', 'h3', '.bili-video-card__info--desc'),
    'xiaohongshu': ('www.xiaohongshu.com', '/search_result', 'keyword', '.note-item', 'a.cover, a.title', '.title', '.desc'),
    'zhihu': ('www.zhihu.com', '/search', 'q', '.SearchResult-Card', '.ContentItem-title a', '.ContentItem-title', '.RichContent-inner'),
}
CAPTCHA = 'input[name="captcha"], iframe[src*="captcha"], #captcha, .captcha, #verify, .geetest_panel'
SENSITIVE = re.compile(r'(?:token|secret|password|session|cookie|authorization|credential|api[_-]?key)', re.I)
EXTRACT = '''([card, link, title, snippet, captcha]) => {
    if (document.querySelector(captcha) || /captcha|验证码|安全验证/i.test(document.title)) return null;
    return Array.from(document.querySelectorAll(card)).slice(0, 100).map(item => ({
        url: item.querySelector(link)?.getAttribute('href') || '',
        title: item.querySelector(title)?.textContent || '',
        snippet: item.querySelector(snippet)?.textContent || ''
    }));
}'''


def allowed_url(url, engine):
    part = urlsplit(url)
    return (part.scheme == 'https' and (part.hostname, part.path) == ENGINES[engine][:2]
            and part.port in (None, 443) and part.username is None and part.password is None)


def result_url(url, base):
    if not url.strip() or url.lstrip().startswith('#') or len(url) > 8192 or any(ord(c) < 32 for c in url):
        return None
    try:
        part = urlsplit(urljoin(base, url))
        if (part.scheme not in ('http', 'https') or not part.hostname or part.username is not None
                or part.password is not None or part.port == 0):
            return None
        query = [(k, v) for k, v in parse_qsl(part.query, keep_blank_values=True, max_num_fields=100)
                 if k.lower() not in {'auth', 'key', 'sid'} and not SENSITIVE.search(k)]
        return urlunsplit((part.scheme, part.netloc, part.path or '/', urlencode(query), ''))
    except ValueError:
        return None


def error(code):
    return {'status': 'error', 'error': code, 'results': []}


async def search(engine, query, connect):
    status = 'error'
    try:
        result = await _search(engine, query, connect)
        status = result['status']
        return result
    except TimeoutError:
        return error('timeout')
    except Exception as exc:
        return error('invalid_arguments' if isinstance(exc, UnicodeError) else 'browser_error')
    finally:
        name = engine if isinstance(engine, str) and engine in ENGINES else 'invalid'
        try:
            syslog.syslog(syslog.LOG_INFO, f'gaoji_search engine={name} status={status}')
        except OSError:
            logging.warning('gaoji_search syslog unavailable')


async def _search(engine, query, connect):
    if (not isinstance(engine, str) or engine not in ENGINES or not isinstance(query, str)
            or not query.strip() or len(query) > 512 or '\x00' in query):
        return error('invalid_arguments')
    host, path, param, card, link, title, snippet = ENGINES[engine]
    url = f'https://{host}{path}?' + urlencode({param: query})
    captcha_redirect = False
    async with asyncio.timeout(20):
        context = await (await connect()).new_context(service_workers='block')
        try:
            async def route_request(route):
                nonlocal captcha_redirect
                request = route.request
                target = urlsplit(request.url)
                if request.is_navigation_request() and not allowed_url(request.url, engine):
                    if target.hostname == host and re.search(r'captcha|verify|/sorry', target.path, re.I):
                        captcha_redirect = True
                    await route.abort()
                elif request.method != 'GET' or target.scheme not in ('http', 'https'):
                    await route.abort()
                else:
                    await route.continue_()
            await context.route('**/*', route_request)
            async def close_socket(socket):
                await socket.close()
            await context.route_web_socket('**/*', close_socket)
            page = await context.new_page()
            try:
                response = await page.goto(url, wait_until='domcontentloaded', timeout=15000)
            except Exception:
                return {'status': 'captcha', 'results': []} if captcha_redirect else error('navigation_error')
            if captcha_redirect:
                return {'status': 'captcha', 'results': []}
            if not allowed_url(page.url, engine):
                return error('unexpected_url')
            deadline = asyncio.timeout(3)
            try:
                async with deadline:
                    await page.locator(f'{card}, {CAPTCHA}').first.wait_for(timeout=0)
            except TimeoutError:
                if not deadline.expired():
                    raise
            if not allowed_url(page.url, engine):
                return error('unexpected_url')
            data = await page.evaluate(EXTRACT, [card, link, title, snippet, CAPTCHA])
            if not allowed_url(page.url, engine):
                return error('unexpected_url')
            if captcha_redirect or data is None:
                return {'status': 'captcha', 'results': []}
            if response is None or response.status >= 400:
                return error('http_error')
            results, seen = [], set()
            for item in data:
                target = result_url(item['url'], page.url)
                text = item['title'].strip()[:300]
                if target and text and target not in seen:
                    seen.add(target)
                    results.append({'title': text, 'url': target, 'snippet': item['snippet'].strip()[:800]})
                if len(results) == 10:
                    break
            return {'status': 'ok' if results else 'empty', 'results': results}
        finally:
            await asyncio.wait_for(context.close(), 3)


if __name__ == '__main__':
    import gaoji_browser
    args = json.loads(sys.argv[1])
    print(json.dumps(asyncio.run(gaoji_browser.search(*args)), ensure_ascii=False))
