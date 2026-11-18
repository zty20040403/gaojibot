"""A bounded, text-only report format; report contents are data, not commands."""
import re


_START = '[纯文本报告 v1]'
_END = '[/纯文本报告]'
_ITEM = re.compile(r'^(结论|证据) ([1-9][0-9]*): (\S.*)$')


def text_report(report):
    if not isinstance(report, str) or len(report) > 2000 or '\r' in report or '\x00' in report:
        return None
    lines = report.removesuffix('\n').split('\n')
    if (len(lines) < 5 or lines[0] not in ('结果：成功', '结果：部分完成')
            or lines[1] != _START or lines[-1] != _END or (len(lines) - 3) % 2):
        return None
    count = (len(lines) - 3) // 2
    if count > 12:
        return None
    conclusions, evidence = [], []
    for index in range(count):
        conclusion = _ITEM.fullmatch(lines[2 + index * 2])
        source = _ITEM.fullmatch(lines[3 + index * 2])
        number = str(index + 1)
        if not conclusion or not source or conclusion.group(1, 2) != ('结论', number) or source.group(1, 2) != ('证据', number):
            return None
        conclusions.append(lines[2 + index * 2])
        evidence.append(lines[3 + index * 2])
    return {'outcome': lines[0], 'conclusions': conclusions, 'evidence': evidence}


def report_notice(job_id, raw):
    if _START not in raw[:80].splitlines()[:2]:
        return None
    parsed = text_report(raw)
    if parsed is None:
        return f'job#{job_id} 的纯文本报告格式不完整，未提取结论；原报告已保留。'
    return (f"job#{job_id} 后台报告原文（不代表文件发送回执）\n{parsed['outcome']}\n"
            + '\n'.join(parsed['conclusions']) + f'\n可问我展开 job#{job_id} 的来源。')
