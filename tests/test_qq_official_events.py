import copy
import json
from pathlib import Path
import unittest

from src.qq_official.events import EventParseError, MAX_EVENT_BYTES, parse_dispatch

FIXTURE = Path(__file__).parent / "fixtures" / "qq_official" / "group_message.json"


class QQOfficialEventTests(unittest.TestCase):
    def setUp(self):
        self.event = json.loads(FIXTURE.read_text())

    def test_three_ids_are_distinct_and_optional_data_preserved(self):
        event = parse_dispatch(self.event)
        self.assertEqual((event.event_id, event.seq), ("EVENT_ID_TEST_1", 42))
        msg = event.message
        self.assertEqual(msg.message_id, "MESSAGE_ID_TEST_1")
        self.assertEqual(msg.msg_idx, "MSG_INDEX_TEST_1")
        self.assertEqual(msg.ref_msg_idx, "REF_INDEX_TEST_1")
        self.assertTrue(msg.mentions_bot)
        self.assertEqual(msg.elements[0].author.native_id, "MEMBER_OPENID_TEST_2")
        self.assertEqual(msg.elements[0].elements[0].author.native_id, "MEMBER_OPENID_TEST_3")
        self.assertEqual(msg.elements[0].attachments[0].asr_refer_text, "测试识别文本")
        self.assertEqual(msg.elements[0].attachments[0].content_type, "audio/wav")
        self.assertIn("signature=TEST_ONLY", msg.elements[0].attachments[0].url)
        self.assertNotIn("TEST_1", repr(event))
        self.assertNotIn("signature", repr(msg))

    def test_mentions_need_platform_evidence(self):
        for event_type, mentions, expected in [
            ("GROUP_MESSAGE_CREATE", [], False),
            ("GROUP_MESSAGE_CREATE", [{"is_you": "true"}], False),
            ("GROUP_MESSAGE_CREATE", [{"is_you": True}], True),
            ("GROUP_AT_MESSAGE_CREATE", [], True),
        ]:
            with self.subTest(event_type=event_type, mentions=mentions):
                self.event["t"] = event_type
                self.event["d"]["mentions"] = mentions
                self.assertEqual(parse_dispatch(self.event).message.mentions_bot, expected)

    def test_c2c_missing_fields_do_not_borrow_group_identity(self):
        msg = parse_dispatch({"op": 0, "t": "C2C_MESSAGE_CREATE", "d": {
            "author": {"member_openid": "MEMBER_OPENID_TEST_1", "member_role": "not-a-role"}
        }}).message
        self.assertIsNone(msg.message_id)
        self.assertIsNone(msg.author.native_id)
        self.assertIsNone(msg.endpoint_id)
        self.assertEqual(msg.author.platform_role, "")
        self.assertIsNone(msg.occurred_at)
        self.assertEqual(msg.elements, ())

    def test_lifecycle_unknown_and_absent_ids_are_not_empty_dedupe_keys(self):
        for event_type in ("GROUP_ADD_ROBOT", "GROUP_DEL_ROBOT", "GROUP_MSG_RECEIVE", "C2C_MSG_REJECT", "FUTURE_EVENT"):
            with self.subTest(event_type=event_type):
                event = parse_dispatch({"op": 0, "t": event_type, "d": {"new_field": "test"}})
                self.assertEqual(event.data["new_field"], "test")
                self.assertIsNone(event.event_id)
                self.assertIsNone(event.seq)
                self.assertIsNone(event.message)

    def test_ref_formats_and_conflicting_evidence(self):
        for ext in (["msg_idx=TEST_INDEX", "ref_msg_idx=REF_INDEX_TEST_1"],
                    [{"key": "msg_idx", "value": "TEST_INDEX"}], {"msg_idx": "TEST_INDEX"}):
            with self.subTest(ext=ext):
                self.event["d"]["message_scene"]["ext"] = ext
                self.assertEqual(parse_dispatch(self.event).message.msg_idx, "TEST_INDEX")
        self.event["d"]["message_scene"]["ext"] = ["ref_msg_idx=OTHER_INDEX_TEST"]
        msg = parse_dispatch(self.event).message
        self.assertTrue(msg.reference_conflict)
        self.assertIsNone(msg.ref_msg_idx)
        self.assertEqual(msg.elements[0].msg_idx, "REF_INDEX_TEST_1")

    def test_missing_quote_author_is_unknown_and_input_is_copied(self):
        del self.event["d"]["msg_elements"][0]["author"]
        msg = parse_dispatch(self.event).message
        self.assertIsNone(msg.elements[0].author)
        self.event["d"]["content"] = "changed"
        self.assertNotEqual(msg.content, "changed")
        self.assertNotEqual(msg.raw["content"], "changed")

    def test_card_does_not_substitute_preview_and_rejects_unsafe_scheme(self):
        for url, expected in [("javascript:alert(1)", None), ("https://user:secret@example.invalid/", None), ("https://example.invalid/post", "https://example.invalid/post"), (None, None)]:
            with self.subTest(url=url):
                self.event["d"]["ark_data"] = {"jump_url": url, "preview": "https://example.invalid/image.png"}
                msg = parse_dispatch(self.event).message
                self.assertEqual(msg.jump_url, expected)
                self.assertIn("preview", msg.ark_data)

    def test_payload_limits_quarantine_without_echoing_data(self):
        deep = {}
        for _ in range(20):
            deep = {"child": deep}
        invalid = [
            b"{broken PRIVATE_TEST_TEXT", {"op": 7, "t": "BAD", "d": {}},
            {"op": 0, "t": "BAD", "d": "PRIVATE_TEST_TEXT"},
            {"op": 0, "t": "BAD", "d": deep},
            {"op": 0, "t": "BAD", "d": {"secret": "x" * MAX_EVENT_BYTES}},
            {"op": 0, "t": "BAD", "d": {"list": list(range(257))}},
            {"op": 0, "t": "BAD", "d": {"number": float("nan")}},
            {"op": 0, "t": "BAD", "d": {"unicode": "\ud800"}},
        ]
        for value in invalid:
            with self.subTest(type=type(value).__name__):
                with self.assertRaises(EventParseError) as caught:
                    parse_dispatch(value)
                self.assertNotIn("PRIVATE_TEST_TEXT", str(caught.exception))

    def test_timestamp_needs_timezone_and_opaque_ids_are_never_int_cast(self):
        for value, known in [("2026-10-28T10:00:00Z", True), ("2026-10-28T10:00:00", False), ("garbage", False), (None, False)]:
            data = copy.deepcopy(self.event)
            data["d"]["timestamp"] = value
            with self.subTest(value=value):
                msg = parse_dispatch(data).message
                self.assertEqual(msg.occurred_at is not None, known)
                self.assertEqual(msg.author.native_id, "MEMBER_OPENID_TEST_1")

    def test_oversized_numeric_mapping_cannot_bypass_byte_budget(self):
        huge = int("9" * 4000)
        with self.assertRaises(EventParseError) as caught:
            parse_dispatch({"op": 0, "t": "TEST_EVENT", "d": {str(n): huge for n in range(100)}})
        self.assertEqual(str(caught.exception), "event_number_limit")
        # Escaped control bytes count toward serialized size, too.
        with self.assertRaises(EventParseError):
            parse_dispatch({"op": 0, "t": "TEST_EVENT", "d": {"content": "\x00" * 50000}})

    def test_untrusted_type_fields_cannot_leak_in_repr(self):
        unknown = parse_dispatch({"op": 0, "t": "PRIVATE_TEST_TYPE", "d": {}})
        self.assertNotIn("PRIVATE_TEST_TYPE", repr(unknown))
        self.event["d"]["attachments"][0]["content_type"] = "PRIVATE_TEST_MIME"
        attachment = parse_dispatch(self.event).message.attachments[0]
        self.assertNotIn("PRIVATE_TEST_MIME", repr(attachment))
        self.assertEqual(attachment.content_type, "PRIVATE_TEST_MIME")

    def test_conflicting_duplicate_scene_and_element_indices_stay_unresolved(self):
        self.event["d"]["message_scene"]["ext"] = [
            "msg_idx=INDEX_A_TEST", "msg_idx=INDEX_B_TEST", "ref_msg_idx=REF_INDEX_TEST_1",
            {"key": "ref_msg_idx", "value": "REF_CONFLICT_TEST"},
        ]
        msg = parse_dispatch(self.event).message
        self.assertTrue(msg.reference_conflict)
        self.assertIsNone(msg.msg_idx)
        self.assertIsNone(msg.ref_msg_idx)
        self.event["d"]["message_scene"]["ext"] = []
        self.event["d"]["msg_elements"].append({"msg_idx": "REF_CONFLICT_TEST"})
        msg = parse_dispatch(self.event).message
        self.assertTrue(msg.reference_conflict)
        self.assertIsNone(msg.ref_msg_idx)
