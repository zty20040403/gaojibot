"""Memory addresses derived from trusted inbound identity and history routing."""

from .conversation_scope import ConversationScope
from .conversation_keys import conversation_key
from .platform_types import InboundMessage


def group_memory_scope(scope: ConversationScope) -> str:
    if scope.kind != "group":
        raise ValueError("group memory requires a group history scope")
    if scope.platform == "onebot-v11":
        return f"group:{scope.native_conversation_id}"
    if scope.account is None:
        return f"group:{scope.platform}:{scope.native_conversation_id}"
    return scope.key


def memory_scopes_for_inbound(inbound: InboundMessage) -> tuple[str | None, str]:
    group = group_memory_scope(inbound.scope) if inbound.scope.kind == "group" else None
    # Existing OneBot user memory was conversation-local; keep its exact key.
    user = (
        conversation_key(inbound)
        if inbound.endpoint.account.platform == "onebot-v11"
        else inbound.actor.key
    )
    return group, user
