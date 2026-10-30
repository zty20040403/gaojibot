"""Native ingress provenance for logical migration fences.

Only the authenticated TxA path can issue an admission. It is transaction-bound
and consumed after the original gateway receipt is inserted. No worker may
backfill an old inbox with today's logical epoch. This is provenance, not a
queue, grant, or replacement for the ordinary account/actor/job checks.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import weakref

from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from src.plugins.ai_chat.workers.durable_jobs import JobDeferred
from .lease import assert_epoch
from .migration_types import MigrationDenied


@dataclass(frozen=True, eq=False, repr=False)
class NativeAdmission:
    endpoint: EndpointKey
    actor: ActorKey
    native_id: str
    generation: int
    endpoint_epoch: int
    route_id: str | None
    logical_epoch: int
    received_at: int


class SourceAdmission:
    """Explicit admission adapter, installed only by MigrationServices.

    Ordinary actor admission remains unchanged; source-aware consumers call
    require_inbox/require_execution with their actual persisted source.
    """
    def __init__(self, admission, sources):
        self.base, self.sources = admission, sources

    def __call__(self, cursor, endpoint, actor):
        return self.base(cursor, endpoint, actor)

    def require_inbox(self, cursor, inbox_id, *, endpoint, actor, operation):
        return self.sources.require_inbox(cursor, inbox_id, endpoint=endpoint,
            actor=actor, operation=operation)

    def lock_inbox(self, cursor, inbox_id, *, operation):
        """Acquire route fences before the caller's inbox/job/domain locks."""
        row=cursor.execute('SELECT endpoint_key,actor_key FROM qq_migration_native_sources WHERE inbox_id=?', (inbox_id,)).fetchone()
        if row is None:raise MigrationDenied('native_source_unavailable')
        return self.require_inbox(cursor,inbox_id,endpoint=EndpointKey.parse(row['endpoint_key']),
            actor=ActorKey.parse(row['actor_key']),operation=operation)

    def require_execution(self, cursor, execution_id, *, endpoint, actor, operation):
        execution = cursor.execute("SELECT * FROM qq_official_turn_executions WHERE execution_id=?",
            (execution_id,)).fetchone()
        if (execution is None or execution['endpoint_key'] != endpoint.key
                or execution['requester_actor_key'] != actor.key):
            raise MigrationDenied('native_execution_source_unavailable')
        return self.require_inbox(cursor, execution['inbox_id'], endpoint=endpoint,
            actor=actor, operation=operation)

    def require_file(self,cursor,*,artifact,job=None,plan_id=None,now):
        verifier=self.sources.verifier
        if verifier is not None and verifier.file_proof(cursor,artifact=artifact,plan_id=plan_id,now=now) is not None:
            return
        if job is not None and plan_id is None:
            self._hold_unattempted_file(cursor, artifact, now)
        self.require_execution(cursor,artifact['source_execution_id'],
            endpoint=EndpointKey.parse(artifact['endpoint_key']),actor=ActorKey.parse(artifact['requester_actor_key']),
            operation='execute' if job is not None else 'send')

    def _hold_unattempted_file(self, cursor, artifact, now):
        """Delay only; the caller already checked account, endpoint and file job.

        Planned routes still admit normal generation. Keep its untouched file
        job available for the real verification request before ordinary logical
        source checks reject the frozen route. No new sending authority, deadline
        or retry is created here. Already attempted/unknown files retain their
        original recovery rules. Do not acquire additional source route locks
        after the caller's endpoint/job locks for this read-only denial.
        """
        if (artifact.get('source_migration_item_id') is not None
                or artifact.get('source_execution_id') is None
                or artifact['bytes_state'] != 'available'
                or now >= min(artifact['pending_deadline_at'], artifact['retain_until'])):
            return
        row = cursor.execute("""SELECT j.deadline FROM migration_routes r
            JOIN migration_journals j USING(route_id)
            JOIN qq_official_file_deliveries d ON d.artifact_id=?
            WHERE r.target_account_key=? AND r.target_endpoint_key=?
              AND r.target_generation=? AND r.target_endpoint_epoch=?
              AND r.phase IN ('planned','frozen','draining','switching','verifying')
              AND j.deadline>? AND d.state='stored'
              AND d.active_plan_id IS NULL AND d.credential_ref IS NULL
              AND d.publication_request_key IS NULL
              AND NOT EXISTS (SELECT 1 FROM qq_official_file_stage_attempts s
                              WHERE s.artifact_id=d.artifact_id)
            LIMIT 1""",
            (artifact['artifact_id'], artifact['account_key'], artifact['endpoint_key'],
             artifact['endpoint_generation'], artifact['route_epoch'], now)).fetchone()
        if row is not None:
            deadline = min(row['deadline'], artifact['pending_deadline_at'], artifact['retain_until'])
            raise JobDeferred('migration_file_verification_pending', min(15, deadline-now))

    def file_verification(self,cursor,*,artifact,now):
        verifier=self.sources.verifier
        if verifier is None:return None
        proof=verifier.file_proof(cursor,artifact=artifact,now=now)
        if proof is None:return None
        return proof,verifier._source(cursor,proof)['expires_at']


class NativeSources:
    def __init__(self, routes, *, verifier=None):
        self.routes = routes
        self.verifier=verifier
        self._issued = weakref.WeakKeyDictionary()

    def admit(self, cursor, *, token, decoded, received_at):
        """Called for a new authenticated human event BEFORE any reducer."""
        assert_epoch(cursor, token)
        endpoint, actor = decoded.endpoint, decoded.actor
        if endpoint.account.key != token.account_key or not actor.matches_endpoint(endpoint):
            raise MigrationDenied('native_source_identity_mismatch')
        physical = cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?', (endpoint.key,)).fetchone()
        if (endpoint.kind == 'group' and physical is not None and physical['binding_state'] != 'active'
                and self.routes._route(cursor, endpoint.scope.key) is None):
            # M2's body-free /bind reducer may accept an unbound group. This
            # exception issues no source and therefore no model/send authority.
            return None
        self.routes.lock(cursor, endpoints=(endpoint,), scopes=(endpoint.scope.key,))
        route = self.routes._route(cursor, endpoint.scope.key)
        epoch = route['epoch'] if route else 0
        view = self.routes.assert_active_route(cursor, canonical_scope=endpoint.scope,
            endpoint=endpoint, route_epoch=epoch, operation='ingest')
        proof = NativeAdmission(endpoint, actor, decoded.native_message_id,
            view.endpoint_generation, view.endpoint_route_epoch, view.route_id,
            view.route_epoch, received_at)
        txid = cursor.execute('SELECT txid_current() AS txid').fetchone()['txid']
        self._issued[proof] = (cursor, txid)
        return proof

    def record(self, cursor, *, token, proof, inbox_id, session_id, seq):
        issued = self._issued.get(proof)
        txid = cursor.execute('SELECT txid_current() AS txid').fetchone()['txid']
        if issued is None or issued != (cursor, txid):
            raise MigrationDenied('native_admission_not_issued')
        assert_epoch(cursor, token)
        row = cursor.execute('SELECT * FROM qq_official_inbound_events WHERE inbox_id=?', (inbox_id,)).fetchone()
        receipt = cursor.execute('''SELECT * FROM qq_official_gateway_receipts
            WHERE account_key=? AND session_id=? AND seq=? AND inbox_id=?''',
            (token.account_key, session_id, seq, inbox_id)).fetchone()
        if (row is None or receipt is None or row['account_key'] != token.account_key
                or proof.endpoint.account.key != token.account_key
                or row['endpoint_key'] != proof.endpoint.key
                or row['native_message_id'] != proof.native_id
                or row['endpoint_generation'] != proof.generation
                or row['route_epoch'] != proof.endpoint_epoch
                or row['received_at'] != proof.received_at
                or receipt['received_at'] != proof.received_at):
            raise MigrationDenied('native_source_receipt_mismatch')
        if row['status'] == 'pending' and row['echo_state'] == 'not_echo':
            kind = 'message'
        elif row['reason_code'] == 'binding_control' and row['control_actor_key'] == proof.actor.key:
            kind = 'binding_control'
        elif row['reason_code']=='admin_identity_control' and row['identity_control_actor_key']==proof.actor.key:
            kind='admin_control'
        elif row['reason_code'] == 'admin_control' and row['admin_control_actor_key'] == proof.actor.key:
            kind = 'admin_control'
        else:
            # Skipped/unknown/unbound ordinary traffic creates no authority.
            self._issued.pop(proof, None)
            return
        values = (inbox_id, token.account_key, proof.endpoint.key, proof.actor.key,
            session_id, seq, proof.native_id, proof.endpoint.scope.key, proof.generation,
            proof.endpoint_epoch, proof.route_id, proof.logical_epoch, kind, proof.received_at)
        cursor.execute('''INSERT INTO qq_migration_native_sources
            (inbox_id,account_key,endpoint_key,actor_key,source_session_id,source_seq,
             native_message_id,scope_key,endpoint_generation,endpoint_route_epoch,
             logical_route_id,logical_route_epoch,source_kind,received_at)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', values)
        self._issued.pop(proof, None)

    def require_inbox(self, cursor, inbox_id, *, endpoint, actor, operation):
        source = cursor.execute('SELECT * FROM qq_migration_native_sources WHERE inbox_id=?', (inbox_id,)).fetchone()
        inbox = cursor.execute('SELECT * FROM qq_official_inbound_events WHERE inbox_id=?', (inbox_id,)).fetchone()
        if (source is None or inbox is None or source['account_key'] != endpoint.account.key
                or source['endpoint_key'] != endpoint.key or source['actor_key'] != actor.key
                or not actor.matches_endpoint(endpoint) or actor.identity_kind == 'bot'
                or source['native_message_id'] != inbox['native_message_id']
                or source['endpoint_generation'] != inbox['endpoint_generation']
                or source['endpoint_route_epoch'] != inbox['route_epoch']):
            raise MigrationDenied('native_source_unavailable')
        view = self.routes.assert_active_route(cursor, canonical_scope=source['scope_key'],
            endpoint=endpoint, route_epoch=source['logical_route_epoch'], operation=operation)
        # An unmanaged source may become planned at the same epoch zero. That
        # does not grant another endpoint or advance any execution epoch.
        if (source['logical_route_id'] is not None and view.route_id != source['logical_route_id']
                or view.endpoint_generation != source['endpoint_generation']
                or view.endpoint_route_epoch != source['endpoint_route_epoch']):
            raise MigrationDenied('native_source_route_changed')
        return dict(source)

    def plan(self, cursor, *, token, plan, operation, now, **_):
        """Exact original inbox + actor; controls retain their own M3 checks."""
        assert_epoch(cursor, token)
        endpoint = EndpointKey.parse(plan['endpoint_key'])
        actor = ActorKey.parse(plan['requester_actor_key'])
        if plan['account_key'] != token.account_key or endpoint.account.key != token.account_key:
            raise MigrationDenied('native_plan_account_mismatch')
        part = cursor.execute('SELECT * FROM deliveries WHERE plan_id=? AND part_index=0', (plan['plan_id'],)).fetchone()
        if part is None:
            raise MigrationDenied('native_plan_part_unavailable')
        if operation == 'enqueue':
            source = cursor.execute('''SELECT s.*,i.canonical_message_id FROM qq_migration_native_sources s
                JOIN qq_official_inbound_events i USING(inbox_id)
                WHERE s.account_key=? AND s.endpoint_key=? AND s.actor_key=? AND s.native_message_id=?''',
                (token.account_key,endpoint.key,actor.key,part['reply_to_native_message_id'])).fetchall()
            if len(source) != 1:
                raise MigrationDenied('native_plan_source_unavailable')
            source = source[0]
            if plan['source_canonical_message_id'] is None:
                manifest = json.loads(plan['manifest_json'])
                control = manifest.get('control') or {}
                if plan['purpose'] not in {'binding_otp','approval_otp'} and control.get('kind') not in {'authenticated_control','binding_preview'}:
                    raise MigrationDenied('native_plan_control_unavailable')
                if control.get('kind') == 'authenticated_control':
                    control_row = cursor.execute('SELECT inbox_id FROM qq_official_control_decisions WHERE control_id=?',
                        (control.get('control_id'),)).fetchone()
                    if control_row is None or control_row['inbox_id'] != source['inbox_id']:
                        raise MigrationDenied('native_plan_control_mismatch')
            elif (source['source_kind'] != 'message'
                    or source['canonical_message_id'] != plan['source_canonical_message_id']):
                raise MigrationDenied('native_plan_message_mismatch')
            self.require_inbox(cursor, source['inbox_id'], endpoint=endpoint,actor=actor,operation='send')
            cursor.execute('''INSERT INTO qq_migration_native_plan_sources
                (plan_id,inbox_id,account_key,endpoint_key,actor_key) VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING''',
                (plan['plan_id'],source['inbox_id'],token.account_key,endpoint.key,actor.key))
        binding = cursor.execute('SELECT * FROM qq_migration_native_plan_sources WHERE plan_id=?', (plan['plan_id'],)).fetchone()
        if (binding is None or binding['account_key'] != token.account_key
                or binding['endpoint_key'] != endpoint.key or binding['actor_key'] != actor.key):
            raise MigrationDenied('native_plan_binding_unavailable')
        source=self.require_inbox(cursor,binding['inbox_id'],endpoint=endpoint,actor=actor,operation='send')
        inbox=cursor.execute('SELECT canonical_message_id FROM qq_official_inbound_events WHERE inbox_id=?', (binding['inbox_id'],)).fetchone()
        if (source['native_message_id'] != part['reply_to_native_message_id']
                or source['scope_key'] != plan['source_scope_key']
                or (plan['source_canonical_message_id'] is not None
                    and inbox['canonical_message_id'] != plan['source_canonical_message_id'])):
            raise MigrationDenied('native_plan_source_mismatch')
        return True
