"""Score first, then keep only a currently deliverable unsolicited response.

Host ports bind the actual immutable ingress source and existing scorer/outbox.
This helper does not manufacture an execution or relax a send window.
"""
from dataclasses import dataclass
from src.plugins.ai_chat.adapters.qq_official.send_policy import PolicyWait, SendPolicyChoice


@dataclass(frozen=True)
class ProactiveLifetime:
    received_at: int
    deadline_at: int

    def __post_init__(self):
        if (type(self.received_at) is not int or type(self.deadline_at) is not int
                or not 0<=self.received_at<self.deadline_at<=self.received_at+30):
            raise ValueError('invalid_proactive_lifetime')


@dataclass(frozen=True)
class ProactiveOutcome:
    status: str
    reason: str


async def score_and_deliver(*, source, scorer, current, send_policy, enqueue, now, threshold):
    # Source validation can reject a revoked/non-target source. Send permission
    # is deliberately not consulted before the existing classifier runs.
    lifetime=await current(source)
    if type(lifetime) is not ProactiveLifetime:raise PermissionError('proactive_source_lifetime_unavailable')
    if now()>=lifetime.deadline_at:return ProactiveOutcome('discarded','source_expired')
    decision=await scorer(source)
    if await current(source)!=lifetime:raise PermissionError('proactive_source_changed')
    if not decision.should_reply(threshold) or not decision.reply.strip():
        return ProactiveOutcome('discarded','below_threshold')
    observed=now()
    if observed>=lifetime.deadline_at:return ProactiveOutcome('discarded','source_expired')
    choice=await send_policy(source,purpose='unsolicited',deadline=lifetime.deadline_at)
    if isinstance(choice,PolicyWait):
        return ProactiveOutcome('discarded','send_unavailable')
    if (not isinstance(choice,SendPolicyChoice) or choice.purpose!='unsolicited'
            or choice.requires_probe or choice.send_by_at<=observed):
        return ProactiveOutcome('discarded','send_unavailable')
    if await current(source)!=lifetime:raise PermissionError('proactive_source_changed')
    if now()>=lifetime.deadline_at:return ProactiveOutcome('discarded','source_expired')
    # The actual enqueue and M3 prepare independently recheck source/window;
    # an expired window yields unsolicited_window_unavailable, never a backlog.
    await enqueue(source,decision.reply,purpose='unsolicited',expires_at=min(lifetime.deadline_at,choice.send_by_at))
    return ProactiveOutcome('queued','current_send_authority')
