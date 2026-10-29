"""Captured OneBot route authority, inherited by actual child asyncio tasks."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import inspect
import weakref

from ..agent.control import RouteFence, active_route_fence
from ..platform_types import ActorKey, EndpointKey
from ..conversation_scope import ConversationScope


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False)
class SourceProof:
    source_id: str
    endpoint: EndpointKey
    scope: ConversationScope
    actor: ActorKey | None
    route_epoch: int
    runtime_id: str
    deadline_at: int
    worker_owner: str | None = None
    worker_generation: int | None = None
    result_digest: str | None = None


_issued: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
active_source_route: ContextVar[SourceProof | None] = ContextVar("onebot_source_route", default=None)
_source_runtime = None


def install_source_runtime(runtime):
    global _source_runtime
    _source_runtime = runtime


def uninstall_source_runtime(runtime):
    global _source_runtime
    if _source_runtime is runtime:
        _source_runtime = None


def legacy_project_gate(scope):
    """No configured managed runtime means exact legacy behavior, no I/O."""
    if _source_runtime is not None and scope.platform == "onebot-v11":
        proof = active_source_route.get()
        require_source(proof, _source_runtime.sources)
        if proof.scope.key != scope.key or (scope.bot_native_user_id and scope.bot_native_user_id != proof.endpoint.account.app_id):
            raise PermissionError("project_source_route_mismatch")
        _source_runtime.sources.current(proof)


def guard_source_transaction(cursor, *, scope=None):
    """Route/account/source before domain locks; no nested store transaction."""
    if scope is not None:
        if _source_runtime is None or scope.platform != "onebot-v11":
            return
        proof = active_source_route.get()
        require_source(proof, _source_runtime.sources)
        if proof.scope.key != scope.key:
            raise PermissionError("project_source_route_mismatch")
        _source_runtime.sources.require(cursor, proof)
        return
    fence = active_route_fence.get()
    if fence is not None and fence.transaction_guard is not None:
        fence.transaction_guard(cursor)


def require_model_source():
    """Unbound background loops cannot start models in a managed deployment."""
    from ..agent.control import active_job_fence, RouteAuthorityLost
    proof = active_source_route.get()
    if proof is not None and proof.result_digest is not None:
        raise RouteAuthorityLost("result_source_cannot_execute")
    if _source_runtime is not None:
        if active_route_fence.get() is None and active_job_fence.get() is None:
            raise RouteAuthorityLost("model_source_unavailable")


def capture_created_job(cursor, job, *, scope):
    if _source_runtime is None or scope.platform != "onebot-v11":
        return None
    return _source_runtime.capture_created_job(cursor, job, scope=scope)


def begin_project_effect(cursor, *, scope, domain, reference):
    if _source_runtime is None or scope.platform != "onebot-v11":
        return None
    return _source_runtime.sources.begin_effect(cursor, active_source_route.get(), scope=scope,
                                                domain=domain, reference=reference)


def start_project_effect(scope, *, domain, reference):
    if _source_runtime is None or scope.platform != "onebot-v11":
        return None
    with _source_runtime.sources.transaction() as cursor:
        return begin_project_effect(cursor, scope=scope, domain=domain, reference=reference)


def observe_project_effect(cursor, *, domain, reference):
    if _source_runtime is None:
        return
    row = cursor.execute("SELECT activity_id FROM migration_dispatch_activities WHERE operation='execute' AND operation_ref=?",
                         (domain + ":" + reference,)).fetchone()
    if row is not None:
        _source_runtime.routes.observe_effect(cursor, activity_id=row["activity_id"], source=(domain, reference),
                                             now=_source_runtime.sources._now())


def issue_source(owner, **values) -> SourceProof:
    """Internal host factory; construction alone grants no authority."""
    proof = SourceProof(**values)
    _issued[proof] = owner
    return proof


def require_source(proof: SourceProof, owner) -> None:
    if not isinstance(proof, SourceProof) or _issued.get(proof) is not owner:
        raise PermissionError("onebot_source_proof_required")


@contextmanager
def source_context(proof: SourceProof, check):
    source_token = active_source_route.set(proof)
    owner = _issued.get(proof)
    guard = (lambda cursor: owner.require(cursor, proof)) if owner is not None else None
    fence_token = active_route_fence.set(RouteFence(check, guard))
    try:
        yield proof
    finally:
        active_route_fence.reset(fence_token)
        active_source_route.reset(source_token)


def guarded_handler(handler, runtime):
    """Install the fence inside the handler task, never in a preprocessor task."""
    # NoneBot resolves annotations against the wrapper's globals. Freeze the
    # original handler's evaluated types so deferred Bot/Event annotations keep
    # working after wrapping across modules.
    signature = inspect.signature(handler, eval_str=True)

    @wraps(handler)
    async def wrapped(*args, **kwargs):
        boundary = getattr(runtime, "migration_routes", None)
        if boundary is None:
            return await handler(*args, **kwargs)
        bound = signature.bind(*args, **kwargs)
        bot = bound.arguments.get("bot")
        if bot is None:
            from nonebot.internal.matcher import current_bot
            bot = current_bot.get()
        async with boundary.handler_context(handler.__name__, bot, bound.arguments.get("event")):
            return await handler(*args, **kwargs)

    wrapped.__signature__ = signature
    return wrapped
