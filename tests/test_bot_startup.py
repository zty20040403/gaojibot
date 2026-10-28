from contextlib import nullcontext
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch


class BotStartupTests(unittest.TestCase):
    def test_required_plugin_failure_does_not_start_empty_server(self) -> None:
        self._run_entrypoint(plugin=None, fails=True)

    def test_loaded_plugin_starts_server(self) -> None:
        self._run_entrypoint(plugin=object(), fails=False)

    def _run_entrypoint(self, *, plugin: object | None, fails: bool) -> None:
        with (
            patch("dotenv.load_dotenv"),
            patch("nonebot.init"),
            patch("nonebot.get_driver"),
            patch("nonebot.load_plugin", return_value=plugin) as load_plugin,
            patch("nonebot.run") as run,
        ):
            path = Path(__file__).resolve().parents[1] / "bot.py"
            if fails:
                with self.assertRaisesRegex(SystemExit, "Required ai_chat plugin failed"):
                    runpy.run_path(str(path), run_name="__main__")
                run.assert_not_called()
            else:
                runpy.run_path(str(path), run_name="__main__")
                run.assert_called_once_with(timeout_graceful_shutdown=10)
            load_plugin.assert_called_once_with("src.plugins.ai_chat.onebot_plugin")


class LocalRuntimePluginStartupTests(unittest.TestCase):
    def test_desktop_container_loads_explicit_onebot_entrypoint(self) -> None:
        from src.local_runtime.entrypoint import main

        with (
            patch("src.local_runtime.entrypoint.load_environment", return_value=""),
            patch("src.local_runtime.cluster_credentials.cluster_credentials", return_value=nullcontext()),
            patch("src.local_runtime.runtime_health.install_health_writer") as health,
            patch("nonebot.init"),
            patch("nonebot.get_driver"),
            patch("nonebot.load_plugin", return_value=object()) as load_plugin,
            patch("nonebot.run") as run,
        ):
            self.assertEqual(main(["serve"]), 0)
            load_plugin.assert_called_once_with("src.plugins.ai_chat.onebot_plugin")
            health.assert_called_once_with()
            run.assert_called_once_with(timeout_graceful_shutdown=10)
