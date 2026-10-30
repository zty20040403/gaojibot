"""Official file services: owned bytes, one uploader and the shared sender."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hashlib
import json
import re
from pathlib import Path

from src.plugins.ai_chat.agent.control import LeaseLost
from src.plugins.ai_chat.application.owned_sandbox import ArtifactAvailability, GenerationLimits
from src.plugins.ai_chat.project_blobs import ProjectBlobStore
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey
from src.sandbox_runtime.source_fence import active_sandbox_source
from .file_artifacts import ArtifactStore, current_source
from .file_authority import assert_file_read_current
from .file_access import ArtifactRef, LinkError, VerifiedStream
from .file_runtime import OfficialFileRuntime
from .file_upload import FileUploader, MAX_FILE_BYTES
from .lease import assert_epoch


@dataclass(frozen=True)
class FileServiceConfig:
    private_directory: Path = field(repr=False)
    manager: object = field(repr=False)
    generation_enabled: bool = False
    generation_limits: GenerationLimits = GenerationLimits(max_file_bytes=MAX_FILE_BYTES, max_total_artifact_bytes=256*1024*1024)
    max_file_bytes: int = MAX_FILE_BYTES
    quota_bytes: int = 2 * 1024 * 1024 * 1024
    max_artifacts: int = 1000
    min_free_bytes: int = 64 * 1024 * 1024
    account_capacity: int = 16
    actor_capacity: int = 4
    destroy_expired_resources: bool = True
    source_authority: object = None
    existing_artifact_authority: object = None


def file_services_factory(config=None):
    async def create(runtime, token, rest):
        return FileServices(runtime, token, rest, config) if config is not None else None
    return create


class FileServices:
    def __init__(self, runtime, token, rest, config):
        self.runtime, self.token, self.config = runtime, token, config
        self.calls, self.transaction, self.jobs = runtime._calls, runtime.deliveries.transaction, runtime.jobs
        self.admission, self.clock = runtime.store.admitted, runtime.store.clock
        self.closed = False
        self._maintenance_lock = asyncio.Lock()
        self._orphan_cursor = self._capacity_cursor = None
        self.payloads = None
        self.directory = config.private_directory / hashlib.sha256(token.account_key.encode()).hexdigest()
        (self.directory / 'objects').mkdir(parents=True, exist_ok=True, mode=0o700)
        self.artifacts = ArtifactStore(transaction=self.transaction, jobs=self.jobs,
            blobs=ProjectBlobStore(self.directory / 'objects'), clock=self.clock,
            max_bytes=config.max_file_bytes, quota_bytes=config.quota_bytes,
            max_artifacts=config.max_artifacts, account_key=token.account_key, maintenance_guard=self._account)
        self.uploader = FileUploader(rest, clock=self.clock)
        from .file_dispatcher import OfficialFileDispatcher
        self.dispatcher = OfficialFileDispatcher(self)
        self.generation = OfficialFileRuntime(account=AccountKey.parse(token.account_key), token=token,
            artifact_store=self.artifacts, database_calls=self.calls, manager=config.manager,
            admission=self.admission, route_availability=self.readiness, enabled=config.generation_enabled,
            limits=config.generation_limits, account_limit=config.account_capacity, actor_limit=config.actor_capacity)
        self.generation.send_artifact = self.dispatcher.send_artifact

    def _local(self):
        return not self.closed and not self.runtime._stopping and self.runtime._token is self.token

    def _account(self, cursor):
        if not self._local():
            raise LeaseLost('file_services_unavailable')
        assert_epoch(cursor, self.token)

    async def _fence(self):
        def check():
            with self.transaction() as cursor:
                self._account(cursor)
        await self.calls(check)

    async def readiness(self, binding):
        def check():
            with self.transaction() as cursor:
                self._account(cursor)
                current_source(cursor, binding.source, int(self.clock()))
                return ArtifactAvailability(True, min(MAX_FILE_BYTES, self.config.max_file_bytes), 'native')
        return await self.calls(check)

    def _route(self, cursor, artifact, route):
        return (route == 'native' and artifact['byte_size'] <= self.config.max_file_bytes
            and self.admission(cursor, EndpointKey.parse(artifact['endpoint_key']),
                               ActorKey.parse(artifact['requester_actor_key'])))

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
            await self.calls(self.artifacts.collect_expired, limit=50)
            _, self._orphan_cursor = await self.calls(self.artifacts.collect_orphans,
                after_sha=self._orphan_cursor, limit=50)
            await self._capacity()

    async def close(self):
        self.closed = True
        await self.uploader.close()
