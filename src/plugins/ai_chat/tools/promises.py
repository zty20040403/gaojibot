"""Promise follow-ups (/work/.gaoji/self/promises.md + container systemd timers)."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json
import re
import secrets
import shlex
import time

from ..ai_tools import ToolDefinition
from ..background_tools import workspace_owner
from ..handler_constants import SHANGHAI_TZ
from ..self_files import PromiseItem
from .registry import FunctionTool
from .state import ToolContext, TurnState, structured_tool_source

PROMISE_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": "promise",
        "description": (
            "记下对群友的承诺（如“等我查查”“明天帮你看”），并在容器 systemd 里定好回访定时器，"
            "到点后通过 `gaoji wake` 唤醒你回来兑现。也可查看未兑现承诺或标记已完成。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "list", "done"]},
                "what": {"type": "string", "maxLength": 200},
                "when": {
                    "type": "string",
                    "description": "到期时间：支持 10m / 2h / 1d / 明天 / YYYY-MM-DD HH:MM 或 ISO-8601（默认北京时间）",
                    "maxLength": 64,
                },
                "who": {"type": "string", "maxLength": 50},
                "id": {"type": "string", "maxLength": 24},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
}

_REL_RE = re.compile(r"^(\d+)\s*(s|sec|m|min|h|hour|hours|d|day|days|秒|分钟|分|小时|天)$", re.IGNORECASE)
_UNIT_RE = re.compile(r"^gaoji-promise-[a-z0-9]+$")


def enabled(context: ToolContext) -> bool:
    return "memory" in context.capabilities and getattr(context.app, "self_files", None) is not None


def _result(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def parse_when(raw: str, *, now: int | None = None) -> tuple[int, str]:
    base_ts = int(time.time()) if now is None else int(now)
    text = (raw or "").strip()
    if not text:
        due = base_ts + 3600
        return due, datetime.fromtimestamp(due, SHANGHAI_TZ).isoformat()

    rel = _REL_RE.match(text)
    if rel:
        amount = max(1, int(rel.group(1)))
        unit = rel.group(2).casefold()
        mult = 1 if unit in {"s", "sec", "秒"} else (
            60 if unit in {"m", "min", "分钟", "分"} else (
                3600 if unit in {"h", "hour", "hours", "小时"} else 86400
            )
        )
        due = min(base_ts + amount * mult, base_ts + 90 * 86400)
        return due, datetime.fromtimestamp(due, SHANGHAI_TZ).isoformat()

    base_dt = datetime.fromtimestamp(base_ts, SHANGHAI_TZ)
    if text in {"明天", "明日"}:
        dt = (base_dt + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        return int(dt.timestamp()), dt.isoformat()
    if text in {"今晚", "晚上"}:
        dt = base_dt.replace(hour=20, minute=0, second=0, microsecond=0)
        if int(dt.timestamp()) <= base_ts:
            dt = dt + timedelta(days=1)
        return int(dt.timestamp()), dt.isoformat()

    normalized = text.replace(" ", "T", 1) if len(text) >= 16 and text[10] == " " else text
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SHANGHAI_TZ)
    due = int(dt.timestamp())
    if due <= base_ts:
        due = base_ts + 60
        dt = datetime.fromtimestamp(due, SHANGHAI_TZ)
    return due, dt.astimezone(SHANGHAI_TZ).isoformat()


def build_promise_units(item: PromiseItem) -> tuple[str, str]:
    utc_str = datetime.fromtimestamp(item.due_at, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    wake_msg = f"回访（承诺 {item.promise_id}，对象：{item.who or '群友'}）：{item.what}"
    timer_text = (
        f"[Unit]\nDescription=gaoji promise timer {item.promise_id}\n\n"
        f"[Timer]\nOnCalendar={utc_str}\nAccuracySec=15s\nPersistent=true\n\n"
        f"[Install]\nWantedBy=timers.target\n"
    )
    cleanup_cmd = (
        f"rm -f /work/.gaoji/systemd/{item.unit_name}.timer /work/.gaoji/systemd/{item.unit_name}.service"
        f" && /run/current-system/sw/bin/gaoji units apply || true"
    )
    service_text = (
        f"[Unit]\nDescription=gaoji promise wake {item.promise_id}\n\n"
        f"[Service]\nType=oneshot\n"
        f"ExecStart=/run/current-system/sw/bin/gaoji wake {shlex.quote(wake_msg)}\n"
        f"ExecStartPost=/bin/sh -c {shlex.quote(cleanup_cmd)}\n"
    )
    return timer_text, service_text


async def _install_container_timer(sandbox_manager, owner: str, unit_name: str, timer_text: str, service_text: str) -> bool:
    if sandbox_manager is None or not _UNIT_RE.fullmatch(unit_name):
        return False
    try:
        sb = await sandbox_manager.ensure_default(owner)
        sid = str(sb["id"])
        if not await sandbox_manager._running(sid):
            await sandbox_manager.start(owner, sid)
        script = (
            "import base64, os, subprocess, sys\n"
            "from pathlib import Path\n"
            "d = Path('/work/.gaoji/systemd')\n"
            "d.mkdir(parents=True, exist_ok=True)\n"
            "u, t_b64, s_b64 = sys.argv[1:4]\n"
            "(d / (u + '.timer')).write_bytes(base64.b64decode(t_b64))\n"
            "(d / (u + '.service')).write_bytes(base64.b64decode(s_b64))\n"
            "r = subprocess.run(['/run/current-system/sw/bin/gaoji', 'units', 'apply'], capture_output=True, text=True)\n"
            "sys.exit(r.returncode)\n"
        )
        t_b64 = base64.b64encode(timer_text.encode("utf-8")).decode("ascii")
        s_b64 = base64.b64encode(service_text.encode("utf-8")).decode("ascii")
        code, _, _ = await sandbox_manager._guest_exec_raw(
            sid,
            "/run/current-system/sw/bin/python3",
            ["-c", script, unit_name, t_b64, s_b64],
            timeout=60,
        )
        return code == 0
    except Exception:
        return False


async def _remove_container_timer(sandbox_manager, owner: str, unit_name: str) -> None:
    if sandbox_manager is None or not unit_name or not _UNIT_RE.fullmatch(unit_name):
        return
    try:
        sandboxes = await sandbox_manager.list_owned(owner)
        sb = next((item for item in sandboxes if item.get("purpose") == "conversation"), None)
        if sb is None or not await sandbox_manager._running(str(sb["id"])):
            return
        cmd = f"rm -f /work/.gaoji/systemd/{unit_name}.timer /work/.gaoji/systemd/{unit_name}.service && gaoji units apply"
        await sandbox_manager._guest_exec_raw(str(sb["id"]), "/bin/sh", ["-c", cmd], timeout=30)
    except Exception:
        pass


async def promise(turn: TurnState, arguments: dict[str, object]) -> str:
    source = structured_tool_source(turn.context)
    if source is None:
        return _result({"ok": False, "error": "当前没有可操作的会话。"})
    store = getattr(turn.context.app, "self_files", None)
    if store is None:
        return _result({"ok": False, "error": "self 存储未启用。"})

    action = str(arguments.get("action") or "").strip()
    scope_key = source.scope.key
    items = store.read_promises(scope_key)
    inbound = turn.context.inbound
    owner = workspace_owner(inbound.endpoint)
    sandbox_manager = getattr(turn.context.app, "sandbox_manager", None)

    if action == "list":
        return _result({
            "ok": True,
            "promises": [
                {
                    "id": item.promise_id,
                    "due_iso": item.due_iso,
                    "who": item.who,
                    "what": item.what,
                    "done": item.done,
                }
                for item in items[-20:]
            ],
        })

    if action == "done":
        target_id = str(arguments.get("id") or "").strip()
        if not target_id:
            return _result({"ok": False, "error": "请提供承诺 id。"})
        updated: list[PromiseItem] = []
        matched: PromiseItem | None = None
        for item in items:
            if item.promise_id == target_id:
                matched = PromiseItem(**{**item.__dict__, "done": True})
                updated.append(matched)
            else:
                updated.append(item)
        if matched is None:
            return _result({"ok": False, "error": f"没找到承诺 {target_id}。"})
        rel, content = store.write_promises(scope_key, updated)
        await _remove_container_timer(sandbox_manager, owner, matched.unit_name)
        store.schedule_sync(sandbox_manager, owner, writes={rel: content}, commit_message=f"done promise {target_id}")
        return _result({"ok": True, "id": target_id, "done": True})

    if action == "create":
        what = " ".join(str(arguments.get("what") or "").split())[:200]
        if not what:
            return _result({"ok": False, "error": "承诺内容不能为空。"})
        try:
            due_ts, due_iso = parse_when(str(arguments.get("when") or ""))
        except ValueError:
            return _result({"ok": False, "error": "无法解析到期时间，请用如 10m、2h、1d 或 YYYY-MM-DD HH:MM。"})
        who = " ".join(str(arguments.get("who") or inbound.sender_display or "群友").split())[:50]
        promise_id = f"p{secrets.token_hex(3)}"
        unit_name = f"gaoji-promise-{promise_id}"
        item = PromiseItem(
            promise_id=promise_id,
            due_at=due_ts,
            due_iso=due_iso,
            who=who,
            what=what,
            done=False,
            unit_name=unit_name,
        )
        # Keep open items plus up to 30 most recent completed entries.
        open_items = [x for x in items if not x.done]
        done_items = [x for x in items if x.done][-30:]
        new_items = [*done_items, *open_items, item]
        rel, content = store.write_promises(scope_key, new_items)
        timer_text, service_text = build_promise_units(item)
        timer_ok = await _install_container_timer(sandbox_manager, owner, unit_name, timer_text, service_text)
        await store.sync_to_container(
            sandbox_manager,
            owner,
            writes={rel: content},
            commit_message=f"promise {promise_id}",
            ensure_started=False,
        )
        return _result({
            "ok": True,
            "id": promise_id,
            "due_iso": due_iso,
            "unit": unit_name,
            "timer_installed": timer_ok,
        })

    return _result({"ok": False, "error": "未知的 promise 操作。"})


HANDLERS = (
    FunctionTool(PROMISE_TOOL, enabled, promise, platform_neutral=True),
)
