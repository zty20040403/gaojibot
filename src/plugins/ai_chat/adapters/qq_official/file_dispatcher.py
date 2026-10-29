"""One durable file-job handler; uploads/publishes once, then hands off to M3.

The runtime owns the existing worker, account lease and draining DatabaseCalls.
No message is sent here. Unknown upload/publication outcomes never mint a new
attempt or credential. Multipart transport remains disabled in this handler.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import secrets
import threading
import time

from src.plugins.ai_chat.agent.control import LeaseLost
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey
from src.plugins.ai_chat.workers.durable_jobs import JobDeferred
from src.qq_official.errors import ERROR_CONTRACT
from .file_artifacts import _ref, _time, file_job_kind
from .file_links import ArtifactRef
from .lease import assert_epoch
from .native_media import CONTRACT, NativeLimits, UploadedMedia, build_small_upload, fingerprint
from .native_upload import OfficialMediaUploader, UploadGuardError, UploadIntent, issue_upload_permit
from .native_vault import NativeUploadBinding
from .protected_payload import ProtectedContentRef


class FileDispatchUnavailable(RuntimeError):
    def __init__(self):
        super().__init__('official_file_dispatch_unavailable')


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def _reason(value):
    return value if isinstance(value, str) and re.fullmatch('[a-z0-9_]{1,80}', value) else 'upload_unavailable'


# Persist the version in the existing safe failure-code field. Do not replace
# these fixed strings with mutable module constants when updating a protocol.
_REJECTION_CONTRACTS = {
    'native_file_unavailable_v1': ('qq-autogen-2026-08-10-zero-v1', 'qq-official-plan-2026-10-28'),
}


def native_rejected(cursor, endpoint, *, endpoint_generation, route_epoch,
                    verified_at, contract=CONTRACT, file_type=4):
    """Read exact endpoint/type/route rejection newer than trusted capability.

    A later trusted account acceptance must have a strictly newer verified_at;
    same-second rejection wins. Other endpoints, epochs or contracts never share
    this negative evidence. Only current artifact PDF/ZIP uploads are type 4.
    """
    if (not isinstance(endpoint, EndpointKey) or endpoint.account.platform != 'qq-official'
            or type(endpoint_generation) is not int or endpoint_generation < 1
            or type(route_epoch) is not int or route_epoch < 1
            or type(file_type) is not int):
        raise ValueError('invalid_native_rejection_scope')
    _time(verified_at)
    if file_type != 4:
        return False
    for marker, versions in _REJECTION_CONTRACTS.items():
        if versions != (contract, ERROR_CONTRACT):
            continue
        row = cursor.execute('''SELECT 1 FROM qq_official_file_stage_attempts s
            JOIN qq_official_file_artifacts a ON a.artifact_id=s.artifact_id
            WHERE a.account_key=? AND a.endpoint_key=? AND a.endpoint_generation=? AND a.route_epoch=?
              AND a.mime IN ('application/pdf','application/zip')
              AND s.operation='upload_small' AND s.state='rejected' AND s.failure_code=?
              AND s.platform_code=50015014 AND s.http_status IN (200,400) AND s.settled_at>=? LIMIT 1''',
            (endpoint.account.key, endpoint.key, endpoint_generation, route_epoch, marker, verified_at)).fetchone()
        if row is not None:
            return True
    return False


def _unsupported_stage(row):
    return (row['state'] == 'rejected' and row['operation'] == 'upload_small'
            and row['platform_code'] == 50015014 and row['http_status'] in {200, 400}
            and _REJECTION_CONTRACTS.get(row['failure_code']) == (CONTRACT, ERROR_CONTRACT))


class OfficialFileDispatcher:
    """Register this callable for account_job_kind('file', account.key).

    publisher_factory(authority) is a trusted host factory. It must construct a
    FileLinkPublisher whose PgPublicationStore uses the supplied synchronous
    publication_authority callback. check_delivery_ready is likewise a trusted
    synchronous cursor port; absent evidence denies publication by default.
    lease_check is a cheap synchronous local check immediately before I/O; SQL
    source/account/job validation also runs through calls before every request.
    """
    def __init__(self, token, artifacts, payloads, outbox, rest, *, calls,
                 lease_check, publisher_factory=None, check_delivery_ready=None,
                 clock=time.time, limits=NativeLimits()):
        if not callable(calls) or not callable(lease_check) or not callable(clock):
            raise ValueError('invalid_file_dispatcher_configuration')
        self.token, self.artifacts, self.payloads = token, artifacts, payloads
        self.outbox, self.rest, self.calls = outbox, rest, calls
        self.lease_check, self.publisher_factory = lease_check, publisher_factory
        self.check_delivery_ready = check_delivery_ready or (lambda *_: False)
        self.clock, self.limits = clock, limits
        self._clock_lock, self._last_now = threading.Lock(), 0

    def _now(self):
        with self._clock_lock:
            value = self.clock()
            if (type(value) not in {int, float} or not 0 <= value <= 2**63 - 1
                    or not math.isfinite(value)):
                raise ValueError('invalid_file_dispatch_clock')
            self._last_now = max(self._last_now, int(value))
            return self._last_now

    def _local(self):
        if self.lease_check() is not True:
            raise UploadGuardError()

    def _transaction(self, function, *args):
        with self.artifacts.transaction() as cursor:
            return function(cursor, *args)

    async def _db(self, function, *args):
        return await self.calls(self._transaction, function, *args)

    def _job(self, cursor, job, artifact_id, now):
        assert_epoch(cursor, self.token)
        row = cursor.execute('''SELECT j.*,d.artifact_id,a.account_key FROM durable_jobs j
            JOIN qq_official_file_deliveries d ON d.job_id=j.job_id
            JOIN qq_official_file_artifacts a ON a.artifact_id=d.artifact_id
            WHERE j.job_id=? FOR SHARE OF j''', (job.job_id,)).fetchone()
        if (row is None or row['account_key'] != self.token.account_key or row['artifact_id'] != artifact_id
                or row['status'] != 'running' or row['kind'] != job.kind
                or json.loads(row['payload_json']) != job.payload
                or row['lease_owner'] != job.lease_owner or row['lease_generation'] != job.lease_generation
                or row['attempts'] != job.attempts or (row['lease_until'] or 0) <= now):
            raise LeaseLost('file_job_unavailable')

    def _open(self, cursor, job, artifact_id):
        now = self._now()
        self._job(cursor, job, artifact_id, now)
        delivery = cursor.execute('SELECT * FROM qq_official_file_deliveries WHERE artifact_id=?',
                                  (artifact_id,)).fetchone()
        if delivery['state'] in {'handed_off', 'message_accepted', 'delivery_unknown', 'upload_unknown',
                                'publication_unknown', 'failed', 'cancelled', 'expired'}:
            return None, None, dict(delivery)
        grant = self.payloads.issue(cursor, token=self.token, job=job, artifact_id=artifact_id, now=now)
        artifact, delivery = self.payloads.assert_grant(cursor, grant, now)
        return grant, artifact, delivery

    def _current(self, cursor, grant, route=None, ready=False):
        self._local()
        now = self._now()
        artifact, delivery = self.payloads.assert_grant(cursor, grant, now)
        if route is not None:
            self.payloads._route(cursor, artifact, route)
        if ready and self.check_delivery_ready(cursor, artifact, now) is not True:
            raise JobDeferred('official_file_delivery_not_ready',
                              min(15, max(1, artifact['pending_deadline_at'] - now)))
        return artifact, delivery

    def _terminal(self, cursor, job, artifact_id, state, reason):
        now = self._now()
        self._job(cursor, job, artifact_id, now)
        cursor.execute('''UPDATE qq_official_file_deliveries SET state=?,reason_code=?,updated_at=?
            WHERE artifact_id=? AND active_plan_id IS NULL
            AND state NOT IN ('failed','cancelled','expired','upload_unknown','publication_unknown')''',
            (state, _reason(reason), now, artifact_id))
        return {'status': state, 'reason': _reason(reason)}

    async def _stop(self, job, artifact_id, state, reason):
        try:
            return await self._db(self._terminal, job, artifact_id, state, reason)
        except LeaseLost:
            return {'status': 'needs_attention', 'reason': 'file_authority_unavailable'}

    def _choose(self, cursor, grant):
        artifact, delivery = self._current(cursor, grant)
        if delivery['route'] is not None:
            return delivery['route']
        if not artifact['draft'] and self.payloads.route_policy(cursor, artifact, 'native') is True:
            if artifact['byte_size'] <= min(self.limits.small_bytes, 3_750_000):
                return 'native'
            if self.payloads.route_policy(cursor, artifact, 'link') is not True:
                return 'large_unavailable'
        if self.payloads.route_policy(cursor, artifact, 'link') is True:
            return 'link'
        if artifact['draft']:
            return 'draft_unavailable'
        raise JobDeferred('official_file_route_not_ready', 15)

    async def __call__(self, job):
        artifact_id = job.payload.get('artifact_id') if isinstance(job.payload, dict) else None
        try:
            _ref(artifact_id)
            if job.payload != {'artifact_id': artifact_id} or job.kind != file_job_kind(
                    AccountKey.parse(self.token.account_key)):
                raise LeaseLost('file_job_unavailable')
            grant, artifact, delivery = await self._db(self._open, job, artifact_id)
            if grant is None:
                return {'status': delivery['state']}
            if delivery['credential_ref']:
                return await self._db(self._handoff_existing, grant)
            fallback = await self._db(self._rejected_fallback, grant)
            if fallback == 'link':
                return await self._link(job, grant, artifact)
            if fallback == 'unavailable':
                return await self._stop(job, artifact_id, 'failed', 'native_file_unavailable')
            route = await self._db(self._choose, grant)
            if route == 'native':
                return await self._native(job, grant, artifact)
            if route == 'link':
                return await self._link(job, grant, artifact)
            return await self._stop(job, artifact_id, 'failed',
                'large_upload_unavailable' if route == 'large_unavailable' else 'native_draft_details_unavailable')
        except (JobDeferred, asyncio.CancelledError):
            raise
        except (LeaseLost, PermissionError):
            return await self._stop(job, artifact_id, 'cancelled', 'file_authority_unavailable')
        except Exception:
            raise FileDispatchUnavailable() from None

    def _handoff_existing(self, cursor, grant):
        artifact, delivery = self._current(cursor, grant)
        row = cursor.execute('SELECT * FROM qq_official_file_credentials WHERE credential_ref=? AND artifact_id=?',
                             (delivery['credential_ref'], artifact['artifact_id'])).fetchone()
        if row is None:
            raise PermissionError('file_credential_unavailable')
        ref = ProtectedContentRef(row['artifact_id'], row['credential_ref'], row['route'],
                                  row['content_commitment'], row['expires_at'])
        self.payloads.handoff(cursor, grant, ref, self.outbox, self._now())
        return {'status': 'handed_off'}

    async def _native(self, job, grant, artifact):
        await self._db(self._current, grant, 'native')
        content = await self.calls(self.artifacts.read_verified, artifact['artifact_id'],
                                   sha256=artifact['sha256'], byte_size=artifact['byte_size'])
        fp = fingerprint([content], filename=artifact['filename'], file_type=4)
        endpoint = EndpointKey.parse(artifact['endpoint_key'])
        intent = UploadIntent(artifact['artifact_id'], endpoint, fp,
                              min(artifact['pending_deadline_at'], artifact['retain_until']))
        async def current(_intent, _operation):
            if _intent != intent:
                raise UploadGuardError()
            await self._db(self._current, grant, 'native')
        async def begin(stage):
            attempt_id = await self._db(self._begin_stage, grant, job, stage)
            return issue_upload_permit(stage, stage_attempt_id=attempt_id, check_current=self._local)
        uploader = OfficialMediaUploader(self.rest, ensure_current=current, before_network=begin,
                                        clock=self._now, limits=self.limits)
        request = build_small_upload(endpoint, fp, content, limits=self.limits)
        stage = uploader._stage(intent, request.stage, [request.path, request.body])
        old = await self._db(self._existing_stage, grant)
        if old is not None:
            if old['request_commitment'] != stage.request_commitment or old['operation'] != 'upload_small':
                return await self._stop(job, artifact['artifact_id'], 'upload_unknown', 'upload_stage_conflict')
            binding = NativeUploadBinding(artifact['artifact_id'], old['stage_attempt_id'], endpoint, fp, artifact['mime'])
            if old['state'] not in {'dispatched', 'confirmed', 'unknown'}:
                return await self._stop(job, artifact['artifact_id'], 'failed', 'upload_previous_failure')
            try:
                reference, _ = await self.calls(self.payloads.native_vault.recover_stage, binding)
            except Exception:
                await self._db(self._settle_stage, old['stage_attempt_id'], stage, 'unknown', None, None)
                return await self._stop(job, artifact['artifact_id'], 'upload_unknown', 'upload_evidence_unavailable')
            await self._db(self._settle_stage, old['stage_attempt_id'], stage, 'confirmed', reference.binding_digest, None)
            return await self._db(self._handoff_native, grant, binding, reference)
        result = await uploader.upload_small(intent, content)
        if result.stage_attempt_id is None:
            # A before-network commit may have succeeded without delivering its
            # return value. Preserve any intent; never allocate another one.
            old = await self._db(self._existing_stage, grant)
            if old is not None:
                return await self._stop(job, artifact['artifact_id'], 'upload_unknown', 'upload_intent_unknown')
            return await self._stop(job, artifact['artifact_id'], 'failed', _reason(result.category))
        binding = NativeUploadBinding(artifact['artifact_id'], result.stage_attempt_id, endpoint, fp, artifact['mime'])
        if result.disposition == 'confirmed' and isinstance(result.value, UploadedMedia):
            reference = await self.calls(self.payloads.native_vault.save, binding, result.value)
            await self._db(self._settle_stage, result.stage_attempt_id, stage, 'confirmed', reference.binding_digest, result)
            return await self._db(self._handoff_native, grant, binding, reference)
        status = {'confirmed_rejected': 'rejected', 'proven_unsent': 'unsent'}.get(result.disposition, 'unknown')
        await self._db(self._settle_stage, result.stage_attempt_id, stage, status, None, result)
        if status == 'rejected' and await self._db(self._rejected_fallback, grant) == 'link':
            return await self._link(job, grant, artifact)
        return await self._stop(job, artifact['artifact_id'], 'upload_unknown' if status == 'unknown' else 'failed',
                                'upload_unknown' if status == 'unknown' else 'upload_rejected')

    def _rejected_fallback(self, cursor, grant):
        artifact, delivery = self._current(cursor, grant)
        if delivery['route'] != 'native':
            return None
        rows = cursor.execute('SELECT * FROM qq_official_file_stage_attempts WHERE artifact_id=?',
                              (artifact['artifact_id'],)).fetchall()
        if len(rows) != 1 or not _unsupported_stage(rows[0]):
            return None
        if delivery['active_plan_id'] is not None or delivery['credential_ref'] is not None:
            raise PermissionError('file_rejection_handoff_conflict')
        try:
            self.payloads._route(cursor, artifact, 'link')
        except PermissionError:
            return 'unavailable'
        cursor.execute('''UPDATE qq_official_file_deliveries SET route='link',state='stored',
            reason_code='native_file_unavailable',updated_at=? WHERE artifact_id=?
            AND active_plan_id IS NULL AND credential_ref IS NULL''', (self._now(), artifact['artifact_id']))
        return 'link'

    def _existing_stage(self, cursor, grant):
        artifact, _ = self._current(cursor, grant, 'native')
        rows = cursor.execute('SELECT * FROM qq_official_file_stage_attempts WHERE artifact_id=? ORDER BY created_at,stage_attempt_id',
                              (artifact['artifact_id'],)).fetchall()
        if len(rows) > 1:
            raise PermissionError('multiple_upload_attempts')
        return dict(rows[0]) if rows else None

    def _begin_stage(self, cursor, grant, job, stage):
        artifact, delivery = self._current(cursor, grant, 'native')
        if (stage.operation != 'upload_small' or stage.part_index is not None
                or stage.intent.artifact_id != artifact['artifact_id'] or delivery['active_plan_id'] is not None
                or self._existing_stage(cursor, grant) is not None):
            raise UploadGuardError()
        attempt_id, now = secrets.token_hex(16), self._now()
        cursor.execute('''INSERT INTO qq_official_file_stage_attempts
            (stage_attempt_id,artifact_id,operation,request_commitment,job_id,job_lease_generation,
             account_epoch,state,created_at,expires_at)
            VALUES (?,?,'upload_small',?,?,?,?,'dispatched',?,?)''',
            (attempt_id, artifact['artifact_id'], stage.request_commitment, job.job_id,
             job.lease_generation, self.token.epoch, now, stage.intent.deadline_at))
        cursor.execute("UPDATE qq_official_file_deliveries SET state='uploading',route='native',updated_at=? WHERE artifact_id=?",
                       (now, artifact['artifact_id']))
        return attempt_id

    def _settle_stage(self, cursor, attempt_id, stage, state, receipt_digest, result):
        # Historical completion is evidence only: no account authority, new
        # network operation, credential selection or handoff is granted here.
        row = cursor.execute('SELECT * FROM qq_official_file_stage_attempts WHERE stage_attempt_id=? FOR UPDATE',
                             (attempt_id,)).fetchone()
        if (row is None or row['artifact_id'] != stage.intent.artifact_id
                or row['request_commitment'] != stage.request_commitment or row['operation'] != stage.operation):
            raise PermissionError('upload_stage_evidence_conflict')
        if row['state'] == 'confirmed':
            if state == 'confirmed' and row['receipt_commitment'] != receipt_digest:
                raise PermissionError('upload_receipt_conflict')
            return
        if row['state'] not in {'dispatched', 'unknown'}:
            return
        failure = _reason(result.category) if result and result.category else None
        if (state == 'rejected' and result is not None and result.disposition == 'confirmed_rejected'
                and result.category == 'native_file_unavailable' and result.code == 50015014
                and result.http_status in {200, 400}
                and _REJECTION_CONTRACTS['native_file_unavailable_v1'] == (CONTRACT, ERROR_CONTRACT)):
            failure = 'native_file_unavailable_v1'
        cursor.execute('''UPDATE qq_official_file_stage_attempts SET state=?,settled_at=?,receipt_commitment=?,
            failure_code=?,http_status=?,platform_code=? WHERE stage_attempt_id=?''',
            (state, self._now(), receipt_digest, failure,
             result.http_status if result else None, result.code if result else None, attempt_id))

    def _handoff_native(self, cursor, grant, binding, reference):
        ref = self.payloads.register_native(cursor, grant, binding, reference, self._now())
        self.payloads.handoff(cursor, grant, ref, self.outbox, self._now())
        return {'status': 'handed_off'}

    def _link_intent(self, cursor, grant):
        artifact, delivery = self._current(cursor, grant, 'link', True)
        if delivery['publication_request_key'] is not None:
            return artifact, delivery, False
        now = self._now()
        expiry = min(now + 3600, artifact['retain_until'], artifact['pending_deadline_at'])
        key = _digest(['qq-file-publication-v1', self.token.account_key, artifact['artifact_id']])
        cursor.execute('''UPDATE qq_official_file_deliveries SET state='publishing',route='link',
            publication_request_key=?,publication_started_at=?,publication_expires_at=?,updated_at=?
            WHERE artifact_id=? AND publication_request_key IS NULL''', (key, now, expiry, now, artifact['artifact_id']))
        delivery = dict(cursor.execute('SELECT * FROM qq_official_file_deliveries WHERE artifact_id=?',
                                      (artifact['artifact_id'],)).fetchone())
        return artifact, delivery, True

    async def _link(self, job, grant, artifact):
        if self.publisher_factory is None:
            return await self._stop(job, artifact['artifact_id'], 'failed', 'link_backend_unavailable')
        artifact, delivery, first = await self._db(self._link_intent, grant)
        def authority(cursor, supplied, expiry):
            current, stored = self._current(cursor, grant, 'link', True)
            expected = ArtifactRef(current['artifact_id'], current['sha256'], current['byte_size'], current['mime'], current['filename'])
            if supplied != expected or expiry != stored['publication_expires_at']:
                raise PermissionError('file_publication_intent_conflict')
            return True
        publisher = self.publisher_factory(authority)
        try:
            if first:
                published = await publisher.publish(ArtifactRef(artifact['artifact_id'], artifact['sha256'],
                    artifact['byte_size'], artifact['mime'], artifact['filename']),
                    request_key=delivery['publication_request_key'], expires_at=delivery['publication_expires_at'])
            else:
                row = await publisher.store.by_request(delivery['publication_request_key'])
                if row is None or row.expires_at != delivery['publication_expires_at']:
                    raise PermissionError('file_publication_unknown')
                published = await publisher.restore(row.credential_ref)
        except asyncio.CancelledError:
            raise
        except Exception:
            # An unknown DB result may still have committed the first row.
            # Recover only that row and its original credential, never publish.
            try:
                row = await publisher.store.by_request(delivery['publication_request_key'])
                if row is None or row.expires_at != delivery['publication_expires_at']:
                    raise PermissionError('file_publication_unknown')
                published = await publisher.restore(row.credential_ref)
            except asyncio.CancelledError:
                raise
            except Exception:
                return await self._stop(job, artifact['artifact_id'], 'publication_unknown', 'publication_unknown')
        return await self._db(self._handoff_link, grant, published.publication_ref)

    def _handoff_link(self, cursor, grant, publication_ref):
        self._current(cursor, grant, 'link', True)
        ref = self.payloads.register_link(cursor, grant, publication_ref, self._now())
        self.payloads.handoff(cursor, grant, ref, self.outbox, self._now())
        return {'status': 'handed_off'}
