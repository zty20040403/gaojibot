from __future__ import annotations

import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterator, Mapping, Sequence

from src.bot_storage import DatabaseSource, open_store_connection, StoreCursor

from .context_store import CaptureCandidate, ContextStore
from .conversation_scope import ConversationScope
from .memory_scopes import group_memory_scope
from .ledger import MessageLedger
from .long_term_memory import LongTermMemoryError, LongTermMemoryStore, MemoryEntry
from .storage.jobs import DurableJob, DurableJobStore


@dataclass(frozen=True)
class MemoryProposal:
    content: str
    source_message_id: int


@dataclass(frozen=True)
class HistorianResult:
    summary_p1: str
    summary_p2: str
    summary_p3: str
    memories: tuple[MemoryProposal, ...] = ()
    summary_p4: str = ""
    topic: str = ""
    importance: float = 0.5
    confidence: float = 0.5
    participants: tuple[str, ...] = ()
    evidence_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class HistorianRun:
    published: int
    memories_added: int
    failures: tuple[str, ...]


HistorianGenerator = Callable[[CaptureCandidate], Awaitable[HistorianResult]]
ProtectedProvider = Callable[[ConversationScope], Sequence[int]]


class HistorianService:
    def __init__(
        self,
        ledger: MessageLedger,
        context_store: ContextStore,
        memory_store: LongTermMemoryStore,
        generator: HistorianGenerator,
        *,
        protected_provider: ProtectedProvider | None = None,
    ) -> None:
        self.ledger = ledger
        self.context_store = context_store
        self.memory_store = memory_store
        self.generator = generator
        self.protected_provider = protected_provider

    def schedule_due(
        self,
        job_store: DurableJobStore,
        *,
        idle_seconds: int = 600,
        max_scopes: int = 20,
        max_attempts: int = 4,
        now: int | None = None,
    ) -> int:
        timestamp = int(time.time() if now is None else now)
        idle_cutoff = timestamp - max(int(idle_seconds), 60)
        scheduled = 0
        for scope in self.ledger.list_scopes()[: max(int(max_scopes), 1)]:
            recent = self.ledger.recent_in_scope(scope, 1)
            if not recent or recent[-1].occurred_at > idle_cutoff:
                continue
            candidate = self.context_store.capture_candidate(
                self.ledger,
                scope,
                protected_message_ids=self._protected(scope),
                settled=True,
            )
            if candidate is None:
                continue
            payload = {
                "scope_key": scope.key,
                "scope": {
                    "platform": scope.platform,
                    "kind": scope.kind,
                    "native_conversation_id": scope.native_conversation_id,
                },
                "expected_cursor": candidate.expected_cursor,
                "source_message_ids": [
                    item.canonical_message_id for item in candidate.messages
                ],
                "source_hash": candidate.source_hash,
                "last_activity_at": recent[-1].occurred_at,
                "scheduled_at": timestamp,
            }
            _job, created = job_store.enqueue(
                kind="context.historian_capture",
                idempotency_key=(
                    f"context.historian:{scope.key}:"
                    f"{candidate.expected_cursor}:{candidate.source_hash}"
                ),
                scope_key=scope.key,
                payload=payload,
                priority=30,
                max_attempts=max_attempts,
                now=timestamp,
            )
            scheduled += int(created)
        return scheduled

    async def handle_job(self, job: DurableJob) -> Mapping[str, Any]:
        candidate, scope = self._candidate_from_job(job)
        generation_mode = "historian"
        try:
            generated = await self.generator(candidate)
            self._validate(candidate, generated)
        except Exception:
            if job.attempts < job.max_attempts:
                raise
            generated = self._fallback(candidate)
            generation_mode = "fallback"
        compartment, memories_added = self._publish_result(
            scope,
            candidate,
            generated,
            generation_mode=generation_mode,
        )
        return {
            "episode_handle": compartment.expand_handle,
            "generation_mode": generation_mode,
            "start_message_id": compartment.start_message_id,
            "end_message_id": compartment.end_message_id,
            "source_hash": compartment.source_hash,
            "memories_added": memories_added,
        }

    async def run_once(self, *, max_scopes: int = 20) -> HistorianRun:
        published = 0
        memories_added = 0
        failures: list[str] = []
        for scope in self.ledger.list_scopes()[: max(int(max_scopes), 1)]:
            candidate = self.context_store.capture_candidate(
                self.ledger,
                scope,
                protected_message_ids=self._protected(scope),
            )
            if candidate is None:
                continue
            try:
                generated = await self.generator(candidate)
                self._validate(candidate, generated)
                _compartment, added = self._publish_result(
                    scope,
                    candidate,
                    generated,
                    generation_mode="historian",
                )
                published += 1
                memories_added += added
            except Exception as exc:
                failures.append(f"{scope.key}: {exc}")
        return HistorianRun(published, memories_added, tuple(failures))

    def _publish_result(
        self,
        scope: ConversationScope,
        candidate: CaptureCandidate,
        generated: HistorianResult,
        *,
        generation_mode: str,
    ):
        evidence_ids = generated.evidence_ids or tuple(
            item.canonical_message_id for item in candidate.messages
        )
        participants = generated.participants or tuple(
            dict.fromkeys(
                item.sender_display
                for item in candidate.messages
                if item.sender_display.strip()
            )
        )
        summary_p4 = generated.summary_p4.strip() or generated.summary_p3.strip()
        topic = generated.topic.strip() or generated.summary_p3.strip()[:120]
        compartment = self.context_store.publish_generated(
            candidate,
            (
                generated.summary_p1,
                generated.summary_p2,
                generated.summary_p3,
            ),
            summary_p4=summary_p4,
            topic=topic,
            importance=generated.importance,
            confidence=generated.confidence,
            participants=participants,
            evidence_ids=evidence_ids,
            generation_mode=generation_mode,
        )
        memories_added = 0
        if scope.kind != "group" or generation_mode == "fallback":
            return compartment, memories_added
        memory_scope = _group_memory_scope(scope)
        allowed_ids = {
            item.canonical_message_id for item in candidate.messages
        }
        for proposal in generated.memories[:10]:
            if proposal.source_message_id not in allowed_ids:
                continue
            if _looks_like_secret(proposal.content):
                continue
            try:
                _entry, created = self.memory_store.add(
                    memory_scope,
                    "group",
                    proposal.content,
                    creator_user_id=0 if scope.platform == "onebot-v11" else None,
                    creator_principal_id=0,
                    source_message_id=proposal.source_message_id,
                    reason="historian proposal",
                )
            except LongTermMemoryError:
                continue
            memories_added += int(created)
        return compartment, memories_added

    def _candidate_from_job(
        self,
        job: DurableJob,
    ) -> tuple[CaptureCandidate, ConversationScope]:
        raw_scope = job.payload.get("scope")
        if not isinstance(raw_scope, dict):
            raise ValueError("historian job has no conversation scope")
        encoded_scope = job.payload.get("scope_key")
        if encoded_scope is not None:
            scope = ConversationScope.from_key(str(encoded_scope))
            if (
                raw_scope.get("platform") != scope.platform
                or raw_scope.get("kind") != scope.kind
                or raw_scope.get("native_conversation_id") != scope.native_conversation_id
            ):
                raise ValueError("historian job scope metadata does not match its address")
        else:
            # Already queued legacy jobs predate account-aware history keys.
            scope = ConversationScope(
                str(raw_scope.get("platform") or ""),
                str(raw_scope.get("kind") or ""),  # type: ignore[arg-type]
                str(raw_scope.get("native_conversation_id") or ""),
            )
        if scope.key != job.scope_key:
            raise ValueError("historian job scope does not match its lease")
        raw_ids = job.payload.get("source_message_ids")
        if not isinstance(raw_ids, list):
            raise ValueError("historian job has no evidence range")
        source_ids = tuple(int(item) for item in raw_ids)
        candidate = self.context_store.restore_capture_candidate(
            self.ledger,
            scope,
            expected_cursor=int(job.payload.get("expected_cursor") or 0),
            source_message_ids=source_ids,
            source_hash=str(job.payload.get("source_hash") or ""),
        )
        return candidate, scope

    def _protected(self, scope: ConversationScope) -> tuple[int, ...]:
        return tuple(
            int(item)
            for item in (
                self.protected_provider(scope)
                if self.protected_provider is not None
                else ()
            )
        )

    def _fallback(self, candidate: CaptureCandidate) -> HistorianResult:
        p1, p2, p3 = self.context_store._summaries(list(candidate.messages))
        participants = tuple(
            dict.fromkeys(
                item.sender_display
                for item in candidate.messages
                if item.sender_display.strip()
            )
        )
        evidence = tuple(
            item.canonical_message_id for item in candidate.messages
        )
        return HistorianResult(
            summary_p1=p1,
            summary_p2=p2,
            summary_p3=p3,
            summary_p4=p3[:300],
            topic=p3[:120],
            importance=0.35,
            confidence=0.35,
            participants=participants,
            evidence_ids=evidence,
        )

    @staticmethod
    def _validate(
        candidate: CaptureCandidate,
        generated: HistorianResult,
    ) -> None:
        summaries = (
            generated.summary_p1.strip(),
            generated.summary_p2.strip(),
            generated.summary_p3.strip(),
        )
        if any(not summary for summary in summaries):
            raise ValueError("historian omitted a required summary tier")
        if len(summaries[0]) > 3200 or len(summaries[1]) > 1600:
            raise ValueError("historian summary exceeds the publication budget")
        allowed = {message.canonical_message_id for message in candidate.messages}
        if generated.evidence_ids and not set(generated.evidence_ids).issubset(allowed):
            raise ValueError("historian summary cites evidence outside its capture")
        if not 0 <= generated.importance <= 1:
            raise ValueError("historian importance is outside 0..1")
        if not 0 <= generated.confidence <= 1:
            raise ValueError("historian confidence is outside 0..1")
        for proposal in generated.memories:
            if proposal.source_message_id not in allowed:
                raise ValueError("historian memory cites evidence outside its capture")
            if not proposal.content.strip():
                raise ValueError("historian proposed an empty memory")


@dataclass(frozen=True)
class DreamOperation:
    action: str
    memory_id: int
    expected_version: int
    content: str = ""
    reason: str = ""


DreamGenerator = Callable[
    [str, Sequence[MemoryEntry], str],
    Awaitable[Sequence[DreamOperation]],
]


class DreamService:
    def __init__(
        self,
        memory_store: LongTermMemoryStore,
        generator: DreamGenerator,
        *,
        evidence_provider: Callable[[MemoryEntry], str] | None = None,
        min_entries: int = 15,
    ) -> None:
        self.memory_store = memory_store
        self.generator = generator
        self.evidence_provider = evidence_provider
        self.min_entries = max(int(min_entries), 2)

    async def run_once(self) -> dict[str, int]:
        changed = 0
        failed = 0
        for scope_key in self.memory_store.scope_keys():
            entries = self.memory_store.list_entries([scope_key])
            if len(entries) < self.min_entries:
                continue
            evidence = "\n".join(
                self.evidence_provider(entry)
                for entry in entries
                if self.evidence_provider is not None
            )
            try:
                operations = await self.generator(scope_key, entries, evidence)
                changed += self._apply(scope_key, entries, operations)
            except Exception:
                failed += 1
        return {"changed": changed, "failed_scopes": failed}

    def _apply(
        self,
        scope_key: str,
        entries: Sequence[MemoryEntry],
        operations: Sequence[DreamOperation],
    ) -> int:
        visible = {entry.id: entry for entry in entries}
        changed = 0
        for operation in list(operations)[:50]:
            current = visible.get(int(operation.memory_id))
            if current is None or current.version != int(operation.expected_version):
                continue
            reason = "dream consolidation: " + (
                " ".join(operation.reason.split())[:140] or "maintenance"
            )
            if operation.action == "update":
                updated = self.memory_store.update(
                    current.id,
                    operation.content,
                    [scope_key],
                    expected_version=current.version,
                    actor_user_id=0,
                    actor_principal_id=0,
                    source_message_id=current.source_message_id,
                    reason=reason,
                )
                visible[current.id] = updated
                changed += 1
            elif operation.action in {"remove", "archive"}:
                if self.memory_store.remove(
                    current.id,
                    [scope_key],
                    actor_user_id=0,
                    actor_principal_id=0,
                    source_message_id=current.source_message_id,
                    reason=reason,
                ):
                    visible.pop(current.id, None)
                    changed += 1
        return changed


class MaintenanceState:
    def __init__(self, path: DatabaseSource) -> None:
        self.path, self._connection = open_store_connection(path)
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def completed(self, job_name: str, success_key: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT last_success_key FROM maintenance_state
                WHERE job_name = ?
                """,
                (job_name,),
            ).fetchone()
        return row is not None and str(row[0]) == success_key

    def mark_completed(self, job_name: str, success_key: str) -> None:
        with self._transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO maintenance_state (
                    job_name, last_success_key, updated_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(job_name) DO UPDATE SET
                    last_success_key = excluded.last_success_key,
                    updated_at = excluded.updated_at
                """,
                (job_name, success_key, int(time.time())),
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


def parse_historian_payload(payload: dict[str, object]) -> HistorianResult:
    raw_memories = payload.get("memories")
    memories: list[MemoryProposal] = []
    if isinstance(raw_memories, list):
        for raw in raw_memories[:10]:
            if not isinstance(raw, dict):
                continue
            try:
                source_message_id = int(raw.get("source_message_id") or 0)
            except (TypeError, ValueError):
                continue
            content = " ".join(str(raw.get("content") or "").split())
            if source_message_id > 0 and content:
                memories.append(MemoryProposal(content[:300], source_message_id))
    raw_participants = payload.get("participants")
    participants = (
        tuple(
            " ".join(str(item).split())[:120]
            for item in raw_participants[:32]
            if str(item).strip()
        )
        if isinstance(raw_participants, list)
        else ()
    )
    raw_evidence = payload.get("evidence_ids")
    evidence_ids: list[int] = []
    if isinstance(raw_evidence, list):
        for item in raw_evidence:
            try:
                value = int(item)
            except (TypeError, ValueError):
                continue
            if value > 0:
                evidence_ids.append(value)
    return HistorianResult(
        summary_p1=str(payload.get("summary_p1") or "").strip(),
        summary_p2=str(payload.get("summary_p2") or "").strip(),
        summary_p3=str(payload.get("summary_p3") or "").strip(),
        summary_p4=str(payload.get("summary_p4") or "").strip(),
        topic=" ".join(str(payload.get("topic") or "").split())[:300],
        importance=_bounded_number(payload.get("importance"), 0.5),
        confidence=_bounded_number(payload.get("confidence"), 0.5),
        participants=tuple(dict.fromkeys(participants)),
        evidence_ids=tuple(dict.fromkeys(evidence_ids)),
        memories=tuple(memories),
    )


def parse_dream_payload(payload: dict[str, object]) -> list[DreamOperation]:
    raw_operations = payload.get("operations")
    if not isinstance(raw_operations, list):
        return []
    operations: list[DreamOperation] = []
    for raw in raw_operations[:50]:
        if not isinstance(raw, dict):
            continue
        action = str(raw.get("action") or "").strip().lower()
        if action not in {"update", "remove", "archive"}:
            continue
        try:
            memory_id = int(raw.get("memory_id") or 0)
            expected_version = int(raw.get("expected_version") or 0)
        except (TypeError, ValueError):
            continue
        if memory_id <= 0 or expected_version <= 0:
            continue
        content = " ".join(str(raw.get("content") or "").split())[:300]
        if action == "update" and not content:
            continue
        operations.append(
            DreamOperation(
                action=action,
                memory_id=memory_id,
                expected_version=expected_version,
                content=content,
                reason=" ".join(str(raw.get("reason") or "").split())[:200],
            )
        )
    return operations


def render_capture(candidate: CaptureCandidate) -> str:
    return "\n".join(
        f"msg#{message.canonical_message_id} {message.sender_display}: "
        f"{message.prompt_text}"
        for message in candidate.messages
        if message.prompt_text
    )


def _group_memory_scope(scope: ConversationScope) -> str:
    return group_memory_scope(scope)


def _looks_like_secret(content: str) -> bool:
    return bool(
        re.search(r"\bsk-[A-Za-z0-9_-]{10,}\b", content)
        or re.search(
            r"(?i)(?:api[_ -]?key|access[_ -]?token|password|secret|密码|验证码)"
            r"\s*[:=：]\s*\S+",
            content,
        )
    )


def _bounded_number(value: object, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return min(max(number, 0.0), 1.0)
