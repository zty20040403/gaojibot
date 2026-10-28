import unittest

from src.plugins.ai_chat.adapters.qq_official.format_fallback import (
    FORMAT_PARAMETER_CODES,
    FORMAT_PINNED_DOC_COMMIT,
    FORMAT_SOURCE_HTML_SHA256,
    classify_format_rejection,
    decide_format_fallback,
)
from src.qq_official.errors import QQAPIError, classify_error


class FormatFallbackTests(unittest.TestCase):
    def decision(self, error, **overrides):
        options = {"attempted_variant": "primary_markdown", "fallbacks_used": 0,
                   "frozen_plain_variant_ready": True}
        options.update(overrides)
        return decide_format_fallback(error, **options)

    def test_exact_five_primary_contract_codes_upgrade_m0_unknown(self):
        self.assertEqual(set(FORMAT_PARAMETER_CODES), {50037, 50041, 50042, 50054, 50057})
        for code in FORMAT_PARAMETER_CODES:
            for status in (200, 400):
                with self.subTest(code=code, status=status):
                    original = classify_error(status, code)
                    self.assertIsNone(original.accepted)
                    decision = self.decision(original)
                    self.assertEqual(decision.action, "select_frozen_plain_once")
                    self.assertIs(decision.rejection.accepted, False)
                    self.assertEqual(decision.rejection.source_sha256, FORMAT_SOURCE_HTML_SHA256)
                    self.assertIn(FORMAT_PINNED_DOC_COMMIT, decision.rejection.pinned_source_url)
                    self.assertIsNone(original.accepted)  # original evidence is not overwritten

    def test_each_part_can_switch_once_and_restart_uses_persisted_count(self):
        code = 50057
        first = self.decision(classify_error(400, code))
        self.assertEqual(first.action, "select_frozen_plain_once")
        # Simulate durable compare-and-swap primary -> fallback before any I/O.
        stored = {"variant": "plain_fallback", "fallbacks_used": 1}
        after_restart = self.decision(classify_error(400, code), attempted_variant=stored["variant"],
                                      fallbacks_used=stored["fallbacks_used"])
        self.assertEqual(after_restart.action, "no_fallback")
        self.assertEqual(self.decision(classify_error(400, code), fallbacks_used=1).action, "no_fallback")

    def test_moderation_unknown_content_permission_and_generic_codes_never_fallback(self):
        for code in (40034006, 50055, 50056, 12002, 50035, 304036, 99999999):
            with self.subTest(code=code):
                self.assertEqual(self.decision(classify_error(400, code)).action, "no_fallback")

    def test_async_accepted_http_or_conflicting_transport_status_never_fallback(self):
        for status in (201, 202, 204, 301, 401, 403, 404, 422, 429, 500, 503, 599, 0, 600, True, None):
            with self.subTest(status=status):
                error = QQAPIError("unknown", status=status, code=50041)
                self.assertIsNone(classify_format_rejection(error))

    def test_timeout_invalid_response_or_remote_category_does_not_become_evidence(self):
        for category in ("transport_ambiguous", "invalid_response", "response_too_large", "content_rejected"):
            error = QQAPIError(category, status=400, code=50041)
            self.assertEqual(self.decision(error).action, "no_fallback")

    def test_non_send_stages_never_trigger_message_fallback(self):
        for stage in ("token", "upload", "gateway", "self_api"):
            self.assertEqual(self.decision(QQAPIError("unknown", status=400, code=50041, stage=stage)).action, "no_fallback")

    def test_rejected_parameter_body_does_not_override_existing_acceptance_or_ambiguity(self):
        error = classify_error(400, 50041)
        self.assertEqual(self.decision(error, part_already_accepted=True).action, "no_fallback")
        self.assertEqual(self.decision(error, earlier_attempt_unresolved=True).action, "no_fallback")
        self.assertEqual(self.decision(QQAPIError("unknown", accepted=True, status=400, code=50041)).action, "no_fallback")

    def test_plain_variant_must_already_be_valid_and_frozen(self):
        error = classify_error(400, 50041)
        self.assertEqual(self.decision(error, frozen_plain_variant_ready=False).action, "no_fallback")
        self.assertEqual(self.decision(error, attempted_variant="primary_text").action, "no_fallback")
        decision = self.decision(error)
        self.assertIn("fresh_attempt_sequence_and_rate_reservation", decision.required_checks)
        self.assertIn("unchanged_frozen_plain_content", decision.required_checks)

    def test_malformed_code_and_state_fail_closed(self):
        for code in (True, "50041", None):
            self.assertEqual(self.decision(QQAPIError("unknown", status=400, code=code)).action, "no_fallback")
        for change in ({"fallbacks_used": True}, {"fallbacks_used": -1},
                       {"frozen_plain_variant_ready": 1}, {"earlier_attempt_unresolved": "false"}):
            with self.assertRaises(ValueError):
                self.decision(classify_error(400, 50041), **change)


if __name__ == "__main__":
    unittest.main()
