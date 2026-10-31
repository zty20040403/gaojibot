from __future__ import annotations

from tests.postgres_fixture import postgres_for_test

import unittest



from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import (
    MediaNode,
    MentionNode,
    MessageBody,
    TextNode,
    body_to_json,
)


class MessageLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = MessageLedger(postgres_for_test(self))
        self.addCleanup(self.ledger.close)
        self.group_a = ConversationScope(
            "onebot-v11",
            "group",
            "100",
            actor_native_user_id="7",
        )
        self.group_b = ConversationScope(
            "onebot-v11",
            "group",
            "200",
            actor_native_user_id="7",
        )

    def record_text(
        self,
        scope: ConversationScope,
        native_message_id: str,
        text: str,
        *,
        user_id: str = "7",
        reply_to: str | None = None,
    ):
        return self.ledger.record_message(
            scope,
            native_message_id=native_message_id,
            sender_native_user_id=user_id,
            sender_display="Alice",
            body=MessageBody((TextNode(0, text),)),
            occurred_at=100 + int(native_message_id),
            reply_to_native_message_id=reply_to,
        )

    def test_duplicate_native_message_is_idempotent(self) -> None:
        first = self.record_text(self.group_a, "1", "first")
        second = self.record_text(self.group_a, "1", "updated")

        self.assertEqual(
            first.canonical_message_id,
            second.canonical_message_id,
        )
        stored = self.ledger.get_in_scope(
            self.group_a,
            first.canonical_message_id,
        )
        self.assertEqual(stored.rendered_text, "first")  # type: ignore[union-attr]
        self.assertEqual(stored.occurred_at, 101)  # type: ignore[union-attr]

    def test_inline_media_bytes_are_not_persisted_in_canonical_body(self) -> None:
        stored = self.ledger.record_message(
            self.group_a,
            native_message_id="9",
            sender_native_user_id="7",
            sender_display="Alice",
            body=MessageBody(
                (
                    MediaNode(
                        0,
                        "audio",
                        source=str(b"silk-data"),
                        raw_data={"file": b"silk-data"},
                    ),
                )
            ),
            occurred_at=109,
        )

        encoded = body_to_json(stored.body)
        media = stored.body.nodes[0]
        self.assertNotIn("c2lsay1kYXRh", encoded)
        self.assertNotIn("c2lsay1kZGF0YQ==", encoded)
        self.assertEqual(media.source, "")  # type: ignore[union-attr]
        self.assertIn("sha256", encoded)

    def test_guessed_id_cannot_cross_conversation_scope(self) -> None:
        secret = self.record_text(self.group_a, "1", "group A secret")

        self.assertIsNone(
            self.ledger.get_in_scope(
                self.group_b,
                secret.canonical_message_id,
            )
        )
        self.assertEqual(
            self.ledger.search_in_scope(self.group_b, "secret"),
            [],
        )

    def test_reply_relation_is_backfilled_when_target_arrives_later(self) -> None:
        reply = self.record_text(
            self.group_a,
            "2",
            "reply",
            reply_to="1",
        )
        self.assertIsNone(reply.reply_to_canonical_message_id)

        target = self.record_text(self.group_a, "1", "target")
        updated_reply = self.ledger.get_in_scope(
            self.group_a,
            reply.canonical_message_id,
        )

        self.assertEqual(
            updated_reply.reply_to_canonical_message_id,  # type: ignore[union-attr]
            target.canonical_message_id,
        )

    def test_clear_hides_old_messages_but_accepts_new_messages(self) -> None:
        old = self.record_text(self.group_a, "1", "old context")

        self.assertEqual(self.ledger.hide_history(self.group_a), 1)
        self.assertIsNone(
            self.ledger.get_in_scope(
                self.group_a,
                old.canonical_message_id,
            )
        )
        self.assertEqual(
            self.ledger.search_in_scope(self.group_a, "old"),
            [],
        )

        new = self.record_text(self.group_a, "2", "new context")
        self.assertIsNotNone(
            self.ledger.get_in_scope(
                self.group_a,
                new.canonical_message_id,
            )
        )

    def test_same_onebot_identity_has_stable_principal_across_groups(self) -> None:
        body = MessageBody((MentionNode(0, "88", "Bob"),))
        first = self.ledger.record_message(
            self.group_a,
            native_message_id="1",
            sender_native_user_id="7",
            sender_display="Alice",
            body=body,
            occurred_at=101,
        )
        second = self.ledger.record_message(
            self.group_b,
            native_message_id="2",
            sender_native_user_id="7",
            sender_display="Alice",
            body=body,
            occurred_at=102,
        )

        first_mention = first.body.nodes[0]
        second_mention = second.body.nodes[0]
        self.assertIsInstance(first_mention, MentionNode)
        self.assertIsInstance(second_mention, MentionNode)
        self.assertEqual(
            first_mention.principal_id,  # type: ignore[union-attr]
            second_mention.principal_id,  # type: ignore[union-attr]
        )

    def test_rendered_context_contains_canonical_handles_not_native_ids(self) -> None:
        message = self.record_text(self.group_a, "987654", "hello")

        rendered = self.ledger.render_recent(self.group_a)

        self.assertIn(f"msg#{message.canonical_message_id}", rendered)
        self.assertIn(f"[mention#{message.sender_principal_id}]", rendered)
        self.assertNotIn("987654", rendered)

    def test_roster_uses_each_principals_latest_display_name(self) -> None:
        first = self.ledger.record_message(
            self.group_a,
            native_message_id="1",
            sender_native_user_id="7",
            sender_display="Old Name",
            body=MessageBody((TextNode(0, "first"),)),
            occurred_at=101,
        )
        self.ledger.record_message(
            self.group_a,
            native_message_id="2",
            sender_native_user_id="7",
            sender_display="New Name",
            body=MessageBody((TextNode(0, "second"),)),
            occurred_at=102,
        )

        roster = self.ledger.render_roster(self.group_a)

        self.assertEqual(
            roster,
            f"[mention#{first.sender_principal_id}]=New Name",
        )

    def test_roster_registration_and_reverse_identity_lookup(self) -> None:
        principals = self.ledger.ensure_principal_identities(
            "onebot-v11",
            [(88, "Bob"), (99, "Carol")],
            seen_at=123,
        )

        self.assertEqual(
            self.ledger.native_identity_for_principal(
                "onebot-v11", principals["88"]
            ),
            ("88", "Bob"),
        )

    def test_commands_are_audited_but_excluded_from_prompt_projection(self) -> None:
        command = self.ledger.record_message(
            self.group_a,
            native_message_id="cmd-1",
            sender_native_user_id="1",
            sender_display="Alice",
            body=MessageBody((TextNode(0, "/clear"),)),
            message_kind="command",
        )
        feedback = self.ledger.record_message(
            self.group_a,
            native_message_id="note-1",
            sender_native_user_id="1",
            sender_display="Alice",
            body=MessageBody((TextNode(0, "!feedback 改成方案 B"),)),
            message_kind="chat",
        )

        self.assertEqual(command.rendered_text, "/clear")
        self.assertEqual(command.prompt_text, "")
        self.assertEqual(feedback.prompt_text, "改成方案 B")
        rendered = self.ledger.render_recent(self.group_a)
        self.assertNotIn("/clear", rendered)
        self.assertNotIn("!feedback", rendered)
        self.assertIn("改成方案 B", rendered)
        self.assertEqual(self.ledger.search_in_scope(self.group_a, "clear"), [])

    def test_global_index_projection_respects_visibility_and_command_kind(self) -> None:
        hidden = self.record_text(self.group_a, "1", "hidden")
        self.ledger.hide_history(self.group_a)
        visible = self.record_text(self.group_a, "2", "visible")
        self.ledger.record_message(
            self.group_b,
            native_message_id="cmd",
            sender_native_user_id="7",
            sender_display="Alice",
            body=MessageBody((TextNode(0, "/clear"),)),
            message_kind="command",
        )

        indexed = self.ledger.all_visible_messages()
        self.assertNotIn(hidden.canonical_message_id, [item.canonical_message_id for item in indexed])
        self.assertEqual([item.canonical_message_id for item in indexed], [visible.canonical_message_id])
        self.assertEqual({scope.key for scope in self.ledger.list_scopes()}, {self.group_a.key, self.group_b.key})


if __name__ == "__main__":
    unittest.main()
