from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ..conversation_scope import ConversationScope
from ..ledger import CanonicalMessage, MessageLedger
from .graph import TopicEdge, topic_terms
from .models import ContextTokenBudget, TurnContextPlan
from .ranking import (
    HybridReranker,
    RankedRecall,
    RecallCandidate,
    RecallRankingDecision,
    combine_budgeted_sections,
    fit_token_budget,
)
from .router import RecallDecision


_MESSAGE_HANDLE = re.compile(r"msg#([1-9][0-9]*)")
_MEMORY_HANDLE = re.compile(r"memory#([1-9][0-9]*)")
_EPISODE_HANDLE = re.compile(r"episode#([0-9a-fA-F-]{8,})")


@dataclass(frozen=True)
class HybridRecallContext:
    query: str
    group_context: str
    memory_context: str
    candidates: tuple[RankedRecall, ...]
    semantic_available: bool
    focus_context: str = ""
    timeline_context: str = ""
    recall_context: str = ""
    route_mode: str = "legacy"
    candidate_decisions: tuple[RecallRankingDecision, ...] = ()
    scope_rejections: tuple[dict[str, object], ...] = ()
    group_memory_context: str = ""
    user_memory_context: str = ""

    def journal_candidates(self) -> list[dict[str, object]]:
        return [
            {
                "handle": item.candidate.handle,
                "source": item.candidate.source,
                "score": item.score,
                "reason_codes": list(item.candidate.reason_codes),
                "lexical_score": item.candidate.lexical_score,
                "semantic_score": item.candidate.semantic_score,
                "relation_score": item.candidate.relation_score,
                "recency_score": item.candidate.recency_score,
                "topic_score": item.candidate.topic_score,
                "importance_score": item.candidate.importance_score,
                "actor_score": item.candidate.actor_score,
                "topic_conflict": item.candidate.topic_conflict,
                "scope_key": item.candidate.scope_key,
                "evidence_ids": list(
                    item.candidate.metadata.get("evidence_ids", [])
                ),
            }
            for item in self.candidates
        ]

    def journal_decisions(self) -> list[dict[str, object]]:
        ranked = [
            {
                "handle": item.candidate.handle,
                "source": item.candidate.source,
                "selected": item.selected,
                "raw_score": item.raw_score,
                "adjusted_score": item.adjusted_score,
                "decision_codes": list(item.decision_codes),
                "reason_codes": list(item.candidate.reason_codes),
                "content_preview": item.candidate.content[:600],
                "scores": {
                    "lexical": item.candidate.lexical_score,
                    "semantic": item.candidate.semantic_score,
                    "relation": item.candidate.relation_score,
                    "recency": item.candidate.recency_score,
                    "topic": item.candidate.topic_score,
                    "importance": item.candidate.importance_score,
                    "actor": item.candidate.actor_score,
                    "topic_conflict": item.candidate.topic_conflict,
                    "leakage_risk": item.candidate.leakage_risk,
                },
                "scope_key": item.candidate.scope_key,
                "evidence_ids": list(
                    item.candidate.metadata.get("evidence_ids", [])
                ),
            }
            for item in self.candidate_decisions
        ]
        decisions = [*ranked, *self.scope_rejections]
        maximum = 120
        if len(decisions) <= maximum:
            return decisions
        omitted = len(decisions) - maximum
        return [
            *decisions[:maximum],
            {
                "handle": "audit#omitted",
                "source": "audit_summary",
                "selected": False,
                "raw_score": 0.0,
                "adjusted_score": 0.0,
                "decision_codes": ["audit_truncated"],
                "reason_codes": [],
                "content_preview": "",
                "scores": {},
                "scope_key": self.candidates[0].candidate.scope_key
                if self.candidates
                else "",
                "evidence_ids": [],
                "omitted_count": omitted,
            },
        ]


async def build_hybrid_recall(
    *,
    ledger: MessageLedger,
    scope: ConversationScope,
    plan: TurnContextPlan | None,
    user_text: str,
    group_memory_scope: str | None,
    user_memory_scope: str,
    memory_store: Any,
    context_store: Any = None,
    topic_graph_store: Any = None,
    semantic_recall: Any = None,
    pin_store: Any = None,
    source_store: Any = None,
    media_library: Any = None,
    recall_decision: RecallDecision | None = None,
    current_native_user_id: str | int = "",
    current_actor_key: str = "",
    include_group_memory: bool = True,
    include_user_memory: bool = True,
    fallback_group_memory: bool = False,
    fallback_user_memory: bool = False,
    budget: ContextTokenBudget,
    semantic_timeout_seconds: float = 3.0,
    now: int | None = None,
) -> HybridRecallContext:
    """Build one scope-safe candidate pool and rerank all recall sources."""

    query = " ".join(
        ((plan.topic_query if plan is not None else "") or user_text).split()
    ).strip()
    current_time = int(now or time.time())
    route_mode = recall_decision.mode if recall_decision is not None else "legacy"
    graph_enabled = bool(
        plan is not None
        and (
            recall_decision is None
            or recall_decision.include_graph
        )
    )
    timeline_enabled = bool(
        recall_decision is not None
        and recall_decision.include_recent_timeline
    )
    semantic_enabled = bool(
        semantic_recall is not None
        and (
            recall_decision is None
            or recall_decision.include_semantic
        )
    )
    group_memory_enabled = bool(
        include_group_memory
        and group_memory_scope is not None
        and (
            recall_decision is None
            or recall_decision.include_group_memory
        )
    )
    user_memory_enabled = bool(
        include_user_memory
        and (
            recall_decision is None
            or recall_decision.include_user_memory
        )
    )
    candidates: list[RecallCandidate] = []

    group_memories = (
        memory_store.list_entries([group_memory_scope])
        if group_memory_enabled and group_memory_scope is not None
        else []
    )
    user_memories = (
        memory_store.list_entries([user_memory_scope])
        if user_memory_enabled
        else []
    )
    for entry in group_memories:
        candidates.append(
            _memory_candidate(
                entry,
                "group_memory",
                current_time,
                fallback=fallback_group_memory,
            )
        )
    for entry in user_memories:
        candidates.append(
            _memory_candidate(
                entry,
                "user_memory",
                current_time,
                fallback=fallback_user_memory,
            )
        )

    focused_ids: set[int] = set()
    if graph_enabled and plan is not None:
        focused_ids = {
            int(message_id)
            for message_id in (
                plan.focus_message_id,
                *plan.related_message_ids,
            )
            if message_id is not None
        }
        graph_messages = ledger.visible_messages_by_ids(scope, tuple(focused_ids))
        for message in graph_messages:
            is_focus = message.canonical_message_id == plan.focus_message_id
            candidates.append(
                _message_candidate(
                    message,
                    "relation_graph",
                    current_time,
                    current_native_user_id=current_native_user_id,
                    current_actor_key=current_actor_key,
                    relation_score=1.0 if is_focus else 0.78,
                    reason=("selected_focus" if is_focus else "topic_graph_neighbor"),
                )
            )

    if timeline_enabled:
        recent_limit = min(max(budget.timeline // 55, 6), 40)
        for message in ledger.recent_in_scope(scope, recent_limit + 1):
            if (
                message.canonical_message_id in focused_ids
                or plan is not None
                and message.canonical_message_id == plan.current_message_id
                or message.message_kind == "command"
                or not message.prompt_text.strip()
            ):
                continue
            candidates.append(
                _message_candidate(
                    message,
                    "group_timeline",
                    current_time,
                    current_native_user_id=current_native_user_id,
                    current_actor_key=current_actor_key,
                    relation_score=_plan_relation(plan, message.canonical_message_id),
                    reason="recent_group_timeline",
                )
            )

    history_enabled = bool(
        recall_decision is not None
        and recall_decision.mode in {"old_topic", "follow_up", "group_memory"}
    )
    if history_enabled and topic_terms(query):
        candidates.extend(
            _lexical_history_candidates(
                ledger,
                scope,
                query,
                now=current_time,
                current_message_id=plan.current_message_id if plan is not None else 0,
                excluded_ids=focused_ids,
                current_native_user_id=current_native_user_id,
                current_actor_key=current_actor_key,
            )
        )

    episodes = []
    if context_store is not None and (
        history_enabled
        or recall_decision is None and semantic_recall is not None
    ):
        episodes = [
            item
            for item in context_store.active_compartments(limit=5000)
            if item.scope_key == scope.key
        ]
        for episode in episodes:
            candidate = _episode_candidate(episode, query)
            if candidate is not None:
                candidates.append(candidate)

    if (
        pin_store is not None
        and recall_decision is not None
        and recall_decision.include_pins
    ):
        for pin, message in pin_store.messages(ledger, scope):
            candidates.append(
                _message_candidate(
                    message,
                    "pinned_message",
                    current_time,
                    current_native_user_id=current_native_user_id,
                    current_actor_key=current_actor_key,
                    relation_score=0.65,
                    importance_score=0.95,
                    reason="pinned_in_current_scope",
                    metadata={"pinned_at": pin.created_at},
                )
            )

    if (
        source_store is not None
        and recall_decision is not None
        and recall_decision.include_shared_sources
    ):
        try:
            visible_sources = source_store.search_visible(scope, query, limit=6)
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            visible_sources = []
        for source in visible_sources:
            candidates.append(_source_candidate(scope, source, query, current_time))

    if (
        media_library is not None
        and recall_decision is not None
        and recall_decision.include_media
        and query
    ):
        try:
            media_records = await media_library.search_media(scope, query, limit=4)
        except (OSError, RuntimeError, TypeError, ValueError, httpx.HTTPError):
            media_records = []
        for record in media_records:
            candidates.append(_media_candidate(scope, record, query))

    allowed_scopes = {scope.key}
    if group_memory_enabled and group_memory_scope is not None:
        allowed_scopes.add(group_memory_scope)
    if user_memory_enabled:
        allowed_scopes.add(user_memory_scope)
    semantic_available = semantic_enabled
    semantic_hits: list[Any] = []
    if semantic_enabled and topic_terms(query):
        try:
            semantic_hits = await asyncio.wait_for(
                semantic_recall.search(sorted(allowed_scopes), query, limit=24),
                timeout=max(float(semantic_timeout_seconds), 0.25),
            )
        except (
            asyncio.TimeoutError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            httpx.HTTPError,
        ):
            semantic_available = False

    memory_by_handle = {
        f"memory#{entry.id}": entry
        for entry in (*group_memories, *user_memories)
    }
    active_episodes = {
        f"episode#{entry.expand_handle}": entry for entry in episodes
    }
    semantic_edges: list[TopicEdge] = []
    for hit in semantic_hits:
        if str(hit.scope_key) not in allowed_scopes:
            continue
        candidate = _semantic_candidate(
            hit,
            ledger=ledger,
            scope=scope,
            memory_by_handle=memory_by_handle,
            active_episodes=active_episodes,
            now=current_time,
            group_memory_scope=group_memory_scope,
            user_memory_scope=user_memory_scope,
            current_native_user_id=current_native_user_id,
            current_actor_key=current_actor_key,
        )
        if candidate is None:
            continue
        candidates.append(candidate)
        matched_message = _MESSAGE_HANDLE.fullmatch(str(hit.source_handle))
        if (
            topic_graph_store is not None
            and plan is not None
            and matched_message is not None
            and str(hit.scope_key) == scope.key
            and int(matched_message.group(1)) != plan.current_message_id
        ):
            semantic_edges.append(
                TopicEdge(
                    plan.current_message_id,
                    int(matched_message.group(1)),
                    "semantic_similarity",
                    min(max(float(hit.score), 0.0), 1.0),
                    {
                        "model": str(
                            getattr(
                                getattr(semantic_recall, "embedder", None),
                                "model",
                                "",
                            )
                        ),
                        "score": round(float(hit.score), 4),
                    },
                )
            )
    if semantic_edges and topic_graph_store is not None:
        try:
            await asyncio.to_thread(topic_graph_store.upsert, scope, semantic_edges)
        except (OSError, RuntimeError, TypeError, ValueError):
            pass

    scope_rejections = tuple(
        {
            "handle": item.handle,
            "source": item.source,
            "selected": False,
            "raw_score": 0.0,
            "adjusted_score": 0.0,
            "decision_codes": ["scope_rejected"],
            "reason_codes": list(item.reason_codes),
            "content_preview": "",
            "scores": {},
            "scope_key": "[REDACTED]",
            "evidence_ids": [],
        }
        for item in candidates
        if not _scope_allowed(
            item,
            scope=scope,
            group_memory_scope=group_memory_scope,
            user_memory_scope=user_memory_scope,
        )
    )
    candidates = [
        item
        for item in candidates
        if _scope_allowed(
            item,
            scope=scope,
            group_memory_scope=group_memory_scope,
            user_memory_scope=user_memory_scope,
        )
    ]
    rerank_result = HybridReranker().rerank_with_audit(
        query,
        candidates,
        limit=12,
        relative_threshold=0.58,
    )
    ranked = rerank_result.selected
    grouped: dict[str, list[RankedRecall]] = {}
    for item in ranked:
        grouped.setdefault(item.candidate.source, []).append(item)

    focus_context = fit_token_budget(
        _render_ranked(
            "[引用与话题关系图证据]",
            grouped.get("relation_graph", []),
        ),
        budget.focus,
    )
    timeline_context = fit_token_budget(
        _render_ranked(
            "[当前群相关时间线]",
            grouped.get("group_timeline", []),
        ),
        budget.timeline,
    )
    recall_context = combine_budgeted_sections(
        [
            (
                "history",
                _render_ranked(
                    "[当前群旧消息证据]",
                    grouped.get("raw_history", []),
                ),
                budget.semantic,
            ),
            (
                "episodes",
                _render_ranked(
                    "[当前群 Historian 章节]",
                    grouped.get("historian_episode", []),
                ),
                budget.semantic,
            ),
            (
                "pins",
                _render_ranked(
                    "[当前群固定消息]",
                    grouped.get("pinned_message", []),
                ),
                budget.semantic,
            ),
            (
                "sources",
                _render_ranked(
                    "[当前群帖子、视频和分享来源]",
                    grouped.get("shared_source", []),
                ),
                budget.semantic,
            ),
            (
                "media",
                _render_ranked(
                    "[当前群媒体简介]",
                    grouped.get("media_summary", []),
                ),
                budget.semantic,
            ),
        ],
        total_budget=budget.semantic,
    )
    group_context = combine_budgeted_sections(
        [
            ("focus", focus_context, budget.focus),
            ("timeline", timeline_context, budget.timeline),
            ("recall", recall_context, budget.semantic),
        ],
        total_budget=budget.focus + budget.timeline + budget.semantic,
    )
    group_memory_context = fit_token_budget(
        _render_ranked(
            "[当前群公共记忆]",
            grouped.get("group_memory", []),
        ),
        budget.group_memory,
    )
    user_memory_context = fit_token_budget(
        _render_ranked(
            "[当前用户个人记忆]",
            grouped.get("user_memory", []),
        ),
        budget.user_memory,
    )
    memory_context = "\n\n".join(
        item for item in (group_memory_context, user_memory_context) if item
    )
    return HybridRecallContext(
        query=query,
        group_context=group_context,
        memory_context=memory_context,
        candidates=ranked,
        semantic_available=semantic_available,
        focus_context=focus_context,
        timeline_context=timeline_context,
        recall_context=recall_context,
        route_mode=route_mode,
        candidate_decisions=rerank_result.decisions,
        scope_rejections=scope_rejections,
        group_memory_context=group_memory_context,
        user_memory_context=user_memory_context,
    )


def _memory_candidate(
    entry: Any,
    source: str,
    now: int,
    *,
    fallback: bool,
) -> RecallCandidate:
    updated_at = int(entry.updated_at or entry.created_at or 0)
    source_message_id = getattr(entry, "source_message_id", None)
    return RecallCandidate(
        handle=f"memory#{entry.id}",
        source=source,  # type: ignore[arg-type]
        scope_key=str(entry.scope_key),
        content=str(entry.content),
        relation_score=0.9 if fallback else 0.0,
        recency_score=_recency(updated_at, now, horizon=90 * 86400),
        importance_score=0.7,
        actor_score=1.0 if source == "user_memory" else 0.55,
        reason_codes=(
            "explicit_memory_reference" if fallback else "memory_candidate",
            "exact_memory_scope",
        ),
        metadata={
            "memory_id": int(entry.id),
            "creator_user_id": getattr(entry, "creator_user_id", None),
            "creator_actor_key": str(getattr(entry, "creator_actor_key", "")),
            "creator_principal_id": int(
                getattr(entry, "creator_principal_id", 0) or 0
            ),
            "evidence_ids": [int(source_message_id)] if source_message_id else [],
        },
    )


def _message_candidate(
    message: CanonicalMessage,
    source: str,
    now: int,
    *,
    current_native_user_id: str | int,
    current_actor_key: str = "",
    relation_score: float = 0.0,
    importance_score: float = 0.45,
    reason: str,
    metadata: dict[str, object] | None = None,
) -> RecallCandidate:
    sender = message.sender_display or message.sender_native_user_id
    content = f"{sender}: {message.prompt_text}"
    return RecallCandidate(
        handle=f"msg#{message.canonical_message_id}",
        source=source,  # type: ignore[arg-type]
        scope_key=message.scope_key,
        content=content,
        relation_score=relation_score,
        recency_score=_recency(message.occurred_at, now, horizon=7 * 86400),
        importance_score=importance_score,
        actor_score=(
            0.8
            if message.is_from_actor(current_actor_key, current_native_user_id)
            else 0.5
        ),
        reason_codes=(reason, "same_conversation_scope"),
        metadata={
            "sender_native_user_id": str(message.sender_native_user_id),
            "sender_actor_key": message.sender_actor_key,
            "sender_principal_id": message.sender_principal_id,
            "occurred_at": message.occurred_at,
            "evidence_ids": [message.canonical_message_id],
            **(metadata or {}),
        },
    )


def _lexical_history_candidates(
    ledger: MessageLedger,
    scope: ConversationScope,
    query: str,
    *,
    now: int,
    current_message_id: int,
    excluded_ids: set[int],
    current_native_user_id: str | int,
    current_actor_key: str = "",
) -> list[RecallCandidate]:
    terms = topic_terms(query)
    if not terms:
        return []
    rows: list[tuple[float, CanonicalMessage]] = []
    for message in ledger.recent_in_scope(scope, 500):
        if (
            message.canonical_message_id == current_message_id
            or message.canonical_message_id in excluded_ids
            or message.message_kind == "command"
            or not message.prompt_text.strip()
        ):
            continue
        message_terms = topic_terms(message.prompt_text)
        overlap = len(terms.intersection(message_terms)) / max(len(terms), 1)
        if overlap < 0.12:
            continue
        rows.append((overlap, message))
    rows.sort(key=lambda item: (item[0], item[1].occurred_at), reverse=True)
    result: list[RecallCandidate] = []
    for overlap, message in rows[:12]:
        base = _message_candidate(
            message,
            "raw_history",
            now,
            current_native_user_id=current_native_user_id,
            current_actor_key=current_actor_key,
            reason="lexical_history",
        )
        result.append(
            RecallCandidate(
                **{
                    **base.__dict__,
                    "lexical_score": min(overlap, 1.0),
                    "topic_score": min(overlap, 1.0),
                }
            )
        )
    return result


def _episode_candidate(episode: Any, query: str) -> RecallCandidate | None:
    content = " ".join(
        item
        for item in (
            str(getattr(episode, "topic", "") or ""),
            str(getattr(episode, "summary_p2", "") or ""),
            str(getattr(episode, "summary_p1", "") or ""),
            str(getattr(episode, "summary_p4", "") or ""),
        )
        if item.strip()
    ).strip()
    if not content:
        return None
    query_terms = topic_terms(query)
    content_terms = topic_terms(content)
    overlap = len(query_terms.intersection(content_terms)) / max(len(query_terms), 1)
    return RecallCandidate(
        handle=f"episode#{episode.expand_handle}",
        source="historian_episode",
        scope_key=str(episode.scope_key),
        content=content,
        lexical_score=min(overlap, 1.0),
        topic_score=min(overlap, 1.0),
        importance_score=float(getattr(episode, "importance", 0.5) or 0.5),
        topic_conflict=0.45 if query_terms and overlap == 0 else 0.0,
        reason_codes=("historian_episode", "same_conversation_scope"),
        metadata={
            "confidence": float(getattr(episode, "confidence", 0.5) or 0.5),
            "evidence_ids": list(getattr(episode, "evidence_ids", ()) or ()),
            "participants": list(getattr(episode, "participants", ()) or ()),
        },
    )


def _source_candidate(
    scope: ConversationScope,
    source: Any,
    query: str,
    now: int,
) -> RecallCandidate:
    content = " ".join(
        item
        for item in (
            str(getattr(source, "platform", "") or ""),
            str(getattr(source, "content_kind", "") or ""),
            str(getattr(source, "title", "") or ""),
            str(getattr(source, "author", "") or ""),
            str(getattr(source, "summary", "") or ""),
            str(getattr(source, "body_text", "") or "")[:1000],
        )
        if item.strip()
    )
    overlap = _term_overlap(query, content)
    fetched_at = int(getattr(source, "fetched_at", 0) or 0)
    return RecallCandidate(
        handle=str(source.handle),
        source="shared_source",
        scope_key=scope.key,
        content=content,
        lexical_score=overlap,
        topic_score=overlap,
        recency_score=_recency(fetched_at, now, horizon=30 * 86400),
        importance_score=0.6,
        reason_codes=("visible_shared_source", "same_conversation_scope"),
        metadata={"platform": str(getattr(source, "platform", ""))},
    )


def _media_candidate(
    scope: ConversationScope,
    record: Any,
    query: str,
) -> RecallCandidate:
    content = " ".join(
        item
        for item in (
            str(getattr(record, "summary", "") or ""),
            str(getattr(record, "description", "") or ""),
            str(getattr(record, "extracted_text", "") or ""),
        )
        if item.strip()
    )
    overlap = _term_overlap(query, content)
    return RecallCandidate(
        handle=str(record.handle),
        source="media_summary",
        scope_key=scope.key,
        content=content,
        lexical_score=overlap,
        semantic_score=float(getattr(record, "score", 0.0) or 0.0),
        topic_score=overlap,
        importance_score=0.45,
        reason_codes=("visible_media_summary", "same_conversation_scope"),
        metadata={"media_id": int(record.media_id)},
    )


def _semantic_candidate(
    hit: Any,
    *,
    ledger: MessageLedger,
    scope: ConversationScope,
    memory_by_handle: dict[str, Any],
    active_episodes: dict[str, Any],
    now: int,
    group_memory_scope: str | None,
    user_memory_scope: str,
    current_native_user_id: str | int,
    current_actor_key: str = "",
) -> RecallCandidate | None:
    handle = str(hit.source_handle)
    if hit.source_type == "message":
        matched = _MESSAGE_HANDLE.fullmatch(handle)
        if matched is None or hit.scope_key != scope.key:
            return None
        message = ledger.get_in_scope(scope, int(matched.group(1)))
        if message is None:
            return None
        base = _message_candidate(
            message,
            "raw_history",
            now,
            current_native_user_id=current_native_user_id,
            current_actor_key=current_actor_key,
            reason="semantic_history",
        )
        return RecallCandidate(
            **{
                **base.__dict__,
                "semantic_score": float(hit.score),
                "topic_score": min(max(float(hit.score), 0.0), 1.0),
            }
        )
    if hit.source_type == "episode":
        matched = _EPISODE_HANDLE.fullmatch(handle)
        episode = active_episodes.get(handle) if matched is not None else None
        if episode is None or hit.scope_key != scope.key:
            return None
        base = _episode_candidate(episode, str(hit.content))
        if base is None:
            return None
        return RecallCandidate(
            **{
                **base.__dict__,
                "semantic_score": float(hit.score),
                "topic_conflict": 0.0,
                "reason_codes": (*base.reason_codes, "semantic_recall"),
            }
        )
    if hit.source_type == "memory":
        matched = _MEMORY_HANDLE.fullmatch(handle)
        entry = memory_by_handle.get(handle) if matched is not None else None
        if entry is None or hit.scope_key != entry.scope_key:
            return None
        if entry.scope_key == group_memory_scope:
            source = "group_memory"
        elif entry.scope_key == user_memory_scope:
            source = "user_memory"
        else:
            return None
        base = _memory_candidate(entry, source, now, fallback=False)
        return RecallCandidate(
            **{
                **base.__dict__,
                "semantic_score": float(hit.score),
                "topic_score": min(max(float(hit.score), 0.0), 1.0),
                "reason_codes": (*base.reason_codes, "semantic_recall"),
            }
        )
    return None


def _scope_allowed(
    candidate: RecallCandidate,
    *,
    scope: ConversationScope,
    group_memory_scope: str | None,
    user_memory_scope: str,
) -> bool:
    if candidate.source == "user_memory":
        return candidate.scope_key == user_memory_scope
    if candidate.source == "group_memory":
        return (
            group_memory_scope is not None
            and candidate.scope_key == group_memory_scope
        )
    return candidate.scope_key == scope.key


def _plan_relation(plan: TurnContextPlan | None, message_id: int) -> float:
    if plan is None:
        return 0.0
    if message_id == plan.focus_message_id:
        return 1.0
    if message_id in plan.related_message_ids:
        return 0.75
    for candidate in plan.candidates:
        if candidate.message_id == message_id:
            return candidate.relation_score
    return 0.0


def _term_overlap(query: str, content: str) -> float:
    query_terms = topic_terms(query)
    if not query_terms:
        return 0.0
    return min(
        len(query_terms.intersection(topic_terms(content))) / len(query_terms),
        1.0,
    )


def _recency(timestamp: int, now: int, *, horizon: int) -> float:
    if timestamp <= 0 or now <= 0:
        return 0.0
    age = max(now - timestamp, 0)
    return max(1.0 - age / max(int(horizon), 1), 0.0)


def _render_ranked(title: str, items: list[RankedRecall]) -> str:
    if not items:
        return ""
    lines = [title]
    lines.extend(
        f"- [{item.candidate.handle} · score={item.score:.2f}] "
        f"{item.candidate.content}"
        for item in items
    )
    return "\n".join(lines)
