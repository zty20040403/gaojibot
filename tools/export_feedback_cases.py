"""Explicit, read-only export of labelled group turns; output contains private chats."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.context_feedback_evaluation import digest, final_label, write_private_json


def _ids(plan: dict) -> list[int]:
    values = [plan.get("focus_message_id")]
    values.extend(json.loads(plan["related_message_ids_json"]))
    values.extend(json.loads(plan["topic_message_ids_json"]))
    for item in json.loads(plan["recall_candidates_json"]):
        if item.get("selected"):
            values.extend(item.get("evidence_ids", []))
    return sorted({int(value) for value in values if str(value).isdigit() and int(value) > 0})


def export_cases(connection, *, schema: str, scopes: list[str], limit: int = 100,
                 history_limit: int = 500) -> dict:
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", schema):
        raise ValueError("Invalid schema")
    if not scopes or any(not re.fullmatch(r"onebot-v11:group:[0-9]+", scope) for scope in scopes):
        raise ValueError("Select explicit group scope keys")
    if not 1 <= limit <= 500 or not 1 <= history_limit <= 5000:
        raise ValueError("Invalid export bounds")
    cases, skipped = [], []
    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        cursor.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(schema)))
        cursor.execute("SET LOCAL statement_timeout = '15s'")
        cursor.execute("""SELECT p.*, t.final_text, t.tool_call_count, t.status,
            t.trigger_canonical_message_id, t.profile, t.model,
            f.verdict, f.resource_version AS feedback_version,
            votes.asker, votes.others_up, votes.others_down
            FROM turn_context_plans p JOIN agent_turns t ON t.turn_id = p.turn_id
            JOIN turn_visibility tv ON tv.scope_key = t.scope_key
            LEFT JOIN turn_context_feedback f ON f.turn_id = t.turn_id
            LEFT JOIN LATERAL (
                SELECT MAX(CASE WHEN is_asker = 1 AND vote <> 0 THEN vote END) AS asker,
                    COUNT(*) FILTER (WHERE is_asker = 0 AND vote = 1) AS others_up,
                    COUNT(*) FILTER (WHERE is_asker = 0 AND vote = -1) AS others_down,
                    COUNT(*) FILTER (WHERE vote <> 0) AS active_votes
                FROM turn_reaction_votes r WHERE r.turn_id = t.turn_id
            ) votes ON true
            WHERE t.scope_key = ANY(%s) AND p.scope_key = t.scope_key
                AND t.turn_ordinal >= tv.min_turn_ordinal
                AND (f.verdict IS NOT NULL OR votes.active_votes > 0)
            ORDER BY t.turn_id DESC LIMIT %s""", (scopes, limit + 1))
        plans = cursor.fetchall()
        for plan in plans[:limit]:
            case_id = f"turn#{plan['turn_id']}"
            vote = {"asker": plan["asker"], "others_up": plan["others_up"] or 0,
                    "others_down": plan["others_down"] or 0}
            label, label_source = final_label({"verdict": plan["verdict"]}, vote)
            if label is None or not plan["final_text"]:
                skipped.append({"case_id": case_id, "reason": "unlabelled_or_no_answer"})
                continue
            cursor.execute("""SELECT m.canonical_message_id AS id, c.scope_key,
                m.sender_principal_id AS principal_id, m.direction, m.message_kind,
                m.rendered_text AS text, m.body_json, m.occurred_at AS at,
                m.reply_to_canonical_message_id AS reply_to
                FROM messages m JOIN conversations c ON c.conversation_id = m.conversation_id
                JOIN conversation_visibility v ON v.conversation_id = c.conversation_id
                WHERE c.scope_key = %s AND m.canonical_message_id >= v.min_canonical_message_id
                    AND m.canonical_message_id <= %s
                ORDER BY m.canonical_message_id DESC LIMIT %s""",
                (plan["scope_key"], plan["trigger_canonical_message_id"], history_limit + 1))
            history = list(reversed(cursor.fetchall()))
            if not history or history[-1]["id"] != plan["trigger_canonical_message_id"]:
                skipped.append({"case_id": case_id, "reason": "question_hidden_or_missing"})
                continue
            question = history.pop()
            history = [item for item in history if item["message_kind"] == "chat"]
            multimodal = False
            for item in [*history, question]:
                body = json.loads(item.pop("body_json"))
                multimodal |= any(node.get("type") not in {"text", "mention", "reply"}
                                  for node in body.get("nodes", []))
            evidence_ids = _ids(plan)
            missing = sorted(set(evidence_ids) - {item["id"] for item in [*history, question]})
            reason = ("tool_turn_not_replayed" if plan["tool_call_count"] else
                      "command_not_replayed" if question["message_kind"] != "chat" else
                      "multimodal_turn_not_replayed" if multimodal else
                      "original_answer_may_be_truncated" if len(plan["final_text"]) >= 4000 else
                      "evidence_not_in_visible_snapshot" if missing else "")
            case = {"case_id": case_id, "scope_key": plan["scope_key"], "label": label,
                "label_source": label_source, "admin_verdict": plan["verdict"],
                "admin_version": plan["feedback_version"], "votes": vote,
                "question": question, "messages": history, "evidence_ids": evidence_ids,
                "missing_evidence_ids": missing, "original_answer": plan["final_text"],
                "original_profile": plan["profile"], "original_model": plan["model"],
                "original_status": plan["status"], "context_hash": plan["context_hash"],
                "resolver_version": plan["resolver_version"], "unsupported_reason": reason}
            if len(json.dumps(case, ensure_ascii=False)) > 400_000:
                skipped.append({"case_id": case_id, "reason": "case_exceeds_data_bound"})
                continue
            case["sha256"] = digest(case)
            cases.append(case)
    return {"format": "gaoji-feedback-v1", "private": True, "scopes": scopes,
            "created_at": datetime.now(timezone.utc).isoformat(), "cases": cases,
            "skipped": skipped, "limit": limit, "history_limit": history_limit,
            "has_more": len(plans) > limit,
            "limitations": ["raw_chat_not_anonymized", "bounded_history_before_trigger",
                            "no_point_in_time_memory_snapshot", "no_tool_execution"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", action="append", required=True, help="onebot-v11:group:GROUP_ID")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--history-limit", type=int, default=500)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    dsn = os.getenv("AI_POSTGRES_DSN", "").strip()
    if not dsn:
        parser.error("Set AI_POSTGRES_DSN explicitly; .env is not loaded")
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10,
                             application_name="gaoji-feedback-export") as connection:
            result = export_cases(connection, schema=os.getenv("AI_POSTGRES_SCHEMA", "qq_bot"),
                scopes=args.scope, limit=args.limit, history_limit=args.history_limit)
        write_private_json(args.output, result)
    except Exception as exc:
        print(f"Export failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"Private export: {len(result['cases'])} cases, {len(result['skipped'])} skipped, has_more={result['has_more']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
