"""Trusted attachment authority and synchronous protected M3 resolution.

All SQL uses the caller's cursor. Only bounded private credential files are read
inside a transaction; no network, nested transaction, or model supplied URL is
accepted here. Historical receipts never depend on this current-send port.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import threading
import weakref

from src.plugins.ai_chat.agent.control import LeaseLost
from src.plugins.ai_chat.platform_types import EndpointKey, ActorKey
from src.qq_official.outgoing import escape_platform_tags
from .file_artifacts import _ref, _time
from .file_authority import assert_file_current
from .file_links import ArtifactRef, Publication, LinkError, _origin
from .native_media import ArtifactFingerprint
from .native_vault import NativeUploadBinding, NativeUploadRef, NativeVaultError
from .protected_payload import ProtectedContentRef, commitment, validate_content


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False)
class _FileGrant:
    port: object
    token: object
    job: object
    artifact_id: str

    def __repr__(self):
        return 'FileGrant(<protected>)'


def _reference(row):
    return ProtectedContentRef(row['artifact_id'], row['credential_ref'], row['route'],
                               row['content_commitment'], row['expires_at'])


def _artifact_ref(row):
    return ArtifactRef(row['artifact_id'], row['sha256'], row['byte_size'], row['mime'], row['filename'])


class TrustedFilePayloads:
    def __init__(self, *, admission, route_policy=None, link_vault=None,
                 native_vault=None, link_origin=None, allow_loopback=False):
        self.admission = admission
        self.route_policy = route_policy or (lambda cursor, artifact, route: False)
        self.link_vault, self.native_vault = link_vault, native_vault
        self.link_origin = _origin(link_origin, allow_loopback) if link_origin is not None else None
        self._issued, self._lock = weakref.WeakValueDictionary(), threading.Lock()

    def __repr__(self):
        return 'TrustedFilePayloads(<protected>)'

    def issue(self, cursor, *, token, job, artifact_id, now):
        _ref(artifact_id)
        assert_file_current(cursor, token=token, job=job, artifact_id=artifact_id,
                            admission=self.admission, now=now)
        grant = _FileGrant(self, token, job, artifact_id)
        with self._lock:
            self._issued[id(grant)] = grant
        return grant

    def assert_grant(self, cursor, grant, now):
        with self._lock:
            if (not isinstance(grant, _FileGrant) or grant.port is not self
                    or self._issued.get(id(grant)) is not grant):
                raise PermissionError('unissued_file_grant')
        return assert_file_current(cursor, token=grant.token, job=grant.job,
            artifact_id=grant.artifact_id, admission=self.admission, now=now)

    def _route(self, cursor, artifact, route):
        # Type-7 captions are not a verified draft-detail channel. Link messages
        # carry the complete trusted draft reason. A native-only configuration
        # must report this limitation instead of claiming draft delivery.
        if route == 'native' and artifact['draft']:
            raise PermissionError('native_draft_details_unavailable')
        if self.route_policy(cursor, artifact, route) is not True:
            raise PermissionError('file_route_unavailable')
        if route == 'native' and self.native_vault is None:
            raise PermissionError('native_vault_unavailable')
        if route == 'link' and (self.link_vault is None or self.link_origin is None):
            raise PermissionError('link_backend_unavailable')

    def _publication(self, cursor, artifact, publication_ref, now):
        row = cursor.execute('''SELECT * FROM qq_official_file_publications
            WHERE publication_ref=? AND artifact_id=? FOR SHARE''',
            (publication_ref, artifact['artifact_id'])).fetchone()
        if (row is None or row['state'] != 'active' or row['origin'] != self.link_origin
                or not row['created_at'] <= now < row['expires_at']
                or row['expires_at'] > min(artifact['retain_until'], artifact['pending_deadline_at'])):
            raise PermissionError('file_publication_unavailable')
        return Publication(row['publication_ref'], row['credential_ref'], row['request_key'],
            _artifact_ref(artifact), row['token_digest'], row['wire_commitment'], row['origin'],
            row['created_at'], row['expires_at'], row['state'], row['served_complete_count'])

    def _link_content(self, artifact, publication):
        try:
            token = self.link_vault.read(publication)
            url = publication.origin + '/files/' + token
            if not hmac.compare_digest(hashlib.sha256(url.encode()).hexdigest(), publication.wire_commitment):
                raise LinkError('publication_integrity_failure')
            expiry = datetime.fromtimestamp(publication.expires_at, timezone.utc).isoformat().replace('+00:00', 'Z')
        except (LinkError, OSError, OverflowError, ValueError):
            raise PermissionError('file_link_credential_unavailable') from None
        lines = ['文件：' + escape_platform_tags(artifact['filename'])]
        if artifact['draft']:
            lines.append('未完成部分：' + escape_platform_tags(artifact['draft_reason']))
        lines.extend([f"大小：{artifact['byte_size']} bytes", 'SHA-256：' + artifact['sha256'],
                      '下载有效期至：' + expiry, url])
        return {'msg_type': 0, 'content': '\n'.join(lines)}

    def _native_content(self, artifact, credential):
        fp = ArtifactFingerprint(artifact['filename'], 4, artifact['byte_size'], artifact['sha256'],
            credential['native_md5'], credential['native_sha1'], credential['native_md5_10m'])
        binding = NativeUploadBinding(artifact['artifact_id'], credential['native_stage_attempt_id'],
            EndpointKey.parse(artifact['endpoint_key']), fp, artifact['mime'])
        reference = NativeUploadRef(credential['credential_ref'], credential['native_binding_digest'],
                                    credential['native_expires_at'])
        try:
            receipt = self.native_vault.read(reference, binding)
        except NativeVaultError:
            raise PermissionError('native_file_credential_unavailable') from None
        return {'msg_type': 7, 'media': {'file_info': receipt.file_info}}

    def _catalog(self, cursor, artifact, ref, now):
        row = cursor.execute('''SELECT * FROM qq_official_file_credentials
            WHERE credential_ref=? AND artifact_id=? FOR SHARE''',
            (ref.credential_ref, artifact['artifact_id'])).fetchone()
        if (row is None or _reference(row) != ref or not row['created_at'] <= now < row['expires_at']
                or row['expires_at'] > min(artifact['retain_until'], artifact['pending_deadline_at'])):
            raise PermissionError('file_credential_unavailable')
        self._route(cursor, artifact, row['route'])
        if row['route'] == 'native':
            stage = cursor.execute('''SELECT * FROM qq_official_file_stage_attempts
                WHERE stage_attempt_id=? AND artifact_id=? FOR SHARE''',
                (row['native_stage_attempt_id'], artifact['artifact_id'])).fetchone()
            if (stage is None or stage['state'] != 'confirmed'
                    or stage['operation'] not in {'upload_small', 'upload_complete'}
                    or stage['receipt_commitment'] != row['native_binding_digest']):
                raise PermissionError('native_file_evidence_unavailable')
        return dict(row)

    def _store_credential(self, cursor, grant, artifact, delivery, fields, now):
        if delivery['active_plan_id'] is not None:
            raise PermissionError('file_already_handed_off')
        if delivery['credential_ref'] is not None and delivery['credential_ref'] != fields['credential_ref']:
            raise PermissionError('file_credential_already_selected')
        keys = tuple(fields)
        cursor.execute('INSERT INTO qq_official_file_credentials (' + ','.join(keys) + ') VALUES ('
            + ','.join('?' for _ in keys) + ') ON CONFLICT(credential_ref) DO NOTHING', tuple(fields.values()))
        row = cursor.execute('SELECT * FROM qq_official_file_credentials WHERE credential_ref=? FOR SHARE',
                             (fields['credential_ref'],)).fetchone()
        # The first timestamp/deadline is durable and never refreshed on replay.
        if row is None or any(row[k] != value for k, value in fields.items() if k not in {'created_at', 'expires_at'}):
            raise PermissionError('file_credential_conflict')
        ref = _reference(row)
        self._catalog(cursor, artifact, ref, now)
        cursor.execute('''UPDATE qq_official_file_deliveries SET credential_ref=?,route=?,state=?,updated_at=?
            WHERE artifact_id=? AND active_plan_id IS NULL''',
            (ref.credential_ref, ref.route, 'uploaded' if ref.route == 'native' else 'published', now, grant.artifact_id))
        if cursor.rowcount != 1:
            raise PermissionError('file_handoff_conflict')
        return ref

    def register_native(self, cursor, grant, binding, reference, now):
        artifact, delivery = self.assert_grant(cursor, grant, now)
        self._route(cursor, artifact, 'native')
        if (not isinstance(binding, NativeUploadBinding) or not isinstance(reference, NativeUploadRef)
                or binding.artifact_id != grant.artifact_id or binding.endpoint.key != artifact['endpoint_key']
                or binding.mime != artifact['mime'] or binding.artifact.file_type != 4
                or (binding.artifact.filename, binding.artifact.size, binding.artifact.sha256) !=
                   (artifact['filename'], artifact['byte_size'], artifact['sha256'])):
            raise PermissionError('native_file_binding_conflict')
        try:
            media = self.native_vault.read(reference, binding)
        except NativeVaultError:
            raise PermissionError('native_file_credential_unavailable') from None
        content = {'msg_type': 7, 'media': {'file_info': media.file_info}}
        expiry = min(reference.expires_at, artifact['retain_until'], artifact['pending_deadline_at'], now + 3600)
        if expiry <= now:
            raise PermissionError('native_file_credential_expired')
        fields = dict(credential_ref=reference.credential_ref, artifact_id=grant.artifact_id, route='native',
            content_commitment=commitment(content), created_at=now, expires_at=expiry,
            native_stage_attempt_id=binding.stage_attempt_id, native_binding_digest=reference.binding_digest,
            native_expires_at=reference.expires_at, native_md5=binding.artifact.md5,
            native_sha1=binding.artifact.sha1, native_md5_10m=binding.artifact.md5_10m)
        return self._store_credential(cursor, grant, artifact, delivery, fields, now)

    def register_link(self, cursor, grant, publication_ref, now):
        artifact, delivery = self.assert_grant(cursor, grant, now)
        self._route(cursor, artifact, 'link')
        publication = self._publication(cursor, artifact, publication_ref, now)
        if (delivery['publication_request_key'] != publication.request_key
                or delivery['publication_expires_at'] != publication.expires_at
                or delivery['publication_started_at'] is None or delivery['route'] != 'link'):
            raise PermissionError('file_publication_intent_conflict')
        content = self._link_content(artifact, publication)
        fields = dict(credential_ref=publication.credential_ref, artifact_id=grant.artifact_id, route='link',
            content_commitment=commitment(content), created_at=now, expires_at=publication.expires_at,
            publication_ref=publication.publication_ref)
        return self._store_credential(cursor, grant, artifact, delivery, fields, now)

    @staticmethod
    def _binding(artifact, ref, binding):
        if (not isinstance(ref, ProtectedContentRef) or ref.artifact_id != artifact['artifact_id']
                or binding.endpoint.key != artifact['endpoint_key']
                or binding.requester.key != artifact['requester_actor_key']
                or binding.source_scope_key != artifact['source_scope_key']
                or binding.source_canonical_message_id != artifact['source_canonical_message_id']
                or binding.source_native_message_id != artifact['source_native_message_id']
                or binding.endpoint_generation != artifact['endpoint_generation']
                or binding.route_epoch != artifact['route_epoch'] or binding.turn_id is not None
                or not artifact['created_at'] < binding.expires_at <= ref.expires_at):
            raise PermissionError('file_plan_source_conflict')

    def authorize_enqueue(self, cursor, *, token, source_grant, content_ref, binding, now):
        try:
            artifact, delivery = self.assert_grant(cursor, source_grant, now)
        except LeaseLost:
            raise PermissionError('file_source_unavailable') from None
        if source_grant.token is not token or binding.plan_id is not None or binding.delivery_id is not None:
            raise PermissionError('file_enqueue_grant_conflict')
        if delivery['active_plan_id'] is not None:
            raise PermissionError('file_already_handed_off')
        self._binding(artifact, content_ref, binding)
        self._catalog(cursor, artifact, content_ref, now)
        if delivery['credential_ref'] != content_ref.credential_ref:
            raise PermissionError('file_selected_credential_conflict')

    def authorize(self, cursor, *, token, content_ref, binding, now):
        _time(now)
        try:
            artifact, delivery = assert_file_current(cursor, token=token, artifact_id=content_ref.artifact_id,
                admission=self.admission, now=now, plan_id=binding.plan_id)
        except LeaseLost:
            raise PermissionError('file_source_unavailable') from None
        self._binding(artifact, content_ref, binding)
        association = cursor.execute('''SELECT * FROM qq_official_file_message_plans
            WHERE plan_id=? AND artifact_id=? AND credential_ref=? AND delivery_id=? FOR SHARE''',
            (binding.plan_id, content_ref.artifact_id, content_ref.credential_ref, binding.delivery_id)).fetchone()
        if (association is None or delivery['credential_ref'] != content_ref.credential_ref
                or association['account_key'] != token.account_key
                or association['endpoint_key'] != binding.endpoint.key
                or association['requester_actor_key'] != binding.requester.key):
            raise PermissionError('file_plan_association_conflict')
        self._catalog(cursor, artifact, content_ref, now)

    def resolve(self, cursor, *, token, content_ref, binding, now):
        self.authorize(cursor, token=token, content_ref=content_ref, binding=binding, now=now)
        artifact = dict(cursor.execute('SELECT * FROM qq_official_file_artifacts WHERE artifact_id=?',
                                      (content_ref.artifact_id,)).fetchone())
        credential = self._catalog(cursor, artifact, content_ref, now)
        if content_ref.route == 'native':
            content = self._native_content(artifact, credential)
        else:
            publication = self._publication(cursor, artifact, credential['publication_ref'], now)
            content = self._link_content(artifact, publication)
        return validate_content(content_ref, content)

    def handoff(self, cursor, grant, content_ref, outbox, now):
        artifact, delivery = self.assert_grant(cursor, grant, now)
        self._catalog(cursor, artifact, content_ref, now)
        if content_ref.artifact_id != grant.artifact_id or delivery['credential_ref'] != content_ref.credential_ref:
            raise PermissionError('file_selected_credential_conflict')
        if delivery['active_plan_id'] is not None:
            # An unknown message may never cause a second plan/credential.
            from .outbox_types import PlanRef
            row = cursor.execute('''SELECT delivery_id,credential_ref FROM qq_official_file_message_plans
                WHERE plan_id=? AND artifact_id=?''', (delivery['active_plan_id'], grant.artifact_id)).fetchone()
            if row is None or row['credential_ref'] != content_ref.credential_ref:
                raise PermissionError('file_plan_association_conflict')
            return PlanRef(delivery['active_plan_id'], (row['delivery_id'],), False)
        expiry = min(content_ref.expires_at, artifact['retain_until'], artifact['pending_deadline_at'])
        if expiry <= now:
            raise PermissionError('file_credential_expired')
        # Conservatively start the one-hour cap at handoff, before any possible
        # physical send. Retries never move this earlier recorded boundary.
        first = artifact['first_possible_send_at'] if artifact['first_possible_send_at'] is not None else now
        retain = min(artifact['retain_until'], first + 3600)
        if expiry > retain:
            raise PermissionError('file_credential_retention_conflict')
        cursor.execute('''UPDATE qq_official_file_artifacts
            SET first_possible_send_at=COALESCE(first_possible_send_at,?),retain_until=? WHERE artifact_id=?''',
            (now, retain, grant.artifact_id))
        ref = outbox.enqueue_artifact(grant.token, content_ref=content_ref, source_grant=grant,
            endpoint=EndpointKey.parse(artifact['endpoint_key']), requester=ActorKey.parse(artifact['requester_actor_key']),
            logical_key='artifact:' + grant.artifact_id, source_scope_key=artifact['source_scope_key'], expires_at=expiry,
            source_native_message_id=artifact['source_native_message_id'],
            source_canonical_message_id=artifact['source_canonical_message_id'], transaction_cursor=cursor)
        cursor.execute('''INSERT INTO qq_official_file_message_plans
            (plan_id,artifact_id,account_key,endpoint_key,requester_actor_key,credential_ref,delivery_id,created_at)
            VALUES (?,?,?,?,?,?,?,?)''', (ref.plan_id, grant.artifact_id, artifact['account_key'], artifact['endpoint_key'],
            artifact['requester_actor_key'], content_ref.credential_ref, ref.delivery_ids[0], now))
        cursor.execute('''UPDATE qq_official_file_deliveries SET state='handed_off',active_plan_id=?,updated_at=?
            WHERE artifact_id=? AND active_plan_id IS NULL''', (ref.plan_id, now, grant.artifact_id))
        if cursor.rowcount != 1:
            raise PermissionError('file_handoff_conflict')
        return ref
