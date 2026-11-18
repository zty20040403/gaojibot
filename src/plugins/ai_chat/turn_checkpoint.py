"""Loop snapshots exclude base messages and images; the host gates unsafe turns.

Storage, atomic replacement, fencing, retention and eligibility are host-owned.
Secret detection uses the turn journal's existing heuristic, not a guarantee;
ops/sensitive turns must not be checkpointed. Unsafe required messages make a
checkpoint non-resumable. Unsafe tool results are replaced with an explicit
unavailable result; they must never be interpreted as successful execution.
"""
import json
import re
from copy import deepcopy

from .turn_journal import _contains_secret

_BLOB = re.compile(r'(?i:data:[^\s,]*;base64,|base64://|"(?:base64|b64|b64_json|image_base64|file_data)"\s*:)|[A-Za-z0-9+/]{100,}={0,2}')
_QUOTED_SECRET = re.compile(r"(?i)(?:password|secret|api[_-]?key|(?:access[_-]?|auth[_-]?)?token|authorization|cookie|密码|验证码)[\"']\s*:\s*[\"'][^\"']+")
_SUPPRESSED = json.dumps({"ok": False, "error": "工具结果未安全保存；原结果不可用，不得假定成功。"}, ensure_ascii=False)
UNKNOWN_RESULT = json.dumps({"ok": False, "error": "工具执行结果未知；未重新执行，不得假定成功。", "outcome": "unknown"}, ensure_ascii=False)


def safe_content(value):
    encoded = json.dumps(value, ensure_ascii=False, default=str).replace('\\"', '"')
    return not _contains_secret(value) and not _BLOB.search(encoded) and not _QUOTED_SECRET.search(encoded)


def durable_messages(messages):
    result = deepcopy(messages)
    resumable = True
    for item in result:
        if item.get("role") == "tool":
            if not safe_content(item.get("content")):
                item["content"] = _SUPPRESSED
        elif isinstance(item.get("content"), list):
            item["content"] = "[恢复时图片未保存，无法查看原图片。]"
        elif not safe_content(item):
            item.clear()
            item.update(role="user", content="[恢复所需内容因安全策略未保存。]")
            resumable = False
    return result, resumable
