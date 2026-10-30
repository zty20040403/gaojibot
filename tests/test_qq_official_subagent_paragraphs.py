"""Pure regression: soft paragraph budget does not reduce hard wire capacity."""
import unittest

from src.plugins.ai_chat.adapters.qq_official.renderer import RenderError, render_plan, restore_plan
from src.plugins.ai_chat.adapters.qq_official.subagent_commands import _view
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey

ENDPOINT = EndpointKey(AccountKey('qq-official', 'TEST_SOFT_PARTS', 'test'), 'group', 'TEST_GROUP')


def render(text, **options):
    return render_plan(MessageBody((TextNode(0, text),)), ENDPOINT, **options)


class SubagentParagraphTests(unittest.TestCase):
    def test_short_four_or_five_paragraphs_use_three_parts_without_losing_tail(self):
        for count in (4, 5):
            with self.subTest(count=count):
                parts = [f'段{i}' for i in range(count)]
                plan = render('[split]'.join(parts), paragraphs=True, paragraph_parts=3)
                self.assertEqual(plan.status, 'ready')
                self.assertEqual(plan.max_parts, 4)
                self.assertEqual([p.text_content for p in plan.parts],
                    [parts[0], parts[1], '\n\n'.join(parts[2:])])

    def test_original_six_to_eight_thousand_character_capacity_is_preserved(self):
        for size in (6000, 6500, 7000, 8000):
            with self.subTest(size=size):
                text = '甲' * size
                plan = render(text, paragraphs=True, paragraph_parts=3)
                self.assertEqual(plan.status, 'ready')
                self.assertEqual(''.join(p.text_content for p in plan.parts), text)
                self.assertLessEqual(len(plan.parts), 4)
                self.assertTrue(all(len(p.markdown_content) <= 2000 for p in plan.parts))

    def test_short_leading_paragraphs_cannot_waste_the_old_hard_capacity(self):
        text = '一\n\n二\n\n' + '甲' * 7994
        plan = render(text, paragraphs=True, paragraph_parts=3)
        self.assertEqual(plan.status, 'ready')
        self.assertEqual(len(plan.parts), 4)
        self.assertEqual(''.join(p.text_content for p in plan.parts), text)
        self.assertEqual(restore_plan(plan.to_json(), endpoint=ENDPOINT).to_json(), plan.to_json())

    def test_hard_character_and_byte_overflow_never_return_a_partial_plan(self):
        for text, options in [('甲' * 8001, {}), ('甲' * 4001, {'max_bytes': 3000})]:
            with self.subTest(options=options):
                plan = render(text, paragraphs=True, paragraph_parts=3, **options)
                self.assertEqual((plan.status, plan.reason, plan.parts), ('needs_attachment', 'part_limit', ()))

    def test_default_old_plan_and_default_natural_budget_remain_unchanged(self):
        text = '一\n\n二[split]三\n\n四'
        old = render(text)
        self.assertEqual([p.text_content for p in old.parts], [text])
        self.assertEqual(restore_plan(old.to_json(), endpoint=ENDPOINT).to_json(), old.to_json())
        self.assertEqual([p.text_content for p in render(text, paragraphs=True).parts], ['一', '二', '三', '四'])
        for invalid in (True, 0, 5, 3.0, '3'):
            with self.subTest(invalid=invalid), self.assertRaises(RenderError):
                render(text, paragraphs=True, paragraph_parts=invalid)

    def test_owner_task_status_explicitly_distinguishes_unrepresentable_result(self):
        row = dict(task_id=1, status='completed', created_at=1, finished_at=2,
                   objective='TEST task', result_suppressed_reason='result_needs_attachment')
        view = _view(row, 3)
        self.assertIn('未发送：结果超过消息上限，完整结果已保留', view.summary)
        self.assertTrue(view.summary.startswith('completed/'))
        row['result_suppressed_reason'] = 'TEST unknown stored reason'
        self.assertEqual(_view(row, 3).summary, 'completed · TEST task')
