"""Real formal storage + isolated-schema draft DDL; no platform/model network."""

from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor
import json
import threading

from src.plugins.ai_chat.adapters.qq_official.migration_history import (
    HistoryUnavailable, activate_public_history, read_public_history,
)
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey


from tests.qq_official_public_history_fixture import PublicHistoryFixture

class PublicHistoryTests(PublicHistoryFixture):
    def test_requires_durable_approval_and_cannot_caller_approve_digest(self):
        preview = self.prepare()
        with self.assertRaises(HistoryUnavailable):
            self.read(preview)
        with self.assertRaisesRegex(HistoryUnavailable, "approval_rejected"):
            with self.tx() as cursor:
                activate_public_history(cursor, guard=self.guard, grant_id=preview.grant_id,
                                        approved_manifest_digest=preview.manifest_digest)
        self.activate(preview)
        self.assertEqual(2, len(self.read(preview).messages))

    def test_original_conversation_ids_authors_and_native_lookup_are_unchanged(self):
        before = tuple(asdict(item) for item in self.ledger.recent_in_scope(self.source))
        preview = self.prepare()
        self.activate(preview)
        view = self.read(preview)
        self.assertEqual(before, tuple(asdict(item) for item in view.messages))
        self.assertNotEqual(view.source_conversation_id, view.target_conversation_id)
        self.assertTrue(all(not item.source_endpoint_key and not item.sender_actor_key for item in view.messages))
        self.assertEqual("42", view.messages[0].sender_native_user_id)
        self.assertEqual(self.first.canonical_message_id,
                         self.ledger.canonical_id_for_native(self.source, "same-native"))
        self.assertEqual(self.target_message.canonical_message_id, self.ledger.canonical_id_for_native(
            self.endpoint.scope, "same-native", endpoint=self.endpoint))
        self.assertEqual(before, tuple(asdict(item) for item in self.ledger.recent_in_scope(self.source)))
        self.assertNotIn("PUBLIC FIRST", repr(view))

    def test_new_source_rows_after_preview_and_after_activation_stay_out(self):
        preview = self.prepare()
        self.source_message("later-one", "AFTER PREVIEW")
        self.activate(preview)
        self.source_message("later-two", "AFTER ACTIVATE")
        self.assertEqual([self.first.canonical_message_id, self.second.canonical_message_id],
                         [item.canonical_message_id for item in self.read(preview).messages])

    def test_source_visibility_floor_and_frozen_floor_both_apply(self):
        self.query("""UPDATE conversation_visibility SET min_canonical_message_id=?
            WHERE conversation_id=(SELECT conversation_id FROM conversations WHERE scope_key=?)""",
                   (self.second.canonical_message_id, self.source.key))
        preview = self.prepare()
        self.activate(preview)
        self.query("""UPDATE conversation_visibility SET min_canonical_message_id=1
            WHERE conversation_id=(SELECT conversation_id FROM conversations WHERE scope_key=?)""",
                   (self.source.key,))
        self.assertEqual([self.second.canonical_message_id],
                         [item.canonical_message_id for item in self.read(preview).messages])
        self.ledger.hide_history(self.source)
        self.assertEqual((), self.read(preview).messages)

    def test_target_clear_does_not_resurrect_source_history(self):
        preview = self.prepare()
        self.activate(preview)
        self.ledger.hide_history(self.endpoint.scope)
        self.assertEqual((), self.read(preview).messages)

    def test_target_clear_with_no_own_rows_is_detected_by_clear_stamp(self):
        self.query("DELETE FROM messages WHERE canonical_message_id=?", (self.target_message.canonical_message_id,))
        preview = self.prepare()
        self.activate(preview)
        self.assertEqual(0, self.ledger.hide_history(self.endpoint.scope))
        self.assertEqual(1, self.ledger.visible_message_floor(self.endpoint.scope))
        self.assertEqual((), self.read(preview).messages)

    def test_cross_app_environment_group_private_and_reverse_direction_rejected(self):
        preview = self.prepare()
        self.activate(preview)
        other_account = AccountKey("qq-official", "TEST_OTHER", "test")
        other_env = AccountKey("qq-official", self.account.app_id, "sandbox")
        candidates = (EndpointKey(other_account, "group", self.endpoint.native_conversation_id),
                      EndpointKey(other_env, "group", self.endpoint.native_conversation_id),
                      EndpointKey(self.account, "group", "OTHER"),
                      EndpointKey(self.account, "private", self.endpoint.native_conversation_id),
                      EndpointKey(AccountKey("onebot-v11", "9", "test"), "group", "700"))
        for endpoint in candidates:
            with self.subTest(endpoint=endpoint.account.platform):
                with self.assertRaises(HistoryUnavailable):
                    self.read(preview, endpoint=endpoint)
        with self.assertRaises(HistoryUnavailable):
            self.read(preview, epoch=8)

    def test_revoked_grant_and_rollback_route_independently_deny(self):
        preview = self.prepare()
        self.activate(preview)
        self.query("UPDATE draft_history_approvals SET phase='rolled_back',epoch=8")
        with self.assertRaises(HistoryUnavailable):
            self.read(preview)
        self.query("UPDATE draft_history_approvals SET phase='completed',epoch=7")
        self.query("UPDATE qq_migration_public_history_grants SET state='revoked'")
        with self.assertRaises(HistoryUnavailable):
            self.read(preview)

    def test_endpoint_generation_and_route_are_separate_from_logical_epoch(self):
        preview = self.prepare()
        self.activate(preview)
        for column in ("generation", "route_epoch"):
            with self.subTest(column=column):
                self.query(f"UPDATE qq_official_endpoints SET {column}=2")
                with self.assertRaises(HistoryUnavailable):
                    self.read(preview)
                self.query(f"UPDATE qq_official_endpoints SET {column}=1")

    def test_actual_account_lease_loss_denies_read(self):
        preview = self.prepare()
        self.activate(preview)
        self.account_lease.close()
        with self.assertRaises(Exception):
            self.read(preview)

    def test_public_note_snapshot_rejects_user_and_foreign_group_memory(self):
        personal = self.note(scope="group:700:user:42", kind="user", text="PERSONAL SECRET")
        foreign = self.note(scope="group:701", text="OTHER GROUP SECRET")
        official_personal = self.note(scope=self.actor.key, kind="user", text="OFFICIAL PERSONAL SECRET")
        for item in (personal, foreign, official_personal):
            with self.assertRaises(HistoryUnavailable):
                self.prepare(notes=(item.id,))
        public = self.note(source_id=self.first.canonical_message_id)
        preview = self.prepare(notes=(public.id,))
        self.activate(preview)
        self.assertEqual((public,), self.read(preview).public_notes)
        stored = self.query("SELECT snapshot_json FROM qq_migration_public_history_snapshots")
        self.assertNotIn("SECRET", repr(stored))

    def test_note_version_update_and_delete_do_not_replace_frozen_snapshot(self):
        note = self.note(source_id=self.first.canonical_message_id)
        preview = self.prepare(notes=(note.id,))
        self.activate(preview)
        self.memory.update(note.id, "NEW SECRET CONTENT", [note.scope_key], expected_version=1)
        self.assertEqual((), self.read(preview).public_notes)
        self.memory.remove(note.id, [note.scope_key])
        self.assertEqual((), self.read(preview).public_notes)
        payload = self.query("SELECT snapshot_json FROM qq_migration_public_history_snapshots")[0][0]
        self.assertIn("PUBLIC NOTE", payload)
        self.assertNotIn("NEW SECRET", payload)

    def test_unanchored_manual_note_drops_on_source_clear(self):
        note = self.note()
        preview = self.prepare(notes=(note.id,))
        self.activate(preview)
        self.assertEqual((note,), self.read(preview).public_notes)
        self.ledger.hide_history(self.source)
        self.assertEqual((), self.read(preview).public_notes)

    def test_note_with_foreign_evidence_id_is_not_public_source(self):
        note = self.note(source_id=self.target_message.canonical_message_id)
        with self.assertRaises(HistoryUnavailable):
            self.prepare(notes=(note.id,))

    def test_chapter_snapshot_validates_original_evidence_and_no_later_mutation(self):
        chapter = self.chapter()
        preview = self.prepare(chapters=(chapter.compartment_id,))
        self.activate(preview)
        self.assertEqual((chapter,), self.read(preview).chapters)
        self.query("UPDATE context_compartments SET summary_p1='AFTER APPROVAL' WHERE compartment_id=?",
                   (chapter.compartment_id,))
        self.assertEqual((), self.read(preview).chapters)
        self.assertNotIn("AFTER APPROVAL", self.query(
            "SELECT snapshot_json FROM qq_migration_public_history_snapshots")[0][0])

    def test_chapter_tombstone_missing_evidence_and_source_clear_filter_snapshot(self):
        chapter = self.chapter()
        preview = self.prepare(chapters=(chapter.compartment_id,))
        self.activate(preview)
        self.query("UPDATE context_compartments SET active=0")
        self.assertEqual((), self.read(preview).chapters)
        self.query("UPDATE context_compartments SET active=1")
        self.query("DELETE FROM messages WHERE canonical_message_id=?", (self.first.canonical_message_id,))
        self.assertEqual((), self.read(preview).chapters)
        self.ledger.hide_history(self.source)
        self.assertEqual((), self.read(preview).chapters)

    def test_mutable_change_between_preview_approval_refuses_activation(self):
        chapter = self.chapter()
        note = self.note()
        preview = self.prepare(chapters=(chapter.compartment_id,), notes=(note.id,))
        self.memory.update(note.id, "CHANGED", [note.scope_key], expected_version=1)
        with self.assertRaisesRegex(HistoryUnavailable, "preview_source_changed"):
            self.activate(preview)
        self.assertEqual("prepared", self.query("SELECT state FROM qq_migration_public_history_grants")[0][0])

    def test_frozen_snapshot_and_grant_tampering_are_detected(self):
        note = self.note()
        preview = self.prepare(notes=(note.id,))
        self.activate(preview)
        self.query("UPDATE qq_migration_public_history_snapshots SET snapshot_json='{}'")
        with self.assertRaisesRegex(HistoryUnavailable, "snapshot_changed"):
            self.read(preview)
        self.query("UPDATE qq_migration_public_history_grants SET source_cutoff=source_cutoff+1")
        with self.assertRaisesRegex(HistoryUnavailable, "snapshot_changed"):
            self.read(preview)

    def test_commands_never_become_public_timeline_or_note_evidence(self):
        secret = self.source_message("secret-command", "/bind SECRET", kind="command")
        note = self.note(source_id=secret.canonical_message_id)
        with self.assertRaises(HistoryUnavailable):
            self.prepare(notes=(note.id,))
        preview = self.prepare()
        self.activate(preview)
        self.assertEqual(2, len(self.read(preview).messages))

    def test_atomic_prepare_failure_leaves_no_partial_grant_or_snapshot(self):
        chapter = self.chapter()
        foreign = self.note(scope="group:701")
        with self.assertRaises(HistoryUnavailable):
            self.prepare(chapters=(chapter.compartment_id,), notes=(foreign.id,))
        self.assertEqual(0, self.query("SELECT COUNT(*) FROM qq_migration_public_history_grants")[0][0])
        self.assertEqual(0, self.query("SELECT COUNT(*) FROM qq_migration_public_history_snapshots")[0][0])

    def test_snapshot_json_is_immutable_data_not_effect_authority(self):
        note = self.note()
        preview = self.prepare(notes=(note.id,))
        self.activate(preview)
        payload = json.loads(self.query("SELECT snapshot_json FROM qq_migration_public_history_snapshots")[0][0])
        self.assertEqual(note.content, payload["content"])
        self.assertFalse(hasattr(self.read(preview), "native_lookup"))
        self.assertFalse(hasattr(self.read(preview), "send"))
        self.assertFalse(hasattr(self.read(preview), "personal_memory"))
        with self.assertRaises(HistoryUnavailable):
            self.read(preview, limit=101)

    def test_false_boolean_guard_cannot_accidentally_authorize(self):
        preview = self.prepare()
        self.activate(preview)
        with self.assertRaisesRegex(HistoryUnavailable, "guard_contract"):
            with self.tx() as cursor:
                read_public_history(cursor, guard=lambda *_: False, grant_id=preview.grant_id,
                                    target_endpoint=self.endpoint, route_epoch=7)

    def test_oversized_chapter_is_rejected_before_decode(self):
        chapter = self.chapter()
        self.query("UPDATE context_compartments SET source_message_ids_json=?",
                   ("[" + ",".join("1" for _ in range(40000)) + "]",))
        with self.assertRaises(HistoryUnavailable):
            self.prepare(chapters=(chapter.compartment_id,))

    def test_clear_serializes_with_visibility_read_and_next_read_is_empty(self):
        preview = self.prepare()
        self.activate(preview)
        started = threading.Event()
        finished = threading.Event()
        other_ledger = MessageLedger(self.database)
        self.addCleanup(other_ledger.close)

        def clear():
            started.set()
            result = other_ledger.hide_history(self.endpoint.scope)
            finished.set()
            return result

        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.tx() as cursor:
                view = read_public_history(cursor, guard=self.guard, grant_id=preview.grant_id,
                                           target_endpoint=self.endpoint, route_epoch=7)
                self.assertEqual(2, len(view.messages))
                future = pool.submit(clear)
                self.assertTrue(started.wait(1))
                self.assertFalse(finished.wait(0.1))
            self.assertEqual(1, future.result(timeout=2))
        self.assertEqual((), self.read(preview).messages)
