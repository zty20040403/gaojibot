from __future__ import annotations

import re
import json
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Mapping

from src.bot_storage import DatabaseSource, open_store_connection, StoreCursor

from ..conversation_scope import ConversationScope
from ..ledger import CanonicalMessage
from ..message_ir import MentionNode


_LOW_INFORMATION = re.compile(
    r"^(?:哈+|呵+|啊+|哦+|嗯+|草+|笑死|确实|好吧|行吧|可以|收到|"
    r"[?？!！。.，,~～…]+)$",
    re.IGNORECASE,
)
_DEICTIC_CONTINUATION = re.compile(
    r"^(?:这个|那个|这|那|它|他|她|上面|前面|然后|所以|确实|但是|不过)"
)
_ANSWER_CONTINUATION = re.compile(
    r"^(?:可能|因为|原因|应该|建议|可以|不能|需要|看起来|我觉得|我认为|"
    r"大概|估计|确实|不是|是)"
)
_QUESTION_MARKERS = ("?", "？", "吗", "么", "什么", "怎么", "为什么", "咋")


@dataclass(frozen=True)
class TopicEdge:
    source_message_id: int
    target_message_id: int
    relation: str
    weight: float
    evidence: dict[str, object]


class TopicGraphStore:
    """Durable, scope-local evidence for why two messages share a topic."""

    def __init__(self, source: DatabaseSource) -> None:
        self.path, self._connection = open_store_connection(source)
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def upsert(
        self,
        scope: ConversationScope,
        edges: tuple[TopicEdge, ...] | list[TopicEdge],
    ) -> int:
        if not edges:
            return 0
        timestamp = int(time.time())
        changed = 0
        with self._transaction() as cursor:
            for edge in edges:
                if edge.source_message_id == edge.target_message_id:
                    continue
                cursor.execute(
                    """
                    INSERT INTO message_topic_edges (
                        scope_key, source_message_id, target_message_id,
                        relation, weight, evidence_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(
                        scope_key, source_message_id, target_message_id, relation
                    ) DO UPDATE SET
                        weight = excluded.weight,
                        evidence_json = excluded.evidence_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        scope.key,
                        int(edge.source_message_id),
                        int(edge.target_message_id),
                        edge.relation[:80],
                        min(max(float(edge.weight), 0.0), 1.0),
                        json.dumps(
                            edge.evidence,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        timestamp,
                        timestamp,
                    ),
                )
                changed += max(int(cursor.rowcount), 0)
        return changed

    def upsert_semantic(
        self,
        scope: ConversationScope,
        source_message_id: int,
        target_message_id: int,
        score: float,
        *,
        model: str = "",
    ) -> int:
        if int(source_message_id) == int(target_message_id):
            return 0
        return self.upsert(
            scope,
            [
                TopicEdge(
                    int(source_message_id),
                    int(target_message_id),
                    "semantic_similarity",
                    min(max(float(score), 0.0), 1.0),
                    {"model": model, "score": round(float(score), 4)},
                )
            ],
        )

    @contextmanager
    def _transaction(self) -> Iterator[StoreCursor]:
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute("BEGIN")
                yield cursor
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()
            finally:
                cursor.close()


def topic_terms(text: str) -> set[str]:
    folded = str(text).casefold()
    terms = {
        match.group(0)
        for match in re.finditer(r"[a-z0-9][a-z0-9_.+#/-]{1,}", folded)
    }
    for run in re.findall(r"[\u3400-\u9fff]+", folded):
        terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return terms.difference(
        {
            "一个",
            "一下",
            "为什么",
            "什么",
            "可以",
            "怎么",
            "这个",
            "那个",
            "觉得",
            "认为",
            "然后",
            "所以",
        }
    )


class MessageReferenceGraph:
    """A scope-local weighted graph over immutable message nodes."""

    def __init__(
        self,
        messages: list[CanonicalMessage],
        *,
        semantic_scores: Mapping[tuple[int, int], float] | None = None,
    ) -> None:
        self.messages = {
            message.canonical_message_id: message for message in messages
        }
        self.parents: dict[int, int] = {}
        self.children: dict[int, list[int]] = {}
        self._edges: dict[tuple[int, int, str], TopicEdge] = {}
        ordered = sorted(messages, key=lambda item: item.canonical_message_id)
        previous_by_sender: dict[str, CanonicalMessage] = {}
        for index, message in enumerate(ordered):
            parent = message.reply_to_canonical_message_id
            if parent is not None and parent in self.messages:
                self.parents[message.canonical_message_id] = parent
                self.children.setdefault(parent, []).append(
                    message.canonical_message_id
                )
                relation = "bot_reply" if message.direction == "outbound" else "reply"
                self._add_edge(message, self.messages[parent], relation, 1.0)

            sender_key = (
                f"principal:{message.sender_principal_id}"
                if message.sender_principal_id is not None
                else f"native:{message.sender_native_user_id}"
            )
            previous_sender = previous_by_sender.get(sender_key)
            if (
                previous_sender is not None
                and message.occurred_at - previous_sender.occurred_at <= 900
            ):
                self._add_edge(
                    message,
                    previous_sender,
                    "same_sender_continuation",
                    0.58,
                )
            previous_by_sender[sender_key] = message

            if index:
                previous = ordered[index - 1]
                gap = max(message.occurred_at - previous.occurred_at, 0)
                if gap <= 120:
                    self._add_edge(
                        message,
                        previous,
                        "temporal_proximity",
                        max(0.18, 0.38 - gap / 600),
                        {"gap_seconds": gap},
                    )

            for mentioned_principal in self._mentioned_principals(message):
                target = next(
                    (
                        item
                        for item in reversed(ordered[:index])
                        if item.sender_principal_id == mentioned_principal
                    ),
                    None,
                )
                # Mentioning the bot invokes it; it does not make the bot's last
                # reply the subject of the new message.
                if target is not None and target.direction != "outbound":
                    self._add_edge(
                        message,
                        target,
                        "mention",
                        0.88,
                        {"mentioned_principal_id": mentioned_principal},
                    )

            message_terms = topic_terms(message.prompt_text)
            for candidate in reversed(ordered[max(0, index - 12) : index]):
                gap = max(message.occurred_at - candidate.occurred_at, 0)
                if gap > 1800:
                    break
                candidate_terms = topic_terms(candidate.prompt_text)
                union = message_terms | candidate_terms
                similarity = (
                    len(message_terms & candidate_terms) / len(union)
                    if union
                    else 0.0
                )
                if similarity >= 0.18:
                    self._add_edge(
                        message,
                        candidate,
                        "lexical_topic",
                        min(0.35 + similarity * 0.55, 0.85),
                        {"jaccard": round(similarity, 4)},
                    )
                if (
                    self._is_question(candidate.prompt_text)
                    and not self._is_question(message.prompt_text)
                    and message.sender_native_user_id
                    != candidate.sender_native_user_id
                    and gap <= 300
                    and (
                        similarity >= 0.1
                        or bool(
                            _DEICTIC_CONTINUATION.match(
                                message.prompt_text.strip()
                            )
                        )
                        or bool(
                            _ANSWER_CONTINUATION.match(
                                message.prompt_text.strip()
                            )
                        )
                        or message.reply_to_canonical_message_id
                        == candidate.canonical_message_id
                    )
                ):
                    self._add_edge(
                        message,
                        candidate,
                        "question_answer",
                        0.66,
                        {"gap_seconds": gap},
                    )

        for pair, score in (semantic_scores or {}).items():
            source = self.messages.get(int(pair[0]))
            target = self.messages.get(int(pair[1]))
            if source is not None and target is not None and float(score) >= 0.5:
                self._add_edge(
                    source,
                    target,
                    "semantic_similarity",
                    float(score),
                    {"score": round(float(score), 4)},
                )
        for children in self.children.values():
            children.sort()

    @property
    def edges(self) -> tuple[TopicEdge, ...]:
        return tuple(self._edges.values())

    def connection_score(
        self,
        message_id: int,
        *,
        reference_message_id: int | None = None,
    ) -> tuple[float, tuple[str, ...]]:
        related = [
            edge
            for edge in self._edges.values()
            if int(message_id) in {edge.source_message_id, edge.target_message_id}
            and (
                reference_message_id is None
                or int(reference_message_id)
                in {edge.source_message_id, edge.target_message_id}
            )
        ]
        if not related:
            return 0.0, ()
        weights = sorted((edge.weight for edge in related), reverse=True)
        score = min(weights[0] + sum(weights[1:4]) * 0.18, 1.0)
        relations = tuple(
            dict.fromkeys(
                edge.relation
                for edge in sorted(related, key=lambda item: item.weight, reverse=True)
            )
        )
        return score, relations[:5]

    def strongly_related_ids(
        self,
        focus_message_id: int,
        current_message_id: int,
        *,
        limit: int = 8,
    ) -> tuple[int, ...]:
        """Return topical evidence without weak mention/time/sender-only links."""
        selected = {
            *self.ancestors(focus_message_id, limit=3),
            *self.descendants(
                focus_message_id,
                before_message_id=current_message_id,
                limit=limit,
            ),
        }
        topical_relations = {
            "reply",
            "bot_reply",
            "question_answer",
            "lexical_topic",
            "semantic_similarity",
        }
        neighbors: dict[int, float] = {}
        for edge in self._edges.values():
            if edge.relation not in topical_relations:
                continue
            if edge.source_message_id == focus_message_id:
                neighbor = edge.target_message_id
            elif edge.target_message_id == focus_message_id:
                neighbor = edge.source_message_id
            else:
                continue
            if neighbor >= current_message_id:
                continue
            neighbors[neighbor] = max(neighbors.get(neighbor, 0.0), edge.weight)
        for message_id, _weight in sorted(
            neighbors.items(),
            key=lambda item: (item[1], item[0]),
            reverse=True,
        )[:limit]:
            selected.add(message_id)
        return tuple(
            message_id
            for message_id in sorted(selected)[-max(int(limit), 0) :]
            if message_id != focus_message_id
        )

    def root(self, message_id: int) -> int:
        current = int(message_id)
        visited: set[int] = set()
        while current in self.parents and current not in visited:
            visited.add(current)
            current = self.parents[current]
        return current

    def ancestors(self, message_id: int, *, limit: int = 6) -> tuple[int, ...]:
        current = int(message_id)
        result: list[int] = []
        visited: set[int] = set()
        while current in self.parents and len(result) < max(int(limit), 0):
            current = self.parents[current]
            if current in visited:
                break
            visited.add(current)
            result.append(current)
        result.reverse()
        return tuple(result)

    def descendants(
        self,
        message_id: int,
        *,
        before_message_id: int,
        limit: int = 12,
    ) -> tuple[int, ...]:
        queue = deque(self.children.get(int(message_id), ()))
        found: list[int] = []
        visited: set[int] = set()
        while queue and len(found) < max(int(limit), 0):
            candidate = queue.popleft()
            if candidate in visited or candidate >= int(before_message_id):
                continue
            visited.add(candidate)
            found.append(candidate)
            queue.extend(self.children.get(candidate, ()))
        return tuple(sorted(found))

    def related_ids(
        self,
        focus_message_id: int,
        current_message_id: int,
        *,
        limit: int = 8,
    ) -> tuple[int, ...]:
        focus = self.messages.get(int(focus_message_id))
        if focus is None:
            return ()
        hard_links = [
            *self.ancestors(focus_message_id, limit=3),
            *self.descendants(
                focus_message_id,
                before_message_id=current_message_id,
                limit=limit,
            ),
        ]
        selected = set(hard_links)
        graph_neighbors: dict[int, float] = {}
        for edge in self._edges.values():
            if edge.source_message_id == focus_message_id:
                neighbor = edge.target_message_id
            elif edge.target_message_id == focus_message_id:
                neighbor = edge.source_message_id
            else:
                continue
            if neighbor >= current_message_id or edge.weight < 0.45:
                continue
            graph_neighbors[neighbor] = max(graph_neighbors.get(neighbor, 0.0), edge.weight)
        for message_id, _weight in sorted(
            graph_neighbors.items(),
            key=lambda item: (item[1], item[0]),
            reverse=True,
        )[:limit]:
            selected.add(message_id)
        focus_terms = topic_terms(focus.prompt_text)
        chronological = sorted(
            (
                message
                for message in self.messages.values()
                if focus_message_id
                < message.canonical_message_id
                < current_message_id
            ),
            key=lambda item: item.canonical_message_id,
        )
        last_time = focus.occurred_at
        for message in chronological:
            if message.canonical_message_id in selected:
                focus_terms.update(topic_terms(message.prompt_text))
                last_time = message.occurred_at
                continue
            age = max(message.occurred_at - last_time, 0)
            terms = topic_terms(message.prompt_text)
            overlap = bool(focus_terms.intersection(terms))
            continuation = bool(
                _DEICTIC_CONTINUATION.match(message.prompt_text.strip())
            )
            if age <= 300 and (overlap or continuation):
                selected.add(message.canonical_message_id)
                focus_terms.update(terms)
                last_time = message.occurred_at

        ordered = [
            message_id
            for message_id in sorted(selected)
            if message_id != focus_message_id
        ]
        return tuple(ordered[-max(int(limit), 0) :])

    def _add_edge(
        self,
        source: CanonicalMessage,
        target: CanonicalMessage,
        relation: str,
        weight: float,
        evidence: dict[str, object] | None = None,
    ) -> None:
        if source.canonical_message_id == target.canonical_message_id:
            return
        key = (
            source.canonical_message_id,
            target.canonical_message_id,
            relation,
        )
        edge = TopicEdge(
            source.canonical_message_id,
            target.canonical_message_id,
            relation,
            min(max(float(weight), 0.0), 1.0),
            dict(evidence or {}),
        )
        existing = self._edges.get(key)
        if existing is None or edge.weight > existing.weight:
            self._edges[key] = edge

    @staticmethod
    def _mentioned_principals(message: CanonicalMessage) -> tuple[int, ...]:
        return tuple(
            dict.fromkeys(
                int(node.principal_id)
                for node in message.body.nodes
                if isinstance(node, MentionNode) and node.principal_id is not None
            )
        )

    @staticmethod
    def _is_question(text: str) -> bool:
        compact = " ".join(str(text).split()).casefold()
        return any(marker in compact for marker in _QUESTION_MARKERS)

    def topic_query(
        self,
        focus_message_id: int,
        related_message_ids: tuple[int, ...],
        *,
        max_chars: int = 900,
    ) -> str:
        ordered_ids = tuple(
            dict.fromkeys((int(focus_message_id), *related_message_ids))
        )
        snippets: list[str] = []
        used = 0
        for message_id in ordered_ids:
            message = self.messages.get(message_id)
            if message is None:
                continue
            text = " ".join(message.prompt_text.split()).strip()
            if not text or _LOW_INFORMATION.fullmatch(text):
                continue
            remaining = max(int(max_chars) - used, 0)
            if remaining <= 0:
                break
            snippets.append(text[:remaining])
            used += len(snippets[-1]) + 1
        return " ".join(snippets)
