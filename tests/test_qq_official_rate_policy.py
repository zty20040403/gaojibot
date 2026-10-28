from __future__ import annotations

import asyncio
from dataclasses import replace
import unittest

from src.plugins.ai_chat.adapters.qq_official.rate_policy import (
    LocalAccountRateGate, MAX_TIME, RatePolicyError, RateWaitExpired,
    VerifiedDayBoundary, build_rate_specs, daily_quota_spec,
)
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey


ACCOUNT = AccountKey("qq-official", "TEST_APP:非数字/+", "test")
GROUP = EndpointKey(ACCOUNT, "group", "TEST_GROUP:原样/+ ")
PRIVATE = EndpointKey(ACCOUNT, "private", GROUP.native_conversation_id)


class PolicyTests(unittest.TestCase):
    def test_exact_scene_specific_proactive_rate_specs(self):
        rows = (
            (GROUP, "certified", [(None, 60, 60), (GROUP.key, 20, 60)]),
            (GROUP, "unverified", [(None, 30, 60), (GROUP.key, 20, 60)]),
            (PRIVATE, "certified", [(None, 10, 1), (PRIVATE.key, 20, 60)]),
            (PRIVATE, "unverified", [(None, 5, 1), (None, 30, 60), (PRIVATE.key, 20, 60)]),
        )
        for endpoint, certification, expected in rows:
            with self.subTest(kind=endpoint.kind, certification=certification):
                specs = build_rate_specs(endpoint, "proactive", certification=certification)
                self.assertEqual([(s.endpoint_key, s.limit_count, s.interval_seconds) for s in specs], expected)
                self.assertTrue(all(s.capacity == 1 and s.send_mode == "proactive" for s in specs))

    def test_unknown_certification_is_conservative_unverified(self):
        for endpoint in (GROUP, PRIVATE):
            self.assertEqual(build_rate_specs(endpoint, "proactive"),
                             build_rate_specs(endpoint, "proactive", certification="unverified"))

    def test_passive_has_no_proactive_rate_or_daily_quota(self):
        for endpoint in (GROUP, PRIVATE):
            self.assertEqual(build_rate_specs(endpoint, "passive"), ())
            self.assertIsNone(daily_quota_spec(endpoint, "passive", now=100))

    def test_full_identity_isolation_no_numeric_cast_or_delimiter_collisions(self):
        endpoints = [GROUP, PRIVATE,
                     EndpointKey(AccountKey("qq-official", ACCOUNT.app_id, "production"), "group", GROUP.native_conversation_id),
                     EndpointKey(AccountKey("qq-official", "TEST_OTHER", "test"), "group", GROUP.native_conversation_id),
                     EndpointKey(ACCOUNT, "group", "TEST_GROUP:原样/+"),
                     EndpointKey(ACCOUNT, "group", 'TEST_\"],null,[OTHER')]
        keys = [build_rate_specs(endpoint, "proactive")[-1].rate_key for endpoint in endpoints]
        quota_keys = [daily_quota_spec(endpoint, "proactive", now=100, unverified_period_start=10).quota_key for endpoint in endpoints]
        self.assertEqual(len(set(keys)), len(endpoints))
        self.assertEqual(len(set(quota_keys)), len(endpoints))
        self.assertEqual(build_rate_specs(GROUP, "proactive")[-1].endpoint_key, GROUP.key)

    def test_account_bucket_shared_across_endpoints_but_not_scenes(self):
        other_group = EndpointKey(ACCOUNT, "group", "TEST_OTHER")
        first = build_rate_specs(GROUP, "proactive")[0]
        second = build_rate_specs(other_group, "proactive")[0]
        self.assertEqual(first.rate_key, second.rate_key)
        self.assertNotEqual(first.rate_key, build_rate_specs(PRIVATE, "proactive")[1].rate_key)

    def test_certification_revision_not_part_of_existing_counter_identity(self):
        unverified = build_rate_specs(GROUP, "proactive")[0]
        certified = build_rate_specs(GROUP, "proactive", certification="certified")[0]
        self.assertEqual(unverified.rate_key, certified.rate_key)
        quota = daily_quota_spec(GROUP, "proactive", now=100, unverified_period_start=10)
        self.assertEqual(quota.quota_key, replace(quota, policy_revision=2, limit_count=500).quota_key)
        self.assertRegex(quota.quota_key, r"^[a-f0-9]{64}$")
        self.assertEqual(quota.policy_revision, 1)

    def test_unknown_day_has_no_midnight_or_restart_reset(self):
        specs = [daily_quota_spec(GROUP, "proactive", now=now, unverified_period_start=123) for now in (123, 86400, 8640000)]
        self.assertEqual(len({s.quota_key for s in specs}), 1)
        self.assertTrue(all(s.period_end_at is None and not s.reset_verified and s.period_start_at == 123 for s in specs))
        self.assertTrue(all(s.limit_count == 1000 and s.release_policy == "not_dispatched_only" for s in specs))

    def test_wrong_new_unknown_anchor_cannot_get_fresh_counter_key(self):
        original = daily_quota_spec(GROUP, "proactive", now=100, unverified_period_start=10)
        accidental_reset = daily_quota_spec(GROUP, "proactive", now=200, unverified_period_start=200)
        self.assertEqual(original.quota_key, accidental_reset.quota_key)
        self.assertNotEqual(original.lookup_guard_tuple, accidental_reset.lookup_guard_tuple)
        with self.assertRaises(RatePolicyError):
            daily_quota_spec(GROUP, "proactive", now=200)

    def test_verified_boundary_explicit_origin_and_period_identity(self):
        boundary = VerifiedDayBoundary(ACCOUNT, 123, "test_provider_day")
        first = daily_quota_spec(PRIVATE, "proactive", now=123 + 86399, verified_boundary=boundary)
        second = daily_quota_spec(PRIVATE, "proactive", now=123 + 86400, verified_boundary=boundary)
        self.assertEqual((first.period_start_at, first.period_end_at), (123, 123 + 86400))
        self.assertEqual(second.period_start_at, first.period_end_at)
        self.assertNotEqual(first.quota_key, second.quota_key)
        self.assertTrue(first.reset_verified)
        self.assertEqual(first.evidence_code, "test_provider_day")

    def test_verified_boundary_account_scope_and_numeric_bounds(self):
        boundary = VerifiedDayBoundary(ACCOUNT, 123, "test_day")
        other = EndpointKey(AccountKey("qq-official", ACCOUNT.app_id, "production"), "group", "TEST_GROUP")
        with self.assertRaises(RatePolicyError):
            daily_quota_spec(other, "proactive", now=200, verified_boundary=boundary)
        for now in (True, -1, MAX_TIME + 1, 122):
            with self.assertRaises(RatePolicyError):
                daily_quota_spec(GROUP, "proactive", now=now, verified_boundary=boundary)
        with self.assertRaises(RatePolicyError):
            daily_quota_spec(GROUP, "proactive", now=MAX_TIME, verified_boundary=boundary)

    def test_ids_not_printed_by_specs(self):
        rate = build_rate_specs(GROUP, "proactive")[-1]
        quota = daily_quota_spec(GROUP, "proactive", now=100, unverified_period_start=10)
        for value in (ACCOUNT.app_id, GROUP.native_conversation_id, ACCOUNT.key, GROUP.key):
            self.assertNotIn(value, repr(rate))
            self.assertNotIn(value, repr(quota))

    def test_invalid_identity_and_configuration_fail_closed(self):
        for native_id in ("TEST\x00ID", "TEST\u200dID", "x" * 257):
            with self.assertRaises(RatePolicyError):
                build_rate_specs(EndpointKey(ACCOUNT, "group", native_id), "proactive")
        for certification in (True, "personal", "enterprise", "yes"):
            with self.assertRaises(RatePolicyError):
                build_rate_specs(GROUP, "proactive", certification=certification)
        with self.assertRaises(RatePolicyError):
            build_rate_specs(GROUP, "auto")


class AutoClock:
    def __init__(self):
        self.mono, self.wall = 0.0, 1000.0
        self.sleeps = []
        self.on_sleep = None
        self.gate = None

    async def sleep(self, seconds):
        if self.gate is not None:
            assert not self.gate._lock.locked(), "sleep held local lock"
        self.sleeps.append(seconds)
        if self.on_sleep:
            self.on_sleep(seconds)
        self.mono += seconds
        self.wall += seconds
        await asyncio.sleep(0)

    def make_gate(self, **kwargs):
        self.gate = LocalAccountRateGate(ACCOUNT, clock=lambda: self.mono, wall_clock=lambda: self.wall, sleep=self.sleep, **kwargs)
        return self.gate


class ManualClock(AutoClock):
    def __init__(self):
        super().__init__()
        self.pending = []

    async def sleep(self, seconds):
        assert not self.gate._lock.locked()
        self.sleeps.append(seconds)
        future = asyncio.get_running_loop().create_future()
        self.pending.append((self.mono + seconds, future))
        await future

    async def advance(self, seconds):
        self.mono += seconds
        self.wall += seconds
        for deadline, future in self.pending:
            if deadline <= self.mono and not future.done():
                future.set_result(None)
        for _ in range(3):
            await asyncio.sleep(0)


async def no_cooldown():
    return None


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_initial_bucket_and_no_idle_burst(self):
        clock = AutoClock()
        gate = clock.make_gate()
        first = await gate.acquire(GROUP, "proactive", read_blocked_until=no_cooldown)
        self.assertEqual(first.acquired_at_monotonic, 3)
        clock.mono += 100
        clock.wall += 100
        second = await gate.acquire(GROUP, "proactive", read_blocked_until=no_cooldown)
        third = await gate.acquire(GROUP, "proactive", read_blocked_until=no_cooldown)
        self.assertEqual(second.acquired_at_monotonic, 103)
        self.assertEqual(third.acquired_at_monotonic, 106)

    async def test_restart_gate_begins_empty_again(self):
        clock = AutoClock()
        first_gate = clock.make_gate()
        await first_gate.acquire(GROUP, "proactive", read_blocked_until=no_cooldown)
        restarted = clock.make_gate()
        result = await restarted.acquire(GROUP, "proactive", read_blocked_until=no_cooldown)
        self.assertEqual(result.acquired_at_monotonic, 6)

    async def test_concurrent_endpoints_share_account_bucket_atomically(self):
        clock = ManualClock()
        gate = clock.make_gate()
        second = EndpointKey(ACCOUNT, "group", "TEST_SECOND")
        tasks = [asyncio.create_task(gate.acquire(endpoint, "proactive", read_blocked_until=no_cooldown)) for endpoint in (GROUP, second)]
        await asyncio.sleep(0)
        await clock.advance(3)
        self.assertEqual(sum(task.done() for task in tasks), 1)
        await clock.advance(2)
        permits = await asyncio.gather(*tasks)
        self.assertEqual(sorted(p.acquired_at_monotonic for p in permits), [3, 5])
        self.assertTrue(all(bucket.tokens >= 0 for bucket in gate._buckets.values()))

    async def test_simultaneous_same_endpoint_obeys_stricter_endpoint_gate(self):
        clock = ManualClock()
        gate = clock.make_gate()
        tasks = [asyncio.create_task(gate.acquire(GROUP, "proactive", read_blocked_until=no_cooldown)) for _ in range(3)]
        await asyncio.sleep(0)
        for count in range(1, 4):
            await clock.advance(3)
            self.assertEqual(sum(task.done() for task in tasks), count)
        self.assertEqual([p.acquired_at_monotonic for p in await asyncio.gather(*tasks)], [3, 6, 9])

    async def test_profile_change_reuses_key_and_discards_existing_rate_credit(self):
        clock = AutoClock()
        gate = clock.make_gate()
        await gate.acquire(GROUP, "proactive", read_blocked_until=no_cooldown)
        keys = set(gate._buckets)
        clock.mono += 100
        clock.wall += 100
        upgraded = await gate.acquire(GROUP, "proactive", certification="certified", read_blocked_until=no_cooldown)
        self.assertEqual(set(gate._buckets), keys)
        self.assertEqual(upgraded.acquired_at_monotonic, 104)

    async def test_passive_does_not_spend_proactive_tokens_but_obeys_cooldown(self):
        clock = AutoClock()
        gate = clock.make_gate()
        permit = await gate.acquire(GROUP, "passive", read_blocked_until=lambda: 1005)
        self.assertEqual(permit.acquired_at_monotonic, 5)
        self.assertEqual(gate._buckets, {})

    async def test_async_cooldown_is_read_on_every_wake_and_extension_wins(self):
        clock = AutoClock()
        gate = clock.make_gate()
        reads = []

        async def read():
            await asyncio.sleep(0)
            reads.append(clock.wall)
            return 1005 if len(reads) == 1 else 1010

        permit = await gate.acquire(GROUP, "proactive", read_blocked_until=read)
        self.assertEqual(clock.sleeps, [5, 5])
        self.assertEqual(permit.acquired_at_monotonic, 10)
        self.assertEqual(len(reads), 3)

    async def test_long_cooldown_uses_bounded_sleeps_without_lock(self):
        clock = AutoClock()
        gate = clock.make_gate()
        permit = await gate.acquire(PRIVATE, "proactive", read_blocked_until=lambda: 1125)
        self.assertEqual(clock.sleeps, [60, 60, 5])
        self.assertEqual(permit.acquired_at_monotonic, 125)

    async def test_permit_bound_to_target_mode_and_profile(self):
        clock = AutoClock()
        gate = clock.make_gate()
        permit = await gate.acquire(GROUP, "passive", read_blocked_until=no_cooldown)
        self.assertTrue(permit.matches(GROUP, "passive"))
        self.assertFalse(permit.matches(GROUP, "proactive"))
        self.assertFalse(permit.matches(PRIVATE, "passive"))
        self.assertFalse(permit.matches(GROUP, "passive", certification="certified"))
        other = EndpointKey(AccountKey("qq-official", ACCOUNT.app_id, "sandbox"), "group", GROUP.native_conversation_id)
        self.assertFalse(permit.matches(other, "passive"))
        self.assertNotIn(GROUP.native_conversation_id, repr(permit))
        self.assertNotIn(ACCOUNT.app_id, repr(gate))

    async def test_wall_clock_rollback_cannot_extend_original_deadline(self):
        clock = AutoClock()
        gate = clock.make_gate()
        clock.on_sleep = lambda _: setattr(clock, "wall", clock.wall - 100)
        with self.assertRaises(RateWaitExpired):
            await gate.acquire(GROUP, "proactive", read_blocked_until=no_cooldown, deadline=1002)
        self.assertEqual(clock.mono, 2)
        self.assertLess(clock.wall, 1002)

    async def test_wall_clock_forward_jump_expires_before_dispatch(self):
        clock = AutoClock()
        gate = clock.make_gate()
        clock.on_sleep = lambda _: setattr(clock, "wall", clock.wall + 100)
        with self.assertRaises(RateWaitExpired):
            await gate.acquire(GROUP, "proactive", read_blocked_until=no_cooldown, deadline=1010)
        self.assertEqual(clock.mono, 3)

    async def test_elapsed_async_cooldown_read_cannot_bypass_deadline(self):
        clock = AutoClock()
        gate = clock.make_gate()

        async def slow_read():
            clock.mono += 10
            clock.wall += 10
            return None

        with self.assertRaises(RateWaitExpired):
            await gate.acquire(GROUP, "passive", read_blocked_until=slow_read, deadline=1005)
        self.assertEqual(gate._buckets, {})

    async def test_expired_request_never_reads_db_cooldown(self):
        clock = AutoClock()
        gate = clock.make_gate()

        def forbidden():
            raise AssertionError("expired request read cooldown")

        with self.assertRaises(RateWaitExpired):
            await gate.acquire(GROUP, "proactive", read_blocked_until=forbidden, deadline=1000)

    async def test_wrong_account_never_reads_cooldown(self):
        clock = AutoClock()
        gate = clock.make_gate()
        other = EndpointKey(AccountKey("qq-official", "TEST_OTHER", "test"), "group", "TEST_G")

        def forbidden():
            raise AssertionError("wrong account read cooldown")

        with self.assertRaises(RatePolicyError):
            await gate.acquire(other, "proactive", read_blocked_until=forbidden)

    async def test_cancellation_during_wait_consumes_no_partial_rate_token(self):
        clock = ManualClock()
        gate = clock.make_gate()
        task = asyncio.create_task(gate.acquire(GROUP, "proactive", read_blocked_until=no_cooldown))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await clock.advance(3)
        result = await gate.acquire(GROUP, "proactive", read_blocked_until=no_cooldown)
        self.assertEqual(result.acquired_at_monotonic, 3)

    async def test_bucket_capacity_fails_closed_without_unbounded_allocation(self):
        clock = AutoClock()
        gate = clock.make_gate(max_buckets=2)
        await gate.acquire(GROUP, "proactive", read_blocked_until=no_cooldown)
        other = EndpointKey(ACCOUNT, "group", "TEST_OTHER")
        with self.assertRaisesRegex(RatePolicyError, "local_bucket_capacity_exceeded"):
            await gate.acquire(other, "proactive", read_blocked_until=no_cooldown)
        self.assertEqual(len(gate._buckets), 2)

    async def test_bad_cooldown_deadline_and_clock_rejected_without_permit(self):
        for value in (True, -1, float("inf"), float("nan"), 10**999, "1000"):
            clock = AutoClock()
            gate = clock.make_gate()
            with self.assertRaises(RatePolicyError):
                await gate.acquire(GROUP, "passive", read_blocked_until=lambda: value)
            with self.assertRaises(RatePolicyError):
                await gate.acquire(GROUP, "passive", read_blocked_until=no_cooldown, deadline=value)
        clock = AutoClock()
        gate = clock.make_gate()
        await gate.acquire(GROUP, "passive", read_blocked_until=no_cooldown)
        clock.mono = -1
        with self.assertRaises(RatePolicyError):
            await gate.acquire(GROUP, "passive", read_blocked_until=no_cooldown)


if __name__ == "__main__":
    unittest.main()
