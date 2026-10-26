from __future__ import annotations

import hashlib
import json
import unittest

from tests import test_subagent_v2  # noqa: F401 - initialize the isolated NoneBot runtime
from src.plugins.ai_chat.agent.evidence import (
    READ_TASK_EVIDENCE_BATCH, canonical, read_evidence, read_evidence_batch,
)
from src.plugins.ai_chat.deepseek import _bounded_tool_result
from src.plugins.ai_chat.tool_policy import ToolCatalog


def evidence(index, content):
    payload = {"content": content}
    return {"evidence_id": f"evidence#{index:032x}", "payload": payload,
            "payload_hash": hashlib.sha256(canonical(payload).encode()).hexdigest(),
            "complete": True, "recorded_at": 100}


class EvidenceBatchTests(unittest.TestCase):
    def test_eight_small_receipts_fit_one_valid_model_result(self):
        items = [evidence(index, f"host-{index} is online") for index in range(8)]
        raw = read_evidence_batch(items, {"requests": [{"ref": item["evidence_id"]} for item in items]})
        self.assertEqual(raw, _bounded_tool_result(raw, 12000))
        reply = json.loads(raw)
        self.assertTrue(reply["ok"])
        self.assertEqual(len(reply["results"]), 8)
        for page, item in zip(reply["results"], items):
            self.assertIsNone(page["next_offset"])
            self.assertEqual(json.loads(page["content"]), item["payload"])

    def test_json_escaping_and_envelope_are_included_in_single_page_budget(self):
        item = evidence(1, ('中文\\\"\n' * 4000))
        offset, fragments = 0, []
        while offset is not None:
            raw = read_evidence([item], {"ref": item["evidence_id"], "offset": offset}, max_chars=1000)
            self.assertLessEqual(len(raw), 1000)
            self.assertEqual(_bounded_tool_result(raw, 1000), raw)
            page = json.loads(raw)
            self.assertTrue(page["ok"])
            self.assertTrue(page["content"])
            fragments.append(page["content"])
            self.assertTrue(page["next_offset"] is None or page["next_offset"] > offset)
            offset = page["next_offset"]
        self.assertEqual(json.loads("".join(fragments)), item["payload"])

    def test_large_batch_pages_reconstruct_all_evidence_without_exceeding_wire_budget(self):
        items = [evidence(index, ('\\\"中\n' * 1700) if index % 2 else 'small') for index in range(8)]
        requests = [{"ref": item["evidence_id"], "offset": 0} for item in items]
        fragments = {item["evidence_id"]: [] for item in items}
        calls = 0
        while requests:
            raw = read_evidence_batch(items, {"requests": requests}, max_chars=12000)
            self.assertLessEqual(len(raw), 12000)
            reply = json.loads(raw)
            self.assertTrue(reply["ok"])
            next_requests = []
            for request, page in zip(requests, reply["results"]):
                self.assertTrue(page["ok"])
                fragments[page["ref"]].append(page["content"])
                if page["next_offset"] is not None:
                    self.assertGreater(page["next_offset"], request["offset"])
                    next_requests.append({"ref": page["ref"], "offset": page["next_offset"]})
            requests = next_requests
            calls += 1
            self.assertLess(calls, 40)
        for item in items:
            self.assertEqual(json.loads("".join(fragments[item["evidence_id"]])), item["payload"])

    def test_batch_rejects_invalid_requests_and_only_returns_authorized_content(self):
        item = evidence(1, "authorized")
        for requests in ([], [{}], [None], [{"ref": "x", "offset": True}], [{"ref": "x", "offset": -1}],
                         [{"ref": "x", "task_id": 2}], [{"ref": "x"}] * 9, [{"ref": "x"}] * 2):
            with self.subTest(requests=requests):
                self.assertFalse(json.loads(read_evidence_batch([item], {"requests": requests}))["ok"])
        reply = json.loads(read_evidence_batch([item], {"requests": [
            {"ref": item["evidence_id"]}, {"ref": "evidence#foreign"}]}))
        self.assertTrue(reply["results"][0]["ok"])
        self.assertFalse(reply["results"][1]["ok"])
        self.assertNotIn("content", reply["results"][1])
        self.assertFalse(json.loads(read_evidence_batch([item], {"requests": [{"ref": item["evidence_id"]}]}, max_chars=40))["ok"])

    def test_batch_is_schema_checked_read_only_and_keeps_incomplete_flag(self):
        catalog = ToolCatalog([READ_TASK_EVIDENCE_BATCH])
        self.assertEqual(catalog.policy("read_task_evidence_batch").risk, "low")
        self.assertEqual(catalog.policy("read_task_evidence_batch").idempotency, "pure")
        self.assertTrue(catalog.validate("read_task_evidence_batch", {"requests": [{"ref": "evidence#1"}]}).ok)
        self.assertFalse(catalog.validate("read_task_evidence_batch", {"requests": [{"ref": "x"}] * 9}).ok)
        item = {**evidence(1, "truncated preview"), "complete": False}
        page = json.loads(read_evidence_batch([item], {"requests": [{"ref": item["evidence_id"]}]}))["results"][0]
        self.assertFalse(page["complete"])
