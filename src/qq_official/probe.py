"""User-run, opt-in QQ contract probes. Never imported by application startup."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import tempfile
import time
from typing import Any, Awaitable, Callable

from .credentials import Credentials, verify_private_file
from .errors import QQAPIError
from .events import EventParseError, LIFECYCLE_EVENTS, MESSAGE_EVENTS, parse_dispatch
from .gateway import GatewayConnection
from .http import RestClient
from .token import TokenManager

CHECKS = {
    "reply": (1, "普通被动文字回复"),
    "non_at": (1, "用非 @ 群消息回复；只适用于群"),
    "proactive": (1, "主动发送，不使用回复窗口"),
    "markdown_table": (1, "Markdown 表格，需人工核对呈现"),
    "markdown_code": (1, "Markdown 代码块，需人工核对呈现"),
    "markdown_formula": (1, "Markdown 公式，需人工核对呈现"),
    "dedup": (2, "相同 msg_id + msg_seq 重复请求，会故意触发去重"),
    "length": (1, "2001 字有限长度探测，可能被拒绝"),
    "window_limit": (6, "回复窗口上限探测，最多 6 次，会故意触发限制"),
}
PENDING_CHECKS = ["image_upload", "pdf_upload", "zip_upload", "chunked_upload", "recall", "voice_send"]
_KNOWN_EVENTS = MESSAGE_EVENTS | LIFECYCLE_EVENTS | {"READY", "RESUMED", "INTERACTION_CREATE"}
_FIXED_CONTENT = {
    "reply": "QQ 官方接入测试：被动文字回复。",
    "non_at": "QQ 官方接入测试：普通非 @ 消息的被动回复。",
    "proactive": "QQ 官方接入测试：主动发送。",
    "markdown_table": "| 项目 | 状态 |\n| --- | --- |\n| 测试 | 请核对表格呈现 |",
    "markdown_code": "```python\nprint('QQ probe')\n```",
    "markdown_formula": "公式呈现测试：$x^2 + y^2 = 1$",
    "dedup": "QQ 官方接入测试：相同序号去重验证。",
    "length": "测" * 2001,
    "window_limit": "QQ 官方接入测试：回复窗口上限验证。",
}


@dataclass(frozen=True)
class ProbeOptions:
    report: Path
    app_id: str = field(repr=False)
    environment: str = "production"
    group: str | None = field(default=None, repr=False)
    user: str | None = field(default=None, repr=False)
    checks: tuple[str, ...] = ()
    duration: float = 120
    raw: Path | None = None

    def validate(self) -> None:
        if not 0 < self.duration <= 3600:
            raise ValueError("duration must be between 0 and 3600 seconds")
        if self.environment not in {"production", "sandbox", "test"}:
            raise ValueError("invalid probe environment")
        if any(check not in CHECKS for check in self.checks) or len(set(self.checks)) != len(self.checks):
            raise ValueError("unknown or duplicate probe check")
        if self.checks and not (self.group or self.user):
            raise ValueError("sending checks require an explicit target")
        if self.raw and self.raw.absolute() == self.report.absolute():
            raise ValueError("raw identity file and report must differ")

    @property
    def targets(self) -> tuple[tuple[str, str], ...]:
        return tuple((kind, target) for kind, target in (("group", self.group), ("c2c", self.user)) if target)


class ProtectedReport:
    """Reserve without following links; atomic 0600 replacements thereafter."""

    def __init__(self, path: Path):
        if os.name == "nt":
            raise PermissionError("Windows report ACL support is required")
        self.path = path
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            verify_private_file(fd)
        finally:
            os.close(fd)

    def write(self, data: dict[str, Any]) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=".qq-probe-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                verify_private_file(stream.fileno())
                json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


class Pseudonyms:
    def __init__(self):
        self.key = secrets.token_bytes(32)
        self.identities: dict[str, str] = {}

    def get(self, kind: str, value: str | None, *, namespace: str = "") -> str | None:
        if not value:
            return None
        digest = hmac.new(self.key, json.dumps([kind, namespace, value], separators=(",", ":")).encode(), hashlib.sha256).hexdigest()[:16]
        alias = f"{kind}_{digest}"
        self.identities[alias] = value
        return alias


def _now(clock: Callable[[], float]) -> str:
    return datetime.fromtimestamp(clock(), timezone.utc).isoformat()


def _receipt(value: dict[str, Any], aliases: Pseudonyms, namespace: str) -> dict[str, Any]:
    ext = value.get("ext_info") if isinstance(value.get("ext_info"), dict) else {}
    return {
        "message_id": aliases.get("message", value.get("id") if isinstance(value.get("id"), str) else None, namespace=namespace),
        "ref_idx": aliases.get("reference", ext.get("ref_idx") if isinstance(ext.get("ref_idx"), str) else None, namespace=namespace),
        "fields": sorted(key for key in value if key in {"id", "timestamp", "ext_info"}),
    }


def sanitized_sample(event, aliases: Pseudonyms, account: str) -> dict[str, Any]:
    """Allowlist a fixture shape; never copy opaque payload/unknown fields."""
    message = event.message
    if message is None:
        return {"op": 0, "t": event.event_type if event.event_type in _KNOWN_EVENTS else "OTHER", "s": event.seq, "d": {}}
    namespace = json.dumps([account, message.kind, message.endpoint_id])

    def author_sample(author):
        result = {}
        if author.native_id:
            result["member_openid" if message.kind == "group" else "user_openid"] = aliases.get("actor", author.native_id, namespace=namespace)
        if author.display_name:
            result["nickname"] = "测试发言者"
        if author.platform_role:
            result["member_role"] = author.platform_role
        if "bot" in author.raw:
            result["bot"] = author.is_bot
        return result

    def attachment_sample(attachment):
        result = {}
        for name in ("url", "voice_wav_url"):
            if name in attachment.raw:
                result[name] = "https://media.example.invalid/redacted"
        if "asr_refer_text" in attachment.raw:
            result["asr_refer_text"] = "已脱敏识别文本"
        if "filename" in attachment.raw:
            result["filename"] = "fixture.bin"
        if "size" in attachment.raw:
            result["size"] = 1024
        if "content_type" in attachment.raw:
            safe_mimes = {"image/png", "image/jpeg", "image/webp", "image/gif", "audio/wav", "audio/ogg", "audio/mpeg", "audio/silk", "video/mp4", "application/pdf", "application/zip", "application/octet-stream", "text/plain", "voice"}
            result["content_type"] = attachment.content_type if attachment.content_type in safe_mimes else "application/octet-stream"
        return result

    def element_sample(element):
        result = {}
        if element.msg_idx:
            result["msg_idx"] = aliases.get("reference", element.msg_idx, namespace=namespace)
        if "content" in element.raw:
            result["content"] = "已脱敏引用文本"
        if element.author:
            result["author"] = author_sample(element.author)
        if "attachments" in element.raw:
            result["attachments"] = [attachment_sample(a) for a in element.attachments]
        if "msg_elements" in element.raw:
            result["msg_elements"] = [element_sample(e) for e in element.elements]
        return result

    body: dict[str, Any] = {"author": author_sample(message.author)}
    if message.message_id:
        body["id"] = aliases.get("message", message.message_id, namespace=namespace)
    if message.kind == "group" and message.endpoint_id:
        body["group_openid"] = aliases.get("group", message.endpoint_id, namespace=account)
    if "content" in message.raw:
        body["content"] = "已脱敏消息文本"
    if "timestamp" in message.raw:
        body["timestamp"] = "2000-01-01T00:00:00Z"
    if message.message_type is not None:
        body["message_type"] = message.message_type if message.message_type in {0, 1, 2, 3, 7, 102, 103} else 0
    if "mentions" in message.raw:
        body["mentions"] = []
        for mention in message.mentions:
            item = {"is_you": mention["is_you"]} if isinstance(mention.get("is_you"), bool) else {}
            for key in ("id", "member_openid", "user_openid"):
                if isinstance(mention.get(key), str):
                    item[key] = aliases.get("actor", mention[key], namespace=namespace)
            body["mentions"].append(item)
    if "attachments" in message.raw:
        body["attachments"] = [attachment_sample(a) for a in message.attachments]
    if "msg_elements" in message.raw:
        body["msg_elements"] = [element_sample(e) for e in message.elements]
    scene = message.raw.get("message_scene")
    if isinstance(scene, dict):
        entries = []
        ext = scene.get("ext")
        if isinstance(ext, dict):
            entries = list(ext.items())
        elif isinstance(ext, list):
            for item in ext:
                if isinstance(item, str) and "=" in item:
                    entries.append(tuple(item.split("=", 1)))
                elif isinstance(item, dict):
                    entries.append((item.get("key"), item.get("value")))
        body["message_scene"] = {"ext": [f"{key.strip()}={aliases.get('reference', value.strip(), namespace=namespace)}" for key, value in entries if isinstance(key, str) and key.strip() in {"msg_idx", "ref_msg_idx"} and isinstance(value, str) and value.strip()]}
    if message.ark_data:
        body["ark_data"] = {key: "已脱敏卡片文本" for key in ("title", "source", "summary") if key in message.ark_data}
        if message.jump_url:
            body["ark_data"]["jump_url"] = "https://example.invalid/redacted"
    return {"op": 0, "t": event.event_type, "s": event.seq,
            "id": aliases.get("event", event.event_id, namespace=namespace), "d": body}


async def run_probe(
    options: ProbeOptions,
    rest: RestClient,
    gateway: GatewayConnection,
    *,
    confirm: Callable[[str], bool | Awaitable[bool]] = lambda _: False,
    output: Callable[[str], Any] = print,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Run one immutable, explicitly confirmed batch; never expand its targets.

    A report is reserved before networking. Each passive check consumes a distinct
    fresh event; ordinary messages cannot silently authorize a later probe batch.
    """
    options.validate()
    _silence_transport_logs()
    report_file = ProtectedReport(options.report)
    raw_file = ProtectedReport(options.raw) if options.raw else None
    aliases = Pseudonyms()
    account = aliases.get("account", options.app_id, namespace=options.environment)
    report: dict[str, Any] = {
        "schema": 1, "started_at": _now(clock), "account": account,
        "environment": options.environment,
        "transport_contract": "sandbox_address_requires_account_verification" if options.environment == "sandbox" else "configured_contract",
        "status": "preparing", "events": [], "samples": [], "checks": [],
        "pending": [{"check": name, "status": "not_implemented"} for name in PENDING_CHECKS],
        "self_echo": "not_observed", "dropped_events": 0,
        "notice": "Only this account, environment, endpoint and time are evidenced; accepted Markdown still requires visual inspection.",
    }
    target_names = {(kind, target): aliases.get(kind, target, namespace=str(account)) for kind, target in options.targets}
    report["targets"] = [{"kind": kind, "target": target_names[(kind, target)]} for kind, target in options.targets]
    report_file.write(report)
    if raw_file:
        raw_file.write({"purpose": "Protected local identity mapping only; do not commit or share.", "identities": aliases.identities})
    if options.checks:
        limit = sum(CHECKS[name][0] for name in options.checks) * len(options.targets)
        summary = f"探测目标：{', '.join(str(v) for v in target_names.values())}；最多 {limit} 次发送请求。\n" + "\n".join(f"- {name}: {CHECKS[name][1]}" for name in options.checks)
        output(summary)
        decision = confirm(summary)
        if hasattr(decision, "__await__"):
            decision = await decision
        if decision is not True:
            report["status"] = "cancelled"
            report_file.write(report)
            return report
    else:
        output("只监听，不发送消息；报告默认使用假名。")
    report["status"] = "listening"
    report_file.write(report)
    queues = {key: asyncio.Queue(maxsize=100) for key in options.targets}
    sent_ids: set[tuple[str, str, str]] = set()
    observed_ids: set[tuple[str, str, str]] = set()
    window_ids: set[tuple[str, str, str]] = set()
    dropped_events = 0
    uncertain_targets: set[tuple[str, str]] = set()
    reply_deadlines: dict[tuple[str, str, str], float] = {}
    sampled_shapes: set[tuple[str, int | None]] = set()

    async def on_dispatch(envelope: dict[str, Any]) -> None:
        nonlocal dropped_events
        if len(report["events"]) >= 1000:
            dropped_events += 1
            return
        try:
            event = parse_dispatch(envelope)
        except EventParseError:
            report["events"].append({"type": "invalid", "status": "quarantined"})
            return
        shape = (event.event_type, event.message.message_type if event.message else None)
        if event.message and shape not in sampled_shapes and len(sampled_shapes) < 20:
            sampled_shapes.add(shape)
            report["samples"].append(sanitized_sample(event, aliases, str(account)))
        event_type = event.event_type if event.event_type in _KNOWN_EVENTS else "OTHER"
        summary: dict[str, Any] = {"type": event_type, "observed_at": _now(clock)}
        message = event.message
        if message:
            namespace = json.dumps([account, message.kind, message.endpoint_id])
            summary.update({
                "kind": message.kind, "endpoint": aliases.get(message.kind, message.endpoint_id, namespace=str(account)),
                "actor": aliases.get("actor", message.author.native_id, namespace=namespace),
                "event_id": aliases.get("event", event.event_id, namespace=namespace),
                "message_id": aliases.get("message", message.message_id, namespace=namespace),
                "msg_idx": aliases.get("reference", message.msg_idx, namespace=namespace),
                "ref_msg_idx": aliases.get("reference", message.ref_msg_idx, namespace=namespace),
                "mentions_bot": message.mentions_bot,
                "attachment_fields": sorted({field for attachment in message.attachments for field in ("voice_wav_url", "asr_refer_text", "url", "content_type", "filename", "size") if field in attachment.raw}),
            })
            if message.message_id:
                observed_ids.add((message.kind, message.endpoint_id, message.message_id))
                if (message.kind, message.endpoint_id, message.message_id) in sent_ids:
                    report["self_echo"] = "observed_matching_receipt"
            key = (message.kind, message.endpoint_id)
            identity = (*key, message.message_id)
            if key in queues and message.message_id and identity not in window_ids:
                window_ids.add(identity)
                try:
                    queues[key].put_nowait(message)
                except asyncio.QueueFull:
                    dropped_events += 1
        if len(report["events"]) < 1000:
            report["events"].append(summary)
        else:
            dropped_events += 1

    async def send(kind: str, target: str, check: str, payload: dict[str, Any]) -> dict[str, Any]:
        item: dict[str, Any] = {"check": check, "kind": kind, "target": target_names[(kind, target)], "time": _now(clock), "stage": "send"}
        namespace = json.dumps([account, kind, target])
        expires_at = reply_deadlines.get((kind, target, payload.get("msg_id")))
        if expires_at is not None and clock() >= expires_at:
            item.update(status="not_run", reason="reply_window_expired_before_send")
            report["checks"].append(item)
            report_file.write(report)
            return item
        try:
            response = await rest.send_message(kind, target, payload)
            receipt = _receipt(response, aliases, namespace)
            item.update(status="accepted" if receipt["message_id"] else "ambiguous", receipt=receipt)
            if isinstance(response.get("id"), str):
                sent_ids.add((kind, target, response["id"]))
                if (kind, target, response["id"]) in observed_ids:
                    report["self_echo"] = "observed_matching_receipt"
            if check.startswith("markdown_") and receipt["message_id"]:
                item["rendering"] = "manual_verification_required"
        except QQAPIError as exc:
            item.update(status="rejected" if exc.accepted is False else "ambiguous", code=exc.code, http_status=exc.status, category=exc.category, stage=exc.stage,
                        request_id=aliases.get("request", exc.request_id, namespace=namespace))
        except (OSError, TimeoutError):
            item.update(status="ambiguous", category="transport_unknown")
        if item["status"] == "ambiguous":
            uncertain_targets.add((kind, target))
        report["checks"].append(item)
        report_file.write(report)
        output(f"{check} / {kind}: {item['status']}" + (f" (code={item['code']})" if item.get("code") is not None else ""))
        return item

    loop = asyncio.get_running_loop()
    deadline = loop.time() + options.duration
    gateway_task = asyncio.create_task(gateway.run(on_dispatch))
    try:
        if not options.checks:
            await asyncio.wait({gateway_task}, timeout=options.duration)
        for kind, target in options.targets:
            for check in options.checks:
                if (kind, target) in uncertain_targets:
                    report["checks"].append({"check": check, "kind": kind, "target": target_names[(kind, target)], "status": "not_run", "reason": "previous_send_ambiguous"})
                    continue
                if check == "non_at" and kind != "group":
                    report["checks"].append({"check": check, "kind": kind, "target": target_names[(kind, target)], "status": "not_applicable"})
                    continue
                payload: dict[str, Any] = {"msg_type": 2 if check.startswith("markdown_") else 0}
                if payload["msg_type"] == 2:
                    payload["markdown"] = {"content": _FIXED_CONTENT[check]}
                else:
                    payload["content"] = _FIXED_CONTENT[check]
                message = None
                if check != "proactive":
                    output(f"等待 {kind} 的新测试消息用于 {check}" + ("（请勿 @ 机器人）" if check == "non_at" else ""))
                    while loop.time() < deadline:
                        incoming = asyncio.create_task(queues[(kind, target)].get())
                        done, _ = await asyncio.wait({incoming, gateway_task}, timeout=max(0, deadline - loop.time()), return_when=asyncio.FIRST_COMPLETED)
                        if incoming not in done:
                            incoming.cancel()
                            await asyncio.gather(incoming, return_exceptions=True)
                            break
                        candidate = incoming.result()
                        if candidate.author.is_bot or (kind, target, candidate.message_id) in sent_ids:
                            continue
                        age = clock() - candidate.occurred_at if candidate.occurred_at is not None else None
                        if age is None or age < -5 or age >= (295 if kind == "group" else 3595):
                            continue
                        if kind == "group" and check == "non_at" and candidate.mentions_bot:
                            continue
                        if kind == "group" and check != "non_at" and not candidate.mentions_bot:
                            continue
                        message = candidate
                        break
                    if message is None:
                        report["checks"].append({"check": check, "kind": kind, "target": target_names[(kind, target)], "status": "not_run", "reason": "no_fresh_eligible_window"})
                        continue
                    payload.update(msg_id=message.message_id, msg_seq=1)
                    reply_deadlines[(kind, target, message.message_id)] = message.occurred_at + (295 if kind == "group" else 3595)
                result = await send(kind, target, check, payload)
                if result["status"] != "accepted":
                    continue  # No fallback or replacement seq after any uncertain result.
                if check == "dedup":
                    repeat = await send(kind, target, check, payload)
                    repeat["dedup_observation"] = "rejection_observed" if repeat["status"] == "rejected" else "unconfirmed"
                elif check == "window_limit":
                    for seq in range(2, 7 if kind == "group" else 6):
                        payload["msg_seq"] = seq
                        result = await send(kind, target, check, payload)
                        if result["status"] != "accepted":
                            break
        report["status"] = "completed" if not gateway_task.done() or gateway_task.exception() is None else "connection_failed"
    except asyncio.CancelledError:
        report["status"] = "interrupted"
        raise
    except Exception:
        report["status"] = "failed"
        raise
    finally:
        health = dict(gateway.health)
        if gateway_task.done() and health.get("reason") not in {None, "stopped", "not_started", "ready"}:
            report["status"] = "connection_failed"
        await gateway.stop()
        gateway_task.cancel()
        outcomes = await asyncio.gather(gateway_task, return_exceptions=True)
        if outcomes and isinstance(outcomes[0], Exception) and report["status"] == "completed":
            report["status"] = "connection_failed"
        report["ended_at"] = _now(clock)
        report["dropped_events"] = dropped_events
        report["gateway"] = {key: health[key] for key in ("state", "close_code", "message_gap", "reason", "durable_seq", "identify_remaining", "identify_max_concurrency", "rest_error") if key in health}
        report_file.write(report)
        if raw_file:
            raw_file.write({"purpose": "Protected local identity mapping only; do not commit or share.", "identities": aliases.identities})
    return report


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="QQ 官方机器人：受控测试工具，默认只监听。")
    parser.add_argument("--app-id-file", type=Path, required=True)
    parser.add_argument("--app-secret-file", type=Path, required=True)
    parser.add_argument("--group")
    parser.add_argument("--user")
    parser.add_argument("--sandbox", action="store_true")
    parser.add_argument("--report", type=Path, required=True, help="新建脱敏报告（文件必须不存在）")
    parser.add_argument("--raw", type=Path, help="可选：单独的本机身份映射文件（0600），不含正文或密钥")
    parser.add_argument("--checks", default="", help="逗号分隔：" + ",".join(CHECKS))
    parser.add_argument("--duration", type=float, default=120)
    parser.add_argument("--test-api-base", help=argparse.SUPPRESS)
    parser.add_argument("--confirm-batch", action="store_true", help="明确同意当前列出的目标与探测项，供受控非交互运行")
    return parser


def _silence_transport_logs() -> None:
    for name in ("httpx", "httpcore", "websockets", "aiohttp"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        logger.setLevel(logging.CRITICAL + 1)


async def async_main(argv: list[str] | None = None) -> int:
    args = argument_parser().parse_args(argv)
    # SDK HTTP logging may include target paths; this standalone CLI suppresses it.
    _silence_transport_logs()
    if args.test_api_base and args.sandbox:
        raise ValueError("test endpoint and sandbox cannot be combined")
    credentials = Credentials.from_files(args.app_id_file, args.app_secret_file)
    options = ProbeOptions(report=args.report, app_id=credentials.app_id, group=args.group, user=args.user,
                           checks=tuple(item for item in args.checks.split(",") if item), duration=args.duration,
                           raw=args.raw, environment="test" if args.test_api_base else "sandbox" if args.sandbox else "production")
    options.validate()
    base_url = "https://sandbox.api.sgroup.qq.com" if args.sandbox else "https://api.bot.qq.com"
    token = TokenManager(credentials, base_url=args.test_api_base or base_url, allow_test_server=bool(args.test_api_base))
    rest = RestClient(token, allow_test_server=bool(args.test_api_base))

    async def confirm(_: str) -> bool:
        if args.confirm_batch:
            return True
        answer = await asyncio.to_thread(input, "确认仅对以上测试目标执行这批探测？输入 YES：")
        return answer == "YES"

    try:
        report = await run_probe(options, rest, GatewayConnection(rest), confirm=confirm)
        return 0 if report["status"] in {"completed", "cancelled"} else 1
    finally:
        await rest.aclose()
        await token.aclose()


def main() -> int:
    try:
        return asyncio.run(async_main())
    except KeyboardInterrupt:
        print("探测已停止；已观察到的结果保存在受保护报告中。")
        return 130
    except Exception as exc:
        # Do not print exception text: third-party exceptions can carry headers/URLs.
        print(f"探测失败：{type(exc).__name__}。检查凭证文件权限、参数和报告状态。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
