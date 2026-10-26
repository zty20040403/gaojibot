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
            patch("nonebot.load_plugin", return_value=plugin),
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
