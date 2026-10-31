from __future__ import annotations

import unittest




from src.plugins.ai_chat.message_ir import (
    ForwardNode,
    MediaNode,
    MessageBody,
    MentionNode,
    TextNode,
    body_from_json,
    body_to_json,
    render_fallback_text,
    render_prompt_text,
    resolve_mentions,
)
from src.plugins.ai_chat.message_lowering import (
    OutboundCapabilities,
    lower_message,
)



class MessageIRTests(unittest.TestCase):

    def test_prompt_uses_canonical_principal_and_media_handles(self) -> None:
        body=MessageBody((MentionNode(0,'TEST_USER','Alice'),MediaNode(1,'image',description='截图')))
        resolved=resolve_mentions(body,lambda native_id,display:7)
        prompt = render_prompt_text(resolved, canonical_message_id=12)

        self.assertIn("[mention#7: Alice]", prompt)
        self.assertIn("[image#12.1: 截图]", prompt)



    def test_versioned_json_round_trip_handles_raw_bytes(self) -> None:
        body=MessageBody((MediaNode(0,'audio',raw_data={'content':b'silk-data'}),))
        restored = body_from_json(body_to_json(body))

        self.assertEqual(restored, body)
        self.assertTrue(restored.has_media("audio"))



    def test_capability_lowering_is_auditable_and_chunks_utf8_safely(self) -> None:
        body = MessageBody(
            (
                MentionNode(0, "123", "Alice"),
                TextNode(1, "你好世界abcdef"),
                MediaNode(2, "image", source="photo.jpg"),
            )
        )
        lowered = lower_message(
            body,
            OutboundCapabilities(
                mention="text",
                image="drop",
                max_text_bytes=6,
            ),
            destination_platform="text-only",
        )

        self.assertTrue(any(note.node_kind == "MentionNode" for note in lowered.notes))
        self.assertTrue(any(note.action == "drop" for note in lowered.notes))
        for chunk in lowered.chunks:
            text = render_fallback_text(chunk)
            self.assertLessEqual(len(text.encode("utf-8")), 6)
        self.assertEqual(
            "".join(render_fallback_text(chunk) for chunk in lowered.chunks),
            "@Alice你好世界abcdef",
        )

    def test_forward_node_round_trips_but_lowers_to_text(self) -> None:
        body = MessageBody((ForwardNode(0, "forward-1", 3),))
        restored = body_from_json(body_to_json(body))
        rendered = render_fallback_text(restored)

        self.assertEqual(restored, body)
        self.assertEqual(rendered, "[合并转发:3 条]")






if __name__ == "__main__":
    unittest.main()
