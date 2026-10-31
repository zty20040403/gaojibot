import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from src.bot_security.cli import main


class DeploymentBootstrapTests(unittest.TestCase):
    def test_generated_password_is_private_and_existing_accounts_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / "key"
            key.write_bytes(b"x" * 32)
            key.chmod(0o600)
            output = root / "initial-password"
            store = MagicMock()
            store.accounts.return_value = []
            with patch.dict(os.environ, AI_POSTGRES_DSN="isolated-test"), patch("src.bot_security.cli.PostgresDatabase"), patch("src.bot_security.cli.SecurityStore", return_value=store):
                args = ["bootstrap", "--secret-file", str(key), "--username", "kenneth", "--qq-id", "3526452465", "--generate-password-file", str(output)]
                self.assertEqual(main(args), 0)
                password = output.read_text().strip()
                self.assertGreaterEqual(len(password), 24)
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
                store.bootstrap.assert_called_once_with("kenneth", password, "3526452465")
                store.accounts.return_value = [{"username": "kenneth"}]
                self.assertEqual(main(args), 1)
                self.assertEqual(output.read_text().strip(), password)
