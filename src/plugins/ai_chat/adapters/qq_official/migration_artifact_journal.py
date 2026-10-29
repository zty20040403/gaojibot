"""Migration authority for real M6 artifacts; no second queue or fake parent job."""
from dataclasses import dataclass, field
import json

from .migration_types import MigrationDenied, MigrationItemProof, _issue, require_issued
from .file_artifacts import file_job_kind
from .file_authority import _job_current


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False)
class MigrationArtifactProof:
    artifact_id: str
    item: MigrationItemProof
    file_job: object = field(repr=False)
    issuance_transaction_id: int


class MigrationArtifactMethods:
    def current_artifact_source(self, cursor, artifact_id, *, now):
        artifact = cursor.execute('SELECT * FROM qq_official_migration_artifacts WHERE artifact_id=?', (artifact_id,)).fetchone()
        if artifact is None:
            raise MigrationDenied('migration_artifact_source_missing')
        item = self._issue_item(cursor,item_id=artifact['item_id'],operation='artifact',now=now)
        if (item.source_kind not in {'subagent','file','project_task'} or item.source_kind!=artifact['source_kind'] or item.schedule_json is not None
                or item.job_id!=artifact['source_job_id'] or item.target_endpoint.key!=artifact['endpoint_key']
                or item.target_endpoint.account.key!=artifact['account_key'] or item.target_actor.key!=artifact['actor_key']
                or item.route_epoch!=artifact['logical_route_epoch'] or item.mapping_id!=artifact['mapping_id']
                or item.mapping_revision!=artifact['mapping_revision'] or item.result_deadline!=artifact['result_deadline']
                or self.ports.bound_artifact(cursor,item,artifact_id,now=now) is not None):
            raise MigrationDenied('migration_artifact_source_changed')
        return item

    def issue_artifact(self, cursor, *, artifact_id, job, now):
        item = self.current_artifact_source(cursor,artifact_id,now=now)
        delivery=cursor.execute('SELECT * FROM qq_official_file_deliveries WHERE artifact_id=?', (artifact_id,)).fetchone()
        if (delivery is None or delivery['state'] not in {'uploaded','published'}
                or delivery['active_plan_id'] is not None or delivery['credential_ref'] is None):
            raise MigrationDenied('migration_artifact_not_ready')
        _job_current(cursor,job,job_id=delivery['job_id'],kind=file_job_kind(item.target_endpoint.account),
                     payload={'artifact_id':artifact_id},now=now)
        if job.job_id==item.job_id:
            raise MigrationDenied('migration_artifact_requires_file_job')
        return _issue(MigrationArtifactProof,artifact_id=artifact_id,item=item,file_job=job,
            issuance_transaction_id=cursor.execute('SELECT txid_current()').fetchone()[0])

    def require_artifact(self, cursor, proof, *, now):
        require_issued(proof,MigrationArtifactProof)
        if proof.issuance_transaction_id!=cursor.execute('SELECT txid_current()').fetchone()[0]:
            raise MigrationDenied('artifact_handoff_requires_same_transaction')
        item=self.current_artifact_source(cursor,proof.artifact_id,now=now)
        if (item.item_id,item.export_digest,item.target_endpoint,item.target_actor,item.mapping_id,item.mapping_revision,
                item.route_epoch,item.endpoint_generation,item.endpoint_route_epoch,item.job_id,item.result_deadline)!= (
                proof.item.item_id,proof.item.export_digest,proof.item.target_endpoint,proof.item.target_actor,
                proof.item.mapping_id,proof.item.mapping_revision,proof.item.route_epoch,proof.item.endpoint_generation,
                proof.item.endpoint_route_epoch,proof.item.job_id,proof.item.result_deadline):
            raise MigrationDenied('migration_artifact_proof_stale')
        delivery=cursor.execute('SELECT * FROM qq_official_file_deliveries WHERE artifact_id=?', (proof.artifact_id,)).fetchone()
        if delivery is None or delivery['state'] not in {'uploaded','published','handed_off'}:
            raise MigrationDenied('migration_artifact_not_ready')
        _job_current(cursor,proof.file_job,job_id=delivery['job_id'],kind=file_job_kind(item.target_endpoint.account),
                     payload={'artifact_id':proof.artifact_id},now=now)
        return item

    def _artifact_plan(self,cursor,item,artifact_id,plan_id,now):
        artifact=cursor.execute('SELECT * FROM qq_official_file_artifacts WHERE artifact_id=?', (artifact_id,)).fetchone()
        relation=cursor.execute('SELECT * FROM qq_official_file_message_plans WHERE plan_id=? AND artifact_id=?', (plan_id,artifact_id)).fetchone()
        delivery=cursor.execute('SELECT * FROM qq_official_file_deliveries WHERE artifact_id=?', (artifact_id,)).fetchone()
        plan=cursor.execute('SELECT * FROM delivery_plans WHERE plan_id=?', (plan_id,)).fetchone()
        if (artifact is None or relation is None or delivery is None or plan is None
                or delivery['active_plan_id']!=plan_id or delivery['state'] not in {'handed_off','message_accepted'}
                or plan['payload_mode']!='protected' or plan['purpose']!='requested_artifact'
                or plan['account_key']!=item.target_endpoint.account.key or plan['endpoint_key']!=item.target_endpoint.key
                or plan['requester_actor_key']!=item.target_actor.key or plan['source_scope_key']!=item.target_endpoint.scope.key
                or plan['endpoint_generation']!=item.endpoint_generation or plan['route_epoch']!=item.endpoint_route_epoch
                or plan['source_canonical_message_id'] is not None
                or json.loads(plan['manifest_json'])['source_native_message_id']!=''
                or plan['expires_at']>min(item.result_deadline,artifact['retain_until'],artifact['pending_deadline_at'])
                or now>=plan['expires_at']):
            raise MigrationDenied('migration_artifact_plan_mismatch')
        return delivery

    def bind_artifact_plan(self,cursor,proof,*,plan_id,now):
        item=self.require_artifact(cursor,proof,now=now)
        self._artifact_plan(cursor,item,proof.artifact_id,plan_id,now)
        values=(plan_id,proof.artifact_id,item.item_id,item.job_id,proof.file_job.job_id,
            proof.file_job.lease_generation,item.route_epoch,item.mapping_revision,
            item.target_endpoint.account.key,item.target_endpoint.key,item.target_actor.key,now)
        existing=cursor.execute('SELECT * FROM migration_artifact_plans WHERE plan_id=?', (plan_id,)).fetchone()
        if existing is not None:
            if tuple(existing.values())!=values:
                raise MigrationDenied('migration_artifact_plan_changed')
            return
        if cursor.execute('SELECT 1 FROM delivery_attempts a JOIN deliveries d USING(delivery_id) WHERE d.plan_id=?', (plan_id,)).fetchone():
            raise MigrationDenied('migration_artifact_already_attempted')
        cursor.execute('INSERT INTO migration_artifact_plans VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',values)

    def current_artifact_plan_source(self,cursor,plan_id,*,now):
        binding=cursor.execute('SELECT * FROM migration_artifact_plans WHERE plan_id=?', (plan_id,)).fetchone()
        if binding is None:
            raise MigrationDenied('migration_artifact_plan_missing')
        item=self.current_artifact_source(cursor,binding['artifact_id'],now=now)
        if (binding['item_id']!=item.item_id or binding['source_job_id']!=item.job_id
                or binding['logical_route_epoch']!=item.route_epoch or binding['mapping_revision']!=item.mapping_revision):
            raise MigrationDenied('migration_artifact_plan_changed')
        delivery=self._artifact_plan(cursor,item,binding['artifact_id'],plan_id,now)
        if binding['file_job_id']!=delivery['job_id']:
            raise MigrationDenied('migration_artifact_job_changed')
        return item
