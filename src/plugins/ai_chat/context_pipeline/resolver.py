from __future__ import annotations

import re
from datetime import datetime

from src.bot_storage import DatabaseError

from ..conversation_scope import ConversationScope
from ..ledger import CanonicalMessage, MessageLedger
from .graph import MessageReferenceGraph, TopicGraphStore
from .models import ContextCandidate, TurnContextPlan


_FOLLOW_UP_PATTERNS = (
    r"你(?:怎么|咋)看",
    r"你觉得(?:呢|怎么样)?",
    r"你认为(?:呢|怎么样)?",
    r"^(?:那|这|这个|那个|这件事|这话|上面|上面那个)(?:呢|怎么样|怎么说|啥意思)?[？?]?$",
    r"^(?:为什么|为啥|然后呢|后来呢|真的吗|是吗|对吗|咋办|怎么办)[？?]?$",
    r"^(?:怎么说|你说呢|评价一下|锐评|细说|展开说说|接着说)[吧呢？?]*$",
)
_QUESTION_MARKERS = (
    "?",
    "？",
    "吗",
    "么",
    "怎么",
    "为什么",
    "为啥",
    "如何",
    "是否",
    "哪个",
    "什么",
    "谁",
    "几",
    "能不能",
    "要不要",
    "是不是",
    "咋",
)
_QUERY_FILLERS = (
    "你觉得",
    "你认为",
    "你怎么看",
    "你咋看",
    "这个",
    "那个",
    "这件事",
    "评价一下",
    "锐评",
    "一下",
    "呢",
    "吗",
)
_LOW_INFORMATION = re.compile(
    r"^(?:哈+|呵+|啊+|哦+|嗯+|草+|笑死|确实|好吧|行吧|可以|收到|[?？!！。.，,~～…]+)$",
    re.IGNORECASE,
)
_DEICTIC_FOLLOW_UP = re.compile(
    r"^(?:(?:这个|那个|这|那|它|他|她|上面|上面那个|前面)(?:呢|怎么样|"
    r"怎么说|咋样|是什么|啥意思)?|评价一下|锐评|细说|展开说说)[？?]?$",
    re.IGNORECASE,
)
_OPINION_FOLLOW_UP = re.compile(
    r"^(?:你(?:觉得|认为)(?:呢|怎么样)?|你(?:怎么|咋)看|你说呢|"
    r"评价一下|锐评|细说|展开说说)[吧呢？?]*$",
    re.IGNORECASE,
)


class ReferenceResolver:
    def __init__(
        self,
        *,
        candidate_limit: int = 40,
        related_limit: int = 8,
        graph_store: TopicGraphStore | None = None,
    ) -> None:
        self.candidate_limit = min(max(int(candidate_limit), 8), 100)
        self.related_limit = min(max(int(related_limit), 2), 20)
        self.graph_store = graph_store

    def resolve(
        self,
        ledger: MessageLedger,
        scope: ConversationScope,
        *,
        current_message_id: int,
        current_text: str,
        current_native_user_id: str | int,
        current_actor_key: str = "",
        now: int | None = None,
        prefer_latest: bool = False,
    ) -> TurnContextPlan:
        current = ledger.get_in_scope(scope, current_message_id)
        current_principal_id = current.sender_principal_id if current else None
        recent = [
            message
            for message in ledger.recent_in_scope(scope, self.candidate_limit + 1)
            if message.canonical_message_id < current_message_id
            and message.message_kind != "command"
            and bool(message.prompt_text.strip())
        ]
        graph_messages = [*recent]
        if current is not None:
            graph_messages.append(current)
        graph = MessageReferenceGraph(graph_messages)
        if self.graph_store is not None:
            try:
                self.graph_store.upsert(
                    scope,
                    tuple(
                        edge
                        for edge in graph.edges
                        if current_message_id
                        in {edge.source_message_id, edge.target_message_id}
                    ),
                )
            except (OSError, RuntimeError, DatabaseError):
                pass
        explicit_target = (
            ledger.get_in_scope(scope, current.reply_to_canonical_message_id)
            if current is not None
            and current.reply_to_canonical_message_id is not None
            else None
        )
        if explicit_target is not None:
            related = graph.related_ids(
                explicit_target.canonical_message_id,
                current_message_id,
                limit=self.related_limit,
            )
            topic_id = graph.root(explicit_target.canonical_message_id)
            topic_message_ids = tuple(
                dict.fromkeys((topic_id, explicit_target.canonical_message_id, *related))
            )
            rendered = self._render(
                explicit_target,
                self._messages_by_ids(recent, related),
                ambiguous=False,
            )
            return TurnContextPlan(
                scope_key=scope.key,
                current_message_id=current_message_id,
                current_principal_id=current_principal_id,
                focus_message_id=explicit_target.canonical_message_id,
                confidence=1.0,
                reason_codes=("explicit_reply", "same_scope"),
                related_message_ids=related,
                candidates=(
                    ContextCandidate(
                        explicit_target.canonical_message_id,
                        100.0,
                        ("explicit_reply", "same_scope"),
                        relation_score=1.0,
                        recency_score=1.0,
                    ),
                ),
                rendered_context=rendered,
                topic_id=topic_id,
                topic_message_ids=topic_message_ids,
                topic_query=graph.topic_query(
                    explicit_target.canonical_message_id,
                    related,
                ),
            )

        if prefer_latest and recent:
            focus = next(
                (
                    message
                    for message in reversed(recent)
                    if message.direction == "inbound"
                ),
                recent[-1],
            )
            related = graph.related_ids(
                focus.canonical_message_id,
                current_message_id,
                limit=self.related_limit,
            )
            topic_id = graph.root(focus.canonical_message_id)
            return TurnContextPlan(
                scope_key=scope.key,
                current_message_id=current_message_id,
                current_principal_id=current_principal_id,
                focus_message_id=focus.canonical_message_id,
                confidence=0.95,
                reason_codes=("empty_mention_latest", "same_scope"),
                related_message_ids=related,
                candidates=(
                    ContextCandidate(
                        focus.canonical_message_id,
                        95.0,
                        ("empty_mention_latest", "same_scope"),
                        relation_score=0.95,
                        recency_score=1.0,
                    ),
                ),
                rendered_context=self._render(
                    focus,
                    self._messages_by_ids(recent, related),
                    ambiguous=False,
                ),
                topic_id=topic_id,
                topic_message_ids=tuple(
                    dict.fromkeys((topic_id, focus.canonical_message_id, *related))
                ),
                topic_query=graph.topic_query(
                    focus.canonical_message_id,
                    related,
                ),
            )

        follow_up = self._looks_like_follow_up(current_text)
        if not follow_up or not recent:
            return TurnContextPlan(
                scope_key=scope.key,
                current_message_id=current_message_id,
                current_principal_id=current_principal_id,
                focus_message_id=None,
                confidence=0.0,
                reason_codes=("standalone_message",),
                related_message_ids=(),
                candidates=(),
                rendered_context="",
                topic_query=" ".join(current_text.split()).strip(),
            )

        timestamp = int(now if now is not None else current.occurred_at if current else 0)
        latest_anchor_follow_up = bool(
            _OPINION_FOLLOW_UP.fullmatch(" ".join(current_text.split()).strip())
            or _DEICTIC_FOLLOW_UP.fullmatch(" ".join(current_text.split()).strip())
        )
        if latest_anchor_follow_up:
            focus = self._latest_human_anchor(recent)
            if focus is not None:
                related = graph.strongly_related_ids(
                    focus.canonical_message_id,
                    current_message_id,
                    limit=self.related_limit,
                )
                topic_id = graph.root(focus.canonical_message_id)
                reasons = ("adjacent_human_anchor", "same_scope")
                return TurnContextPlan(
                    scope_key=scope.key,
                    current_message_id=current_message_id,
                    current_principal_id=current_principal_id,
                    focus_message_id=focus.canonical_message_id,
                    confidence=0.99,
                    reason_codes=reasons,
                    related_message_ids=related,
                    candidates=(
                        ContextCandidate(
                            focus.canonical_message_id,
                            160.0,
                            reasons,
                            relation_score=1.0,
                            recency_score=1.0,
                        ),
                    ),
                    rendered_context=self._render(
                        focus,
                        self._messages_by_ids(recent, related),
                        ambiguous=False,
                    ),
                    topic_id=topic_id,
                    topic_message_ids=tuple(
                        dict.fromkeys((topic_id, focus.canonical_message_id, *related))
                    ),
                    topic_query=graph.topic_query(
                        focus.canonical_message_id,
                        related,
                    ),
                )
        query_terms = self._query_terms(current_text)
        deictic_follow_up = bool(
            _DEICTIC_FOLLOW_UP.fullmatch(" ".join(current_text.split()).strip())
        )
        scored: list[tuple[CanonicalMessage, ContextCandidate]] = []
        for distance, message in enumerate(reversed(recent)):
            graph_score, graph_relations = graph.connection_score(
                message.canonical_message_id,
                reference_message_id=current_message_id,
            )
            score, reasons, components = self._score(
                message,
                distance=distance,
                now=timestamp,
                current_native_user_id=str(current_native_user_id),
                current_actor_key=current_actor_key,
                query_terms=query_terms,
                deictic_follow_up=deictic_follow_up,
                reply_count=len(graph.children.get(message.canonical_message_id, ())),
                topic_support_count=len(
                    graph.related_ids(
                        message.canonical_message_id,
                        current_message_id,
                        limit=3,
                    )
                ),
                graph_score=graph_score,
                graph_relations=graph_relations,
            )
            if score <= 0:
                continue
            scored.append(
                (
                    message,
                    ContextCandidate(
                        message_id=message.canonical_message_id,
                        score=round(score, 2),
                        reason_codes=tuple(reasons),
                        lexical_score=components["lexical"],
                        relation_score=components["relation"],
                        recency_score=components["recency"],
                    ),
                )
            )
        scored.sort(
            key=lambda item: (
                item[1].score,
                item[0].canonical_message_id,
            ),
            reverse=True,
        )
        top = scored[0] if scored else None
        runner_up = scored[1] if len(scored) > 1 else None
        if top is None or top[1].score < 45:
            candidates = tuple(item[1] for item in scored[:5])
            return TurnContextPlan(
                scope_key=scope.key,
                current_message_id=current_message_id,
                current_principal_id=current_principal_id,
                focus_message_id=None,
                confidence=0.0,
                reason_codes=("no_reliable_focus",),
                related_message_ids=(),
                candidates=candidates,
                rendered_context=(
                    "[追问指向不明确] 如果近期消息仍不足以确认对象，先简短问清楚。"
                ),
                topic_query=" ".join(current_text.split()).strip(),
            )

        margin = top[1].score - (runner_up[1].score if runner_up else 0.0)
        confidence = min(
            0.99,
            max(
                0.45,
                (top[1].score / 100.0)
                * (0.78 + min(max(margin, 0.0) / 50.0, 0.22)),
            ),
        )
        ambiguous = runner_up is not None and margin < 8
        if ambiguous:
            confidence = min(confidence, 0.59)
        focus = top[0]
        related = graph.related_ids(
            focus.canonical_message_id,
            current_message_id,
            limit=self.related_limit,
        )
        topic_id = graph.root(focus.canonical_message_id)
        reasons = tuple(dict.fromkeys((*top[1].reason_codes, "same_scope")))
        rendered = self._render(
            focus,
            self._messages_by_ids(recent, related),
            ambiguous=ambiguous,
        )
        return TurnContextPlan(
            scope_key=scope.key,
            current_message_id=current_message_id,
            current_principal_id=current_principal_id,
            focus_message_id=focus.canonical_message_id,
            confidence=round(confidence, 4),
            reason_codes=reasons,
            related_message_ids=related,
            candidates=tuple(item[1] for item in scored[:5]),
            rendered_context=rendered,
            topic_id=topic_id,
            topic_message_ids=tuple(
                dict.fromkeys((topic_id, focus.canonical_message_id, *related))
            ),
            topic_query=graph.topic_query(
                focus.canonical_message_id,
                related,
            ),
        )

    @classmethod
    def _looks_like_follow_up(cls, text: str) -> bool:
        compact = " ".join(str(text).split()).strip()
        if not compact:
            return False
        return len(compact) <= 40 and any(
            re.fullmatch(pattern, compact, re.IGNORECASE)
            for pattern in _FOLLOW_UP_PATTERNS
        )

    @classmethod
    def _latest_human_anchor(
        cls,
        recent: list[CanonicalMessage],
    ) -> CanonicalMessage | None:
        fallback: CanonicalMessage | None = None
        for message in reversed(recent):
            if message.direction != "inbound":
                continue
            text = " ".join(message.prompt_text.split()).strip()
            if not text or _LOW_INFORMATION.fullmatch(text):
                continue
            fallback = fallback or message
            if not cls._looks_like_follow_up(text):
                return message
        return fallback

    @classmethod
    def _score(
        cls,
        message: CanonicalMessage,
        *,
        distance: int,
        now: int,
        current_native_user_id: str,
        current_actor_key: str = "",
        query_terms: tuple[str, ...],
        deictic_follow_up: bool,
        reply_count: int,
        topic_support_count: int,
        graph_score: float,
        graph_relations: tuple[str, ...],
    ) -> tuple[float, list[str], dict[str, float]]:
        text = " ".join(message.prompt_text.split()).strip()
        score = max(42.0 - distance * 6.0, 4.0)
        reasons = ["recent_message"]
        age = max(now - message.occurred_at, 0) if now else 0
        if age <= 300:
            score += 15
            reasons.append("within_5m")
        elif age <= 1800:
            score += 8
            reasons.append("within_30m")
        if cls._is_question(text):
            score += 32
            reasons.append("recent_question")
        if message.direction == "inbound":
            score += 5
            reasons.append("human_message")
        else:
            score -= 12
            reasons.append("bot_message_penalty")
        if not message.is_from_actor(current_actor_key, current_native_user_id):
            score += 8
            reasons.append("other_participant")
        if _LOW_INFORMATION.fullmatch(text):
            score -= 35
            reasons.append("low_information_penalty")
        overlap = cls._term_overlap(query_terms, text)
        if overlap:
            score += min(overlap * 7, 21)
            reasons.append("lexical_overlap")
        if message.reply_to_canonical_message_id is not None:
            score += 3
            reasons.append("reply_chain")
        if reply_count:
            score += min(reply_count * 4, 12)
            reasons.append("has_reply_descendants")
        if topic_support_count:
            score += min(topic_support_count * 14, 28)
            reasons.append("established_topic")
        if graph_score:
            score += min(graph_score * 18, 18)
            reasons.extend(f"graph:{relation}" for relation in graph_relations[:3])
        if deictic_follow_up:
            if distance == 0:
                score += 34
                reasons.append("deictic_latest")
            else:
                score -= min(distance * 4, 16)
        lexical = min(overlap / max(len(query_terms), 1), 1.0)
        relation = min(
            (0.45 if cls._is_question(text) else 0.15)
            + (0.2 if message.reply_to_canonical_message_id is not None else 0.0)
            + min(reply_count * 0.15, 0.3)
            + min(topic_support_count * 0.15, 0.3)
            + min(graph_score * 0.25, 0.25)
            + (0.35 if deictic_follow_up and distance == 0 else 0.0),
            1.0,
        )
        recency = 1.0 if age <= 300 else 0.7 if age <= 1800 else max(
            1.0 - distance / 10.0,
            0.0,
        )
        return max(score, 0.0), reasons, {
            "lexical": round(lexical, 4),
            "relation": round(relation, 4),
            "recency": round(recency, 4),
        }

    @staticmethod
    def _is_question(text: str) -> bool:
        compact = text.strip().casefold()
        return any(marker in compact for marker in _QUESTION_MARKERS)

    @classmethod
    def _query_terms(cls, text: str) -> tuple[str, ...]:
        normalized = re.sub(r"[\W_]+", "", text.casefold())
        for filler in _QUERY_FILLERS:
            normalized = normalized.replace(
                re.sub(r"[\W_]+", "", filler.casefold()),
                "",
            )
        if len(normalized) < 2:
            return ()
        return tuple(
            dict.fromkeys(
                normalized[index : index + 2]
                for index in range(len(normalized) - 1)
            )
        )

    @staticmethod
    def _term_overlap(terms: tuple[str, ...], text: str) -> int:
        normalized = re.sub(r"[\W_]+", "", text.casefold())
        return sum(1 for term in terms if term in normalized)

    @staticmethod
    def _messages_by_ids(
        recent: list[CanonicalMessage],
        message_ids: tuple[int, ...],
    ) -> list[CanonicalMessage]:
        wanted = set(message_ids)
        return [
            message
            for message in recent
            if message.canonical_message_id in wanted
        ]

    @classmethod
    def _render(
        cls,
        focus: CanonicalMessage,
        related: list[CanonicalMessage],
        *,
        ambiguous: bool,
    ) -> str:
        lines = [
            "[当前群追问焦点]",
            cls._message_line(focus),
        ]
        if related:
            lines.append("[相关消息]")
            lines.extend(cls._message_line(message) for message in related)
        if ambiguous:
            lines.append(
                "候选接近；证据不足时先问清楚指代。"
            )
        else:
            lines.append("围绕上述焦点回答，不要转去该用户的旧私聊。")
        return "\n".join(lines)

    @staticmethod
    def _message_line(message: CanonicalMessage) -> str:
        stamp = datetime.fromtimestamp(message.occurred_at).strftime("%m-%d %H:%M")
        sender = (
            f"[mention#{message.sender_principal_id}] {message.sender_display}"
            if message.sender_principal_id is not None
            else message.sender_display
        )
        return (
            f"[msg#{message.canonical_message_id} | {stamp} | {sender}] "
            f"{message.prompt_text[:500]}"
        )
