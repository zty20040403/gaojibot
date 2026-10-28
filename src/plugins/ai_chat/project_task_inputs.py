"""Copy a pinned project baseline into an isolated, root-owned step input tree."""

import asyncio
import json
import re

from src.project_content import manifest_bytes
from src.project_update import ARTIFACT_NAME, DESCRIPTOR
from .agent.control import assert_job_owned
from .agent.execution import active_agent_step
from .onebot_codec import scope_from_event
from .project_access import project_access
from .project_executor import _store
from .project_tasks import ProjectTaskStore
from .projects import ProjectConflict
from .workers.durable_jobs import JobDeferred


IMPORT_PROJECT_BASELINE = {"type": "function", "function": {
    "name": "import_project_baseline",
    "description": "把当前任务绑定的项目基线完整导入本步骤沙盒，原件只读。先复制到自己的工作目录再修改，不直接挂载项目卷。",
    "parameters": {"type": "object", "additionalProperties": False,
                   "properties": {"sandbox_id": {"type": "string"}}, "required": ["sandbox_id"]},
}}


async def inputs_for_task(context, task_id):
    """Rebind original task inputs, never resolve the user's mutable focus."""
    app = context.app
    task = await app.db_call(app.subagent_store.get, task_id)
    if task is None:
        raise LookupError("Task no longer exists")
    if task.project_id is None:
        return None
    scope = scope_from_event(context.event)
    if task.scope_key != scope.key or str(task.requester_user_id) != scope.actor_native_user_id:
        raise PermissionError("Project task belongs to another conversation or actor")
    runtime = getattr(app, "project_runtime", None)
    if (runtime is None or not app.settings.projects_enabled
            or not runtime.ready or runtime.closed):
        raise JobDeferred("Project runtime unavailable; retaining the original task binding", 30)
    bindings = ProjectTaskStore(runtime.store, app.subagent_store)

    async def resolve():
        access = await project_access(context)
        if not app.settings.is_sandbox_user_allowed(context.event.user_id):
            raise PermissionError("Project actor is no longer allowed to execute sandboxes")
        return access

    inputs = ProjectTaskInputs(bindings, resolve)
    # Fail before starting a worker or consulting any current-project notes.
    await inputs.index(task_id)
    return inputs


def project_output_contract():
    return (
        f"\n[项目更新交付格式]\n最终负责合并改动的步骤只交付一个 {ARTIFACT_NAME}，"
        f"ZIP 根目录包含 {DESCRIPTOR} 和 files/<相对路径>。描述文件为 JSON："
        '{"format":1,"project_id":基线项目编号,"base_version":基线版本号,'
        '"base_manifest_sha256":"基线哈希","files":[{"path":"相对路径",'
        '"sha256":"文件实际SHA256","size":实际字节数,"mode":420}],"delete":[]}。'
        "files 按路径排序，只放新增或修改的保留文件；未列出的文件原样保留，"
        "删除必须在 delete 中明确列出并按路径排序。哈希和大小必须用程序实算，不能猜。"
        "不打包依赖、缓存或浏览器缓存，不创建符号链接。修改依赖时同时提交锁文件；"
        "如需修改重建方式，用可选 rebuild 字段声明 command（单行命令）、"
        "locks（2-8 个包含 path 和实际 sha256 的输入，含依赖清单和锁文件，按路径排序）、"
        "excluded（排序且不重叠的可重建目录）；null 表示移除，省略表示沿用。"
        "中间步骤只向最终合并步骤提供自己的产物，不能各自交付一个项目更新包。"
        "独立验收人必须导入这一个精确快照和项目基线，把基线复制到自己的可写目录，"
        "按描述应用新增、修改和删除后实际检查完整项目，不能仅检查 ZIP 能否打开；"
        "验收人不生成新的更新包。宿主在验收后负责版本发布和工作区恢复，"
        "尚未收到宿主恢复完成回执时，不能声称项目工作区已经更新。"
    )


class ProjectTaskInputs:
    def __init__(self, store, resolve_access):
        self.store, self.resolve_access = store, resolve_access

    async def index(self, task_id):
        assert_job_owned()
        access = await self.resolve_access()
        value = await _store(self.store.baseline, access, task_id)
        return {key: value[key] for key in ("project_id", "version_no", "manifest_sha256")} | {
            "file_count": len(value["manifest"]["files"]),
            "total_bytes": sum(item["size"] for item in value["manifest"]["files"]),
        }

    async def import_into(self, task_id, workspaces, arguments):
        if (set(arguments) != {"sandbox_id"} or not isinstance(arguments["sandbox_id"], str)
                or not re.fullmatch(r"s[0-9a-f]{6}", arguments["sandbox_id"])):
            raise ValueError("A task-owned sandbox ID is required")
        step = active_agent_step.get()
        prefix = f"task#{task_id}/"
        if not isinstance(step, str) or not step.startswith(prefix) or not step[len(prefix):]:
            raise PermissionError("Project inputs are only available inside their task step")
        sandbox_id = arguments["sandbox_id"]
        executor, manager = workspaces.executor, workspaces.manager

        async def guard():
            assert_job_owned()
            access = await self.resolve_access()
            conversation = await asyncio.to_thread(self.store.require_step, access, task_id, step[len(prefix):])
            if executor.base_owner != conversation or executor.owner != f"{conversation}:{step}":
                raise PermissionError("Project inputs require the isolated step owner")
            return access

        access = await guard()
        baseline = await asyncio.to_thread(self.store.baseline, access, task_id)
        owner = executor.owner
        # Reject the project workspace even if an adapter mistakenly accepts its
        # identifier. The destination must already belong to this task step.
        sandboxes = await manager.list(owner)
        if not any(item.get("sandbox_id") == sandbox_id and item.get("purpose") == "task" for item in sandboxes):
            raise PermissionError("Destination is not this step's task sandbox")
        await guard()
        await manager.start_owned(owner, sandbox_id)
        root = f"upstream/project-{baseline['project_id']}/{baseline['manifest_sha256']}"
        document = baseline["manifest"]
        for item in document["files"]:
            access = await guard()
            observed = await asyncio.to_thread(self.store.read_baseline_file, access, task_id, item["path"])
            if any(observed[k] != item[k] for k in ("sha256", "size", "mode")):
                raise ProjectConflict("Pinned baseline changed during import")
            await guard()
            await manager.install_readonly_file(owner, sandbox_id, f"{root}/files/{item['path']}", observed["content"])
        await guard()
        encoded = manifest_bytes(document, quota_bytes=sum(item["size"] for item in document["files"]))
        # The manifest is installed last. Partial imports remain replayable;
        # absence of a completed return never authorizes treating them as ready.
        await manager.install_readonly_file(owner, sandbox_id, f"{root}/manifest.json", encoded)
        await guard()
        return json.dumps({"ok": True, "read_only": True, "project_id": baseline["project_id"],
            "version_no": baseline["version_no"], "manifest_sha256": baseline["manifest_sha256"],
            "path": f"/workspace/{root}/files", "manifest_path": f"/workspace/{root}/manifest.json",
            "file_count": len(document["files"])}, ensure_ascii=False)
