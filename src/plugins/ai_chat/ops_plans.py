"""Operator-owned argv recipes; callers never choose a host, shell or credential."""
from dataclasses import asdict,dataclass,field
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit

from src.qq_official.credentials import _read_secret
from .host_apis import canonical_path

NAME = re.compile(r'[a-z][a-z0-9_-]{0,47}')


class OpsPlanError(ValueError):
    """Fixed diagnostics, never echo a private configuration or user argument."""


def text(value,limit):
    if type(value) is not str or not 1<=len(value)<=limit or any(unicodedata.category(c) in {'Cc','Cf','Cs'} for c in value):
        raise OpsPlanError('invalid_ops_configuration')
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


@dataclass(frozen=True)
class Parameter:
    choices: tuple[str,...] = ()
    minimum: int | None = None
    maximum: int | None = None
    revision: bool = False

    @classmethod
    def parse(cls,raw):
        if type(raw) is not dict:
            raise OpsPlanError('invalid_ops_parameter_rule')
        if set(raw)=={'enum'} and type(raw['enum']) is list and 1<=len(raw['enum'])<=64:
            choices = tuple(text(value,256) for value in raw['enum'])
            if len(set(choices))==len(choices):
                return cls(choices=choices)
        if set(raw)=={'minimum','maximum'} and all(type(value) is int for value in raw.values()):
            if -10**9<=raw['minimum']<=raw['maximum']<=10**9:
                return cls(minimum=raw['minimum'],maximum=raw['maximum'])
        if raw=={'format':'git_revision'}:
            return cls(revision=True)
        raise OpsPlanError('invalid_ops_parameter_rule')

    def accepts(self,value):
        if self.choices:
            return type(value) is str and value in self.choices
        if self.revision:
            return type(value) is str and re.fullmatch(r'(?:[a-f0-9]{40}|[a-f0-9]{64})',value) is not None
        return type(value) is int and self.minimum<=value<=self.maximum

    def schema(self):
        if self.choices:
            return dict(type='string',enum=list(self.choices))
        if self.revision:
            return dict(type='string',pattern=r'^(?:[a-f0-9]{40}|[a-f0-9]{64})$')
        return dict(type='integer',minimum=self.minimum,maximum=self.maximum)


@dataclass(frozen=True,repr=False)
class OpsPlan:
    name: str
    about: str
    host: str
    argv: tuple[str,...]
    parameters: tuple[tuple[str,Parameter],...]
    timeout_seconds: int

    @classmethod
    def parse(cls,name,raw):
        if not NAME.fullmatch(text(name,48)) or type(raw) is not dict or set(raw)!={'about','host','argv','parameters','timeout_seconds'}:
            raise OpsPlanError('invalid_ops_plan')
        about,host = text(raw['about'],500),text(raw['host'],64)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',host):
            raise OpsPlanError('invalid_ops_host')
        if type(raw['argv']) is not list or not 1<=len(raw['argv'])<=64:
            raise OpsPlanError('invalid_ops_argv')
        argv = tuple(text(value,4096) for value in raw['argv'])
        if not re.fullmatch(r'/[A-Za-z0-9_./+-]+',argv[0]) or '..' in argv[0].split('/'):
            raise OpsPlanError('ops_executable_must_be_fixed')
        if type(raw['parameters']) is not dict or len(raw['parameters'])>16:
            raise OpsPlanError('invalid_ops_parameters')
        parameters = tuple((text(key,48),Parameter.parse(value)) for key,value in sorted(raw['parameters'].items()))
        if any(not NAME.fullmatch(key) for key,_ in parameters):
            raise OpsPlanError('invalid_ops_parameters')
        used = set()
        for value in argv[1:]:
            if '{' in value or '}' in value:
                if value not in {'{'+key+'}' for key,_ in parameters}:
                    raise OpsPlanError('ops_placeholders_must_be_whole_arguments')
                used.add(value[1:-1])
        if used!={key for key,_ in parameters}:
            raise OpsPlanError('ops_parameter_not_used')
        timeout = raw['timeout_seconds']
        if type(timeout) is not int or not 1<=timeout<=1800:
            raise OpsPlanError('invalid_ops_timeout')
        return cls(name,about,host,argv,parameters,timeout)

    def schema(self):
        return dict(type='object',properties={key:rule.schema() for key,rule in self.parameters},
            required=[key for key,_ in self.parameters],additionalProperties=False)

    def render(self,args):
        if type(args) is not dict or args.keys()!={key for key,_ in self.parameters} or any(
                not rule.accepts(args[key]) for key,rule in self.parameters):
            raise OpsPlanError('ops_arguments_denied')
        values = {'{'+key+'}':str(value) for key,value in args.items()}
        return dict(host=self.host,profile='operator',command={'argv':[values.get(value,value) for value in self.argv]},
            timeout_seconds=self.timeout_seconds)


@dataclass(frozen=True,repr=False)
class OpsConfiguration:
    base_url: str
    secret_file: str
    actor: str
    plans: tuple[OpsPlan,...] = field(repr=False)

    @classmethod
    def parse(cls,raw):
        try:
            if type(raw) is not dict or set(raw)!={'base_url','secret_file','actor','plans'}:
                raise ValueError
            base,secret,actor = text(raw['base_url'],4096).rstrip('/'),text(raw['secret_file'],4096),text(raw['actor'],128)
            parsed = urlsplit(base)
            if (not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
                    or re.search(r'\s|\\',base) or parsed.port==0
                    or (parsed.scheme!='https' and not (parsed.scheme=='http' and parsed.hostname in {'127.0.0.1','::1'}))):
                raise ValueError
            canonical_path((parsed.path or '/').encode())
            if not Path(secret).is_absolute() or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}',actor):
                raise ValueError
            if type(raw['plans']) is not dict or not 1<=len(raw['plans'])<=32:
                raise ValueError
            plans = tuple(OpsPlan.parse(name,value) for name,value in sorted(raw['plans'].items()))
            return cls(base,secret,actor,plans)
        except (ValueError,TypeError,UnicodeError):
            raise OpsPlanError('invalid_ops_configuration') from None

    def plan(self,name):
        result = next((plan for plan in self.plans if plan.name==name),None)
        if result is None:
            raise OpsPlanError('ops_plan_not_configured')
        return result

    def secret(self):
        try:
            value = _read_secret(self.secret_file)
            if not 32<=len(value)<=512 or any(not 33<=ord(c)<=126 for c in value):
                raise ValueError
            return value
        except (ValueError,OSError):
            raise OpsPlanError('ops_credential_unavailable') from None

    def binding(self,plan,secret):
        return fingerprint(dict(base=self.base_url,actor=self.actor,secret_hash=fingerprint(secret),plan=asdict(plan)))

    def catalog(self):
        # Only descriptions and validation rules go to the private job. Host URL,
        # secret path/value and command templates are never tool arguments.
        return [dict(name=plan.name,about=plan.about,parameters=plan.schema()) for plan in self.plans]
