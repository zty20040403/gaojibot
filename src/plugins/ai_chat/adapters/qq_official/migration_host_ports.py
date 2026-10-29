"""Database-backed migration ports and exact M3 source selection.

Host dependencies are explicit. Missing domains deny; a current route alone is
never a replacement for the plan's original source or a user's grant.
"""
from __future__ import annotations

from dataclasses import asdict
from src.bot_storage.database import StoreRow
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from .migration_types import MigrationDenied, MigrationItemProof, VerificationProof, require_issued


class MigrationPlanGuard:
    def __init__(self, *, native, journal, continuations=(), artifact_authority=None):
        self.native, self.journal = native, journal
        self.continuations = tuple(continuations)
        self.artifact_authority = artifact_authority

    def __call__(self, cursor, *, token, plan, operation, now, source_proof=None, **kwargs):
        if (operation not in {'enqueue','send'} or plan is None
                or plan['account_key'] != token.account_key):
            raise MigrationDenied('migration_plan_source_unavailable')
        if operation == 'enqueue' and source_proof is not None:
            from .migration_artifact_journal import MigrationArtifactProof
            if type(source_proof) is MigrationArtifactProof:
                self.journal.require_artifact(cursor,source_proof,now=now)
                self._item_plan(plan,source_proof.item)
                return True
            if type(source_proof) is MigrationItemProof:
                require_issued(source_proof, MigrationItemProof)
                if source_proof.issuance_transaction_id != cursor.execute('SELECT txid_current() AS txid').fetchone()['txid']:
                    raise MigrationDenied('migration_source_transaction_changed')
                row = cursor.execute('SELECT * FROM durable_jobs WHERE job_id=? FOR UPDATE', (source_proof.job_id,)).fetchone()
                if row is None:
                    raise MigrationDenied('migration_job_required')
                self.journal.require_item(cursor,source_proof,operation='send',job=DurableJobStore._row(row),now=now)
                self._item_plan(plan, source_proof)
                return True
            if type(source_proof) is VerificationProof:
                require_issued(source_proof, VerificationProof)
                _, current = self.journal._verification_current(cursor,source_proof.migration_id,source_proof.kind,now)
                if asdict(current) != asdict(source_proof):
                    raise MigrationDenied('verification_proof_stale')
                self.journal.bind_verification_plan(cursor,source_proof,plan_id=plan['plan_id'],now=now)
                return True
            if self.artifact_authority is not None:
                # The dedicated artifact adapter validates its own issued type,
                # immutable artifact, actual file job and exact target binding.
                if self.artifact_authority(cursor, token=token, plan=plan,
                        source_proof=source_proof, now=now) is True:
                    return True
            raise MigrationDenied('unissued_migration_plan_source')
        if cursor.execute('SELECT 1 FROM migration_verification_intents WHERE plan_id=?', (plan['plan_id'],)).fetchone():
            self.journal.current_verification_plan_source(cursor,plan['plan_id'],now=now)
            return True
        if cursor.execute('SELECT 1 FROM migration_artifact_plans WHERE plan_id=?', (plan['plan_id'],)).fetchone():
            self.journal.current_artifact_plan_source(cursor,plan['plan_id'],now=now)
            return True
        for domain in self.continuations:
            if domain.current_plan_source(cursor,plan['plan_id'],now) is True:
                return True
        # Once bound as a migrated item, a missing domain cannot silently fall
        # through to an unrelated native trigger with the same textual ID.
        if cursor.execute('SELECT 1 FROM migration_item_plans WHERE plan_id=?', (plan['plan_id'],)).fetchone():
            raise MigrationDenied('migration_plan_domain_unavailable')
        return self.native.plan(cursor,token=token,plan=plan,operation=operation,now=now,**kwargs)

    @staticmethod
    def _item_plan(plan, proof):
        if (plan['endpoint_key'] != proof.target_endpoint.key
                or plan['requester_actor_key'] != proof.target_actor.key
                or plan['endpoint_generation'] != proof.endpoint_generation
                or plan['route_epoch'] != proof.endpoint_route_epoch
                or plan['source_scope_key'] != proof.target_endpoint.scope.key
                or proof.result_deadline is None or plan['expires_at'] > proof.result_deadline):
            raise MigrationDenied('migration_plan_source_mismatch')


class MigrationHostPorts:
    """Real adapters for the journal; unsupported domains remain explicit deny."""
    def __init__(self, *, control, mappings, sources, baselines, export,
                 domains=(), verification=None, artifacts=None):
        self.authority, self.mappings, self.sources = control, mappings, sources
        self.baselines, self.exporter = dict(baselines), export
        self.domains = tuple(domains)
        self.verifier, self.artifacts = verification, artifacts

    def control(self, cursor, authorization, **kwargs):
        return self.authority.control(cursor,authorization,**kwargs)

    def baseline(self,cursor,source,*,account,now):
        holder = self.baselines.get(account.key)
        if holder is None or holder is not source:
            raise MigrationDenied('migration_baseline_unconfigured')
        return holder.evidence(cursor,source,account=account,now=now)

    def export(self,cursor,*args):
        if self.exporter is None:
            raise MigrationDenied('migration_export_unconfigured')
        return self.exporter(cursor,*args)

    def mapping(self,cursor,*args):
        return self.mappings.lookup(cursor,*args)

    def effect(self,cursor,source,*,source_endpoint,source_scope,endpoint,operation,now):
        self.sources.require(cursor,source,operation=operation,source_endpoint=source_endpoint,source_scope=source_scope)

    def effect_receipt(self,cursor,source,*,activity,now):
        return self.sources.effect_receipt(cursor,source,activity=activity,now=now)

    def _domain(self,item):
        if type(item) is MigrationItemProof:
            require_issued(item,MigrationItemProof)
            kind=item.source_kind
        elif type(item) in (dict,StoreRow):
            kind=item.get('kind')
        else:raise MigrationDenied('migration_domain_source_unavailable')
        matches = [domain for kinds,domain in self.domains if kind in kinds]
        if len(matches) != 1:
            raise MigrationDenied('migration_domain_unconfigured')
        return matches[0]

    def bound_job(self,cursor,item,job,**kwargs):
        return self._domain(item).validate_bound_job(cursor,item,job,**kwargs)

    def bound_plan(self,cursor,item,plan_id,**kwargs):
        return self._domain(item).validate_bound_plan(cursor,item,plan_id,**kwargs)

    def bound_schedule(self,cursor,item,target_routine_id,**kwargs):
        return self._domain(item).validate_bound_schedule(cursor,item,target_routine_id,**kwargs)

    def occurrence(self,cursor,parent,**kwargs):
        return self._domain(parent).validate_occurrence(cursor,parent,**kwargs)

    def verification_request(self,cursor,source,**kwargs):
        if self.verifier is None:
            raise MigrationDenied('verification_host_unconfigured')
        return self.verifier.request(cursor,source,**kwargs)

    def verification_plan(self,cursor,proof,plan_id,**kwargs):
        if self.verifier is None:
            raise MigrationDenied('verification_host_unconfigured')
        return self.verifier.plan(cursor,proof,plan_id,**kwargs)

    def verification(self,cursor,source,**kwargs):
        if self.verifier is None:
            raise MigrationDenied('verification_host_unconfigured')
        return self.verifier.receipt(cursor,source,**kwargs)

    def revoke(self,cursor,*,migration_id,target_endpoint,now):
        cursor.execute("UPDATE qq_migration_public_history_grants SET state='revoked' WHERE migration_id=? AND state<>'revoked'", (migration_id,))
        # Existing jobs/plans stay historical evidence. Their source guards
        # reject the revoked journal/epoch before every new effect.

    def existing_artifact(self,cursor,source,**kwargs):
        if self.artifacts is None:
            raise MigrationDenied('migration_artifact_host_unconfigured')
        return self.artifacts.existing_artifact(cursor,source,**kwargs)

    def bound_artifact(self,cursor,item,artifact_id,**kwargs):
        if self.artifacts is None:
            raise MigrationDenied('migration_artifact_host_unconfigured')
        return self.artifacts.validate_bound_artifact(cursor,item,artifact_id,**kwargs)
