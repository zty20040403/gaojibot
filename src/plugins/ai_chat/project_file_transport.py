"""OneBot project file dispatch; ambiguous uploads are reconciled, not repeated."""

import asyncio
import base64
import time

from nonebot.adapters.onebot.v11 import MessageSegment

from .bridges import BridgeOutcomeUnknown, BridgePermanentError, BridgeRetryableError
from .project_executor import _store
from .project_file_outbox import image_message_id
from .projects import ProjectConflict
from .tool_policy import tool_enabled
from .workers.durable_jobs import JobDeferred


def project_runtime(app):
    runtime = getattr(app, "project_runtime", None)
    if runtime is None or not runtime.ready or runtime.closed:
        raise BridgeRetryableError("项目功能未就绪，文件保留在投递队列中")
    return runtime


async def file_identity(app, delivery):
    runtime = project_runtime(app)
    try:
        return await _store(runtime.files.identity, delivery)
    except PermissionError as exc:
        raise BridgePermanentError("项目文件投递身份不匹配") from exc


async def _access(runtime, identity):
    try:
        return await runtime.resolve_access(identity, identity["actor_principal_id"])
    except JobDeferred as exc:
        raise BridgeRetryableError("暂时无法核实项目成员身份，未发送文件") from exc
    except PermissionError as exc:
        raise BridgePermanentError("项目成员权限已失效，未发送文件") from exc


async def confirm_group_file(bot, identity, group_id):
    response = await bot.call_api("get_group_root_files", group_id=int(group_id))
    files = response.get("files") if isinstance(response, dict) else None
    if not isinstance(files, list):
        return None
    for item in files:
        if not isinstance(item, dict):
            continue
        if (
            item.get("file_name") != identity["filename"]
            or type(item.get("file_size")) is not int
            or item["file_size"] != identity["byte_size"]
            or str(item.get("uploader")) != str(bot.self_id)
            or type(item.get("upload_time")) is not int
            or not identity["upload_started_at"]
            or not identity["upload_started_at"] - 5
            <= item["upload_time"]
            <= int(time.time()) + 60
        ):
            continue
        file_id = item.get("file_id")
        if isinstance(file_id, str) and file_id.strip():
            return file_id
    return None


async def deliver_file(app, bot, delivery):
    runtime = project_runtime(app)
    identity = await file_identity(app, delivery)
    if str(bot.self_id) != identity["bot_native_user_id"]:
        raise BridgePermanentError("项目附件必须由原机器人账号发送")
    if not tool_enabled(identity["tool_name"]):
        await _store(app.delivery_store.defer_unsent, delivery, "文件发送工具已关闭")
        return
    access = await _access(runtime, identity)
    if identity["upload_started_at"] is not None:
        raise BridgeOutcomeUnknown("文件已有上传尝试，等待核对回执，不会重复上传")
    try:
        content = await _store(runtime.files.prepare, access, delivery)
    except ProjectConflict:
        await _store(
            app.delivery_store.defer_unsent, delivery, "项目文件读取尚未完成核实"
        )
        return
    except PermissionError as exc:
        raise BridgePermanentError("项目文件访问已被撤销") from exc
    except (OSError, ValueError) as exc:
        raise BridgePermanentError("项目文件副本校验失败，未发送") from exc
    # Membership is rechecked after potentially slow checksum I/O.
    await _access(runtime, identity)
    try:
        started = await _store(runtime.files.begin_upload, delivery)
    except ProjectConflict as exc:
        raise BridgeOutcomeUnknown("投递租约或上传状态已变化，未重复上传") from exc
    identity["upload_started_at"] = started
    try:
        async with asyncio.timeout(60):
            if identity["media_kind"] == "image":
                response = await bot.call_api(
                    "send_group_msg" if delivery.target_kind == "group" else "send_private_msg",
                    **(
                        {"group_id": int(delivery.target_native_conversation_id)}
                        if delivery.target_kind == "group"
                        else {"user_id": int(delivery.target_native_conversation_id)}
                    ),
                    message=[MessageSegment.image(content)],
                )
                receipt_id = image_message_id(
                    response.get("message_id") if isinstance(response, dict) else None
                )
                await _store(runtime.files.confirm, delivery, receipt_id)
                return
            response = await bot.call_api(
                "upload_group_file"
                if delivery.target_kind == "group"
                else "upload_private_file",
                **(
                    {"group_id": int(delivery.target_native_conversation_id)}
                    if delivery.target_kind == "group"
                    else {"user_id": int(delivery.target_native_conversation_id)}
                ),
                file="base64://" + base64.b64encode(content).decode(),
                name=identity["filename"],
            )
            if delivery.target_kind == "group":
                file_id = await confirm_group_file(
                    bot, identity, delivery.target_native_conversation_id
                )
            else:
                file_id = (
                    response.get("file_id") if isinstance(response, dict) else None
                )
            if not isinstance(file_id, str) or not file_id.strip():
                raise BridgeOutcomeUnknown("上传已尝试，但没有可核实的文件回执")
            await _store(runtime.files.confirm, delivery, file_id)
    except Exception as exc:
        # Includes timeouts and adapter errors after dispatch: the remote side
        # may have accepted bytes even if this process did not receive a result.
        raise BridgeOutcomeUnknown("上传结果尚未核实，保留附件并等待回执") from exc


async def reconcile_files(app, bots):
    runtime = getattr(app, "project_runtime", None)
    if runtime is None or not runtime.ready or runtime.closed:
        return
    for grant, identity in await _store(runtime.files.pending_reads):
        try:
            access = await _access(runtime, identity)
            project = await _store(runtime.store.get, access, grant.project_id)
            receipt = await _store(runtime.io.receipt, access, grant)
            evidence = await runtime.backends[project["backend"]].io_evidence(project)
            await _store(runtime.io.complete, grant, receipt["result"], evidence)
        except (
            BridgePermanentError,
            BridgeRetryableError,
            ProjectConflict,
            OSError,
            ValueError,
        ):
            continue
    await _store(runtime.files.resume_unsent)
    for delivery in await _store(runtime.files.reconcile_due):
        try:
            identity = await file_identity(app, delivery)
            bot = next(
                (b for b in bots if str(b.self_id) == identity["bot_native_user_id"]),
                None,
            )
            # A matching image in history is not proof of this particular send:
            # two intentional sends can have identical bytes. Without the API's
            # message receipt retain the copy and uncertainty, never guess/resend.
            if bot is None or delivery.target_kind != "group" or identity["media_kind"] == "image":
                continue
            await _access(runtime, identity)
            async with asyncio.timeout(10):
                file_id = await confirm_group_file(
                    bot, identity, delivery.target_native_conversation_id
                )
            if file_id:
                await _store(runtime.files.confirm, delivery, file_id)
        except Exception:
            # No upload occurs in reconciliation; keep the immutable file and
            # try a read-only receipt lookup later if the platform is offline.
            continue
