"""Exercise the live-test harness with real process loss and a simulated WebUI."""
from __future__ import annotations

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest


@unittest.skipUnless(os.getenv("TEST_FILE_ACCEPTANCE_DSN"), "Requires a dedicated local acceptance database")
class AcceptanceHarnessProcessTests(unittest.TestCase):
    def test_five_real_crash_boundaries_using_simulated_napcat(self):
        files, requests = [], []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, body.get("action")))
                if self.path == "/api/auth/login":
                    data = {"Credential": "isolated-test-session"}
                elif self.headers.get("Authorization") != "Bearer isolated-test-session":
                    self.send_error(401)
                    return
                elif self.path == "/api/Debug/call":
                    action, params = body["action"], body["params"]
                    if action == "get_login_info":
                        data = {"user_id": 123}
                    elif action == "get_status" and not params:
                        data = {"online": True, "good": True}
                    elif action == "get_group_root_files" and params == {"group_id": 456, "file_count": 50}:
                        data = {"files": files}
                    elif action == "upload_group_file" and params["group_id"] == 456:
                        content = base64.b64decode(params["file"].removeprefix("base64://"), validate=True)
                        files.append({"file_name": params["name"], "file_size": len(content), "uploader": 123,
                                      "upload_time": 0, "file_id": f"simulated-{len(files) + 1}"})
                        data = None
                    else:
                        self.send_error(400)
                        return
                    data = {"status": "ok", "retcode": 0, "data": data}
                else:
                    self.send_error(404)
                    return
                response = json.dumps({"code": 0, "data": data}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "test-token").write_text("isolated-test-token")
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "tools.live_file_outbox_acceptance",
                        "--webui-url",
                        f"http://127.0.0.1:{server.server_port}",
                        "--bot-id",
                        "123",
                        "--group-id",
                        "456",
                        "--requester-id",
                        "789",
                        "--token-file",
                        str(root / "test-token"),
                        "--output-dir",
                        str(root / "output"),
                        "--execute-live",
                        "--confirm-group",
                        "456",
                    ],
                    cwd=Path(__file__).resolve().parents[1],
                    capture_output=True,
                    text=True,
                    timeout=240,
                    env={
                        **os.environ,
                        "AI_SUBAGENTS_ENABLED": "false",
                        "TEST_POSTGRES_DSN": os.environ["TEST_FILE_ACCEPTANCE_DSN"],
                    },
                )
                self.assertEqual(result.returncode, 0, result.stdout[-1500:] + result.stderr[-1500:])
                report = json.loads((root / "output/result.json").read_text())
                self.assertEqual(report["status"], "passed")
                self.assertEqual(report["schema_cleanup"], "completed")
                self.assertEqual(len(report["cases"]), 5)
                self.assertEqual([row["final_state"] for row in report["cases"]],
                                 ["acknowledged", "acknowledged", "unknown", "acknowledged", "acknowledged"])
                self.assertEqual(sum(row["upload_reservations"] for row in report["cases"]), 4)
                self.assertEqual(len(files), 4)
                self.assertEqual(len({row["file_name"] for row in files}), 4)
                self.assertEqual(sum(action == "upload_group_file" for _, action in requests), 4)
                self.assertNotIn("isolated-test-session", (root / "output/result.json").read_text())
                self.assertNotIn("isolated-test-token", result.stdout + result.stderr)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(10)


if __name__ == "__main__":
    unittest.main()
