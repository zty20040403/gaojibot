from dataclasses import FrozenInstanceError
import hashlib
import json
import random
import re
import subprocess
import sys
import unittest

from src.plugins.ai_chat.message_ir import (
    CardNode, EmoteNode, ForwardNode, MediaNode, MentionNode, MessageBody, TextNode, UnsupportedNode,
)
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey
from src.plugins.ai_chat.adapters.qq_official.renderer import (
    RenderError, RenderedPlan, content_payload, render_plan, restore_plan,
)


ACCOUNT = AccountKey("qq-official", "TEST_RENDER_APP", "test")
GROUP = EndpointKey(ACCOUNT, "group", "TEST_GROUP")
MEMBER = ActorKey(ACCOUNT, "group_member", "TEST_GROUP", "TEST_MEMBER")
ALLOWED = frozenset({MEMBER})


def body(text):
    return MessageBody((TextNode(0, text),))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class RendererTests(unittest.TestCase):
    def plan(self, text, **kwargs):
        return render_plan(body(text), GROUP, **kwargs)

    def assert_ready(self, plan, source=None):
        self.assertEqual(plan.status, "ready")
        self.assertGreater(len(plan.parts), 0)
        for index, part in enumerate(plan.parts):
            self.assertEqual(part.index, index)
            self.assertLessEqual(len(part.markdown_content), plan.max_chars)
            self.assertLessEqual(len(part.text_content), plan.max_chars)
            if plan.max_bytes is not None:
                self.assertLessEqual(len(part.markdown_content.encode()), plan.max_bytes)
                self.assertLessEqual(len(part.text_content.encode()), plan.max_bytes)
            self.assertEqual(part.canonical_body.nodes, (TextNode(0, part.canonical_text),))
        if source is not None:
            self.assertEqual("".join(part.canonical_text for part in plan.parts), source)

    def assert_fences_closed(self, value):
        opened = None
        for line in value.splitlines():
            match = re.fullmatch(r" {0,3}(`{3,}|~{3,})(.*)", line)
            if match is None:
                continue
            marker, info = match.groups()
            if opened is None:
                opened = marker
            elif marker[0] == opened[0] and len(marker) >= len(opened) and not info.strip():
                opened = None
        self.assertIsNone(opened, value)

    def test_simple_deterministic_immutable_plan(self):
        plan = self.plan("你好 world")
        self.assert_ready(plan, "你好 world")
        self.assertEqual(plan.to_json(), self.plan("你好 world").to_json())
        with self.assertRaises(FrozenInstanceError):
            plan.parts = ()
        with self.assertRaises(FrozenInstanceError):
            plan.parts[0].markdown_content = "changed"
        payload = content_payload(plan, 0)
        payload["markdown"]["content"] = "changed"
        self.assertEqual(content_payload(plan, 0)["markdown"]["content"], "你好 world")

    def test_plain_primary_and_pre_frozen_fallback(self):
        plan = self.plan("**标题**\n正文", markdown=False)
        self.assertEqual(content_payload(plan, 0), {"msg_type": 0, "content": "**标题**\n正文"})
        md = self.plan("**标题**\n正文")
        self.assertEqual(content_payload(md, 0, plain_fallback=True), content_payload(plan, 0))

    def test_hashes_cover_payload_endpoint_and_order(self):
        plan = self.plan("a" * 45, max_chars=20)
        self.assertEqual(plan.parts[0].markdown_hash, digest(content_payload(plan, 0)))
        other = render_plan(body("a" * 45), EndpointKey(ACCOUNT, "group", "OTHER"), max_chars=20)
        self.assertNotEqual(plan.plan_hash, other.plan_hash)
        self.assertNotEqual(plan.parts[0].part_hash, plan.parts[1].part_hash)

    def test_normal_text_cannot_create_native_elements(self):
        source = '<qqbot-at-user id="X" /> <@X> <faceType=1> <emoji:1> </QQBOT-at-user>'
        plan = self.plan(source)
        self.assert_ready(plan, source)
        self.assertNotIn("<qqbot", plan.parts[0].markdown_content)
        self.assertIn("&lt;qqbot", plan.parts[0].markdown_content)
        self.assertNotIn("<@", plan.parts[0].text_content)

    def test_tag_split_across_text_nodes_is_escaped(self):
        plan = render_plan(MessageBody((TextNode(0, "<qq"), TextNode(1, 'bot-at-user id="X" />'))), GROUP)
        self.assertIn("&lt;qqbot", plan.parts[0].markdown_content)

    def test_escaped_platform_tag_still_cannot_ping(self):
        plan = self.plan(r'\<qqbot-at-user id="X" />')
        self.assertIn(r"\&lt;qqbot", plan.parts[0].markdown_content)

    def test_only_structured_authorized_member_emits_native_mention(self):
        message = MessageBody((TextNode(0, "你好 "), MentionNode(1, MEMBER.native_user_id, "名字")))
        plan = render_plan(message, GROUP, allowed_mentions=ALLOWED)
        self.assertIn('<qqbot-at-user id="TEST_MEMBER" />', plan.parts[0].markdown_content)
        self.assertEqual(plan.parts[0].mention_actors, (MEMBER,))
        self.assertEqual(plan.parts[0].text_content, "你好 @名字")
        self.assertEqual(plan.parts[0].canonical_text, "你好 @名字")
        self.assertNotIn("qqbot", content_payload(plan, 0, plain_fallback=True)["content"])

    def test_same_literal_member_different_app_env_or_group_not_authority(self):
        message = MessageBody((MentionNode(0, MEMBER.native_user_id, "名字"),))
        for account, group in ((AccountKey("qq-official", "OTHER", "test"), "TEST_GROUP"),
                               (AccountKey("qq-official", ACCOUNT.app_id, "sandbox"), "TEST_GROUP"),
                               (ACCOUNT, "OTHER_GROUP")):
            with self.subTest(account=account, group=group):
                fake = ActorKey(account, "group_member", group, MEMBER.native_user_id)
                plan = render_plan(message, GROUP, allowed_mentions=frozenset({fake}))
                self.assertNotIn("<qqbot", plan.parts[0].markdown_content)
                self.assertEqual(plan.parts[0].mention_actors, ())

    def test_private_and_bot_actor_never_authorize_group_mention(self):
        message = MessageBody((MentionNode(0, MEMBER.native_user_id, "名字"),))
        private = EndpointKey(ACCOUNT, "private", MEMBER.native_user_id)
        bot = ActorKey(ACCOUNT, "bot", "", MEMBER.native_user_id)
        for endpoint, allowed in ((private, ALLOWED), (GROUP, frozenset({bot}))):
            plan = render_plan(message, endpoint, allowed_mentions=allowed)
            self.assertNotIn("<qqbot", plan.parts[0].markdown_content)

    def test_attribute_injection_member_falls_back_without_identity_exposure(self):
        native = 'X" /><qqbot-at-all />'
        actor = ActorKey(ACCOUNT, "group_member", "TEST_GROUP", native)
        plan = render_plan(MessageBody((MentionNode(0, native, "名字"),)), GROUP, allowed_mentions=frozenset({actor}))
        self.assertEqual(plan.parts[0].text_content, "@名字")
        self.assertNotIn(native, plan.to_json())

    def test_authorized_mentions_are_atomic(self):
        message = MessageBody((TextNode(0, "a" * 20), MentionNode(1, MEMBER.native_user_id, "名字")))
        plan = render_plan(message, GROUP, max_chars=40, allowed_mentions=ALLOWED)
        self.assert_ready(plan)
        self.assertEqual(len(plan.parts), 2)
        self.assertNotIn("<qqbot", plan.parts[0].markdown_content)
        self.assertTrue(plan.parts[1].markdown_content.startswith("<qqbot"))

    def test_mention_inside_code_is_literal_text(self):
        message = MessageBody((TextNode(0, "```txt\n"), MentionNode(1, MEMBER.native_user_id, "名字"), TextNode(2, "\n```")))
        plan = render_plan(message, GROUP, allowed_mentions=ALLOWED)
        self.assertNotIn("<qqbot", plan.parts[0].markdown_content)
        self.assertEqual(plan.parts[0].mention_actors, ())

    def test_named_and_unnamed_emotes_become_readable_text(self):
        plan = render_plan(MessageBody((EmoteNode(0, "306", "牛气冲天"), EmoteNode(1, "42"))), GROUP)
        self.assertEqual(plan.parts[0].text_content, "[牛气冲天][表情42]")

    def test_media_requires_attachment_and_returns_no_partial_text(self):
        plan = render_plan(MessageBody((TextNode(0, "answer"), MediaNode(1, "file", name="secret.txt"))), GROUP)
        self.assertEqual((plan.status, plan.reason, plan.parts), ("needs_attachment", "media_requires_attachment", ()))
        with self.assertRaises(RenderError):
            content_payload(plan, 0)

    def test_card_and_forward_are_labeled_text_not_native_claims(self):
        plan = render_plan(MessageBody((CardNode(0, "标题", "https://example.invalid/a"), ForwardNode(1, "id", 3, "摘要"))), GROUP)
        self.assertEqual(plan.warnings, ("card_as_text", "forward_as_text"))
        self.assertIn("卡片摘要", plan.parts[0].text_content)
        self.assertIn("合并转发摘要", plan.parts[0].text_content)
        self.assertEqual(content_payload(plan, 0)["msg_type"], 2)

    def test_unsupported_node_fails_explicitly(self):
        with self.assertRaisesRegex(RenderError, "unsupported_node"):
            render_plan(MessageBody((UnsupportedNode(0, "alien", "secret"),)), GROUP)

    def test_long_result_never_silently_truncates(self):
        plan = self.plan("x" * 81, max_chars=20, max_parts=4)
        self.assertEqual((plan.status, plan.reason, plan.parts), ("needs_attachment", "part_limit", ()))
        self.assertNotIn("文件已", repr(plan))

    def test_exact_limit_and_four_parts_preserve_all_source(self):
        plan = self.plan("x" * 80, max_chars=20, max_parts=4)
        self.assert_ready(plan, "x" * 80)
        self.assertEqual(len(plan.parts), 4)

    def test_fenced_code_is_closed_and_reopened_with_language(self):
        source = "```python\n" + "print('测试')\n" * 8 + "```\n结束"
        plan = self.plan(source, max_chars=60)
        self.assert_ready(plan, source)
        self.assertGreater(len(plan.parts), 1)
        for part in plan.parts:
            self.assert_fences_closed(part.markdown_content)
        self.assertTrue(plan.parts[1].markdown_content.startswith("```python\n"))

    def test_tilde_and_long_backtick_fences_preserved(self):
        for fence in ("~~~~", "````"):
            source = fence + "text\n" + "x" * 100 + "\n" + fence
            plan = self.plan(source, max_chars=48)
            self.assert_ready(plan, source)
            for part in plan.parts:
                self.assert_fences_closed(part.markdown_content)

    def test_unclosed_fence_closed_on_wire_but_source_not_fabricated(self):
        source = "```py\nx=1"
        plan = self.plan(source)
        self.assert_ready(plan, source)
        self.assertTrue(plan.parts[0].markdown_content.endswith("\n```"))
        self.assertEqual(plan.warnings, ("closed_unterminated_fence",))
        self.assertEqual(plan.parts[0].canonical_text, source)

    def test_inline_code_is_atomic_or_requests_attachment(self):
        source = "prefix `code with spaces` suffix"
        plan = self.plan(source, max_chars=25)
        self.assert_ready(plan, source)
        self.assertTrue(any("`code with spaces`" in part.markdown_content for part in plan.parts))
        self.assertEqual(self.plan("`" + "x" * 50 + "`", max_chars=25).reason, "indivisible_unit_limit")

    def test_split_cannot_create_new_fence_from_midline_literal_marker(self):
        for marker in ("```", "~~~"):
            source = "x" * 20 + marker + "not a fence"
            plan = self.plan(source, max_chars=20)
            self.assert_ready(plan, source)
            for part in plan.parts:
                self.assert_fences_closed(part.markdown_content)
            self.assertIn("\\" + marker[0], plan.parts[1].markdown_content)

    def test_multiline_inline_code_remains_inline_at_new_part_start(self):
        source = "prefix " + "```code\nsecond```"
        plan = self.plan(source, max_chars=20)
        self.assert_ready(plan, source)
        self.assertEqual(plan.parts[1].markdown_content, "```code second```")

    def test_indented_code_splits_only_between_lines(self):
        source = "    one\n    two\n    three"
        plan = self.plan(source, max_chars=13)
        self.assert_ready(plan, source)
        self.assertTrue(all(part.markdown_content.startswith("    ") for part in plan.parts))
        self.assertEqual(self.plan("    " + "x" * 30, max_chars=20).reason, "indivisible_unit_limit")

    def test_math_preserved_by_default_and_explicit_conversion(self):
        source = r"内联 \(x^2\) 块 \[y+1\]"
        self.assertEqual(self.plan(source).parts[0].markdown_content, source)
        plan = self.plan(source, math_mode="dollar_delimiters")
        self.assertEqual(plan.parts[0].markdown_content, "内联 $x^2$ 块 $$y+1$$")
        self.assertEqual(plan.parts[0].text_content, source)
        self.assertEqual(plan.parts[0].canonical_text, source)

    def test_math_conversion_skips_fenced_inline_and_escaped_text(self):
        source = "```tex\n\\(a\\)\n```\n`\\(b\\)` \\\\(c\\\\) \\(d\\)"
        plan = self.plan(source, math_mode="dollar_delimiters")
        output = plan.parts[0].markdown_content
        self.assertIn("\\(a\\)", output)
        self.assertIn("`\\(b\\)`", output)
        self.assertIn("\\\\(c\\\\)", output)
        self.assertTrue(output.endswith("$d$"))

    def test_math_with_dollar_inside_is_not_rewritten(self):
        source = r"\(x + $y$\)"
        self.assertEqual(self.plan(source, math_mode="dollar_delimiters").parts[0].markdown_content, source)

    def test_math_and_mentions_inside_indented_code_are_literal(self):
        source = "    \\(a\\)\n\t\\[b\\]\n\\(c\\)"
        plan = self.plan(source, math_mode="dollar_delimiters")
        self.assertEqual(plan.parts[0].markdown_content, "    \\(a\\)\n\t\\[b\\]\n$c$")
        message = MessageBody((TextNode(0, "    "), MentionNode(1, MEMBER.native_user_id, "名字")))
        plan = render_plan(message, GROUP, allowed_mentions=ALLOWED)
        self.assertNotIn("<qqbot", plan.parts[0].markdown_content)

    def test_combining_mark_after_semantic_token_stays_with_token(self):
        source = "aaaaa`code`\u0301zzz"
        plan = self.plan(source, max_chars=10)
        self.assert_ready(plan, source)
        self.assertTrue(any("`code`\u0301" in part.canonical_text for part in plan.parts))

    def test_unicode_clusters_do_not_split(self):
        clusters = ("e\u0301", "👩🏽‍💻", "🇬🇧", "1️⃣", "क्‍ष", "각", "\r\n")
        for cluster in clusters:
            with self.subTest(cluster=cluster):
                source = "a" * 9 + cluster + "z" * 8
                plan = self.plan(source, max_chars=10)
                self.assert_ready(plan, source)
                self.assertTrue(any(cluster in part.canonical_text for part in plan.parts))

    def test_oversize_grapheme_requests_attachment(self):
        plan = self.plan("x" + "\u0301" * 25, max_chars=20)
        self.assertEqual(plan.reason, "indivisible_unit_limit")
        self.assertEqual(plan.parts, ())

    def test_unicode_prepend_and_halfwidth_voicing_stay_together(self):
        for cluster in ("\u0d4eഅ", "ｶﾞ", "\U00011f02\U00011f04"):
            source = "a" * 9 + cluster + "z"
            plan = self.plan(source, max_chars=10)
            self.assert_ready(plan, source)
            self.assertTrue(any(cluster in part.canonical_text for part in plan.parts))

    def test_unicode_byte_budget_independent_of_character_budget(self):
        plan = self.plan("中文" * 10, max_chars=100, max_bytes=18)
        self.assert_ready(plan, "中文" * 10)
        self.assertEqual(len(plan.parts), 4)

    def test_restore_roundtrip_and_current_authority(self):
        plan = render_plan(MessageBody((MentionNode(0, MEMBER.native_user_id, "名字"),)), GROUP, allowed_mentions=ALLOWED)
        restored = restore_plan(plan.to_json(), endpoint=GROUP, allowed_mentions=ALLOWED)
        self.assertEqual(plan, restored)
        self.assertEqual(content_payload(restored, 0), content_payload(plan, 0))
        with self.assertRaisesRegex(RenderError, "mention_authority_changed"):
            restore_plan(plan.to_json(), endpoint=GROUP)

    def test_restore_rejects_different_endpoint_and_environment(self):
        plan = self.plan("test")
        for endpoint in (EndpointKey(ACCOUNT, "private", "TEST_GROUP"),
                         EndpointKey(AccountKey("qq-official", ACCOUNT.app_id, "sandbox"), "group", "TEST_GROUP")):
            with self.assertRaisesRegex(RenderError, "manifest_identity_mismatch"):
                restore_plan(plan.to_json(), endpoint=endpoint)

    def test_restore_rejects_tampering_order_and_extra_fields(self):
        plan = self.plan("a" * 30, max_chars=20)
        for mutation in (lambda data: data["parts"][0].update(markdown_content="changed"),
                         lambda data: data["parts"].reverse(),
                         lambda data: data.update(unknown="extra")):
            data = json.loads(plan.to_json())
            mutation(data)
            with self.assertRaises(RenderError):
                restore_plan(json.dumps(data), endpoint=GROUP)

    def test_restore_hash_cannot_authorize_injected_raw_markup(self):
        plan = self.plan("safe")
        data = json.loads(plan.to_json())
        data["parts"][0]["markdown_content"] = '<qqbot-at-user id="TEST_MEMBER" />'
        with self.assertRaisesRegex(RenderError, "untrusted_markup"):
            restore_plan(json.dumps(data), endpoint=GROUP, allowed_mentions=ALLOWED)

    def test_restore_bounds_and_non_sendable_manifest(self):
        plan = self.plan("abc")
        with self.assertRaisesRegex(RenderError, "manifest_limits_changed"):
            restore_plan(plan.to_json(), endpoint=GROUP, max_chars=10)
        with self.assertRaisesRegex(RenderError, "manifest_not_sendable"):
            restore_plan(self.plan("x" * 100, max_chars=10).to_json(), endpoint=GROUP)

    def test_payload_helper_rejects_unvalidated_objects(self):
        for value in ("<qqbot-at-all />", {}, RenderedPlan()):
            with self.assertRaises(RenderError):
                content_payload(value, 0)

    def test_repr_and_validation_errors_do_not_leak_text_or_ids(self):
        plan = self.plan("SECRET_BODY_SENTINEL")
        self.assertNotIn("SECRET_BODY_SENTINEL", repr(plan))
        self.assertNotIn("TEST_GROUP", repr(plan))
        with self.assertRaisesRegex(RenderError, "invalid_unicode"):
            self.plan("SECRET\ud800")

    def test_repr_never_exposes_enumerable_otp_hashes(self):
        plan = self.plan("123456")
        part = plan.parts[0]
        for rendered in (repr(plan), repr(part)):
            for secret in ("123456", plan.source_hash, plan.plan_hash,
                           part.part_hash, part.markdown_hash, part.text_hash):
                self.assertNotIn(secret, rendered)

    def test_invalid_options_are_safe_errors(self):
        for kwargs in ({"max_chars": True}, {"max_chars": 0}, {"max_parts": 0},
                       {"max_parts": 17}, {"max_bytes": False}, {"markdown": "yes"},
                       {"math_mode": "unsafe"}, {"allowed_mentions": {MEMBER}}):
            with self.subTest(kwargs=kwargs), self.assertRaises(RenderError):
                self.plan("test", **kwargs)
        for text in ("", "  ", "\x00"):
            with self.assertRaises(RenderError):
                self.plan(text)

    def test_seeded_plain_text_lossless_boundary_stress(self):
        randomizer = random.Random(892)
        alphabet = ("x", "中文", "e\u0301", "👩🏽‍💻", "🇨🇳", "क्‍ष", "각", "\r\n")
        for _ in range(80):
            source = "".join(randomizer.choice(alphabet) for _ in range(12))
            plan = self.plan(source, max_chars=25, max_parts=16)
            self.assert_ready(plan, source)
            restored = restore_plan(plan.to_json(), endpoint=GROUP, max_parts=16)
            self.assertEqual(plan, restored)

    def test_clean_subprocess_import_does_not_load_sdk_or_runtime(self):
        script = "import sys; import src.plugins.ai_chat.adapters.qq_official.renderer; assert not any(k == 'nonebot' or k.startswith('nonebot.') for k in sys.modules)"
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
