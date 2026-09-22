"""Prepare an isolated native instance without resetting its WebUI identity."""
import argparse
import json
import os
from pathlib import Path
import secrets
import tempfile


def read_object(path):
    if not path.exists():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Invalid configuration")
    return value


def write_object(path, value):
    if path.exists() and read_object(path) == value:
        path.chmod(0o600)
        return
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def configure(directory, account, url, previous_url, host, port, token_file=None):
    onebot_path = directory / f"onebot11_{account}.json"
    webui_path = directory / "webui.json"
    onebot, webui = read_object(onebot_path), read_object(webui_path)
    network = onebot.setdefault("network", {})
    clients = network.get("websocketClients", [])
    targets = [c for c in clients if c.get("name") == "gaoji" or c.get("url") in {url, previous_url}]
    if len(targets) > 1 or any(c.get("enable") and c not in targets for c in clients):
        raise ValueError("Ambiguous reverse-WebSocket configuration")
    target = targets[0] if targets else {}
    token = target.get("token", "")
    if token_file is not None:
        token = token_file.read_text().strip()
        if len(token) < 32 or any(c.isspace() for c in token):
            raise ValueError("Invalid credential")
    target.update(name="gaoji", enable=True, url=url, token=token,
                  messagePostFormat="array", reportSelfMessage=False,
                  debug=False, reconnectInterval=5000, heartInterval=30000)
    if not targets:
        clients.append(target)
    network["websocketClients"] = clients
    webui.update(host=host, port=port)
    if not webui.get("token"):
        webui["token"] = secrets.token_urlsafe(32)
    write_object(onebot_path, onebot)
    write_object(webui_path, webui)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--previous-url", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token-file", type=Path)
    args = parser.parse_args()
    try:
        configure(args.directory, args.account, args.url, args.previous_url,
                  args.host, args.port, args.token_file)
    except Exception:
        raise SystemExit("Could not prepare native NapCat configuration; check private files and connection settings") from None
