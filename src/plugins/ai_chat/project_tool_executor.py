"""Existing sandbox tools routed to a scoped project, without task cleanup."""

from copy import deepcopy
import hashlib
import json

from jsonschema import ValidationError, validate

from src.bot_storage import DatabaseError
from src.project_content import project_path
from src.sandbox_runtime.docker import NIX_ATTRIBUTE_PATTERN, SandboxError
from .ai_tools import SANDBOX_TOOLS
from .project_access import project_access, projects_available
from .project_executor import _store
from .project_io import BROWSER_SESSION_TOOL, WRITE_TOOLS
from .project_file_outbox import ATTACHMENT_TOOLS, MAX_IMAGE_BYTES
from .project_usage import ProjectQuotaRejected
from .projects import ProjectConflict, ProjectQuotaExceeded
from .tool_invocation import active_tool_call_id
from .workers.durable_jobs import JobDeferred
from .vm_sandbox import PACKAGE_PATTERN


PROJECT_IO_TOOLS = frozenset({"sandbox_read_file", *ATTACHMENT_TOOLS, *WRITE_TOOLS})


def routes_to_project(context, name, arguments):
    return (
        name in PROJECT_IO_TOOLS
        and "sandbox_id" not in arguments
        and projects_available(context)
    )


def project_parameters(definition):
    schema = deepcopy(definition["function"]["parameters"])
    schema["required"].remove("sandbox_id")
    if "command" in schema["properties"]:
        schema["properties"]["command"].update(minLength=1, maxLength=32768)
    return schema


def _error(code, message):
    return {"ok": False, "code": code, "error": message}


def _identity(context, name, arguments):
    call_id = active_tool_call_id.get()
    if not isinstance(call_id, str) or not call_id or len(call_id) > 256:
        raise ProjectConflict("A stable host tool-call identity is required")
    from .tools.state import structured_tool_source
    structured = structured_tool_source(context)
    if structured is not None:
        inbound = getattr(context,'inbound',None)
        marker = (['inbound',inbound.native_message_id] if inbound is not None else
                  [structured.source_kind,structured.source_ref,structured.source_revision])
        key = ['official-project-io-v1',structured.endpoint.key,structured.actor.key,*marker,call_id]
        digest = hashlib.sha256(json.dumps([name,arguments],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        return hashlib.sha256(json.dumps(key).encode()).hexdigest(),digest
    from .onebot_codec import scope_from_event
    event, scope = context.event, scope_from_event(context.event)
    key = [
        scope.key,
        scope.bot_native_user_id,
        scope.actor_native_user_id,
        event.message_id,
        event.time,
        call_id,
    ]
    digest = hashlib.sha256(
        json.dumps([name, arguments], sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return hashlib.sha256(json.dumps(key).encode()).hexdigest(), digest


async def _settle(runtime, access, grant):
    receipt = await _store(runtime.io.receipt, access, grant)
    if receipt["state"] == "completed":
        return receipt["result"]
    if receipt["tool"] == BROWSER_SESSION_TOOL:
        return await runtime.browser.drain(access, grant)
    if (
        receipt["state"] not in {"dispatched", "needs_attention"}
        or receipt["result"] is None
    ):
        return None
    project = await _store(runtime.store.get, access, grant.project_id)
    evidence = await runtime.backends[project["backend"]].io_evidence(project)
    await _store(runtime.io.complete, grant, receipt["result"], evidence)
    return receipt["result"]


async def _invoke(manager, target, name, args):
    owner, sandbox = target["owner"], target["sandbox_id"]
    if name == "sandbox_exec":
        result = await manager.exec(
            owner,
            sandbox,
            args["command"],
            args.get("timeout_seconds"),
            packages=args.get("packages", []),
        )
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    if name == "sandbox_write_file":
        size = await manager.write_file(
            owner, sandbox, args["path"], args["content"].encode("utf-8")
        )
        return {"ok": True, "path": args["path"], "bytes_written": size}
    try:
        content = await manager.read_file(
            owner, sandbox, args["path"], max_bytes=64 * 1024
        )
    except SandboxError:
        return _error("project_read_failed", "文件不存在、不可读或超过读取大小限制。")
    return {
        "ok": True,
        "path": args["path"],
        "content": content.decode("utf-8", errors="replace"),
    }


async def _execute(context, name, args):
    runtime = context.app.project_runtime
    key, digest = _identity(context, name, args)
    access = await project_access(context)
    from .project_authorization import official_project
    official = official_project(access) is not None
    if official and getattr(context,'inbound',None) is None:
        raise PermissionError('Project task steps use their isolated pinned baseline, not the live workspace')
    if name in WRITE_TOOLS and not official and not context.app.settings.is_sandbox_user_allowed(
        context.event.user_id
    ):
        raise PermissionError("Sandbox execution is not allowed")
    grant = await _store(runtime.io.find, access, key, name, digest)
    project = (
        await _store(runtime.store.get, access, grant.project_id)
        if grant
        else await _store(runtime.store.current, access)
    )
    if project is None:
        return _error(
            "project_not_selected",
            "尚未选择项目，请先创建或打开项目；临时沙盒请明确提供 sandbox_id。",
        )
    pid = project["project_id"]
    if grant is not None:
        receipt = await _store(runtime.io.receipt, access, grant)
        if receipt["state"] != "reserved":
            result = await _settle(runtime, access, grant)
            if result is None:
                return _error(
                    "project_io_pending",
                    "上次调用的执行结果尚未核实，已保留记录，不会重新执行。",
                )
            access = await project_access(context)
            return (await _store(runtime.io.receipt, access, grant))["result"]
    elif project["status"] != "active" or project["lifecycle_job_id"] is not None:
        if project["lifecycle_job_id"] is None:
            if not official and not context.app.settings.is_sandbox_user_allowed(context.event.user_id):
                raise PermissionError("Sandbox execution is not allowed")
            await _store(
                runtime.lifecycle.request,
                access,
                pid,
                "wake",
                expected_version=project["version"],
                request_key="tool-wake:" + key,
            )
        return _error(
            "project_preparing",
            "项目正在恢复工作区；请通过 project_status 查看进度，准备好后再执行。",
        )
    if grant is None:
        pending = await _store(runtime.io.pending, access, pid)
        if pending is not None:
            await _settle(runtime, access, pending)
        project = await _store(runtime.store.get, access, pid)
        if name in WRITE_TOOLS:
            await runtime.sampler.sample_one(project, source=access) if official else await runtime.sampler.sample_one(project)
        grant = await _store(
            runtime.io.admit,
            access,
            pid,
            name,
            request_key=key,
            arguments_sha256=digest,
            expected_version=project["version"],
            expected_bytes=len(args["content"].encode("utf-8"))
            if name == "sandbox_write_file"
            else 0,
            installing_dependencies=bool(args.get("packages")),
            journal_turn_id=context.journal_turn_id,
            native_message_id=(context.inbound.native_message_id if context.inbound is not None else None)
            if official else context.event.message_id,
        )
    try:
        await runtime.backends[project["backend"]].io_evidence(project)
        access = await project_access(context)
        target = await _store(runtime.io.dispatch, access, grant)
        if name in ATTACHMENT_TOOLS:
            limit = context.app.sandbox_manager.max_file_bytes or 256 * 1024**2
            if name == "send_image_from_sandbox":
                limit = min(limit, MAX_IMAGE_BYTES)
            try:
                content = await context.app.sandbox_manager.read_file(
                    target["owner"], target["sandbox_id"], args["path"],
                    max_bytes=limit,
                )
            except SandboxError:
                result = _error("project_read_failed", "文件不存在、不可读或超过传输限制。")
            else:
                access = await project_access(context)
                try:
                    if official:
                        delivery = getattr(runtime,'official_file_delivery',None)
                        if delivery is None:
                            raise PermissionError('Official project file delivery is unavailable')
                        result = await delivery(access,grant,content,args['path'],args.get('filename'))
                    else:
                        result = await _store(runtime.files.queue, access, grant, content, args["path"], args.get("filename"))
                except ProjectQuotaRejected as exc:
                    result = _error(exc.code, "会话用量尚未核实，未创建文件副本或投递。")
                except ProjectQuotaExceeded:
                    result = _error("project_scope_quota_full", "会话容量不足，未创建文件副本或投递。")
                except ProjectConflict:
                    raise
                except ValueError:
                    result = _error("project_attachment_invalid", "文件为空、名称或容量不符合要求，未创建投递。")
        else:
            result = await _invoke(context.app.sandbox_manager, target, name, args)
        result.update(project_handle=f"prj#{pid}", operation_id=grant.operation_id)
        await _store(runtime.io.observe_result, grant, result)
        await _settle(runtime, access, grant)
    except BaseException:
        # Persist uncertainty even when the outer loop cancels its wait. A new
        # tool-call ID is not permission to repeat an unconfirmed side effect.
        receipt = await _store(runtime.io.receipt, access, grant)
        if receipt["state"] == "reserved":
            await _store(runtime.io.abort, grant)
        elif receipt["state"] in {"dispatched", "needs_attention"}:
            await _store(runtime.io.uncertain, grant)
        raise
    if name in WRITE_TOOLS:
        try:
            project = await _store(runtime.store.get, access, pid)
            await runtime.sampler.sample_one(project, source=access) if official else await runtime.sampler.sample_one(project)
        except (
            PermissionError,
            JobDeferred,
            DatabaseError,
            OSError,
            ValueError,
            SandboxError,
        ):
            # The execution receipt is already committed. Retain its byte
            # reservation until the supervised sampler obtains a valid sample.
            pass
    access = await project_access(context)
    return (await _store(runtime.io.receipt, access, grant))["result"]


async def execute_project_tool(context, name, arguments):
    try:
        definition = next(d for d in SANDBOX_TOOLS if d["function"]["name"] == name)
        validate(arguments, project_parameters(definition))
        if any(not arguments[k].strip() for k in ("command", "path") if k in arguments):
            raise ValueError("Empty project command or path")
        if "command" in arguments and "\0" in arguments["command"]:
            raise ValueError("Invalid project command")
        pattern = (
            PACKAGE_PATTERN
            if context.app.settings.sandbox_backend == "vm"
            else NIX_ATTRIBUTE_PATTERN
        )
        if any(not pattern.fullmatch(value) for value in arguments.get("packages", [])):
            raise ValueError("Invalid dependency name")
        if "path" in arguments:
            arguments = {**arguments, "path": project_path(arguments["path"])}
        if arguments.get("background"):
            return json.dumps(
                _error(
                    "project_background_unavailable",
                    "项目命令目前在本轮执行；后台项目任务尚未接通，不会改用临时沙盒。",
                ),
                ensure_ascii=False,
            )
        if (
            name == "sandbox_write_file"
            and len(arguments["content"].encode("utf-8"))
            > context.app.sandbox_manager.max_file_bytes
        ):
            raise ValueError("File exceeds transfer limit")
        result = await _execute(context, name, arguments)
    except ValidationError:
        result = _error("project_invalid_arguments", "工具参数格式不正确。")
    except PermissionError:
        result = _error("project_forbidden", "当前账号或成员没有这项项目权限。")
    except ProjectQuotaRejected as exc:
        result = _error(
            exc.code, "项目或会话容量不足，或用量尚未核实；请查看项目状态。"
        )
    except ProjectConflict:
        result = _error(
            "project_conflict",
            "项目状态已变化、正被占用或上次调用尚未核实；没有重复执行，请查看项目状态。",
        )
    except JobDeferred:
        result = _error(
            "project_identity_unavailable",
            "暂时无法核实账号或群成员身份，没有继续操作。",
        )
    except (DatabaseError, OSError, ValueError, SandboxError):
        result = _error(
            "project_io_unconfirmed",
            "项目工具未取得完整回执；若已开始执行，记录和工作区会保留，不会自动重跑。",
        )
    return json.dumps(result, ensure_ascii=False)
