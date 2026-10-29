"""Host-resolved project identity shared by chat context and registry tools."""

from .agent.execution import active_agent_step
from .platform_types import BackgroundToolSource
from .tools.state import structured_tool_source


def official_inbound(context):
    inbound = getattr(context, "inbound", None)
    return inbound if inbound is not None and inbound.endpoint.account.platform == "qq-official" else None


def official_source(context):
    source = structured_tool_source(context)
    return source if source is not None and source.endpoint.account.platform == "qq-official" else None


def official_provider(context):
    if official_source(context) is None:
        return None
    return getattr(getattr(context, "platform", None), "project_provider", None)


def _issue_project_access(store, provider):
    with store._transaction() as cursor:
        return provider.issue(cursor)


def projects_configured(context):
    if "projects" not in context.capabilities or context.routine_run_id or active_agent_step.get() is not None:
        return False
    runtime = getattr(context.app, "project_runtime", None)
    if official_inbound(context) is not None:
        return bool(runtime is not None and context.app.settings.projects_enabled
                    and official_provider(context) is not None)
    from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent
    return bool(
        runtime is not None
        and context.app.settings.projects_enabled
        and isinstance(context.event, (GroupMessageEvent, PrivateMessageEvent))
    )


def projects_available(context):
    return bool(
        projects_configured(context)
        and context.app.project_runtime.ready
        and not context.app.project_runtime.closed
    )


async def project_access(context, *, principal=None):
    source = official_source(context)
    if source is not None:
        provider = official_provider(context)
        runtime = getattr(context.app, "project_runtime", None)
        if (provider is None or runtime is None or not runtime.ready or runtime.closed
                or not context.app.settings.projects_enabled):
            raise PermissionError("Official project authority is unavailable")
        if isinstance(source, BackgroundToolSource):
            matches = getattr(provider, "matches_tool_source", None)
            if not callable(matches) or matches(source) is not True:
                raise PermissionError("Project background source changed")
        access = await context.app.db_call(_issue_project_access, context.app.project_runtime.store, provider)
        if access.endpoint != source.endpoint or access.actor != source.actor:
            raise PermissionError("Project source identity changed")
        if principal is not None and principal != access.principal_id:
            raise PermissionError("Project principal changed")
        return access
    from .onebot_codec import scope_from_event
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
