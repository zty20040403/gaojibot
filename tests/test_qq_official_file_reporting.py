"""Content acceptance and file message acceptance remain separate facts."""
import unittest

from src.plugins.ai_chat.agent.outcomes import outcome_report


class OfficialFileReportingTests(unittest.TestCase):
    def setUp(self):
        self.validation = {'status': 'passed', 'task_outcome': {
            'status': 'passed', 'criteria': [{'status': 'passed', 'description': 'PDF内容检查',
                'reason': '已独立检查', 'kind': 'evidence'}]}}

    def test_transport_pending_or_unknown_does_not_undo_content_acceptance(self):
        for state in ('stored', 'upload_unknown', 'publication_unknown', 'delivery_unknown', 'expired'):
            with self.subTest(state=state):
                result = outcome_report(self.validation, '不应重复旧的模型交付结论', [{
                    'official_artifact': True, 'filename': 'test.pdf', 'state': state,
                    'ok': False, 'message_accepted': False}])
                self.assertIn('内容已完成并核实 1 项验收。', result)
                self.assertIn('文件消息：0/1 个已被平台接受。', result)
                self.assertNotIn('任务尚未全部完成', result)
                self.assertNotIn('不应重复', result)

    def test_accepted_message_does_not_claim_download_or_pass_failed_content(self):
        validation = {**self.validation, 'status': 'failed'}
        result = outcome_report(validation, '', [{
            'official_artifact': True, 'filename': '草稿-test.pdf', 'state': 'message_accepted',
            'ok': True, 'message_accepted': True, 'draft': True, 'draft_reason': '图表尚缺'}])
        self.assertIn('独立复核仍有未通过项', result)
        self.assertIn('文件消息：1/1 个已被平台接受。', result)
        self.assertIn('图表尚缺', result)
        self.assertNotIn('用户已下载', result)
        self.assertNotIn('内容已完成', result)

    def test_legacy_file_waiting_heading_unchanged(self):
        result = outcome_report(self.validation, '', [{'ok': False, 'state': 'queued'}])
        self.assertIn('已核实 1 项验收，但任务尚未全部完成。', result)


if __name__ == '__main__':
    unittest.main()
