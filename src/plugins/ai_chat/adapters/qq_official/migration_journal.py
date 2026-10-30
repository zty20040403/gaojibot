"""Executable migration state machine, using existing jobs and true leases.

All host ports are mandatory/default-deny. No network, sleep, hidden commit,
model loop, target-inbound fabrication or autonomous background queue is here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re
import secrets

from .migration_types import (
    ControlEvidence, ExportEvidence, MappingEvidence, MigrationDenied, MigrationItemProof,
    VerificationEvidence, _issue, digest, encoded, require_issued,
)
from .migration_routes import RouteService, positive, same_physical_scope
from .migration_verification import VerificationMethods
from .migration_artifact_journal import MigrationArtifactMethods
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from src.plugins.ai_chat.storage.jobs import DurableJob

PARKED_AT = 2**63-1


@dataclass(frozen=True)
class Plan:
    migration_id: str
    route_id: str
    preview_digest: str
    route_epoch: int


class Journal(VerificationMethods, MigrationArtifactMethods):
    def __init__(self, routes: RouteService):
        self.routes = routes
        self.ports = routes.ports

    def _control(self, cursor, authorization, action, preview_digest, now):
        evidence = self.ports.control(cursor, authorization, action=action,
                                      preview_digest=preview_digest, now=now)
        if not isinstance(evidence, ControlEvidence):
            raise MigrationDenied("console_authority_required")
        row = cursor.execute("""SELECT a.account_id FROM admin_accounts a JOIN admin_sessions s USING(account_id)
            WHERE a.account_id=? AND a.version=? AND a.enabled=1 AND a.role='admin'
            AND s.session_hash=? AND s.account_version=a.version AND s.expires_at>?
            FOR SHARE OF a,s""", (evidence.account_id, evidence.account_version, evidence.session_hash, now)).fetchone()
        if row is None:
            raise MigrationDenied("console_session_expired")
        return evidence

    def _mapping(self, cursor, source_endpoint, source_scope, source_actor, target, now):
        evidence = self.ports.mapping(cursor, source_endpoint, source_scope, source_actor, target, now)
        if (not isinstance(evidence, MappingEvidence) or evidence.source != source_actor
                or evidence.source_endpoint != source_endpoint or evidence.source_scope.key != source_scope.key
                or evidence.target_endpoint != target or not evidence.target.matches_endpoint(target)
                or evidence.target.identity_kind == "bot" or evidence.revision < 1
                or evidence.expires_at <= now):
            raise MigrationDenied("actor_mapping_unavailable")
        return evidence

    def _export(self, cursor, source, scope, target, kind, ref, now):
        value = self.ports.export(cursor, source, scope, target, kind, ref, now)
        if (not isinstance(value, ExportEvidence) or value.kind != kind or value.source_ref != ref
                or value.classification not in {"unsent", "unknown", "confirmed", "terminal", "unsupported"}
                or value.source_revision < 1 or value.kind not in {"reminder", "routine", "subagent", "project_task", "file"}):
            raise MigrationDenied("invalid_migration_export")
        if not re.fullmatch(r"[0-9a-f]{64}", value.export_digest):
            raise MigrationDenied("invalid_migration_export_digest")
        payload = json.loads(value.export_json)
        if not isinstance(payload, dict) or encoded(payload) != value.export_json:
            raise MigrationDenied("noncanonical_migration_export")
        if value.classification == "unsent" and (value.source_actor is None
                or not value.source_actor.matches_endpoint(source) or value.source_actor.identity_kind == "bot"):
            raise MigrationDenied("migration_source_actor_required")
        return value

    def _material(self, cursor, source, scope, target, kind, ref, now):
        value = self._export(cursor, source, scope, target, kind, ref, now)
        mapping = self._mapping(cursor, source, scope, value.source_actor, target, now) if value.classification == "unsent" else None
        data = asdict(value)
        data["source_actor"] = value.source_actor.key if value.source_actor else None
        data["mapping"] = None if mapping is None else {
            "mapping_id": mapping.mapping_id, "revision": mapping.revision, "source": mapping.source.key,
            "target": mapping.target.key, "target_endpoint": mapping.target_endpoint.key,
            "expires_at": mapping.expires_at,
        }
        return value, mapping, data

    def plan(self, cursor, *, source_endpoint, source_scope, target_endpoint, items, authorization, baseline_source, deadline, now):
        if (source_endpoint.kind != "group" or target_endpoint.kind != "group"
                or target_endpoint.account.platform != "qq-official"
                or not isinstance(source_scope, ConversationScope)
                or not same_physical_scope(source_scope.key, source_endpoint)
                or (source_scope.bot_native_user_id and source_scope.bot_native_user_id != source_endpoint.account.app_id)
                or source_scope.key == target_endpoint.scope.key
                or not isinstance(items, tuple) or len(items) > 100 or len(set(items)) != len(items)):
            raise MigrationDenied("invalid_migration_selection")
        if not now < positive(deadline) <= now+86400:
            raise MigrationDenied("invalid_migration_deadline")
        # Baseline must predate planning/business. Never manufacture readiness
        # merely because a plan is requested. The host source proof is rechecked.
        baseline = self.ports.baseline(cursor, baseline_source, account=source_endpoint.account, now=now)
        endpoint_rows = self.routes.lock(cursor, endpoints=(source_endpoint, target_endpoint),
                                        scopes=(source_scope.key, target_endpoint.scope.key))
        registered = self.routes._baseline(cursor, source_endpoint.account.key, now)
        if (registered["runtime_id"], registered["deployment_digest"]) != (baseline.runtime_id, baseline.deployment_digest):
            raise MigrationDenied("baseline_changed")
        if self.routes._route(cursor, source_scope.key) or self.routes._route(cursor, target_endpoint.scope.key):
            raise MigrationDenied("migration_route_already_exists")
        admin = self._control(cursor, authorization, "plan", "", now)
        materials = [self._material(cursor, source_endpoint, source_scope, target_endpoint, kind, ref, now)
                     for kind, ref in items]
        target = endpoint_rows[target_endpoint.key]
        source = endpoint_rows.get(source_endpoint.key)
        preview = {
            "source_endpoint": source_endpoint.key, "source_scope": source_scope.key,
            "target_endpoint": target_endpoint.key, "target_scope": target_endpoint.scope.key,
            "baseline_runtime_id": registered["runtime_id"], "baseline_revision": registered["revision"],
            "source_generation": source["generation"] if source else None,
            "source_endpoint_epoch": source["route_epoch"] if source else None,
            "target_generation": target["generation"], "target_endpoint_epoch": target["route_epoch"],
            "deadline": deadline, "items": [material[2] for material in materials],
        }
        fingerprint, route_id, migration_id = digest(preview), secrets.token_hex(16), secrets.token_hex(16)
        cursor.execute("""INSERT INTO migration_routes VALUES
            (?,?,?,?,?,?,?,?,?,?,?,0,'planned',?,?,?)""",
            (route_id, source_scope.key, source_endpoint.key, target_endpoint.scope.key, target_endpoint.key,
             source_endpoint.account.key, target_endpoint.account.key, preview["source_generation"],
             preview["source_endpoint_epoch"], preview["target_generation"], preview["target_endpoint_epoch"],
             source_endpoint.key, registered["runtime_id"], registered["revision"]))
        cursor.execute("INSERT INTO migration_scope_aliases VALUES (?,?),(?,?)",
                       (source_scope.key, route_id, target_endpoint.scope.key, route_id))
        cursor.execute("""INSERT INTO migration_journals(migration_id,route_id,preview_digest,preview_json,
            creator_id,creator_version,deadline,created_at) VALUES (?,?,?,?,?,?,?,?)""",
                       (migration_id, route_id, fingerprint, encoded(preview), admin.account_id, admin.account_version, deadline, now))
        cursor.execute("INSERT INTO migration_preview_revisions VALUES (?,1,?,?,?)",
                       (migration_id, fingerprint, encoded(preview), now))
        for value, mapping, material in materials:
            cursor.execute("""INSERT INTO migration_items(item_id,migration_id,kind,source_ref,source_revision,
                classification,export_json,export_digest,source_actor_key,target_actor_key,mapping_id,
                mapping_revision,mapping_expires_at,execution_deadline,result_deadline,source_canonical_id,
                source_native_id,schedule_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (secrets.token_hex(16), migration_id, value.kind, value.source_ref, value.source_revision,
                 value.classification, value.export_json, value.export_digest,
                 mapping.source.key if mapping else None, mapping.target.key if mapping else None,
                 mapping.mapping_id if mapping else None, mapping.revision if mapping else None,
                 mapping.expires_at if mapping else None, value.execution_deadline, value.result_deadline,
                 value.source_canonical_id, value.source_native_id, value.schedule_json, now))
        self._event(cursor, migration_id, "planned", 0, "preview_created", now)
        return Plan(migration_id, route_id, fingerprint, 0)

    def _load(self, cursor, migration_id):
        journal = cursor.execute("SELECT * FROM migration_journals WHERE migration_id=?", (migration_id,)).fetchone()
        if journal is None:
            raise MigrationDenied()
        row = cursor.execute("SELECT * FROM migration_routes WHERE route_id=?", (journal["route_id"],)).fetchone()
        self.routes.lock(cursor, endpoints=(EndpointKey.parse(row["source_endpoint_key"]), EndpointKey.parse(row["target_endpoint_key"])),
                         scopes=(row["source_scope_key"], row["target_scope_key"]))
        journal = cursor.execute("SELECT * FROM migration_journals WHERE migration_id=? FOR UPDATE", (migration_id,)).fetchone()
        row = cursor.execute("SELECT * FROM migration_routes WHERE route_id=? FOR UPDATE", (journal["route_id"],)).fetchone()
        preview = json.loads(journal["preview_json"])
        if digest(preview) != journal["preview_digest"]:
            raise MigrationDenied("migration_manifest_changed")
        for stored, frozen in (("source_scope_key", "source_scope"), ("target_scope_key", "target_scope"),
                               ("source_endpoint_key", "source_endpoint"), ("target_endpoint_key", "target_endpoint"),
                               ("target_generation", "target_generation"), ("target_endpoint_epoch", "target_endpoint_epoch")):
            if row[stored] != preview[frozen]:
                raise MigrationDenied("migration_manifest_changed")
        return journal, row

    def refresh_plan(self, cursor, *, migration_id, authorization, now, deadline=None):
        """Replace a stale planned preview; retain each earlier approved digest.

        An administrator may explicitly reschedule an unfrozen plan. Every
        source is re-read; task, mapping, file and reply deadlines are unchanged.
        """
        journal, route = self._load(cursor, migration_id)
        if route["phase"] != "planned" or route["epoch"] != 0:
            raise MigrationDenied("migration_phase_mismatch")
        self._control(cursor, authorization, "refresh", journal["preview_digest"], now)
        if deadline is None:
            deadline = journal["deadline"]
        if not now < positive(deadline) <= now+86400:
            raise MigrationDenied("invalid_migration_deadline")
        baseline = self.routes._baseline(cursor, route["source_account_key"], now)
        source, target = EndpointKey.parse(route["source_endpoint_key"]), EndpointKey.parse(route["target_endpoint_key"])
        scope = ConversationScope.from_key(route["source_scope_key"])
        previous = json.loads(journal["preview_json"])
        materials = [self._material(cursor, source, scope, target, value["kind"], value["source_ref"], now)
                     for value in previous["items"]]
        endpoint = cursor.execute("SELECT generation,route_epoch FROM qq_official_endpoints WHERE endpoint_key=?", (target.key,)).fetchone()
        source_row = (cursor.execute("SELECT generation,route_epoch FROM qq_official_endpoints WHERE endpoint_key=?", (source.key,)).fetchone()
                      if source.account.platform == "qq-official" else None)
        preview = {**previous, "deadline": deadline,
                   "baseline_runtime_id": baseline["runtime_id"], "baseline_revision": baseline["revision"],
                   "source_generation": source_row["generation"] if source_row else None,
                   "source_endpoint_epoch": source_row["route_epoch"] if source_row else None,
                   "target_generation": endpoint["generation"], "target_endpoint_epoch": endpoint["route_epoch"],
                   "items": [value[2] for value in materials]}
        fingerprint = digest(preview)
        cursor.execute("""UPDATE migration_routes SET baseline_runtime_id=?,baseline_revision=?,
            source_generation=?,source_endpoint_epoch=?,target_generation=?,target_endpoint_epoch=? WHERE route_id=?""",
            (baseline["runtime_id"], baseline["revision"], preview["source_generation"], preview["source_endpoint_epoch"],
             preview["target_generation"], preview["target_endpoint_epoch"], route["route_id"]))
        for value, mapping, _ in materials:
            cursor.execute("""UPDATE migration_items SET item_revision=item_revision+1,source_revision=?,
                classification=?,export_json=?,export_digest=?,source_actor_key=?,target_actor_key=?,mapping_id=?,
                mapping_revision=?,mapping_expires_at=?,execution_deadline=?,result_deadline=?,
                source_canonical_id=?,source_native_id=?,schedule_json=? WHERE migration_id=? AND kind=? AND source_ref=?""",
                (value.source_revision, value.classification, value.export_json, value.export_digest,
                 mapping.source.key if mapping else None, mapping.target.key if mapping else None,
                 mapping.mapping_id if mapping else None, mapping.revision if mapping else None,
                 mapping.expires_at if mapping else None, value.execution_deadline, value.result_deadline,
                 value.source_canonical_id, value.source_native_id, value.schedule_json,
                 migration_id, value.kind, value.source_ref))
        cursor.execute("""UPDATE migration_journals SET preview_digest=?,preview_json=?,deadline=?,revision=revision+1,
            approved_by=NULL,approved_version=NULL WHERE migration_id=?""", (fingerprint, encoded(preview), deadline, migration_id))
        cursor.execute("INSERT INTO migration_preview_revisions VALUES (?,?,?,?,?)",
                       (migration_id, journal["revision"]+1, fingerprint, encoded(preview), now))
        self._event(cursor, migration_id, "planned", route["epoch"], "preview_refreshed", now)
        return Plan(migration_id, route["route_id"], fingerprint, route["epoch"])

    def _event(self, cursor, migration_id, phase, epoch, reason, now):
        cursor.execute("INSERT INTO migration_events(migration_id,phase,route_epoch,reason,occurred_at) VALUES (?,?,?,?,?)",
                       (migration_id, phase, epoch, reason, now))

    def _phase(self, cursor, journal, route, phase, now, *, advance=False, active=None):
        epoch = route["epoch"]+int(advance)
        cursor.execute("UPDATE migration_routes SET phase=?,epoch=?,active_endpoint_key=? WHERE route_id=?",
                       (phase, epoch, active or route["active_endpoint_key"], route["route_id"]))
        cursor.execute("UPDATE migration_journals SET revision=revision+1 WHERE migration_id=?", (journal["migration_id"],))
        self._event(cursor, journal["migration_id"], phase, epoch, "phase_transition", now)
        return epoch

    def freeze(self, cursor, *, migration_id, reviewed_digest, authorization, now):
        journal, route = self._load(cursor, migration_id)
        if route["phase"] != "planned" or journal["deadline"] <= now or reviewed_digest != journal["preview_digest"]:
            raise MigrationDenied("migration_review_stale")
        admin = self._control(cursor, authorization, "freeze", reviewed_digest, now)
        baseline = self.routes._baseline(cursor, route["source_account_key"], now)
        if (baseline["runtime_id"], baseline["revision"]) != (route["baseline_runtime_id"], route["baseline_revision"]):
            raise MigrationDenied("baseline_changed")
        source, target = EndpointKey.parse(route["source_endpoint_key"]), EndpointKey.parse(route["target_endpoint_key"])
        preview = json.loads(journal["preview_json"])
        current = [self._material(cursor, source, ConversationScope.from_key(route["source_scope_key"]), target, value["kind"], value["source_ref"], now)[2]
                   for value in preview["items"]]
        if current != preview["items"]:
            raise MigrationDenied("migration_preview_changed")
        endpoint = cursor.execute("SELECT generation,route_epoch FROM qq_official_endpoints WHERE endpoint_key=?", (target.key,)).fetchone()
        if (endpoint["generation"], endpoint["route_epoch"]) != (route["target_generation"], route["target_endpoint_epoch"]):
            raise MigrationDenied("endpoint_epoch_stale")
        if source.account.platform == "qq-official":
            physical_source = cursor.execute("SELECT generation,route_epoch FROM qq_official_endpoints WHERE endpoint_key=?", (source.key,)).fetchone()
            if (physical_source["generation"], physical_source["route_epoch"]) != (route["source_generation"], route["source_endpoint_epoch"]):
                raise MigrationDenied("source_endpoint_epoch_stale")
        cursor.execute("UPDATE migration_journals SET approved_by=?,approved_version=? WHERE migration_id=?",
                       (admin.account_id, admin.account_version, migration_id))
        return self._phase(cursor, journal, route, "frozen", now, advance=True)

    def begin_draining(self, cursor, *, migration_id, authorization, now):
        journal, route = self._load(cursor, migration_id)
        self._control(cursor, authorization, "drain", journal["preview_digest"], now)
        if route["phase"] != "frozen":
            raise MigrationDenied("migration_phase_mismatch")
        return self._phase(cursor, journal, route, "draining", now)

    def quarantine_activity(self, cursor, *, migration_id, activity_id, authorization, now):
        journal, route = self._load(cursor, migration_id)
        self._control(cursor, authorization, "quarantine", journal["preview_digest"], now)
        if route["phase"] not in {"draining", "rollback_pending"}:
            raise MigrationDenied("migration_phase_mismatch")
        row = cursor.execute("SELECT * FROM migration_dispatch_activities WHERE activity_id=? FOR UPDATE", (activity_id,)).fetchone()
        if row is None or row["source_scope_key"] not in {route["source_scope_key"], route["target_scope_key"]}:
            raise MigrationDenied("activity_scope_mismatch")
        if row["state"] in {"dispatched", "unknown"}:
            cursor.execute("UPDATE migration_dispatch_activities SET state='quarantined' WHERE activity_id=?", (activity_id,))
        self._event(cursor, migration_id, route["phase"], route["epoch"], "unknown_retained_on_original_route", now)

    def switch_target(self, cursor, *, migration_id, authorization, now):
        journal, route = self._load(cursor, migration_id)
        self._control(cursor, authorization, "switch", journal["preview_digest"], now)
        if route["phase"] != "draining" or journal["deadline"] <= now:
            raise MigrationDenied("migration_phase_mismatch")
        if cursor.execute("""SELECT 1 FROM migration_dispatch_activities WHERE source_scope_key IN (?,?)
            AND state IN ('dispatched','unknown') LIMIT 1""", (route["source_scope_key"], route["target_scope_key"])).fetchone():
            raise MigrationDenied("migration_dispatch_unresolved")
        return self._phase(cursor, journal, route, "switching", now, advance=True, active=route["target_endpoint_key"])

    def begin_verification(self, cursor, *, migration_id, authorization, now):
        journal, route = self._load(cursor, migration_id)
        self._control(cursor, authorization, "verify", journal["preview_digest"], now)
        if route["phase"] != "switching" or journal["deadline"] <= now:
            raise MigrationDenied("migration_phase_mismatch")
        nonce = secrets.token_hex(16)
        cursor.execute("UPDATE migration_journals SET verification_nonce=?,verification_until=? WHERE migration_id=?",
                       (nonce, min(now+300, journal["deadline"]), migration_id))
        self._phase(cursor, journal, route, "verifying", now)
        return nonce

    def record_verification(self, cursor, *, migration_id, source, now):
        journal, route = self._load(cursor, migration_id)
        if route["phase"] != "verifying" or journal["verification_until"] <= now:
            raise MigrationDenied("verification_expired")
        value = self.ports.verification(cursor, source, migration_id=migration_id,
            target_endpoint=EndpointKey.parse(route["target_endpoint_key"]), nonce=journal["verification_nonce"], now=now)
        if not isinstance(value, VerificationEvidence) or value.kind not in {"text", "file"} or value.nonce != journal["verification_nonce"]:
            raise MigrationDenied("verification_evidence_required")
        intent = cursor.execute("SELECT * FROM migration_verification_intents WHERE migration_id=? AND kind=?",
                                (migration_id, value.kind)).fetchone()
        if intent is None or intent["state"] not in {"bound", "completed"}:
            raise MigrationDenied("verification_plan_unregistered")
        receipt = cursor.execute("""SELECT r.receipt_id FROM delivery_receipts r
            JOIN delivery_attempts a USING(attempt_id) JOIN deliveries d USING(delivery_id)
            WHERE r.receipt_id=? AND d.delivery_id=? AND r.endpoint_key=? AND r.account_key=?
              AND r.applied_at IS NOT NULL AND d.status='committed' AND a.state='committed'
              AND d.plan_id=?""",
            (value.receipt_id, value.delivery_id, route["target_endpoint_key"], route["target_account_key"], intent["plan_id"])).fetchone()
        if receipt is None:
            raise MigrationDenied("verification_receipt_not_applied")
        old = cursor.execute("SELECT receipt_id FROM migration_verification_receipts WHERE migration_id=? AND kind=?",
                             (migration_id, value.kind)).fetchone()
        if old and old[0] != value.receipt_id:
            raise MigrationDenied("verification_receipt_conflict")
        cursor.execute("INSERT INTO migration_verification_receipts VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
                       (migration_id, value.kind, value.receipt_id, value.delivery_id, value.nonce))
        cursor.execute("UPDATE migration_verification_intents SET state='completed' WHERE migration_id=? AND kind=?",
                       (migration_id, value.kind))

    def complete(self, cursor, *, migration_id, authorization, now):
        journal, route = self._load(cursor, migration_id)
        self._control(cursor, authorization, "complete", journal["preview_digest"], now)
        if route["phase"] != "verifying" or journal["deadline"] <= now:
            raise MigrationDenied("migration_phase_mismatch")
        kinds = cursor.execute("SELECT kind FROM migration_verification_receipts WHERE migration_id=?", (migration_id,)).fetchall()
        if {row[0] for row in kinds} != {"text", "file"}:
            raise MigrationDenied("verification_incomplete")
        # Ordinary target jobs remain parked until continuation activates them
        # through require_item; merely opening this gate never resets deadlines.
        return self._phase(cursor, journal, route, "completed", now)

    def rollback(self, cursor, *, migration_id, authorization, now, finish=False):
        journal, route = self._load(cursor, migration_id)
        self._control(cursor, authorization, "rollback", journal["preview_digest"], now)
        if not finish:
            if route["phase"] in {"planned", "rolled_back", "rollback_pending"}:
                raise MigrationDenied("migration_phase_mismatch")
            if self.ports.revoke(cursor, migration_id=migration_id,
                                 target_endpoint=EndpointKey.parse(route["target_endpoint_key"]), now=now) is not None:
                raise MigrationDenied("migration_revoke_contract")
            cursor.execute("UPDATE migration_items SET state='revoked' WHERE migration_id=?", (migration_id,))
            return self._phase(cursor, journal, route, "rollback_pending", now, advance=True)
        if route["phase"] != "rollback_pending":
            raise MigrationDenied("migration_phase_mismatch")
        self.routes._baseline(cursor, route["source_account_key"], now)
        if cursor.execute("""SELECT 1 FROM migration_dispatch_activities WHERE source_scope_key IN (?,?)
            AND state IN ('dispatched','unknown') LIMIT 1""", (route["source_scope_key"], route["target_scope_key"])).fetchone():
            raise MigrationDenied("migration_dispatch_unresolved")
        return self._phase(cursor, journal, route, "rolled_back", now, advance=True, active=route["source_endpoint_key"])

    def _item(self, cursor, item_id, operation, now, job=None, plan_id=None):
        item = cursor.execute("SELECT * FROM migration_items WHERE item_id=?", (item_id,)).fetchone()
        if item is None:
            raise MigrationDenied("migration_item_missing")
        journal, route = self._load(cursor, item["migration_id"])
        item = cursor.execute("SELECT * FROM migration_items WHERE item_id=? FOR UPDATE", (item_id,)).fetchone()
        phases = {"prepare": {"switching", "verifying"}, "bind": {"switching", "verifying"}, "activate": {"completed"},
                  "execute": {"completed"}, "send": {"completed"}, "deliver": {"completed"}, "expire": {"completed"}, "artifact": {"completed"}}
        if (operation not in phases or route["phase"] not in phases[operation]
                or item["classification"] != "unsent" or item["state"] in {"revoked", "expired", "quarantined"}):
            raise MigrationDenied("migration_item_not_current")
        deadline = item["execution_deadline"] if operation == "execute" else item["result_deadline"]
        if operation == "expire":
            if deadline is None or now < deadline:
                raise MigrationDenied("migration_item_not_expired")
        elif deadline is not None and deadline <= now:
            raise MigrationDenied("migration_item_expired")
        source, target = EndpointKey.parse(route["source_endpoint_key"]), EndpointKey.parse(route["target_endpoint_key"])
        if operation != "expire":
            mapping = self._mapping(cursor, source, ConversationScope.from_key(route["source_scope_key"]), ActorKey.parse(item["source_actor_key"]), target, now)
            if (mapping.mapping_id, mapping.revision, mapping.target.key, mapping.expires_at) != (
                    item["mapping_id"], item["mapping_revision"], item["target_actor_key"], item["mapping_expires_at"]):
                raise MigrationDenied("migration_actor_mapping_changed")
        if operation == "prepare" and item["state"] == "reviewed" and item["parent_item_id"] is None:
            current = self._material(cursor, source, ConversationScope.from_key(route["source_scope_key"]), target, item["kind"], item["source_ref"], now)[2]
            frozen = next((record for record in json.loads(journal["preview_json"])["items"]
                           if record["kind"] == item["kind"] and record["source_ref"] == item["source_ref"]), None)
            if current != frozen or current["export_digest"] != item["export_digest"]:
                raise MigrationDenied("migration_source_changed")
        if operation == "prepare" and item["state"] == "prepared" and item["schedule_json"] is None:
            persisted = cursor.execute("SELECT * FROM durable_jobs WHERE job_id=? FOR UPDATE", (item["job_id"],)).fetchone()
            if (persisted is None or persisted["kind"] != item["expected_job_kind"]
                    or persisted["scope_key"] != target.scope.key or persisted["status"] != "pending"
                    or persisted["next_attempt_at"] != PARKED_AT or persisted["attempts"] != 0
                    or self.ports.bound_job(cursor, item, persisted, operation="prepare", now=now) is not None):
                raise MigrationDenied("prepared_migration_binding_missing")
        if operation == "prepare" and item["state"] == "prepared" and item["schedule_json"] is not None:
            if item["target_routine_id"] is None or self.ports.bound_schedule(
                    cursor, item, item["target_routine_id"], operation="prepare", now=now) is not None:
                raise MigrationDenied("prepared_schedule_binding_missing")
        endpoint = cursor.execute("SELECT generation,route_epoch FROM qq_official_endpoints WHERE endpoint_key=?", (target.key,)).fetchone()
        if (endpoint["generation"], endpoint["route_epoch"]) != (route["target_generation"], route["target_endpoint_epoch"]):
            raise MigrationDenied("endpoint_epoch_stale")
        if operation == "artifact" and item["state"] != "active":
            raise MigrationDenied("migration_artifact_item_not_active")
        if operation in {"execute", "send"}:
            if not isinstance(job, DurableJob) or item["job_id"] != job.job_id or item["state"] != "active":
                raise MigrationDenied("migration_job_required")
            current = cursor.execute("SELECT * FROM durable_jobs WHERE job_id=? FOR UPDATE", (job.job_id,)).fetchone()
            if (current is None or current["status"] != "running" or current["kind"] != item["expected_job_kind"]
                    or current["scope_key"] != target.scope.key or current["lease_until"] is None or current["lease_until"] <= now
                    or current["lease_owner"] != job.lease_owner or not job.lease_owner
                    or current["lease_generation"] != job.lease_generation or job.lease_generation < 1
                    or current["attempts"] != job.attempts or current["kind"] != job.kind
                    or current["scope_key"] != job.scope_key or json.loads(current["payload_json"]) != job.payload):
                raise MigrationDenied("migration_job_lease_lost")
            if self.ports.bound_job(cursor, item, job, operation=operation, now=now) is not None:
                raise MigrationDenied("migration_job_binding_unavailable")
        if operation == "deliver":
            binding = cursor.execute("SELECT * FROM migration_item_plans WHERE plan_id=? AND item_id=?",
                                     (plan_id, item_id)).fetchone()
            if (binding is None or binding["source_job_id"] != item["job_id"]
                    or binding["logical_route_epoch"] != route["epoch"]
                    or binding["mapping_revision"] != item["mapping_revision"]
                    or self.ports.bound_plan(cursor, item, plan_id, now=now) is not None):
                raise MigrationDenied("migration_plan_binding_unavailable")
        return item, journal, route

    def issue_item(self, cursor, *, item_id, operation="prepare", job=None, plan_id=None, now):
        if operation not in {"prepare", "activate", "execute", "send", "deliver", "expire"}:
            raise MigrationDenied("invalid_item_operation")
        return self._issue_item(cursor, item_id=item_id, operation=operation, job=job, plan_id=plan_id, now=now)

    def _issue_item(self, cursor, *, item_id, operation, job=None, plan_id=None, now):
        item, journal, route = self._item(cursor, item_id, operation, now, job, plan_id)
        return _issue(MigrationItemProof, item_id=item_id, migration_id=item["migration_id"],
            item_revision=item["item_revision"], export_digest=item["export_digest"],
            source_endpoint=EndpointKey.parse(route["source_endpoint_key"]),
            source_scope=ConversationScope.from_key(route["source_scope_key"]), source_kind=item["kind"], source_key=item["source_ref"],
            source_revision=item["source_revision"],
            target_endpoint=EndpointKey.parse(route["target_endpoint_key"]), target_actor=ActorKey.parse(item["target_actor_key"]),
            mapping_id=item["mapping_id"], mapping_revision=item["mapping_revision"], route_epoch=route["epoch"],
            endpoint_generation=route["target_generation"], endpoint_route_epoch=route["target_endpoint_epoch"],
            source_canonical_id=item["source_canonical_id"], source_native_id=item["source_native_id"],
            execution_deadline=item["execution_deadline"], result_deadline=item["result_deadline"],
            schedule_json=item["schedule_json"], job_id=item["job_id"],
            issuance_transaction_id=cursor.execute("SELECT txid_current()").fetchone()[0],
            target_routine_id=item["target_routine_id"])

    def require_item(self, cursor, proof, *, operation, job=None, plan_id=None, now):
        require_issued(proof, MigrationItemProof)
        current = self._issue_item(cursor, item_id=proof.item_id, operation=operation, job=job, plan_id=plan_id, now=now)
        # job_id is filled once during prepare; the original issued proof may be
        # used to activate that exact persisted job, never to replace it.
        old, new = asdict(proof), asdict(current)
        old.pop("job_id")
        new.pop("job_id")
        old.pop("issuance_transaction_id")
        new.pop("issuance_transaction_id")
        old.pop("target_routine_id")
        new.pop("target_routine_id")
        if (old != new or (proof.job_id is not None and proof.job_id != current.job_id)
                or (proof.target_routine_id is not None and proof.target_routine_id != current.target_routine_id)):
            raise MigrationDenied("migration_proof_stale")
        return current

    def bind_item_job(self, cursor, proof, *, job_id, expected_kind, now):
        require_issued(proof, MigrationItemProof)
        if proof.issuance_transaction_id != cursor.execute("SELECT txid_current()").fetchone()[0]:
            raise MigrationDenied("conversion_requires_same_transaction")
        self.require_item(cursor, proof, operation="bind", now=now)
        if proof.schedule_json is not None:
            raise MigrationDenied("schedule_requires_occurrence_child")
        row = cursor.execute("SELECT * FROM durable_jobs WHERE job_id=? FOR UPDATE", (positive(job_id),)).fetchone()
        if (row is None or row["status"] != "pending" or row["attempts"] != 0
                or row["next_attempt_at"] != PARKED_AT or row["kind"] != expected_kind
                or row["scope_key"] != proof.target_endpoint.scope.key):
            raise MigrationDenied("migration_job_not_parked")
        item = cursor.execute("SELECT job_id FROM migration_items WHERE item_id=?", (proof.item_id,)).fetchone()
        if item[0] is not None and item[0] != job_id:
            raise MigrationDenied("migration_job_already_bound")
        if self.ports.bound_job(cursor, proof, row, operation="prepare", now=now) is not None:
            raise MigrationDenied("migration_job_binding_unavailable")
        cursor.execute("UPDATE migration_items SET job_id=?,expected_job_kind=?,state='prepared' WHERE item_id=?",
                       (job_id, expected_kind, proof.item_id))

    def activate_item(self, cursor, proof, *, due_at, now):
        current = self.require_item(cursor, proof, operation="activate", now=now)
        positive(due_at, zero=True)
        if current.result_deadline is not None and due_at >= current.result_deadline:
            raise MigrationDenied("migration_due_after_deadline")
        row = cursor.execute("SELECT * FROM durable_jobs WHERE job_id=? FOR UPDATE", (current.job_id,)).fetchone()
        if row is None:
            raise MigrationDenied("migration_job_missing")
        item = cursor.execute("SELECT state FROM migration_items WHERE item_id=?", (proof.item_id,)).fetchone()
        if item[0] == "active":
            return current.job_id
        if row["status"] != "pending" or row["next_attempt_at"] != PARKED_AT or row["attempts"] != 0:
            raise MigrationDenied("migration_job_not_parked")
        # The continuation host validates due_at against the frozen original
        # domain record. It is not a deadline renewal or new occurrence.
        if self.ports.bound_job(cursor, current, row, operation="activate", due_at=due_at, now=now) is not None:
            raise MigrationDenied("migration_job_binding_unavailable")
        cursor.execute("UPDATE durable_jobs SET next_attempt_at=?,updated_at=? WHERE job_id=?", (due_at, now, current.job_id))
        cursor.execute("UPDATE migration_items SET state='active' WHERE item_id=?", (proof.item_id,))
        return current.job_id

    def bind_item_plan(self, cursor, proof, *, plan_id, job, now):
        self.require_item(cursor, proof, operation="send", job=job, now=now)
        plan = cursor.execute("SELECT * FROM delivery_plans WHERE plan_id=? FOR SHARE", (plan_id,)).fetchone()
        if (plan is None or plan["endpoint_key"] != proof.target_endpoint.key
                or plan["account_key"] != proof.target_endpoint.account.key
                or plan["requester_actor_key"] != proof.target_actor.key
                or plan["source_scope_key"] != proof.target_endpoint.scope.key
                or plan["endpoint_generation"] != proof.endpoint_generation
                or plan["route_epoch"] != proof.endpoint_route_epoch
                or (proof.result_deadline is not None and plan["expires_at"] > proof.result_deadline)):
            raise MigrationDenied("migration_plan_namespace_mismatch")
        if self.ports.bound_plan(cursor, proof, plan_id, now=now) is not None:
            raise MigrationDenied("migration_plan_binding_unavailable")
        row = cursor.execute("SELECT item_id FROM migration_item_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is not None and row[0] != proof.item_id:
            raise MigrationDenied("migration_plan_already_bound")
        cursor.execute("INSERT INTO migration_item_plans VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                       (plan_id, proof.item_id, job.job_id, proof.route_epoch, proof.mapping_revision, now))

    def current_plan_source(self, cursor, plan_id, *, now):
        row = cursor.execute("SELECT item_id FROM migration_item_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise MigrationDenied("migration_plan_binding_unavailable")
        return self.issue_item(cursor, item_id=row[0], operation="deliver", plan_id=plan_id, now=now)

    def add_occurrence(self, cursor, parent_proof, *, run_id, scheduled_at, job_id, expected_kind,
                       execution_deadline, result_deadline, now):
        """One bounded child for one REAL routine_run/job, never a second queue.

        Host occurrence port must check cron/timezone, frozen source version and
        exact capability grant, run uniqueness/domain links and old-unknown
        exclusion. Default deny. This method independently checks all budgets,
        deadlines, completed phase, immutable mapping and current target route.
        """
        parent = self.require_item(cursor, parent_proof, operation="activate", now=now)
        if parent.schedule_json is None or parent.job_id is not None:
            raise MigrationDenied("frozen_schedule_required")
        state = cursor.execute("SELECT state FROM migration_items WHERE item_id=?", (parent.item_id,)).fetchone()
        if state[0] != "active" or parent.target_routine_id is None:
            raise MigrationDenied("schedule_not_active")
        if self.ports.bound_schedule(cursor, parent, parent.target_routine_id, operation="occurrence", now=now) is not None:
            raise MigrationDenied("schedule_binding_unavailable")
        schedule = json.loads(parent.schedule_json)
        required = {"cron", "timezone", "version", "cutover", "schedule_until", "max_occurrences",
                    "execution_seconds", "result_seconds", "capability_grant"}
        if not isinstance(schedule, dict) or set(schedule) != required:
            raise MigrationDenied("invalid_frozen_schedule")
        for key in ("version", "schedule_until", "max_occurrences", "execution_seconds", "result_seconds"):
            positive(schedule[key])
        if (not schedule["cutover"] < positive(scheduled_at) < schedule["schedule_until"]
                or schedule["execution_seconds"] > schedule["result_seconds"]
                or execution_deadline != min(scheduled_at+schedule["execution_seconds"], schedule["schedule_until"])
                or result_deadline != min(scheduled_at+schedule["result_seconds"], schedule["schedule_until"])
                or result_deadline <= now):
            raise MigrationDenied("occurrence_outside_frozen_budget")
        value = self.ports.occurrence(cursor, parent, run_id=run_id, scheduled_at=scheduled_at, job_id=job_id, now=now)
        expected = {"run_id": run_id, "scheduled_at": scheduled_at, "job_id": job_id,
                    "expected_kind": expected_kind, "schedule_digest": digest(schedule)}
        if value != expected:
            raise MigrationDenied("occurrence_domain_proof_required")
        occurrence_key = encoded([schedule["version"], scheduled_at])
        existing = cursor.execute("SELECT * FROM migration_items WHERE parent_item_id=? AND occurrence_key=?",
                                  (parent.item_id, occurrence_key)).fetchone()
        if existing is not None:
            if existing["job_id"] != job_id or existing["source_ref"] != "run:"+str(run_id):
                raise MigrationDenied("occurrence_already_bound")
            return self.issue_item(cursor, item_id=existing["item_id"], operation="activate", now=now)
        count = cursor.execute("SELECT COUNT(*) FROM migration_items WHERE parent_item_id=?", (parent.item_id,)).fetchone()[0]
        if count >= schedule["max_occurrences"]:
            raise MigrationDenied("schedule_occurrence_budget_exhausted")
        row = cursor.execute("SELECT * FROM durable_jobs WHERE job_id=? FOR UPDATE", (positive(job_id),)).fetchone()
        if (row is None or row["kind"] != expected_kind or row["status"] != "pending" or row["attempts"] != 0
                or row["next_attempt_at"] != PARKED_AT or row["scope_key"] != parent.target_endpoint.scope.key):
            raise MigrationDenied("occurrence_job_not_parked")
        item_id = secrets.token_hex(16)
        data = {"parent_item_id": parent.item_id, "parent_export_digest": parent.export_digest,
                "run_id": run_id, "scheduled_at": scheduled_at, "schedule": schedule}
        cursor.execute("""INSERT INTO migration_items(item_id,migration_id,parent_item_id,occurrence_key,
            kind,source_ref,source_revision,classification,export_json,export_digest,source_actor_key,
            target_actor_key,mapping_id,mapping_revision,mapping_expires_at,execution_deadline,result_deadline,
            state,job_id,expected_job_kind,created_at)
            SELECT ?,migration_id,item_id,?,'routine',?,source_revision,'unsent',?,?,source_actor_key,
            target_actor_key,mapping_id,mapping_revision,mapping_expires_at,?,?,'prepared',?,?,?
            FROM migration_items WHERE item_id=?""",
            (item_id, occurrence_key, "run:"+str(run_id), encoded(data), digest(data), execution_deadline,
             result_deadline, job_id, expected_kind, now, parent.item_id))
        return self.issue_item(cursor, item_id=item_id, operation="activate", now=now)

    def bind_schedule(self, cursor, proof, *, target_routine_id, now):
        require_issued(proof, MigrationItemProof)
        if proof.issuance_transaction_id != cursor.execute("SELECT txid_current()").fetchone()[0]:
            raise MigrationDenied("conversion_requires_same_transaction")
        current = self.require_item(cursor, proof, operation="bind", now=now)
        if current.schedule_json is None or current.job_id is not None:
            raise MigrationDenied("frozen_schedule_required")
        if current.target_routine_id not in {None, target_routine_id}:
            raise MigrationDenied("schedule_already_bound")
        if self.ports.bound_schedule(cursor, current, target_routine_id, operation="prepare", now=now) is not None:
            raise MigrationDenied("schedule_binding_unavailable")
        cursor.execute("UPDATE migration_items SET target_routine_id=?,state='prepared' WHERE item_id=?",
                       (positive(target_routine_id), proof.item_id))

    def activate_schedule(self, cursor, proof, *, now):
        current = self.require_item(cursor, proof, operation="activate", now=now)
        if current.schedule_json is None or current.target_routine_id is None or current.job_id is not None:
            raise MigrationDenied("frozen_schedule_required")
        if self.ports.bound_schedule(cursor, current, current.target_routine_id, operation="activate", now=now) is not None:
            raise MigrationDenied("schedule_binding_unavailable")
        cursor.execute("UPDATE migration_items SET state='active' WHERE item_id=?", (proof.item_id,))
        return current.target_routine_id

    def expire_item(self, cursor, proof, *, now):
        current = self.require_item(cursor, proof, operation="expire", now=now)
        if current.job_id is not None:
            job = cursor.execute("SELECT * FROM durable_jobs WHERE job_id=? FOR UPDATE", (current.job_id,)).fetchone()
            if job is None or job["scope_key"] != current.target_endpoint.scope.key or job["status"] == "running":
                raise MigrationDenied("active_job_expiry_requires_owner")
            if self.ports.bound_job(cursor, current, job, operation="expire", now=now) is not None:
                raise MigrationDenied("migration_job_binding_unavailable")
            cursor.execute("""UPDATE durable_jobs SET status='cancelled',finished_at=?,updated_at=?,
                last_error='migration_deadline_expired' WHERE job_id=? AND status='pending'""", (now, now, current.job_id))
        elif current.target_routine_id is not None:
            if self.ports.bound_schedule(cursor, current, current.target_routine_id, operation="expire", now=now) is not None:
                raise MigrationDenied("schedule_binding_unavailable")
        else:
            raise MigrationDenied("migration_target_binding_missing")
        cursor.execute("UPDATE migration_items SET state='expired' WHERE item_id=?", (proof.item_id,))
