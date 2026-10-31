"""No database/network: conversational paragraph planning and frozen recovery."""
import hashlib
import json
import subprocess
import sys
import unittest

from src.plugins.ai_chat.adapters.qq_official.renderer import (
    RenderError, content_payload, render_plan, restore_plan,
)
from src.plugins.ai_chat.message_ir import MessageBody, MentionNode, TextNode
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey

ACCOUNT = AccountKey("qq-official", "TEST_PARAGRAPHS", "test")
ENDPOINT = EndpointKey(ACCOUNT, "group", "TEST_GROUP")
ACTOR = ActorKey(ACCOUNT, "group_member", "TEST_GROUP", "TEST_ACTOR")


def body(text):
    return MessageBody((TextNode(0, text),))


class ReplyParagraphTests(unittest.TestCase):
    def test_headings_keep_tables_lists_and_code_together(self):
        source = "引言\n\n背景\n\n### 数据\n|a|b|\n|-|-|\n|1|2|\n\n- 一\n- 二\n\n---\n\n### 代码\n```md\n### literal\n---\n```"
        plan = self.render(source)
        self.assertEqual(len(plan.parts), 4)
        self.assertIn("|1|2|\n\n- 一", plan.parts[2].markdown_content)
        self.assertNotIn("---", plan.parts[2].markdown_content)
        self.assertIn("### literal\n---", plan.parts[3].markdown_content)
        self.contents(plan)  # Frozen recovery retains the same chunks.

    def test_qq_markdown_spacing_math_and_long_section(self):
        plan = self.render("### 一\n**加粗** 空格  \n\\[x=1\\]", math_mode="dollar_delimiters")
        self.assertEqual(plan.parts[0].markdown_content, "### 一\n**加粗**\u00a0空格\n$$x=1$$")
        self.assertEqual(self.contents(self.render("### 长\n段落一\n\n段落二\n\n段落三", max_chars=12)),
                         ["### 长\n段落一", "段落二", "段落三"])

    def render(self, text, **options):
        return render_plan(body(text), ENDPOINT, paragraphs=True, **options)

    def contents(self, plan):
        self.assertEqual(plan.status, "ready")
        self.assertEqual([p.index for p in plan.parts], list(range(len(plan.parts))))
        for part in plan.parts:
            for text in (part.markdown_content, part.text_content):
                self.assertLessEqual(len(text), plan.max_chars)
                if plan.max_bytes is not None:
                    self.assertLessEqual(len(text.encode()), plan.max_bytes)
        self.assertEqual(plan.source_hash, hashlib.sha256(
            "".join(p.canonical_text for p in plan.parts).encode()).hexdigest())
        restored = restore_plan(plan.to_json(), endpoint=ENDPOINT,
            allowed_mentions=frozenset({ACTOR}), max_chars=plan.max_chars,
            max_parts=plan.max_parts, max_bytes=plan.max_bytes)
        self.assertEqual(restored.to_json(), plan.to_json())
        return [part.text_content for part in plan.parts]

    def test_blank_lines_and_marker_follow_legacy_reply_boundaries(self):
        source = "第一段\n\n第二段 [split] 第三段"
        expected = ["第一段", "第二段", "第三段"]
        self.assertEqual(expected, ["第一段", "第二段", "第三段"])
        self.assertEqual(self.contents(self.render(source)), expected)

    def test_single_newline_stays_in_same_paragraph(self):
        self.assertEqual(self.contents(self.render("标题\n正文\n- 项目一\n- 项目二")),
            ["标题\n正文\n- 项目一\n- 项目二"])

    def test_crlf_does_not_turn_every_line_into_paragraph(self):
        self.assertEqual(self.contents(self.render("第一行\r\n第二行\r\n \t\r\n第三段")),
            ["第一行\r\n第二行", "第三段"])

    def test_repeated_empty_paragraphs_do_not_emit_empty_messages(self):
        self.assertEqual(self.contents(self.render(" \n\n[split]一[split][split]\n\n二\n\n ")),
            ["一", "二"])

    def test_fenced_code_is_one_paragraph_with_literal_control_syntax(self):
        source = "说明\n```python\nprint('[split]')\n\nprint('ok')\n```\n结束"
        self.assertEqual(self.contents(self.render(source)), ["说明", "```python\nprint('[split]')\n\nprint('ok')\n```", "结束"])

    def test_tilde_fence_and_inline_code_are_not_paragraph_controls(self):
        self.assertEqual(self.contents(self.render("说明\n~~~text\na[split]b\n\nc\n~~~\n结束")),
            ["说明", "~~~text\na[split]b\n\nc\n~~~", "结束"])
        self.assertEqual(self.contents(self.render("`a[split]b` [split] 第二段")),
            ["`a[split]b`", "第二段"])

    def test_math_atom_retains_internal_blank_lines(self):
        self.assertEqual(self.contents(self.render("说明\n\n$$a\n\nb$$\n\n结论")),
            ["说明", "$$a\n\nb$$", "结论"])

    def test_unterminated_fence_is_closed_without_splitting_its_control_text(self):
        plan = self.render("说明\n```text\na[split]b\n\nc")
        self.assertEqual(self.contents(plan), ["说明", "```text\na[split]b\n\nc"])
        self.assertTrue(plan.parts[-1].markdown_content.endswith("\n```"))

    def test_overflow_paragraphs_merge_in_last_slot_without_losing_tail(self):
        source = "\n\n".join(f"段{i}" for i in range(7))
        self.assertEqual(self.contents(self.render(source)),
            ["段0", "段1", "段2", "段3\n\n段4\n\n段5\n\n段6"])

    def test_one_slot_keeps_all_paragraphs_separated(self):
        self.assertEqual(self.contents(self.render("一[split]二\n\n三", max_parts=1)), ["一\n\n二\n\n三"])

    def test_overflow_tail_that_cannot_fit_never_produces_partial_plan(self):
        plan = self.render("a\n\nb\n\nc\n\n" + "x" * 30, max_chars=12)
        self.assertEqual((plan.status, plan.reason, plan.parts), ("needs_attachment", "part_limit", ()))

    def test_long_paragraph_still_fragments_by_hard_character_limit(self):
        self.assertEqual(self.contents(self.render("a" * 25 + "\n\n尾段", max_chars=12)),
            ["a" * 12, "a" * 12, "a", "尾段"])

    def test_utf8_budget_and_graphemes_are_preserved(self):
        plan = self.render("中文中文\n\n末段", max_bytes=9)
        self.assertEqual(self.contents(plan), ["中文中", "文", "末段"])
        cluster = "👩🏽‍💻"
        self.assertEqual(self.contents(self.render(cluster * 2 + "\n\n末", max_chars=len(cluster))),
            [cluster, cluster, "末"])

    def test_indivisible_atom_fails_as_a_whole(self):
        plan = self.render("第一段\n\n`" + "x" * 30 + "`", max_chars=20)
        self.assertEqual((plan.status, plan.reason, plan.parts),
            ("needs_attachment", "indivisible_unit_limit", ()))

    def test_structured_mentions_remain_bound_to_their_original_actor(self):
        value = MessageBody((TextNode(0, "第一段\n\n"),
            MentionNode(1, ACTOR.native_user_id, "成员[split]名字"), TextNode(2, "\n\n第三段")))
        plan = render_plan(value, ENDPOINT, paragraphs=True, allowed_mentions=frozenset({ACTOR}))
        self.assertEqual(self.contents(plan), ["第一段", "@成员[split]名字", "第三段"])
        self.assertEqual([part.mention_actors for part in plan.parts], [(), (ACTOR,), ()])
        self.assertEqual(content_payload(plan, 1)["markdown"]["content"],
            '<qqbot-at-user id="TEST_ACTOR" />')
        with self.assertRaisesRegex(RenderError, "mention_authority_changed"):
            restore_plan(plan.to_json(), endpoint=ENDPOINT)

    def test_model_quote_and_platform_tags_never_create_authority(self):
        source = '[reply#123] 第一段\n\n<qqbot-at-user id="TEST_ACTOR" />'
        plan = self.render(source)
        self.assertEqual(len(plan.parts), 2)
        self.assertIn("[reply#123]", plan.parts[0].text_content)
        self.assertIn("&lt;qqbot", plan.parts[1].markdown_content)
        self.assertTrue(all(not part.mention_actors for part in plan.parts))
        self.assertNotIn("message_reference", json.dumps(content_payload(plan, 0)))

    def test_default_and_old_manifest_recovery_are_unchanged(self):
        text = "第一段\n\n第二段[split]第三段"
        old = render_plan(body(text), ENDPOINT)
        self.assertEqual([p.text_content for p in old.parts], [text])
        self.assertEqual(restore_plan(old.to_json(), endpoint=ENDPOINT).to_json(), old.to_json())
        self.assertNotEqual(old.to_json(), self.render(text).to_json())

    def test_plain_fallback_uses_exact_frozen_paragraphs(self):
        plan = self.render("**第一段**\n\n第二段")
        self.assertEqual([content_payload(plan, i, plain_fallback=True)["content"] for i in range(2)],
            ["**第一段**", "第二段"])

    def test_paragraph_switch_requires_strict_boolean(self):
        with self.assertRaisesRegex(RenderError, "invalid_render_options"):
            render_plan(body("第一段"), ENDPOINT, paragraphs=1)

    def test_import_and_plan_do_not_load_onebot_or_nonebot(self):
        code = '''import sys
class NoSDK:
    def find_spec(self, name, *args):
        if name == "nonebot" or name.startswith("nonebot."):
            raise AssertionError("SDK must not load")
sys.meta_path.insert(0, NoSDK())
from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey
plan = render_plan(MessageBody((TextNode(0, "first\\n\\nsecond"),)),
    EndpointKey(AccountKey("qq-official", "TEST", "test"), "group", "TEST"), paragraphs=True)
assert [p.text_content for p in plan.parts] == ["first", "second"]
assert not any(k.startswith("nonebot") for k in sys.modules)
'''
        result = subprocess.run([sys.executable, "-c", code], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
