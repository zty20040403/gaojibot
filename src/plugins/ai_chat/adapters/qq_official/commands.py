"""Official adapter around the real shared command router; no legacy event or LLM."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import inspect

from src.plugins.ai_chat.application.commands import (
    CommandResult, CommandServices, dispatch_command, parse_command,
)
from src.plugins.ai_chat.platform_types import InboundMessage


@dataclass(frozen=True)
class RouteResult:
    kind: str
    result: CommandResult | None = field(default=None, repr=False)


async def _await(value):
    return await value if inspect.isawaitable(value) else value


async def route_command(text, inbound: InboundMessage, *, services: CommandServices,
                        admission, before_effect, services_factory=None) -> RouteResult:
    """Trusted runtime callbacks, not booleans/capabilities supplied by a model.

    before_effect MUST durably begin_command_execution (prepared -> executing),
    not merely check a lease, before settings/task-cancel/usage callbacks run.
    The caller checkpoints its result and performs the sole existing M3 handoff.
    """
    if (inbound.endpoint.account.platform != "qq-official" or inbound.actor.identity_kind == "bot"
            or not inbound.actor.matches_endpoint(inbound.endpoint)):
        return RouteResult("suppressed")
    if not isinstance(text, str) or not text.lstrip().startswith(("/", "!")):
        return RouteResult("not_command")
    # Binding tokens never reach the shared command callback, an execution row,
    # the normal inbox body or an LLM. M2 has already handled this trusted route.
    prefix = text.lstrip()[1:].split(None, 1)[0].casefold() if text.lstrip()[1:] else ""
    if prefix in {"bind", "绑定", "whoami"}:
        return RouteResult("control")
    if await _await(admission(inbound)) is not True:
        return RouteResult("suppressed")
    parsed = parse_command(text)
    await _await(before_effect(parsed.name if parsed is not None else "unsupported", inbound))
    if parsed is None:
        return RouteResult("reply", CommandResult("unsupported", "此入口暂不支持该命令。可用命令请查看 /help。"))
    if services_factory is not None:
        services = await _await(services_factory())
        if not isinstance(services, CommandServices):
            raise ValueError("command_services_factory_invalid")
    # Defensive removal prevents accidental future parser changes from enabling
    # a second binding reducer behind the generic router.
    result = await dispatch_command(parsed, inbound, services=replace(services, binding_handler=None))
    if result.status == "unknown":
        result = CommandResult("unsupported", "此入口暂不支持该命令。可用命令请查看 /help。")
    return RouteResult("reply", result)


def usage_for_actor(cursor, inbound):
    """Owned completed turn totals, not legacy scope-wide/canonical-alias totals."""
    endpoint, actor = inbound.endpoint, inbound.actor
    if not actor.matches_endpoint(endpoint) or actor.identity_kind == "bot":
        raise ValueError("invalid_usage_actor")
    row = cursor.execute("""SELECT COUNT(*) AS turns,
        COALESCE(SUM(t.input_tokens),0) AS input_tokens,
        COALESCE(SUM(t.output_tokens),0) AS output_tokens,
        COALESCE(SUM(t.total_tokens),0) AS total_tokens
        FROM qq_official_turn_executions x JOIN agent_turns t ON t.turn_id=x.journal_turn_id
        JOIN turn_visibility v ON v.scope_key=t.scope_key
        WHERE x.account_key=? AND x.endpoint_key=? AND x.requester_actor_key=?
          AND t.turn_ordinal>=v.min_turn_ordinal AND t.status<>'running'""",
        (endpoint.account.key, endpoint.key, actor.key)).fetchone()
    return {key: int(row[key] if row else 0) for key in ("turns", "input_tokens", "output_tokens", "total_tokens")}


def usage_callback(transaction, db_call):
    async def callback(inbound, _parsed):
        def read():
            with transaction() as cursor:
                return usage_for_actor(cursor, inbound)
        totals = await _await(db_call(read))
        return CommandResult("ok", "本人当前入口已完成回合用量：\n"
            f"回合：{totals['turns']}\n输入 Token：{totals['input_tokens']}\n"
            f"输出 Token：{totals['output_tokens']}\n合计 Token：{totals['total_tokens']}")
    return callback


from ...agent.control import LeaseLost


class CommandEffectDenied(LeaseLost):
    pass


class _CommandTasks:
    def __init__(self, tasks, inbound, scope):
        self.tasks, self.inbound, self.scope = tasks, inbound, scope

    def list_for_endpoint(self, *args):
        with self.scope(self.inbound):
            return self.tasks.list_for_endpoint(*args)

    def cancel_for_endpoint(self, *args):
        with self.scope(self.inbound):
            return self.tasks.cancel_for_endpoint(*args)


def guarded_services(services, inbound, *, effect_scope, database_call, settings_authority=None):
    async def call(function, *args):
        def run():
            with effect_scope(inbound):
                return function(*args)
        return await database_call(run)
    tasks = _CommandTasks(services.running_tasks, inbound, effect_scope) if services.running_tasks is not None else None
    return replace(services, db_call=call, running_tasks=tasks)
