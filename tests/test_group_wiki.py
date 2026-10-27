from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import unittest

import nonebot

nonebot.init()

from src.bot_storage import DatabaseError
from src.plugins.ai_chat.context_store import ContextStore
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.group_wiki import GroupWikiStore, WikiLeaseLost
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode, body_to_json
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from tests.postgres_fixture import postgres_for_test


class GroupWikiFixture:
    def setUp(self):
        self.db = postgres_for_test(self)
        self.ledger = self.owned(MessageLedger(self.db))
        self.context = self.owned(ContextStore(self.db, historian_managed=True))
        self.jobs = self.owned(DurableJobStore(self.db))
        self.store = self.wiki()
        self.scope = ConversationScope("onebot-v11", "group", "100")
        self.other = ConversationScope("onebot-v11", "group", "200")

    def owned(self, store):
        self.addCleanup(store.close)
        return store

    def wiki(self):
        return self.owned(GroupWikiStore(self.db, ledger=self.ledger, context=self.context, jobs=self.jobs))

    def chapter(self, scope=None, *, author="7", text="The group chose Python for its project"):
        scope = scope or self.scope
        native_id = str(self.ledger.visible_message_count_after(scope, 0) + 1)
        message = self.ledger.record_message(scope, native_message_id=native_id,
            sender_native_user_id=author, sender_display="Participant " + author,
            body=MessageBody((TextNode(0, text),)), occurred_at=100)
        candidate = self.context.capture_candidate(self.ledger, scope, settled=True)
        chapter = self.context.publish_generated(candidate, (text, text, text))
        return message, chapter

    def enable(self, scope=None, **overrides):
        settings = dict(bot_id="301", enabled=True, newcomer_enabled=False, expected_version=0, now=100)
        settings.update(overrides)
        return self.store.configure(scope or self.scope, **settings)

    def job(self, slot="2026-10-27"):
        self.enable()
        self.store.enqueue(self.scope, slot=slot, now=100)
        return self.jobs.claim_due("wiki-worker", now=100)[0]

    def entry(self, message, **overrides):
        item = dict(category="decision", title="Project language", content="Use Python",
                    evidence_ids=[message.canonical_message_id])
        item.update(overrides)
        return item


class GroupWikiStoreTests(GroupWikiFixture, unittest.TestCase):
    def test_defaults_off_and_configuration_is_versioned_per_group(self):
        self.assertFalse(self.store.settings(self.scope)["enabled"])
        self.assertFalse(self.store.settings(self.scope)["newcomer_enabled"])
        self.assertIsNone(self.store.enqueue(self.scope, slot="today", now=100))
        self.assertEqual(self.enable()["version"], 1)
        with self.assertRaises(ValueError):
            self.enable()
        self.assertFalse(self.store.settings(self.other)["enabled"])
        self.assertEqual(len(self.store.enabled_groups()), 1)
        with self.assertRaises(ValueError):
            self.enable(ConversationScope("onebot-v11", "private", "100"))

    def test_source_capture_is_scoped_and_preserves_all_three_summary_levels(self):
        message, chapter = self.chapter()
        self.chapter(self.other, text="Other group secret")
        sources = self.store.capture(self.scope)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["message_ids"], [message.canonical_message_id])
        self.assertEqual(sources[0]["source_hash"], chapter.source_hash)
        self.assertEqual(sources[0]["p1"], chapter.summary_p1)
        self.assertEqual(sources[0]["p2"], chapter.summary_p2)
        self.assertEqual(sources[0]["p3"], chapter.summary_p3)
        self.assertEqual(sources[0]["statements"][0]["principal_id"], message.sender_principal_id)
        self.assertEqual(sources[0]["statements"][0]["text"], message.prompt_text)

    def test_concurrent_schedule_uses_one_durable_snapshot_and_job(self):
        self.chapter()
        self.enable()
        stores = (self.store, self.wiki())
        with ThreadPoolExecutor(max_workers=6) as executor:
            jobs = list(executor.map(lambda i: stores[i % 2].enqueue(self.scope, slot="today", now=100), range(12)))
        self.assertEqual(len({job.job_id for job in jobs}), 1)
        self.assertEqual(len(self.jobs.claim_due("worker", now=100)), 1)

    def test_publication_receipt_survives_takeover_and_search_is_scoped(self):
        message, _ = self.chapter()
        job = self.job()
        receipt = self.store.publish(job, [self.entry(message)], now=100)
        self.assertEqual(receipt["revision"], 1)
        self.assertEqual(self.store.page(self.scope, query="python")["items"][0]["title"], "Project language")
        self.assertEqual(self.store.page(self.scope, query="unrelated")["items"], [])
        self.assertEqual(self.store.page(self.other)["items"], [])
        self.assertTrue(self.jobs.defer(job, delay_seconds=0, now=100))
        replacement = self.jobs.claim_due("replacement", now=100)[0]
        restarted = self.wiki()
        self.assertEqual(restarted.execution(replacement, now=100)["receipt"], receipt)
        self.assertEqual(restarted.publish(replacement, [], now=100), receipt)
        self.assertEqual(restarted.page(self.scope)["revision"], 1)
        self.assertIsNone(restarted.enqueue(self.scope, slot="next-day", now=200))

    def test_missing_or_foreign_evidence_never_creates_a_page(self):
        message, _ = self.chapter()
        foreign, _ = self.chapter(self.other)
        job = self.job()
        for evidence in ([], [foreign.canonical_message_id], [True], [999999]):
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                self.store.publish(job, [self.entry(message, evidence_ids=evidence)], now=100)
        self.assertEqual(self.store.page(self.scope)["revision"], 0)

    def test_expertise_must_be_backed_by_the_subjects_own_messages(self):
        message, _ = self.chapter(text="I maintain the group's Python project")
        other, _ = self.chapter(author="8", text="I maintain the database")
        job = self.job()
        with self.assertRaises(ValueError):
            self.store.publish(job, [self.entry(message, category="expertise",
                               subject_principal_id=other.sender_principal_id)], now=100)
        self.store.publish(job, [self.entry(message, category="expertise",
                           subject_principal_id=message.sender_principal_id)], now=100)
        self.assertEqual(self.store.page(self.scope)["items"][0]["subject_principal_id"], message.sender_principal_id)

    def test_history_cleared_before_generation_rejects_publication(self):
        message, _ = self.chapter()
        job = self.job()
        self.ledger.hide_history(self.scope)
        with self.assertRaises(ValueError):
            self.store.publish(job, [self.entry(message)], now=100)
        self.assertEqual(self.store.page(self.scope)["items"], [])

    def test_history_cleared_after_publication_hides_derived_entries_immediately(self):
        message, _ = self.chapter()
        self.store.publish(self.job(), [self.entry(message)], now=100)
        self.ledger.hide_history(self.scope)
        page = self.store.page(self.scope)
        self.assertEqual(page["items"], [])
        self.assertEqual(page["stale_entries"], 1)

    def test_changed_chapter_or_raw_evidence_is_not_silently_reused(self):
        for change in ("chapter", "message"):
            with self.subTest(change=change):
                postgres_for_test(self, reset=True)
                message, chapter = self.chapter()
                job = self.job()
                with self.store._transaction() as cursor:
                    if change == "chapter":
                        cursor.execute("UPDATE context_compartments SET summary_p2='changed' WHERE compartment_id=?",
                                       (chapter.compartment_id,))
                    else:
                        cursor.execute("UPDATE messages SET body_json=? WHERE canonical_message_id=?",
                                       (body_to_json(MessageBody((TextNode(0, "changed"),))), message.canonical_message_id))
                with self.assertRaises(ValueError):
                    self.store.publish(job, [self.entry(message)], now=100)

    def test_search_cache_changes_do_not_override_canonical_message_evidence(self):
        message, _ = self.chapter()
        job = self.job()
        with self.store._transaction() as cursor:
            cursor.execute("UPDATE messages SET rendered_text='stale search cache' WHERE canonical_message_id=?",
                           (message.canonical_message_id,))
        self.store.publish(job, [self.entry(message)], now=100)
        self.assertEqual(len(self.store.page(self.scope)["items"]), 1)

    def test_obvious_credentials_are_redacted_before_generation_and_rejected_in_output(self):
        message, _ = self.chapter(text="password: example-private-value")
        job = self.job()
        self.assertNotIn("example-private-value", str(job.payload["sources"]))
        self.assertIn("[REDACTED]", str(job.payload["sources"]))
        with self.assertRaisesRegex(ValueError, "credential"):
            self.store.publish(job, [self.entry(message, content="password: example-private-value")], now=100)

    def test_disabled_wiki_cancels_jobs_and_hides_existing_page(self):
        message, _ = self.chapter()
        job = self.job()
        self.store.publish(job, [self.entry(message)], now=100)
        self.enable(enabled=False, expected_version=1)
        self.assertEqual(self.store.page(self.scope)["items"], [])
        with self.assertRaises(WikiLeaseLost):
            self.store.execution(job, now=100)

    def test_old_generation_cannot_replace_a_newer_page(self):
        first, _ = self.chapter()
        old_job = self.job()
        second, _ = self.chapter(text="The group now chose Rust")
        self.store.enqueue(self.scope, slot="next-day", now=100)
        new_job = self.jobs.claim_due("new-worker", now=100)[0]
        self.store.publish(new_job, [self.entry(second, content="Use Rust")], now=100)
        with self.assertRaisesRegex(ValueError, "newer wiki page"):
            self.store.publish(old_job, [self.entry(first)], now=100)
        self.assertEqual(self.store.page(self.scope)["items"][0]["content"], "Use Rust")

    def test_raw_evidence_changes_after_publication_hide_derived_content(self):
        message, _ = self.chapter()
        self.store.publish(self.job(), [self.entry(message)], now=100)
        with self.store._transaction() as cursor:
            cursor.execute("UPDATE messages SET body_json=? WHERE canonical_message_id=?",
                           (body_to_json(MessageBody((TextNode(0, "redacted"),))), message.canonical_message_id))
        page = self.store.page(self.scope)
        self.assertEqual(page["items"], [])
        self.assertEqual(page["stale_entries"], 1)

    def test_wrong_scope_payload_and_superseded_lease_are_rejected(self):
        message, _ = self.chapter()
        job = self.job()
        for bad in (replace(job, scope_key=self.other.key), replace(job, kind="other"),
                    replace(job, payload={**job.payload, "bot_id": "999"})):
            with self.assertRaises(WikiLeaseLost):
                self.store.publish(bad, [self.entry(message)], now=100)
        self.jobs.defer(job, delay_seconds=0, now=100)
        self.jobs.claim_due("replacement", now=100)
        with self.assertRaises(WikiLeaseLost):
            self.store.publish(job, [self.entry(message)], now=100)

    def test_publication_receipt_failure_rolls_back_page(self):
        message, _ = self.chapter()
        job = self.job()
        with self.store._transaction() as cursor:
            cursor.execute("ALTER TABLE group_wiki_publications ADD CONSTRAINT test_receipt_failure CHECK (revision < 0)")
        with self.assertRaises(DatabaseError):
            self.store.publish(job, [self.entry(message)], now=100)
        self.assertEqual(self.store.page(self.scope)["revision"], 0)
        with self.store._transaction() as cursor:
            cursor.execute("ALTER TABLE group_wiki_publications DROP CONSTRAINT test_receipt_failure")
        self.assertEqual(self.store.publish(job, [self.entry(message)], now=100)["revision"], 1)

    def test_incremental_generation_preserves_old_entries_and_updates_exact_topics(self):
        first, _ = self.chapter()
        self.store.publish(self.job(), [self.entry(first)], now=100)
        self.jobs.mark_succeeded(1, "wiki-worker", now=100)
        second, _ = self.chapter(text="Release on Friday")
        job = self.store.enqueue(self.scope, slot="next-day", now=100)
        self.assertEqual(len(job.payload["sources"]), 1)
        claimed = self.jobs.claim_due("wiki-worker", now=100)[0]
        self.store.publish(claimed, [self.entry(second, title="Release date", content="Friday")], now=100)
        page = self.store.page(self.scope)
        self.assertEqual({item["title"] for item in page["items"]}, {"Project language", "Release date"})
        third, _ = self.chapter(text="Use Rust instead of Python")
        self.store.enqueue(self.scope, slot="third-day", now=100)
        claimed = self.jobs.claim_due("next-worker", now=100)[0]
        self.store.publish(claimed, [self.entry(third, content="Use Rust")], now=100)
        page = self.store.page(self.scope)
        self.assertEqual(len(page["items"]), 2)
        self.assertEqual(self.store.page(self.scope, query="language")["items"][0]["content"], "Use Rust")

    def test_backlog_is_processed_oldest_first_and_empty_results_are_checkpointed(self):
        chapters = [self.chapter(text=f"Chapter {index}") for index in range(35)]
        first = self.job()
        self.assertEqual(len(first.payload["sources"]), 32)
        self.assertEqual(first.payload["sources"][0]["message_ids"], [chapters[0][0].canonical_message_id])
        self.store.publish(first, [], now=100)
        self.jobs.mark_succeeded(first.job_id, first.lease_owner, now=100)
        second = self.store.enqueue(self.scope, slot="2026-10-27", now=100)
        self.assertNotEqual(first.job_id, second.job_id)
        self.assertEqual(len(second.payload["sources"]), 3)
        claimed = self.jobs.claim_due("wiki-worker", now=100)[0]
        self.store.publish(claimed, [self.entry(chapters[-1][0])], now=100)
        self.assertEqual(self.store.page(self.scope)["source_chapters"], 35)
        self.assertIsNone(self.store.enqueue(self.scope, slot="tomorrow", now=200))

    def test_regenerated_chapter_replaces_only_entries_derived_from_that_chapter(self):
        first, chapter = self.chapter()
        second, _ = self.chapter(text="Release on Friday")
        self.store.publish(self.job(), [self.entry(first), self.entry(second, title="Release date")], now=100)
        with self.store._transaction() as cursor:
            cursor.execute("UPDATE context_compartments SET summary_p2='A corrected summary' WHERE compartment_id=?",
                           (chapter.compartment_id,))
        job = self.store.enqueue(self.scope, slot="corrected", now=100)
        self.assertEqual(len(job.payload["sources"]), 1)
        claimed = self.jobs.claim_due("new-worker", now=100)[0]
        self.store.publish(claimed, [], now=100)
        self.assertEqual([item["title"] for item in self.store.page(self.scope)["items"]], ["Release date"])

    def test_more_than_one_evidence_page_and_cleared_history_do_not_resurrect_old_entries(self):
        chapters = [self.chapter(text=f"Fact {index}") for index in range(105)]
        self.enable()
        for batch in range(4):
            self.store.enqueue(self.scope, slot=f"batch{batch}", now=100)
            job = self.jobs.claim_due("wiki-worker", now=100)[0]
            message = chapters[min(batch * 32 + 31, 104)][0]
            self.store.publish(job, [self.entry(message, title=f"Fact {batch}")], now=100)
            self.jobs.mark_succeeded(job.job_id, job.lease_owner, now=100)
        self.assertEqual(self.store.page(self.scope)["source_chapters"], 105)
        self.assertEqual(len(self.store.page(self.scope)["items"]), 4)
        self.ledger.hide_history(self.scope)
        self.ledger.record_message(self.scope, native_message_id="after-clear", sender_native_user_id="7",
                                  sender_display="Member", body=MessageBody((TextNode(0, "New discussion"),)), occurred_at=200)
        candidate = self.context.capture_candidate(self.ledger, self.scope, settled=True)
        self.context.publish_generated(candidate, ("New", "New", "New"))
        self.store.enqueue(self.scope, slot="fresh", now=200)
        job = self.jobs.claim_due("wiki-worker", now=200)[0]
        self.store.publish(job, [], now=200)
        page = self.store.page(self.scope)
        self.assertEqual(page["source_chapters"], 1)
        self.assertEqual(page["items"], [])


if __name__ == "__main__":
    unittest.main()
