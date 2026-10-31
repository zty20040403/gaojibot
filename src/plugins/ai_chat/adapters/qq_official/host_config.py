"""Protected, default-off host configuration for the actual application entry.

Only deployment configuration can select these factories. It contains no
Python import paths, credential values or identity mappings.
File uploads use the configured account directly, without route proofs.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import logging
from pathlib import Path

from src.bot_security.store import SecurityError
from src.qq_official.credentials import _read_secret
from ...platform_types import AccountKey
from .config import _boolean
from .file_services import FileServiceConfig, file_services_factory
from .host_services import HostServices


class HostConfigurationError(ValueError):
    """Fixed diagnostics: never include the protected document or path."""


def _object(value, required, optional=()):
    if type(value) is not dict or not set(required)<=value.keys() or value.keys()-set(required)-set(optional):
        raise HostConfigurationError('invalid_official_host_configuration')
    return value


def _proactive(raw):
    from .native_background_factory import ProactiveConfiguration
    data=dict(_object(raw,('enabled',),('profile','percent','max_checks_per_hour','threshold','max_reply_chars')))
    profile=data.pop('profile',None)
    if profile is not None and (type(profile) is not str or not 1<=len(profile)<=128 or profile!=profile.strip()):raise ValueError
    if data['enabled'] and profile is None:raise ValueError
    return ProactiveConfiguration(**data),profile


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
    media: object | None = field(default=None,repr=False)
    proactive: object | None = field(default=None,repr=False)
    proactive_profile: str | None = None

    @classmethod
    def parse(cls,text):
        try:
            raw=json.loads(text)
            if raw.get('version',1)!=1:raise ValueError
            account=AccountKey.parse(raw['account'])
            if account.platform!='qq-official':raise ValueError
        except (ValueError,TypeError,KeyError,AttributeError):
            raise HostConfigurationError('invalid_official_host_account') from None
        from .media_runtime import MediaConfig
        values=dict(files=_optional('files',lambda:_files(raw.get('files'))),
            media=_optional('media',lambda:MediaConfig(**raw['media']) if raw.get('media') is not None else None))
        # Old documents may still carry schedules/wiki/host_services; those features were removed.
        background=_optional('background',lambda:dict(raw.get('native_background') or {}),{})
        if 'proactive' in background:
            parsed=_optional('proactive',lambda:_proactive(background['proactive']))
            if parsed is not None:values.update(proactive=parsed[0],proactive_profile=parsed[1])
        return cls(account,**values)

    @classmethod
    def from_env(cls,env):
        migration=_boolean(env.get('AI_QQ_MIGRATION_ENABLED','false'),'migration')
        official=_boolean(env.get('AI_QQ_OFFICIAL_ENABLED','false'),'enabled')
        path=(env.get('AI_QQ_OFFICIAL_HOST_CONFIG_FILE') if official else None) or (
            env.get('AI_QQ_MIGRATION_CONFIG_FILE') if migration else None)
        if not path:return None
        return _optional('host_configuration',lambda:cls.parse(_read_secret(path,None)))

    def build(self,context,runtime,*,file_link_ttl_seconds=3600):
        config=self
        if self.proactive_profile is not None and _optional(
                'proactive',lambda:context.model_catalog.resolve(self.proactive_profile)) is None:
            config=replace(config,proactive=None)
        service=HostServices(database=context.database,transaction=context.delivery_store.transaction,
            database_calls=context.db_call)
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
        factory=background_factory(proactive_configuration=self.config.proactive or ProactiveConfiguration(),
            proactive_profile=self.config.proactive_profile)
        return await factory(runtime,token)

    def approval_factory(self,runtime,token):
        self._session(runtime,token)
        from .approval import approval_services_factory
        return approval_services_factory(
            security=getattr(getattr(self.context,'mobile_authorization',None),'store',None))(runtime,token)

    async def files_factory(self,runtime,token,rest):
        session=self._session(runtime,token)
        if self.config.files is None:return None
        config=FileServiceConfig(manager=self.context.sandbox_manager,**self.config.files)
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
        file_link_ttl_seconds=delivery.file_link_ttl_seconds)
