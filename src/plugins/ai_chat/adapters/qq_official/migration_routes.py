"""Route/dispatch transaction boundary. No network or commits inside this module."""
from __future__ import annotations

import json
import secrets

from .migration_types import (
    BaselineEvidence, DenyPorts, DispatchProof, ExecutionReceiptEvidence, MigrationDenied, RouteSnapshot,
    _issue, digest, encoded, require_issued,
)
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.platform_types import EndpointKey


def positive(value, *, zero=False):
    if type(value) is not int or not (0 if zero else 1) <= value <= 2**63-1:
        raise MigrationDenied("invalid_migration_number")
    return value


def live_session(cursor, pid, key):
    positive(pid)
    if type(key) is not int or not -(2**63) <= key < 2**63:
        raise MigrationDenied("invalid_baseline")
    bits = key & ((1 << 64)-1)
    return cursor.execute("""SELECT 1 FROM pg_catalog.pg_locks WHERE locktype='advisory'
        AND granted AND pid=? AND classid::bigint=? AND objid::bigint=? AND objsubid=1
        AND database=(SELECT oid FROM pg_database WHERE datname=current_database())""",
        (pid, bits >> 32, bits & ((1 << 32)-1))).fetchone() is not None


def physical_scope(endpoint):
    if endpoint.account.platform == "onebot-v11":
        return ConversationScope("onebot-v11", endpoint.kind, endpoint.native_conversation_id)
    return endpoint.scope


def same_physical_scope(scope, endpoint):
    value = ConversationScope.from_key(scope)
    return (value.platform == endpoint.account.platform and value.kind == endpoint.kind
            and value.native_conversation_id == endpoint.native_conversation_id
            and (value.account == endpoint.account or (value.account is None and value.platform == "onebot-v11")))


class RouteService:
    def __init__(self, *, tokens=None, ports=None):
        self.tokens = dict(tokens or {})
        self.ports = ports or DenyPorts()

    def register_baseline(self, cursor, *, account, source, now):
        evidence = self.ports.baseline(cursor, source, account=account, now=now)
        if (not isinstance(evidence, BaselineEvidence) or evidence.account_key != account.key
                or evidence.expires_at <= now or evidence.expires_at > now+3600
                or len(evidence.deployment_digest) != 64
                or not live_session(cursor, evidence.backend_pid, evidence.advisory_key)):
            raise MigrationDenied("baseline_unavailable")
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext(?))", ("migration.account:"+account.key,))
        existing = cursor.execute("SELECT * FROM migration_runtime_baselines WHERE account_key=? FOR UPDATE",
                                  (account.key,)).fetchone()
        if (existing is not None and existing["runtime_id"] != evidence.runtime_id
                and live_session(cursor, existing["backend_pid"], existing["advisory_key"])):
            raise MigrationDenied("baseline_already_owned")
        revision = (existing["revision"] + 1) if existing else 1
        if existing is not None and existing["runtime_id"] == evidence.runtime_id:
            if (existing["backend_pid"], existing["advisory_key"], existing["deployment_digest"]) != (
                    evidence.backend_pid, evidence.advisory_key, evidence.deployment_digest):
                raise MigrationDenied("baseline_changed")
            revision = existing["revision"]
        cursor.execute("""INSERT INTO migration_runtime_baselines VALUES (?,?,?,?,?,?,?,?,'ready')
            ON CONFLICT(account_key) DO UPDATE SET runtime_id=excluded.runtime_id,revision=excluded.revision,
            backend_pid=excluded.backend_pid,advisory_key=excluded.advisory_key,
            deployment_digest=excluded.deployment_digest,expires_at=excluded.expires_at,state='ready'""",
            (account.key, evidence.runtime_id, revision, evidence.backend_pid, evidence.advisory_key,
             evidence.deployment_digest, evidence.expires_at, now))
        return revision

    def _baseline(self, cursor, account_key, now):
        row = cursor.execute("SELECT * FROM migration_runtime_baselines WHERE account_key=? FOR UPDATE",
                             (account_key,)).fetchone()
        if (row is None or row["state"] != "ready" or row["expires_at"] <= now
                or not live_session(cursor, row["backend_pid"], row["advisory_key"])):
            raise MigrationDenied("baseline_unavailable")
        return row

    def _route(self, cursor, scope):
        return cursor.execute("""SELECT r.* FROM migration_routes r JOIN migration_scope_aliases a
            USING(route_id) WHERE a.scope_key=?""", (scope,)).fetchone()

    def lock(self, cursor, *, endpoints, scopes):
        """Account -> endpoint -> sorted scope mutex -> route -> caller domain.

        Caller has already taken BLOB_LOCK when needed. Initial nonlocking
        discovery only adds immutable route endpoints; identity is reread later.
        """
        endpoints = {item.key: item for item in endpoints}
        scopes = set(scopes)
        for scope in tuple(scopes):
            row = self._route(cursor, scope)
            if row is not None:
                for key in ("source_endpoint_key", "target_endpoint_key"):
                    endpoint = EndpointKey.parse(row[key])
                    endpoints[endpoint.key] = endpoint
                scopes.update((row["source_scope_key"], row["target_scope_key"]))
        accounts = {item.account.key: item.account for item in endpoints.values()}
        for key, account in sorted(accounts.items()):
            if account.platform == "qq-official":
                token = self.tokens.get(key)
                if token is None:
                    raise MigrationDenied("official_lease_unavailable")
                assert_epoch(cursor, token)
            # Baseline rows are locked only by the dedicated admission/freeze
            # check AFTER route locks. Ordinary project-authority callers may
            # already hold this official account+endpoint; taking a legacy
            # baseline before endpoint locks here would invert their ordering.
        endpoint_rows = {}
        for key, endpoint in sorted(endpoints.items()):
            if endpoint.account.platform == "qq-official":
                row = cursor.execute("SELECT * FROM qq_official_endpoints WHERE endpoint_key=? AND account_key=? FOR UPDATE",
                                     (key, endpoint.account.key)).fetchone()
                if (row is None or row["membership_state"] != "present"
                        or row["binding_state"] == "revoked"
                        or (endpoint.kind == "group" and row["binding_state"] != "active")):
                    raise MigrationDenied("endpoint_unavailable")
                endpoint_rows[key] = row
        for scope in sorted(scopes):
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(?))", ("migration.scope:"+scope,))
        for scope in sorted(scopes):
            row = self._route(cursor, scope)
            if row is not None:
                cursor.execute("SELECT route_id FROM migration_routes WHERE route_id=? FOR UPDATE", (row["route_id"],))
        return endpoint_rows

    def assert_active_route(self, cursor, *, canonical_scope, endpoint, route_epoch, operation):
        if operation not in {"read", "ingest", "execute", "send"}:
            raise MigrationDenied("unsupported_route_operation")
        positive(route_epoch, zero=True)
        scope = canonical_scope.key if isinstance(canonical_scope, ConversationScope) else canonical_scope
        row = self._route(cursor, scope) or self._route(cursor, physical_scope(endpoint).key)
        if row is None and endpoint.account.platform != "qq-official":
            if not same_physical_scope(scope, endpoint) or route_epoch != 0:
                raise MigrationDenied("unmanaged_scope_mismatch")
            return RouteSnapshot(None, 0, "unmanaged", endpoint, scope, None, None)
        scope_keys = (scope,) if row is None else (scope, row["source_scope_key"], row["target_scope_key"])
        endpoints = self.lock(cursor, endpoints=(endpoint,), scopes=scope_keys)
        row = self._route(cursor, scope) or self._route(cursor, physical_scope(endpoint).key)
        physical = endpoints.get(endpoint.key)
        generation = physical["generation"] if physical else None
        endpoint_epoch = physical["route_epoch"] if physical else None
        if row is None:
            if scope != endpoint.scope.key or route_epoch != 0:
                raise MigrationDenied("unmanaged_scope_mismatch")
            return RouteSnapshot(None, 0, "unmanaged", endpoint, scope, generation, endpoint_epoch)
        if row["epoch"] != route_epoch:
            raise MigrationDenied("route_epoch_stale")
        if scope not in {row["source_scope_key"], row["target_scope_key"]}:
            raise MigrationDenied("unapproved_route_alias")
        if operation == "ingest" and not same_physical_scope(scope, endpoint):
            raise MigrationDenied("physical_ingest_scope_required")
        if row["phase"] == "planned":
            allowed = same_physical_scope(scope, endpoint)
        else:
            allowed = (row["phase"] in {"completed", "rolled_back"}
                       and row["active_endpoint_key"] == endpoint.key)
            if row["phase"] == "rolled_back" and scope != row["source_scope_key"]:
                # Rollback is not a reverse public-history/resource grant.
                allowed = False
        if not allowed:
            raise MigrationDenied("route_paused")
        if endpoint.key == row["target_endpoint_key"] and (
                generation != row["target_generation"] or endpoint_epoch != row["target_endpoint_epoch"]):
            raise MigrationDenied("endpoint_epoch_stale")
        if endpoint.key == row["source_endpoint_key"] and physical is not None and (
                generation != row["source_generation"] or endpoint_epoch != row["source_endpoint_epoch"]):
            raise MigrationDenied("source_endpoint_epoch_stale")
        return RouteSnapshot(row["route_id"], row["epoch"], row["phase"],
                             EndpointKey.parse(row["active_endpoint_key"]), scope, generation, endpoint_epoch)

    def begin_dispatch(self, cursor, *, source_endpoint, source_scope, physical_endpoint,
                       route_epoch, target_route_epoch, operation_ref, operation,
                       source, now, attempt_id=None):
        if operation not in {"send", "execute"} or not isinstance(operation_ref, str) or not 1 <= len(operation_ref) <= 128:
            raise MigrationDenied("invalid_dispatch_intent")
        self.lock(cursor, endpoints=(source_endpoint, physical_endpoint),
                  scopes=(source_scope.key, physical_scope(physical_endpoint).key))
        baseline = self._baseline(cursor, source_endpoint.account.key, now)
        snapshot = self.assert_active_route(cursor, canonical_scope=source_scope, endpoint=source_endpoint,
                                            route_epoch=route_epoch, operation=operation)
        self.assert_active_route(cursor, canonical_scope=physical_scope(physical_endpoint), endpoint=physical_endpoint,
                                 route_epoch=target_route_epoch, operation=operation)
        if self.ports.effect(cursor, source, source_endpoint=source_endpoint, source_scope=source_scope,
                             endpoint=physical_endpoint, operation=operation, now=now) is not None:
            raise MigrationDenied("effect_authority_contract")
        if cursor.execute("SELECT 1 FROM migration_dispatch_activities WHERE source_account_key=? AND operation_ref=?",
                          (source_endpoint.account.key, operation_ref)).fetchone():
            raise MigrationDenied("dispatch_already_admitted")
        if attempt_id is not None:
            attempt = cursor.execute("""SELECT a.attempt_id,d.source_scope_key,d.target_platform,d.target_kind,
                d.target_native_conversation_id,d.endpoint_key FROM delivery_attempts a
                JOIN deliveries d USING(delivery_id) WHERE a.attempt_id=? FOR UPDATE OF a,d""", (attempt_id,)).fetchone()
            if (attempt is None or attempt["source_scope_key"] != source_scope.key
                    or attempt["target_platform"] != physical_endpoint.account.platform
                    or attempt["target_kind"] != physical_endpoint.kind
                    or attempt["target_native_conversation_id"] != physical_endpoint.native_conversation_id
                    or (attempt["endpoint_key"] is not None and attempt["endpoint_key"] != physical_endpoint.key)):
                raise MigrationDenied("dispatch_attempt_mismatch")
        identifier = secrets.token_hex(16)
        cursor.execute("""INSERT INTO migration_dispatch_activities(activity_id,source_account_key,
            source_scope_key,source_endpoint_key,target_endpoint_key,route_id,route_epoch,runtime_id,
            operation_ref,operation,attempt_id,state,dispatched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,'dispatched',?)""",
            (identifier, source_endpoint.account.key, source_scope.key, source_endpoint.key,
             physical_endpoint.key, snapshot.route_id, route_epoch, baseline["runtime_id"],
             operation_ref, operation, attempt_id, now))
        return _issue(DispatchProof, activity_id=identifier, source_endpoint_key=source_endpoint.key,
                      target_endpoint_key=physical_endpoint.key, runtime_id=baseline["runtime_id"], route_epoch=route_epoch)

    def observe_dispatch(self, cursor, *, proof, status, native_id=None, now):
        """Historical evidence only. No current route/baseline/session required."""
        require_issued(proof, DispatchProof)
        if status not in {"accepted", "confirmed_rejected", "unknown"}:
            raise MigrationDenied("invalid_dispatch_observation")
        if native_id is not None and (not isinstance(native_id, str) or not 1 <= len(native_id.encode()) <= 256):
            raise MigrationDenied("invalid_dispatch_observation")
        if status == "accepted" and not native_id:
            raise MigrationDenied("missing_dispatch_receipt")
        row = cursor.execute("SELECT * FROM migration_dispatch_activities WHERE activity_id=? FOR UPDATE",
                             (proof.activity_id,)).fetchone()
        if row is None or (row["source_endpoint_key"], row["target_endpoint_key"], row["runtime_id"], row["route_epoch"]) != (
                proof.source_endpoint_key, proof.target_endpoint_key, proof.runtime_id, proof.route_epoch):
            raise MigrationDenied("dispatch_proof_mismatch")
        self._record_dispatch_observation(cursor,row,status=status,native_id=native_id,now=now)

    @staticmethod
    def _record_dispatch_observation(cursor,row,*,status,native_id,now):
        # Callers have verified a real transport proof or an exact historical
        # self-echo against its persisted physical attempt. This records facts.
        activity_id = row['activity_id']
        payload = {"status": status, "native_id": native_id}
        fingerprint = digest(payload)
        cursor.execute("INSERT INTO migration_activity_observations VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                       (activity_id, fingerprint, encoded(payload), now))
        old = json.loads(row["receipt_json"]) if row["receipt_json"] else None
        reliable = cursor.execute("""SELECT COUNT(*) FROM migration_activity_observations
            WHERE activity_id=? AND receipt_json::jsonb->>'status'<>'unknown'""", (activity_id,)).fetchone()[0]
        # Later reliable evidence can resolve unknown; conflicting reliable
        # evidence remains unknown and every receipt is retained append-only.
        if reliable > 1:
            result = "unknown"
        elif old and old["status"] != "unknown" and status == "unknown":
            return
        else:
            result = status
        cursor.execute("UPDATE migration_dispatch_activities SET state=?,receipt_digest=?,receipt_json=?,observed_at=? WHERE activity_id=?",
                       (result, fingerprint, encoded(payload), now, activity_id))

    def assert_existing_artifact_read(self, cursor, *, endpoint, artifact_ref, source, now):
        """Explicit host read class; cannot authorize generation/upload/send/history.

        Does not require logical epoch currency. The port must recheck the exact
        artifact's original endpoint/generation, source grants, retention and
        immutable bytes. A configured runtime's phase freeze alone is not revoke.
        """
        self.lock(cursor, endpoints=(endpoint,), scopes=(endpoint.scope.key,))
        if self.ports.existing_artifact(cursor, source, endpoint=endpoint, artifact_ref=artifact_ref, now=now) is not None:
            raise MigrationDenied("artifact_read_authority_contract")

    def observe_effect(self, cursor, *, activity_id, source, now):
        """Settle an exact persisted host effect, without granting another RPC.

        Local lifecycle/IO receipts need no made-up platform message ID. The
        mandatory host port loads and verifies the immutable domain receipt and
        its original activity/source association. Current route/lease is not a
        prerequisite for recording this historical fact.
        """
        row = cursor.execute("SELECT * FROM migration_dispatch_activities WHERE activity_id=? FOR UPDATE",
                             (activity_id,)).fetchone()
        if row is None or row["operation"] != "execute" or row["source_id"] is None:
            raise MigrationDenied("execution_activity_required")
        evidence = self.ports.effect_receipt(cursor, source, activity=dict(row), now=now)
        if (not isinstance(evidence, ExecutionReceiptEvidence)
                or evidence.domain not in {"project.lifecycle", "project.io"}
                or not isinstance(evidence.reference, str) or not 1 <= len(evidence.reference) <= 128
                or evidence.status not in {"accepted", "confirmed_no", "unknown"}
                or not isinstance(evidence.receipt_digest, str)
                or len(evidence.receipt_digest) != 64
                or any(c not in "0123456789abcdef" for c in evidence.receipt_digest)):
            raise MigrationDenied("execution_receipt_required")
        status = {"confirmed_no": "confirmed_rejected"}.get(evidence.status, evidence.status)
        payload = {"kind": "execution_receipt", "domain": evidence.domain,
                   "reference": evidence.reference, "digest": evidence.receipt_digest, "status": status}
        fingerprint = digest(payload)
        cursor.execute("INSERT INTO migration_activity_observations VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                       (activity_id, fingerprint, encoded(payload), now))
        reliable = cursor.execute("""SELECT COUNT(*) FROM migration_activity_observations
            WHERE activity_id=? AND receipt_json::jsonb->>'status'<>'unknown'""", (activity_id,)).fetchone()[0]
        old = json.loads(row["receipt_json"]) if row["receipt_json"] else None
        if reliable > 1:
            status = "unknown"
        elif old and old["status"] != "unknown" and status == "unknown":
            return
        cursor.execute("""UPDATE migration_dispatch_activities SET state=?,receipt_digest=?,receipt_json=?,observed_at=?
            WHERE activity_id=?""", (status, fingerprint, encoded(payload), now, activity_id))
