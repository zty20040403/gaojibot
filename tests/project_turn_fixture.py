"""Real ledger and journal identity for project mutation tests."""

from src.plugins.ai_chat.message_ir import MessageBody, TextNode


def start_project_turn(ledger, journal, scope, *, message_id=5, direction="inbound"):
    message = ledger.record_message(
        scope,
        native_message_id=str(message_id),
        sender_native_user_id=scope.actor_native_user_id,
        sender_display="Fixture",
        body=MessageBody((TextNode(0, "Change the project"),)),
        direction=direction,
    )
    return journal.start_turn(
        scope,
        trigger_canonical_message_id=message.canonical_message_id,
        objective="Fixture project edit",
        provider="fixture",
        model="fixture",
    )
