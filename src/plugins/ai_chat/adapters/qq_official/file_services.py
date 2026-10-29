"""Explicit host composition for owned artifacts; no configuration or listeners."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Callable

from src.plugins.ai_chat.agent.control import LeaseLost
from src.plugins.ai_chat.application.owned_sandbox import ArtifactAvailability, GenerationLimits
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from src.plugins.ai_chat.project_blobs import ProjectBlobStore
from src.qq_official.credentials import _verify_posix_acl
from src.sandbox_runtime.source_fence import active_sandbox_source
from .file_artifacts import ArtifactStore, current_source
from .file_authority import assert_file_read_current
from .file_dispatcher import OfficialFileDispatcher
from .file_links import ArtifactRef, FileCredentialVault, FileLinkPublisher, LinkError, VerifiedStream, _origin, create_download_app
from .file_payloads import TrustedFilePayloads
from .file_publications import PgPublicationStore
from .lease import LeaseLost as AccountLeaseLost, assert_epoch
from .image_formats import file_type_for_mime
from .native_media import CONTRACT
from .official_metrics import observe
from .native_vault import NativeUploadVault
from .send_policy import SendPolicyChoice, check_send_policy


@dataclass(frozen=True)
class NativeRouteEvidence:
    endpoint: EndpointKey = field(repr=False)
    endpoint_generation: int
    route_epoch: int
    capability_revision: int
    max_bytes: int
    verified_at: int
    expires_at: int
    contract: str = CONTRACT
    file_type: int = 4
    multipart: bool = False


@dataclass(frozen=True)
class FileServiceConfig:
    private_directory: Path = field(repr=False)
    manager: object = field(repr=False)
    # Trusted host ports; a model cannot configure routes or claim verification.
    route_authority: Callable | None = field(default=None, repr=False)
    native_evidence: Callable | None = field(default=None, repr=False)
    link_origin: str | None = field(default=None, repr=False)
    link_reachable: Callable | None = field(default=None, repr=False)
    generation_enabled: bool = False
    generation_limits: GenerationLimits = GenerationLimits()
    max_file_bytes: int = 20 * 1024 * 1024
    quota_bytes: int = 512 * 1024 * 1024
    max_artifacts: int = 1000
    min_free_bytes: int = 64 * 1024 * 1024
    account_capacity: int = 16
    actor_capacity: int = 4
    destroy_expired_resources: bool = False
    source_authority: Callable | None = field(default=None, repr=False)
    existing_artifact_authority: Callable | None = field(default=None, repr=False)
    # M7 typed route evidence; old two-argument port remains type-4 only.
    native_media_evidence: Callable | None = field(default=None, repr=False)
    # Retained public reads keep original expiry, independent of live proxy probes.
    # The complete existing publication/source checks always precede this port.
    retained_link_authority: Callable | None = field(default=None, repr=False)
    link_ttl_seconds: int = 3600
    multipart_configuration: object | None = field(default=None, repr=False)
    part_transport: object | None = field(default=None, repr=False)

    def __post_init__(self):
        if (not isinstance(self.private_directory, Path) or not self.private_directory.is_absolute()
                or type(self.generation_enabled) is not bool or type(self.destroy_expired_resources) is not bool
                or not isinstance(self.generation_limits, GenerationLimits)
                or type(self.link_ttl_seconds) is not int or not 60 <= self.link_ttl_seconds <= 3600
                or type(self.min_free_bytes) is not int or self.min_free_bytes < 0
                or any(type(value) is not int or value < 1 for value in
                       (self.max_file_bytes,self.quota_bytes,self.max_artifacts,
                        self.account_capacity,self.actor_capacity))
                or not self.actor_capacity <= self.account_capacity <= 100
                or any(port is not None and not callable(port) for port in
                       (self.route_authority,self.source_authority,self.existing_artifact_authority,self.native_media_evidence,
                        self.native_evidence,self.link_reachable,self.retained_link_authority))):
            raise ValueError('invalid_file_service_configuration')
        if self.multipart_configuration is not None:
            from .native_part_transport import MultipartConfiguration
            if type(self.multipart_configuration) is not MultipartConfiguration:
                raise ValueError('invalid_multipart_configuration')
        elif self.part_transport is not None:
            raise ValueError('incomplete_multipart_configuration')
        if self.link_origin is not None:
            _origin(self.link_origin, False)  # HTTPS public origin only; no implicit loopback deployment.


def _directory(path):
    path.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700
                or not stat.S_ISDIR(info.st_mode)):
            raise ValueError('private_artifact_directory_unavailable')
        _verify_posix_acl(fd)
    finally:
        os.close(fd)


def file_services_factory(config: FileServiceConfig | None = None):
    """Return runtime's explicit factory. None performs no filesystem/DB work."""
    async def create(runtime, token, rest):
        if config is None:
            return None
        work=asyncio.create_task(runtime._calls(FileServices, runtime, token, rest, config))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            # The constructor runs in a thread. Retain its result so cancellation
            # cannot lose open private vault descriptors.
            while not work.done():
                try: await asyncio.shield(work)
                except asyncio.CancelledError: continue
                except BaseException: break
            if not work.cancelled() and work.exception() is None:
                cleanup=asyncio.create_task(work.result().close())
                while not cleanup.done():
                    try: await asyncio.shield(cleanup)
                    except asyncio.CancelledError: continue
                cleanup.result()
            raise
    return create


class _AccountPublicationStore:
    """Keep the shared publication table scoped to this account's artifacts."""
    def __init__(self,service,store): self.service,self.store=service,store

    async def _get(self,method,*args):
        row=await getattr(self.store,method)(*args)
        if row is not None and await self.service.calls(self.service.artifacts.get,row.artifact.artifact_id) is None:
            row=None
        await self.service._fence()
        return row

    async def by_digest(self,value): return await self._get('by_digest',value)
    async def get(self,value): return await self._get('get',value)
    async def by_credential(self,value): return await self._get('by_credential',value)
    async def by_request(self,value): return await self._get('by_request',value)
    async def create(self,value):
        row=await self.store.create(value)
        await self.service._fence()
        return row
    async def deactivate(self,ref,state):
        if await self.get(ref) is not None:
            await self.store.deactivate(ref,state)
            await self.service._fence()
    async def record_served(self,ref,**kwargs):
        result=await self.store.record_served(ref,**kwargs) if await self.get(ref) is not None else False
        await self.service._fence()
        return result


class _DownloadStore:
    """Apply revocation to all public reads, including HEAD and each chunk."""
    def __init__(self, service, store):
        self.service,self.store = service,store

    async def _get(self, method, *args):
        try:
            row = await getattr(self.store,method)(*args)
            if row is None:
                return None
            await self.service.calls(self.service._download_current,row)
        except (LeaseLost, PermissionError, LinkError):
            return None
        return row

    async def by_digest(self,value): return await self._get('by_digest',value)
    async def get(self,value): return await self._get('get',value)
    async def by_credential(self,value): return await self._get('by_credential',value)
    async def by_request(self,value): return await self._get('by_request',value)
    async def deactivate(self,*args): return await self.store.deactivate(*args)
    async def record_served(self,ref,**kwargs):
        return await self.store.record_served(ref,**kwargs) if await self.get(ref) is not None else False


class FileServices:
    def __init__(self, runtime, token, rest, config):
        self.runtime,self.token,self.config = runtime,token,config
        self.calls,self.transaction,self.jobs = runtime._calls,runtime.deliveries.transaction,runtime.jobs
        self.admission,self.clock = runtime.store.admitted,runtime.store.clock
        self.closed = False
        self._maintenance_lock=asyncio.Lock()
        self._close_task=None
        self._native_cursor=self._link_cursor=self._orphan_cursor=self._capacity_cursor=None
        self.link_vault=self.native_vault=self.session_vault=None
        self._session_cursor=None
        try:
            _directory(config.private_directory)
            self.directory=config.private_directory/hashlib.sha256(token.account_key.encode()).hexdigest()
            _directory(self.directory)
            _directory(self.directory/'objects')
            blobs=ProjectBlobStore(self.directory/'objects')
            self.artifacts=ArtifactStore(transaction=self.transaction,jobs=self.jobs,blobs=blobs,
                clock=self.clock,max_bytes=config.max_file_bytes,quota_bytes=config.quota_bytes,
                max_artifacts=config.max_artifacts,account_key=token.account_key,maintenance_guard=self._account)
            self.native_vault=NativeUploadVault(self.directory/'native',clock=self.clock)
            self.link_vault=FileCredentialVault(self.directory/'links')
            part_transport=None
            if config.multipart_configuration is not None:
                from .native_session_vault import NativeSessionVault
                from .native_part_transport import PinnedPartTransport
                self.session_vault=NativeSessionVault(self.directory/'upload-sessions',clock=self.clock)
                part_transport=config.part_transport or PinnedPartTransport(config.multipart_configuration)
            self.payloads=TrustedFilePayloads(admission=self.admission,route_policy=self._route,
                native_vault=self.native_vault,link_vault=self.link_vault,link_origin=config.link_origin,
                source_authority=config.source_authority)
            self.dispatcher=OfficialFileDispatcher(token,self.artifacts,self.payloads,runtime.outbox,rest,
                calls=self.calls,lease_check=self._local,publisher_factory=self.publisher_factory if config.link_origin else None,
                check_delivery_ready=self.check_delivery_ready,clock=self.clock,
                link_ttl_seconds=config.link_ttl_seconds,
                multipart_configuration=config.multipart_configuration,multipart_vault=self.session_vault,
                part_transport=part_transport)
            from .file_runtime import OfficialFileRuntime
            from src.plugins.ai_chat.platform_types import AccountKey
            self.generation=OfficialFileRuntime(account=AccountKey.parse(token.account_key),token=token,
                artifact_store=self.artifacts,database_calls=self.calls,manager=config.manager,
                admission=self.admission,route_availability=self.readiness,enabled=config.generation_enabled,
                limits=config.generation_limits,account_limit=config.account_capacity,actor_limit=config.actor_capacity)
            self._publication_store=self._store()
            self._maintenance_publisher=self._publisher(self._publication_store) if config.link_origin else None
            self.download_publisher=self._publisher(_DownloadStore(self,self._publication_store)) if config.link_origin else None
            self.download_app=create_download_app(self.download_publisher) if self.download_publisher is not None else None
        except BaseException:
            for vault in (self.native_vault,self.link_vault,self.session_vault):
                if vault is not None: vault.close()
            raise

    def _local(self):
        return not self.closed and not self.runtime._stopping and self.runtime._token is self.token

    def _account(self,cursor):
        if not self._local(): raise LeaseLost('file_services_unavailable')
        try: assert_epoch(cursor,self.token)
        except AccountLeaseLost: raise LeaseLost('file_services_unavailable') from None

    def _fence_sync(self):
        with self.transaction() as cursor: self._account(cursor)

    async def _fence(self):
        await self.calls(self._fence_sync)

    def _link_ready(self):
        return self.config.link_origin is not None and self.config.link_reachable is not None and self.config.link_reachable() is True

    def _native_evidence(self,cursor,endpoint,kind):
        if self.config.native_media_evidence is not None:
            return self.config.native_media_evidence(cursor,endpoint,kind)
        if kind==4 and self.config.native_evidence is not None:
            return self.config.native_evidence(cursor,endpoint)
        return None

    def _native_limit(self,proof):
        if (proof.file_type==4 and proof.multipart is True and self.config.multipart_configuration is not None
                and self.dispatcher.multipart is not None):
            return min(proof.max_bytes,self.config.max_file_bytes,self.dispatcher.limits.total_bytes)
        return min(proof.max_bytes,self.dispatcher.limits.small_bytes,3_750_000)

    def _route(self,cursor,artifact,route):
        self._account(cursor)
        endpoint,actor=EndpointKey.parse(artifact['endpoint_key']),ActorKey.parse(artifact['requester_actor_key'])
        if (endpoint.account.key!=self.token.account_key or not actor.matches_endpoint(endpoint)
                or actor.identity_kind=='bot' or type(artifact.get('byte_size')) is not int
                or not 1<=artifact['byte_size']<=self.artifacts.max_bytes
                or self.config.route_authority is None
                or self.config.route_authority(cursor,endpoint,actor,route) is not True):
            return False
        row=cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=? AND account_key=? FOR SHARE',
            (endpoint.key,self.token.account_key)).fetchone()
        if (row is None or row['generation']!=artifact['endpoint_generation'] or row['route_epoch']!=artifact['route_epoch']
                or row['membership_state']!='present' or row['binding_state']=='revoked'
                or endpoint.kind=='group' and row['binding_state']!='active'
                or self.admission(cursor,endpoint,actor) is not True): return False
        if route=='link': return self._link_ready()
        if route!='native' or artifact.get('draft',False): return False
        kind=file_type_for_mime(artifact['mime'])
        proof=self._native_evidence(cursor,endpoint,kind)
        now=int(self.clock())
        if (not isinstance(proof,NativeRouteEvidence) or proof.endpoint!=endpoint
                or any(type(x) is not int for x in (proof.endpoint_generation,proof.route_epoch,proof.capability_revision,
                    proof.max_bytes,proof.verified_at,proof.expires_at,proof.file_type))
                or (proof.endpoint_generation,proof.route_epoch,proof.capability_revision)!=(row['generation'],row['route_epoch'],row['capability_revision'])
                or not 0<=proof.verified_at<=now<proof.expires_at
                or type(proof.multipart) is not bool or proof.multipart and proof.file_type!=4
                or not 1<=artifact['byte_size']<=self._native_limit(proof)
                or proof.contract!=CONTRACT or proof.file_type!=kind): return False
        from .file_dispatcher import native_rejected
        return not native_rejected(cursor,endpoint,endpoint_generation=row['generation'],route_epoch=row['route_epoch'],
            verified_at=proof.verified_at,contract=CONTRACT,file_type=kind)

    def check_delivery_ready(self,cursor,artifact,now):
        self._account(cursor)
        endpoint=EndpointKey.parse(artifact['endpoint_key'])
        row=cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=? AND account_key=? FOR UPDATE',
            (endpoint.key,self.token.account_key)).fetchone()
        if row is None:return False
        choice=check_send_policy(cursor,token=self.token,endpoint=endpoint,endpoint_row=row,
            source_native_id=artifact['source_native_message_id'],purpose='requested_artifact',
            deadline=min(artifact['retain_until'],artifact['pending_deadline_at']),now=now,
            certification=self.runtime.outbox.certification,allow_newer_windows=self.runtime.outbox.allow_newer_windows,
            allow_proactive_probe=False,verified_boundary=self.runtime.outbox.verified_boundary)
        return isinstance(choice,SendPolicyChoice)

    async def readiness(self,binding):
        def check():
            _directory(self.directory)
            # Snapshot the bounded physical inventory before taking source or
            # account locks. Mutations recheck atomically under BLOB_LOCK.
            used_bytes,objects=self.artifacts.physical_usage()
            with self.transaction() as cursor:
                self._account(cursor)
                source=current_source(cursor,binding.source,int(self.clock()))
                capacity=cursor.execute("SELECT count(*) AS total,count(*) FILTER (WHERE endpoint_key=? AND requester_actor_key=?) AS actor,count(*) FILTER (WHERE source_key=?) AS own FROM qq_official_generation_capacity WHERE account_key=? AND state='reserved'",(binding.endpoint.key,binding.actor.key,binding.source_key,self.token.account_key)).fetchone()
                # A successful captured run of this exact authenticated task can
                # be verified again after its old workspace has been destroyed.
                # This only keeps the route available: the capture reader still
                # requires exact persisted metadata/hash, and new hashes still
                # undergo the atomic physical quota check.
                captured=binding.source.task_id is not None and cursor.execute("""SELECT 1
                    FROM subagent_artifacts a JOIN subagent_runs r ON r.run_id=a.run_id AND r.task_id=a.task_id
                    WHERE a.task_id=? AND r.status IN ('succeeded','partial')
                      AND a.handle ~ '^snapshot:[a-f0-9]{64}:s[a-f0-9]{6}:/workspace/.+' LIMIT 1""",
                    (binding.source.task_id,)).fetchone() is not None
                existing=bool(capacity['own'] or captured)
                cap=min(self.config.max_file_bytes,self.config.generation_limits.max_file_bytes,
                    self.config.quota_bytes if existing else self.config.quota_bytes-used_bytes,
                    shutil.disk_usage(self.directory).free-self.config.min_free_bytes)
                # An existing workspace remains usable at the reservation cap.
                # Every new create still goes through the atomic capacity store.
                if (cap<=0 or not existing and (objects>=self.config.max_artifacts
                        or capacity['total']>=self.config.account_capacity or capacity['actor']>=self.config.actor_capacity)):
                    return ArtifactAvailability(False,0,'link')
                artifact=dict(endpoint_key=binding.endpoint.key,requester_actor_key=binding.actor.key,
                    endpoint_generation=source['endpoint_generation'],route_epoch=source['route_epoch'],byte_size=1,draft=False,
                    mime='application/pdf')  # Existing generation exposes PDF/ZIP, never image-route authority.
                if self._route(cursor,artifact,'link'):return ArtifactAvailability(True,int(cap),'link')
                if self._route(cursor,artifact,'native'):
                    proof=self._native_evidence(cursor,binding.endpoint,4)
                    return ArtifactAvailability(True,min(int(cap),self._native_limit(proof)),'native')
                return ArtifactAvailability(False,0,'link')
        return await self.calls(check)

    @contextmanager
    def _publication_transaction(self):
        with self.transaction() as cursor:
            self._account(cursor)
            yield cursor

    def _store(self,authority=None):
        def scoped(cursor,artifact,expiry):
            row=cursor.execute('SELECT account_key FROM qq_official_file_artifacts WHERE artifact_id=?',
                (artifact.artifact_id,)).fetchone()
            return (row is not None and row['account_key']==self.token.account_key and authority is not None
                    and authority(cursor,artifact,expiry) is True)
        return _AccountPublicationStore(self,PgPublicationStore(transaction_factory=self._publication_transaction,
            calls=self.calls,clock=self.clock,publication_authority=scoped))

    def publisher_factory(self,authority):
        return self._publisher(self._store(authority))

    def _publisher(self,store):
        return FileLinkPublisher(store=store,vault=self.link_vault,open_artifact=self.open_artifact,
            origin=self.config.link_origin,clock=self.clock)

    def _download_current(self,publication):
        with self.transaction() as cursor:
            self._account(cursor)
            row=cursor.execute('SELECT * FROM qq_official_file_publications WHERE publication_ref=? AND artifact_id=? FOR SHARE',
                (publication.publication_ref,publication.artifact.artifact_id)).fetchone()
            if (row is None or row['state']!='active' or row['expires_at']<=int(self.clock())
                    or row['origin']!=self.config.link_origin or row['token_digest']!=publication.token_digest):
                raise LinkError('publication_unavailable')
            delivery=cursor.execute('SELECT active_plan_id FROM qq_official_file_deliveries WHERE artifact_id=?',
                (publication.artifact.artifact_id,)).fetchone()
            if delivery is None or delivery['active_plan_id'] is None:raise LinkError('publication_unavailable')
            artifact,_=assert_file_read_current(cursor,token=self.token,artifact_id=publication.artifact.artifact_id,
                admission=self.admission,now=int(self.clock()),
                source_authority=self.config.source_authority)
            if self.config.retained_link_authority is not None:
                allowed = self.config.retained_link_authority(cursor,artifact) is True
                if self.config.existing_artifact_authority is not None:
                    allowed = allowed and self.config.existing_artifact_authority(cursor,artifact) is True
            elif self.config.existing_artifact_authority is None:
                allowed = self._route(cursor,artifact,'link')
            else:
                allowed = self._link_ready() and self.config.existing_artifact_authority(cursor,artifact) is True
            if not allowed:raise LinkError('publication_unavailable')
            if (artifact['sha256'],artifact['byte_size'])!=(publication.artifact.sha256,publication.artifact.size):
                raise LinkError('publication_unavailable')

    @asynccontextmanager
    async def open_artifact(self,ref):
        # HTTP store rechecks publication/source before headers and every chunk.
        # This port independently binds the immutable bytes to an active publication.
        def publication():
            with self.transaction() as cursor:
                self._account(cursor)
                row=cursor.execute("SELECT publication_ref FROM qq_official_file_publications WHERE artifact_id=? AND state='active' AND expires_at>? ORDER BY created_at DESC LIMIT 1",(ref.artifact_id,int(self.clock()))).fetchone()
                if row is None:raise LinkError('publication_unavailable')
                return row['publication_ref']
        try:
            key=await self.calls(publication)
            value=await self._publication_store.get(key)
            if value is None:raise LinkError('publication_unavailable')
            await self.calls(self._download_current,value)
            body=await self.calls(self.artifacts.read_verified,ref.artifact_id,sha256=ref.sha256,byte_size=ref.size)
            async def chunks():
                for offset in range(0,len(body),64*1024):
                    try: await self.calls(self._download_current,value)
                    except LeaseLost: raise LinkError('publication_unavailable') from None
                    yield body[offset:offset+64*1024]
            yield VerifiedStream(ref.sha256,ref.size,chunks())
        except LeaseLost:
            raise LinkError('publication_unavailable') from None

    def _console_current(self,artifact_id,expected=None):
        with self.transaction() as cursor:
            self._account(cursor)
            row,_=assert_file_read_current(cursor,token=self.token,artifact_id=artifact_id,
                admission=self.admission,now=int(self.clock()),source_authority=self.config.source_authority)
            ref=ArtifactRef(row['artifact_id'],row['sha256'],row['byte_size'],row['mime'],row['filename'])
            identity=tuple(row[key] for key in ('account_key','endpoint_key','requester_actor_key',
                'source_execution_id','source_job_id','source_task_id','source_task_revision',
                'source_canonical_message_id','source_native_message_id','source_scope_key',
                'endpoint_generation','route_epoch'))
            current=(ref,identity)
            if expected is not None and current!=expected:
                raise LinkError('console_artifact_unavailable')
            return current

    @asynccontextmanager
    async def console_artifact(self,artifact_id):
        """Trusted authenticated-console port; no public token, path or route.

        The host handler must authenticate its console session before calling
        and throughout streaming. This port separately derives actor/endpoint
        from the current account's immutable manifest and rechecks source
        authority before headers and each bounded chunk.
        """
        try:
            current=await self.calls(self._console_current,artifact_id)
            ref=current[0]
            body=await self.calls(self.artifacts.read_verified,artifact_id,sha256=ref.sha256,byte_size=ref.size)
            await self.calls(self._console_current,artifact_id,current)
            async def chunks():
                for offset in range(0,len(body),64*1024):
                    try: await self.calls(self._console_current,artifact_id,current)
                    except (Exception,LeaseLost):
                        raise LinkError('console_artifact_unavailable') from None
                    yield body[offset:offset+64*1024]
            yield ref,VerifiedStream(ref.sha256,ref.size,chunks())
        except (Exception,LeaseLost):
            raise LinkError('console_artifact_unavailable') from None

    def _expired_capacity(self):
        with self.transaction() as cursor:
            self._account(cursor)
            return [dict(r) for r in cursor.execute("SELECT * FROM qq_official_generation_capacity WHERE account_key=? AND state='reserved' AND expires_at<=? AND (CAST(? AS TEXT) IS NULL OR reservation_id>?) ORDER BY reservation_id LIMIT 20",(self.token.account_key,int(self.clock()),self._capacity_cursor,self._capacity_cursor)).fetchall()]

    def _release_absent(self,row):
        with self.transaction() as cursor:
            self._account(cursor)
            cursor.execute("UPDATE qq_official_generation_capacity SET state='released',released_at=?,release_evidence='host_verified_absent' WHERE reservation_id=? AND account_key=? AND owner=? AND source_key=? AND state='reserved' AND expires_at<=?",(int(self.clock()),row['reservation_id'],self.token.account_key,row['owner'],row['source_key'],int(self.clock())))

    async def _capacity(self):
        rows=await self.calls(self._expired_capacity)
        for row in rows:
            endpoint,actor=EndpointKey.parse(row['endpoint_key']),ActorKey.parse(row['requester_actor_key'])
            if endpoint.account.key!=self.token.account_key or not actor.matches_endpoint(endpoint):continue
            raw=json.dumps([endpoint.account.key,endpoint.key,actor.key],ensure_ascii=False,separators=(',',':'))
            owner='qqo:v1:'+hashlib.sha256(raw.encode()).hexdigest()+':x:'+hashlib.sha256(row['source_key'].encode()).hexdigest()[:32]
            if row['owner'] not in {owner,owner+':verify'}:continue
            await self._fence()
            guard=active_sandbox_source.set(self._fence)
            try:
                items=await self.config.manager.list(row['owner'])
                await self._fence()
                if not isinstance(items,list):continue
                if items and self.config.destroy_expired_resources:
                    for item in items:
                        sid=item.get('sandbox_id') if isinstance(item,dict) else None
                        if not isinstance(sid,str) or not re.fullmatch('s[0-9a-f]{6}',sid):break
                        await self._fence()
                        await self.config.manager.destroy(row['owner'],sid)
                        await self._fence()
                    items=await self.config.manager.list(row['owner'])
                    await self._fence()
                if items==[]:await self.calls(self._release_absent,row)
            except Exception:
                # Unknown host outcomes retain capacity. Other expired owners
                # can still be checked; cancellation/authority loss propagate.
                continue
            finally:active_sandbox_source.reset(guard)
        self._capacity_cursor=rows[-1]['reservation_id'] if len(rows)==20 else None

    async def maintain(self):
        async with self._maintenance_lock:
            await self._maintain()

    async def _maintain(self):
        await self._fence()
        removed=await self.calls(self.artifacts.collect_expired,limit=50)
        observe(getattr(self.runtime,'observer',None),'cleanup','completed',kind='artifact_tombstone',count=removed)
        await self._fence()
        removed,self._orphan_cursor=await self.calls(self.artifacts.collect_orphans,after_sha=self._orphan_cursor,limit=50)
        observe(getattr(self.runtime,'observer',None),'cleanup','completed',kind='orphan_blob',count=removed)
        await self._fence()
        result=await self.calls(self.native_vault.collect_expired,after_ref=self._native_cursor,limit=50)
        self._native_cursor=result['next_ref']
        if self.session_vault is not None:
            await self._fence()
            result=await self.calls(self.session_vault.collect_expired,after_ref=self._session_cursor,limit=50)
            self._session_cursor=result['next_ref']
        await self._fence()
        if self._maintenance_publisher is not None:
            result=await self._maintenance_publisher.collect_credentials(after_ref=self._link_cursor,limit=50)
            self._link_cursor=result['next_ref']
        await self._capacity()

    async def close(self):
        if self._close_task is None:
            self.closed=True
            self._close_task=asyncio.create_task(self._close_resources())
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            while not self._close_task.done():
                try: await asyncio.shield(self._close_task)
                except asyncio.CancelledError: continue
            self._close_task.result()
            raise

    async def _close_resources(self):
        async with self._maintenance_lock:
            try:
                if self.session_vault is not None:await self.calls(self.session_vault.close)
            finally:
                try: await self.calls(self.native_vault.close)
                finally: await self.calls(self.link_vault.close)
