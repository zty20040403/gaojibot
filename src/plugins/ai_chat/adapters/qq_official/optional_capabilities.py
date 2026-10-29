"""Explicit unavailable outcomes for unverified optional QQ contracts."""
from dataclasses import dataclass

from src.plugins.ai_chat.platform_types import EffectiveCapabilities

OPTIONAL = frozenset({'recall_own','recall_member','mute_member','remove_member',
                      'member_join_notice','reaction_feedback','voice_output'})


@dataclass(frozen=True)
class OptionalOutcome:
    status: str
    reason: str


def optional_operation(operation, *, effective: EffectiveCapabilities, now,
                       developer_approved=False, group_authorized=False,
                       local_operator=False, tool_policy=False):
    """No guessed REST action; evidence alone cannot create a transport port.

    M7 must add an independently verified physical contract/adapter and tests
    before this module may report an available operation. A missing reaction is
    never converted to negative feedback.
    """
    if operation not in OPTIONAL:
        return OptionalOutcome('unavailable','unsupported_operation')
    if not effective.allows(operation,now):
        return OptionalOutcome('unavailable','platform_contract_unverified')
    if any(value is not True for value in (developer_approved,group_authorized,local_operator,tool_policy)):
        return OptionalOutcome('denied','capability_intersection_denied')
    return OptionalOutcome('unavailable','verified_transport_unavailable')


def reaction_reward(event):
    # No reaction contract has been established. Even an apparently positive
    # payload remains untrusted; an absent event is not a negative sample.
    return None
