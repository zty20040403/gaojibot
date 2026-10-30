"""Protected, default-off host configuration for the actual application entry.

Only deployment configuration can select these factories. It contains no
Python import paths, credential values, identity mappings or project grants.
File uploads use the configured account directly, without route proofs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

from src.bot_security.store import SecurityError
from src.qq_official.credentials import _read_secret
from ...platform_types import AccountKey, EndpointKey
from .config import _boolean
from .file_services import FileServiceConfig, file_services_factory
from .migration_services import MigrationServices, OneBotAccount, ExportDeadline


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


@dataclass(frozen=True,repr=False)
class HostConfiguration:
    account: AccountKey
    onebot_accounts: tuple[OneBotAccount,...]
    scheduled_profile: str | None = None
    subagent_profile: str | None = None
    deadlines: tuple = ()
    files: dict | None = field(default=None,repr=False)
    native_routes: tuple = field(default=(),repr=False)
    host_services: tuple = field(default=(),repr=False)
    media: object | None = field(default=None,repr=False)
    native_schedule: object | None = field(default=None,repr=False)
    native_schedule_profile: str | None = None
    proactive: object | None = field(default=None,repr=False)
    proactive_profile: str | None = None
    wiki: object | None = field(default=None,repr=False)
    download: object | None = field(default=None,repr=False)

    @classmethod
    def parse(cls,text):
        try:
            raw=_object(json.loads(text,object_pairs_hook=_pairs),
                ('version','account','onebot_accounts'),
                ('scheduled_profile','subagent_profile','deadlines','files','host_services','media','native_background'))
            if type(raw['version']) is not int or raw['version']!=1:raise ValueError
            account=AccountKey.parse(raw['account'])
            if account.platform!='qq-official':raise ValueError
            sources=[]
            for item in _list(raw['onebot_accounts'],16):
                _object(item,('account','deployment_digest'))
                sources.append(OneBotAccount(AccountKey.parse(item['account']),item['deployment_digest']))
            if len({x.account.key for x in sources})!=len(sources):raise ValueError
            profiles=[]
            for name in ('scheduled_profile','subagent_profile'):
                value=raw.get(name)
                if value is not None and (type(value) is not str or not value.strip() or len(value)>128):raise ValueError
                profiles.append(value)
            deadlines={}
            for item in _list(raw.get('deadlines',[]),128):
                _object(item,('source_endpoint','kind','reference','execution','result'),('schedule_json',))
                endpoint=EndpointKey.parse(item['source_endpoint'])
                if endpoint.account not in {x.account for x in sources}:raise ValueError
                if item['kind'] not in {'reminder','routine','subagent','project_task','file'}:raise ValueError
                if type(item['reference']) is not str or not 1<=len(item['reference'])<=256:raise ValueError
                key=(endpoint.key,item['kind'],item['reference'])
                if key in deadlines:raise ValueError
                deadlines[key]=ExportDeadline(item['execution'],item['result'],item.get('schedule_json'))
            files=raw.get('files')
            evidence=[]
            download=None
            if files is not None:
                files=dict(files)
                # Accepted for existing deployment files, no longer consulted.
                for obsolete in ('native_routes', 'multipart', 'download'):
                    files.pop(obsolete, None)
                files['private_directory']=Path(files['private_directory'])
                FileServiceConfig(manager=None,**files)  # Validate without opening files or creating a manager.
            from .host_authority import AllowedService
            services=[]
            for item in _list(raw.get('host_services',[]),64):
                _object(item,('host_id','resource_ref','backend_ref','backend_binding_version','actions'))
                item=dict(item);item['actions']=frozenset(_list(item['actions'],16))
                services.append(AllowedService(**item))
            from .media_runtime import MediaConfig
            media=None
            if raw.get('media') is not None:
                value=_object(raw['media'],(),tuple(MediaConfig.__dataclass_fields__))
                media=MediaConfig(**value)
            schedule,schedule_profile,proactive,proactive_profile,wiki=_native_background(raw.get('native_background'),account)
            return cls(account,tuple(sources),*profiles,tuple(deadlines.items()),files,tuple(evidence),tuple(services),
                media=media,native_schedule=schedule,native_schedule_profile=schedule_profile,
                proactive=proactive,proactive_profile=proactive_profile,wiki=wiki,download=download)
        except (ValueError,TypeError,KeyError,OverflowError,RecursionError,SecurityError):
            raise HostConfigurationError('invalid_official_host_configuration') from None

    @classmethod
    def from_env(cls,env):
        migration=_boolean(env.get('AI_QQ_MIGRATION_ENABLED','false'),'migration')
        official=_boolean(env.get('AI_QQ_OFFICIAL_ENABLED','false'),'enabled')
        old=env.get('AI_QQ_MIGRATION_CONFIG_FILE','') if migration else ''
        native=env.get('AI_QQ_OFFICIAL_HOST_CONFIG_FILE','') if official else ''
        if not migration and not native:return None
        try:
            if migration and not old:raise ValueError
            if old and native and Path(old)!=Path(native):
                raise HostConfigurationError('conflicting_official_host_configuration')
            path=old or native
            if not path or not Path(path).is_absolute():raise ValueError
            return cls.parse(_read_secret(path,None))
        except (ValueError,OSError):
            raise HostConfigurationError('official_host_configuration_unavailable') from None

    def build(self,context,runtime,*,project_grant_ttl_seconds=86400,file_link_ttl_seconds=3600):
        mobile=getattr(context,'mobile_authorization',None)
        if context.database is None or context.delivery_store is None or mobile is None:
            raise HostConfigurationError('official_host_requires_durable_security_services')
        for profile in (self.scheduled_profile,self.subagent_profile,self.native_schedule_profile,self.proactive_profile,
                        self.wiki.profile if self.wiki is not None and self.wiki.enabled else None):
            if profile is not None:context.model_catalog.resolve(profile)
        def canonical_scope(event):
            if not getattr(runtime,'onebot_enabled',True):
                raise HostConfigurationError('legacy_source_unavailable')
            return runtime.handlers.chat._conversation_scope(event)
        service=MigrationServices(database=context.database,transaction=context.delivery_store.transaction,
            database_calls=context.db_call,security=mobile.store,onebot_accounts=self.onebot_accounts,
            canonical_scope=canonical_scope,
            deadlines=self.deadlines,scheduled_profile=self.scheduled_profile,subagent_profile=self.subagent_profile,
            project_grant_ttl_seconds=project_grant_ttl_seconds)
        context.official_host=ConfiguredHost(self,context,service,file_link_ttl_seconds=file_link_ttl_seconds)
        service.file_admission=context.official_host.file_admission
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
        from .approval_services import approval_services_factory
        return approval_services_factory(security=self.context.mobile_authorization.store,
            services=self.config.host_services,fleet_client=self.context.fleet_client)(runtime,token)

    def file_admission(self,cursor,*,item_id,endpoint,actor,now):
        session=self.migration.official.get(endpoint.account.key)
        files=session._files if session is not None else None
        # This is the configured profile policy, not proof of deliverability.
        # Every source still passes the journal/mapping/task guards. M6 checks
        # the exact route before generation/upload/send; expired upload evidence
        # must not revoke an independently authorized retained-copy read.
        return (files is not None and session._attached and files.generation.enabled
                and endpoint.account==self.config.account and session.admission(cursor,endpoint,actor) is True)

    async def files_factory(self,runtime,token,rest):
        session=self._session(runtime,token)
        if self.config.files is None:return None
        from .project_artifacts import ProjectArtifactAuthority
        def route_guard(cursor,**kwargs):
            self.migration.routes.assert_active_route(cursor,**kwargs)
            return True
        authority=ProjectArtifactAuthority(token=token,clock=runtime.store.clock,route_guard=route_guard,
            project_scope_guard=self.migration.project_grants.project_scope_guard,
            read_route_guard=lambda cursor,artifact,project_binding,**kwargs:
                kwargs['canonical_scope'].key==kwargs['endpoint'].scope.key)
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
