"""Host-only M5 contract values. None of these records authorize by construction."""
from dataclasses import dataclass, field
import hashlib
import json
from typing import Protocol
import weakref

from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from src.plugins.ai_chat.conversation_scope import ConversationScope


class MigrationDenied(ValueError):
    def __init__(self, reason="migration_unavailable"):
        super().__init__(reason)


def encoded(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(text.encode()) > 262144:
        raise MigrationDenied("migration_preview_too_large")
    return text


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


@dataclass(frozen=True, repr=False)
class ControlEvidence:
    account_id: str
    account_version: int
    session_hash: str


@dataclass(frozen=True, repr=False)
class BaselineEvidence:
    account_key: str
    runtime_id: str
    backend_pid: int
    advisory_key: int
    deployment_digest: str
    expires_at: int


@dataclass(frozen=True, repr=False)
class MappingEvidence:
    mapping_id: str
    revision: int
    source: ActorKey
    target: ActorKey
    target_endpoint: EndpointKey
    expires_at: int
    source_endpoint: EndpointKey
    source_scope: ConversationScope


@dataclass(frozen=True, repr=False)
class ExportEvidence:
    kind: str
    source_ref: str
    source_revision: int
    export_json: str
    classification: str
    source_actor: ActorKey | None
    export_digest: str
    execution_deadline: int | None = None
    result_deadline: int | None = None
    source_canonical_id: int | None = None
    source_native_id: str | None = None
    schedule_json: str | None = None


@dataclass(frozen=True, repr=False)
class VerificationEvidence:
    receipt_id: str
    delivery_id: int
    kind: str
    nonce: str


@dataclass(frozen=True, repr=False)
class ExecutionReceiptEvidence:
    """Returned by a trusted persisted-domain verifier, never a model output."""
    domain: str
    reference: str
    receipt_digest: str
    status: str


class Ports(Protocol):
    def control(self, cursor, authorization, *, action, preview_digest, now) -> ControlEvidence: ...
    def baseline(self, cursor, source, *, account, now) -> BaselineEvidence: ...
    def export(self, cursor, source_endpoint, source_scope, target_endpoint, kind, source_ref, now) -> ExportEvidence: ...
    def mapping(self, cursor, source_endpoint, source_scope, source_actor, target_endpoint, now) -> MappingEvidence: ...
    def effect(self, cursor, source, *, source_endpoint, source_scope, endpoint, operation, now) -> None: ...
    def effect_receipt(self, cursor, source, *, activity, now) -> ExecutionReceiptEvidence: ...
    def verification(self, cursor, source, *, migration_id, target_endpoint, nonce, now) -> VerificationEvidence: ...
    def verification_request(self, cursor, source, *, migration_id, endpoint, actor, kind, nonce, deadline, now) -> None: ...
    def verification_plan(self, cursor, proof, plan_id, *, now) -> None: ...
    def revoke(self, cursor, *, migration_id, target_endpoint, now) -> None: ...
    def bound_job(self, cursor, item, job, *, operation, now, due_at=None) -> None: ...
    def bound_plan(self, cursor, item, plan_id, *, now) -> None: ...
    def bound_artifact(self, cursor, item, artifact_id, *, now) -> None: ...
    def bound_schedule(self, cursor, item, target_routine_id, *, operation, now) -> None: ...
    def occurrence(self, cursor, parent, *, run_id, scheduled_at, job_id, now) -> dict: ...
    def existing_artifact(self, cursor, source, *, endpoint, artifact_ref, now) -> None: ...


class DenyPorts:
    def __getattr__(self, name):
        def deny(*args, **kwargs):
            raise MigrationDenied("migration_port_unconfigured")
        return deny


@dataclass(frozen=True)
class RouteSnapshot:
    route_id: str | None
    route_epoch: int
    phase: str
    active_endpoint: EndpointKey = field(repr=False)
    resource_scope: str = field(repr=False)
    endpoint_generation: int | None
    endpoint_route_epoch: int | None


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False)
class DispatchProof:
    activity_id: str
    source_endpoint_key: str
    target_endpoint_key: str
    runtime_id: str
    route_epoch: int


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False)
class VerificationProof:
    migration_id: str
    kind: str
    request_id: str
    endpoint: EndpointKey
    actor: ActorKey
    nonce: str
    route_epoch: int
    endpoint_generation: int
    endpoint_route_epoch: int
    deadline: int


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False)
class MigrationItemProof:
    item_id: str
    migration_id: str
    item_revision: int
    export_digest: str
    source_endpoint: EndpointKey
    source_scope: ConversationScope
    source_kind: str
    source_key: str
    source_revision: int
    target_endpoint: EndpointKey
    target_actor: ActorKey
    mapping_id: str
    mapping_revision: int
    route_epoch: int
    endpoint_generation: int
    endpoint_route_epoch: int
    source_canonical_id: int | None
    source_native_id: str | None
    execution_deadline: int | None
    result_deadline: int | None
    schedule_json: str | None
    job_id: int | None
    issuance_transaction_id: int
    target_routine_id: int | None

    @property
    def logical_route_epoch(self):
        return self.route_epoch

    @property
    def source_canonical_message_id(self):
        return self.source_canonical_id

    @property
    def source_native_message_id(self):
        return self.source_native_id


_issued = weakref.WeakValueDictionary()


def _issue(cls, **fields):
    value = cls(**fields)
    _issued[id(value)] = value
    return value


def require_issued(value, cls):
    if type(value) is not cls or _issued.get(id(value)) is not value:
        raise MigrationDenied("unissued_migration_proof")
