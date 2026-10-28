"""Route existing browser tools to a durable, project-owned browser session."""

import json

from src.bot_storage import DatabaseError
from src.sandbox_runtime.docker import SandboxError
from src.sandbox_runtime.project_browser_worker import METHODS, arguments as browser_arguments
from src.sandbox_runtime.wire import ProtocolError

from .project_access import project_access, projects_available, projects_configured
from .project_browser_store import action_digest
from .project_executor import ProjectBackendUnavailable, _store
from .project_io import BROWSER_SESSION_TOOL
from .project_tool_executor import _error, _identity, _settle
from .project_usage import ProjectQuotaRejected
from .projects import ProjectConflict, ProjectQuotaExceeded
from .tool_invocation import active_tool_call_id
from .workers.durable_jobs import JobDeferred


def project_browser_available(context):
    return bool(
        "browser" in context.capabilities
        and projects_available(context)
        and context.app.settings.browser_enabled
        and context.app.project_runtime.browser is not None
    )


def project_browser_definition(definition, context):
    if not project_browser_available(context):
        return
    function = definition["function"]
    function["description"] += (
        " 已选择项目时，默认使用该项目独立的持久浏览器，需要项目编辑权限。"
        "未选项目时使用原有会话浏览器。元素 ref 仅属于产生它的浏览器，切换项目后先重新 snapshot。"
    )
    if function["name"] == "browser_clear":
        function["description"] = (
            "清除当前项目浏览器的 Cookie、登录状态和缓存，并关闭浏览器；不删除项目其他文件。"
            "会影响共享这个项目的编辑者。未选择项目时仅清除原有会话浏览器状态。"
        )


async def _bind(context, access, grant, project):
    runtime = context.app.project_runtime
    client = runtime.browser._client(project)
    health = await client.health()
    if (health.get("channel_ready") is not True
            or type(health.get("project_browser_protocol")) is not int
            or health["project_browser_protocol"] != 1
            or health.get("instance_id") != context.app.settings.project_instance_id):
        raise ProjectBackendUnavailable("Runner cannot verify the project browser installation")
    access = await project_access(context)
    await _store(runtime.browser.store.bind, access, grant, health["instance_id"])


async def _result(context, access, grant, name, value):
    runtime = context.app.project_runtime
    if name in {"browser_close", "browser_clear"}:
        await runtime.browser.drain(access, grant)
    # Drain and identity lookups await external systems. Recheck before disclosing
    # even a previously committed page to a member who may have just left.
    access = await project_access(context)
    await _store(runtime.browser.store.session, access, grant)
    field = {"browser_close": "closed", "browser_clear": "cleared"}.get(name, "page")
    return {"ok": True, field: value, "project_handle": f"prj#{grant.project_id}",
            "operation_id": grant.operation_id}


async def _execute(context, name, args):
    runtime = context.app.project_runtime
    browser = runtime.browser
    access = await project_access(context)
    # Legacy, non-project callers need no durable project call identity.
    if active_tool_call_id.get() is None:
        if await _store(runtime.store.current, access) is None:
            return None
    key, _ = _identity(context, name, args)
    call = await _store(browser.store.find, access, key, name, args)
    hashed = action_digest(name, args)
    grant = call.grant if call else await _store(runtime.io.find, access, key, BROWSER_SESSION_TOOL, hashed)
    project = (await _store(runtime.store.get, access, grant.project_id) if grant
               else await _store(runtime.store.current, access))
    if project is None:
        return None
    if not context.app.settings.is_sandbox_user_allowed(context.event.user_id):
        raise PermissionError("Project browsers require sandbox permission")
    if call is not None:
        row = await _store(browser.store.receipt, access, call)
        if row["state"] != "reserved":
            value = await browser.recover(access, call)
            return await _result(context, access, grant, name, value)
    if grant is None:
        if project["status"] != "active" or project["lifecycle_job_id"] is not None:
            if project["lifecycle_job_id"] is None:
                await _store(runtime.lifecycle.request, access, project["project_id"], "wake",
                             expected_version=project["version"], request_key="browser-wake:" + key)
            return _error("project_preparing", "项目正在恢复工作区，请通过 project_status 查看进度后再操作浏览器。")
        grant = await _store(browser.store.current, access, project["project_id"],
                             context.journal_turn_id, context.event.message_id)
        if grant is None:
            pending = await _store(runtime.io.pending, access, project["project_id"])
            if pending is not None:
                await _settle(runtime, access, pending)
            project = await _store(runtime.store.get, access, project["project_id"])
            # Reject an unsupported backend before reserving its workspace.
            browser._client(project)
            await runtime.sampler.sample_one(project)
            access = await project_access(context)
            grant = await _store(runtime.io.admit, access, project["project_id"], BROWSER_SESSION_TOOL,
                                 request_key=key, arguments_sha256=hashed, expected_version=project["version"],
                                 journal_turn_id=context.journal_turn_id, native_message_id=context.event.message_id)
    receipt = await _store(runtime.io.receipt, access, grant)
    if receipt["state"] == "reserved":
        await _bind(context, access, grant, project)
    await browser.open(access, grant)
    value = await browser.execute(access, grant, key, name, args)
    return await _result(context, access, grant, name, value)


async def execute_project_browser(context, name, args):
    """None means no selected/frozen project; failures must never fall through."""
    if name not in METHODS or not projects_configured(context):
        return None
    if not project_browser_available(context):
        return json.dumps(_error("project_browser_unavailable", "项目浏览器当前不可用，没有切换到普通会话浏览器。"), ensure_ascii=False)
    try:
        args = browser_arguments(name, args)
        result = await _execute(context, name, args)
    except ProtocolError:
        result = _error("project_invalid_arguments", "浏览器参数格式不正确。")
    except PermissionError:
        result = _error("project_forbidden", "当前账号或成员没有该项目的浏览器编辑权限。")
    except ProjectBackendUnavailable:
        result = _error("project_browser_unavailable", "该项目运行环境尚未接通独立浏览器，不会改用其他项目或普通会话的浏览器。")
    except ProjectQuotaRejected as exc:
        result = _error(exc.code, "项目或会话容量不足，或用量尚未核实；请查看项目状态。")
    except ProjectQuotaExceeded:
        result = _error("project_scope_quota_full", "会话容量不足，未启动浏览器。")
    except ProjectConflict:
        result = _error("project_conflict", "项目正被占用、状态已变化或上次操作尚未核实；没有重复点击，请查看项目状态。")
    except JobDeferred:
        result = _error("project_identity_unavailable", "暂时无法核实账号或群成员身份，没有继续操作。")
    except (DatabaseError, OSError, ValueError, SandboxError):
        result = _error("project_browser_unconfirmed", "项目浏览器未取得完整回执，已保留执行记录；不会自动重复点击或输入。")
    return json.dumps(result, ensure_ascii=False) if result is not None else None
