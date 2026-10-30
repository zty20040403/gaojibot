"""Protected, default-off host configuration for the actual application entry.

Only deployment configuration can select these factories. It contains no
Python import paths, credential values, identity mappings or project grants.
File uploads use the configured account directly, without route proofs.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import logging
from pathlib import Path

from src.bot_security.store import SecurityError
from src.qq_official.credentials import _read_secret
from ...platform_types import AccountKey, EndpointKey
from .config import _boolean
from .file_services import FileServiceConfig, file_services_factory
from .host_services import HostServices


class HostConfigurationError(ValueError):
    """Fixed diagnostics: never include the protected document or path."""


def _object(value, required, optional=()):
    if type(value) is not dict or not set(required)<=value.keys() or value.keys()-set(required)-set(optional):
        raise HostConfigurationError('invalid_official_host_configuration')
    return value


def _pairs(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise HostConfigurationError('duplicate_official_host_configuration_key')
        result[key]=value
    return result


def _list(value,limit):
    if type(value) is not list or len(value)>limit:
        raise HostConfigurationError('invalid_official_host_configuration_list')
    return value


def _native_background(raw,account):
    if raw is None:return None,None,None,None,None
    from .native_schedule_policy import NativeScheduleConfiguration
    from .native_background_factory import ProactiveConfiguration
    from .wiki_configuration import WikiConfiguration,WikiCollectionEvidence
    _object(raw,(),('schedules','proactive','wiki'))
    schedule,schedule_profile,proactive,proactive_profile,wiki=None,None,None,None,None
    def profile(value,enabled):
        if value is not None and (type(value) is not str or not 1<=len(value)<=128 or value!=value.strip()):raise ValueError
        if enabled and value is None:raise ValueError
        return value
    if 'schedules' in raw:
        data=dict(_object(raw['schedules'],('enabled','policy_id','revision','authorization_until'),
            ('profile','execution_seconds','delivery_seconds','max_tokens','daily_tokens','daily_runs')))
        schedule_profile=profile(data.pop('profile',None),data['enabled'])
        schedule=NativeScheduleConfiguration(**data)
    if 'proactive' in raw:
        data=dict(_object(raw['proactive'],('enabled',),('profile','percent','max_checks_per_hour','threshold','max_reply_chars')))
        proactive_profile=profile(data.pop('profile',None),data['enabled'])
        proactive=ProactiveConfiguration(**data)
    if 'wiki' in raw:
        data=dict(_object(raw['wiki'],('enabled',),('profile','hour','timezone','check_seconds','collection_evidence')))
        evidence=[]
        for entry in _list(data.pop('collection_evidence',[]),128):
            entry=dict(_object(entry,('endpoint','endpoint_generation','route_epoch','evidence_inbox_id','verified_at','expires_at')))
            entry['endpoint']=EndpointKey.parse(entry['endpoint'])
            if entry['endpoint'].account!=account:raise ValueError
            evidence.append(WikiCollectionEvidence(**entry))
        data['profile']=profile(data.get('profile'),data['enabled'])
        wiki=WikiConfiguration(**data,collection_evidence=tuple(evidence))
    return schedule,schedule_profile,proactive,proactive_profile,wiki


logger = logging.getLogger(__name__)


def _optional(name, load, default=None):
    try:
        return load()
    except (ValueError, TypeError, KeyError, OSError, SecurityError) as error:
        logger.warning('QQ feature disabled feature=%s error=%s', name, type(error).__name__)
        return default


def _files(raw):
    if raw is None:
        return None
    if not isinstance(raw,dict):raise ValueError('invalid_file_settings')
    allowed = set(FileServiceConfig.__dataclass_fields__) - {'manager','source_authority','existing_artifact_authority'}
    value = {k:v for k,v in raw.items() if k in allowed}
    value['private_directory'] = Path(value['private_directory'])
    if not value['private_directory'].is_absolute():
        raise ValueError('file_directory_must_be_absolute')
    if 'generation_limits' in value:
        from ...application.owned_sandbox import GenerationLimits
        value['generation_limits'] = GenerationLimits(**value['generation_limits'])
    FileServiceConfig(manager=None, **value)
    return value


@dataclass(frozen=True,repr=False)
class HostConfiguration:
    account: AccountKey
    files: dict | None = field(default=None,repr=False)
    host_services: tuple = field(default=(),repr=False)
    media: object | None = field(default=None,repr=False)
    native_schedule: object | None = field(default=None,repr=False)
    native_schedule_profile: str | None = None
    proactive: object | None = field(default=None,repr=False)
    proactive_profile: str | None = None
    wiki: object | None = field(default=None,repr=False)

    @classmethod
    def parse(cls,text):
        try:
            raw=json.loads(text)
            if raw.get('version',1)!=1:raise ValueError
            account=AccountKey.parse(raw['account'])
            if account.platform!='qq-official':raise ValueError
        except (ValueError,TypeError,KeyError,AttributeError):
            raise HostConfigurationError('invalid_official_host_account') from None
        from .host_authority import AllowedService
        from .media_runtime import MediaConfig
        def services():
            return tuple(AllowedService(**{**entry,'actions':frozenset(entry['actions'])})
                         for entry in raw.get('host_services',[]))
        values=dict(files=_optional('files',lambda:_files(raw.get('files'))),
            host_services=_optional('host_services',services,()),
            media=_optional('media',lambda:MediaConfig(**raw['media']) if raw.get('media') is not None else None))
        background=_optional('background',lambda:dict(raw.get('native_background') or {}),{})
        for key in ('schedules','proactive','wiki'):
            if key not in background:continue
            parsed=_optional(key,lambda:_native_background({key:background[key]},account))
            if parsed is None:continue
            schedule,sp,proactive,pp,wiki=parsed
            if key=='schedules':values.update(native_schedule=schedule,native_schedule_profile=sp)
            elif key=='proactive':values.update(proactive=proactive,proactive_profile=pp)
            else:values['wiki']=wiki
        return cls(account,**values)

    @classmethod
    def from_env(cls,env):
        migration=_boolean(env.get('AI_QQ_MIGRATION_ENABLED','false'),'migration')
        official=_boolean(env.get('AI_QQ_OFFICIAL_ENABLED','false'),'enabled')
        path=(env.get('AI_QQ_OFFICIAL_HOST_CONFIG_FILE') if official else None) or (
            env.get('AI_QQ_MIGRATION_CONFIG_FILE') if migration else None)
        if not path:return None
        return _optional('host_configuration',lambda:cls.parse(_read_secret(path,None)))

    def build(self,context,runtime,*,project_grant_ttl_seconds=86400,file_link_ttl_seconds=3600):
        config=self
        for feature,profile in (('native_schedule',self.native_schedule_profile),('proactive',self.proactive_profile),
                ('wiki',self.wiki.profile if self.wiki is not None and self.wiki.enabled else None)):
            if profile is not None and _optional(feature,lambda:context.model_catalog.resolve(profile)) is None:
                config=replace(config,**{feature:None})
        service=HostServices(database=context.database,transaction=context.delivery_store.transaction,
            database_calls=context.db_call,project_grant_ttl_seconds=project_grant_ttl_seconds)
        context.official_host=ConfiguredHost(config,context,service)
        return service


class ConfiguredHost:
    def __init__(self,config,context,migration,*,file_link_ttl_seconds=3600):
        if type(file_link_ttl_seconds) is not int or not 60<=file_link_ttl_seconds<=3600:
            raise HostConfigurationError('invalid_file_link_lifetime')
        self.file_link_ttl_seconds=file_link_ttl_seconds
        self.config,self.context,self.migration=config,context,migration
        self.download_host=None

    def download_status(self):
        if self.download_host is None:
            return dict(configured=False,listening=False,proxy_reachable=False,evidence_configured=False)
        return self.download_host.status()

    async def close(self):
        if self.download_host is not None:await self.download_host.close()

    def media_factory(self,runtime,token):
        if self.config.media is None:return None
        if token.account_key!=self.config.account.key:
            raise HostConfigurationError('official_host_account_mismatch')
        from .media_runtime import OfficialMediaService
        return OfficialMediaService(runtime,token,self.context,self.config.media)

    def _session(self,runtime,token):
        if token.account_key!=self.config.account.key:
            raise HostConfigurationError('official_host_account_mismatch')
        session=self.migration.official.get(token.account_key)
        if session is None or session.runtime is not runtime or session.token is not token:
            raise HostConfigurationError('official_host_epoch_unavailable')
        return session

    def migration_factory(self,runtime,token):
        if token.account_key!=self.config.account.key:
            raise HostConfigurationError('official_host_account_mismatch')
        return self.migration.official_factory(runtime,token)

    async def background_factory(self,runtime,token):
        self._session(runtime,token)
        from .native_background_factory import background_factory,ProactiveConfiguration
        from .wiki_service import OfficialWikiService
        wiki=self.config.wiki
        def wiki_factory(actual,current):
            if wiki is None or not wiki.enabled:return None
            if actual is not runtime or current is not token:raise HostConfigurationError('official_host_epoch_unavailable')
            return OfficialWikiService(runtime=actual,token=current,app=self.context,
                collection_allowed=wiki.allowed,configuration=wiki)
        factory=background_factory(schedule_configuration=self.config.native_schedule,
            schedule_profile=self.config.native_schedule_profile,
            proactive_configuration=self.config.proactive or ProactiveConfiguration(),
            proactive_profile=self.config.proactive_profile,wiki_factory=wiki_factory)
        return await factory(runtime,token)

    def approval_factory(self,runtime,token):
        self._session(runtime,token)
        from .approval import approval_services_factory
        return approval_services_factory(security=getattr(getattr(self.context,'mobile_authorization',None),'store',None),
            services=self.config.host_services,fleet_client=self.context.fleet_client)(runtime,token)

    async def files_factory(self,runtime,token,rest):
        session=self._session(runtime,token)
        if self.config.files is None:return None
        from .project_artifacts import ProjectArtifactAuthority
        def route_guard(cursor,**kwargs):
            self.migration.routes.assert_active_route(cursor,**kwargs)
            return True
        authority=ProjectArtifactAuthority(token=token,clock=runtime.store.clock,route_guard=route_guard,
            project_scope_guard=self.migration.project_grants.project_scope_guard,
            read_route_guard=lambda cursor,artifact,project_binding,**kwargs: route_guard(cursor,**kwargs))
        config=FileServiceConfig(manager=self.context.sandbox_manager,**self.config.files,
            source_authority=authority)
        if config.generation_enabled and config.manager is None:
            raise HostConfigurationError('official_host_sandbox_unavailable')
        files=await file_services_factory(session.file_configuration(config))(runtime,token,rest)
        try:
            session.attach_file_services(files)
        except BaseException:
            await files.close()
            raise
        return files


def configured_migration_factory(env=None):
    """Read protected deployment configuration only during startup."""
    import os
    from .config import OfficialConfig
    env=os.environ if env is None else env
    config=HostConfiguration.from_env(env)
    if config is None:return None
    delivery=OfficialConfig.from_env(env).delivery
    return lambda context,runtime:config.build(context,runtime,
        project_grant_ttl_seconds=delivery.project_grant_ttl_seconds,
        file_link_ttl_seconds=delivery.file_link_ttl_seconds)
