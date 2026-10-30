"""Source-bound artifact registration/projection ports; no transport authority."""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from pathlib import PurePath
import unicodedata
from typing import Any

from ..platform_types import ActorKey, EndpointKey


class ArtifactDeliveryError(ValueError):
    pass


def official_task(task: Any) -> bool:
    endpoint = getattr(task, "endpoint", None)
    return endpoint is not None and endpoint.account.platform == "qq-official"


def _name(value: str) -> None:
    if (not isinstance(value, str) or not value
            or any(c in value for c in ("/", "\\"))
            or any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in value) or len(value.encode("utf-8")) > 240):
        raise ArtifactDeliveryError("invalid_artifact_name")


def canonical_artifact_filename(filename: str, *, draft: bool) -> str:
    """Host display-name normalization, never a filesystem path or authority.

    The Coordinator request and persistent manifest must use this same result;
    projections continue to compare the real stored filename exactly.
    """
    if (not isinstance(filename, str) or not filename or type(draft) is not bool
            or filename != filename.strip() or len(filename.encode("utf-8")) > 4096
            or any(c in "/\\" or unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in filename)):
        raise ArtifactDeliveryError("invalid_artifact_name")
    prefix = "草稿-" if draft else ""
    if draft and filename.startswith(prefix):
        filename = filename[len(prefix):]
    suffix = PurePath(filename).suffix
    budget = 180 - len((prefix + suffix).encode("utf-8"))
    stem = (filename[:-len(suffix)] if suffix else filename).encode("utf-8")[:budget].decode("utf-8", "ignore")
    if not stem or stem in {".", ".."}:
        raise ArtifactDeliveryError("invalid_artifact_name")
    return prefix + stem + suffix


@dataclass(frozen=True)
class ArtifactDeliveryRequest:
    task_id: int
    run_id: int
    revision: int
    endpoint: EndpointKey = field(repr=False)
    actor: ActorKey = field(repr=False)
    snapshot: str = field(repr=False)
    size: int
    filename: str = field(repr=False)
    draft: bool
    draft_reason: str = field(default="", repr=False)

    def __post_init__(self):
        if (any(type(v) is not int or v <= 0 for v in (self.task_id, self.run_id, self.revision, self.size))
                or not re.fullmatch(r"[a-f0-9]{64}", self.snapshot)
                or self.endpoint.account.platform != "qq-official"
                or not self.actor.matches_endpoint(self.endpoint)
                or self.actor.identity_kind == "bot" or type(self.draft) is not bool
                or not isinstance(self.draft_reason, str) or len(self.draft_reason.encode("utf-8")) > 1000
                or bool(self.draft_reason) != self.draft):
            raise ArtifactDeliveryError("invalid_artifact_binding")
        _name(self.filename)


_STATES = frozenset({"stored", "queued", "uploading", "uploaded", "upload_unknown", "publishing", "publication_unknown", "published", "handed_off",
                     "message_accepted", "delivery_unknown", "failed", "cancelled", "expired", "unsupported"})


@dataclass(frozen=True)
class ArtifactDeliveryView:
    """Host projection, not proof/capability usable to send. Never holds a URL."""
    task_id: int
    revision: int
    endpoint: EndpointKey = field(repr=False)
    actor: ActorKey = field(repr=False)
    artifact_id: str = field(repr=False)
    snapshot: str = field(repr=False)
    filename: str = field(repr=False)
    size: int
    draft: bool
    state: str
    updated_at: int
    draft_reason: str = field(default="", repr=False)

    def __post_init__(self):
        if (self.state not in _STATES or not re.fullmatch(r"[a-f0-9]{32}", self.artifact_id)
                or not re.fullmatch(r"[a-f0-9]{64}", self.snapshot)
                or any(type(v) is not int or v <= 0 for v in (self.task_id, self.revision, self.size))
                or type(self.updated_at) is not int or self.updated_at < 0 or type(self.draft) is not bool
                or not isinstance(self.draft_reason, str) or len(self.draft_reason.encode("utf-8")) > 1000
                or (not self.draft and self.draft_reason)
                or self.endpoint.account.platform != "qq-official"
                or not self.actor.matches_endpoint(self.endpoint) or self.actor.identity_kind == "bot"):
            raise ArtifactDeliveryError("invalid_artifact_projection")
        _name(self.filename)

    def for_task(self, task: Any, revision: int) -> dict[str, Any]:
        if (self.task_id, self.revision, self.endpoint, self.actor) != (
                task.task_id, revision, task.endpoint, task.requester_actor):
            raise ArtifactDeliveryError("artifact_projection_source_mismatch")
        accepted = self.state == "message_accepted"
        return {"key": self.artifact_id, "revision": self.revision,
                "state": "acknowledged" if accepted else self.state,
                "updated_at": self.updated_at,
                "payload": {"artifact_id": self.artifact_id, "filename": self.filename,
                    "size": self.size, "draft": self.draft, "draft_reason": self.draft_reason, "state": self.state,
                    "ok": accepted, "message_accepted": accepted, "official_artifact": True}}

    def for_request(self, task: Any, request: ArtifactDeliveryRequest) -> dict[str, Any]:
        row = self.for_task(task, request.revision)
        if (self.snapshot, self.filename, self.size, self.draft, self.draft_reason) != (
                request.snapshot, request.filename, request.size, request.draft, request.draft_reason):
            raise ArtifactDeliveryError("artifact_projection_content_mismatch")
        return row["payload"]


def project_rows(task: Any, revision: int, values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, (tuple, list)) or len(values) > 32:
        raise ArtifactDeliveryError("invalid_artifact_projection")
    rows = []
    seen = set()
    for value in values:
        if not isinstance(value, ArtifactDeliveryView) or value.artifact_id in seen:
            raise ArtifactDeliveryError("invalid_artifact_projection")
        seen.add(value.artifact_id)
        rows.append(value.for_task(task, revision))
    return rows


def delivery_state(payloads: list[dict], *, required: bool) -> str:
    if not payloads:
        return "pending" if required else "not_requested"
    if all(p.get("message_accepted") is True for p in payloads):
        return "message_accepted"
    if any(p.get("state") == "delivery_unknown" for p in payloads):
        return "delivery_unknown"
    if any(p.get("state") == "upload_unknown" for p in payloads):
        return "upload_unknown"
    if any(p.get("state") == "publication_unknown" for p in payloads):
        return "publication_unknown"
    if any(p.get("state") in {"failed", "cancelled", "expired", "unsupported"} for p in payloads):
        return "unavailable"
    return "pending"
