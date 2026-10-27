from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import nonebot

nonebot.init()

from tests.postgres_fixture import postgres_for_test
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.quota import UsageStore
from src.plugins.ai_chat.routines import RoutineLeaseLost, RoutineStore, cron_time
from src.plugins.ai_chat.storage.jobs import DurableJobStore


def timestamp(value: str, zone: str = "Asia/Shanghai") -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(zone)).timestamp())


class RoutineCronTests(unittest.TestCase):
    def test_cron_is_strictly_after_and_uses_the_selected_timezone(self):
        now = timestamp("2026-10-27T08:59:59")
        due = cron_time("0 9 * * *", "Asia/Shanghai", now)
        self.assertEqual(due, now + 1)
        self.assertEqual(cron_time("0 9 * * *", "Asia/Shanghai", due), due + 86400)
        self.assertEqual(cron_time("0 9 * * *", "UTC", now), due + 8 * 3600)

    def test_weekday_month_and_leap_day_schedules(self):
        self.assertEqual(
            cron_time("0 9 * * 1", "Asia/Shanghai", timestamp("2026-10-27T08:00")),
            timestamp("2026-11-02T09:00"),
        )
        self.assertEqual(
            cron_time("0 9 29 2 *", "UTC", timestamp("2026-10-27T00:00", "UTC")),
            timestamp("2028-02-29T09:00", "UTC"),
        )

    def test_invalid_or_unbounded_extensions_are_rejected(self):
        for expression, zone in (
            ("@reboot", "UTC"),
            ("* * * * * *", "UTC"),
            ("H 9 * * *", "UTC"),
            ("0 9 * * MON", "UTC"),
            ("0 25 * * *", "UTC"),
            ("0 9 31 2 *", "UTC"),
            ("0 9 * * *", "Not/AZone"),
            ("0 9 * * *; reboot", "UTC"),
        ):
            with (
                self.subTest(expression=expression, zone=zone),
                self.assertRaises(ValueError),
            ):
                cron_time(expression, zone, timestamp("2026-10-27T00:00"))

    def test_dst_preserves_local_nine_oclock(self):
        for start, expected, hours in (
            ("2026-03-28T09:00", "2026-03-29T09:00", 23),
            ("2026-10-24T09:00", "2026-10-25T09:00", 25),
        ):
            with self.subTest(start=start):
                before = timestamp(start, "Europe/London")
                after = cron_time("0 9 * * *", "Europe/London", before)
                self.assertEqual(after, timestamp(expected, "Europe/London"))
                self.assertEqual(after - before, hours * 3600)


class RoutineStoreTests(unittest.TestCase):
    def setUp(self):
        self.db = postgres_for_test(self)
        self.jobs = self.owned(DurableJobStore(self.db, lease_seconds=10))
        self.outbox = self.owned(DeliveryStore(self.db, recover_interrupted=False))
        self.usage = self.owned(UsageStore(self.db))
        self.store = self.new_store()
        self.scope = ConversationScope("onebot-v11", "group", "101", "201", "301")
        self.now = timestamp("2026-10-27T08:59:00")
        self.body = MessageBody((TextNode(0, "An evidence-based daily summary."),))

    def owned(self, value):
        self.addCleanup(value.close)
        return value

    def new_store(self):
        return self.owned(
            RoutineStore(self.db, jobs=self.jobs, outbox=self.outbox, usage=self.usage)
        )

    def create(self, **changes):
        args = dict(
            name="daily",
            prompt="Summarize the recent group discussion.",
            cron="0 9 * * *",
            now=self.now,
        )
        args.update(changes)
        return self.store.set(self.scope, **args)

    def running(self, **changes):
        routine = self.create(**changes)
        run = self.store.materialize_due(now=routine.next_due_at)[0]
        job = self.jobs.claim_due("worker-a", now=routine.next_due_at)[0]
        return routine, run, job

    def budget(self, routine, now=None):
        return self.usage.routine_status(
            routine.routine_id,
            scope_key=self.scope.key,
            bot_id="301",
            creator_id="201",
            now=self.now if now is None else now,
        )

    def test_budget_exhaustion_notice_is_once_per_accounting_day_not_each_cron_slot(self):
        routine, run, job = self.running(cron="* * * * *", max_tokens=2000, daily_tokens=2000)
        now = routine.next_due_at
        self.store.begin_attempt(job, now=now)
        self.jobs.defer(job, delay_seconds=0, now=now)
        replacement = self.jobs.claim_due("worker-b", now=now)[0]
        self.assertEqual(self.store.begin_attempt(replacement, now=now).status, "skipped")
        self.assertEqual(self.store.queue_terminal_notices(now=now), 1)
        first = self.store.get_run(run.run_id)
        self.jobs.mark_succeeded(replacement.job_id, "worker-b", result={}, now=now)
        next_run = self.store.materialize_due(now=now + 60)[0]
        next_job = self.jobs.claim_due("worker-c", now=now + 60)[0]
        self.assertEqual(self.store.begin_attempt(next_job, now=now + 60).status, "skipped")
        self.assertEqual(self.store.queue_terminal_notices(now=now + 60), 0)
        self.assertEqual(self.store.get_run(next_run.run_id).delivery_id, first.delivery_id)
        self.assertEqual(len(self.outbox.recent()), 1)

    def test_deadline_survives_reopen_and_does_not_reserve_again_after_expiry(self):
        routine, run, job = self.running()
        now = routine.next_due_at
        first = self.store.begin_attempt(job, timeout_seconds=30, now=now)
        self.assertEqual(first.deadline_at, now + 30)
        self.jobs.recover_expired_leases(now=now + 10)
        replacement = self.jobs.claim_due("worker-b", now=now + 10)[0]
        restarted = self.new_store()
        second = restarted.begin_attempt(replacement, timeout_seconds=3000, now=now + 10)
        self.assertEqual(second.deadline_at, first.deadline_at)
        self.jobs.recover_expired_leases(now=now + 30)
        expired_job = self.jobs.claim_due("worker-c", now=now + 30)[0]
        expired = restarted.begin_attempt(expired_job, now=now + 30)
        self.assertEqual(expired.status, "failed")
        self.assertEqual(self.budget(routine, now=now)["attempts"], 2)
        self.assertEqual(restarted.queue_terminal_notices(now=now + 30), 1)
        self.assertEqual(restarted.queue_terminal_notices(now=now + 30), 0)
        self.assertIn("总时限", str(self.outbox.recent()[0].body))

    def test_completed_result_can_still_be_delivered_after_execution_deadline(self):
        routine, run, job = self.running()
        now = routine.next_due_at
        started = self.store.begin_attempt(job, timeout_seconds=5, now=now)
        self.store.save_result(job, started.attempt_id, self.body, now=now + 1)
        self.jobs.recover_expired_leases(now=now + 10)
        replacement = self.jobs.claim_due("worker-b", now=now + 10)[0]
        self.assertEqual(self.store.begin_attempt(replacement, now=now + 10).status, "generated")
        delivery_id = self.store.queue_result(replacement, now=now + 10)
        self.assertEqual(self.outbox.get(delivery_id).body, self.body)

    def test_ownership_group_bot_and_private_isolation(self):
        routine = self.create()
        for scope in (
            self.scope.with_actor("202"),
            replace(self.scope, native_conversation_id="102"),
            replace(self.scope, bot_native_user_id="302"),
        ):
            with self.subTest(scope=scope):
                self.assertEqual(self.store.list_owned(scope), [])
                self.assertFalse(
                    self.store.cancel(scope, routine.routine_id, expected_version=1)
                )
        private = ConversationScope("onebot-v11", "private", "201", "202", "301")
        with self.assertRaises(ValueError):
            self.store.set(private, name="bad", prompt="bad", cron="* * * * *")
        with self.assertRaises(ValueError):
            self.store.list_owned(replace(self.scope, bot_native_user_id=""))
        self.assertEqual(self.store.list_owned(self.scope)[0].scope, self.scope)

    def test_create_is_idempotent_and_changes_require_matching_version(self):
        first = self.create()
        self.assertEqual(self.create(now=self.now + 86400), first)
        with self.assertRaises(ValueError):
            self.create(prompt="Changed")
        updated = self.create(prompt="Changed", expected_version=1)
        self.assertEqual(updated.version, 2)
        with self.assertRaises(ValueError):
            self.create(prompt="Changed", expected_version=1)
        with self.assertRaises(ValueError):
            self.store.cancel(self.scope, updated.routine_id, expected_version=1)
        self.assertTrue(
            self.store.cancel(self.scope, updated.routine_id, expected_version=2)
        )
        self.assertEqual(self.store.list_owned(self.scope, include_disabled=False), [])

    def test_concurrent_creates_obey_owner_limit(self):
        stores = [self.new_store() for _ in range(8)]

        def create(index):
            try:
                stores[index].set(
                    self.scope,
                    name=f"routine-{index}",
                    prompt="Summary",
                    cron="0 9 * * *",
                    now=self.now,
                )
                return True
            except ValueError:
                return False

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(create, range(8)))
        self.assertEqual(sum(results), 5)
        self.assertEqual(len(self.store.list_owned(self.scope)), 5)

    def test_cron_coalesces_missed_runs_without_overlapping(self):
        routine = self.create(cron="* * * * *")
        late = routine.next_due_at + 6 * 3600 + 17
        runs = self.store.materialize_due(now=late)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].scheduled_at, late - 17)
        self.assertEqual(runs[0].missed_from, routine.next_due_at)
        self.assertEqual(self.store.materialize_due(now=late + 300), [])
        self.assertEqual(self.jobs.stats().get("pending"), 1)

    def test_concurrent_schedulers_only_create_one_job(self):
        routine = self.create()
        stores = [self.new_store() for _ in range(6)]
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(
                executor.map(
                    lambda store: store.materialize_due(now=routine.next_due_at), stores
                )
            )
        self.assertEqual(sum(len(items) for items in results), 1)
        self.assertEqual(self.jobs.stats().get("pending"), 1)

    def test_enqueue_error_rolls_back_run_schedule_and_job(self):
        routine = self.create()
        enqueue = self.jobs.enqueue

        def fail_after_insert(**kwargs):
            enqueue(**kwargs)
            raise RuntimeError("lost commit")

        with patch.object(self.jobs, "enqueue", side_effect=fail_after_insert):
            with self.assertRaises(RuntimeError):
                self.store.materialize_due(now=routine.next_due_at)
        self.assertEqual(
            self.store.list_owned(self.scope)[0].next_due_at, routine.next_due_at
        )
        self.assertEqual(sum(self.jobs.stats().values()), 0)
        self.assertEqual(len(self.store.materialize_due(now=routine.next_due_at)), 1)

    def test_restart_recovers_result_without_another_budget_reservation(self):
        routine, run, job = self.running()
        now = routine.next_due_at
        attempt = self.store.begin_attempt(job, now=now)
        self.store.save_result(job, attempt.attempt_id, self.body, now=now)
        restarted = self.new_store()
        self.jobs.recover_expired_leases(now=now + 10)
        replacement = self.jobs.claim_due("worker-b", now=now + 10)[0]
        recovered = restarted.begin_attempt(replacement, now=now + 10)
        self.assertEqual(recovered.status, "generated")
        self.assertEqual(self.budget(routine)["attempts"], 1)
        with self.assertRaises(RoutineLeaseLost):
            self.store.queue_result(job, now=now + 10)
        delivery = restarted.queue_result(replacement, now=now + 10)
        self.assertEqual(restarted.queue_result(replacement, now=now + 10), delivery)
        self.assertEqual(restarted.get_run(run.run_id).status, "enqueued")
        self.assertEqual(self.outbox.get(delivery).target_native_conversation_id, "101")

    def test_outbox_error_rolls_back_both_records(self):
        routine, run, job = self.running()
        now = routine.next_due_at
        attempt = self.store.begin_attempt(job, now=now)
        self.store.save_result(job, attempt.attempt_id, self.body, now=now)
        enqueue = self.outbox.enqueue

        def fail_after_insert(**kwargs):
            enqueue(**kwargs)
            raise RuntimeError("delivery transaction failed")

        with patch.object(self.outbox, "enqueue", side_effect=fail_after_insert):
            with self.assertRaises(RuntimeError):
                self.store.queue_result(job, now=now)
        self.assertEqual(self.store.get_run(run.run_id).status, "generated")
        self.assertIsNone(self.store.get_run(run.run_id).delivery_id)
        self.assertEqual(sum(self.outbox.stats().values()), 0)
        self.assertIsNotNone(self.store.queue_result(job, now=now))

    def test_cancel_stops_pending_delivery_but_not_other_jobs(self):
        routine, run, job = self.running()
        now = routine.next_due_at
        attempt = self.store.begin_attempt(job, now=now)
        self.store.save_result(job, attempt.attempt_id, self.body, now=now)
        delivery_id = self.store.queue_result(job, now=now)
        other, _ = self.jobs.enqueue(kind="other", idempotency_key="other", now=now)
        self.store.cancel(self.scope, routine.routine_id, expected_version=1, now=now)
        self.assertEqual(self.outbox.get(delivery_id).status, "cancelled")
        self.assertEqual(self.jobs.get(other.job_id).status, "pending")
        self.assertEqual(self.budget(routine)["charged_tokens"], routine.max_tokens)
        with self.assertRaises(RoutineLeaseLost):
            self.store.queue_result(job, now=now)

    def test_update_and_lease_replacement_fence_old_worker(self):
        routine, run, job = self.running()
        now = routine.next_due_at
        first = self.store.begin_attempt(job, now=now)
        self.assertTrue(self.store.owns_attempt(job, first.attempt_id, now=now))
        self.jobs.recover_expired_leases(now=now + 10)
        replacement = self.jobs.claim_due("worker-b", now=now + 10)[0]
        second = self.store.begin_attempt(replacement, now=now + 10)
        self.assertNotEqual(first.attempt_id, second.attempt_id)
        self.assertFalse(self.store.owns_attempt(job, first.attempt_id, now=now + 10))
        with self.assertRaises(RoutineLeaseLost):
            self.store.save_result(job, first.attempt_id, self.body, now=now + 10)
        self.create(prompt="New plan", expected_version=1, now=now + 10)
        with self.assertRaises(RoutineLeaseLost):
            self.store.save_result(
                replacement, second.attempt_id, self.body, now=now + 10
            )
        self.assertEqual(self.store.get_run(run.run_id).status, "cancelled")

    def test_failed_job_reconciliation_unblocks_future_schedule(self):
        routine, run, job = self.running()
        now = routine.next_due_at
        self.store.begin_attempt(job, now=now)
        self.jobs.mark_failed(
            job.job_id,
            job.lease_owner,
            "provider unavailable",
            retryable=False,
            now=now,
        )
        self.assertEqual(self.store.reconcile_terminal_jobs(now=now), 1)
        self.assertEqual(self.store.get_run(run.run_id).status, "failed")
        self.assertEqual(len(self.store.materialize_due(now=now + 86400)), 1)

    def test_reconciliation_requeues_only_the_saved_result(self):
        routine, run, job = self.running()
        now = routine.next_due_at
        attempt = self.store.begin_attempt(job, now=now)
        self.store.save_result(job, attempt.attempt_id, self.body, now=now)
        self.jobs.mark_failed(
            job.job_id, job.lease_owner, "outbox unavailable", retryable=False, now=now
        )
        self.assertEqual(self.store.reconcile_terminal_jobs(now=now), 1)
        recovered_job = self.jobs.claim_due("worker-b", now=now)[0]
        self.assertEqual(
            self.store.begin_attempt(recovered_job, now=now).status, "generated"
        )
        self.store.queue_result(recovered_job, now=now)
        self.assertEqual(self.budget(routine)["attempts"], 1)

    def test_budget_is_separate_for_each_routine_and_cannot_be_refunded_twice(self):
        routine, run, job = self.running(max_tokens=2000, daily_tokens=2000)
        now = routine.next_due_at
        attempt = self.store.begin_attempt(job, now=now)
        self.assertEqual(self.budget(routine)["charged_tokens"], 2000)
        usage_id = self.usage.settle_routine(
            attempt.attempt_id,
            provider="test",
            model="small",
            input_tokens=150,
            output_tokens=50,
            cached_tokens=100,
            usage_known=True,
            now=now,
        )
        self.assertEqual(
            self.usage.settle_routine(
                attempt.attempt_id,
                provider="test",
                model="small",
                usage_known=True,
                now=now,
            ),
            usage_id,
        )
        self.assertEqual(self.budget(routine)["charged_tokens"], 200)
        self.assertEqual(self.usage.status(self.scope.key, now=now).calls, 1)
        other = self.create(name="other", max_tokens=2000, daily_tokens=2000)
        self.assertEqual(self.budget(other)["charged_tokens"], 0)
        self.assertIsNone(
            self.usage.routine_status(
                routine.routine_id,
                scope_key=self.scope.key,
                bot_id="301",
                creator_id="202",
                now=now,
            )
        )

    def test_lost_attempt_retains_budget_and_retry_cannot_exceed_daily_limit(self):
        routine, run, job = self.running(max_tokens=2000, daily_tokens=2000)
        now = routine.next_due_at
        first = self.store.begin_attempt(job, now=now)
        self.jobs.recover_expired_leases(now=now + 10)
        replacement = self.jobs.claim_due("worker-b", now=now + 10)[0]
        self.assertEqual(
            self.store.begin_attempt(replacement, now=now + 10).status, "skipped"
        )
        self.assertIsNone(
            self.usage.settle_routine(
                first.attempt_id,
                provider="test",
                model="small",
                usage_known=False,
                now=now,
            )
        )
        self.assertEqual(self.budget(routine)["charged_tokens"], 2000)
        self.assertEqual(self.usage.status(self.scope.key, now=now).calls, 0)
        self.assertEqual(self.budget(routine, now + 86400)["charged_tokens"], 0)

    def test_budget_reservations_are_idempotent_and_serialize_concurrent_retries(self):
        routine, run, job = self.running(max_tokens=2000, daily_tokens=4000)
        now = routine.next_due_at
        stores = [self.owned(UsageStore(self.db)) for _ in range(8)]
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(
                executor.map(
                    lambda store: store.reserve_routine(
                        run_id=run.run_id, attempt_id="same", now=now
                    ),
                    stores,
                )
            )
        self.assertTrue(all(results))
        self.assertEqual(self.budget(routine)["attempts"], 1)
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(
                executor.map(
                    lambda item: item[1].reserve_routine(
                        run_id=run.run_id, attempt_id=f"retry-{item[0]}", now=now
                    ),
                    enumerate(stores),
                )
            )
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.budget(routine)["charged_tokens"], 4000)

    def test_settlement_rollback_keeps_reservation_and_usage_consistent(self):
        routine, run, job = self.running()
        now = routine.next_due_at
        attempt = self.store.begin_attempt(job, now=now)
        record = self.usage.record

        def failed_record(**kwargs):
            record(**kwargs)
            raise RuntimeError("usage commit failed")

        with patch.object(self.usage, "record", side_effect=failed_record):
            with self.assertRaises(RuntimeError):
                self.usage.settle_routine(
                    attempt.attempt_id,
                    provider="test",
                    model="small",
                    input_tokens=100,
                    usage_known=True,
                    now=now,
                )
        self.assertEqual(self.budget(routine)["charged_tokens"], routine.max_tokens)
        self.assertEqual(self.usage.status(self.scope.key, now=now).calls, 0)

    def test_daily_run_limit_survives_edit_and_cannot_be_bypassed_by_small_usage(self):
        routine, run, job = self.running(cron="* * * * *", daily_runs=1)
        now = routine.next_due_at
        attempt = self.store.begin_attempt(job, now=now)
        self.store.save_result(job, attempt.attempt_id, self.body, now=now)
        self.store.queue_result(job, now=now)
        self.usage.settle_routine(
            attempt.attempt_id,
            provider="test",
            model="small",
            input_tokens=1,
            usage_known=True,
            now=now,
        )
        self.create(
            cron="* * * * *",
            prompt="Changed",
            daily_runs=1,
            expected_version=1,
            now=now,
        )
        self.store.materialize_due(now=now + 60)
        replacement = self.jobs.claim_due("worker-b", now=now + 60)[0]
        self.assertEqual(
            self.store.begin_attempt(replacement, now=now + 60).status, "skipped"
        )
        self.assertEqual(self.budget(routine)["runs"], 1)

    def test_concurrent_settlement_records_usage_exactly_once(self):
        routine, run, job = self.running()
        now = routine.next_due_at
        attempt = self.store.begin_attempt(job, now=now)
        stores = [self.owned(UsageStore(self.db)) for _ in range(6)]
        with ThreadPoolExecutor(max_workers=6) as executor:
            ids = list(
                executor.map(
                    lambda store: store.settle_routine(
                        attempt.attempt_id,
                        provider="test",
                        model="small",
                        input_tokens=100,
                        output_tokens=20,
                        usage_known=True,
                        now=now,
                    ),
                    stores,
                )
            )
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(self.usage.status(self.scope.key, now=now).calls, 1)
        self.assertEqual(self.budget(routine)["charged_tokens"], 120)

    def test_reservation_and_attempt_update_rollback_together(self):
        routine, run, job = self.running()
        now = routine.next_due_at
        reserve = self.usage.reserve_routine

        def fail_after_reserve(**kwargs):
            reserve(**kwargs)
            raise RuntimeError("attempt commit failed")

        with patch.object(
            self.usage, "reserve_routine", side_effect=fail_after_reserve
        ):
            with self.assertRaises(RuntimeError):
                self.store.begin_attempt(job, now=now)
        self.assertEqual(self.store.get_run(run.run_id).status, "queued")
        self.assertEqual(self.budget(routine)["charged_tokens"], 0)
        self.assertEqual(self.store.begin_attempt(job, now=now).status, "running")

    def test_cancel_does_not_rewrite_committed_delivery(self):
        routine, run, job = self.running()
        now = routine.next_due_at
        attempt = self.store.begin_attempt(job, now=now)
        self.store.save_result(job, attempt.attempt_id, self.body, now=now)
        delivery_id = self.store.queue_result(job, now=now)
        self.outbox.claim_due(now=now)
        self.assertTrue(
            self.outbox.mark_committed(
                delivery_id, native_message_id="receipt", now=now
            )
        )
        self.store.cancel(self.scope, routine.routine_id, expected_version=1, now=now)
        self.assertEqual(self.outbox.get(delivery_id).status, "committed")
        self.assertEqual(self.outbox.get(delivery_id).native_message_id, "receipt")


if __name__ == "__main__":
    unittest.main()
