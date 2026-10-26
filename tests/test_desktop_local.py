import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch
from src.local_runtime.files import read_json, write_json
from src.local_runtime.desktop import DesktopController
from src.local_runtime.setup_clients import SetupError
from src.local_runtime.desktop import desktop_compose, ROOT


class DesktopComposeTests(unittest.TestCase):
    def test_install_progress_reports_safe_categories_and_failure_without_raw_logs(self):
        with tempfile.TemporaryDirectory() as temporary:
            c = DesktopController.__new__(DesktopController)
            c.directory = Path(temporary)
            c.install_path = c.directory / 'install.json'
            c.deployment = {'docker': '/test/docker'}
            write_json(c.install_path, {'stage': 'database', 'message': 'preparing'})
            def observed(arguments, **options):
                self.assertEqual(arguments[-3:], ['pull', 'database', 'napcat'])
                options['log'].write(b'private-token-not-for-ui')
                options['report']({'detail': '解压依赖', 'elapsed_seconds': 12, 'quiet_seconds': 0, 'failure': '磁盘空间不足'})
                return 1
            with patch('src.local_runtime.image_install.run_observed', observed):
                with self.assertRaisesRegex(SetupError, '磁盘空间不足'):
                    c.install_compose(['pull', 'database', 'napcat'], timeout=30)
            state = read_json(c.install_path)
            self.assertEqual(state['progress']['elapsed_seconds'], 12)
            self.assertNotIn('private-token', str(state))
            self.assertNotIn('failure', state['progress'])

    def test_named_volume_isolation_and_loopback(self):
        config = {"id": "e" * 32, "ports": {"bot": 28080, "napcat": 26099}}
        directory = ROOT / "data/instances" / config["id"]
        job = "a" * 32
        deployment = {"job": job, "runtime_image": "gaoji-app-runtime:" + "b" * 64,
                      "volume": "gaoji-desktop-" + job + "-data", "network": "gaoji-desktop-" + job}
        images = {"postgres": "pgvector/pgvector@sha256:" + "c" * 64,
                  "napcat": "mlikiowa/napcat-docker@sha256:" + "d" * 64}
        document = desktop_compose(config, directory, deployment, images)
        self.assertNotIn("ports", document["services"]["database"])
        self.assertNotIn("ports", document["services"]["napcat"])
        self.assertTrue(document["services"]["bot"]["ports"][0].startswith("127.0.0.1:"))
        self.assertEqual(document["services"]["bot"]["pull_policy"], "never")
        for service in document["services"].values():
            for mount in service["volumes"]:
                self.assertEqual(mount["type"], "volume")
                self.assertTrue(mount["volume"]["subpath"].startswith("data/instances/" + config["id"] + "/"))
                self.assertNotIn("docker.sock", mount["target"])
                self.assertNotEqual(mount["volume"]["subpath"], "data/instances/" + config["id"] + "/config")
        deployment["volume"] = "somebody-elses-data"
        with self.assertRaises(ValueError):
            desktop_compose(config, directory, deployment, images)


if __name__ == "__main__":
    unittest.main()
