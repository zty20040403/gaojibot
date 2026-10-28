"""Compatibility primitive draft: a description of task ownership, not a grant."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping
import unicodedata

from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey


def _bounded(value: str, limit: int) -> None:
    if (not isinstance(value, str) or not value.strip()
            or len(value.encode("utf-8")) > limit
            or any(unicodedata.category(c) in {'Cc', 'Cf', 'Cs'} for c in value)):
        raise ValueError("invalid_task_identity")


@dataclass(frozen=True)
class LegacyTaskOwner:
    # No new positive-number restriction: preserve the established legacy API.
    requester_user_id: int = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.requester_user_id) is not int:
            raise ValueError("invalid_task_identity")

    def as_payload(self) -> dict[str, object]:
        return {"requester_user_id": self.requester_user_id}


@dataclass(frozen=True)
class StructuredTaskOwner:
    endpoint: EndpointKey = field(repr=False)
    requester_actor: ActorKey = field(repr=False)

    def __post_init__(self) -> None:
        if (not isinstance(self.endpoint, EndpointKey)
                or not isinstance(self.requester_actor, ActorKey)
                or self.endpoint.account.platform != "qq-official"
                or self.requester_actor.identity_kind == "bot"
                or not self.requester_actor.matches_endpoint(self.endpoint)):
            raise ValueError("invalid_task_identity")
        for value in (self.endpoint.account.app_id,
                      self.endpoint.native_conversation_id,
                      self.requester_actor.native_user_id):
            _bounded(value, 256)
        if self.requester_actor.identity_namespace:
            _bounded(self.requester_actor.identity_namespace, 256)
        for value, limit in ((self.endpoint.account.key, 512),
                             (self.endpoint.key, 1000),
                             (self.requester_actor.key, 1280)):
            _bounded(value, limit)

    def as_payload(self) -> dict[str, object]:
        return {"requester_user_id": None, "task_identity_version": 1,
                "endpoint_key": self.endpoint.key,
                "requester_actor_key": self.requester_actor.key}


TaskOwner = LegacyTaskOwner | StructuredTaskOwner
_STRUCTURED_FIELDS = {"task_identity_version", "endpoint_key", "requester_actor_key"}


def make_owner(requester_user_id, endpoint=None, requester_actor=None) -> TaskOwner:
    if endpoint is None and requester_actor is None:
        return LegacyTaskOwner(requester_user_id)
    if requester_user_id is not None or endpoint is None or requester_actor is None:
        raise ValueError("invalid_task_identity")
    return StructuredTaskOwner(endpoint, requester_actor)


def owner_fields(owner: TaskOwner) -> dict[str, object]:
    if isinstance(owner, LegacyTaskOwner):
        return {"requester_user_id": owner.requester_user_id}
    return {"requester_user_id": None, "endpoint": owner.endpoint,
            "requester_actor": owner.requester_actor}


def object_owner(value) -> TaskOwner:
    return make_owner(value.requester_user_id, value.endpoint, value.requester_actor)


def owner_from_payload(payload: Mapping[str, object]) -> TaskOwner:
    if not _STRUCTURED_FIELDS.intersection(payload):
        # Exactly the previous contract reader's legacy coercion. This branch
        # can never be reached after any structured marker has been observed.
        return LegacyTaskOwner(int(payload.get("requester_user_id") or 0))
    if (not _STRUCTURED_FIELDS.issubset(payload)
            or type(payload["task_identity_version"]) is not int
            or payload["task_identity_version"] != 1
            or payload.get("requester_user_id") is not None
            or "requester_user_id" not in payload):
        raise ValueError("invalid_task_identity")
    endpoint_key, actor_key = payload["endpoint_key"], payload["requester_actor_key"]
    _bounded(endpoint_key, 1000)
    _bounded(actor_key, 1280)
    try:
        endpoint, actor = EndpointKey.parse(endpoint_key), ActorKey.parse(actor_key)
    except (TypeError, ValueError):
        raise ValueError("invalid_task_identity") from None
    if endpoint.key != endpoint_key or actor.key != actor_key:
        raise ValueError("invalid_task_identity")
    return StructuredTaskOwner(endpoint, actor)


def require_same_owner(actual: TaskOwner, expected: TaskOwner) -> None:
    """Use only after actual authority/fencing; history scope is not an input."""
    if type(actual) is not type(expected) or actual != expected:
        raise PermissionError("task_owner_mismatch")
