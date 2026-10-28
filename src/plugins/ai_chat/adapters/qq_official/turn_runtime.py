"""Compose the existing shared text service for one official account lease."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

from src.plugins.ai_chat.application.commands import CommandPolicy, CommandServices, parse_command
from src.plugins.ai_chat.application.platform_conversation import build_platform_chat_orchestrator
from src.plugins.ai_chat.application.platform_chat import PlatformChatService
from src.plugins.ai_chat.agent.control import LeaseLost
from src.plugins.ai_chat.conversation_keys import conversation_key
from src.plugins.ai_chat.model_catalog import SUPPORTED_REASONING_EFFORTS
from src.plugins.ai_chat.platform_types import EndpointKey

from . import execution
from .command_fence import guarded_services
from .commands import usage_callback
from .lease import assert_epoch
from .turn_consumer import OfficialTurnConsumer
from .turn_platform import TransportState


def build_turn_consumer(runtime, token, app):
    """Called by the authenticated runtime, never by a message or model tool."""
    def scope_enabled(scope):
        if scope.account != runtime.loaded.account:
            return False
        if app.model_preferences.get_scope_enabled_override(scope) is False:
            return False
        endpoint = EndpointKey(scope.account, scope.kind, scope.native_conversation_id)
        with runtime.deliveries.transaction() as cursor:
            assert_epoch(cursor, token)
            row = cursor.execute("SELECT binding_state,membership_state FROM qq_official_endpoints WHERE endpoint_key=?",
                                 (endpoint.key,)).fetchone()
            # A bound official group is locally enabled by default. This is
            # only a feature switch; execution still checks actual actor
            # admission, source identity and route/account/job fences.
            return bool(row and row["membership_state"] == "present"
                and (row["binding_state"] == "active" if endpoint.kind == "group"
                     else row["binding_state"] != "revoked"))

    async def memory_writer(inbound, platform, ports, user_text, answer):
        def commit():
            if (platform.execution.token is not token or ports.conversation_id != conversation_key(inbound)):
                raise LeaseLost("official_memory_scope_changed")
            with runtime.deliveries.transaction() as cursor:
                row = platform.execution.current(cursor, int(runtime.store.clock()))
                if row["state"] != "executing" or row["deadline_at"] <= int(runtime.store.clock()):
                    raise LeaseLost("official_memory_execution_unavailable")
                # Hold the account/job fences through the existing synchronous
                # memory write; do not resume a second model turn on failure.
                app.memory.append_turn(ports.conversation_id, user_text, answer)
        await runtime._calls(commit)

    service_options = {"memory_writer": memory_writer, "scope_enabled": scope_enabled}
    subagents = runtime.subagent_service
    if subagents is not None and app.settings.subagent_entry_enabled:
        from .subagent_actions import task_registry_for
        service_options.update(subagent_actions_factory=subagents.actions_for, registry_factory=task_registry_for)
    service = PlatformChatService(app, **service_options)
    orchestrator = build_platform_chat_orchestrator(app, service=service)

    def command_services(life, inbound):
        active_cursor = ContextVar("official_command_cursor", default=None)

        @contextmanager
        def effect_scope(current):
            with life.effect_scope(current) as cursor:
                marker = active_cursor.set(cursor)
                try:
                    yield cursor
                finally:
                    active_cursor.reset(marker)

        def policy(cursor, *, group_default=None):
            # These are already host-configured profiles. Personal preferences
            # stay within the actual endpoint/actor; only a current local owner
            # may change that endpoint's group defaults.
            profiles = tuple(profile.name for profile in app.model_catalog.profiles if profile.configured)
            owner = runtime.store.authority.owner_for_actor(cursor, inbound.endpoint, inbound.actor)
            if group_default is None:
                source = cursor.execute("""SELECT turn_text FROM qq_official_inbound_events
                    WHERE inbox_id=? AND account_key=?""", (life.handle.inbox_id, token.account_key)).fetchone()
                parsed = parse_command(source["turn_text"]) if source is not None else None
                words = parsed.argument.split(None, 1) if parsed is not None else ()
                group_default = bool(words and words[0].casefold() in {"group", "群默认"})
            preference = None if group_default else app.model_preferences.get_explicit(conversation_key(inbound))
            if preference is None and inbound.endpoint.kind == "group":
                preference = (app.model_preferences.get_scope_default(inbound.endpoint.scope)
                    or app.settings.model_profile_for_scope(inbound.endpoint.scope))
            profile = app.model_catalog.resolve_preference(preference)
            # The same provider restriction applies in PlatformChatService and
            # the unchanged OneBot command. Unsupported settings must not be
            # reported as applied; clearing a stale override remains allowed.
            efforts = tuple(SUPPORTED_REASONING_EFFORTS) if profile.configured and profile.provider in {"openai", "cliproxy"} else ()
            return CommandPolicy(models=profiles, efforts=efforts,
                default_model=profile.name,
                default_effort=profile.reasoning_effort or "",
                allow_model_read=True, allow_model_change=True,
                allow_effort_read=True, allow_effort_change=True,
                allow_group_default=owner is not None)

        async def host_policy(_inbound):
            def read():
                with life.effect_scope(inbound) as cursor:
                    return policy(cursor)
            return await runtime._calls(read)

        def settings_authority(cursor, _inbound, domain, method, args):
            # Recompute from the actual storage operation at the effect fence,
            # including group defaults independently of an owner's own model.
            current = policy(cursor, group_default="_scope_" in method)
            if domain not in {"model", "effort"}:
                return False
            if method.startswith("get"):
                return True
            if "_scope_" in method and not current.allow_group_default:
                return False
            if method.startswith("set"):
                return args[1] in (current.models if domain == "model" else current.efforts)
            return method.startswith("clear")

        tasks = app.running_tasks
        if subagents is not None:
            from .subagent_commands import TaskRegistryAdapter
            tasks = TaskRegistryAdapter(tasks, token, active_cursor.get, runtime.store.admitted, runtime.store.clock)
        services = CommandServices(running_tasks=tasks,
            model_preferences=app.model_preferences, reasoning_preferences=app.reasoning_preferences,
            host_policy=host_policy, usage=usage_callback(runtime.deliveries.transaction, runtime._calls))
        return guarded_services(services, inbound, effect_scope=effect_scope,
            database_call=runtime._calls, settings_authority=settings_authority)

    def transport_state():
        gateway = runtime._gateway
        return TransportState(runtime.loaded.account, runtime.loaded.credentials is not None,
            runtime._token is token and gateway is not None and gateway.state == "ready")

    return OfficialTurnConsumer(api=execution, token=token, store=runtime.store,
        outbox=runtime.outbox, journal=runtime.journal, database_calls=runtime._calls,
        orchestrator=orchestrator, command_services=command_services,
        transport_state=transport_state, usage=app.usage_store,
        sender_wake=lambda: runtime.sender.wake() if runtime.sender is not None else None,
        clock=runtime.store.clock)
