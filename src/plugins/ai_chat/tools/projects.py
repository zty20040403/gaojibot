"""Conversation-scoped project commands, using the same runtime as leased jobs."""

from functools import partial
import hashlib
import json

from jsonschema import ValidationError, validate
from ..onebot_codec import scope_from_event
from ..project_access import project_access as _access, projects_available as enabled
from ..project_context import current_project_payload
from ..project_executor import _store
from ..project_tool_definitions import PROJECT_TOOLS
from ..projects import ProjectConflict, ProjectQuotaExceeded
from ..workers.durable_jobs import JobDeferred
from .registry import FunctionTool


def _key(context, action, target):
    event, scope = context.event, scope_from_event(context.event)
    # Transport identity is supplied by the host, never accepted from tool args.
    source = [
        scope.key,
        scope.bot_native_user_id,
        scope.actor_native_user_id,
        event.message_id,
        event.time,
        action,
        target,
    ]
    return hashlib.sha256(json.dumps(source, ensure_ascii=False).encode()).hexdigest()


def _execution_allowed(context):
    if not context.app.settings.is_sandbox_user_allowed(context.event.user_id):
        raise PermissionError("Sandbox admission required")


def _project(row):
    return {
        "handle": f"prj#{row['project_id']}",
        "title": row["title"],
        "status": row["status"],
        "revision": row["version"],
        "task_handle": f"task#{row['locked_task_id']}"
        if row["locked_task_id"]
        else None,
        "preparing": row["lifecycle_job_id"] is not None,
    }


def _version(row):
    return {
        "handle": f"v{row['version_no']}",
        **{k: row[k] for k in ("label", "pinned", "created_at")},
    }


async def _target(runtime, access, arguments):
    if "project_handle" in arguments:
        project = await _store(
            runtime.store.get, access, int(arguments["project_handle"][4:])
        )
    else:
        project = await _store(runtime.store.current, access)
    if project is None:
        raise LookupError("No current project")
    if project["bot_native_user_id"] != access.scope.bot_native_user_id:
        raise PermissionError("Project requires operator account binding")
    return project


async def _wake(context, access, project, action):
    runtime = context.app.project_runtime
    if project["status"] == "active" or project["lifecycle_job_id"] is not None:
        return project
    await _store(
        runtime.lifecycle.request,
        access,
        project["project_id"],
        "wake",
        expected_version=project["version"],
        request_key=_key(context, action, project["project_id"]),
    )
    return await _store(runtime.store.get, access, project["project_id"])


async def _pending(runtime, access, project_id):
    project = await _store(runtime.store.get, access, project_id)
    if project["lifecycle_job_id"] is None:
        return None
    runs = await _store(runtime.lifecycle.journal, access, project_id)
    run = next((r for r in runs if r["job_id"] == project["lifecycle_job_id"]), None)
    job = await _store(runtime.app.job_store.get, project["lifecycle_job_id"])
    if run is None or job is None:
        return {"status": "unknown"}
    status = "needs_attention" if run["status"] == "needs_attention" else job.status
    return {
        "status": status,
        "action": run["action"],
        "completed_steps": run["next_step"],
        "total_steps": len(run["plan"]["steps"]),
    }


async def execute(turn, arguments, *, name):
    context, runtime = turn.context, turn.context.app.project_runtime
    definition = next(
        item for item in PROJECT_TOOLS if item["function"]["name"] == name
    )
    try:
        validate(arguments, definition["function"]["parameters"])
    except ValidationError:
        return json.dumps(
            {
                "ok": False,
                "code": "project_invalid_arguments",
                "error": "项目参数格式不正确，请检查句柄和修订号。",
            },
            ensure_ascii=False,
        )
    try:
        access = await _access(context)
        store = runtime.store
        if name == "project_list":
            current = await _store(store.current, access)
            if (
                current
                and current["bot_native_user_id"] != access.scope.bot_native_user_id
            ):
                current = None
            result = {
                "projects": [
                    _project(p)
                    for p in await _store(store.list, access)
                    if p["bot_native_user_id"] == access.scope.bot_native_user_id
                ],
                "current": f"prj#{current['project_id']}" if current else None,
            }
        elif name == "project_create":
            _execution_allowed(context)
            title = arguments["title"].strip()
            project = await _store(
                store.create,
                access,
                title,
                backend=context.app.settings.sandbox_backend,
                creation_key=_key(context, name, title),
            )
            project = await _wake(context, access, project, name)
            current = await _store(store.current, access)
            result = {
                "project": _project(project),
                "current": current is not None
                and current["project_id"] == project["project_id"],
            }
        else:
            project = await _target(runtime, access, arguments)
            pid = project["project_id"]
            if name == "project_open":
                project = await _store(store.open, access, pid)
                readonly = False
                try:
                    _execution_allowed(context)
                    await _store(store.get, access, pid, edit=True)
                except PermissionError:
                    readonly = True
                if not readonly:
                    project = await _wake(context, access, project, name)
                result = {
                    "project": _project(project),
                    "current": True,
                    "readonly": readonly,
                }
            elif name == "project_versions":
                result = {
                    "project": _project(project),
                    "versions": [
                        _version(v) for v in await _store(store.versions, access, pid)
                    ],
                }
            elif name == "project_set_rebuild":
                _execution_allowed(context)
                updated = await _store(
                    store.update,
                    access,
                    pid,
                    expected_version=arguments["expected_revision"],
                    rebuild_command=arguments["command"],
                )
                result = {
                    "project": _project(updated),
                    "command": updated["rebuild_command"],
                    "executed": False,
                }
            else:
                usage = await _store(runtime.usage.status, access, pid)
                # Before any physical generation exists, the configured backend
                # is authoritative; the schema default must not claim hard quota.
                if project["workspace_generation"] == 0 and not usage["complete"]:
                    usage["quota_mode"] = runtime.backends[
                        project["backend"]
                    ].quota_mode
                result = {
                    "project": _project(project),
                    "usage": usage,
                    "versions": [
                        _version(v)
                        for v in (await _store(store.versions, access, pid))[:3]
                    ],
                }
        if "project" in result and result["project"]["preparing"]:
            operation = await _pending(
                runtime, access, int(result["project"]["handle"][4:])
            )
            result["operation"] = operation
            result["project"]["operation_status"] = (
                operation["status"] if operation else "unknown"
            )
        if name in {"project_create", "project_open"}:
            result["current_project_context"] = await current_project_payload(context)
        result = {"ok": True, **result}
    except JobDeferred:
        result = {
            "ok": False,
            "code": "project_identity_unavailable",
            "error": "暂时无法核实 QQ 在线状态或群成员，请稍后重试。",
        }
    except PermissionError:
        result = {
            "ok": False,
            "code": "project_forbidden",
            "error": "当前会话没有可访问的这个项目，或你没有此操作权限。",
        }
    except ProjectQuotaExceeded:
        result = {
            "ok": False,
            "code": "project_quota_full",
            "error": "项目数量或空间已达上限，请清理不需要的项目或联系管理员调整额度。",
        }
    except ProjectConflict:
        result = {
            "ok": False,
            "code": "project_conflict",
            "error": "项目状态已变化或任务尚未结束，请查看 /项目 状态 后再操作。",
        }
    except LookupError:
        result = {
            "ok": False,
            "code": "project_not_selected",
            "error": "尚未选择项目，或选择已超过 24 小时；用 /项目 查看并切换。",
        }
    except ValueError:
        result = {
            "ok": False,
            "code": "project_invalid_arguments",
            "error": "项目参数无效，请检查名称、句柄或重建命令。",
        }
    return json.dumps(result, ensure_ascii=False)


HANDLERS = tuple(
    FunctionTool(d, enabled, partial(execute, name=d["function"]["name"]))
    for d in PROJECT_TOOLS
)
