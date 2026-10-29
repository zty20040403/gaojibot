"""Write-only controller credentials, immutable revision references and safe settings.

No network or database calls. A reference names local private files; it never
proves a gateway, owner identity, capability, or exclusive global deployment.
"""
from __future__ import annotations

from dataclasses import dataclass,field
import json
import os
from pathlib import Path
import re
import secrets
import stat

from src.qq_official.credentials import Credentials,_read_secret,verify_private_file
from src.plugins.ai_chat.platform_types import AccountKey
from .validation import InstanceError

_REFERENCE=re.compile(r'^[a-f0-9]{32}$')
_FILES=('app-id','app-secret','bind-pepper','metadata.json')


def platforms(value=None):
    if value is None:return {'onebot':True,'qq_official':False}
    if (type(value) is not dict or set(value)!={'onebot','qq_official'}
            or any(type(x) is not bool for x in value.values()) or not any(value.values())):
        raise InstanceError('Invalid platform selection')
    return dict(value)


def _identities(value,limit):
    import unicodedata
    if (type(value) is not list or len(value)>limit or any(type(x) is not str or not x.strip()
            or len(x.encode('utf-8'))>256 or any(unicodedata.category(c) in {'Cc','Cf','Cs'} for c in x) for x in value)):
        raise InstanceError('Invalid official identity selection')
    return list(dict.fromkeys(value))


def official_settings(value=None):
    defaults=dict(environment='production',private_policy='owner_only',owner_openids=[],private_allowed_openids=[],
        daily_calls=100,daily_input_tokens=500000,daily_output_tokens=100000,exclusive_deployment_confirmed=False)
    if value is None:return defaults
    if type(value) is not dict or set(value)-{'files'}!=set(defaults):raise InstanceError('Invalid official settings')
    result=dict(value)
    if 'files' in result:
        from .official_files import file_settings
        result['files']=file_settings(result['files'])
    if result['environment'] not in {'production','sandbox'} or result['private_policy'] not in {'owner_only','allowlist','public'}:
        raise InstanceError('Invalid official environment or admission policy')
    if type(result['exclusive_deployment_confirmed']) is not bool:raise InstanceError('Invalid official handover confirmation')
    result['owner_openids']=_identities(result['owner_openids'],32)
    result['private_allowed_openids']=_identities(result['private_allowed_openids'],256)
    # Explicit finite deployment limits; zero is not an implicit unlimited budget.
    for name,limit in [('daily_calls',100000),('daily_input_tokens',100000000),('daily_output_tokens',100000000)]:
        if type(result[name]) is not int or not 1<=result[name]<=limit:raise InstanceError('Invalid official cost limit')
    if result['private_policy']=='allowlist' and not result['private_allowed_openids']:
        raise InstanceError('Official allowlist policy requires recipients')
    return result


def _directory(path,*,create=False):
    if create:path.mkdir(mode=0o700,exist_ok=True)
    info=path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)&0o077):
        raise InstanceError('Official private directory is unavailable')
    return path


def _root(directory,*,create=False):
    # InstanceStore supplies its validated directory, never a renderer path.
    _directory(directory)
    _directory(directory/'config')
    return _directory(directory/'config'/'official-credentials',create=create)


def _ref(value):
    if type(value) is not str or not _REFERENCE.fullmatch(value):raise InstanceError('Invalid official credential reference')
    return value


def _write(path,data):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        with os.fdopen(fd,'wb',closefd=False) as stream:
            stream.write(data);stream.flush();os.fsync(fd)
        verify_private_file(fd)
    finally:os.close(fd)


@dataclass(frozen=True,repr=False)
class CredentialBundle:
    reference:str
    environment:str
    credentials:Credentials=field(repr=False)
    pepper:str=field(repr=False)

    @property
    def account(self):return AccountKey('qq-official',self.credentials.app_id,self.environment)

    def __repr__(self):return 'CredentialBundle(<protected>)'


def read_credentials(directory:Path,reference:str)->CredentialBundle:
    try:
        folder=_directory(_root(directory)/_ref(reference))
        metadata=json.loads(_read_secret(folder/'metadata.json',None))
        if type(metadata) is not dict or set(metadata)!={'version','environment'} or type(metadata['version']) is not int or metadata['version']!=1:
            raise ValueError
        if metadata['environment'] not in {'production','sandbox'}:raise ValueError
        credentials=Credentials.from_files(folder/'app-id',folder/'app-secret')
        account=AccountKey('qq-official',credentials.app_id,metadata['environment'])
        if len(account.key.encode('utf-8'))>512:raise ValueError
        pepper=_read_secret(folder/'bind-pepper',None)
        if not re.fullmatch('[a-f0-9]{64}',pepper):raise ValueError
        return CredentialBundle(reference,metadata['environment'],credentials,pepper)
    except (OSError,ValueError,TypeError,UnicodeError):
        raise InstanceError('Official protected credentials are unavailable') from None


def stage_credentials(directory:Path,*,environment:str,previous:str|None,supplied=None)->str|None:
    """Caller holds the instance revision lock; publish returned ref only on CAS.

    Existing immutable bundles are never overwritten. Failed settings publication
    leaves an unreferenced private bundle, not a mixed old/new credential pair.
    """
    if environment not in {'production','sandbox'}:raise InstanceError('Invalid official environment')
    prior=read_credentials(directory,previous) if previous is not None else None
    if supplied is None:
        if prior is not None and prior.environment!=environment:
            raise InstanceError('Changing official environment requires explicit credentials')
        return previous
    if type(supplied) is not dict or set(supplied)!={'app_id','app_secret'}:
        raise InstanceError('Supply the official credential pair separately')
    try:
        credentials=Credentials(supplied['app_id'],supplied['app_secret'])
        account=AccountKey('qq-official',credentials.app_id,environment)
        if len(account.key.encode('utf-8'))>512:raise ValueError
    except (ValueError,TypeError,UnicodeError):raise InstanceError('Invalid official credential pair') from None
    if prior is not None and prior.environment==environment and prior.credentials==credentials:return previous
    same_account=prior is not None and prior.environment==environment and prior.credentials.app_id==credentials.app_id
    pepper=prior.pepper if same_account else secrets.token_hex(32)
    folder=None
    try:
        parent=_root(directory,create=True);reference=secrets.token_hex(16);folder=parent/reference
        folder.mkdir(mode=0o700)
        values={'app-id':credentials.app_id,'app-secret':credentials.app_secret,'bind-pepper':pepper,
            'metadata.json':json.dumps({'version':1,'environment':environment},separators=(',',':'))}
        for name,value in values.items():_write(folder/name,value.encode('utf-8'))
        descriptor=os.open(folder,os.O_RDONLY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
        descriptor=os.open(parent,os.O_RDONLY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
        read_credentials(directory,reference)
        return reference
    except (OSError,ValueError,TypeError):
        # Only our fresh random directory is removed; prior audit/revisions stay.
        if folder is not None:
            for name in _FILES:
                try:(folder/name).unlink()
                except FileNotFoundError:pass
            try:folder.rmdir()
            except OSError:pass
        raise InstanceError('Official protected credentials could not be saved') from None


def runtime_environment(settings,reference,*,configured,revision=1):
    selected=platforms(settings.get('platforms'));official=official_settings(settings.get('official'))
    result={'AI_ONEBOT_ENABLED':str(selected['onebot']).lower(),'AI_QQ_OFFICIAL_ENABLED':str(selected['qq_official']).lower()}
    if not selected['qq_official']:return result
    if reference is None:
        if configured:raise InstanceError('Save official credentials before starting')
        # Migrations/model checks can run before a QQ account has been entered.
        result['AI_QQ_OFFICIAL_ENABLED']='false'
        return result
    base='/run/gaoji/'+runtime_name(reference,revision)
    result.update(AI_QQ_OFFICIAL_APP_ID_FILE=base+'/app-id',AI_QQ_OFFICIAL_APP_SECRET_FILE=base+'/app-secret',
        AI_QQ_OFFICIAL_BIND_PEPPER_FILE=base+'/bind-pepper',AI_QQ_OFFICIAL_HOST_CONFIG_FILE=base+'/host.json',
        AI_QQ_OFFICIAL_SANDBOX=str(official['environment']=='sandbox').lower(),
        AI_QQ_OFFICIAL_PRIVATE_POLICY=official['private_policy'],
        AI_QQ_OFFICIAL_OWNER_OPENIDS=json.dumps(official['owner_openids'],ensure_ascii=False),
        AI_QQ_OFFICIAL_PRIVATE_ALLOWED_OPENIDS=json.dumps(official['private_allowed_openids'],ensure_ascii=False),
        AI_QQ_OFFICIAL_GROUP_ADMISSION='manual_bind',AI_QUOTA_ENABLED='true',
        AI_QUOTA_DAILY_CALLS=str(official['daily_calls']),AI_QUOTA_DAILY_INPUT_TOKENS=str(official['daily_input_tokens']),
        AI_QUOTA_DAILY_OUTPUT_TOKENS=str(official['daily_output_tokens']))
    if configured and not official['exclusive_deployment_confirmed']:
        raise InstanceError('Confirm the previous deployment is stopped or use a different AppID')
    return result


def runtime_name(reference,revision):
    if type(revision) is not int or not 1<=revision<2**63:raise InstanceError('Invalid official configuration revision')
    return 'official-'+_ref(reference)+'-r'+str(revision)


def _materialize_runtime(directory:Path,*,reference:str,revision:int,environment:str,target_root:Path,bot_owned=False,files=None):
    """Copy a single immutable configuration into the existing runtime mount.

    The host supplies target_root. Caller must not expose it in request fields.
    Source and runtime copies have different ownership. The runtime's own strict
    Credentials verifier rechecks the actual Linux file and ACL when starting.
    """
    bundle=read_credentials(directory,reference)
    if bundle.environment!=environment:raise InstanceError('Official credential environment differs from configuration')
    name=runtime_name(reference,revision);target=target_root/name
    expected_uid=10001 if bot_owned and os.geteuid()==0 else os.geteuid()
    root_info=target_root.lstat()
    if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid not in {os.geteuid(),expected_uid} or stat.S_IMODE(root_info.st_mode)&0o077:
        raise InstanceError('Official runtime directory is unavailable')
    from .official_files import host_file_configuration
    host={'version':1,'account':bundle.account.key,'onebot_accounts':[]}
    file_config=host_file_configuration(files,account=bundle.account)
    if file_config is not None:host['files']=file_config
    values={'app-id':bundle.credentials.app_id,'app-secret':bundle.credentials.app_secret,'bind-pepper':bundle.pepper,
        'host.json':json.dumps(host,ensure_ascii=False,separators=(',',':'))}
    values={key:value.encode('utf-8') for key,value in values.items()}
    if target.exists() or target.is_symlink():
        info=target.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=expected_uid or stat.S_IMODE(info.st_mode)!=0o700:
            raise InstanceError('Official runtime directory is unavailable')
        if set(x.name for x in target.iterdir())!=set(values):raise InstanceError('Official runtime copy is incomplete')
        for key,expected in values.items():
            fd=os.open(target/key,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
            try:
                info=os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid!=expected_uid or stat.S_IMODE(info.st_mode)!=0o600:
                    raise InstanceError('Official runtime copy is unsafe')
                if os.read(fd,32769)!=expected:raise InstanceError('Official runtime revision has conflicting contents')
            finally:os.close(fd)
        return name
    temporary=target_root/('.official-'+secrets.token_hex(16))
    temporary.mkdir(mode=0o700)
    try:
        for key,data in values.items():
            _write(temporary/key,data)
            if expected_uid!=os.geteuid():os.chown(temporary/key,expected_uid,expected_uid)
        descriptor=os.open(temporary,os.O_RDONLY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
        if expected_uid!=os.geteuid():os.chown(temporary,expected_uid,expected_uid)
        os.rename(temporary,target)
        descriptor=os.open(target_root,os.O_RDONLY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
    finally:
        if temporary.exists():
            for key in values:(temporary/key).unlink(missing_ok=True)
            temporary.rmdir()
    return name


def materialize_runtime(directory:Path,**kwargs):
    try:return _materialize_runtime(directory,**kwargs)
    except (OSError,ValueError,TypeError):
        raise InstanceError('Official runtime credentials could not be prepared') from None


def validate_backup_bundle(value):
    """Private snapshot data; never return this object through controller status."""
    try:
        if type(value) is not dict or set(value) != {'account', 'app_id', 'app_secret', 'environment', 'pepper'}:
            raise ValueError
        credentials = Credentials(value['app_id'], value['app_secret'])
        account = AccountKey('qq-official', credentials.app_id, value['environment'])
        if value['environment'] not in {'production', 'sandbox'} or account.key != value['account'] or len(account.key.encode()) > 512:
            raise ValueError
        if type(value['pepper']) is not str or not re.fullmatch('[a-f0-9]{64}', value['pepper']):
            raise ValueError
        return dict(value)
    except (ValueError, TypeError, UnicodeError):
        raise InstanceError('Official backup credentials are incomplete') from None


def backup_bundle(directory, reference):
    bundle = read_credentials(directory, reference)
    return validate_backup_bundle(dict(account=bundle.account.key, app_id=bundle.credentials.app_id,
        app_secret=bundle.credentials.app_secret, environment=bundle.environment, pepper=bundle.pepper))


def restore_bundle(directory, value, *, reference=None):
    """Recreate exactly the backed-up account/pepper; no replacement identities.

    The reference override is used only by the host restore journal to copy its
    verified candidate bundle before publishing instance.json. Never a UI field.
    """
    value = validate_backup_bundle(value)
    reference = _ref(reference) if reference is not None else secrets.token_hex(16)
    parent = _root(directory, create=True)
    folder = parent / reference
    if folder.exists():
        if backup_bundle(directory, reference) != value:
            raise InstanceError('Official restore credentials conflict')
        return reference
    folder.mkdir(mode=0o700)
    try:
        values = {'app-id': value['app_id'], 'app-secret': value['app_secret'], 'bind-pepper': value['pepper'],
            'metadata.json': json.dumps({'version': 1, 'environment': value['environment']}, separators=(',', ':'))}
        for name, data in values.items():
            _write(folder / name, data.encode())
        for path in (folder, parent):
            fd = os.open(path, os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
        if backup_bundle(directory, reference) != value:
            raise ValueError
        return reference
    except (OSError, ValueError, TypeError):
        for name in _FILES:
            (folder / name).unlink(missing_ok=True)
        folder.rmdir()
        raise InstanceError('Official restore credentials are unavailable') from None
