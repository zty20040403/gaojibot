"""Small opt-in semantic entry evaluation. Default: inspect fixtures without API calls."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def evaluate(cases, profile_name):
    import nonebot
    nonebot.init()
    from src.plugins.ai_chat.agent.execution import ENTRY_PROMPT
    from src.plugins.ai_chat.agent.model_routing import agent_profile_names
    from src.plugins.ai_chat.agent_loop import AgentTrace, _execution_entry, _runtime

    catalog, gateway = _runtime()
    profile = catalog.resolve(profile_name)
    allowed = agent_profile_names(catalog, {})
    if profile.name not in allowed:
        raise ValueError("Choose a configured tool-capable non-Sol profile")
    results = []
    try:
        for case in cases:
            trace = AgentTrace()
            try:
                decision, _ = await _execution_entry([
                    {"role": "system", "content": ENTRY_PROMPT},
                    {"role": "system", "content": "当前话题：" + case["context"]},
                    {"role": "user", "content": case["question"]},
                ], profile, trace=trace, max_steps=8, allowed_profiles=allowed)
                actual = decision.mode
                error = ""
            except Exception as exc:
                actual, error = "error", type(exc).__name__
            results.append({"id": case["id"], "expected": case["expected"], "actual": actual,
                            "passed": actual == case["expected"], "error_type": error,
                            "profile": trace.profile, "tokens": trace.total_tokens})
    finally:
        await gateway.close()
    print(json.dumps({"results": results, "correct": sum(r["passed"] for r in results), "total": len(results)}, ensure_ascii=False, indent=2))
    return 0 if all(item["passed"] for item in results) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Make paid model requests; never executes task tools")
    parser.add_argument("--profile", default="gpt-5.6-terra")
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()
    cases = json.loads((ROOT / "tests/fixtures/subagent_entry_cases.json").read_text(encoding="utf-8"))[:max(1, min(args.limit, 8))]
    if not args.live:
        print(json.dumps({"live": False, "note": "Fixtures only, NOT a measured model accuracy", "cases": cases}, ensure_ascii=False, indent=2))
        return 0
    return asyncio.run(evaluate(cases, args.profile))


if __name__ == "__main__":
    raise SystemExit(main())
