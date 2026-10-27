"""Explicit, instance-scoped wiring for the OneBot application services."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .context_pipeline import ReferenceResolver
from .proactive import ProactiveCheckGate
from .runtime import AppContext

if TYPE_CHECKING:
    from .adapters import OneBotIngestAdapter
    from .onebot_chat import OneBotChatService
    from .command_handlers import CommandHandlers
    from .message_ingest import MessageIngest
    from .onebot_delivery import OneBotDelivery
    from .reply_service import ReplyService
    from .tool_executor import ToolExecutor
    from .trigger_service import TriggerService
    from .video_analysis import DeepVideoAnalyzer
    from .routine_worker import RoutineDispatcher


class HandlerService:
    def __init__(self, services: HandlerServices) -> None:
        self.services = services
        self.context = services.context


@dataclass
class HandlerServices:
    context: AppContext
    video_analyzer: DeepVideoAnalyzer | None = None
    ingest_adapter: OneBotIngestAdapter | None = None
    reference_resolver: ReferenceResolver = field(init=False)
    proactive_gate: ProactiveCheckGate = field(default_factory=ProactiveCheckGate)
    commands: CommandHandlers = field(init=False)
    ingest: MessageIngest = field(init=False)
    triggers: TriggerService = field(init=False)
    chat: OneBotChatService = field(init=False)
    tools: ToolExecutor = field(init=False)
    replies: ReplyService = field(init=False)
    delivery: OneBotDelivery = field(init=False)
    routines: RoutineDispatcher | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        from .onebot_chat import OneBotChatService
        from .command_handlers import CommandHandlers
        from .message_ingest import MessageIngest
        from .onebot_delivery import OneBotDelivery
        from .reply_service import ReplyService
        from .tool_executor import ToolExecutor
        from .trigger_service import TriggerService

        if getattr(self.context, "mobile_authorization", None) is not None and getattr(self.context, "fleet_client", None) is not None:
            from .server_task_authorization import ServerTaskAuthorization
            self.context.fleet_client.authorization.tasks = ServerTaskAuthorization(self.context)

        self.reference_resolver = ReferenceResolver(graph_store=self.context.topic_graph_store)
        self.commands = CommandHandlers(self)
        self.ingest = MessageIngest(self)
        self.triggers = TriggerService(self)
        self.chat = OneBotChatService(self)
        self.tools = ToolExecutor(self)
        self.replies = ReplyService(self)
        self.delivery = OneBotDelivery(self)
        if getattr(self.context, "routine_store", None) is not None:
            from .routine_worker import RoutineDispatcher

            self.routines = RoutineDispatcher(self)
        coordinator = getattr(self.context, "subagent_coordinator", None)
        if (
            coordinator is not None
            and getattr(self.context, "job_store", None) is not None
            and getattr(self.context, "delivery_store", None) is not None
        ):
            from .agent.background import SubAgentDispatcher

            coordinator.dispatcher = SubAgentDispatcher(self)

    def group_enabled(self, group_id: int) -> bool:
        override = self.context.model_preferences.get_group_enabled_override(group_id)
        return override if override is not None else self.context.settings.is_group_enabled(group_id)

    def auto_describe_enabled(self, group_id: int) -> bool:
        override = self.context.model_preferences.get_group_vision_auto_describe_override(group_id)
        return override if override is not None else self.context.settings.vision_auto_describe
