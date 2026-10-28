"""Copy a pinned project baseline into an isolated, root-owned step input tree."""

import asyncio
import json
import re

from src.project_content import manifest_bytes
from .agent.control import assert_job_owned
from .agent.execution import active_agent_step
from .projects import ProjectConflict


IMPORT_PROJECT_BASELINE = {"type": "function", "function": {
    "name": "import_project_baseline",
    "description": "把当前任务绑定的项目基线完整导入本步骤沙盒，原件只读。先复制到自己的工作目录再修改，不直接挂载项目卷。",
    "parameters": {"type": "object", "additionalProperties": False,
                   "properties": {"sandbox_id": {"type": "string"}}, "required": ["sandbox_id"]},
}}


class ProjectTaskInputs:
    def __init__(self, store, resolve_access):
        self.store, self.resolve_access = store, resolve_access

    async def index(self, task_id):
        assert_job_owned()
        access = await self.resolve_access()
        value = await asyncio.to_thread(self.store.baseline, access, task_id)
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
