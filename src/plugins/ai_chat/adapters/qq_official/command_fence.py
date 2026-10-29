"""Host-only guards around the existing shared router's exact effect ports.

The supplied synchronous effect_scope must lock and validate the current account,
endpoint, inbox, durable job and executing record until the operation finishes.
Settings still use their existing independent storage transaction: this is a
fence, not a claim of atomic settings+journal commit. An uncertain executing
command is parked for review, never replayed. No scope accepts model callbacks.
"""
from __future__ import annotations

from dataclasses import replace
import inspect

from src.plugins.ai_chat.agent.control import LeaseLost
from src.plugins.ai_chat.conversation_keys import conversation_key


class CommandEffectDenied(LeaseLost):
    """Fixed diagnostic, escapes shared command Exception fallbacks."""


class _Tasks:
    def __init__(self, registry, inbound, effect_scope):
        self._registry, self._inbound, self._scope = registry, inbound, effect_scope

    def _check(self, endpoint, actor):
        if endpoint != self._inbound.endpoint or actor != self._inbound.actor:
            raise CommandEffectDenied('command_target_mismatch')

    def list_for_endpoint(self, endpoint, actor):
        self._check(endpoint, actor)
        with self._scope(self._inbound):
            return self._registry.list_for_endpoint(endpoint, actor)

    def cancel_for_endpoint(self, endpoint, actor, task_id=None):
        self._check(endpoint, actor)
        # No suspension between the authoritative guard and asyncio.Task.cancel.
        with self._scope(self._inbound):
            return self._registry.cancel_for_endpoint(endpoint, actor, task_id)


_READ = {'get_explicit', 'get_scope_default', 'get_scope_member_default'}
_WRITE = {'set', 'clear', 'set_scope_default', 'clear_scope_default',
          'set_scope_member_default', 'clear_scope_member_default'}


def guarded_services(services, inbound, *, effect_scope, database_call, settings_authority):
    """Wrap only this inbound's two supplied stores and exact actor's tasks.

    settings_authority(cursor_or_guard, inbound, domain, method, args) is a
    synchronous host callback inside held locks. It must recheck the real owner
    grant for group-default writes and approved catalog/policy for personal
    changes. Mere platform_role or stale pre-router policy is insufficient.
    """
    stores = ((services.model_preferences, 'model'), (services.reasoning_preferences, 'effort'))

    async def guarded_call(function, *args):
        domain = next((domain for store, domain in stores if store is not None
                       and getattr(function, '__self__', None) is store), None)
        method = getattr(function, '__name__', '')
        store = getattr(function, '__self__', None)
        if domain is None or method not in _READ | _WRITE or function != getattr(store, method, None):
            raise CommandEffectDenied('command_store_method_unavailable')
        scope_method = '_scope_' in method
        target = inbound.endpoint.scope if scope_method else conversation_key(inbound)
        count = 2 if method.startswith('set') else 1
        if len(args) != count or args[0] != target or (scope_method and inbound.endpoint.kind != 'group'):
            raise CommandEffectDenied('command_store_target_mismatch')
        if count == 2 and (not isinstance(args[1], str) or not 1 <= len(args[1]) <= 256):
            raise CommandEffectDenied('command_store_value_invalid')

        def run():
            with effect_scope(inbound) as guard:
                allowed = settings_authority(guard, inbound, domain, method, args)
                if inspect.isawaitable(allowed):
                    if inspect.iscoroutine(allowed):
                        allowed.close()
                    raise CommandEffectDenied('command_policy_must_be_synchronous')
                if allowed is not True:
                    raise CommandEffectDenied('command_policy_changed')
                return function(*args)
        result = database_call(run)
        return await result if inspect.isawaitable(result) else result

    return replace(services, db_call=guarded_call, running_tasks=(
        _Tasks(services.running_tasks, inbound, effect_scope) if services.running_tasks is not None else None))


def build_services(services, inbound, *, lifecycle, database_call, settings_authority):
    """OfficialTurnConsumer command_services(life, inbound) assembly helper."""
    return guarded_services(services, inbound, effect_scope=lifecycle.effect_scope,
        database_call=database_call, settings_authority=settings_authority)
