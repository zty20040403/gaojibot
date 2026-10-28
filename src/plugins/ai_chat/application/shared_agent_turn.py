"""Shared context, replay, model/tool loop and memory path for every chat adapter.

Platform preparation supplies narrowly scoped callbacks. This module never reads
an SDK event or interprets a platform role as local authorization.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any, Callable

from src.bot_storage import DatabaseError
from ..context_pipeline import TurnContextPlan, assess_evidence, rule_recall_route
from ..context_policy import ContextPolicy, chronological_projection_budget, choose_context_policy
from ..context_store import estimate_tokens
from ..handler_constants import TURN_PROMPT_VERSION
from ..observability import telemetry
from ..platform_types import InboundMessage
from ..turn_journal import tool_catalog_fingerprint, tool_effect_labels


@dataclass(frozen=True)
class SharedTurnPorts:
    conversation_id: str
    resolve_plan: Callable[[str, int | None], TurnContextPlan | None]
    memory_scopes: tuple[str | None, str]
    render_memory: Callable[[str, ContextPolicy], str]
    current_user: Callable[[], str]
    fallback_context: Callable[[ContextPolicy, tuple[int, ...]], str]
    refresh_turn_context: Callable[[int | None], str]


@dataclass
class PreparedTurnContext:
    policy: ContextPolicy
    replay_prefix: list[dict[str, Any]]
    replay_covered_message_ids: tuple[int, ...]
    replay_digest_prefix: str
    replay_reason: str
    turn_context: str
    plan_payload: dict[str, Any] | None
    usage: dict[str, int]
    candidates: list[dict[str, object]]
    blocked_reply: str = ""


async def prepare_shared_turn_context(
    app, inbound: InboundMessage, ports: SharedTurnPorts, *, user_text: str,
    selected_profile, tools, journal_turn_id: int | None, turn_context: str = "",
) -> PreparedTurnContext:
    replay_prefix: list[dict[str, Any]] = []
    replay_covered_message_ids: tuple[int, ...] = ()
    replay_digest_prefix = ""
    replay_reason = ""
    context_plan: TurnContextPlan | None = None
    context_plan_payload: dict[str, Any] | None = None
    actual_context_candidates: list[dict[str, object]] = []
    actual_context_usage = {
        "focus": estimate_tokens(user_text),
        "timeline": 0,
        "semantic": 0,
        "group_memory": 0,
        "user_memory": 0,
    }

    blocked_reply = ""
    current_tool_catalog_version = tool_catalog_fingerprint(tools)
    if app.turn_journal is not None and journal_turn_id is not None:
        try:
            (
                await app.db_call(
                    app.turn_journal.update_environment,
                    journal_turn_id,
                    provider=selected_profile.provider_identity,
                    model=selected_profile.model,
                    profile=selected_profile.name,
                    prompt_version=TURN_PROMPT_VERSION,
                    tool_catalog_version=current_tool_catalog_version,
                )
            )
        except (OSError, RuntimeError, DatabaseError) as exc:
            app.logger.warning(
                f"Could not update the turn environment: {exc}"
            )
        if app.settings.turn_replay_enabled:
            parent_turn = await app.db_call(
                app.turn_journal.fork_parent,
                inbound.scope,
                journal_turn_id,
            )
            if parent_turn is not None:
                replay = await app.db_call(
                    app.turn_journal.build_replay,
                    inbound.scope,
                    parent_turn.turn_ordinal,
                    current_provider=selected_profile.provider_identity,
                    current_model=selected_profile.model,
                    current_profile=selected_profile.name,
                    prompt_version=TURN_PROMPT_VERSION,
                    tool_catalog_version=current_tool_catalog_version,
                    max_chars=app.settings.turn_replay_max_chars,
                    max_segments=app.settings.turn_replay_max_segments,
                )
                replay_reason = replay.reason
                replay_digest_prefix = replay.digest_prefix
                turn_context = await app.db_call(
                    ports.refresh_turn_context, journal_turn_id,
                )
                if replay.mode == "verbatim":
                    replay_prefix = list(replay.messages)
                    replay_covered_message_ids = (
                        replay.covered_canonical_message_ids
                    )

    try:
        with telemetry.stage("context.resolve"):
            context_plan = await app.db_call(
                ports.resolve_plan, user_text, journal_turn_id,
            )
    except (OSError, RuntimeError, ValueError, DatabaseError) as exc:
        app.logger.warning(
            f"Group reference resolution failed softly: {exc}"
        )
    with telemetry.stage("context.route"):
        # Keep the live conversation chronological. The
        # lightweight route is retained for memory budgets and observability,
        # but it must never pre-select or remove recent group messages.
        recall_decision = rule_recall_route(
            user_text,
            context_plan,
            is_group=inbound.endpoint.kind == "group",
        )
    context_policy = choose_context_policy(
        user_text,
        context_plan,
        is_group=inbound.endpoint.kind == "group",
        recall_decision=recall_decision,
        configured_max_tokens=app.settings.context_input_budget_tokens,
        model_max_input_tokens=selected_profile.max_input_tokens,
    )
    group_memory_scope, user_memory_scope = ports.memory_scopes
    evidence_assessment = assess_evidence(
        user_text,
        recall_decision,
        context_plan,
        None,
        conversation_scope=inbound.scope.key,
        group_memory_scope=group_memory_scope,
        user_memory_scope=user_memory_scope,
    )
    if (
        context_plan is not None
        and app.turn_journal is not None
        and journal_turn_id is not None
    ):
        try:
            payload = context_plan.journal_payload()
            payload["recall_route"] = recall_decision.journal_payload()
            payload["recall_route"]["context_strategy"] = (
                "chronological_projection"
            )
            payload["adaptive_budget"] = {
                "focus": context_policy.token_budget.focus,
                "timeline": context_policy.token_budget.timeline,
                "semantic": context_policy.token_budget.semantic,
                "group_memory": context_policy.token_budget.group_memory,
                "user_memory": context_policy.token_budget.user_memory,
                "tool_reserve": context_policy.token_budget.tool_reserve,
                "total": context_policy.token_budget.total,
            }
            payload["evidence_guard"] = evidence_assessment.journal_payload()
            context_plan_payload = payload
            (
                await app.db_call(
                    app.turn_journal.record_context_plan,
                    journal_turn_id,
                    payload,
                    created_at=inbound.occurred_at,
                )
            )
        except (OSError, RuntimeError, ValueError, DatabaseError):
            pass
    if (
        app.settings.evidence_guard_enabled
        and not evidence_assessment.sufficient
        and "scope_violation" in evidence_assessment.reason_codes
    ):
        blocked_reply = "上下文范围校验没有通过，已停止使用可能串群或串用户的内容。"

    return PreparedTurnContext(
        context_policy, replay_prefix, replay_covered_message_ids,
        replay_digest_prefix, replay_reason, turn_context, context_plan_payload,
        actual_context_usage, actual_context_candidates, blocked_reply,
    )


async def build_shared_prompt_context(
    app, inbound: InboundMessage, ports: SharedTurnPorts, prepared: PreparedTurnContext,
    *, user_text: str, selected_profile, journal_turn_id: int | None,
) -> tuple[str, str]:
    context_policy = prepared.policy
    replay_covered_message_ids = prepared.replay_covered_message_ids
    context_plan_payload = prepared.plan_payload
    actual_context_usage = prepared.usage
    actual_context_candidates = prepared.candidates
    excluded_ids = replay_covered_message_ids
    legacy_native_exclusion = inbound.native_message_id
    if inbound.endpoint.account.platform != "onebot-v11" and app.message_ledger is not None:
        current_id = await app.db_call(
            app.message_ledger.canonical_id_for_native, inbound.scope,
            inbound.native_message_id, endpoint=inbound.endpoint,
        )
        excluded_ids = (*excluded_ids, current_id) if current_id is not None else excluded_ids
        legacy_native_exclusion = None
    group_prompt_context = ""
    if inbound.endpoint.kind == "group" and app.message_ledger is not None:
        scope = inbound.scope
        replied_native_id = inbound.reply_to_native_message_id
        replied_canonical_id = (
            (
                await app.db_call(
                    app.message_ledger.canonical_id_for_native,
                    scope,
                    replied_native_id,
                    endpoint=inbound.endpoint,
                )
            )
            if replied_native_id is not None
            else None
        )
        protected_ids = (
            (replied_canonical_id,)
            if replied_canonical_id is not None
            else ()
        )
        sections: list[str] = []
        projection_built = False
        if app.pin_store is not None:
            pinned = await app.db_call(
                app.pin_store.render,
                app.message_ledger,
                scope,
                max_chars=2400,
            )
            if pinned:
                pinned_block = (
                    "[pinned messages - long-lived current-group facts]\n"
                    + pinned
                )
                sections.append(pinned_block)
                actual_context_usage["timeline"] += estimate_tokens(
                    pinned_block
                )
                for _pin, message in await app.db_call(
                    app.pin_store.messages,
                    app.message_ledger,
                    scope,
                ):
                    handle = f"msg#{message.canonical_message_id}"
                    if handle not in pinned:
                        continue
                    actual_context_candidates.append(
                        {
                            "handle": handle,
                            "source": "pinned_message",
                            "selected": True,
                            "raw_score": 1.0,
                            "adjusted_score": 1.0,
                            "decision_codes": ["included_in_final_prompt"],
                            "reason_codes": ["pinned_context"],
                            "content_preview": message.prompt_text[:240],
                            "scores": {},
                            "evidence_ids": [message.canonical_message_id],
                        }
                    )
        if app.context_store is not None:
            try:
                projection = await app.db_call(
                    app.context_store.build_projection,
                    app.message_ledger,
                    scope,
                    exclude_native_message_id=legacy_native_exclusion,
                    protected_message_ids=protected_ids,
                    exclude_canonical_message_ids=excluded_ids,
                    materialize=False,
                    token_budget=chronological_projection_budget(
                        app.settings.context_input_budget_tokens,
                        model_max_input_tokens=(
                            selected_profile.max_input_tokens
                        ),
                    ),
                )
            except (
                OSError,
                RuntimeError,
                ValueError,
                DatabaseError,
            ) as exc:
                app.logger.warning(
                    f"Chronological context projection failed softly: {exc}"
                )
            else:
                projection_built = True
                if projection.text:
                    sections.append(projection.text)
                    actual_context_usage["timeline"] += int(
                        getattr(
                            projection,
                            "token_estimate",
                            estimate_tokens(projection.text),
                        )
                    )
                for message_id in getattr(
                    projection,
                    "raw_message_ids",
                    (),
                ):
                    message = await app.db_call(
                        app.message_ledger.get_in_scope,
                        scope,
                        int(message_id),
                    )
                    actual_context_candidates.append(
                        {
                            "handle": f"msg#{int(message_id)}",
                            "source": "group_timeline",
                            "selected": True,
                            "raw_score": 1.0,
                            "adjusted_score": 1.0,
                            "decision_codes": ["included_in_final_prompt"],
                            "reason_codes": ["chronological_live_tail"],
                            "content_preview": (
                                message.prompt_text[:240]
                                if message is not None
                                else ""
                            ),
                            "scores": {},
                            "evidence_ids": [int(message_id)],
                        }
                    )
                for handle in getattr(
                    projection,
                    "compartment_handles",
                    (),
                ):
                    actual_context_candidates.append(
                        {
                            "handle": f"episode#{handle}",
                            "source": "historian_episode",
                            "selected": True,
                            "raw_score": 1.0,
                            "adjusted_score": 1.0,
                            "decision_codes": ["included_in_final_prompt"],
                            "reason_codes": ["chronological_compartment"],
                            "content_preview": "Historian 时间线章节",
                            "scores": {},
                            "evidence_ids": [],
                        }
                    )
        if not projection_built:
            fallback = await app.db_call(
                app.message_ledger.render_recent,
                scope,
                max_messages=app.settings.group_context_messages,
                max_chars=max(app.settings.group_context_chars, 12000),
                exclude_native_message_id=legacy_native_exclusion,
                exclude_canonical_message_ids=excluded_ids,
            )
            if fallback:
                fallback_block = "[protected live tail]\n" + fallback
                sections.append(fallback_block)
                actual_context_usage["timeline"] += estimate_tokens(
                    fallback_block
                )
                for matched in re.finditer(r"\bmsg#([1-9][0-9]*)", fallback):
                    message_id = int(matched.group(1))
                    message = await app.db_call(
                        app.message_ledger.get_in_scope,
                        scope,
                        message_id,
                    )
                    actual_context_candidates.append(
                        {
                            "handle": f"msg#{message_id}",
                            "source": "group_timeline",
                            "selected": True,
                            "raw_score": 1.0,
                            "adjusted_score": 1.0,
                            "decision_codes": ["included_in_final_prompt"],
                            "reason_codes": ["chronological_fallback"],
                            "content_preview": (
                                message.prompt_text[:240]
                                if message is not None
                                else ""
                            ),
                            "scores": {},
                            "evidence_ids": [message_id],
                        }
                    )
        if app.source_store is not None:
            try:
                recent_sources = await app.db_call(
                    app.source_store.render_recent, scope
                )
            except (OSError, RuntimeError, ValueError, DatabaseError) as exc:
                app.logger.warning(
                    f"Recent shared-source context failed: {exc}"
                )
            else:
                if recent_sources:
                    source_block = (
                        "[recent shared sources - inspect with source#/msg#]\n"
                        + recent_sources
                    )
                    sections.append(source_block)
                    actual_context_usage["semantic"] += estimate_tokens(
                        source_block
                    )
                    for handle in dict.fromkeys(
                        re.findall(r"\bsource#[1-9][0-9]*", recent_sources)
                    ):
                        actual_context_candidates.append(
                            {
                                "handle": handle,
                                "source": "shared_source",
                                "selected": True,
                                "raw_score": 1.0,
                                "adjusted_score": 1.0,
                                "decision_codes": ["included_in_final_prompt"],
                                "reason_codes": ["recent_shared_source"],
                                "content_preview": "当前群近期分享内容",
                                "scores": {},
                                "evidence_ids": [],
                            }
                        )
        if replied_canonical_id is not None:
            replied = await app.db_call(
                app.message_ledger.get_in_scope,
                scope,
                replied_canonical_id,
            )
            if replied is not None:
                quoted_block = (
                    "[quoted context - explicit reply target, highest priority]\n"
                    f"[msg#{replied.canonical_message_id} | "
                    f"{replied.sender_display}] {replied.prompt_text}"
                )
                sections.append(quoted_block)
                actual_context_usage["focus"] += estimate_tokens(quoted_block)
                actual_context_candidates.append(
                    {
                        "handle": f"msg#{replied.canonical_message_id}",
                        "source": "relation_graph",
                        "selected": True,
                        "raw_score": 1.0,
                        "adjusted_score": 1.0,
                        "decision_codes": ["included_in_final_prompt"],
                        "reason_codes": ["explicit_reply_target"],
                        "content_preview": replied.prompt_text[:240],
                        "scores": {},
                        "evidence_ids": [replied.canonical_message_id],
                    }
                )
        group_prompt_context = "\n\n".join(sections)
    else:
        group_prompt_context = await app.db_call(
            ports.fallback_context, context_policy, replay_covered_message_ids,
        )
    memory_prompt_context = await app.db_call(
        ports.render_memory, user_text, context_policy,
    )
    for memory_block in memory_prompt_context.split("\n\n"):
        if memory_block.startswith("[当前群相关长期记忆]"):
            memory_source = "group_memory"
        elif memory_block.startswith("[当前用户相关长期记忆]"):
            memory_source = "user_memory"
        else:
            continue
        actual_context_usage[memory_source] += estimate_tokens(memory_block)
        for memory_id, preview in re.findall(
            r"(?m)^- \[#([1-9][0-9]*)\] (.+)$",
            memory_block,
        ):
            actual_context_candidates.append(
                {
                    "handle": f"memory#{memory_id}",
                    "source": memory_source,
                    "selected": True,
                    "raw_score": 1.0,
                    "adjusted_score": 1.0,
                    "decision_codes": ["included_in_final_prompt"],
                    "reason_codes": ["relevant_long_term_memory"],
                    "content_preview": preview[:240],
                    "scores": {},
                    "evidence_ids": [],
                }
            )
    if (
        context_plan_payload is not None
        and app.turn_journal is not None
        and journal_turn_id is not None
    ):
        deduplicated: dict[str, dict[str, object]] = {}
        source_priority = {
            "relation_graph": 5,
            "pinned_message": 4,
            "group_timeline": 3,
            "historian_episode": 2,
            "group_memory": 2,
            "user_memory": 2,
            "shared_source": 1,
        }
        for candidate in actual_context_candidates:
            handle = str(candidate.get("handle") or "")
            current = deduplicated.get(handle)
            if current is None or source_priority.get(
                str(candidate.get("source") or ""),
                0,
            ) > source_priority.get(str(current.get("source") or ""), 0):
                deduplicated[handle] = candidate
        actual_context_usage["total"] = sum(actual_context_usage.values())
        route_payload = dict(context_plan_payload.get("recall_route") or {})
        route_payload["classifier_mode"] = route_payload.get("mode", "")
        route_payload["mode"] = "chronological_projection"
        route_payload["context_strategy"] = "chronological_projection"
        context_plan_payload["recall_route"] = route_payload
        adaptive_budget = dict(
            context_plan_payload.get("adaptive_budget") or {}
        )
        adaptive_budget["used"] = dict(actual_context_usage)
        for key in (
            "focus",
            "timeline",
            "semantic",
            "group_memory",
            "user_memory",
        ):
            adaptive_budget[key] = max(
                int(adaptive_budget.get(key) or 0),
                int(actual_context_usage[key]),
            )
        adaptive_budget["total"] = sum(
            int(adaptive_budget.get(key) or 0)
            for key in (
                "focus",
                "timeline",
                "semantic",
                "group_memory",
                "user_memory",
                "tool_reserve",
            )
        )
        context_plan_payload["adaptive_budget"] = adaptive_budget
        context_plan_payload["recall_candidates"] = list(
            deduplicated.values()
        )
        context_plan_payload["related_message_ids"] = sorted(
            {
                int(evidence_id)
                for candidate in deduplicated.values()
                for evidence_id in candidate.get("evidence_ids", [])
                if str(evidence_id).isdigit()
            }
        )
        context_plan_payload["resolver_version"] = (
            "chronological-projection-v3"
        )
        context_plan_payload["context_hash"] = hashlib.sha256(
            (group_prompt_context + "\n\n" + memory_prompt_context).encode(
                "utf-8"
            )
        ).hexdigest()[:16]
        evidence_payload = dict(
            context_plan_payload.get("evidence_guard") or {}
        )
        if deduplicated:
            evidence_payload.update(
                {
                    "sufficient": True,
                    "reason_codes": ["chronological_projection_visible"],
                    "evidence_handles": list(deduplicated),
                }
            )
        context_plan_payload["evidence_guard"] = evidence_payload
        try:
            (
                await app.db_call(
                    app.turn_journal.record_context_plan,
                    journal_turn_id,
                    context_plan_payload,
                    created_at=inbound.occurred_at,
                )
            )
        except (
            OSError,
            RuntimeError,
            ValueError,
            DatabaseError,
        ) as exc:
            app.logger.warning(
                f"Final context projection journal failed: {exc}"
            )

    return group_prompt_context, memory_prompt_context


async def run_shared_agent_turn(
    user_text, tools, execute_tool, *, app, inbound: InboundMessage,
    ports: SharedTurnPorts, prepared: PreparedTurnContext, llm, **options,
):
    """Invoke the existing agent loop with the same history and identity rules."""
    history = (
        [] if prepared.replay_prefix or inbound.endpoint.kind == "group"
        else await app.db_call(app.memory.get, ports.conversation_id)
    )
    return await llm(
        user_text, history, tools, execute_tool,
        current_user=await app.db_call(ports.current_user),
        replay_prefix=prepared.replay_prefix or None,
        **options,
    )


async def append_shared_turn_memory(app, ports: SharedTurnPorts, user_text: str, answer: str) -> None:
    await app.db_call(app.memory.append_turn, ports.conversation_id, user_text, answer)


async def record_shared_loop_event(app, turn_id: int, event) -> None:
    if app.turn_journal is None:
        return
    labels = event.side_effects or tool_effect_labels(event.tool_name)
    metadata = {
        "call_id": event.call_id,
        "fingerprint": event.fingerprint,
        "risk": event.risk,
        "idempotency": event.idempotency,
        "execution_mode": event.execution_mode,
        "approval": event.approval,
        "duration_ms": event.duration_ms,
    }
    metadata = {key: value for key, value in metadata.items() if value not in {"", 0}}
    if event.kind == "model_note":
        (
            await app.db_call(
                app.turn_journal.record_model_note,
                turn_id,
                event.sequence,
                event.note,
            )
        )
    elif event.kind == "tool_started":
        (
            await app.db_call(
                app.turn_journal.record_tool_started,
                turn_id,
                event.sequence,
                event.tool_name,
                event.arguments,
                labels,
                metadata,
            )
        )
    elif event.kind == "tool_rejected":
        (
            await app.db_call(
                app.turn_journal.record_tool_rejected,
                turn_id,
                event.sequence,
                event.tool_name,
                event.arguments,
                event.result,
                labels,
                metadata,
            )
        )
    elif event.kind in {"tool_finished", "tool_compensated"}:
        (
            await app.db_call(
                app.turn_journal.record_tool_finished,
                turn_id,
                event.sequence,
                event.tool_name,
                event.state,  # type: ignore[arg-type]
                event.result,
                labels,
                metadata,
            )
        )

