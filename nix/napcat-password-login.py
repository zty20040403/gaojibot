"""Bounded password login through the local NapCat WebUI, never a CAPTCHA bypass."""
from __future__ import annotations

import argparse
import fcntl
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import socket
import sys
import tempfile
import time
import urllib.request


class LoginError(Exception):
    """Only fixed, credential-free messages may leave the API boundary."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, token: str, port: int):
        self.token = token
        self.port = port
        self.credential = ""
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(self, endpoint: str, payload: dict) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.credential:
            headers["Authorization"] = "Bearer " + self.credential
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/{endpoint}",
            json.dumps(payload).encode(), headers,
        )
        try:
            with self.http.open(req, timeout=10) as response:
                result = json.load(response)
        except (OSError, ValueError):
            raise LoginError("api_unavailable") from None
        if not isinstance(result, dict) or result.get("code") != 0:
            raise LoginError("api_rejected")
        data = result.get("data")
        if data is not None and not isinstance(data, dict):
            raise LoginError("invalid_response")
        return data or {}

    def authenticate(self) -> None:
        data = self.call("auth/login", {
            "hash": hashlib.sha256((self.token + ".napcat").encode()).hexdigest(),
        })
        if data.get("require2FA") or not isinstance(data.get("Credential"), str) or not data["Credential"]:
            raise LoginError("management_auth_required")
        self.credential = data["Credential"]

    def online_account(self) -> str:
        status = self.call("Debug/call", {"action": "get_status", "params": {}})
        info = self.call("Debug/call", {"action": "get_login_info", "params": {}})
        data = status.get("data") or {}
        account = info.get("data") or {}
        if (status.get("retcode") != 0 or info.get("retcode") != 0
                or not isinstance(data, dict) or not isinstance(account, dict)
                or data.get("online") is not True or data.get("good") is False
                or not account.get("user_id")):
            raise LoginError("online_unverified")
        return str(account["user_id"])


def private_open(path: Path, flags: int):
    fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        os.close(fd)
        raise LoginError("private_file_permissions_required")
    return fd


def read_private(path: Path) -> str:
    with os.fdopen(private_open(path, os.O_RDONLY)) as stream:
        text = stream.read(65537)
    if len(text) > 65536:
        raise LoginError("private_file_too_large")
    return text


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise LoginError("private_directory_permissions_required")


def save_state(path: Path, state: dict) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(state, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_state(path: Path, uin: str, port: int) -> dict:
    try:
        state = json.loads(read_private(path))
    except FileNotFoundError:
        return {"schema": 1, "uin": uin, "port": port, "attempts": [], "blocked": ""}
    if (not isinstance(state, dict) or state.get("schema") != 1
            or state.get("uin") != uin or state.get("port") != port
            or not isinstance(state.get("blocked"), str)
            or not isinstance(state.get("attempts"), list)
            or any(type(x) not in (int, float) or not math.isfinite(x) or x < 0
                   for x in state["attempts"])):
        raise LoginError("invalid_state_do_not_reset_automatically")
    return state


def challenge(data: dict) -> str:
    if data.get("needCaptcha"):
        return "captcha_required"
    if data.get("needNewDevice"):
        return "device_confirmation_required"
    return ""


def requires_human(login: dict) -> bool:
    error = str(login.get("loginError", "")).casefold()
    return any(word in error for word in (
        "验证码", "滑块", "新设备", "设备验证", "风控", "风险", "异常", "冻结", "封禁", "外挂",
        "captcha", "risk", "frozen", "banned", "new device", "verify",
    ))


def run_once(client, state: dict, password, save, now: float) -> str:
    def finish(status: str, *, blocked: str | None = None) -> str:
        state.update(status=status, checked_at=now)
        if blocked is not None:
            state["blocked"] = blocked
        if status == "online":
            state.pop("notification", None)
        save(state)
        return status

    client.authenticate()
    login = client.call("QQLogin/CheckLoginStatus", {})
    if login.get("isLogin") is True:
        if client.online_account() != state["uin"]:
            return finish("different_account_online", blocked="different_account_online")
        return finish("online", blocked="")
    if state["blocked"]:
        return finish("manual_required")
    if requires_human(login):
        return finish("security_confirmation_required", blocked="security_confirmation_required")
    if (login.get("isOffline") is True or login.get("qrLoginAccepted") is True
            or login.get("loginPhase") in {"initializing", "reconnecting", "qrcode_scanned"}):
        return finish("waiting_for_existing_session")
    if login.get("isLogin") is not False or login.get("isOffline") is not False:
        return finish("unknown_login_state")
    attempts = [stamp for stamp in state["attempts"] if now - stamp < 86400]
    if any(stamp > now for stamp in attempts):
        return finish("clock_changed")
    if len(attempts) >= 3 or (attempts and now - max(attempts) < 1800):
        return finish("rate_limited")
    secret = password()
    if not secret or len(secret) > 4096:
        raise LoginError("invalid_password_file")
    password_md5 = hashlib.md5(secret.encode()).hexdigest()
    del secret
    # Persist before submitting: a lost response or process crash must not replay login.
    state.update(attempts=[*attempts, now], blocked="attempt_unconfirmed")
    finish("attempting_password_login")
    try:
        result = client.call("QQLogin/PasswordLogin", {"uin": state["uin"], "passwordMd5": password_md5})
        reason = challenge(result)
        if reason:
            return finish(reason, blocked=reason)
        login = client.call("QQLogin/CheckLoginStatus", {})
        if login.get("isLogin") is True:
            if client.online_account() != state["uin"]:
                return finish("different_account_online", blocked="different_account_online")
            return finish("online", blocked="")
        if requires_human(login):
            return finish("security_confirmation_required", blocked="security_confirmation_required")
    except LoginError:
        return finish("login_not_verified", blocked="attempt_unconfirmed")
    return finish("login_submitted_not_verified", blocked="attempt_unconfirmed")


def notify_manual_required(state, client_factory, *, sender_uin, port, group_id, user_id, save, now):
    if not state.get("blocked"):
        return None
    reason = state["blocked"]
    incident = f"{reason}:{state['attempts'][-1] if state['attempts'] else 0}"
    destination = f"{port}:{sender_uin}:{group_id}:{user_id}"
    previous = state.get("notification", {})
    if not isinstance(previous, dict):
        raise LoginError("invalid_notification_state")
    if previous.get("incident") == incident and previous.get("destination") == destination:
        if previous.get("status") in {"sending", "sent", "unconfirmed"}:
            return None
        if previous.get("retry_at", 0) > now:
            return None
    notice = {"incident": incident, "destination": destination, "checked_at": now}

    def record(status):
        notice["status"] = status
        state["notification"] = notice
        save(state)
        return status

    try:
        client = client_factory()
        client.authenticate()
        if client.online_account() != sender_uin:
            raise LoginError("notification_sender_mismatch")
        member = client.call("Debug/call", {"action": "get_group_member_info", "params": {
            "group_id": int(group_id), "user_id": int(user_id), "no_cache": True,
        }})
        if (member.get("retcode") != 0 or not isinstance(member.get("data"), dict)
                or str(member["data"].get("user_id")) != user_id):
            raise LoginError("notification_recipient_unverified")
    except (LoginError, OSError, ValueError, TypeError):
        notice["retry_at"] = now + 300
        return record("waiting_for_sender")

    descriptions = {
        "device_confirmation_required": "高级 QQ 登录需要设备确认，自动登录已暂停，请在手机 QQ 上确认。",
        "captcha_required": "高级 QQ 登录需要验证码，自动登录已暂停，请打开受保护的 NapCat 登录页完成验证。",
        "security_confirmation_required": "高级 QQ 被安全验证拦住了，自动登录已暂停，请查看手机 QQ 安全提醒并手动处理。",
        "different_account_online": "高级的登录实例出现了其他 QQ 账号，自动登录已暂停，请检查，系统不会替你退出那个账号。",
        "attempt_unconfirmed": "高级 QQ 的登录结果未能确认，已暂停重试，请查看手机 QQ 或 NapCat 登录页。",
    }
    message = descriptions.get(reason, "高级 QQ 登录需要人工处理，自动登录已暂停，请检查手机 QQ 和 NapCat 登录页。")
    # A crash or an ambiguous send receipt must not produce a new @ every minute.
    record("sending")
    try:
        reply = client.call("Debug/call", {"action": "send_group_msg", "params": {
            "group_id": int(group_id), "message": [
                {"type": "at", "data": {"qq": user_id}},
                {"type": "text", "data": {"text": " " + message}},
            ],
        }})
        data = reply.get("data")
        if (reply.get("retcode") == 0 and isinstance(data, dict)
                and re.fullmatch(r"-?[0-9]{1,20}", str(data.get("message_id", "")))):
            notice["message_id"] = str(data["message_id"])
            return record("sent")
    except LoginError:
        pass
    return record("unconfirmed")


def configured_client(path: Path, port: int) -> Client:
    config = json.loads(path.read_text())
    if not isinstance(config, dict) or not isinstance(config.get("token"), str) or not config["token"]:
        raise LoginError("missing_webui_token")
    return Client(config["token"], port)


def webui_ready(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def init_password(path: Path) -> None:
    if not sys.stdin.isatty():
        raise LoginError("interactive_terminal_required")
    private_directory(path.parent)
    if path.exists() or path.is_symlink():
        raise LoginError("password_file_already_exists")
    password = getpass.getpass("QQ password (hidden): ")
    if not password or password != getpass.getpass("Repeat QQ password: "):
        raise LoginError("password_confirmation_failed")
    with os.fdopen(private_open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "w") as stream:
        stream.write(password)
        stream.flush()
        os.fsync(stream.fileno())
    print("Password saved privately. No login was attempted.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init-password", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--port", type=int, default=6100)
    parser.add_argument("--check-ready", action="store_true", help="Check local WebUI readiness without authentication or login")
    parser.add_argument("--uin")
    parser.add_argument("--password-file", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--rearm", action="store_true", help="Explicitly allow a new attempt; preserves rate limits")
    parser.add_argument("--notify-config", type=Path)
    parser.add_argument("--notify-port", type=int)
    parser.add_argument("--notify-uin")
    parser.add_argument("--notify-group")
    parser.add_argument("--notify-user")
    args = parser.parse_args()
    if args.check_ready:
        if not 1 <= args.port <= 65535:
            parser.error("Require a valid --port")
        raise SystemExit(0 if webui_ready(args.port) else 1)
    if args.init_password:
        init_password(args.init_password)
        return
    if (not args.config or not args.password_file or not args.state
            or not re.fullmatch(r"[1-9][0-9]{4,19}", args.uin or "") or not 1 <= args.port <= 65535):
        parser.error("Require --config, --password-file, --state, --uin and a valid --port")
    notification_args = [args.notify_config, args.notify_port, args.notify_uin, args.notify_group, args.notify_user]
    if any(value is not None for value in notification_args):
        if (not all(notification_args) or not 1 <= args.notify_port <= 65535
                or any(not re.fullmatch(r"[1-9][0-9]{4,19}", value or "")
                       for value in [args.notify_uin, args.notify_group, args.notify_user])
                or args.notify_uin == args.uin or args.notify_port == args.port):
            parser.error("Notification requires a separate sender, local port, group and user")
    private_directory(args.state.parent)
    with os.fdopen(private_open(args.state.with_suffix(".lock"), os.O_RDWR | os.O_CREAT), "r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("QQ password login: already_running")
            return
        state = load_state(args.state, args.uin, args.port)
        if args.rearm:
            state["blocked"] = ""
            save_state(args.state, state)
            print("QQ password login: rearmed; rate limits retained")
            return
        save = lambda value: save_state(args.state, value)
        try:
            result = run_once(configured_client(args.config, args.port), state,
                              lambda: read_private(args.password_file), save, time.time())
        finally:
            if args.notify_config:
                notification = notify_manual_required(
                    state, lambda: configured_client(args.notify_config, args.notify_port),
                    sender_uin=args.notify_uin, port=args.notify_port,
                    group_id=args.notify_group, user_id=args.notify_user, save=save, now=time.time(),
                )
                if notification:
                    print("QQ login notification: " + notification)
        print("QQ password login: " + result)


if __name__ == "__main__":
    try:
        main()
    except LoginError as exc:
        print("QQ password login: " + str(exc))
        raise SystemExit(1) from None
    except (OSError, ValueError, KeyError, TypeError, EOFError):
        # Neither raw API errors nor tracebacks may expose password/token/verification URLs.
        print("QQ password login: check private configuration, API availability or manual verification")
        raise SystemExit(1) from None
