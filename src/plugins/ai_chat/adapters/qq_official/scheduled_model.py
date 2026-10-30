"""Shared model execution for native reminders and routines."""
from dataclasses import dataclass, replace
from ...message_ir import MessageBody, TextNode
from ...platform_types import ActorKey, EndpointKey


class ContinuationDenied(ValueError):
    pass


@dataclass(frozen=True, repr=False)
class ScheduledWork:
    endpoint: EndpointKey
    actor: ActorKey
    prompt: str
    execution_deadline: int
    result_deadline: int
    max_tokens: int
    run_id: str


class SharedScheduledModel:
    """The existing common loop; intentionally no host/project/file tools.

    Profile selection is a trusted host callback using the structured actor.
    Capability-granted project work is a separate integration, not inferred from
    old numeric creator/project memberships. No fabricated inbound message.
    """
    def __init__(self, profile_for, usage_callback=None):
        self.profile_for = profile_for
        self.usage_callback = usage_callback

    async def __call__(self, work):
        from src.plugins.ai_chat.agent.control import assert_job_owned
        from src.plugins.ai_chat.agent_loop import ask_deepseek_with_tools
        from src.plugins.ai_chat.model_budget import ModelCallBudget, model_budget_scope
        assert_job_owned()
        profile = self.profile_for(work)
        profile = replace(profile, max_output_tokens=min(profile.max_output_tokens or work.max_tokens, work.max_tokens))
        async def unavailable(*args, **kwargs):
            raise ContinuationDenied()
        budget = ModelCallBudget(work.max_tokens, before_request=assert_job_owned)
        try:
            with model_budget_scope(budget):
                answer = await ask_deepseek_with_tools(work.prompt, [], [], unavailable,
                    profile=profile, max_tool_rounds=0,
                    system_context='Execute this approved scheduled text task. No tools are authorized.')
        finally:
            if self.usage_callback is not None:
                await self.usage_callback(work, budget.summary())
        assert_job_owned()
        return MessageBody((TextNode(0, answer),))
