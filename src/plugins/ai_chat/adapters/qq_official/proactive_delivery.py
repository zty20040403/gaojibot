"""Score eligible unsolicited messages and enqueue replies in the normal sender."""
from dataclasses import dataclass


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


async def score_and_deliver(*, source, scorer, current, send_policy, enqueue, now):
    # Source validation can reject a revoked/non-target source. Send permission
    # is deliberately not consulted before the existing classifier runs.
    lifetime=await current(source)
    if type(lifetime) is not ProactiveLifetime:raise PermissionError('proactive_source_lifetime_unavailable')
    if now()>=lifetime.deadline_at:return ProactiveOutcome('discarded','source_expired')
    decision=await scorer(source)
    if await current(source)!=lifetime:raise PermissionError('proactive_source_changed')
    if not decision.reply.strip():
        return ProactiveOutcome('discarded','silent')
    observed=now()
    if observed>=lifetime.deadline_at:return ProactiveOutcome('discarded','source_expired')
    choice=await send_policy(source,purpose='unsolicited',deadline=lifetime.deadline_at)
    if not choice:
        return ProactiveOutcome('discarded','send_unavailable')
    if await current(source)!=lifetime:raise PermissionError('proactive_source_changed')
    if now()>=lifetime.deadline_at:return ProactiveOutcome('discarded','source_expired')
    # Permission and rate waits belong to the shared sender.
    await enqueue(source,decision.reply,purpose='unsolicited',expires_at=lifetime.deadline_at)
    return ProactiveOutcome('queued','current_send_authority')
