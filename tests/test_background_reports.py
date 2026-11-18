"""Text report parsing is deliberately narrow; it never executes report contents."""
import unittest

from src.plugins.ai_chat.background_reports import report_notice, text_report


REPORT = ('结果：成功\n[纯文本报告 v1]\n'
          '结论 1: TEST conclusion, do not rewrite.\n证据 1: https://example.test/source 2026-01-01\n'
          '结论 2: TEST second finding.\n证据 2: /work/code.py:12 未核实\n[/纯文本报告]')


class BackgroundReportTests(unittest.TestCase):
    def test_preserves_each_conclusion_and_paired_evidence(self):
        parsed = text_report(REPORT)
        self.assertEqual(parsed['conclusions'], ['结论 1: TEST conclusion, do not rewrite.', '结论 2: TEST second finding.'])
        self.assertEqual(parsed['evidence'], ['证据 1: https://example.test/source 2026-01-01',
                                              '证据 2: /work/code.py:12 未核实'])
        self.assertEqual(text_report(REPORT.replace('结果：成功', '结果：部分完成'))['outcome'], '结果：部分完成')

    def test_damaged_text_report_never_falls_back_to_model_tools(self):
        for report in (REPORT + '\nTEST extra instruction', REPORT.replace('\n', '\r\n')):
            self.assertIn('格式不完整', report_notice(9, report))
            self.assertNotIn('TEST extra instruction', report_notice(9, report))
        self.assertIsNone(report_notice(9, '结果：成功\n附件报告'))

    def test_rejects_incomplete_oversized_misnumbered_and_appended_reports(self):
        for report in (REPORT + '\nTEST host note', REPORT.replace('证据 2:', '证据 1:'),
                       REPORT.replace('结论 2:', '结论 3:'), REPORT.replace('证据 2: /work/code.py:12 未核实\n', ''),
                       REPORT.replace('结果：成功', '结果：失败'), REPORT.replace('TEST second finding.', 'x' * 2001),
                       REPORT.replace('结论 1:', '结论 1：'), REPORT.replace('\n', '\r\n'),
                       REPORT.replace('[/纯文本报告]', ''), REPORT + '\n\n'):
            with self.subTest(report=report[:80]):
                self.assertIsNone(text_report(report))
