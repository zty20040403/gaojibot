"""Private, bounded answer comparisons without executing agent tools."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False).encode()).hexdigest()


def final_label(feedback: dict | None, votes: dict) -> tuple[str | None, str]:
    verdict = (feedback or {}).get("verdict")
    if verdict in {"correct", "off_topic"}:
        return ("positive" if verdict == "correct" else "negative"), "administrator"
    if votes.get("asker") in {-1, 1}:
        return ("positive" if votes["asker"] == 1 else "negative"), "asker"
    balance = int(votes.get("others_up", 0)) - int(votes.get("others_down", 0))
    return ("positive" if balance > 0 else "negative" if balance < 0 else None), "community"


def write_private_json(path: Path, value: object) -> None:
    """Never overwrite another export or follow a destination symlink."""
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(encoded)


def validate_export(payload: dict) -> list[dict]:
    if payload.get("format") != "gaoji-feedback-v1" or not isinstance(payload.get("cases"), list):
        raise ValueError("Expected a gaoji-feedback-v1 private export")
    cases = payload["cases"]
    if len(cases) > 500:
        raise ValueError("At most 500 cases per export")
    seen = set()
    for case in cases:
        if not isinstance(case, dict) or case.get("case_id") in seen:
            raise ValueError("Invalid or duplicate case")
        seen.add(case.get("case_id"))
        source = {key: value for key, value in case.items() if key != "sha256"}
        if case.get("sha256") != digest(source):
            raise ValueError("Case hash mismatch; regenerate or review the export")
        messages = case.get("messages", [])
        question = case.get("question", {})
        if case.get("label") not in {"positive", "negative"}:
            raise ValueError("Unlabelled case")
        if (not isinstance(messages, list) or len(messages) > 5000
                or not isinstance(question.get("id"), int)
                or any(not isinstance(m.get("id"), int) or m["id"] >= question["id"]
                       or m.get("scope_key") != case.get("scope_key") for m in messages)
                or question.get("scope_key") != case.get("scope_key")
                or len({m["id"] for m in messages}) != len(messages)):
            raise ValueError("Replay history must be earlier than the question and in the same scope")
        if len(json.dumps(source, ensure_ascii=False)) > 400_000:
            raise ValueError("Replay case exceeds the data bound")
    return cases


async def compare_cases(cases, *, generate, judge, timeout: float = 120.0) -> dict:
    """Callbacks deliberately expose neither labels nor original answers to the candidate."""
    results = []
    groups = {label: {key: 0 for key in ("win", "tie", "loss", "error", "skipped")}
              for label in ("negative", "positive")}
    for case in cases:
        row = {"case_id": case["case_id"], "label": case["label"], "sha256": case["sha256"]}
        if case.get("unsupported_reason"):
            row.update(outcome="skipped", reason=case["unsupported_reason"])
        else:
            try:
                candidate_input = {key: case[key] for key in ("scope_key", "question", "messages")}
                generated = await asyncio.wait_for(generate(candidate_input), timeout)
                answer = str(generated.get("answer", "")).strip()
                if not answer:
                    raise ValueError("Empty candidate answer")
                candidate_is_a = int(digest(case["case_id"])[0], 16) % 2 == 0
                pair = [answer, case["original_answer"]]
                if not candidate_is_a:
                    pair.reverse()
                verdict = await asyncio.wait_for(judge({**candidate_input, "A": pair[0], "B": pair[1]}), timeout)
                winner = verdict.get("winner")
                if winner not in {"A", "B", "tie"}:
                    raise ValueError("Invalid pairwise judgement")
                outcome = "tie" if winner == "tie" else (
                    "win" if (winner == "A") == candidate_is_a else "loss")
                row.update(outcome=outcome, candidate=generated,
                           judge=verdict, candidate_position="A" if candidate_is_a else "B")
            except Exception as exc:
                # Provider exceptions can contain credentials or private prompts.
                row.update(outcome="error", reason=type(exc).__name__)
        groups[case["label"]][row["outcome"]] += 1
        results.append(row)
    compared = sum(group[key] for group in groups.values() for key in ("win", "tie", "loss"))
    return {"format": "gaoji-feedback-comparison-v1", "groups": groups, "results": results,
            "compared": compared, "selected": len(cases),
            "positive_regressions": groups["positive"]["loss"],
            "coverage": compared / len(cases) if cases else 0,
            "release_ready": False, "limitations": ["model_judge_not_ground_truth",
                "bounded_raw_timeline_only", "no_tool_execution", "no_point_in_time_memory_snapshot"]}
