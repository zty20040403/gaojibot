"""Physical OneBot boundary requiring a durable route service.

This module is not a route authority implementation. The port must atomically
check the frozen expected epoch and register/link a durable physical dispatch
before returning. No DB transaction spans the adapter's network await.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

from .onebot_route_context import SourceProof, active_source_route
from ..agent.control import RouteAuthorityLost


@dataclass(frozen=True)
class PhysicalCall:
    api: str
    bot_id: str = field(repr=False)
    # These are only routing hints from the actual adapter parameters. The root
    # service parses API-specific semantics and must reject ambiguous targets.
    message_type: object = field(default=None, repr=False)
    group_id: object = field(default=None, repr=False)
    user_id: object = field(default=None, repr=False)
    message_id: object = field(default=None, repr=False)


@dataclass(frozen=True, repr=False)
class TransportObservation:
    returned: bool
    message_id: str | None = None
    file_id: str | None = None
    error_kind: str | None = None


class DispatchBoundary(Protocol):
    async def begin(self, call: PhysicalCall, source: SourceProof | None) -> object | None: ...
    async def observe(self, permit: object, observation: TransportObservation) -> None: ...


class RouteObservationPending(RouteAuthorityLost):
    """Transport may have run; leave durable intent unknown, never auto-replay."""


_boundary: DispatchBoundary | None = None


def install_boundary(port: DispatchBoundary) -> None:
    global _boundary
    if port is None:
        raise ValueError('route_boundary_required')
    _boundary=port


def uninstall_boundary(port: DispatchBoundary) -> None:
    global _boundary
    if _boundary is port:
        _boundary=None


def _receipt(value: Any, field_name: str) -> str | None:
    raw=value.get(field_name) if isinstance(value,dict) else None
    if type(raw) not in {str,int}:
        return None
    text=str(raw)
    if not 1<=len(text.encode('utf-8',errors='replace'))<=256 or any(ord(x)<32 for x in text):
        return None
    # A bounded native ID is evidence, never the message content/URL/credential.
    if any(x in text for x in ('://','<','>','\n')):
        return None
    return text


async def _observe(port,permit,observation):
    if permit is None:
        return
    pending=asyncio.create_task(port.observe(permit,observation))
    # Preserve the observation across normal/repeated cancellation. Root's port
    # has its own bounded DB deadline and records a late receipt independently
    # from current route authority, just as M3 does.
    cancelled=False
    while not pending.done():
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError:
            cancelled=True
            continue
        except BaseException:
            break
    try:
        pending.result()
    except BaseException:
        raise RouteObservationPending('onebot_route_observation_pending') from None
    if cancelled:
        raise asyncio.CancelledError


class _RouteGuard:
    async def _call_api(self,bot,api: str,**data: Any):
        port=_boundary
        if port is None:
            # Allowed only when official disabled AND no existing migration.
            # Runtime bootstrap owns that decision before enabling business.
            return await super()._call_api(bot,api,**data)
        call=PhysicalCall(api,str(bot.self_id),data.get('message_type'),data.get('group_id'),
            data.get('user_id'),data.get('message_id'))
        permit=await port.begin(call,active_source_route.get())
        try:
            result=await super()._call_api(bot,api,**data)
        except BaseException as error:
            kind=type(error).__name__
            if kind not in {'ActionFailed','NetworkError','TimeoutError','CancelledError'}:
                kind='Other'
            await _observe(port,permit,TransportObservation(False,error_kind=kind))
            raise
        await _observe(port,permit,TransportObservation(True,_receipt(result,'message_id'),_receipt(result,'file_id')))
        return result


def __getattr__(name):
    # Protocol metadata and the official migration assembly remain SDK-free.
    # The actual OneBot entry point explicitly imports this concrete subclass.
    if name != 'RouteGuardedOneBotAdapter':
        raise AttributeError(name)
    from nonebot.adapters.onebot.v11 import Adapter
    adapter = type(name, (_RouteGuard, Adapter), {'__module__': __name__})
    globals()[name] = adapter
    return adapter
