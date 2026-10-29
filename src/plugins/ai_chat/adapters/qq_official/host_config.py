"""Protected, default-off host configuration for the actual application entry.

Only deployment configuration can select these factories. It contains no
Python import paths, credential values, identity mappings or project grants.
Native file evidence is an exact, expiring administrator-reviewed observation;
loading it does not probe the platform or extend its validity.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

from src.bot_security.store import SecurityError
from src.qq_official.credentials import _read_secret
from ...platform_types import AccountKey, EndpointKey
from .config import _boolean
from .file_services import FileServiceConfig, NativeRouteEvidence, file_services_factory
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


@dataclass(frozen=True,repr=False)
class HostConfiguration:
    account: AccountKey
    onebot_accounts: tuple[OneBotAccount,...]
    scheduled_profile: str | None = None
    subagent_profile: str | None = None
    deadlines: tuple = ()
    files: dict | None = field(default=None,repr=False)
    native_routes: tuple[NativeRouteEvidence,...] = field(default=(),repr=False)
    host_services: tuple = field(default=(),repr=False)

    @classmethod
    def parse(cls,text):
        try:
            raw=_object(json.loads(text,object_pairs_hook=_pairs),
                ('version','account','onebot_accounts'),
                ('scheduled_profile','subagent_profile','deadlines','files','host_services'))
            if type(raw['version']) is not int or raw['version']!=1:raise ValueError
            account=AccountKey.parse(raw['account'])
            if account.platform!='qq-official':raise ValueError
            sources=[]
            for item in _list(raw['onebot_accounts'],16):
                _object(item,('account','deployment_digest'))
                sources.append(OneBotAccount(AccountKey.parse(item['account']),item['deployment_digest']))
            if not sources or len({x.account.key for x in sources})!=len(sources):raise ValueError
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
            if files is not None:
                _object(files,('private_directory','native_routes'),
                    ('generation_enabled','max_file_bytes','quota_bytes','max_artifacts','min_free_bytes',
                     'account_capacity','actor_capacity','destroy_expired_resources'))
                files=dict(files)
                for item in _list(files.pop('native_routes'),128):
                    _object(item,('endpoint','endpoint_generation','route_epoch','capability_revision','max_bytes','verified_at','expires_at','contract','file_type'))
                    item=dict(item);item['endpoint']=EndpointKey.parse(item['endpoint'])
                    if item['endpoint'].account!=account:raise ValueError
                    for key in ('endpoint_generation','route_epoch','capability_revision','max_bytes','expires_at'):
                        if type(item[key]) is not int or item[key]<=0:raise ValueError
                    if type(item['verified_at']) is not int or not 0<=item['verified_at']<item['expires_at']:raise ValueError
                    if (type(item['contract']) is not str or not 1<=len(item['contract'].encode())<=256
                            or any(ord(c)<32 or ord(c)==127 for c in item['contract'])
                            or type(item['file_type']) is not int or item['file_type']!=4):raise ValueError
                    evidence.append(NativeRouteEvidence(**item))
                if len({x.endpoint.key for x in evidence})!=len(evidence):raise ValueError
                files['private_directory']=Path(files['private_directory'])
                FileServiceConfig(manager=None,**files)  # Validate without opening files or creating a manager.
            from .host_authority import AllowedService
            services=[]
            for item in _list(raw.get('host_services',[]),64):
                _object(item,('host_id','resource_ref','backend_ref','backend_binding_version','actions'))
                item=dict(item);item['actions']=frozenset(_list(item['actions'],16))
                services.append(AllowedService(**item))
            return cls(account,tuple(sources),*profiles,tuple(deadlines.items()),files,tuple(evidence),tuple(services))
        except (ValueError,TypeError,KeyError,OverflowError,RecursionError,SecurityError):
            raise HostConfigurationError('invalid_official_host_configuration') from None

    @classmethod
    def from_env(cls,env):
        if not _boolean(env.get('AI_QQ_MIGRATION_ENABLED','false'),'migration'):
            return None
        try:
            path=env.get('AI_QQ_MIGRATION_CONFIG_FILE','')
            if not path or not Path(path).is_absolute():raise ValueError
            return cls.parse(_read_secret(path,None))
        except (ValueError,OSError):
            raise HostConfigurationError('official_host_configuration_unavailable') from None

    def build(self,context,runtime):
        mobile=getattr(context,'mobile_authorization',None)
        if context.database is None or context.delivery_store is None or mobile is None:
            raise HostConfigurationError('official_host_requires_durable_security_services')
        for profile in (self.scheduled_profile,self.subagent_profile):
            if profile is not None:context.model_catalog.resolve(profile)
        service=MigrationServices(database=context.database,transaction=context.delivery_store.transaction,
            database_calls=context.db_call,security=mobile.store,onebot_accounts=self.onebot_accounts,
            canonical_scope=lambda event:runtime.handlers.chat._conversation_scope(event),
            deadlines=self.deadlines,scheduled_profile=self.scheduled_profile,subagent_profile=self.subagent_profile)
        context.official_host=ConfiguredHost(self,context,service)
        service.file_admission=context.official_host.file_admission
        return service


class ConfiguredHost:
    def __init__(self,config,context,migration):
        self.config,self.context,self.migration=config,context,migration
        self.evidence={value.endpoint.key:value for value in config.native_routes}

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
            source_authority=authority,native_evidence=lambda cursor,endpoint:self.evidence.get(endpoint.key),
            route_authority=lambda cursor,endpoint,actor,route:route=='native' and endpoint.key in self.evidence)
        if config.generation_enabled and config.manager is None:
            raise HostConfigurationError('official_host_sandbox_unavailable')
        files=await file_services_factory(session.file_configuration(config))(runtime,token,rest)
        try:session.attach_file_services(files)
        except BaseException:
            await files.close()
            raise
        return files


def configured_migration_factory(env=None):
    """Read protected deployment configuration only during startup."""
    import os
    config=HostConfiguration.from_env(os.environ if env is None else env)
    return config.build if config is not None else None
