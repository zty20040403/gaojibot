"""Host-resolved project identity shared by chat context and registry tools."""

from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent

from .agent.execution import active_agent_step
from .onebot_codec import scope_from_event


def projects_available(context):
    runtime = getattr(context.app, "project_runtime", None)
    return bool(
        "projects" in context.capabilities
        and runtime is not None
        and runtime.ready
        and not runtime.closed
        and context.app.settings.projects_enabled
        and not context.routine_run_id
        and active_agent_step.get() is None
        and isinstance(context.event, (GroupMessageEvent, PrivateMessageEvent))
    )


async def project_access(context, *, principal=None):
    app, scope = context.app, scope_from_event(context.event)
    if principal is None:
        principal = await app.db_call(
            app.message_ledger.principal_id_for_native,
            scope.platform,
            scope.actor_native_user_id,
        )
    if principal is None:
        principal = await app.db_call(
            app.message_ledger.ensure_principal_identity,
            scope.platform,
            scope.actor_native_user_id,
            "",
        )
    return await app.project_runtime.resolve_access(
        {
            "scope_key": scope.key,
            "bot_native_user_id": scope.bot_native_user_id,
            "creator_principal_id": principal,
        },
        principal,
    )
