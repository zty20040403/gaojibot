from __future__ import annotations

import tempfile
import unittest
from pathlib import Path



from src.plugins.ai_chat.self_source import SelfSource
from src.plugins.ai_chat.skills import SkillRegistry


class SelfSourceTests(unittest.TestCase):
    def test_reads_and_searches_only_allowlisted_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "src" / "demo.py").write_text(
                "first\nimportant_symbol = 1\n",
                encoding="utf-8",
            )
            (root / ".env").write_text("SECRET=yes", encoding="utf-8")
            source = SelfSource(root)
            paths, truncated = source.paths()
            self.assertFalse(truncated)
            self.assertEqual(paths, ["src/demo.py"])
            matches = source.search("important_symbol")
            self.assertEqual((matches[0].path, matches[0].line), ("src/demo.py", 2))
            self.assertIn("2 | important_symbol", source.read("src/demo.py")["content"])
            with self.assertRaises(ValueError):
                source.read(".env")
            with self.assertRaises(ValueError):
                source.read("../outside.txt")

            (root / "src" / "alias.py").symlink_to(root / "src" / "demo.py")
            with self.assertRaises(ValueError):
                source.read("src/alias.py")

    def test_identity_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tests").mkdir()
            (root / "tests" / "x.py").write_text("x = 1\n", encoding="utf-8")
            source = SelfSource(root)
            self.assertEqual(source.identity(), source.identity())
            self.assertEqual(source.identity()["file_count"], 1)


class SkillRegistryTests(unittest.TestCase):
    def test_index_is_short_and_body_is_loaded_on_demand(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "demo.md").write_text(
                "# Demo\nSummary: concise summary\n\nfull secret workflow",
                encoding="utf-8",
            )
            registry = SkillRegistry(directory)
            self.assertIn("concise summary", registry.prompt_index())
            self.assertNotIn("full secret workflow", registry.prompt_index())
            self.assertIn("full secret workflow", registry.get("demo").body)  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
