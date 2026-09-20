"""Probe the QQ account, not just its process, with bounded transport recovery."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request
import urllib.error


def classify(login: dict, status: dict) -> str:
    if status.get("online") is True and status.get("good") is not False:
        return "online"
    error = str(login.get("loginError", "")).casefold()
    if any(word in error for word in ("失效", "重新登录", "kicked", "expired")):
        return "login_required"
    if not login.get("isLogin") and login.get("qrcodeurl") and not login.get("isOffline"):
        return "login_required"
    if status.get("online") is False or status.get("good") is False:
        return "transport_offline"
    return "unknown"


def decide(previous: dict, status: str, now: float) -> tuple[dict, bool]:
    recoverable = status in {"transport_offline", "unresponsive"}
    failures = int(previous.get("failures", 0)) + 1 if recoverable else 0
    attempts = [stamp for stamp in previous.get("restarts", []) if 0 <= now - stamp < 86400]
    last = previous.get("last_restart", 0)
    restart = recoverable and failures >= 3 and now - last >= 900 and len(attempts) < 3
    state = {"status": status, "checked_at": now, "failures": failures,
             "last_restart": last, "restarts": attempts}
    if restart:
        state.update(last_restart=now, failures=0, restarts=[*attempts, now])
    return state, restart


def probe(config_path: Path, port: int) -> str:
    config = json.loads(config_path.read_text())
    # Never follow redirects with the WebUI credential or inherit proxy settings.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(endpoint: str, payload: dict, credential: str = "") -> dict:
        headers = {"Content-Type": "application/json"}
        if credential:
            headers["Authorization"] = "Bearer " + credential
        request = urllib.request.Request(f"http://127.0.0.1:{port}/api/{endpoint}",
                                         json.dumps(payload).encode(), headers)
        with client.open(request, timeout=4) as response:
            result = json.load(response)
        if result.get("code") != 0:
            raise ValueError("NapCat API rejected request")
        return result.get("data") or {}

    auth = call("auth/login", {"hash": hashlib.sha256((config["token"] + ".napcat").encode()).hexdigest()})
    if auth.get("require2FA") or not auth.get("Credential"):
        return "management_auth_required"
    credential = auth["Credential"]
    login = call("QQLogin/CheckLoginStatus", {}, credential)
    result = call("Debug/call", {"action": "get_status", "params": {}}, credential)
    if result.get("retcode") != 0 or not isinstance(result.get("data"), dict):
        return "unknown"
    return classify(login, result["data"])


def atomic_write(path: Path, content: str, mode: int) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def metrics(state: dict) -> str:
    status = state["status"]
    return (
        "# HELP gaoji_qq_online QQ account online, separately from WebSocket connectivity.\n"
        "# TYPE gaoji_qq_online gauge\n"
        f"gaoji_qq_online {int(status == 'online')}\n"
        "# TYPE gaoji_qq_login_required gauge\n"
        f"gaoji_qq_login_required {int(status == 'login_required')}\n"
        "# TYPE gaoji_qq_health_checked_timestamp_seconds gauge\n"
        f"gaoji_qq_health_checked_timestamp_seconds {state['checked_at']}\n"
        "# TYPE gaoji_qq_health_status gauge\n"
        f'gaoji_qq_health_status{{status="{status}"}} 1\n'
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--unit", required=True)
    parser.add_argument("--systemctl", required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--metrics", type=Path)
    args = parser.parse_args()
    try:
        previous = json.loads(args.state.read_text())
    except FileNotFoundError:
        previous = {}
    # Corrupt recovery state must fail closed, never reset the restart budget.
    active = subprocess.run([args.systemctl, "is-active", "--quiet", args.unit], timeout=5).returncode == 0
    status = "stopped"
    if active:
        try:
            status = probe(args.config, args.port)
        except urllib.error.HTTPError as exc:
            status = "management_auth_required" if exc.code in (401, 403) else "unknown"
        except (OSError, TimeoutError):
            status = "unresponsive"
        except (ValueError, KeyError, TypeError):
            status = "unknown"
    state, restart = decide(previous, status, time.time())
    atomic_write(args.state, json.dumps(state) + "\n", 0o600)
    if args.metrics:
        atomic_write(args.metrics, metrics(state), 0o644)
    if previous.get("status") != status:
        print(f"QQ account health: {status}", flush=True)
    if restart:
        print("QQ transport recovery requested; cooldown 15 minutes, limit 3 per day.", flush=True)
        subprocess.run([args.systemctl, "try-restart", "--no-block", args.unit], check=True, timeout=5)


if __name__ == "__main__":
    main()
