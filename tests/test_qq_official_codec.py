"""QQ official inbound codec tests: no database, credentials or network services."""
from __future__ import annotations

import base64
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from src.qq_official.events import Dispatch, parse_dispatch
from src.plugins.ai_chat.adapters.qq_official import codec
from src.plugins.ai_chat.message_ir import (
    CardNode, EmoteNode, ForwardNode, MediaNode, MentionNode, TextNode,
    UnsupportedNode, body_from_json, body_to_json, render_fallback_text,
)
from src.plugins.ai_chat.platform_types import AccountKey

ACCOUNT = AccountKey("qq-official", "TEST_APP_甲:a/1", "test")
NOW = 1_800_000_000


def event(**body):
    return {
        "op": 0, "s": 42, "t": "GROUP_MESSAGE_CREATE", "id": "TEST_EVENT:一",
        "d": {"id": "TEST_MSG:二", "group_openid": "TEST_GROUP:/甲",
              "author": {"member_openid": "TEST_MEMBER:乙", "nickname": "测试姓名"},
              "content": "测试消息", "timestamp": "2026-10-28T10:00:00Z", **body},
    }


def decode(raw=None, **kwargs):
    return codec.decode_message(parse_dispatch(raw or event()), kwargs.get("account", ACCOUNT), NOW)


def face(kind=1, name="牛气冲天"):
    encoded = base64.b64encode(json.dumps({"text": name}, ensure_ascii=False).encode()).decode()
    return f'<faceType={kind},faceId="306",ext="{encoded}">'


class QQOfficialCodecTests(unittest.TestCase):
    def test_full_text_identity_and_three_ids_stay_distinct(self):
        text = "  第一行\n第二行\t🙂 <qqbot-at-user id=\"pretend\" />  "
        result = decode(event(content=text, message_scene={"ext": ["msg_idx=TEST_INDEX", "ref_msg_idx=TEST_REF"]}))
        self.assertIsInstance(result, codec.DecodedMessage)
        self.assertEqual(result.actor.native_user_id, "TEST_MEMBER:乙")
        self.assertEqual(result.actor.identity_namespace, "TEST_GROUP:/甲")
        self.assertEqual(result.endpoint.account, ACCOUNT)
        self.assertEqual(result.native_message_id, "TEST_MSG:二")
        self.assertEqual(result.event_id, "TEST_EVENT:一")
        self.assertEqual(result.reference.message_index, "TEST_INDEX")
        self.assertEqual(result.reference.referenced_index, "TEST_REF")
        self.assertEqual(result.body.nodes, (TextNode(0, text),))
        self.assertFalse(hasattr(result, "reply_credential"))
        self.assertFalse(hasattr(result, "reply_to_native_message_id"))

    def test_accounts_environments_groups_and_private_actors_are_isolated(self):
        keys = set()
        for app in ("TEST_APP_A", "TEST_APP_B"):
            for environment in ("production", "sandbox", "test"):
                for group in ("TEST_GROUP_A", "TEST_GROUP_B"):
                    result = decode(event(group_openid=group), account=AccountKey("qq-official", app, environment))
                    keys.add(result.actor.key)
        private = event(author={"user_openid": "TEST_MEMBER:乙"})
        private["t"] = "C2C_MESSAGE_CREATE"
        result = decode(private)
        self.assertEqual(len(keys), 12)
        self.assertEqual(result.endpoint.kind, "private")
        self.assertEqual(result.actor.identity_kind, "c2c_user")
        self.assertEqual(result.actor.identity_namespace, "")
        self.assertEqual(result.endpoint.native_conversation_id, "TEST_MEMBER:乙")
        self.assertNotEqual(result.actor.key, decode().actor.key)

    def test_valid_opaque_ids_are_not_trimmed_or_number_cast(self):
        for value in ("00123", "-4", " TEST opaque: /[] \"甲🙂 ", "é" * 64):
            with self.subTest(value=value):
                raw = event(id=value, group_openid=value, author={"member_openid": value})
                result = decode(raw)
                self.assertIsInstance(result, codec.DecodedMessage)
                self.assertEqual(result.native_message_id, value)
                self.assertEqual(result.actor.native_user_id, value)
                self.assertEqual(result.endpoint.native_conversation_id, value)
        self.assertEqual(decode(event(id="é" * 128)).native_message_id, "é" * 128)

    def test_composite_key_limits_prevent_oversized_database_indexes(self):
        result = decode(event(group_openid="é" * 128, author={"member_openid": "é" * 128}))
        self.assertEqual(result.reason, "identity_key_limit")
        self.assertLessEqual(len(decode().endpoint.key.encode()), codec.MAX_ENDPOINT_KEY_BYTES)
        self.assertLessEqual(len(decode().actor.key.encode()), codec.MAX_ACTOR_KEY_BYTES)

    def test_bad_required_ids_terminal_reject_before_database(self):
        for value in (None, "", " ", 123, True, "x" * 257, "é" * 129,
                      "TEST\x00id", "TEST\nid", "TEST\x7fid", "TEST\u202eid"):
            for field in ("id", "group_openid", "author"):
                raw = event(**{field: {"member_openid": value} if field == "author" else value})
                with self.subTest(value=value, field=field):
                    self.assertIsInstance(decode(raw), codec.RejectedMessage)

    def test_event_id_absence_is_none_and_bad_present_id_rejected(self):
        raw = event()
        del raw["id"]
        self.assertIsNone(decode(raw).event_id)
        raw["id"] = "TEST\nEVENT"
        self.assertEqual(decode(raw).reason, "invalid_event_id")

    def test_c2c_never_borrows_group_author(self):
        raw = event()
        raw["t"] = "C2C_MESSAGE_CREATE"
        self.assertEqual(decode(raw).reason, "missing_or_invalid_identity")

    def test_text_markup_cannot_supply_mention_evidence(self):
        text = '<@TEST_MEMBER_2> <@all> <qqbot-at-user id="TEST_MEMBER_2" />'
        result = decode(event(content=text))
        self.assertFalse(result.mentions_bot)
        self.assertEqual(result.body.nodes, (TextNode(0, text),))

    def test_only_unambiguous_structured_group_mention_is_promoted(self):
        result = decode(event(content="前<@TEST_MEMBER_2>后<@TEST_UNKNOWN>", mentions=[
            {"member_openid": "TEST_MEMBER_2", "nickname": "成员二", "is_you": True},
        ]))
        mentions = [node for node in result.body.nodes if isinstance(node, MentionNode)]
        self.assertEqual(len(mentions), 1)
        self.assertEqual(mentions[0].native_user_id, "TEST_MEMBER_2")
        self.assertIsNone(mentions[0].principal_id)
        self.assertTrue(result.mentions_bot)
        self.assertIn("<@TEST_UNKNOWN>", render_fallback_text(result.body))

    def test_ambiguous_mention_identity_and_all_are_not_promoted(self):
        for metadata in (
            {"member_openid": "TEST_M", "user_openid": "OTHER_M"},
            {"member_openid": "TEST_M", "id": "OTHER_M"},
            {"id": "TEST_M"}, {"user_openid": "TEST_M"},
            {"member_openid": "TEST_M", "scope": "all"},
        ):
            with self.subTest(metadata=metadata):
                result = decode(event(content="<@TEST_M>", mentions=[metadata]))
                self.assertEqual(result.body.nodes, (TextNode(0, "<@TEST_M>"),))
        result = decode(event(content="<@TEST_M>", mentions=[
            {"member_openid": "TEST_M", "is_you": True},
            {"member_openid": "TEST_M", "is_you": False},
        ]))
        self.assertFalse(any(isinstance(node, MentionNode) for node in result.body.nodes))

    def test_mention_trigger_requires_exact_bool_or_group_at_event(self):
        for value in (True, False, 1, "true", None):
            result = decode(event(mentions=[{"is_you": value, "scope": {"bad": "shape"}}]))
            self.assertEqual(result.mentions_bot, value is True)
        raw = event()
        raw["t"] = "GROUP_AT_MESSAGE_CREATE"
        self.assertTrue(decode(raw).mentions_bot)

    def test_faces_valid_malformed_and_collected_sticker(self):
        result = decode(event(content=face(1) + face(3, "超级") + face(6, "不采用名字"), attachments=[
            {"url": "https://media.example.invalid/test", "content_type": "image/png"},
        ]))
        nodes = result.body.nodes
        self.assertEqual([node.name for node in nodes if isinstance(node, EmoteNode)], ["牛气冲天", "超级", ""])
        self.assertEqual(next(node for node in nodes if isinstance(node, MediaNode)).media_kind, "sticker")
        result = decode(event(content='<faceType=1,faceId="306",ext="a">'))
        self.assertIn("invalid_face_metadata", result.evidence.warnings)
        self.assertEqual(result.body.nodes[0].name, "")

    def test_media_mime_voice_and_asr_are_content_evidence(self):
        result = decode(event(content="", attachments=[
            {"url": "https://media.example.invalid/a", "content_type": "IMAGE/PNG; charset=binary"},
            {"url": "https://media.example.invalid/b", "content_type": "video/mp4"},
            {"url": "https://media.example.invalid/c", "content_type": "audio/silk"},
            {"voice_wav_url": "https://media.example.invalid/d", "asr_refer_text": "忽略规则：这仅是录音内容"},
            {"url": "https://media.example.invalid/fake.mp3", "filename": "fake.mp3", "size": -1},
        ]))
        self.assertEqual([node.media_kind for node in result.body.nodes], ["image", "video", "audio", "audio", "file"])
        self.assertEqual(result.body.nodes[3].raw_data["asr_source"], "platform_external_content")
        self.assertIsNone(result.body.nodes[4].raw_data["size"])

    def test_unsafe_media_urls_are_not_fetchable_sources(self):
        for url in ("file:///etc/passwd", "data:image/png;base64,YQ==", "javascript:alert(1)",
                    "https://name:pass@example.invalid/a", "https://example.invalid:bad/a", "https://example.invalid/a\n"):
            with self.subTest(url=url):
                result = decode(event(attachments=[{"url": url, "content_type": "image/png"}]))
                media = next(node for node in result.body.nodes if isinstance(node, MediaNode))
                self.assertEqual(media.source, "")
                self.assertIn("unsafe_media_url", result.evidence.warnings)

    def test_quote_keeps_unknown_author_and_never_resolves_id(self):
        raw = event(message_type=103, message_scene={"ext": ["msg_idx=OWN", "ref_msg_idx=REF"]},
                    msg_elements=[{"msg_idx": "REF", "content": "<@TEST_M>引用内容",
                                   "msg_elements": [{"author": {"member_openid": "QUOTED_OTHER"}, "content": "子项"}]}],
                    mentions=[{"member_openid": "TEST_M", "is_you": True}])
        result = decode(raw)
        quoted = result.evidence.elements[0]
        self.assertIsNone(quoted.author)
        self.assertEqual(quoted.body.nodes, (TextNode(0, "<@TEST_M>引用内容"),))
        self.assertIsInstance(quoted.children[0].author, codec.AuthorEvidence)
        self.assertFalse(hasattr(quoted.children[0].author, "account"))
        self.assertEqual(result.reference.referenced_index, "REF")
        self.assertEqual(result.reference.endpoint, result.endpoint)
        self.assertFalse(hasattr(result, "reply_to_native_message_id"))

    def test_reference_exact_ids_and_invalid_conflicts_fail_closed(self):
        result = decode(event(message_scene={"ext": {"msg_idx": " OWN ", "ref_msg_idx": " REF "}}))
        self.assertEqual(result.reference.message_index, " OWN ")
        self.assertEqual(result.reference.referenced_index, " REF ")
        for ext in (["ref_msg_idx=REF", "ref_msg_idx=OTHER"], ["msg_idx=FIRST", "msg_idx=SECOND"],
                    ["ref_msg_idx=BAD\nINDEX"], ["ref_msg_idx=" + "x" * 257]):
            with self.subTest(ext=ext):
                result = decode(event(message_scene={"ext": ext}))
                self.assertTrue(result.reference.conflicting)
                self.assertIsNone(result.reference.message_index)
                self.assertIsNone(result.reference.referenced_index)

    def test_quote_without_element_structure_preserves_quote_evidence(self):
        result = decode(event(message_type=103, message_scene={"ext": ["ref_msg_idx=TEST_REF"]}))
        self.assertEqual(result.evidence.message_type, 103)
        self.assertEqual(result.evidence.elements, ())
        self.assertEqual(result.reference.referenced_index, "TEST_REF")
        self.assertIn("结构缺失", result.body.nodes[-1].description)

    def test_forward_order_author_attachments_and_roundtrip_preserved(self):
        result = decode(event(message_type=102, content="转发说明", msg_elements=[
            {"content": "第一条", "author": {"member_openid": "AUTHOR_A"},
             "attachments": [{"url": "https://media.example.invalid/a", "content_type": "image/png"}]},
            {"content": "第二条", "msg_elements": [{"content": "嵌套"}]},
        ]))
        forward = next(node for node in result.body.nodes if isinstance(node, ForwardNode))
        self.assertEqual(forward.native_id, "")
        self.assertEqual(forward.count, 2)
        children = forward.raw_data["elements"]
        self.assertEqual(children[0]["author"]["native_user_id"], "AUTHOR_A")
        self.assertIsNone(children[1]["author"])
        self.assertEqual(children[0]["body"]["nodes"][0]["text"], "第一条")
        self.assertEqual(body_from_json(body_to_json(result.body)), result.body)

    def test_forward_missing_structure_has_explicit_fallback(self):
        result = decode(event(message_type=102))
        node = next(node for node in result.body.nodes if isinstance(node, ForwardNode))
        self.assertEqual(node.description, "转发结构缺失")
        self.assertIsNone(node.count)

    def test_card_preserves_evidence_without_inventing_preview_link(self):
        ark = {"title": "标题", "source": "来源", "summary": "摘要", "preview": "https://example.invalid/image",
               "meta": {"unconfirmed_layout": "保留证据"}}
        result = decode(event(message_type=3, ark_data=ark))
        card = next(node for node in result.body.nodes if isinstance(node, CardNode))
        self.assertEqual(card.url, "")
        self.assertEqual(card.raw_data["ark_data"], ark)
        for url, expected in (("https://example.invalid/post", "https://example.invalid/post"), ("javascript:alert(1)", "")):
            result = decode(event(message_type=3, ark_data={**ark, "jump_url": url}))
            self.assertEqual(next(node for node in result.body.nodes if isinstance(node, CardNode)).url, expected)

    def test_unknown_message_type_keeps_text_and_bounded_placeholder(self):
        result = decode(event(message_type=999, future_field={"nested": "not promoted"}))
        self.assertEqual(result.body.nodes[0], TextNode(0, "测试消息"))
        self.assertIsInstance(result.body.nodes[-1], UnsupportedNode)
        self.assertEqual(result.evidence.unknown_field_names, ("future_field",))

    def test_bot_author_is_suppressed_without_constructing_bot_actor(self):
        result = decode(event(author={"member_openid": "TEST_BOT_SUPPLIED", "bot": True}))
        self.assertIsInstance(result, codec.RejectedMessage)
        self.assertEqual(result.disposition, "bot_origin")
        self.assertEqual(result.author_native_id, "TEST_BOT_SUPPLIED")
        self.assertFalse(hasattr(result, "actor"))
        for bot in (False, 1, "true", None):
            self.assertIsInstance(decode(event(author={"member_openid": "TEST_HUMAN", "bot": bot})), codec.DecodedMessage)

    def test_platform_owner_does_not_add_local_authority(self):
        result = decode(event(author={"member_openid": "TEST_MEMBER", "member_role": "owner"}))
        self.assertEqual(result.platform_role, "owner")
        self.assertEqual(result.actor.identity_kind, "group_member")
        self.assertFalse(hasattr(result, "owner"))

    def test_non_message_dispatch_has_no_body(self):
        for kind in ("READY", "GROUP_DEL_ROBOT", "C2C_MSG_REJECT", "INTERACTION_CREATE", "UNKNOWN_TEST"):
            raw = {"op": 0, "t": kind, "s": 1, "d": {"private": "DO_NOT_RETURN"}}
            result = decode(raw)
            self.assertEqual(result.disposition, "ignored")
            self.assertNotIn("DO_NOT_RETURN", repr(result))

    def test_timestamp_fallback_does_not_create_reply_window(self):
        for value, source in ((None, "received_missing"), ("bad", "received_missing"),
                              ("2026-10-28T10:00:00", "received_missing"),
                              ("2036-01-01T00:00:00Z", "received_future"),
                              ("2026-10-28T10:00:00Z", "platform")):
            with self.subTest(value=value):
                result = decode(event(timestamp=value))
                self.assertEqual(result.timestamp_source, source)
                self.assertLessEqual(result.occurred_at, NOW)
                self.assertFalse(hasattr(result, "expires_at"))

    def test_reparse_bounds_mutated_dispatch_and_ignores_stale_derived_fields(self):
        parsed = parse_dispatch(event())
        changed = replace(parsed, message=replace(parsed.message, content="STALE_FAKE"))
        self.assertEqual(codec.decode_message(changed, ACCOUNT, NOW).body.nodes[0].text, "测试消息")
        parsed.data["content"] = "x" * (300 * 1024)
        self.assertEqual(codec.decode_message(parsed, ACCOUNT, NOW).reason, "invalid_dispatch_payload")
        self.assertEqual(codec.decode_message(Dispatch("UNKNOWN", 1, None, data={"x": object()}), ACCOUNT, NOW).reason,
                         "invalid_dispatch_payload")

    def test_nul_anywhere_terminal_rejects_before_postgres(self):
        for body in ({"content": "text\x00text"}, {"ark_data": {"title": "\x00"}},
                     {"attachments": [{"filename": "file\x00name"}]}, {"future": {"\x00": "value"}}):
            self.assertEqual(decode(event(**body)).reason, "unsupported_nul_payload")

    def test_payload_copies_and_sensitive_repr(self):
        raw = event(content="PRIVATE_TEST_BODY", attachments=[{"url": "https://media.example.invalid/a?signature=PRIVATE"}])
        parsed = parse_dispatch(raw)
        result = codec.decode_message(parsed, ACCOUNT, NOW)
        raw["d"]["content"] = "changed"
        parsed.data["attachments"][0]["url"] = "changed"
        self.assertEqual(result.body.nodes[0].text, "PRIVATE_TEST_BODY")
        for value in ("PRIVATE", "TEST_MEMBER", "TEST_APP", "TEST_GROUP", "TEST_MSG"):
            self.assertNotIn(value, repr(result))
            self.assertNotIn(value, repr(result.evidence))

    def test_conversion_node_and_element_limits(self):
        result = decode(event(content=face() * 600))
        self.assertLessEqual(len(result.body.nodes), codec.MAX_NODES)
        self.assertIn("token_limit", result.evidence.warnings)
        result = decode(event(message_type=102, msg_elements=[{"content": "entry"} for _ in range(200)]))
        self.assertEqual(len(result.evidence.elements), codec.MAX_ELEMENTS)
        self.assertTrue(result.evidence.truncated)

    def test_no_network(self):
        with patch("socket.create_connection", side_effect=AssertionError("network forbidden")), \
             patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
            result = decode(event(attachments=[{"url": "http://127.0.0.1/private", "content_type": "image/png"}]))
        self.assertIsInstance(result, codec.DecodedMessage)

    def test_clean_process_import_and_conversion_rejects_all_nonebot_imports(self):
        script = f'''
import builtins
import sys
original_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == "nonebot" or name.startswith("nonebot."):
        raise AssertionError("SDK import forbidden")
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded
from src.plugins.ai_chat.adapters.qq_official import codec as module
raw = {event()!r}
result = module.decode_message(module.parse_dispatch(raw), module.AccountKey("qq-official", "TEST_APP", "test"), {NOW})
assert isinstance(result, module.DecodedMessage)
assert not any(name == "nonebot" or name.startswith("nonebot.") for name in sys.modules)
'''
        result = subprocess.run([sys.executable, "-B", "-c", script], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_configuration_is_explicit(self):
        for account, clock in ((AccountKey("onebot-v11", "123", "test"), NOW), (ACCOUNT, True), (ACCOUNT, -1),
                               (AccountKey("qq-official", "TEST\nAPP", "test"), NOW)):
            with self.assertRaises(ValueError):
                codec.decode_message(parse_dispatch(event()), account, clock)


if __name__ == "__main__":
    unittest.main()
