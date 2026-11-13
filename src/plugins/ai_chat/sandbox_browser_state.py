"""Guest-only browser state. Emit domains/results, never cookie/storage values."""
import argparse
import asyncio
import ipaddress
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

PROFILE = Path('/work/.gaoji/chromium')
DOMAIN_LIMIT = 200


class ParentDomainRequired(ValueError):
    def __init__(self, domains):
        self.domains = domains
        super().__init__('browser_logout_parent_domain_required')


def domain_name(value):
    if not isinstance(value,str):
        raise ValueError('invalid_domain')
    name = value.strip().removeprefix('.').rstrip('.').lower()
    try:
        return str(ipaddress.ip_address(name.removeprefix('[').removesuffix(']')))
    except ValueError:
        pass
    if name=='localhost':
        return name
    name = name.encode('idna').decode('ascii')
    if (len(name)>253 or len(name.split('.'))<2 or any(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',part) is None
            for part in name.split('.'))):
        raise ValueError('invalid_domain')
    return name


def domains(*, profile=PROFILE, now=None):
    """Guest-only read of an existing Chromium file, never bot persistence."""
    import sqlite3

    now = time.time() if now is None else now
    paths = [profile/'Default'/suffix for suffix in ('Network/Cookies','Cookies')]
    database = next((path for path in paths if path.is_file()),None)
    if database is None:
        return {'domains':[],'truncated':False}
    connection = sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('PRAGMA trusted_schema=OFF')
        deadline = time.monotonic()+5
        connection.set_progress_handler(lambda: int(time.monotonic()>deadline),1000)
        rows = connection.execute('''SELECT DISTINCT host_key FROM cookies
            WHERE expires_utc=0 OR expires_utc>? ORDER BY host_key LIMIT ?''',
            (int((now+11644473600)*1_000_000),DOMAIN_LIMIT+1)).fetchall()
        names = sorted({domain_name(row[0]) for row in rows})
        return {'domains':names[:DOMAIN_LIMIT],'truncated':len(rows)>DOMAIN_LIMIT}
    finally:
        connection.close()


def within(host, domain):
    return host==domain or host.endswith('.'+domain)


def origin_host(value):
    parsed = urlsplit(value)
    if parsed.scheme not in {'https','http'}:
        return None
    if parsed.username is not None or parsed.password is not None:
        raise ValueError('invalid_browser_origin')
    return domain_name(parsed.hostname)


async def logout(domain, *, context=None):
    domain = domain_name(domain)
    if context is None:
        from gaoji_browser import page
        context = (await page()).context
    # All values returned by Playwright stay inside this guest process. Never
    # print this state, return it, or include browser exceptions in diagnostics.
    state = await context.storage_state()
    parents = sorted({name for item in state['cookies'] if (name:=domain_name(item['domain']))!=domain
        and item['domain'].startswith('.') and within(domain,name)})
    if parents:
        # Do not quietly remove a parent site's other logins, or claim logout
        # while the requested subdomain still inherits its parent's cookie.
        raise ParentDomainRequired(parents)
    names = {domain}
    names.update(name for item in state['cookies'] if within(name:=domain_name(item['domain']),domain))
    origins = {scheme+'://'+('['+name+']' if ':' in name else name) for name in names for scheme in ('https','http')}
    origins.update(item['origin'] for item in state['origins']
        if (host:=origin_host(item['origin'])) is not None and within(host,domain))
    old_pages = list(context.pages)
    control = await context.new_page()
    session = None
    try:
        session = await context.new_cdp_session(control)
        for item in old_pages:
            host = origin_host(item.url)
            if host is not None and within(host,domain):
                parsed = urlsplit(item.url)
                origins.add(parsed.scheme+'://'+parsed.netloc)
                await item.close() # Stop live page JS before clearing site state.
        if len(origins)>DOMAIN_LIMIT*2:
            raise ValueError('browser_origins_limit')
        for origin in sorted(origins):
            await session.send('Storage.clearDataForOrigin',{'origin':origin,'storageTypes':'all'})
        await context.clear_cookies(domain=re.compile(r'(^|\.)'+re.escape(domain)+r'$'))
        if any(within(domain_name(item['domain']),domain) for item in await context.cookies()):
            raise RuntimeError('browser_logout_incomplete')
        return {'domain':domain,'cleared':True}
    finally:
        async with asyncio.timeout(3):
            try:
                if session is not None:
                    await session.detach()
            finally:
                await control.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    actions = parser.add_subparsers(dest='action',required=True)
    actions.add_parser('domains')
    actions.add_parser('logout').add_argument('domain')
    args = parser.parse_args(argv)
    try:
        if args.action=='domains':
            result = domains()
        else:
            async def bounded():
                async with asyncio.timeout(30):
                    return await logout(args.domain)
            result = asyncio.run(bounded())
        print(json.dumps(result,separators=(',',':')))
        return 0
    except ParentDomainRequired as error:
        print(json.dumps({'error':'browser_logout_parent_domain_required','domains':error.domains}))
        return 1
    except Exception:
        print(json.dumps({'error':'browser_state_unavailable'}))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
