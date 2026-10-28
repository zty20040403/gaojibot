from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence
from .sessions import upstream_index
from ..platform_types import EndpointKey, ActorKey
from .task_identity import make_owner, object_owner, owner_fields, owner_from_payload


SubAgentRole = Literal[
    "supervisor",
    "researcher",
    "coder",
    "document",
    "media",
    "analyst",
    "operator",
]
WorkerRole = Literal[
    "researcher",
    "coder",
    "document",
    "media",
    "analyst",
    "operator",
]
ExecutionMode = Literal["direct", "delegate", "workflow"]
MemoryScope = Literal["invocation", "task"]
RiskLevel = Literal["read-only", "controlled-write", "privileged"]


@dataclass(frozen=True)
class AgentSpec:
    role: SubAgentRole
    title: str
    description: str
    instructions: str
    allowed_tools: frozenset[str]
    version: int = 1
    model_policy: str = "inherit"
    max_turns: int = 12
    max_attempts: int = 2
    timeout_seconds: int = 600
    memory_scope: MemoryScope = "invocation"
    risk_level: RiskLevel = "read-only"
    background_default: bool = False
    context_channels: frozenset[str] = frozenset(
        {"conversation", "supporting", "evidence", "artifacts", "upstream"}
    )
    context_budget_chars: int = 7000

    def manifest(self) -> dict[str, object]:
        return {
            "role": self.role,
            "title": self.title,
            "description": self.description,
            "version": self.version,
            "model_policy": self.model_policy,
            "max_turns": self.max_turns,
            "max_attempts": self.max_attempts,
            "timeout_seconds": self.timeout_seconds,
            "memory_scope": self.memory_scope,
            "risk_level": self.risk_level,
            "background_default": self.background_default,
            "context_channels": sorted(self.context_channels),
            "context_budget_chars": self.context_budget_chars,
            "allowed_tools": sorted(self.allowed_tools),
        }


@dataclass(frozen=True)
class ExecutionDecision:
    mode: ExecutionMode
    domains: tuple[str, ...] = ()
    suggested_roles: tuple[WorkerRole, ...] = ()
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextPacket:
    """Host-curated task context from which isolated Agent views are projected."""

    scope_key: str
    conversation_id: str
    requester_user_id: int | None
    trigger_message_id: int | None
    objective: str
    conversation_context: str = ""
    memory_context: str = ""
    supporting_context: str = ""
    evidence_handles: tuple[str, ...] = ()
    artifact_handles: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    endpoint: EndpointKey | None = field(default=None, repr=False)
    requester_actor: ActorKey | None = field(default=None, repr=False)

    def __post_init__(self):
        make_owner(self.requester_user_id, self.endpoint, self.requester_actor)

    @classmethod
    def from_legacy(
        cls,
        *,
        scope_key: str,
        conversation_id: str,
        requester_user_id: int,
        trigger_message_id: int | None,
        objective: str,
        context: str,
    ) -> "ContextPacket":
        return cls(
            scope_key=scope_key,
            conversation_id=conversation_id,
            requester_user_id=requester_user_id,
            trigger_message_id=trigger_message_id,
            objective=objective,
            supporting_context=context,
        )

    def render_for_planner(self, *, max_chars: int = 7000) -> str:
        sections = self._base_sections()
        return _render_sections(sections, max_chars=max_chars)

    def as_payload(self) -> dict[str, object]:
        return {
            "scope_key": self.scope_key,
            "conversation_id": self.conversation_id,
            **object_owner(self).as_payload(),
            "trigger_message_id": self.trigger_message_id,
            "objective": self.objective,
            "conversation_context": self.conversation_context,
            "memory_context": self.memory_context,
            "supporting_context": self.supporting_context,
            "evidence_handles": list(self.evidence_handles),
            "artifact_handles": list(self.artifact_handles),
            "constraints": list(self.constraints),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ContextPacket":
        return cls(
            scope_key=str(payload.get("scope_key") or ""),
            conversation_id=str(payload.get("conversation_id") or ""),
            **owner_fields(owner_from_payload(payload)),
            trigger_message_id=(
                int(payload["trigger_message_id"])
                if payload.get("trigger_message_id") is not None
                else None
            ),
            objective=str(payload.get("objective") or ""),
            conversation_context=str(payload.get("conversation_context") or ""),
            memory_context=str(payload.get("memory_context") or ""),
            supporting_context=str(payload.get("supporting_context") or ""),
            evidence_handles=_string_tuple(payload.get("evidence_handles")),
            artifact_handles=_string_tuple(payload.get("artifact_handles")),
            constraints=_string_tuple(payload.get("constraints")),
        )

    def render_for_worker(
        self,
        role: SubAgentRole,
        *,
        upstream: Mapping[str, Mapping[str, Any]],
        max_chars: int = 7000,
    ) -> str:
        fallback = AgentSpec(
            role=role,
            title=role,
            description="",
            instructions="",
            allowed_tools=frozenset(),
            context_budget_chars=max_chars,
        )
        return self.for_agent(fallback, upstream=upstream).rendered_context

    def for_agent(
        self,
        spec: AgentSpec,
        *,
        upstream: Mapping[str, Mapping[str, Any]],
    ) -> "AgentContext":
        channels = spec.context_channels
        sections: list[tuple[str, str]] = [
            ("作用域", self.scope_key),
            ("分配角色", spec.role),
            ("目标", self.objective),
            ("约束", "\n".join(self.constraints) or "无"),
        ]
        if "evidence" in channels:
            sections.append(("证据句柄", ", ".join(self.evidence_handles) or "无"))
        if "artifacts" in channels:
            sections.append(("产物句柄", ", ".join(self.artifact_handles) or "无"))
        if "conversation" in channels:
            sections.append(("当前会话", self.conversation_context[:3200] or "无"))
        if "memory" in channels:
            sections.append(("相关记忆", self.memory_context[:1400] or "无"))
        if "supporting" in channels:
            sections.append(("补充上下文", self.supporting_context[:2800] or "无"))
        handoff = ("\n\n[上游结构化结果索引]\n" + upstream_index(upstream)) if upstream and "upstream" in channels else ""
        rendered = _render_sections(
            sections,
            max_chars=max(int(spec.context_budget_chars) - len(handoff), 1000),
        ) + handoff
        hash_input = rendered if self.endpoint is None else json.dumps(
            [rendered, object_owner(self).as_payload()], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()
        return AgentContext(
            scope_key=self.scope_key,
            conversation_id=self.conversation_id,
            **owner_fields(object_owner(self)),
            trigger_message_id=self.trigger_message_id,
            role=spec.role,
            agent_definition_version=spec.version,
            objective=self.objective,
            rendered_context=rendered,
            context_hash=digest,
            evidence_handles=self.evidence_handles,
            artifact_handles=self.artifact_handles,
        )

    def _base_sections(self) -> list[tuple[str, str]]:
        return [
            ("作用域", self.scope_key),
            ("目标", self.objective),
            ("约束", "\n".join(self.constraints) or "无"),
            ("证据句柄", ", ".join(self.evidence_handles) or "无"),
            ("产物句柄", ", ".join(self.artifact_handles) or "无"),
            ("当前会话", self.conversation_context or "无"),
            ("相关记忆", self.memory_context or "无"),
            ("补充上下文", self.supporting_context or "无"),
        ]


@dataclass(frozen=True)
class AgentContext:
    """Immutable, role-specific context visible to exactly one Agent run."""

    scope_key: str
    conversation_id: str
    requester_user_id: int | None
    trigger_message_id: int | None
    role: SubAgentRole
    agent_definition_version: int
    objective: str
    rendered_context: str
    context_hash: str
    evidence_handles: tuple[str, ...] = ()
    artifact_handles: tuple[str, ...] = ()
    endpoint: EndpointKey | None = field(default=None, repr=False)
    requester_actor: ActorKey | None = field(default=None, repr=False)

    def __post_init__(self):
        make_owner(self.requester_user_id, self.endpoint, self.requester_actor)

    def as_payload(self) -> dict[str, object]:
        return {
            "scope_key": self.scope_key,
            "conversation_id": self.conversation_id,
            **object_owner(self).as_payload(),
            "trigger_message_id": self.trigger_message_id,
            "role": self.role,
            "agent_definition_version": self.agent_definition_version,
            "objective": self.objective,
            "rendered_context": self.rendered_context,
            "context_hash": self.context_hash,
            "evidence_handles": list(self.evidence_handles),
            "artifact_handles": list(self.artifact_handles),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "AgentContext":
        role = str(payload.get("role") or "researcher")
        return cls(
            scope_key=str(payload.get("scope_key") or ""),
            conversation_id=str(payload.get("conversation_id") or ""),
            **owner_fields(owner_from_payload(payload)),
            trigger_message_id=(
                int(payload["trigger_message_id"])
                if payload.get("trigger_message_id") is not None
                else None
            ),
            role=role,  # type: ignore[arg-type]
            agent_definition_version=int(payload.get("agent_definition_version") or 1),
            objective=str(payload.get("objective") or ""),
            rendered_context=str(payload.get("rendered_context") or ""),
            context_hash=str(payload.get("context_hash") or ""),
            evidence_handles=_string_tuple(payload.get("evidence_handles")),
            artifact_handles=_string_tuple(payload.get("artifact_handles")),
        )


@dataclass(frozen=True)
class AgentResult:
    status: Literal["success", "partial", "failed"]
    summary: str
    facts: tuple[object, ...] = ()
    artifacts: tuple[dict[str, Any], ...] = ()
    citations: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    confidence: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)
    handoff: tuple[str, ...] = ()
    findings: tuple[dict[str, Any], ...] = ()
    completed: tuple[dict[str, Any], ...] = ()
    authorization: tuple[dict[str, Any], ...] = ()
    next_verification: tuple[str, ...] = ()
    report_missing_fields: tuple[str, ...] = ()

    @classmethod
    def parse(cls, answer: str) -> "AgentResult":
        content = answer.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1]
            if content.rstrip().endswith("```"):
                content = content.rstrip()[:-3].rstrip()
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return cls(
                status="partial",
                summary=answer.strip(),
                warnings=("Agent 返回了非结构化结果，主控已保留原文。",),
            )
        if not isinstance(payload, Mapping):
            raise RuntimeError("Sub-Agent result must be a JSON object")
        return cls.from_payload(payload)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "AgentResult":
        raw_status = str(payload.get("status") or "").strip().casefold()
        unresolved = _string_tuple(payload.get("unresolved"))
        if raw_status in {"failed", "failure", "error", "cancelled", "skipped"}:
            status: Literal["success", "partial", "failed"] = "failed"
        elif raw_status in {"partial", "degraded", "incomplete"}:
            status = "partial"
        elif raw_status in {"success", "succeeded", "completed", "ok"}:
            status = "success"
        else:
            status = "partial"
        summary = str(payload.get("summary") or "").strip()
        if status == "success" and (not summary or unresolved):
            status = "partial"
        try:
            confidence = float(payload.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        artifacts = payload.get("artifacts")
        normalized_artifacts = (
            tuple(dict(item) for item in artifacts if isinstance(item, Mapping))
            if isinstance(artifacts, list)
            else ()
        )
        known = {
            "status",
            "summary",
            "facts",
            "artifacts",
            "citations",
            "warnings",
            "unresolved",
            "confidence",
            "metadata",
            "handoff",
            "findings", "completed", "authorization", "next_verification",
        }
        raw_metadata = payload.get("metadata")
        metadata = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
        metadata.update(
            {key: value for key, value in payload.items() if key not in known}
        )
        return cls(
            status=status,
            summary=summary,
            facts=_fact_tuple(payload.get("facts")),
            artifacts=normalized_artifacts,
            citations=_string_tuple(payload.get("citations")),
            warnings=_string_tuple(payload.get("warnings")),
            unresolved=unresolved,
            confidence=min(max(confidence, 0.0), 1.0),
            metadata=metadata,
            handoff=_string_tuple(payload.get("handoff")),
            findings=tuple(item for item in payload.get("findings", []) if isinstance(item, dict)) if isinstance(payload.get("findings"), list) else (),
            completed=tuple(item for item in payload.get("completed", []) if isinstance(item, dict)) if isinstance(payload.get("completed"), list) else (),
            authorization=tuple(item for item in payload.get("authorization", []) if isinstance(item, dict)) if isinstance(payload.get("authorization"), list) else (),
            next_verification=_string_tuple(payload.get("next_verification")),
            report_missing_fields=tuple(key for key in
                ("findings", "completed", "authorization", "next_verification")
                if not isinstance(payload.get(key), list)),
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "facts": list(self.facts),
            "artifacts": [dict(item) for item in self.artifacts],
            "citations": list(self.citations),
            "warnings": list(self.warnings),
            "unresolved": list(self.unresolved),
            "confidence": self.confidence,
            "metadata": dict(self.metadata),
            "handoff": list(self.handoff),
            "findings": list(self.findings), "completed": list(self.completed),
            "authorization": list(self.authorization), "next_verification": list(self.next_verification),
        }


def _render_sections(sections: Sequence[tuple[str, str]], *, max_chars: int) -> str:
    remaining = max(int(max_chars), 0)
    rendered: list[str] = []
    for title, raw_value in sections:
        value = str(raw_value).strip() or "无"
        prefix = f"[{title}]\n"
        separator = "\n\n" if rendered else ""
        available = remaining - len(separator) - len(prefix)
        if available <= 0:
            break
        piece = value[:available]
        rendered.append(prefix + piece)
        remaining -= len(separator) + len(prefix) + len(piece)
    return "\n\n".join(rendered)


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _fact_tuple(value: object) -> tuple[object, ...]:
    if not isinstance(value, list):
        return ()
    result: list[object] = []
    for item in value:
        if isinstance(item, Mapping):
            result.append(dict(item))
        elif str(item).strip():
            result.append(str(item).strip())
    return tuple(result)


def _json_dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
