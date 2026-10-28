"""Keep the wire protocol reusable without the application or platform SDKs."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import unittest


class ProtocolBoundaryTests(unittest.TestCase):
    def test_protocol_imports_only_its_own_package_and_external_libraries(self):
        root = Path(__file__).resolve().parents[1]
        package = root / "src" / "qq_official"
        files = sorted(package.rglob("*.py"))
        self.assertTrue(files, "protocol package must exist")
        for path in files:
            module = ".".join(path.relative_to(root).with_suffix("").parts)
            parent = module.rsplit(".", 1)[0]
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    name = "." * node.level + (node.module or "")
                    names = [importlib.util.resolve_name(name, parent)]
                for name in names:
                    with self.subTest(path=str(path.relative_to(root)), line=node.lineno):
                        self.assertFalse(name == "nonebot" or name.startswith("nonebot."))
                        self.assertFalse(name == "gaojibot" or name.startswith("gaojibot."))
                        if name == "src" or name.startswith("src."):
                            self.assertTrue(
                                name == "src.qq_official" or name.startswith("src.qq_official."),
                                f"protocol imports application code: {name}",
                            )


if __name__ == "__main__":
    unittest.main()
