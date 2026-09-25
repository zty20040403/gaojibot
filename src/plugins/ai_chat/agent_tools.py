from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import shlex
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.exception import ActionFailed
from src.bot_storage import DatabaseError

from .ai_tools import (
    BROWSER_CLICK_TOOL_NAME,
    BROWSER_CLEAR_TOOL_NAME,
    BROWSER_CLOSE_TOOL_NAME,
    BROWSER_NAVIGATE_TOOL_NAME,
    BROWSER_PRESS_KEY_TOOL_NAME,
    BROWSER_SCROLL_TOOL_NAME,
    BROWSER_SNAPSHOT_TOOL_NAME,
    BROWSER_TYPE_TOOL_NAME,
    BROWSER_WAIT_FOR_TOOL_NAME,
    GET_SHARED_CONTENT_TOOL_NAME,
    GET_MESSAGE_BY_ID_TOOL_NAME,
    IMPORT_FILE_TO_SANDBOX_TOOL_NAME,
    INSPECT_SHARED_CONTENT_TOOL_NAME,
    JOB_CANCEL_TOOL_NAME,
    JOB_STATUS_TOOL_NAME,
    LIST_RECENT_FILES_TOOL_NAME,
    NIX_SEARCH_TOOL_NAME,
    SANDBOX_CREATE_TOOL_NAME,
    SANDBOX_DESTROY_TOOL_NAME,
    SANDBOX_EXEC_TOOL_NAME,
    SANDBOX_LIST_TOOL_NAME,
    SANDBOX_READ_FILE_TOOL_NAME,
    SANDBOX_WRITE_FILE_TOOL_NAME,
    SAY_TOOL_NAME,
    SEARCH_MESSAGES_TOOL_NAME,
    SEND_FILE_FROM_SANDBOX_TOOL_NAME,
    SEND_IMAGE_FROM_SANDBOX_TOOL_NAME,
    VIEW_BILIBILI_TOOL_NAME,
    VIEW_FORWARD_TOOL_NAME,
)
from .browser_tools import BrowserManager, BrowserPolicyError, BrowserUnavailable
from .conversation_scope import ConversationScope
from .content_sources import ContentSourceError, ContentSourceStore
from .ledger import CanonicalMessage, MessageLedger
from .media_tools import BilibiliClient, BilibiliError
from .message_ir import ForwardNode, MediaNode, MessageBody, TextNode, render_fallback_text
from .onebot_codec import (
    decode_onebot_message,
    record_onebot_api_message,
    record_onebot_outgoing,
    render_api_attachments,
)
from .onebot_model_output import OneBotModelOutputResolver
from .onebot_availability import file_delivery_blocker
from .sandbox import DockerSandboxManager, SandboxError
from .vm_sandbox import VmSandboxManager
from .storage.jobs import DurableJobStore
from .turn_journal import TurnJournal
from .video_analysis import DeepVideoAnalysisError, DeepVideoAnalyzer
from .agent.execution import active_agent_step

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
DELIVERABLE_SUFFIXES = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".csv",
    ".zip",
    ".tar",
    ".gz",
    ".7z",
}
MESSAGE_HANDLE_PATTERN = re.compile(r"^msg#([1-9][0-9]*)$")
AGENT_TOOL_PROMPT = (
    "你可以使用当前群聊的历史消息、群文件和隔离 Docker 开发沙盒。"
    "遇到创建项目、修改群文件、安装依赖、构建、测试或打包任务时，"
    "必须实际调用工具完成，不要只给示例代码后声称已经做好。"
    "长任务开始和每个关键阶段都要用 say 发简短进度；"
    "有新的实际进展时可以持续汇报，但不要重复发送没有信息量的内容。"
    "先创建合适运行环境的沙盒，再写入或导入文件、执行构建和测试；"
    "沙盒基础环境提供 Python、Node、GCC、Git、SQLite 和中文 PDF；"
    "缺少 Go、Rust、Java、ffmpeg、LibreOffice、OCR 或科学计算库时，"
    "先用 nix_search 找属性名，再通过 sandbox_exec 的 packages 参数按需加载，"
    "不要使用 apt，也不要全局 pip install；"
    "需要交付时，用 send_file_from_sandbox 或 send_image_from_sandbox 发到当前群。"
    "生成含中文的 PDF 时必须使用沙盒内的 gaoji-pdf input.md output.pdf，"
    "再用 pdffonts 确认字体已嵌入、pdftotext 确认中文可提取；"
    "禁止用 Helvetica 等默认西文字体直接生成中文 PDF。"
    "沙盒创建、写入、依赖安装、构建、测试、打包和发文件在现有权限内自动执行，无需手机审批。"
    "用户要求删除或重建时可直接使用 sandbox_destroy，包含未交付文件也不再二次确认。"
    "正常任务收尾仍交给宿主，不能在下游使用或文件交付前自行删除沙盒；任务结束后宿主会停止容器。"
    "任务成功且产物确认送达后，工作区和本地副本最多保留一小时再自动回收，不能承诺永久保留。"
    "尚未回收的沙盒可用 sandbox_list 查找，sandbox_exec 会自动启动已停止的沙盒。"
    "需要交付的文件会先形成宿主持有的不可变快照，再独立发送到 QQ；"
    "上传结果未确认时，快照和工作区都必须保留。"
    "只有工具结果明确成功时才能说任务已完成。"
    "沙盒是临时开发环境，不等于公网部署；需要云平台账号或密钥时，"
    "先完成可运行项目和打包，再说明仍需用户提供外部部署条件。"
    "不要尝试访问宿主机、机器人密钥、其他用户沙盒或其他群的数据。"
    "工具参数里的 message_handle 一律完整照抄上下文或搜索结果中的 msg# 句柄，"
    "不要猜测 OneBot 原始消息 ID、群号或 QQ 号。"
    "用户询问 QQ 分享卡片、帖子、视频或网页内容时，优先调用 "
    "inspect_shared_content，并完整照抄上下文里的 source#、msg# 或链接；"
    "不要先用普通浏览器重复打开同一个分享。用户明确说仔细看视频、分析画面、"
    "听音轨或逐段总结时，把 mode 设为 deep；普通询问使用 quick。"
)

VM_AGENT_TOOL_PROMPT = (
    "你可以使用独立 KVM 虚拟机沙盒处理代码和文件。工作目录是 /workspace；"
    "每个沙盒有独立磁盘，不挂载宿主机目录，重启后文件仍在。"
    "先创建沙盒，再实际写文件、执行命令、验收产物，最后用发送文件工具交付。"
    "客体是 Debian；基础环境有 Python、Git、curl、zip。"
    "缺少依赖时把 Debian 软件包名放进 sandbox_exec.packages，宿主会在该虚拟机内安装；"
    "不确定包名可先在沙盒里执行 apt-cache search。"
    "生成中文 PDF 时安装中文字体和 PDF 工具，并用 pdffonts、pdftotext 验收。"
    "长任务每个关键阶段用 say 简短汇报；只有发送工具确认成功才能说文件已交付。"
    "不要尝试访问宿主机或其他用户沙盒；不要在交付前销毁沙盒。"
)


def _json_result(*, ok: bool, **data: Any) -> str:
    return json.dumps({"ok": ok, **data}, ensure_ascii=False)


def _parse_message_handle(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    matched = MESSAGE_HANDLE_PATTERN.fullmatch(value.strip())
    return int(matched.group(1)) if matched is not None else None


def _message_segments(raw_message: Any) -> list[dict[str, Any]]:
    if isinstance(raw_message, Message):
        return [
            {"type": segment.type, "data": dict(segment.data)}
            for segment in raw_message
        ]
    if isinstance(raw_message, list):
        return [
            item
            for item in raw_message
            if isinstance(item, dict) and isinstance(item.get("type"), str)
        ]
    if isinstance(raw_message, str):
        try:
            return [
                {"type": segment.type, "data": dict(segment.data)}
                for segment in Message(raw_message)
            ]
        except Exception:
            return [{"type": "text", "data": {"text": raw_message}}]
    return []


def _message_attachments(raw_message: Any) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    for segment in _message_segments(raw_message):
        segment_type = segment.get("type", "")
        if segment_type not in {"file", "onlinefile"}:
            continue
        data = segment.get("data")
        if not isinstance(data, dict):
            continue
        attachments.append(
            {
                "file_id": str(data.get("file_id") or ""),
                "file_name": str(
                    data.get("name")
                    or data.get("fileName")
                    or data.get("file")
                    or ""
                ),
                "file_size": int(
                    data.get("file_size") or data.get("fileSize") or 0
                ),
                "type": segment_type,
                "_download_url": str(data.get("url") or ""),
            }
        )
    return attachments


def _safe_file_info(raw_file: dict[str, Any]) -> dict[str, Any]:
    return {
        "_native_file_id": str(raw_file.get("file_id", "")),
        "_busid": int(raw_file.get("busid") or raw_file.get("bus_id") or 102),
        "file_name": str(raw_file.get("file_name", "")),
        "file_size": int(raw_file.get("file_size") or raw_file.get("size") or 0),
        "upload_time": int(raw_file.get("upload_time") or 0),
        "uploader": int(raw_file.get("uploader") or raw_file.get("uploader_id") or 0),
        "_download_url": str(raw_file.get("url") or ""),
    }


class AgentToolExecutor:
    def __init__(
        self,
        *,
        bot: Bot,
        event: GroupMessageEvent,
        owner: str,
        sandbox_manager: DockerSandboxManager | VmSandboxManager,
        max_file_bytes: int,
        ledger: MessageLedger | None = None,
        scope: ConversationScope | None = None,
        turn_journal: TurnJournal | None = None,
        turn_id: int | None = None,
        browser_manager: BrowserManager | None = None,
        source_store: ContentSourceStore | None = None,
        video_analyzer: DeepVideoAnalyzer | None = None,
        job_store: DurableJobStore | None = None,
    ) -> None:
        self.bot = bot
        self.event = event
        self._owner = owner
        self.sandbox_manager = sandbox_manager
        self.max_file_bytes = max(0, int(max_file_bytes))
        self.ledger = ledger
        self.scope = scope
        self.turn_journal = turn_journal
        self.turn_id = turn_id
        self.browser_manager = browser_manager
        self.source_store = source_store
        self.video_analyzer = video_analyzer
        self.job_store = job_store
        self._task_sandbox_ids: set[str] = set()
        self._pending_artifacts: dict[str, set[str]] = {}
        self.output_resolver = OneBotModelOutputResolver(
            bot,
            event,
            ledger,
            scope=scope,
        )

    @property
    def owner(self) -> str:
        step = active_agent_step.get()
        return f"{self._owner}:{step}" if step else self._owner

    @property
    def base_owner(self) -> str:
        return self._owner

    async def _activate_task_sandbox(self, sandbox_id: str) -> None:
        """Resume a retained workspace and include it in end-of-turn quiescing."""
        await self.sandbox_manager.start_owned(self.owner, sandbox_id)
        self._task_sandbox_ids.add(sandbox_id)

    @property
    def canonical_messages_enabled(self) -> bool:
        return self.ledger is not None and self.scope is not None

    async def ensure_canonical_message(
        self,
        native_message_id: str | int,
    ) -> int | None:
        if not self.canonical_messages_enabled:
            return None
        assert self.ledger is not None
        assert self.scope is not None
        existing = self.ledger.canonical_id_for_native(
            self.scope,
            native_message_id,
        )
        if existing is not None:
            return existing

        message = await self.bot.get_msg(message_id=int(native_message_id))
        if int(message.get("group_id") or 0) != self.event.group_id:
            return None
        stored = record_onebot_api_message(
            self.ledger,
            self.scope,
            message,
            bot_native_user_id=self.scope.bot_native_user_id,
        )
        if stored is None:
            return None
        visible = self.ledger.get_in_scope(
            self.scope,
            stored.canonical_message_id,
        )
        return visible.canonical_message_id if visible is not None else None

    async def execute(
        self,
        name: str,
        arguments: dict[str, object],
    ) -> str | None:
        handlers = {
            GET_MESSAGE_BY_ID_TOOL_NAME: self._get_message_by_id,
            SEARCH_MESSAGES_TOOL_NAME: self._search_messages,
            SANDBOX_CREATE_TOOL_NAME: self._sandbox_create,
            SANDBOX_DESTROY_TOOL_NAME: self._sandbox_destroy,
            SANDBOX_LIST_TOOL_NAME: self._sandbox_list,
            SANDBOX_EXEC_TOOL_NAME: self._sandbox_exec,
            NIX_SEARCH_TOOL_NAME: self._nix_search,
            SANDBOX_WRITE_FILE_TOOL_NAME: self._sandbox_write_file,
            SANDBOX_READ_FILE_TOOL_NAME: self._sandbox_read_file,
            SEND_FILE_FROM_SANDBOX_TOOL_NAME: self._send_file_from_sandbox,
            SEND_IMAGE_FROM_SANDBOX_TOOL_NAME: self._send_image_from_sandbox,
            LIST_RECENT_FILES_TOOL_NAME: self._list_recent_files,
            IMPORT_FILE_TO_SANDBOX_TOOL_NAME: self._import_file_to_sandbox,
            SAY_TOOL_NAME: self._say,
            VIEW_FORWARD_TOOL_NAME: self._view_forward,
            VIEW_BILIBILI_TOOL_NAME: self._view_bilibili,
            INSPECT_SHARED_CONTENT_TOOL_NAME: self._inspect_shared_content,
            GET_SHARED_CONTENT_TOOL_NAME: self._get_shared_content,
            BROWSER_NAVIGATE_TOOL_NAME: self._browser_navigate,
            BROWSER_SNAPSHOT_TOOL_NAME: self._browser_snapshot,
            BROWSER_CLICK_TOOL_NAME: self._browser_click,
            BROWSER_TYPE_TOOL_NAME: self._browser_type,
            BROWSER_PRESS_KEY_TOOL_NAME: self._browser_press_key,
            BROWSER_WAIT_FOR_TOOL_NAME: self._browser_wait_for,
            BROWSER_SCROLL_TOOL_NAME: self._browser_scroll,
            BROWSER_CLOSE_TOOL_NAME: self._browser_close,
            BROWSER_CLEAR_TOOL_NAME: self._browser_clear,
            JOB_STATUS_TOOL_NAME: self._job_status,
            JOB_CANCEL_TOOL_NAME: self._job_cancel,
        }
        handler = handlers.get(name)
        if handler is None:
            return None

        try:
            return await handler(arguments)
        except SandboxError as exc:
            return _json_result(ok=False, error=str(exc))
        except (
            BrowserUnavailable,
            BrowserPolicyError,
            BilibiliError,
            ContentSourceError,
            DeepVideoAnalysisError,
        ) as exc:
            return _json_result(ok=False, error=str(exc))
        except ActionFailed as exc:
            logger.warning(f"NapCat agent tool {name} failed: {exc}")
            return _json_result(ok=False, error="NapCat 执行这个操作失败。")
        except (
            httpx.HTTPError,
            OSError,
            RuntimeError,
            ValueError,
            sqlite3.Error,
            DatabaseError,
        ) as exc:
            logger.warning(f"Agent tool {name} failed: {exc}")
            return _json_result(ok=False, error="工具执行失败。")

    async def handoff_tool(
        self,
        name: str,
        arguments: dict[str, object],
        fingerprint: str,
    ) -> str | None:
        if name != SANDBOX_EXEC_TOOL_NAME:
            return _json_result(ok=False, error="这个工具不支持持久任务接管。")
        if self.job_store is None:
            return _json_result(ok=False, error="持久任务队列没有开启。")
        sandbox_id = str(arguments.get("sandbox_id") or "").strip()
        command = str(arguments.get("command") or "").strip()
        packages = self._package_arguments(arguments.get("packages"))
        if not sandbox_id or not command:
            return _json_result(ok=False, error="后台命令参数不完整。")
        owned = await self.sandbox_manager.list(self.owner)
        if not any(item.get("sandbox_id") == sandbox_id for item in owned):
            return _json_result(ok=False, error="当前会话没有这个沙盒。")
        timeout = min(max(int(arguments.get("timeout_seconds") or 300), 1), 300)
        owner_digest = hashlib.sha256(self.owner.encode("utf-8")).hexdigest()[:12]
        job, created = await asyncio.to_thread(
            self.job_store.enqueue,
            kind="agent.sandbox_exec",
            idempotency_key=f"agent.sandbox_exec:{owner_digest}:{fingerprint}",
            scope_key=self.owner,
            payload={
                "owner": self.owner,
                "sandbox_id": sandbox_id,
                "command": command,
                "packages": packages,
                "timeout_seconds": timeout,
            },
            max_attempts=3,
        )
        self._task_sandbox_ids.discard(sandbox_id)
        return _json_result(
            ok=True,
            accepted=True,
            created=created,
            job_handle=job.handle,
            status=job.status,
            sandbox_id=sandbox_id,
            message=(
                "任务已由持久队列接管，机器人重启后也会继续；"
                "未交付产物的沙盒会保留；成功任务确认交付后最多保留一小时。"
            ),
            delivery_semantics="at-least-once with idempotent enqueue",
        )

    async def compensate_tool(
        self,
        name: str,
        arguments: dict[str, object],
        reason: str,
    ) -> str | None:
        if name == SANDBOX_EXEC_TOOL_NAME:
            return _json_result(
                ok=True,
                action="cancel-process",
                reason=reason,
                message="沙盒执行器已收到取消信号并终止子进程。",
            )
        if name in {
            BROWSER_NAVIGATE_TOOL_NAME,
            BROWSER_CLICK_TOOL_NAME,
            BROWSER_TYPE_TOOL_NAME,
            BROWSER_PRESS_KEY_TOOL_NAME,
        } and self.browser_manager is not None:
            closed = await self.browser_manager.close_session(self.owner)
            return _json_result(
                ok=True,
                action="close-browser",
                closed=closed,
                reason=reason,
            )
        return None

    async def _job_status(self, arguments: dict[str, object]) -> str:
        if self.job_store is None:
            return _json_result(ok=False, error="持久任务队列没有开启。")
        job_id = self._parse_job_handle(arguments.get("job_handle"))
        if job_id is None:
            return _json_result(ok=False, error="job_handle 格式无效。")
        job = await asyncio.to_thread(self.job_store.get, job_id)
        if job is None or job.scope_key != self.owner:
            return _json_result(ok=False, error="当前会话看不到这个持久任务。")
        return _json_result(
            ok=True,
            job_handle=job.handle,
            kind=job.kind,
            status=job.status,
            attempts=job.attempts,
            result=job.result,
            last_error=job.last_error,
            updated_at=job.updated_at,
        )

    async def _job_cancel(self, arguments: dict[str, object]) -> str:
        if self.job_store is None:
            return _json_result(ok=False, error="持久任务队列没有开启。")
        job_id = self._parse_job_handle(arguments.get("job_handle"))
        if job_id is None:
            return _json_result(ok=False, error="job_handle 格式无效。")
        job = await asyncio.to_thread(self.job_store.get, job_id)
        if job is None or job.scope_key != self.owner:
            return _json_result(ok=False, error="当前会话看不到这个持久任务。")
        changed = await asyncio.to_thread(self.job_store.cancel, job_id)
        return _json_result(
            ok=changed,
            job_handle=job.handle,
            status="cancelled" if changed else job.status,
            message=("已请求取消持久任务。" if changed else "这个任务当前不能取消。"),
        )

    @staticmethod
    def _parse_job_handle(value: object) -> int | None:
        if not isinstance(value, str) or not value.startswith("job#"):
            return None
        try:
            job_id = int(value.removeprefix("job#"))
        except ValueError:
            return None
        return job_id if job_id > 0 else None

    async def _get_message_by_id(
        self,
        arguments: dict[str, object],
    ) -> str:
        message_id = _parse_message_handle(arguments.get("message_handle"))
        if message_id is None:
            return _json_result(ok=False, error="message_handle 格式无效。")

        if not self.canonical_messages_enabled:
            return _json_result(
                ok=False,
                error="规范消息账本不可用，已拒绝原生 ID 降级读取。",
            )
        assert self.ledger is not None
        assert self.scope is not None
        message = self.ledger.get_in_scope(self.scope, message_id)
        if message is None:
            return _json_result(
                ok=False,
                error="当前群可见上下文中找不到这条规范消息。",
            )
        return _json_result(
            ok=True,
            message=self._canonical_message_payload(message),
        )

    async def _search_messages(
        self,
        arguments: dict[str, object],
    ) -> str:
        query = str(arguments.get("query", "")).strip()
        limit = min(max(int(arguments.get("limit") or 10), 1), 20)
        if not query:
            return _json_result(ok=False, error="搜索关键词不能为空。")

        if not self.canonical_messages_enabled:
            return _json_result(
                ok=False,
                error="规范消息账本不可用，已拒绝原生 ID 降级搜索。",
            )
        assert self.ledger is not None
        assert self.scope is not None
        try:
            response = await self.bot.call_api(
                "get_group_msg_history",
                group_id=self.event.group_id,
                count=100,
                reverse_order=False,
            )
        except ActionFailed as exc:
            logger.warning(
                "NapCat history backfill failed; searching the local "
                f"message ledger instead: {exc}"
            )
        else:
            raw_messages = (
                response.get("messages", [])
                if isinstance(response, dict)
                else response
            )
            if isinstance(raw_messages, list):
                for raw in raw_messages:
                    if isinstance(raw, dict):
                        record_onebot_api_message(
                            self.ledger,
                            self.scope,
                            raw,
                            bot_native_user_id=self.scope.bot_native_user_id,
                        )
        matches = self.ledger.search_in_scope(
            self.scope,
            query,
            limit,
        )
        return _json_result(
            ok=True,
            query=query,
            messages=[
                self._canonical_message_payload(message, include_files=False)
                for message in matches
            ],
        )

    async def _view_forward(self, arguments: dict[str, object]) -> str:
        message_id = _parse_message_handle(arguments.get("message_handle"))
        if message_id is None:
            return _json_result(ok=False, error="message_handle 格式无效。")
        if not self.canonical_messages_enabled:
            return _json_result(ok=False, error="规范消息账本不可用。")
        assert self.ledger is not None
        assert self.scope is not None
        canonical = self.ledger.get_in_scope(self.scope, message_id)
        if canonical is None:
            return _json_result(ok=False, error="当前群看不到这条消息。")
        forwards = [
            node for node in canonical.body.nodes if isinstance(node, ForwardNode)
        ]
        if not forwards:
            return _json_result(ok=False, error="这条消息不是合并转发。")
        forward_id = forwards[0].native_id or canonical.native_message_id
        if not forward_id:
            return _json_result(ok=False, error="这条转发缺少平台读取句柄。")
        response = await self.bot.call_api("get_forward_msg", id=forward_id)
        if isinstance(response, dict):
            raw_children = response.get("messages") or response.get("message") or []
        else:
            raw_children = response
        if not isinstance(raw_children, list):
            return _json_result(ok=False, error="NapCat 没有返回转发子消息。")
        children: list[dict[str, object]] = []
        for raw in raw_children[:100]:
            if not isinstance(raw, dict):
                continue
            sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
            sender_native_id = str(
                sender.get("user_id") or raw.get("user_id") or ""
            )
            sender_principal_id = (
                self.ledger.principal_id_for_native(
                    "onebot-v11",
                    sender_native_id,
                )
                if sender_native_id
                else None
            )
            decoded = decode_onebot_message(
                raw.get("message") or raw.get("content") or []
            )
            children.append(
                {
                    "sender_display": str(
                        sender.get("card")
                        or sender.get("nickname")
                        or raw.get("nickname")
                        or "未知用户"
                    ),
                    "sender_handle": (
                        f"[mention#{sender_principal_id}]"
                        if sender_principal_id is not None
                        else None
                    ),
                    "time": int(raw.get("time") or 0),
                    "text": render_fallback_text(decoded.body)[:2000],
                    "has_nested_forward": any(
                        isinstance(node, ForwardNode)
                        for node in decoded.body.nodes
                    ),
                }
            )
        return _json_result(
            ok=True,
            message_handle=f"msg#{message_id}",
            children=children,
            truncated=len(raw_children) > len(children),
        )

    async def _view_bilibili(self, arguments: dict[str, object]) -> str:
        url = str(arguments.get("url") or "").strip()
        comments = min(max(int(arguments.get("comment_count") or 10), 0), 20)
        client = BilibiliClient()
        try:
            result = await client.inspect(url, comment_count=comments)
        finally:
            await client.close()
        return _json_result(ok=True, video=result)

    async def _inspect_shared_content(
        self,
        arguments: dict[str, object],
    ) -> str:
        if self.source_store is None or self.scope is None:
            return _json_result(ok=False, error="分享来源索引没有开启。")
        mode = str(arguments.get("mode") or "quick").strip().casefold()
        if mode not in {"quick", "deep"}:
            mode = "quick"
        source, cached = await self.source_store.inspect(
            self.scope,
            str(arguments.get("target") or ""),
            browser_fetch=(
                self._fetch_source_page
                if self.browser_manager is not None
                else None
            ),
            force_refresh=bool(arguments.get("force_refresh", False)),
            comment_count=min(
                max(int(arguments.get("comment_count") or 10), 0),
                20,
            ),
        )
        payload = source.as_tool_payload(cached=cached)
        if mode == "deep":
            if self.video_analyzer is None:
                return _json_result(ok=False, error="深度视频分析服务暂时没有开启。")

            async def report(text: str) -> None:
                await self._say({"text": text})

            analysis, analysis_cached = await self.video_analyzer.analyze(
                source,
                question=str(arguments.get("question") or "").strip(),
                force_refresh=bool(arguments.get("force_refresh", False)),
                progress=report,
            )
            tool_analysis = dict(analysis)
            tool_analysis["transcript"] = str(
                tool_analysis.get("transcript") or ""
            )[:5000]
            payload["body_text"] = str(payload.get("body_text") or "")[:1000]
            payload["comments"] = list(payload.get("comments") or [])[:5]
            payload["deep_analysis"] = tool_analysis
            payload["deep_cached"] = analysis_cached
        return _json_result(
            ok=True,
            mode=mode,
            source=payload,
        )

    async def _get_shared_content(
        self,
        arguments: dict[str, object],
    ) -> str:
        if self.source_store is None or self.scope is None:
            return _json_result(ok=False, error="分享来源索引没有开启。")
        source = self.source_store.get_cached(
            self.scope,
            str(arguments.get("source_handle") or ""),
        )
        return _json_result(
            ok=True,
            source=source.as_tool_payload(cached=True),
        )

    async def _fetch_source_page(self, url: str) -> dict[str, object]:
        browser = self._require_browser()
        page = await browser.navigate(self.owner, url)
        if len(str(page.get("text") or "").strip()) < 300:
            await asyncio.sleep(1.0)
            page = await browser.snapshot(self.owner)
        return page

    def _require_browser(self) -> BrowserManager:
        if self.browser_manager is None:
            raise BrowserUnavailable("持久浏览器没有开启。")
        return self.browser_manager

    async def _browser_navigate(self, arguments: dict[str, object]) -> str:
        result = await self._require_browser().navigate(
            self.owner, str(arguments.get("url") or "")
        )
        return _json_result(ok=True, page=result)

    async def _browser_snapshot(self, arguments: dict[str, object]) -> str:
        del arguments
        result = await self._require_browser().snapshot(self.owner)
        return _json_result(ok=True, page=result)

    async def _browser_click(self, arguments: dict[str, object]) -> str:
        result = await self._require_browser().click(
            self.owner, str(arguments.get("ref") or "")
        )
        return _json_result(ok=True, page=result)

    async def _browser_type(self, arguments: dict[str, object]) -> str:
        result = await self._require_browser().type_text(
            self.owner,
            str(arguments.get("ref") or ""),
            str(arguments.get("text") or ""),
            submit=bool(arguments.get("submit", False)),
        )
        return _json_result(ok=True, page=result)

    async def _browser_press_key(self, arguments: dict[str, object]) -> str:
        result = await self._require_browser().press_key(
            self.owner, str(arguments.get("key") or "")
        )
        return _json_result(ok=True, page=result)

    async def _browser_wait_for(self, arguments: dict[str, object]) -> str:
        result = await self._require_browser().wait_for(
            self.owner,
            str(arguments.get("text") or ""),
            int(arguments.get("timeout_seconds") or 15),
        )
        return _json_result(ok=True, page=result)

    async def _browser_scroll(self, arguments: dict[str, object]) -> str:
        result = await self._require_browser().scroll(
            self.owner, int(arguments.get("amount") or 700)
        )
        return _json_result(ok=True, page=result)

    async def _browser_close(self, arguments: dict[str, object]) -> str:
        del arguments
        closed = await self._require_browser().close_session(self.owner)
        return _json_result(ok=True, closed=closed)

    async def _browser_clear(self, arguments: dict[str, object]) -> str:
        del arguments
        cleared = await self._require_browser().clear_profile(self.owner)
        return _json_result(ok=True, cleared=cleared)

    async def _sandbox_create(
        self,
        arguments: dict[str, object],
    ) -> str:
        runtime = str(arguments.get("runtime", "python"))
        sandbox = await self.sandbox_manager.create(self.owner, runtime)
        sandbox_id = str(sandbox.get("sandbox_id") or "").strip()
        if sandbox_id:
            self._task_sandbox_ids.add(sandbox_id)
        return _json_result(ok=True, sandbox=sandbox)

    async def _sandbox_list(
        self,
        arguments: dict[str, object],
    ) -> str:
        del arguments
        sandboxes = await self.sandbox_manager.list(self.owner)
        return _json_result(ok=True, sandboxes=sandboxes)

    async def _sandbox_destroy(self, arguments: dict[str, object]) -> str:
        sandbox_id = str(arguments.get("sandbox_id") or "")
        await self.sandbox_manager.destroy(self.owner, sandbox_id)
        self._task_sandbox_ids.discard(sandbox_id)
        self._pending_artifacts.pop(sandbox_id, None)
        return _json_result(ok=True, sandbox_id=sandbox_id, state="destroyed")

    async def retain_task_sandboxes(self) -> dict[str, tuple[str, ...]]:
        """Stop this turn's containers while preserving their workspaces."""
        stopped: list[str] = []
        failed: list[str] = []
        retained: list[str] = []
        for sandbox_id in sorted(self._task_sandbox_ids):
            try:
                await self.sandbox_manager.stop_owned(self.owner, sandbox_id)
            except Exception as exc:
                logger.warning(
                    f"Could not stop retained sandbox {sandbox_id}: {exc}"
                )
                failed.append(sandbox_id)
            else:
                stopped.append(sandbox_id)
            self._task_sandbox_ids.discard(sandbox_id)
            retained.append(sandbox_id)
        return {
            "stopped": tuple(stopped),
            "failed": tuple(failed),
            "retained": tuple(retained),
        }

    async def _sandbox_exec(
        self,
        arguments: dict[str, object],
    ) -> str:
        sandbox_id = str(arguments.get("sandbox_id", ""))
        command = str(arguments.get("command", ""))
        raw_timeout = arguments.get("timeout_seconds")
        timeout = int(raw_timeout) if raw_timeout is not None else None
        packages = self._package_arguments(arguments.get("packages"))
        await self._activate_task_sandbox(sandbox_id)
        result = await self.sandbox_manager.exec(
            self.owner,
            sandbox_id,
            command,
            timeout,
            packages=packages,
        )
        if result.manifest is not None:
            for path in result.manifest.changed_workspace_paths:
                self._track_pending_artifact(sandbox_id, path)
        return _json_result(
            ok=result.returncode == 0,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            observed_manifest=(
                asdict(result.manifest)
                if result.manifest is not None
                else None
            ),
        )

    async def _nix_search(self, arguments: dict[str, object]) -> str:
        query = str(arguments.get("query") or "")
        matches = await self.sandbox_manager.search_nix_packages(query)
        return _json_result(ok=True, packages=matches)

    @staticmethod
    def _package_arguments(value: object) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise SandboxError("packages 必须是 Nix 属性名数组。")
        return [str(item) for item in value]

    async def _sandbox_write_file(
        self,
        arguments: dict[str, object],
    ) -> str:
        sandbox_id = str(arguments.get("sandbox_id", ""))
        path = str(arguments.get("path", ""))
        content = str(arguments.get("content", "")).encode("utf-8")
        await self._activate_task_sandbox(sandbox_id)
        size = await self.sandbox_manager.write_file(
            self.owner,
            sandbox_id,
            path,
            content,
        )
        self._track_pending_artifact(sandbox_id, path)
        return _json_result(ok=True, path=path, bytes_written=size)

    async def _sandbox_read_file(
        self,
        arguments: dict[str, object],
    ) -> str:
        sandbox_id = str(arguments.get("sandbox_id", ""))
        path = str(arguments.get("path", ""))
        await self._activate_task_sandbox(sandbox_id)
        content = await self.sandbox_manager.read_file(
            self.owner,
            sandbox_id,
            path,
            max_bytes=64 * 1024,
        )
        return _json_result(
            ok=True,
            path=path,
            content=content.decode("utf-8", errors="replace"),
        )

    async def _send_file_from_sandbox(
        self,
        arguments: dict[str, object],
    ) -> str:
        sandbox_id = str(arguments.get("sandbox_id", ""))
        path = str(arguments.get("path", ""))
        requested_name = str(arguments.get("filename", "")).strip()
        filename = self._safe_filename(
            requested_name or PurePosixPath(path).name
        )
        await self._activate_task_sandbox(sandbox_id)
        content = await self.sandbox_manager.read_file(
            self.owner,
            sandbox_id,
            path,
            max_bytes=self.max_file_bytes or None,
        )
        pdf_validation: dict[str, object] | None = None
        if content.startswith(b"%PDF-"):
            quoted_path = shlex.quote(path)
            font_result = await self.sandbox_manager.exec(
                self.owner,
                sandbox_id,
                f"pdffonts {quoted_path}",
                30,
            )
            if font_result.returncode != 0:
                return _json_result(
                    ok=False,
                    error="PDF 结构校验失败，文件未发送。请重新生成后再试。",
                    details=font_result.stderr.strip(),
                )

            text_result = await self.sandbox_manager.exec(
                self.owner,
                sandbox_id,
                f"pdftotext {quoted_path} -",
                30,
            )
            if text_result.returncode != 0:
                return _json_result(
                    ok=False,
                    error="PDF 文字校验失败，文件未发送。请重新生成后再试。",
                    details=text_result.stderr.strip(),
                )

            extracted_text = text_result.stdout.strip()
            embedded_font = any(
                re.search(
                    r"\s+yes\s+(?:yes|no)\s+(?:yes|no)\s+\d+\s+\d+\s*$",
                    line.lower(),
                )
                for line in font_result.stdout.splitlines()
            )
            if extracted_text and not embedded_font:
                return _json_result(
                    ok=False,
                    error=(
                        "PDF 含文字但字体没有嵌入，中文可能显示为方框，文件未发送。"
                        "请使用 gaoji-pdf 重新生成。"
                    ),
                )
            pdf_validation = {
                "checked": True,
                "embedded_font": embedded_font,
                "extractable_text": bool(extracted_text),
            }
        return await self.send_file_content(
            content,
            filename,
            source_sandbox_id=sandbox_id,
            source_path=path,
            pdf_validation=pdf_validation,
        )

    async def file_delivery_blocker(self) -> str | None:
        """A connected adapter can still belong to a logged-out QQ account."""
        return await file_delivery_blocker(self.bot)

    async def send_file_content(
        self,
        content: bytes,
        filename: str,
        *,
        source_sandbox_id: str = "",
        source_path: str = "",
        pdf_validation: dict[str, object] | None = None,
    ) -> str:
        """Upload trusted bytes independently from the producing container."""
        if not content:
            return _json_result(ok=False, not_sent=True, retryable=False, error="交付文件为空，未发送。")
        if self.max_file_bytes and len(content) > self.max_file_bytes:
            return _json_result(
                ok=False,
                not_sent=True,
                retryable=False,
                error=(
                    f"交付文件大小 {len(content)} 字节，超过发送上限 "
                    f"{self.max_file_bytes} 字节。"
                ),
            )
        blocker = await self.file_delivery_blocker()
        if blocker is not None:
            return _json_result(ok=False, not_sent=True, retryable=True, error=blocker,
                                state="not_sent", blocked_reason="transport_unavailable")
        safe_filename = self._safe_filename(filename)
        upload_started_at = int(time.time())
        response = await self.bot.call_api(
            "upload_group_file",
            group_id=self.event.group_id,
            file="base64://" + base64.b64encode(content).decode("ascii"),
            name=safe_filename,
        )
        accepted = bool(response is not False)
        receipt = (
            await self.confirm_group_file(
                safe_filename, len(content), not_before=upload_started_at
            )
            if accepted
            else {"ok": False, "reconciled": False}
        )
        delivered = bool(receipt.get("ok"))
        if delivered and source_sandbox_id:
            self._mark_artifact_delivered(source_sandbox_id, source_path)
            await self._retain_delivered_task_sandbox(source_sandbox_id)
        return _json_result(
            ok=delivered,
            filename=safe_filename,
            size=len(content),
            upload_started_at=upload_started_at,
            uploaded=accepted,
            not_sent=not accepted,
            retryable=not accepted,
            state="acknowledged" if delivered else "unknown",
            receipt=receipt,
            error=(
                "QQ 已受理上传，但群文件列表尚未确认；产物和沙盒已保留，可稍后核对或重试。"
                if accepted and not delivered
                else "QQ 拒绝了文件上传。" if not accepted else ""
            ),
            pdf_validation=pdf_validation,
        )

    async def confirm_group_file(
        self,
        filename: str,
        size: int,
        *,
        attempts: int = 6,
        not_before: int | None = None,
    ) -> dict[str, object]:
        """Confirm that QQ committed an uploaded file before retention starts."""
        expected_uploader = str(
            getattr(
                self.bot,
                "self_id",
                self.scope.bot_native_user_id if self.scope is not None else "",
            )
        )
        last_error = ""
        for attempt in range(min(max(int(attempts), 1), 10)):
            if attempt:
                await asyncio.sleep(min(0.5 * (2 ** (attempt - 1)), 3.0))
            try:
                response = await self.bot.call_api(
                    "get_group_root_files", group_id=self.event.group_id
                )
            except Exception as exc:
                last_error = type(exc).__name__
                continue
            files = response.get("files", []) if isinstance(response, dict) else []
            match = next(
                (
                    item
                    for item in files
                    if item.get("file_name") == filename
                    and int(item.get("file_size", -1)) == int(size)
                    and (
                        not_before is None
                        or not item.get("upload_time")
                        or int(item.get("upload_time") or 0) >= not_before - 5
                    )
                    and (
                        not expected_uploader
                        or str(item.get("uploader")) == expected_uploader
                    )
                ),
                None,
            )
            if match is not None:
                return {
                    "ok": True,
                    "reconciled": True,
                    "file_id": match.get("file_id"),
                    "attempts": attempt + 1,
                }
        return {
            "ok": False,
            "reconciled": False,
            "attempts": min(max(int(attempts), 1), 10),
            "error": last_error,
        }

    @staticmethod
    def _workspace_artifact_path(path: str) -> str:
        normalized = str(PurePosixPath(path.strip()))
        if normalized == "/workspace":
            return ""
        if normalized.startswith("/workspace/"):
            return normalized.removeprefix("/workspace/")
        return normalized.lstrip("/")

    def _track_pending_artifact(self, sandbox_id: str, path: str) -> None:
        relative = self._workspace_artifact_path(path)
        if not relative or PurePosixPath(relative).suffix.lower() not in DELIVERABLE_SUFFIXES:
            return
        self._pending_artifacts.setdefault(sandbox_id, set()).add(relative)

    def _mark_artifact_delivered(self, sandbox_id: str, path: str) -> None:
        relative = self._workspace_artifact_path(path)
        pending = self._pending_artifacts.get(sandbox_id)
        if pending is None:
            return
        pending.discard(relative)
        if not pending:
            self._pending_artifacts.pop(sandbox_id, None)

    async def _retain_delivered_task_sandbox(self, sandbox_id: str) -> None:
        sandboxes = await self.sandbox_manager.list(self.owner)
        if any(
            str(item.get("sandbox_id") or "") == sandbox_id
            and str(item.get("purpose") or "task") == "task"
            for item in sandboxes
        ):
            self._task_sandbox_ids.add(sandbox_id)

    async def _send_image_from_sandbox(
        self,
        arguments: dict[str, object],
    ) -> str:
        sandbox_id = str(arguments.get("sandbox_id", ""))
        path = str(arguments.get("path", ""))
        if PurePosixPath(path).suffix.lower() not in IMAGE_SUFFIXES:
            return _json_result(ok=False, error="只允许发送常见图片格式。")
        await self._activate_task_sandbox(sandbox_id)
        content = await self.sandbox_manager.read_file(
            self.owner,
            sandbox_id,
            path,
            max_bytes=(
                min(self.max_file_bytes, 10 * 1024 * 1024)
                if self.max_file_bytes
                else 10 * 1024 * 1024
            ),
        )
        response = await self.bot.send_group_msg(
            group_id=self.event.group_id,
            message=MessageSegment.image(content),
        )
        canonical_message_id = None
        if self.canonical_messages_enabled and isinstance(response, dict):
            native_message_id = response.get("message_id")
            if native_message_id:
                assert self.ledger is not None
                assert self.scope is not None
                stored = self.ledger.record_message(
                    self.scope,
                    native_message_id=str(native_message_id),
                    sender_native_user_id=self.scope.bot_native_user_id,
                    sender_display="机器人",
                    body=MessageBody(
                        (
                            MediaNode(
                                0,
                                "image",
                                source=f"sandbox:{path}",
                                name=PurePosixPath(path).name,
                                source_type="image",
                            ),
                        )
                    ),
                    occurred_at=int(time.time()),
                    direction="outbound",
                )
                canonical_message_id = stored.canonical_message_id
                self._link_turn_send(canonical_message_id, "send_image")
        return _json_result(
            ok=True,
            size=len(content),
            message_handle=(
                f"msg#{canonical_message_id}"
                if canonical_message_id is not None
                else None
            ),
        )

    async def _list_recent_files(
        self,
        arguments: dict[str, object],
    ) -> str:
        limit = min(max(int(arguments.get("limit") or 20), 1), 50)
        files = await self._group_files(limit)
        return _json_result(
            ok=True,
            files=[self._public_group_file(item) for item in files[:limit]],
        )

    async def _import_file_to_sandbox(
        self,
        arguments: dict[str, object],
    ) -> str:
        sandbox_id = str(arguments.get("sandbox_id", ""))
        file_handle = str(arguments.get("file_handle", "")).strip()
        attachment_handle = str(arguments.get("attachment_handle", "")).strip()
        raw_message_handle = arguments.get("message_handle")
        message_id = _parse_message_handle(raw_message_handle)
        destination = str(arguments.get("destination", "")).strip()
        matched_file: dict[str, Any] | None = None
        native_file_id = ""
        canonical_download_url = ""

        if raw_message_handle is not None and message_id is None:
            return _json_result(
                ok=False,
                error="message_handle 格式无效。",
            )

        if message_id is not None:
            if not self.canonical_messages_enabled:
                return _json_result(
                    ok=False,
                    error="规范消息账本不可用，已拒绝原生 ID 降级读取。",
                )
            assert self.ledger is not None
            assert self.scope is not None
            canonical = self.ledger.get_in_scope(self.scope, message_id)
            if canonical is None or not canonical.native_message_id:
                return _json_result(
                    ok=False,
                    error="当前群可见上下文中找不到这条附件消息。",
                )
            native_message_id = int(canonical.native_message_id)

            canonical_attachments = self._canonical_attachment_records(
                canonical
            )
            if attachment_handle:
                selected = next(
                    (
                        item
                        for item in canonical_attachments
                        if item["handle"] == attachment_handle
                    ),
                    None,
                )
                if selected is None:
                    return _json_result(
                        ok=False,
                        error="当前消息中找不到这个规范附件句柄。",
                    )
                native_file_id = str(selected["_native_file_id"])
                canonical_download_url = str(
                    selected.get("_download_url") or ""
                )
            elif len(canonical_attachments) == 1:
                selected = canonical_attachments[0]
                native_file_id = str(selected["_native_file_id"])
                canonical_download_url = str(
                    selected.get("_download_url") or ""
                )
            elif len(canonical_attachments) > 1:
                return _json_result(
                    ok=False,
                    error="这条消息有多个附件，请提供目标 attachment_handle。",
                )

            message = await self.bot.get_msg(message_id=native_message_id)
            if int(message.get("group_id") or 0) != self.event.group_id:
                return _json_result(ok=False, error="附件消息不属于当前群。")
            attachments = _message_attachments(
                message.get("message") or message.get("raw_message")
            )
            if native_file_id:
                matched_file = next(
                    (
                        item
                        for item in attachments
                        if item["file_id"] == native_file_id
                    ),
                    None,
                )
            elif len(attachments) == 1:
                matched_file = attachments[0]
                native_file_id = matched_file["file_id"]
            elif len(attachments) > 1:
                return _json_result(
                    ok=False,
                    error="这条消息有多个附件，请提供目标 attachment_handle。",
                )
            if matched_file is None:
                return _json_result(
                    ok=False,
                    error="被回复消息中没有找到这个附件。",
                )
            if not native_file_id:
                return _json_result(
                    ok=False,
                    error="这个附件没有可下载的宿主映射。",
                )
            if canonical_download_url and not matched_file.get(
                "_download_url"
            ):
                matched_file["_download_url"] = canonical_download_url
        else:
            if not file_handle:
                return _json_result(
                    ok=False,
                    error="必须提供 groupfile# 句柄或附件消息的 msg# 句柄。",
                )
            group_files = await self._group_files(50)
            matched_file = next(
                (item for item in group_files if item["handle"] == file_handle),
                None,
            )
            if matched_file is None:
                return _json_result(
                    ok=False,
                    error="当前群最近文件中没有这个 groupfile# 句柄。",
                )
            native_file_id = str(matched_file["_native_file_id"])

        if (
            self.max_file_bytes
            and matched_file["file_size"] > self.max_file_bytes
        ):
            return _json_result(ok=False, error="群文件超过导入大小上限。")

        response = await self._resolve_napcat_file(
            matched_file,
            native_file_id,
        )
        content = await self._read_napcat_file(response)
        await self._activate_task_sandbox(sandbox_id)
        await self.sandbox_manager.write_file(
            self.owner,
            sandbox_id,
            destination,
            content,
            allow_large=True,
        )
        return _json_result(
            ok=True,
            source_name=(
                matched_file["file_name"]
                or str(response.get("file_name") or "")
            ),
            destination=destination,
            size=len(content),
        )

    @staticmethod
    def _canonical_message_payload(
        message: CanonicalMessage,
        *,
        include_files: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "handle": f"msg#{message.canonical_message_id}",
            "sender": (
                f"[mention#{message.sender_principal_id}] {message.sender_display}"
                if message.sender_principal_id is not None
                else message.sender_display
            ),
            "time": message.occurred_at,
            "text": message.rendered_text[:1000],
            "reply_to": (
                f"msg#{message.reply_to_canonical_message_id}"
                if message.reply_to_canonical_message_id is not None
                else None
            ),
        }
        if include_files:
            payload["attachments"] = render_api_attachments(
                message.body,
                message.canonical_message_id,
            )
        return payload

    async def _say(
        self,
        arguments: dict[str, object],
    ) -> str:
        text = str(arguments.get("text", "")).strip()
        if not text:
            return _json_result(ok=False, error="进度消息不能为空。")

        outgoing = await self.output_resolver.render(text[:200])
        if not outgoing:
            return _json_result(ok=False, error="进度消息解析后没有可发送内容。")
        response = await self.bot.send_group_msg(
            group_id=self.event.group_id,
            message=outgoing,
        )
        canonical_message_id = None
        if self.canonical_messages_enabled and isinstance(response, dict):
            native_message_id = response.get("message_id")
            if native_message_id:
                assert self.ledger is not None
                assert self.scope is not None
                stored = record_onebot_outgoing(
                    self.ledger,
                    self.scope,
                    native_message_id=str(native_message_id),
                    message=outgoing,
                    occurred_at=int(time.time()),
                )
                canonical_message_id = stored.canonical_message_id
                self._link_turn_send(canonical_message_id, "say")
        return _json_result(
            ok=True,
            message_handle=(
                f"msg#{canonical_message_id}"
                if canonical_message_id is not None
                else None
            ),
        )

    async def _group_files(self, limit: int) -> list[dict[str, Any]]:
        response = await self.bot.call_api(
            "get_group_root_files",
            group_id=self.event.group_id,
            file_count=limit,
        )
        if isinstance(response, dict):
            raw_files = response.get("files", [])
        elif isinstance(response, list):
            raw_files = response
        else:
            raw_files = []
        if not isinstance(raw_files, list):
            return []
        files = [
            _safe_file_info(item)
            for item in raw_files
            if isinstance(item, dict) and item.get("file_id")
        ]
        for file_info in files:
            file_info["handle"] = self._group_file_handle(
                str(file_info["_native_file_id"])
            )
        if self.canonical_messages_enabled:
            assert self.ledger is not None
            for file_info in files:
                native_uploader = file_info.pop("uploader", 0)
                file_info["uploader"] = (
                    self.ledger.principal_label_for_native(
                        "onebot-v11",
                        native_uploader,
                    )
                    or "未知群成员"
                )
        else:
            for file_info in files:
                file_info["uploader"] = "未知群成员"
        return files

    def _group_file_handle(self, native_file_id: str) -> str:
        scope_key = (
            self.scope.key
            if self.scope is not None
            else f"onebot-v11:group:{self.event.group_id}"
        )
        digest = hashlib.sha256(
            f"{scope_key}\0{native_file_id}".encode("utf-8")
        ).hexdigest()[:20]
        return f"groupfile#{digest}"

    @staticmethod
    def _public_group_file(file_info: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in file_info.items()
            if not key.startswith("_")
        }

    @staticmethod
    def _canonical_attachment_records(
        message: CanonicalMessage,
    ) -> list[dict[str, str]]:
        records = []
        for node in message.body.nodes:
            if not isinstance(node, MediaNode) or node.media_kind != "file":
                continue
            records.append(
                {
                    "handle": (
                        f"file#{message.canonical_message_id}."
                        f"{node.segment_index}"
                    ),
                    "_native_file_id": str(
                        node.raw_data.get("file_id") or node.source or ""
                    ),
                    "_download_url": str(node.raw_data.get("url") or ""),
                }
            )
        return records

    async def _resolve_napcat_file(
        self,
        matched_file: dict[str, Any],
        native_file_id: str,
    ) -> dict[str, Any]:
        inline_url = str(matched_file.get("_download_url") or "")
        if inline_url.startswith(("http://", "https://")):
            return {"url": inline_url}

        try:
            group_url = await self.bot.call_api(
                "get_group_file_url",
                group_id=self.event.group_id,
                file_id=native_file_id,
                busid=int(matched_file.get("_busid") or 102),
            )
        except Exception as exc:
            logger.debug(f"NapCat group file URL lookup failed: {exc}")
        else:
            if isinstance(group_url, dict):
                url = str(group_url.get("url") or "")
                if url.startswith(("http://", "https://")):
                    return {"url": url}

        response = await self.bot.call_api(
            "get_file",
            file_id=native_file_id,
        )
        if isinstance(response, dict):
            return response
        raise ValueError("NapCat 没有返回可下载文件。")

    async def _read_napcat_file(self, response: dict[str, Any]) -> bytes:
        encoded = response.get("base64")
        if isinstance(encoded, str) and encoded:
            prefix = "base64://"
            if encoded.startswith(prefix):
                encoded = encoded[len(prefix):]
            content = base64.b64decode(encoded, validate=True)
            return self._check_download_size(content)

        local_file = response.get("file")
        if isinstance(local_file, str) and local_file:
            path = Path(local_file)
            try:
                size = await asyncio.to_thread(path.stat)
                if self.max_file_bytes and size.st_size > self.max_file_bytes:
                    raise ValueError("文件超过导入大小上限。")
                content = await asyncio.to_thread(path.read_bytes)
            except OSError as exc:
                logger.debug(
                    "NapCat local file is unavailable; trying its download URL: "
                    f"{exc}"
                )
            else:
                return self._check_download_size(content)

        url = response.get("url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            async with httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
            ) as client:
                async with client.stream("GET", url) as download:
                    download.raise_for_status()
                    content = bytearray()
                    async for chunk in download.aiter_bytes():
                        content.extend(chunk)
                        if (
                            self.max_file_bytes
                            and len(content) > self.max_file_bytes
                        ):
                            raise ValueError("文件超过导入大小上限。")
                    return bytes(content)

        raise ValueError("NapCat 没有提供可读取的文件内容。")

    def _check_download_size(self, content: bytes) -> bytes:
        if self.max_file_bytes and len(content) > self.max_file_bytes:
            raise ValueError("文件超过导入大小上限。")
        return content

    def _link_turn_send(
        self,
        canonical_message_id: int,
        node_id: str,
    ) -> None:
        if self.turn_journal is None or self.turn_id is None:
            return
        try:
            self.turn_journal.link_send(
                self.turn_id,
                canonical_message_id,
                node_id=node_id,
            )
        except (sqlite3.Error, DatabaseError) as exc:
            logger.warning(f"Could not link tool send to turn: {exc}")

    @staticmethod
    def _safe_filename(filename: str) -> str:
        name = Path(filename).name.strip()
        if not name or name in {".", ".."}:
            raise ValueError("文件名无效。")
        return name[:180]
