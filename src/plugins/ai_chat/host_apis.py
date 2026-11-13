"""Operator-owned API declarations and canonical path rules; no VM secrets.

An allow entry is METHOD /path-prefix, matched on a path-segment boundary.
Omitting allow permits all supported methods; an empty list permits none.
"""
from dataclasses import dataclass, field
import re
from pathlib import Path
from urllib.parse import unquote_to_bytes, urlsplit

from src.qq_official.credentials import _read_secret

METHODS = frozenset({'GET','HEAD','POST','PUT','PATCH','DELETE','OPTIONS'})
HOP_HEADERS = frozenset({'connection','keep-alive','proxy-authenticate','proxy-authorization',
    'te','trailer','transfer-encoding','upgrade','host','content-length','expect','accept-encoding'})
_NAME = re.compile(r'[a-z0-9][a-z0-9-]{0,47}\Z')
_HEADER = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_ESCAPE = re.compile(br'%(?:2e|2f|5c|25)|%(?![0-9a-f]{2})',re.I)


class ApiDenied(ValueError):
    """Only fixed diagnostics; no URL, query, credential value or file path."""


def canonical_path(raw):
    if type(raw) is not bytes or not 1<=len(raw)<=8192 or _ESCAPE.search(raw):
        raise ApiDenied('invalid_api_path')
    try:
        path = unquote_to_bytes(raw).decode('utf-8')
    except UnicodeError:
        raise ApiDenied('invalid_api_path') from None
    if (not path.startswith('/') or '//' in path or any(part in {'.','..'} for part in path.split('/'))
            or any(ord(c)<=32 or ord(c)==127 or c in '\\?#;' for c in path)):
        raise ApiDenied('invalid_api_path')
    return path


def _mapping(raw, *, headers):
    if type(raw) is not dict or len(raw)>64:
        raise ApiDenied('invalid_api_configuration')
    result = {}
    for key,value in raw.items():
        if (type(key) is not str or not 1<=len(key)<=100 or type(value) is not str or len(value)>4096
                or any(ord(c)<32 or ord(c)==127 for c in key+value)):
            raise ApiDenied('invalid_api_configuration')
        if headers:
            key = key.lower()
            if not _HEADER.fullmatch(key) or key in HOP_HEADERS or key in result:
                raise ApiDenied('invalid_api_configuration')
        result[key] = value
    return result


@dataclass(frozen=True)
class HostAPI:
    name: str
    base_url: str = field(repr=False)
    secret_file: str = field(repr=False)
    headers: dict = field(repr=False)
    query: dict = field(repr=False)
    allow: tuple | None
    per_hour: int
    about: str = field(repr=False)

    @classmethod
    def parse(cls, name, raw):
        if type(name) is not str or not _NAME.fullmatch(name) or type(raw) is not dict:
            raise ApiDenied('invalid_api_configuration')
        if (not {'base_url','secret_file'}<=raw.keys()
                or raw.keys()-{'base_url','secret_file','headers','query','allow','per_hour','about'}):
            raise ApiDenied('invalid_api_configuration')
        base,secret_file = raw['base_url'],raw['secret_file']
        try:
            if type(base) is not str or not 1<=len(base)<=4096 or re.search(r'\s|\\',base):
                raise ValueError
            url = urlsplit(base)
            if (url.scheme!='https' or not url.hostname or url.username is not None or url.password is not None
                    or url.query or url.fragment or '{' in base or '}' in base):
                raise ValueError
            _ = url.port
            canonical_path((url.path or '/').encode())
            if type(secret_file) is not str or not 1<=len(secret_file)<=4096 or not Path(secret_file).is_absolute():
                raise ValueError
        except (ValueError,UnicodeError):
            raise ApiDenied('invalid_api_configuration') from None
        headers,query = _mapping(raw.get('headers',{}),headers=True),_mapping(raw.get('query',{}),headers=False)
        if not any('{secret}' in value for value in (*headers.values(),*query.values())):
            raise ApiDenied('api_secret_placeholder_required')
        allow = raw.get('allow')
        rules = []
        if allow is not None:
            if type(allow) is not list or len(allow)>100:
                raise ApiDenied('invalid_api_configuration')
            for rule in allow:
                if type(rule) is not str or ' ' not in rule:
                    raise ApiDenied('invalid_api_configuration')
                method,prefix = rule.split(' ',1)
                if method not in METHODS|{'*'} or '*' in prefix:
                    raise ApiDenied('invalid_api_configuration')
                rules.append((method,canonical_path(prefix.encode()).rstrip('/')))
        per_hour,about = raw.get('per_hour',0),raw.get('about','')
        if type(per_hour) is not int or not 0<=per_hour<=1000000 or type(about) is not str or len(about)>4000:
            raise ApiDenied('invalid_api_configuration')
        return cls(name,base.rstrip('/'),secret_file,headers,query,tuple(rules) if allow is not None else None,per_hour,about)

    def permits(self, method, path):
        try:
            if type(path) is not str or canonical_path(path.encode())!=path:
                return False
        except ValueError:
            return False
        return method in METHODS and (self.allow is None or any(
            verb in {'*',method} and (path==prefix or path.startswith(prefix+'/')) for verb,prefix in self.allow))

    def secret(self):
        try:
            value = _read_secret(self.secret_file)
            if not 1<=len(value)<=16384 or any(ord(c)<=32 or ord(c)>=127 for c in value):
                raise ValueError
            return value
        except (ValueError,OSError):
            raise ApiDenied('api_credential_unavailable') from None


def parse_apis(raw):
    if type(raw) is not dict or len(raw)>32:
        raise ApiDenied('invalid_api_configuration')
    return {name:HostAPI.parse(name,value) for name,value in raw.items()}
