from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading


from src.plugins.ai_chat.adapters.qq_official.migration_journal import Journal
from src.plugins.ai_chat.adapters.qq_official.migration_types import (
    MigrationDenied,
)
from src.plugins.ai_chat.adapters.qq_official.migration_routes import RouteService
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import EndpointKey
from src.plugins.ai_chat.adapters.qq_official.plans import enqueue_plan
from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
from tests import test_qq_official_receipts as receipt_fixture



from tests.qq_official_migration_journal_fixture import JournalFixture

class JournalTests(JournalFixture):
    def test_unmanaged_and_planned_preserve_both_physical_routes_at_epoch_zero(self):
        self.assertEqual("unmanaged", self.route().phase)
        plan = self.plan()
        self.assertEqual(0, plan.route_epoch)
        self.assertEqual("planned", self.route().phase)
        self.assertEqual("planned", self.route(self.endpoint).phase)
        with self.assertRaises(MigrationDenied):
            self.route(self.endpoint, scope=self.source_scope)


    def test_default_ports_and_unreviewed_console_do_not_authorize(self):
        with self.assertRaises(MigrationDenied):
            with self.tx() as cursor:
                RouteService().register_baseline(cursor, account=self.source.account, source=object(), now=self.now)
        plan = self.plan()
        with self.assertRaises(MigrationDenied):
            with self.tx() as cursor:
                self.migration.freeze(cursor, migration_id=plan.migration_id, reviewed_digest=plan.preview_digest,
                                      authorization=self.authorization, now=self.now)


    def test_actual_console_session_version_and_baseline_session_are_live(self):
        plan = self.plan()
        self.query("UPDATE admin_accounts SET version=2")
        with self.assertRaises(MigrationDenied):
            self.freeze(plan)
        self.query("UPDATE admin_accounts SET version=1")
        self.source_connection.close()
        with self.assertRaises(MigrationDenied):
            self.freeze(plan)


    def test_changed_preview_and_mapping_cannot_freeze_old_digest(self):
        plan = self.plan()
        self.ports.export_revision = 2
        with self.assertRaisesRegex(MigrationDenied, "preview_changed"):
            self.freeze(plan)
        self.ports.export_revision = 1
        self.rotate_mapping()
        with self.assertRaisesRegex(MigrationDenied, "preview_changed"):
            self.freeze(plan)
        self.assertEqual("planned", self.route().phase)


    def test_refresh_retains_old_preview_and_needs_new_exact_review(self):
        plan = self.plan()
        self.ports.approved.add(plan.preview_digest)
        self.ports.export_revision = 2
        with self.tx() as cursor:
            refreshed = self.migration.refresh_plan(cursor, migration_id=plan.migration_id,
                                                    authorization=self.authorization, now=self.now)
        self.assertNotEqual(plan.preview_digest, refreshed.preview_digest)
        self.assertEqual(2, self.query("SELECT COUNT(*) FROM migration_preview_revisions")[0][0])
        with self.assertRaises(MigrationDenied):
            with self.tx() as cursor:
                self.migration.freeze(cursor, migration_id=plan.migration_id, reviewed_digest=plan.preview_digest,
                                      authorization=self.authorization, now=self.now)
        self.assertEqual(1, self.freeze(refreshed))


    def test_freeze_advances_epoch_and_denies_source_and_target_new_work(self):
        plan = self.plan()
        self.assertEqual(1, self.freeze(plan))
        for endpoint in (self.source, self.endpoint):
            for epoch in (0, 1):
                with self.assertRaises(MigrationDenied):
                    self.route(endpoint, epoch)
        with self.assertRaises(MigrationDenied):
            self.dispatch()


    def test_preplan_activity_blocks_switch_until_explicit_quarantine(self):
        proof = self.dispatch()
        plan = self.plan()
        self.freeze(plan)
        with self.tx() as cursor:
            self.migration.begin_draining(cursor, migration_id=plan.migration_id, authorization=self.authorization, now=self.now)
        with self.assertRaisesRegex(MigrationDenied, "dispatch_unresolved"):
            with self.tx() as cursor:
                self.migration.switch_target(cursor, migration_id=plan.migration_id, authorization=self.authorization, now=self.now)
        with self.tx() as cursor:
            self.migration.quarantine_activity(cursor, migration_id=plan.migration_id, activity_id=proof.activity_id,
                                               authorization=self.authorization, now=self.now)
            self.assertEqual(2, self.migration.switch_target(cursor, migration_id=plan.migration_id,
                                                            authorization=self.authorization, now=self.now))
        self.assertEqual("quarantined", self.query("SELECT state FROM migration_dispatch_activities")[0][0])


    def test_late_receipt_after_freeze_session_loss_resolves_original_only(self):
        proof = self.dispatch()
        plan = self.plan()
        self.freeze(plan)
        self.source_connection.close()
        with self.tx() as cursor:
            self.routes.observe_dispatch(cursor, proof=proof, status="accepted", native_id="ACTUAL_OLD_RECEIPT", now=self.now+1)
        row = self.query("SELECT * FROM migration_dispatch_activities")[0]
        self.assertEqual("accepted", row["state"])
        self.assertEqual(self.source.key, row["target_endpoint_key"])
        self.assertEqual(0, self.query("SELECT COUNT(*) FROM durable_jobs")[0][0])


    def test_duplicate_dispatch_does_not_resend_and_conflicting_receipts_stay_unknown(self):
        proof = self.dispatch()
        with self.assertRaisesRegex(MigrationDenied, "already_admitted"):
            self.dispatch()
        for native in ("FIRST", "SECOND", "SECOND"):
            with self.tx() as cursor:
                self.routes.observe_dispatch(cursor, proof=proof, status="accepted", native_id=native, now=self.now+1)
        self.assertEqual("unknown", self.query("SELECT state FROM migration_dispatch_activities")[0][0])
        self.assertEqual(2, self.query("SELECT COUNT(*) FROM migration_activity_observations")[0][0])


    def test_secondary_private_target_does_not_escape_frozen_source(self):
        private = EndpointKey(self.source.account, "private", "42")
        self.dispatch(endpoint=private)
        plan = self.plan()
        self.freeze(plan)
        with self.assertRaises(MigrationDenied):
            self.dispatch(endpoint=private, key="second")


    def test_concurrent_planners_create_one_route(self):
        barrier = threading.Barrier(2)
        def plan():
            barrier.wait(timeout=2)
            try:
                return self.plan().migration_id
            except MigrationDenied:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: plan(), range(2)))
        self.assertEqual(1, sum(value is not None for value in results))
        self.assertEqual(1, self.query("SELECT COUNT(*) FROM migration_routes")[0][0])


    def test_freeze_and_dispatch_race_has_no_untracked_send(self):
        plan = self.plan()
        barrier = threading.Barrier(2)
        def freeze():
            barrier.wait(timeout=2)
            return self.freeze(plan)
        def send():
            barrier.wait(timeout=2)
            try:
                return self.dispatch()
            except MigrationDenied:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            frozen = pool.submit(freeze)
            sent = pool.submit(send)
            self.assertEqual(1, frozen.result(timeout=3))
            result = sent.result(timeout=3)
        self.assertEqual(int(result is not None), self.query("SELECT COUNT(*) FROM migration_dispatch_activities")[0][0])


    def test_completion_needs_two_actual_applied_m3_receipts(self):
        plan = self.plan()
        self.assertEqual(2, self.switch(plan))
        with self.assertRaises(MigrationDenied):
            self.route(self.endpoint, 2)
        self.assertEqual(2, self.complete(plan))
        self.assertEqual("completed", self.route(self.endpoint, 2).phase)
        self.assertEqual("completed", self.route(self.endpoint, 2, self.source_scope, "read").phase)
        with self.assertRaises(MigrationDenied):
            self.route(self.endpoint, 2, self.source_scope, "ingest")


    def test_verification_proof_only_allows_its_exact_plan_before_deadline(self):
        migration = self.plan()
        self.switch(migration)
        with self.tx() as cursor:
            self.migration.begin_verification(cursor, migration_id=migration.migration_id,
                authorization=self.authorization, now=self.now)
            proof = self.migration.issue_verification(cursor, migration_id=migration.migration_id,
                kind="text", actor=self.actor, source=self.verification_request_source, now=self.now)
            plan = enqueue_plan(cursor, token=self.token,
                rendered=render_plan(MessageBody((TextNode(0, "FIXED VERIFICATION"),)), self.endpoint),
                requester=self.actor, logical_key="verification-test", source_scope_key=self.endpoint.scope.key,
                expires_at=self.now+200, now=self.now, purpose="final", admission=lambda *_: True)
            self.verification_plans[plan.plan_id] = (proof.migration_id, proof.kind, proof.request_id)
            self.migration.bind_verification_plan(cursor, proof, plan_id=plan.plan_id, now=self.now)
            current = self.migration.current_verification_plan_source(cursor, plan.plan_id, now=self.now)
        self.assertEqual(proof.request_id, current.request_id)
        with self.assertRaises(MigrationDenied):
            self.route(self.endpoint, 2)
        with self.assertRaises(MigrationDenied):
            with self.tx() as cursor:
                self.migration.bind_verification_plan(cursor, replace(proof), plan_id=plan.plan_id, now=self.now)
        with self.assertRaises(MigrationDenied):
            with self.tx() as cursor:
                self.migration.current_verification_plan_source(cursor, plan.plan_id, now=self.now+301)


    def test_already_attempted_plan_cannot_be_relabelled_as_verification(self):
        migration = self.plan()
        self.switch(migration)
        with self.tx() as cursor:
            self.migration.begin_verification(cursor, migration_id=migration.migration_id,
                authorization=self.authorization, now=self.now)
            proof = self.migration.issue_verification(cursor, migration_id=migration.migration_id,
                kind="text", actor=self.actor, source=self.verification_request_source, now=self.now)
        attempted = receipt_fixture.OfficialReceiptTests.attempt(self)
        plan_id = self.query("SELECT plan_id FROM deliveries WHERE delivery_id=?", (attempted.delivery_id,))[0][0]
        self.verification_plans[plan_id] = (proof.migration_id, proof.kind, proof.request_id)
        with self.assertRaisesRegex(MigrationDenied, "already_attempted"):
            with self.tx() as cursor:
                self.migration.bind_verification_plan(cursor, proof, plan_id=plan_id, now=self.now)


    def test_parked_job_does_not_claim_before_completed_then_exact_current_job(self):
        plan = self.plan()
        self.switch(plan)
        proof, job = self.prepare_job(plan)
        self.assertEqual([], self.jobs.claim_due("worker", now=self.now+10, kinds=("migration.test",)))
        with self.assertRaises(MigrationDenied):
            with self.tx() as cursor:
                self.migration.activate_item(cursor, proof, due_at=self.now+10, now=self.now)
        self.complete(plan)
        with self.tx() as cursor:
            self.migration.activate_item(cursor, proof, due_at=self.now+10, now=self.now)
        claimed = self.jobs.claim_due("worker", now=self.now+10, kinds=("migration.test",))[0]
        with self.tx() as cursor:
            issued = self.migration.issue_item(cursor, item_id=proof.item_id, operation="execute", job=claimed, now=self.now+10)
            self.migration.require_item(cursor, issued, operation="execute", job=claimed, now=self.now+10)
        with self.assertRaises(MigrationDenied):
            with self.tx() as cursor:
                self.migration.require_item(cursor, issued, operation="execute",
                                            job=replace(claimed, lease_generation=claimed.lease_generation+1), now=self.now+10)
        with self.assertRaises(MigrationDenied):
            with self.tx() as cursor:
                self.migration.require_item(cursor, replace(issued), operation="execute", job=claimed, now=self.now+10)
        self.assertEqual(job.job_id, claimed.job_id)


    def test_prepared_restart_uses_binding_not_changed_frozen_source(self):
        plan = self.plan()
        self.switch(plan)
        proof, job = self.prepare_job(plan)
        self.ports.classification = "terminal"  # old source frozen/cancelled atomically by host
        with self.tx() as cursor:
            recovered = Journal(self.routes).issue_item(cursor, item_id=proof.item_id, now=self.now)
        self.assertEqual(job.job_id, recovered.job_id)
        self.query("DELETE FROM draft_continuation_bindings")
        with self.assertRaises(MigrationDenied):
            with self.tx() as cursor:
                self.migration.issue_item(cursor, item_id=proof.item_id, now=self.now)


    def test_conversion_proof_must_be_from_same_transaction_and_deadline_not_renewed(self):
        plan = self.plan()
        self.switch(plan)
        item_id = self.query("SELECT item_id FROM migration_items")[0][0]
        with self.tx() as cursor:
            proof = self.migration.issue_item(cursor, item_id=item_id, now=self.now)
        with self.assertRaisesRegex(MigrationDenied, "same_transaction"):
            with self.tx() as cursor:
                self.migration.bind_item_job(cursor, proof, job_id=1, expected_kind="migration.test", now=self.now)
        with self.assertRaisesRegex(MigrationDenied, "item_expired"):
            with self.tx() as cursor:
                self.migration.issue_item(cursor, item_id=item_id, now=self.now+301)


    def test_generated_result_can_prepare_after_execution_deadline_before_result_deadline(self):
        plan = self.plan()
        self.switch(plan)
        item_id = self.query("SELECT item_id FROM migration_items")[0][0]
        with self.tx() as cursor:
            proof = self.migration.issue_item(cursor, item_id=item_id, now=self.now+150)
        self.assertEqual(self.now+100, proof.execution_deadline)
        self.assertEqual(self.now+300, proof.result_deadline)


    def test_existing_plan_after_job_success_still_requires_mapping_and_original_route(self):
        migration = self.plan()
        self.switch(migration)
        proof, _ = self.prepare_job(migration)
        self.complete(migration)
        with self.tx() as cursor:
            self.migration.activate_item(cursor, proof, due_at=self.now+10, now=self.now)
        job = self.jobs.claim_due("worker", now=self.now+10, kinds=("migration.test",))[0]
        with self.tx() as cursor:
            current = self.migration.issue_item(cursor, item_id=proof.item_id, operation="send", job=job, now=self.now+10)
            rendered = render_plan(MessageBody((TextNode(0, "MIGRATED RESULT"),)), self.endpoint)
            plan = enqueue_plan(cursor, token=self.token, rendered=rendered, requester=self.actor,
                logical_key="migrated-result:"+proof.item_id, source_scope_key=self.endpoint.scope.key,
                expires_at=self.now+250, now=self.now+10, purpose="background_result", admission=lambda *_: True)
            cursor.execute("UPDATE draft_continuation_bindings SET plan_id=? WHERE item_id=?", (plan.plan_id, proof.item_id))
            self.migration.bind_item_plan(cursor, current, plan_id=plan.plan_id, job=job, now=self.now+10)
        self.assertTrue(self.jobs.mark_succeeded(job.job_id, job.lease_owner, now=self.now+11, lease_generation=job.lease_generation))
        with self.tx() as cursor:
            result = self.migration.current_plan_source(cursor, plan.plan_id, now=self.now+12)
        self.assertEqual(proof.item_id, result.item_id)
        with self.assertRaises(MigrationDenied):
            with self.tx() as cursor:
                self.migration.require_item(cursor, current, operation="send", job=job, now=self.now+12)
        self.rotate_mapping()
        with self.assertRaises(MigrationDenied):
            with self.tx() as cursor:
                self.migration.current_plan_source(cursor, plan.plan_id, now=self.now+12)
        with self.tx() as cursor:
            self.migration.rollback(cursor, migration_id=migration.migration_id, authorization=self.authorization, now=self.now+12)
        with self.assertRaises(MigrationDenied):
            with self.tx() as cursor:
                self.migration.current_plan_source(cursor, plan.plan_id, now=self.now+12)


    def test_unregistered_copy_of_migration_item_cannot_bind_plan(self):
        migration = self.plan()
        self.switch(migration)
        proof, _ = self.prepare_job(migration)
        with self.assertRaises(MigrationDenied):
            with self.tx() as cursor:
                self.migration.bind_item_job(cursor, replace(proof), job_id=1, expected_kind="migration.test", now=self.now)


    def test_expired_parked_item_cancels_without_claim_or_mapping_reauthorization(self):
        migration = self.plan()
        self.switch(migration)
        proof, job = self.prepare_job(migration)
        self.complete(migration)
        self.rotate_mapping()
        with self.tx() as cursor:
            expired = self.migration.issue_item(cursor, item_id=proof.item_id, operation="expire", now=self.now+301)
            self.migration.expire_item(cursor, expired, now=self.now+301)
        current = self.jobs.get(job.job_id)
        self.assertEqual(("cancelled", 0), (current.status, current.attempts))
        self.assertEqual("expired", self.query("SELECT state FROM migration_items WHERE item_id=?", (proof.item_id,))[0][0])


    def test_physical_account_scope_cannot_bypass_managed_legacy_freeze(self):
        plan = self.plan()
        self.freeze(plan)
        with self.assertRaises(MigrationDenied):
            self.route(scope=self.source.scope, epoch=0)


    def test_rollback_revokes_grants_and_old_epochs_while_preserving_receipts(self):
        plan = self.plan()
        self.switch(plan)
        self.complete(plan)
        with self.tx() as cursor:
            self.assertEqual(3, self.migration.rollback(cursor, migration_id=plan.migration_id,
                authorization=self.authorization, now=self.now))
        self.assertFalse(self.query("SELECT active FROM draft_history_marker")[0][0])
        with self.assertRaises(MigrationDenied):
            self.route(self.endpoint, 2)
        with self.tx() as cursor:
            self.assertEqual(4, self.migration.rollback(cursor, migration_id=plan.migration_id,
                authorization=self.authorization, now=self.now, finish=True))
        self.assertEqual("rolled_back", self.route(epoch=4).phase)
        with self.assertRaises(MigrationDenied):
            self.route(epoch=4, scope=self.endpoint.scope, operation="read")
        self.assertEqual(2, self.query("SELECT COUNT(*) FROM delivery_receipts WHERE applied_at IS NOT NULL")[0][0])


    def test_frozen_route_existing_artifact_read_is_separate_from_new_send(self):
        plan = self.plan()
        self.freeze(plan)
        with self.tx() as cursor:
            self.routes.assert_existing_artifact_read(cursor, endpoint=self.endpoint, artifact_ref="exact-artifact",
                                                       source=self.artifact_source, now=self.now)
        with self.assertRaises(MigrationDenied):
            self.route(self.endpoint, 1)
        with self.assertRaises(MigrationDenied):
            with self.tx() as cursor:
                self.routes.assert_existing_artifact_read(cursor, endpoint=self.endpoint,
                    artifact_ref="wrong", source=self.artifact_source, now=self.now)
