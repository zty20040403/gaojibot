"""Controller-only file settings. Imported evidence is never generated here.

The runtime rechecks live route/source authority and expiry on every effect.
Saving settings or passing the HTTPS self-probe is not target acceptance.
"""
from __future__ import annotations

from copy import deepcopy
import ipaddress
import json
import re
from urllib.parse import urlsplit

from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey
from .validation import InstanceError

PRIVATE_DIRECTORY = '/data/state/qq-official-files'
NATIVE_CONTRACT = 'qq-autogen-2026-08-10-zero-v1'
DOWNLOAD_CONTRACT = 'controlled-download-v1'
_DNS_LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z')


def _fail():
    raise InstanceError('Invalid official file configuration or acceptance evidence')


def _int(value, low=1, high=2**63-1):
    if type(value) is not int or not low <= value <= high:
        _fail()
    return value


def _origin(value):
    try:
        if type(value) is not str or len(value) > 2048:
            _fail()
        url = urlsplit(value)
        if (url.scheme != 'https' or not url.hostname or url.port not in {None, 443}
                or url.username is not None or url.password is not None or url.path
                or '?' in value or '#' in value or '\\' in value
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
                or url.hostname in {'localhost', 'localhost.localdomain'}):
            _fail()
        try:
            address = ipaddress.ip_address(url.hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            _fail()
        return value
    except (TypeError, ValueError, AttributeError):
        _fail()


def _routes(value, *, native, account=None):
    if type(value) is not list or len(value) > 128:
        _fail()
    common = {'endpoint', 'endpoint_generation', 'route_epoch', 'capability_revision',
              'verified_at', 'expires_at', 'contract'}
    keys = common | ({'file_type', 'max_bytes'} if native else set())
    optional = {'multipart'} if native else set()
    seen = set()
    for row in value:
        if type(row) is not dict or not keys <= set(row) or set(row) - keys - optional:
            _fail()
        try:
            endpoint = EndpointKey.parse(row['endpoint'])
        except (ValueError, TypeError, KeyError):
            _fail()
        if (endpoint.account.platform != 'qq-official' or row['endpoint'] != endpoint.key
                or account is not None and endpoint.account != account):
            _fail()
        for key in ('endpoint_generation', 'route_epoch', 'capability_revision'):
            _int(row[key])
        _int(row['verified_at'], 0)
        _int(row['expires_at'], row['verified_at'] + 1)
        if row['contract'] != (NATIVE_CONTRACT if native else DOWNLOAD_CONTRACT):
            _fail()
        kind = None
        if native:
            kind = _int(row['file_type'], 1, 4)
            if kind not in {1, 4}:
                _fail()
            _int(row['max_bytes'], 1, 20 * 1024 * 1024)
            if type(row.get('multipart', False)) is not bool or row.get('multipart', False) and kind != 4:
                _fail()
        identity = endpoint.key, kind
        if identity in seen:
            _fail()
        seen.add(identity)
    return deepcopy(value)


def _multipart(value):
    # This protected host policy is separate from target acceptance evidence.
    # Do not normalize a URL, wildcard or case into a different trusted host.
    if (type(value) is not dict or 'allowed_part_hosts' not in value
            or set(value) - {'allowed_part_hosts', 'max_parts', 'max_session_seconds'}):
        _fail()
    hosts = value['allowed_part_hosts']
    if type(hosts) is not list or not 1 <= len(hosts) <= 16:
        _fail()
    seen = set()
    for host in hosts:
        if (type(host) is not str or not 1 <= len(host) <= 253 or '.' not in host
                or host == 'localhost.localdomain' or all(label.isdecimal() for label in host.split('.'))
                or host.endswith(('.local', '.localhost'))
                or any(not _DNS_LABEL.fullmatch(label) for label in host.split('.'))):
            _fail()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            _fail()
        if host in seen:
            _fail()
        seen.add(host)
    _int(value.get('max_parts', 128), 1, 128)
    _int(value.get('max_session_seconds', 600), 1, 600)
    return deepcopy(value)


def file_settings(value=None, *, account: AccountKey | None = None):
    if value is None:
        return dict(enabled=False, generation_enabled=False, native_routes=[], download=None)
    required = {'enabled', 'generation_enabled', 'native_routes', 'download'}
    if type(value) is not dict or not required <= set(value) or set(value) - required - {'multipart'}:
        _fail()
    if type(value['enabled']) is not bool or type(value['generation_enabled']) is not bool:
        _fail()
    # Bounded private JSON; no host paths, inline credentials or bearer URLs.
    try:
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > 262144:
            _fail()
    except (ValueError, TypeError, UnicodeError):
        _fail()
    result = deepcopy(value)
    result['native_routes'] = _routes(value['native_routes'], native=True, account=account)
    if 'multipart' in value:
        result['multipart'] = _multipart(value['multipart'])
    download = result['download']
    if download is not None:
        if type(download) is not dict or set(download) != {'origin', 'listen_host', 'listen_port', 'routes'}:
            _fail()
        _origin(download['origin'])
        if download['listen_host'] not in {'127.0.0.1', '::1'}:
            _fail()
        _int(download['listen_port'], 1024, 65535)
        if download['listen_port'] == 8080:
            _fail()  # Existing bot listener owns this port in the same namespace.
        download['routes'] = _routes(download['routes'], native=False, account=account)
    return result


def host_file_configuration(value, *, account: AccountKey):
    settings = file_settings(value, account=account)
    if not settings['enabled']:
        return None
    result = dict(private_directory=PRIVATE_DIRECTORY, native_routes=settings['native_routes'],
                  generation_enabled=settings['generation_enabled'], min_free_bytes=64 * 1024 * 1024)
    if settings['download'] is not None:
        result['download'] = settings['download']
    if 'multipart' in settings:
        result['multipart'] = settings['multipart']
    document=dict(version=1,account=account.key,onebot_accounts=[],files=result)
    if len(json.dumps(document,ensure_ascii=False,separators=(',',':')).encode('utf-8')) > 16384:
        raise InstanceError('Official runtime configuration exceeds the protected file size limit')
    return result
