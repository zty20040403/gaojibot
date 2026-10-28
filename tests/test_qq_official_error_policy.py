from __future__ import annotations

import unittest

from src.plugins.ai_chat.adapters.qq_official.error_policy import classify_successful_stage, decide_send_error
from src.qq_official.errors import QQAPIError, classify_error


class ErrorPolicyTests(unittest.TestCase):
    def test_local_prepared_expiry_is_bounded_unsent_not_remote_auth(self):
        error = QQAPIError("prepared_token_unavailable", accepted=False, stage="token")
        for retry in (0, 1, 2):
            decision = decide_send_error(error, auth_retries_used=1, retry_count=retry)
            self.assertEqual((decision.action, decision.reason, decision.message_acceptance),
                             ("backoff_unsent", "prepared_auth_stale", "not_sent"))
            self.assertIs(decision.operation_acceptance, False)
            self.assertEqual(decision.delay_seconds, 1)
        self.assertEqual(decide_send_error(error, retry_count=3).action, "pause_retry_budget")

    def test_prepared_expiry_proof_requires_exact_local_stage_and_no_http_fields(self):
        errors = (
            QQAPIError("prepared_token_unavailable", stage="token"),
            QQAPIError("prepared_token_unavailable", accepted=False, stage="send"),
            QQAPIError("prepared_token_unavailable", accepted=False, stage="token", status=401),
            QQAPIError("prepared_token_unavailable", accepted=False, stage="token", code=0),
            QQAPIError("prepared_send_unavailable", accepted=False, stage="token"),
        )
        for error in errors:
            self.assertNotEqual(decide_send_error(error).action, "backoff_unsent")

    def test_plan_known_error_rows_from_real_m0_classifier(self):
        rows = (
            (200, 40034006, "stop_content"),
            (400, 40034128, "close_window_and_reselect"),
            (200, 40034105, "wait_window"),
            (200, 50002, "wait_rate_limit"),
            (429, None, "wait_rate_limit"),
            (401, None, "refresh_token_once"),
        )
        for status, code, action in rows:
            with self.subTest(status=status, code=code):
                decision = decide_send_error(classify_error(status, code))
                self.assertEqual(decision.action, action)
                self.assertEqual(decision.message_acceptance, "rejected")
                self.assertFalse(decision.automatic_format_fallback)
                self.assertEqual((decision.status, decision.code, decision.stage), (status, code, "request"))

    def test_content_rejection_terminal_at_message_and_upload_stages(self):
        for stage in ("request", "send", "upload"):
            decision = decide_send_error(classify_error(200, 40034006, stage=stage), retry_count=99)
            self.assertEqual(decision.action, "stop_content")
            self.assertIn("no_format_account_or_channel_retry", decision.effects)

    def test_unknown_timeouts_disconnect_5xx_and_missing_receipt_stay_ambiguous(self):
        errors = (
            QQAPIError("transport_ambiguous"),
            QQAPIError("missing_receipt", stage="send"),
            QQAPIError("invalid_response", status=200),
            classify_error(500, None), classify_error(503, None),
            classify_error(200, 99999999),
            classify_error(400, None),
        )
        for error in errors:
            with self.subTest(category=error.category, status=error.status):
                decision = decide_send_error(error)
                self.assertEqual((decision.action, decision.message_acceptance), ("quarantine_ambiguous", "unknown"))
                self.assertIsNone(decision.delay_seconds)

    def test_numeric_code_never_overrides_ambiguous_acceptance(self):
        for code in (40034006, 40034128, 40034105, 50002):
            category = classify_error(200, code).category
            decision = decide_send_error(QQAPIError(category, status=200, code=code, accepted=None))
            self.assertEqual(decision.action, "quarantine_ambiguous")
            self.assertEqual(decision.effects, ("no_automatic_resend",))

    def test_category_string_alone_never_grants_special_action(self):
        for category in ("content_rejected", "window_exhausted", "proactive_denied", "authentication", "rate_limited", "format_rejected", "ip_whitelist_rejected"):
            decision = decide_send_error(QQAPIError(category, accepted=False, status=400))
            self.assertEqual(decision.action, "pause_unknown")
            self.assertFalse(decision.automatic_format_fallback)

    def test_format_primary_diagnostics_remain_unsupported_by_m0_acceptance(self):
        # Real codes from pinned Tencent bot-docs; they are NOT safe-retry rules.
        for code in (50037, 50041, 50042, 50054, 50055, 50056, 50057):
            decision = decide_send_error(classify_error(400, code))
            self.assertEqual(decision.action, "quarantine_ambiguous")
            self.assertFalse(decision.automatic_format_fallback)

    def test_channel_whitelist_code_is_not_invented_ip_mapping(self):
        # Official description is an application/channel allowlist, not egress IP.
        decision = decide_send_error(classify_error(403, 1100307))
        self.assertEqual(decision.action, "quarantine_ambiguous")
        self.assertEqual(decision.effects, ("no_automatic_resend",))

    def test_connection_failure_is_unsent_and_retry_budget_bounded(self):
        error = QQAPIError("connection_failed", accepted=False)
        decision = decide_send_error(error, retry_count=2)
        self.assertEqual((decision.action, decision.message_acceptance, decision.delay_seconds), ("backoff_unsent", "not_sent", 4))
        exhausted = decide_send_error(error, retry_count=3)
        self.assertEqual(exhausted.action, "pause_retry_budget")
        self.assertIsNone(exhausted.delay_seconds)

    def test_connection_claim_with_response_or_ambiguity_is_not_proof(self):
        for error in (QQAPIError("connection_failed"), QQAPIError("connection_failed", accepted=False, status=500)):
            self.assertNotEqual(decide_send_error(error).action, "backoff_unsent")

    def test_rate_limit_honors_retry_after_and_does_not_infer_daily_quota(self):
        error = classify_error(429, None, retry_after=3600)
        decision = decide_send_error(error)
        self.assertEqual(decision.delay_seconds, 3600)
        self.assertIn("no_inferred_daily_quota", decision.effects)
        self.assertEqual(decide_send_error(error, retry_count=3).action, "pause_retry_budget")

    def test_malformed_retry_after_uses_finite_bounded_delay(self):
        for value in (True, float("nan"), float("inf"), -1, 86401, 10**999, "5"):
            error = classify_error(429, None, retry_after=value)
            self.assertEqual(decide_send_error(error, retry_count=2).delay_seconds, 4)

    def test_authentication_single_retry_and_then_pause(self):
        error = classify_error(401, None)
        decision = decide_send_error(error)
        self.assertEqual(decision.action, "refresh_token_once")
        self.assertIn("single_flight_refresh", decision.effects)
        for count in (1, 2, 100):
            self.assertEqual(decide_send_error(error, auth_retries_used=count).action, "pause_auth")

    def test_no_broad_membership_revocation_on_proactive_denial(self):
        decision = decide_send_error(classify_error(200, 40034105))
        self.assertIn("deny_proactive_for_exact_endpoint", decision.effects)
        self.assertIn("retain_membership_and_other_endpoints", decision.effects)

    def test_token_failures_never_consume_a_message_attempt(self):
        rows = (
            (classify_error(401, None, stage="token"), "pause_auth"),
            (classify_error(429, None, stage="token"), "wait_token"),
            (QQAPIError("connection_failed", accepted=False, stage="token"), "wait_token"),
            (QQAPIError("invalid_token_response", accepted=False, stage="token"), "pause_token_preflight"),
            (QQAPIError("transport_ambiguous", stage="token"), "pause_token_preflight"),
        )
        for error, action in rows:
            with self.subTest(category=error.category):
                decision = decide_send_error(error)
                self.assertEqual((decision.action, decision.message_acceptance), (action, "not_sent"))

    def test_upload_unsupported_is_only_exact_capability_and_m6_review(self):
        error = classify_error(200, 50015014, stage="upload")
        decision = decide_send_error(error)
        self.assertEqual((decision.action, decision.message_acceptance), ("review_file_route", "not_sent"))
        self.assertIn("mark_exact_file_capability_temporarily_unavailable", decision.effects)
        self.assertIn("require_m6_route_authorization", decision.effects)
        # The same number at the message POST does not acquire upload semantics.
        self.assertEqual(decide_send_error(classify_error(200, 50015014)).action, "quarantine_ambiguous")

    def test_upload_contract_codes_do_not_infer_quota_or_message_acceptance(self):
        for code in (40093001, 40093002):
            decision = decide_send_error(classify_error(200, code, stage="upload"))
            self.assertEqual((decision.action, decision.message_acceptance), ("review_upload_contract", "not_sent"))
            self.assertIsNone(decision.operation_acceptance)
            self.assertIsNone(decision.delay_seconds)

    def test_upload_ambiguous_is_distinct_from_message_ambiguous(self):
        decision = decide_send_error(QQAPIError("transport_ambiguous", stage="upload"))
        self.assertEqual((decision.action, decision.message_acceptance), ("review_upload_ambiguous", "not_sent"))
        self.assertIsNone(decision.operation_acceptance)

    def test_malformed_numeric_fields_and_unknown_stage_fail_closed(self):
        for status, code in ((True, None), (401, "0"), ("401", None)):
            decision = decide_send_error(QQAPIError("authentication", status=status, code=code, accepted=False))
            self.assertEqual(decision.action, "pause_unknown")
        decision = decide_send_error(classify_error(401, None, stage="gateway"))
        self.assertEqual((decision.action, decision.message_acceptance), ("quarantine_ambiguous", "unknown"))

    def test_out_of_range_status_does_not_apply_even_known_code_effects(self):
        for status in (0, 99, 600, 2**63, True, "200"):
            for code in (40034006, 40034128, 40034105, 50002):
                category = classify_error(200, code).category
                decision = decide_send_error(QQAPIError(category, status=status, code=code, accepted=False))
                self.assertEqual(decision.action, "pause_unknown")
                self.assertEqual(decision.effects, ("no_automatic_resend",))

    def test_retry_counters_cannot_be_reset_with_bad_types(self):
        for keyword in ("auth_retries_used", "retry_count", "max_transient_retries"):
            for value in (-1, True, 1.5, "0"):
                with self.assertRaises(ValueError):
                    decide_send_error(classify_error(429, None), **{keyword: value})

    def test_request_id_kept_for_audit_but_not_in_repr(self):
        decision = decide_send_error(classify_error(401, None, request_id="TEST_PRIVATE_TRACE"))
        self.assertEqual(decision.request_id, "TEST_PRIVATE_TRACE")
        self.assertNotIn("TEST_PRIVATE_TRACE", repr(decision))

    def test_success_is_stage_specific_and_never_claims_local_commit(self):
        for stage in ("token", "upload"):
            decision = classify_successful_stage(stage, {"id": "TEST_FILE_OR_TOKEN_NOT_MESSAGE"})
            self.assertEqual(decision.message_acceptance, "not_sent")
            self.assertIsNone(decision.native_message_id)
            self.assertFalse(decision.delivery_committed)
        sent = classify_successful_stage("send", {"id": "TEST_MESSAGE:原样/+"})
        self.assertEqual(sent.message_acceptance, "accepted")
        self.assertEqual(sent.native_message_id, "TEST_MESSAGE:原样/+")
        self.assertFalse(sent.delivery_committed)
        self.assertNotIn(sent.native_message_id, repr(sent))

    def test_missing_invalid_or_error_receipt_is_not_success(self):
        for payload in ({}, None, {"id": True}, {"id": 123}, {"id": "bad\x00id"}, {"id": "x" * 257},
                        {"id": "TEST_ID", "code": 40034006}):
            self.assertEqual(classify_successful_stage("send", payload).message_acceptance, "unknown")


if __name__ == "__main__":
    unittest.main()
