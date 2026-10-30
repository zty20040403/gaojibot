import copy
import json
import unittest
from pathlib import Path

from src.qq_official.events import parse_dispatch


class ObservedGroupEventTests(unittest.TestCase):
    def test_observed_general_group_event_requires_structured_mention_evidence(self):
        path = Path(__file__).parent / "fixtures" / "qq_official" / "observed_group_message.json"
        frame = json.loads(path.read_text())
        parsed = parse_dispatch(frame)
        self.assertEqual(parsed.event_type, "GROUP_MESSAGE_CREATE")
        self.assertEqual(parsed.message.kind, "group")
        self.assertEqual(parsed.message.message_type, 0)
        self.assertTrue(parsed.message.mentions_bot)
        self.assertTrue(parsed.message.msg_idx.startswith("reference_"))
        self.assertIsNone(parsed.message.ref_msg_idx)
        self.assertFalse(parsed.message.author.is_bot)

        # The platform role and a literal tag are not substitutes for is_you.
        without_mention = copy.deepcopy(frame)
        without_mention["d"]["mentions"][0].pop("is_you")
        without_mention["d"]["content"] = "<@actor_f9436d675f96568f>"
        self.assertFalse(parse_dispatch(without_mention).message.mentions_bot)
