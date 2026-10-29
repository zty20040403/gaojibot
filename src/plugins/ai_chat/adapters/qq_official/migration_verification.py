"""Bounded journal-owned verification only; no ordinary route/effect bypass."""
from dataclasses import asdict
import secrets

from .migration_types import MigrationDenied, VerificationProof, _issue, require_issued
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey


class VerificationMethods:
    def _verification_current(self, cursor, migration_id, kind, now):
        journal, route = self._load(cursor, migration_id)
        row = cursor.execute("SELECT * FROM migration_verification_intents WHERE migration_id=? AND kind=? FOR UPDATE",
                             (migration_id, kind)).fetchone()
        if (row is None or route["phase"] != "verifying" or row["state"] == "completed"
                or row["deadline"] <= now or journal["verification_until"] != row["deadline"]
                or row["nonce"] != journal["verification_nonce"] or row["logical_route_epoch"] != route["epoch"]
                or row["endpoint_key"] != route["target_endpoint_key"]):
            raise MigrationDenied("verification_not_current")
        endpoint = cursor.execute("SELECT generation,route_epoch FROM qq_official_endpoints WHERE endpoint_key=?",
                                  (row["endpoint_key"],)).fetchone()
        if (endpoint["generation"], endpoint["route_epoch"]) != (row["endpoint_generation"], row["endpoint_route_epoch"]):
            raise MigrationDenied("verification_endpoint_changed")
        proof = _issue(VerificationProof, migration_id=migration_id, kind=kind, request_id=row["request_id"],
            endpoint=EndpointKey.parse(row["endpoint_key"]), actor=ActorKey.parse(row["actor_key"]),
            nonce=row["nonce"], route_epoch=row["logical_route_epoch"], endpoint_generation=row["endpoint_generation"],
            endpoint_route_epoch=row["endpoint_route_epoch"], deadline=row["deadline"])
        return row, proof

    def issue_verification(self, cursor, *, migration_id, kind, actor, source, now):
        journal, route = self._load(cursor, migration_id)
        endpoint = EndpointKey.parse(route["target_endpoint_key"])
        if (route["phase"] != "verifying" or journal["verification_until"] <= now
                or kind not in {"text", "file"} or not isinstance(actor, ActorKey)
                or actor.identity_kind == "bot" or not actor.matches_endpoint(endpoint)):
            raise MigrationDenied("verification_request_denied")
        if self.ports.verification_request(cursor, source, migration_id=migration_id, endpoint=endpoint,
                actor=actor, kind=kind, nonce=journal["verification_nonce"], deadline=journal["verification_until"], now=now) is not None:
            raise MigrationDenied("verification_source_required")
        existing = cursor.execute("SELECT actor_key FROM migration_verification_intents WHERE migration_id=? AND kind=?",
                                  (migration_id, kind)).fetchone()
        if existing is not None and existing[0] != actor.key:
            raise MigrationDenied("verification_actor_already_fixed")
        cursor.execute("""INSERT INTO migration_verification_intents VALUES
            (?,?,?,?,?,?,?,?,?,?,NULL,'issued') ON CONFLICT DO NOTHING""",
            (migration_id, kind, secrets.token_hex(16), endpoint.key, actor.key, journal["verification_nonce"],
             route["epoch"], route["target_generation"], route["target_endpoint_epoch"], journal["verification_until"]))
        return self._verification_current(cursor, migration_id, kind, now)[1]

    def bind_verification_plan(self, cursor, proof, *, plan_id, now):
        require_issued(proof, VerificationProof)
        intent, current = self._verification_current(cursor, proof.migration_id, proof.kind, now)
        if asdict(proof) != asdict(current) or intent["plan_id"] not in {None, plan_id}:
            raise MigrationDenied("verification_proof_stale")
        plan = cursor.execute("SELECT * FROM delivery_plans WHERE plan_id=? FOR SHARE", (plan_id,)).fetchone()
        if (plan is None or plan["endpoint_key"] != proof.endpoint.key or plan["account_key"] != proof.endpoint.account.key
                or plan["requester_actor_key"] != proof.actor.key or plan["source_scope_key"] != proof.endpoint.scope.key
                or plan["endpoint_generation"] != proof.endpoint_generation or plan["route_epoch"] != proof.endpoint_route_epoch
                or plan["part_count"] != 1 or plan["expires_at"] > proof.deadline):
            raise MigrationDenied("verification_plan_mismatch")
        if intent["plan_id"] is None and cursor.execute("""SELECT 1 FROM delivery_attempts a
            JOIN deliveries d USING(delivery_id) WHERE d.plan_id=? LIMIT 1""", (plan_id,)).fetchone():
            raise MigrationDenied("verification_plan_already_attempted")
        # Host proves exact bounded template/file source, actual fresh trigger,
        # and journal nonce association; it must not accept an arbitrary model
        # body merely because this metadata object exists.
        if self.ports.verification_plan(cursor, proof, plan_id, now=now) is not None:
            raise MigrationDenied("verification_plan_authority_required")
        cursor.execute("UPDATE migration_verification_intents SET plan_id=?,state='bound' WHERE migration_id=? AND kind=?",
                       (plan_id, proof.migration_id, proof.kind))

    def current_verification_plan_source(self, cursor, plan_id, *, now):
        intent = cursor.execute("SELECT migration_id,kind FROM migration_verification_intents WHERE plan_id=?",
                                (plan_id,)).fetchone()
        if intent is None:
            raise MigrationDenied("verification_plan_unregistered")
        row, proof = self._verification_current(cursor, intent["migration_id"], intent["kind"], now)
        if row["state"] != "bound" or self.ports.verification_plan(cursor, proof, plan_id, now=now) is not None:
            raise MigrationDenied("verification_plan_authority_required")
        return proof
