"""Immutable bytes/manifest plus existing durable jobs, no transport.

Only a live, host-created SourceBinding can register. Read references and hashes
are metadata, never source authority. This file does not enable generation.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import mimetypes
import heapq
import os
import stat
import re
import secrets
import threading
import time
import unicodedata
import weakref

from src.plugins.ai_chat.adapters.qq_official import turn_source as execution
from src.plugins.ai_chat.execution_control import LeaseLost
from .image_formats import artifact_filename as canonical_artifact_filename
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey

MAX_BYTES = 100 * 1024 * 1024
BLOB_LOCK = -634005064065
_issued = weakref.WeakValueDictionary()
_issued_lock = threading.Lock()


class ArtifactError(ValueError):
    """Fixed operator-safe reason only."""


def _time(value):
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ArtifactError('invalid_artifact_time')
    return value


def _key(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
        raise ArtifactError('invalid_artifact_key')
    return value


def _ref(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value):
        raise ArtifactError('invalid_artifact_ref')
    return value


def _clean_filename(value):
    if (not isinstance(value, str) or not 1 <= len(value.encode('utf-8')) <= 180
            or value in {'.', '..'} or value != value.strip()
            or any(c in '/\\' or unicodedata.category(c) in {'Cc', 'Cf', 'Cs'} for c in value)):
        raise ArtifactError('invalid_artifact_filename')
    return value


def detect_content(content, filename, max_bytes):
    """Detect attachment format; independent document acceptance remains separate."""
    if (type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BYTES
            or type(content) is not bytes or not 1 <= len(content) <= max_bytes):
        raise ArtifactError('artifact_size_unavailable')
    _clean_filename(filename)
    return mimetypes.guess_type(filename)[0] or 'application/octet-stream'


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False)
class SourceBinding:
    token: object
    job: object
    admission: object
    handle: object | None
    task_id: int | None
    pending_deadline_at: int
    endpoint: EndpointKey
    actor: ActorKey
    source_execution_id: str | None
    source_key: str
    deadline_at: int
    source_native_message_id: str | None
    extension: object | None = None
    source_migration_item_id: str | None = None

    def __repr__(self):
        return 'SourceBinding(<protected>)'


def _issue(binding):
    with _issued_lock:
        _issued[id(binding)] = binding
    return binding


def _require(binding):
    with _issued_lock:
        if not isinstance(binding, SourceBinding) or _issued.get(id(binding)) is not binding:
            raise LeaseLost('unissued_artifact_source')


def foreground_source(cursor, *, token, handle, job, admission, now, pending_deadline_at):
    row = execution.assert_execution_current(cursor, token=token, handle=handle,
        job=job, admission=admission, now=_time(now))
    _time(pending_deadline_at)
    if (row['state'] != 'executing' or row['execution_kind'] != 'chat'
            or now >= row['deadline_at'] or not now < pending_deadline_at <= now + 86400):
        raise LeaseLost('artifact_source_unavailable')
    return _issue(SourceBinding(token, job, admission, handle, None, pending_deadline_at,
        handle.endpoint, handle.actor, handle.execution_id, 'turn:' + handle.execution_id, row['deadline_at'],
        handle.source_native_message_id))




def current_source(cursor, binding, now):
    _require(binding)
    _time(now)
    if now >= min(binding.deadline_at, binding.pending_deadline_at):
        raise LeaseLost('artifact_source_expired')
    if binding.extension is not None:
        # Only a trusted host issuer can register this weak-issued binding.
        # The extension must re-read its exact persistent domain authority.
        return binding.extension.current_source(cursor, binding, now)
    if binding.task_id is None:
        try:
            row = execution.assert_execution_current(cursor, token=binding.token, handle=binding.handle,
                job=binding.job, admission=binding.admission, now=now)
        except Exception:
            raise LeaseLost('artifact_source_unavailable') from None
        if row['state'] != 'executing' or row['execution_kind'] != 'chat':
            raise LeaseLost('artifact_source_not_executing')
        source = dict(row)
        source['source_canonical_message_id'] = row['canonical_message_id']
        source['source_scope_key'] = row['scope_key']
    else:
        raise LeaseLost('retired_subagent_source')
    if (source['endpoint_key'] != binding.endpoint.key or source['requester_actor_key'] != binding.actor.key
            or source['account_key'] != binding.endpoint.account.key
            or source['source_native_message_id'] != binding.source_native_message_id
            or source.get('source_execution_id',source.get('execution_id')) != binding.source_execution_id):
        raise LeaseLost('artifact_source_conflict')
    return source


@dataclass(frozen=True, repr=False)
class ArtifactRecord:
    artifact_id: str
    sha256: str
    byte_size: int
    mime: str
    filename: str
    draft: bool
    pending_deadline_at: int
    retain_until: int
    bytes_state: str
    delivery_state: str
    created: bool = False

    @property
    def message_accepted(self):
        return self.delivery_state == 'message_accepted'

    def __repr__(self):
        return 'ArtifactRecord(<protected>)'


def _record(row, created=False):
    return ArtifactRecord(*(row[k] for k in ('artifact_id','sha256','byte_size','mime','filename','draft',
        'pending_deadline_at','retain_until','bytes_state','delivery_state')), created=created)


def file_job_kind(account):
    from .keys import account_job_kind
    return account_job_kind('file', account.key)


class ArtifactStore:
    """Synchronous host port. Async callers use the existing DatabaseCalls.

    The private blob lock precedes source locks on every mutation. No external
    I/O occurs here; bounded CAS filesystem writes are serialized with GC. A
    rollback may leave an orphan, which cannot be collected before the grace.
    """
    def __init__(self, *, transaction, jobs, blobs, clock=time.time,
                 max_bytes=MAX_BYTES, quota_bytes=512*1024*1024,max_artifacts=1000, account_key=None, maintenance_guard=None):
        if (type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BYTES
                or type(quota_bytes) is not int or not max_bytes <= quota_bytes <= 10*1024**3
                or type(max_artifacts) is not int or not 1 <= max_artifacts <= 10000):
            raise ArtifactError('invalid_artifact_limits')
        if account_key is not None and (not isinstance(account_key, str)
                or AccountKey.parse(account_key).platform != 'qq-official'):
            raise ArtifactError('invalid_artifact_account')
        if maintenance_guard is not None and not callable(maintenance_guard):
            raise ArtifactError('invalid_artifact_guard')
        self.account_key, self.maintenance_guard = account_key, maintenance_guard
        self.transaction, self.jobs, self.blobs = transaction, jobs, blobs
        self.clock, self.max_bytes, self.quota_bytes = clock, max_bytes, quota_bytes
        self.max_artifacts=max_artifacts

    def _guard(self, cursor):
        if self.maintenance_guard is not None:
            self.maintenance_guard(cursor)

    def _belongs(self, row):
        return row is not None and (self.account_key is None or row['account_key'] == self.account_key)

    def _now(self):
        return _time(int(self.clock()))

    def _physical_usage(self):
        """Metadata inventory under BLOB_LOCK; never follows untrusted links."""
        used, objects = 0, 0

        def count(entry):
            nonlocal used, objects
            info = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise ArtifactError('artifact_storage_inventory')
            used += info.st_size
            objects += 1
            # New writes cannot legitimately grow this inventory past its hard
            # quota. Stop on the first excess, including unknown staging files,
            # rather than scanning arbitrary host-added directory contents.
            if used > self.quota_bytes or objects > self.max_artifacts:
                raise ArtifactError('artifact_storage_quota')

        for directory in (self.blobs.root / 'blobs', self.blobs.root / 'staging'):
            if not stat.S_ISDIR(directory.lstat().st_mode):
                raise ArtifactError('artifact_storage_inventory')
        with os.scandir(self.blobs.root / 'blobs') as shards:
            for shard in shards:
                if not re.fullmatch('[a-f0-9]{2}', shard.name) or not shard.is_dir(follow_symlinks=False):
                    raise ArtifactError('artifact_storage_inventory')
                with os.scandir(shard.path) as entries:
                    for entry in entries:
                        if not re.fullmatch('[a-f0-9]{64}', entry.name) or not entry.name.startswith(shard.name):
                            raise ArtifactError('artifact_storage_inventory')
                        count(entry)
        with os.scandir(self.blobs.root / 'staging') as entries:
            for entry in entries:
                count(entry)
        return used, objects

    def physical_usage(self):
        """Read-only (bytes, objects), including private captures and orphans.

        Owns its BLOB_LOCK -> account transaction. Do not invoke while holding a
        separate account/source lock. A host readiness observation is advisory;
        only capture/register's same-lock check reserves physical bytes.
        """
        try:
            with self.transaction() as cursor:
                cursor.execute('SELECT pg_advisory_xact_lock(?)', (BLOB_LOCK,))
                self._guard(cursor)
                return self._physical_usage()
        except ArtifactError:
            raise
        except Exception:
            raise ArtifactError('artifact_storage_unavailable') from None

    def _store_blob(self, content):
        """Caller holds BLOB_LOCK and a current source fence.

        Count the actual private CAS and abandoned staging bytes, including
        pre-verdict captures and DB-rollback orphans. Account-bound production
        stores have independent roots; an unbound test/legacy root is charged
        conservatively as a whole. The inventory has no model-supplied path.
        """
        sha, size = hashlib.sha256(content).hexdigest(), len(content)
        try:
            try:
                self.blobs.path(sha).lstat()
            except FileNotFoundError:
                pass
            else:
                # Reusing an already immutable object allocates no temp file or
                # new bytes, even if a later configured quota has decreased.
                self.blobs.verify(sha, size)
                return self.blobs.reference(sha, size)
            used, objects = self._physical_usage()
            if used + size > self.quota_bytes or objects >= self.max_artifacts:
                raise ArtifactError('artifact_storage_quota')
            blob = self.blobs.put(content)
            if (blob.sha256, blob.byte_size) != (sha, size):
                raise ArtifactError('artifact_blob_integrity')
            return blob
        except ArtifactError:
            raise
        except Exception:
            raise ArtifactError('artifact_storage_unavailable') from None

    def capture_verified(self, binding, *, content, filename):
        """Persist bounded, format-checked bytes without a manifest or file job.

        The caller is the host-owned export/acceptance pipeline. A returned Blob
        is a fingerprint, not permission to read or register it for another task.
        The existing task journal supplies the durable capture/source relation.
        """
        _require(binding)
        if self.account_key is not None and binding.endpoint.account.key != self.account_key:
            raise LeaseLost('artifact_account_mismatch')
        filename = canonical_artifact_filename(filename, draft=False)
        detect_content(content, filename, self.max_bytes)
        with self.transaction() as cursor:
            cursor.execute('SELECT pg_advisory_xact_lock(?)', (BLOB_LOCK,))
            self._guard(cursor)
            current_source(cursor, binding, self._now())
            blob = self._store_blob(content)
            current_source(cursor, binding, self._now())
            return blob

    def register(self, binding, *, content, filename, logical_request_key, draft=False, draft_reason=''):
        _require(binding)
        if self.account_key is not None and binding.endpoint.account.key != self.account_key:
            raise LeaseLost('artifact_account_mismatch')
        _key(logical_request_key)
        if (type(draft) is not bool or not isinstance(draft_reason, str)
                or len(draft_reason.encode()) > 1000 or (not draft and draft_reason)):
            raise ArtifactError('invalid_artifact_draft')
        filename = canonical_artifact_filename(filename, draft=draft)
        mime = detect_content(content, filename, self.max_bytes)
        sha = hashlib.sha256(content).hexdigest()
        key = hashlib.sha256(('qq-artifact:v1:' + binding.endpoint.account.key + ':'
            + binding.source_key + ':' + logical_request_key).encode()).hexdigest()
        with self.transaction() as cursor:
            cursor.execute('SELECT pg_advisory_xact_lock(?)', (BLOB_LOCK,))
            self._guard(cursor)
            source = current_source(cursor, binding, self._now())
            existing = cursor.execute('''SELECT a.*,d.state AS delivery_state FROM qq_official_file_artifacts a
                JOIN qq_official_file_deliveries d USING(artifact_id) WHERE request_key=?''', (key,)).fetchone()
            if existing:
                expected = {'sha256':sha,'byte_size':len(content),'mime':mime,'filename':filename,
                    'draft':draft,'draft_reason':draft_reason,'pending_deadline_at':binding.pending_deadline_at,
                    'endpoint_key':binding.endpoint.key,'requester_actor_key':binding.actor.key}
                if any(existing[k] != v for k,v in expected.items()):
                    raise ArtifactError('artifact_request_conflict')
                if binding.extension is not None:
                    binding.extension.bind_artifact(cursor,binding,dict(existing),now=self._now(),existing=True)
                elif existing.get('source_extension_kind') is not None:
                    raise ArtifactError('artifact_source_conflict')
                return _record(existing)
            usage = cursor.execute('''SELECT COALESCE(sum(byte_size),0) AS used,count(*) AS objects
                FROM qq_official_file_artifacts WHERE account_key=? AND bytes_state='available' ''',
                (binding.endpoint.account.key,)).fetchone()
            if usage['used'] + len(content) > self.quota_bytes or usage['objects']>=self.max_artifacts:
                raise ArtifactError('artifact_storage_quota')
            blob = self._store_blob(content)
            if (blob.sha256, blob.byte_size) != (sha, len(content)):
                raise ArtifactError('artifact_blob_integrity')
            # Source could expire while a bounded disk write is in progress.
            source = current_source(cursor, binding, self._now())
            now = self._now()
            artifact_id = secrets.token_hex(16)
            fields = dict(artifact_id=artifact_id, request_key=key, account_key=binding.endpoint.account.key,
                endpoint_key=binding.endpoint.key, requester_actor_key=binding.actor.key,
                source_execution_id=binding.source_execution_id, source_task_id=binding.task_id,
                source_task_revision=1 if binding.task_id is not None else None,
                source_canonical_message_id=source['source_canonical_message_id'],
                source_native_message_id=source['source_native_message_id'], source_scope_key=source['source_scope_key'],
                source_job_id=binding.job.job_id, source_job_lease_generation=binding.job.lease_generation,
                source_account_epoch=binding.token.epoch, endpoint_generation=source['endpoint_generation'],
                route_epoch=source['route_epoch'], sha256=sha, byte_size=len(content), mime=mime, filename=filename,
                draft=draft, draft_reason=draft_reason, created_at=now,
                pending_deadline_at=binding.pending_deadline_at, retain_until=binding.pending_deadline_at)
            if binding.extension is not None:
                if binding.extension.kind not in {'project','migration','background'}:
                    raise ArtifactError('artifact_extension_unavailable')
                fields['source_extension_kind'] = binding.extension.kind
                if binding.extension.kind=='migration' or binding.source_migration_item_id is not None:
                    if (binding.source_migration_item_id is None or binding.source_execution_id is not None
                            or binding.source_native_message_id is not None
                            or (binding.task_id is None and getattr(binding.extension,'source_kind',None)!='file')):
                        raise ArtifactError('artifact_migration_source_invalid')
                    fields['source_migration_item_id']=binding.source_migration_item_id
            cursor.execute('INSERT INTO qq_official_file_artifacts (' + ','.join(fields)
                + ') VALUES (' + ','.join('?' for _ in fields) + ')', tuple(fields.values()))
            if binding.extension is not None:
                # Domain binding and artifact commit atomically before the file
                # worker can claim the ordinary durable job below.
                binding.extension.bind_artifact(cursor,binding,fields,now=now,existing=False)
            job, _ = self.jobs.enqueue(kind=file_job_kind(binding.endpoint.account),
                idempotency_key='qq-file:' + key, scope_key='qq-file-endpoint:'
                    + hashlib.sha256(binding.endpoint.key.encode()).hexdigest(),
                payload={'artifact_id':artifact_id}, now=now, max_attempts=5,
                resume_on_lease_loss=True, transaction_cursor=cursor)
            cursor.execute('''INSERT INTO qq_official_file_deliveries(artifact_id,job_id,created_at,updated_at)
                VALUES (?,?,?,?)''', (artifact_id, job.job_id, now, now))
            return _record({**fields,'bytes_state':'available','delivery_state':'stored'}, True)

    def get(self, artifact_id):
        _ref(artifact_id)
        with self.transaction() as cursor:
            row = cursor.execute('''SELECT a.*,d.state AS delivery_state FROM qq_official_file_artifacts a
                JOIN qq_official_file_deliveries d USING(artifact_id) WHERE artifact_id=?
                AND (CAST(? AS TEXT) IS NULL OR a.account_key=?)''',
                (artifact_id,self.account_key,self.account_key)).fetchone()
            return _record(row) if self._belongs(row) else None

    def read_verified(self, artifact_id, *, sha256, byte_size):
        """Trusted transport read after its own source/publication check.

        Keeps a bounded immutable byte copy. A digest/ref does not authorize
        calling this host-only method from a model or an HTTP request.
        """
        _ref(artifact_id)
        _key(sha256)
        if type(byte_size) is not int or not 1 <= byte_size <= self.max_bytes:
            raise ArtifactError('invalid_artifact_size')
        with self.transaction() as cursor:
            cursor.execute('SELECT pg_advisory_xact_lock(?)', (BLOB_LOCK,))
            self._guard(cursor)
            row = cursor.execute('''SELECT * FROM qq_official_file_artifacts WHERE artifact_id=?
                AND (CAST(? AS TEXT) IS NULL OR account_key=?) FOR SHARE''',
                (artifact_id,self.account_key,self.account_key)).fetchone()
            if (not self._belongs(row) or row['bytes_state'] != 'available' or row['retain_until'] <= self._now()
                    or row['sha256'] != sha256 or row['byte_size'] != byte_size):
                raise ArtifactError('artifact_bytes_unavailable')
            try:
                content = self.blobs.read(sha256, max_bytes=self.max_bytes)
            except Exception:
                raise ArtifactError('artifact_bytes_unavailable') from None
            if len(content) != byte_size or hashlib.sha256(content).hexdigest() != sha256:
                raise ArtifactError('artifact_bytes_corrupt')
            return content

    def collect_expired(self, *, limit=100):
        """Bounded automatic lifecycle; no source grants or send operations.

        Publication schema must be present before maintenance is enabled.
        Each metadata tombstone survives byte cleanup and keeps all audit FKs.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ArtifactError('invalid_artifact_collection_limit')
        removed = 0
        with self.transaction() as cursor:
            cursor.execute('SELECT pg_advisory_xact_lock(?)', (BLOB_LOCK,))
            self._guard(cursor)
            now = self._now()
            rows = cursor.execute('''SELECT * FROM qq_official_file_artifacts
                WHERE bytes_state='available' AND retain_until<=? AND (CAST(? AS TEXT) IS NULL OR account_key=?)
                ORDER BY retain_until,artifact_id LIMIT ? FOR UPDATE SKIP LOCKED''', (now,self.account_key,self.account_key,limit)).fetchall()
            for row in rows:
                # A publication cannot be created concurrently: its FOR SHARE
                # artifact lock serializes with this already-held FOR UPDATE.
                active = cursor.execute('''SELECT 1 FROM qq_official_file_publications
                    WHERE artifact_id=? AND state='active' AND expires_at>? LIMIT 1''',
                    (row['artifact_id'],now)).fetchone()
                if active:
                    continue
                cursor.execute('''UPDATE qq_official_file_artifacts SET bytes_state='deleted',bytes_deleted_at=?,cleanup_pending=TRUE
                    WHERE artifact_id=? AND bytes_state='available' ''', (now,row['artifact_id']))
                cursor.execute('''UPDATE qq_official_file_deliveries SET state='expired',reason_code='artifact_expired',updated_at=?
                    WHERE artifact_id=? AND state NOT IN ('message_accepted','delivery_unknown','failed','cancelled','expired')''',
                    (now,row['artifact_id']))
                removed += 1
        # Commit tombstones before unlink. A database rollback must never leave
        # an available manifest pointing at bytes deleted by this collector.
        self.retry_cleanup(limit=limit)
        return removed

    def retry_cleanup(self, *, limit=100):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ArtifactError('invalid_artifact_collection_limit')
        completed = 0
        with self.transaction() as cursor:
            candidates = cursor.execute('''SELECT artifact_id,sha256 FROM qq_official_file_artifacts
                WHERE bytes_state='deleted' AND cleanup_pending AND cleanup_retry_at<=? AND (CAST(? AS TEXT) IS NULL OR account_key=?)
                ORDER BY cleanup_retry_at,bytes_deleted_at,artifact_id LIMIT ?''',(self._now(),self.account_key,self.account_key,limit)).fetchall()
        for candidate in candidates:
            with self.transaction() as cursor:
                cursor.execute('SELECT pg_advisory_xact_lock(?)', (BLOB_LOCK,))
                self._guard(cursor)
                current = cursor.execute('''SELECT 1 FROM qq_official_file_artifacts
                    WHERE artifact_id=? AND bytes_state='deleted' AND cleanup_pending AND (CAST(? AS TEXT) IS NULL OR account_key=?) FOR UPDATE''',
                    (candidate['artifact_id'],self.account_key,self.account_key)).fetchone()
                if current is None:
                    continue
                referenced = cursor.execute('''SELECT 1 FROM qq_official_file_artifacts
                    WHERE sha256=? AND bytes_state='available' AND (CAST(? AS TEXT) IS NULL OR account_key=?) LIMIT 1''', (candidate['sha256'],self.account_key,self.account_key)).fetchone()
                if not referenced:
                    try:
                        self.blobs.remove(candidate['sha256'])
                    except Exception:
                        # Keep the durable pending bit. Other candidates may
                        # still be cleaned in this bounded maintenance pass.
                        cursor.execute('''UPDATE qq_official_file_artifacts SET cleanup_retry_at=?
                            WHERE artifact_id=? AND cleanup_pending''',(min(self._now()+30,2**63-1),candidate['artifact_id']))
                        continue
                cursor.execute('''UPDATE qq_official_file_artifacts SET cleanup_pending=FALSE
                    WHERE artifact_id=? AND bytes_state='deleted' ''',(candidate['artifact_id'],))
                completed += 1
        return completed

    def collect_orphans(self, *, after_sha=None, limit=100):
        """Bounded candidate page; caller carries the returned lexical cursor.

        Enumeration uses O(limit) auxiliary memory. It does inspect filesystem
        directory metadata, so the configured object quota remains required.
        Referenced old objects advance the cursor just like unreferenced ones.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ArtifactError('invalid_artifact_collection_limit')
        if after_sha is not None:
            _key(after_sha)
        removed = 0
        with self.transaction() as cursor:
            cursor.execute('SELECT pg_advisory_xact_lock(?)', (BLOB_LOCK,))
            self._guard(cursor)
            cutoff = self._now() - 86400
            def inventory():
                for shard in (self.blobs.root/'blobs').iterdir():
                    if (not re.fullmatch('[a-f0-9]{2}',shard.name)
                            or not stat.S_ISDIR(shard.lstat().st_mode)):
                        continue
                    with os.scandir(shard) as entries:
                        for entry in entries:
                            sha=entry.name
                            if (re.fullmatch('[a-f0-9]{64}',sha) and sha.startswith(shard.name)
                                    and (after_sha is None or sha>after_sha)
                                    and entry.is_file(follow_symlinks=False)):
                                yield sha
            candidates=heapq.nsmallest(limit+1,inventory())
            page=candidates[:limit]
            for sha in page:
                if self.blobs.path(sha).lstat().st_mtime>cutoff:
                    continue
                if cursor.execute('''SELECT 1 FROM qq_official_file_artifacts
                    WHERE sha256=? AND bytes_state='available' AND (CAST(? AS TEXT) IS NULL OR account_key=?) LIMIT 1''', (sha,self.account_key,self.account_key)).fetchone() is None:
                    self.blobs.remove(sha)
                    removed += 1
            self.blobs.collect_staging(cutoff,limit=limit)
        return removed,(page[-1] if len(candidates)>limit else None)
