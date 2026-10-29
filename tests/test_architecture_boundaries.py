from __future__ import annotations

import ast
import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from pathlib import Path


PLUGIN_ROOT = Path(__file__).parents[1] / "src" / "plugins" / "ai_chat"


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_plugin_entrypoint_only_composes_and_registers(self) -> None:
        entrypoint = PLUGIN_ROOT / "onebot_plugin.py"
        source = entrypoint.read_text(encoding="utf-8")
        self.assertLessEqual(len(source.splitlines()), 100)
        tree = ast.parse(source, filename=str(entrypoint))
        allowed_functions = {
            "ignore_disabled_group_event",
            "startup_app_context",
            "shutdown_app_context",
            "connect_migration_baseline",
        }
        definitions = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertEqual(definitions, allowed_functions)
        self.assertNotIn("FunctionType", source)
        self.assertNotIn("globals()", source)
        self.assertNotIn("build_app_context", source)
        self.assertNotIn("app_context =", source)
        self.assertNotIn("handlers =", source)

    def test_shared_package_entrypoint_has_no_platform_composition(self) -> None:
        source = (PLUGIN_ROOT / "__init__.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        self.assertTrue(all(
            isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            for node in tree.body
        ), "shared package entrypoint must remain documentation-only")

    def test_split_responsibilities_have_single_owners(self) -> None:
        expected = {
            "command_handlers.py": {"handle_ai", "handle_model_command"},
            "message_ingest.py": {
                "handle_canonical_ingest",
                "handle_group_context_recorder",
            },
            "trigger_service.py": {
                "handle_proactive_chat",
                "_semantic_index_loop",
            },
            "onebot_chat.py": {
                "_current_turn_context",
                "_run_tracked_ai",
            },
            "tool_executor.py": {"_ask_ai"},
            "reply_service.py": {"_finish_tracked_ai", "_finish_safely"},
            "onebot_delivery.py": {"_delivery_loop", "_deliver_onebot_outbox"},
        }
        for filename, required in expected.items():
            path = PLUGIN_ROOT / filename
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            definitions = {
                node.name
                for owner in tree.body if isinstance(owner, ast.ClassDef)
                for node in owner.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            self.assertTrue(required <= definitions, f"{filename}: {required - definitions}")
            self.assertFalse(
                any(
                    node.decorator_list
                    for node in tree.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name.startswith("handle_")
                ),
                f"{filename} must not register Matchers",
            )
            imported_modules = {
                ("." * node.level) + (node.module or "")
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
            }
            self.assertNotIn(".", imported_modules, f"{filename} must not import the entrypoint")
            self.assertFalse(any(
                isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in {"globals", "FunctionType", "on_message", "on_command"}
                for node in ast.walk(tree)
            ))

    def test_legacy_agent_paths_remain_import_only_shims(self) -> None:
        for name in ("deepseek.py", "chat_orchestrator.py"):
            source = (PLUGIN_ROOT / name).read_text(encoding="utf-8")
            tree = ast.parse(source)
            self.assertLessEqual(len(source.splitlines()), 25)
            self.assertFalse(any(isinstance(node, ast.ClassDef) for node in tree.body))
            functions = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
            self.assertEqual(functions, {"__getattr__", "__dir__"})

    def test_admin_entrypoint_only_assembles_protected_page_routers(self) -> None:
        source = (PLUGIN_ROOT / "admin.py").read_text(encoding="utf-8")
        self.assertLessEqual(len(source.splitlines()), 150)
        tree = ast.parse(source)
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
        self.assertEqual([node.name for node in functions], ["register_admin"])
        self.assertFalse(any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.decorator_list
            for node in ast.walk(functions[0])
        ))
        for path in (PLUGIN_ROOT / "admin_api").glob("*.py"):
            module_source = path.read_text(encoding="utf-8")
            self.assertLessEqual(len(module_source.splitlines()), 450, path.name)
            for node in ast.walk(ast.parse(module_source)):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotEqual(node.module, "admin", path.name)

    def test_handlers_keep_native_globals_and_isolated_runtime(self) -> None:
        import nonebot
        nonebot.init()
        from src.plugins.ai_chat.handler_services import HandlerServices
        from src.plugins.ai_chat import command_handlers

        def services(profile):
            return HandlerServices(SimpleNamespace(
                topic_graph_store=None,
                model_preferences=SimpleNamespace(get_group_default=Mock(return_value=profile)),
                settings=SimpleNamespace(group_model_profiles={}),
            ))

        first, second = services("first"), services("second")
        self.assertEqual(first.chat._group_default_model_preference("group:1:user:7"), "first")
        self.assertEqual(second.chat._group_default_model_preference("group:1:user:7"), "second")
        self.assertEqual(first.chat._group_default_model_preference("group:1:user:7"), "first")
        self.assertIs(first.commands._format_elapsed.__func__.__globals__, vars(command_handlers))
        self.assertIs(first.tools.services.chat, first.chat)
        self.assertIsNot(first.chat, second.chat)

    def test_application_does_not_depend_on_chat_platform_sdk(self) -> None:
        self._assert_no_imports(
            PLUGIN_ROOT / "application",
            forbidden=("nonebot", ".adapters", "..adapters"),
        )

    def test_storage_is_independent_from_platform_and_use_cases(self) -> None:
        self._assert_no_imports(
            PLUGIN_ROOT / "storage",
            forbidden=("nonebot", ".application", "..application", ".adapters"),
        )

    def test_workers_do_not_import_matchers_or_platform_sdk(self) -> None:
        self._assert_no_imports(
            PLUGIN_ROOT / "workers",
            forbidden=("nonebot", ".matchers", "..matchers", ".adapters"),
        )

    def test_architecture_packages_exist(self) -> None:
        for package in (
            "adapters",
            "application",
            "agent",
            "tools",
            "storage",
            "workers",
        ):
            self.assertTrue((PLUGIN_ROOT / package / "__init__.py").is_file())
            imported = importlib.import_module(
                f"src.plugins.ai_chat.{package}"
            )
            self.assertIsNotNone(imported)

    def _assert_no_imports(
        self,
        root: Path,
        *,
        forbidden: tuple[str, ...],
    ) -> None:
        violations: list[str] = []
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    prefix = "." * node.level
                    names = [prefix + (node.module or "")]
                else:
                    continue
                for name in names:
                    if any(name == item or name.startswith(item + ".") for item in forbidden):
                        violations.append(f"{path.name}:{node.lineno} imports {name}")
        self.assertEqual(violations, [])
