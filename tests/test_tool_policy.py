from __future__ import annotations

import unittest



from src.plugins.ai_chat.tool_policy import (
    ToolCatalog,
    configure_tool_overrides,
    enabled_tool_definitions,
    set_tool_enabled,
)


class ToolPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        configure_tool_overrides({})
        self.addCleanup(configure_tool_overrides, {})
        self.catalog = ToolCatalog(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "search",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 20,
                                },
                                "message_handle": {
                                    "type": "string",
                                    "pattern": r"^msg#[1-9][0-9]*$",
                                },
                                "limit": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 5,
                                },
                            },
                            "required": ["query"],
                            "additionalProperties": False,
                        },
                    },
                }
            ]
        )

    def test_accepts_declared_arguments(self) -> None:
        result = self.catalog.validate("search", {"query": "docs", "limit": 3})
        self.assertTrue(result.ok)

    def test_rejects_unknown_tool_missing_required_and_extra_fields(self) -> None:
        self.assertFalse(self.catalog.validate("shell", {}).ok)
        missing = self.catalog.validate("search", {"limit": 3})
        extra = self.catalog.validate("search", {"query": "docs", "group_id": 9})
        self.assertIn("必填", missing.message)
        self.assertIn("未声明字段", extra.message)

    def test_rejects_wrong_types_and_bounds(self) -> None:
        wrong_type = self.catalog.validate("search", {"query": "docs", "limit": True})
        too_large = self.catalog.validate("search", {"query": "docs", "limit": 9})
        self.assertIn("类型", wrong_type.message)
        self.assertIn("不能大于", too_large.message)

    def test_parse_errors_fail_before_schema(self) -> None:
        result = self.catalog.validate(
            "search",
            {},
            parse_error="invalid json",
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.message, "invalid json")

    def test_rejects_malformed_canonical_handle(self) -> None:
        result = self.catalog.validate(
            "search",
            {"query": "docs", "message_handle": "123456"},
        )
        self.assertFalse(result.ok)
        self.assertIn("格式", result.message)

    def test_model_can_request_owned_sandbox_destruction_without_approval(self) -> None:
        from src.plugins.ai_chat.ai_tools import SANDBOX_TOOLS
        from src.plugins.ai_chat.tool_policy import policy_for_tool

        names = {item["function"]["name"] for item in SANDBOX_TOOLS}
        self.assertIn("sandbox_destroy", names)
        self.assertEqual(policy_for_tool("sandbox_destroy").approval, "never")

    def test_disabled_tool_is_removed_from_model_catalog(self) -> None:
        definitions = [
            {
                "type": "function",
                "function": {"name": "web_search", "parameters": {"type": "object"}},
            },
            {
                "type": "function",
                "function": {"name": "context_search", "parameters": {"type": "object"}},
            },
        ]
        set_tool_enabled("web_search", False)

        enabled = enabled_tool_definitions(definitions)

        self.assertEqual(
            [item["function"]["name"] for item in enabled],
            ["context_search"],
        )


if __name__ == "__main__":
    unittest.main()
