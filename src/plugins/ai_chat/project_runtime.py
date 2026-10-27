"""Opt-in project composition with live identity checks and durable execution."""

from __future__ import annotations

import asyncio
import re

from nonebot import get_bots
from nonebot.adapters.onebot.v11 import Bot

from src.sandbox_runtime.client import SandboxClient
from src.storage_admission import require_capacity

from .conversation_scope import ConversationScope
from .observability import telemetry
from .project_executor import DockerProjectBackend, ProjectLifecycleExecutor, _store
from .project_file_executor import ProjectFileExecutor
from .project_lifecycle import KIND, ProjectLifecycle
from .project_io import ProjectIOStore
from .project_maintenance import ProjectMaintenance
from .project_policy import DAY, ProjectPolicy
from .project_usage import ProjectUsageSampler, ProjectUsageStore
from .projects import ProjectAccess, ProjectStore
from .vm_sandbox import VmSandboxManager
from .workers.durable_jobs import JobDeferred


def validate_project_settings(settings):
    """Reject unsupported composition before opening application resources."""
    if not settings.projects_enabled:
        return
    if not all(
        (
            settings.sandbox_enabled,
            settings.ledger_enabled,
            settings.durable_jobs_enabled,
            settings.outbox_enabled,
        )
    ):
        raise RuntimeError(
            "AI_PROJECTS_ENABLED requires sandbox, ledger, durable jobs and Outbox"
        )
    ProjectPolicy(
        idle_stop_seconds=settings.project_idle_stop_minutes * 60,
        hibernate_seconds=settings.project_hibernate_days * DAY,
        archive_seconds=settings.project_archive_days * DAY,
        keep_recent=settings.project_version_keep_recent,
    )
    if settings.sandbox_backend == "vm":
        if not re.fullmatch(r"[a-f0-9]{32}", settings.project_instance_id):
            raise RuntimeError(
                "VM projects require a stable 32-hex AI_PROJECT_INSTANCE_ID"
            )
    elif settings.sandbox_backend != "oci" or not all(
        (
            settings.sandbox_runner_socket,
            settings.sandbox_runner_token,
        )
    ):
        raise RuntimeError(
            "OCI projects require the private quota-capable sandbox runner"
        )


class ProjectRuntime:
    def __init__(self, handlers):
        self.handlers, self.app = handlers, handlers.context
        validate_project_settings(self.app.settings)
        if not self.app.settings.projects_enabled:
            raise RuntimeError("Projects are disabled")
        if any(
            getattr(self.app, name, None) is None
            for name in (
                "database",
                "message_ledger",
                "job_store",
                "job_worker",
                "delivery_store",
            )
        ):
            raise RuntimeError(
                "Projects require available database, ledger, jobs and Outbox stores"
            )
        self.store = None
        self.io = None
        self.backends = {}
        self.ready = False
        self.closed = False
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._vm_mounts = None

    async def resolve_access(self, project, principal_id):
        """Never use cached event roles or a different connected bot on recovery."""
        if self.closed or not self.app.settings.projects_enabled:
            raise PermissionError("Project runtime is unavailable")
        platform, kind, conversation = project["scope_key"].split(":", 2)
        bot_id = project["bot_native_user_id"]
        if (
            platform != "onebot-v11"
            or kind not in {"group", "private"}
            or not re.fullmatch(r"[1-9][0-9]*", conversation)
            or not re.fullmatch(r"[1-9][0-9]*", bot_id)
        ):
            raise PermissionError(
                "Project has no supported host-bound conversation identity"
            )
        identity = await self.app.db_call(
            self.app.message_ledger.native_identity_for_principal,
            platform,
            principal_id,
        )
        if identity is None or not re.fullmatch(r"[1-9][0-9]*", identity[0]):
            raise PermissionError("Project actor has no current platform identity")
        user_id = identity[0]
        if kind == "private" and (
            user_id != conversation or principal_id != project["creator_principal_id"]
        ):
            raise PermissionError("Private project belongs to another user")
        bot = get_bots().get(bot_id)
        if not isinstance(bot, Bot) or str(bot.self_id) != bot_id:
            raise JobDeferred("Project bot account is disconnected", 30)
        if kind == "group":
            if not await self.app.db_call(
                self.handlers.group_enabled, int(conversation)
            ):
                raise PermissionError("Project group is disabled")
            try:
                async with asyncio.timeout(10):
                    members = await bot.get_group_member_list(
                        group_id=int(conversation), no_cache=True
                    )
                if not isinstance(members, list) or any(
                    not isinstance(member, dict)
                    or not re.fullmatch(r"[1-9][0-9]*", str(member.get("user_id", "")))
                    for member in members
                ):
                    raise ValueError("Invalid current membership response")
            except Exception:
                raise JobDeferred(
                    "Project group membership is temporarily unavailable", 30
                ) from None
            present = {str(member["user_id"]) for member in members}
            if not {user_id, bot_id} <= present:
                raise PermissionError(
                    "Project actor or bot is no longer a group member"
                )
        return ProjectAccess(
            ConversationScope(platform, kind, conversation, user_id, bot_id),
            principal_id,
            member=True,
            admin=int(user_id) in self.app.settings.admin_user_ids,
        )

    async def _execution_access(self, project, principal_id):
        access = await self.resolve_access(project, principal_id)
        if not self.app.settings.is_sandbox_user_allowed(
            int(access.scope.actor_native_user_id)
        ):
            raise PermissionError(
                "Project actor is no longer allowed to execute sandboxes"
            )
        return access

    async def _backend(self):
        manager = self.app.sandbox_manager
        if isinstance(manager, SandboxClient):
            health = await manager.health()
            mode = health.get("project_quota_mode")
            if any(
                health.get(key) is not True
                for key in (
                    "channel_ready",
                    "project_volumes_enabled",
                    "project_workspaces_enabled",
                )
            ) or mode not in {"hard", "soft"}:
                raise RuntimeError(
                    "Private runner has no verified project quota capability"
                )
            expected = self.app.settings.project_instance_id
            if expected and health.get("instance_id") != expected:
                raise RuntimeError(
                    "Private runner belongs to another project installation"
                )
            files = ProjectFileExecutor(
                self.lifecycle, manager, resolve_access=self._execution_access
            )
            return "oci", DockerProjectBackend(manager, files=files, quota_mode=mode)
        if isinstance(manager, VmSandboxManager):
            from src.sandbox_runtime.project_vm_disks import VmProjectDisks
            from src.sandbox_runtime.project_vm_files import VmProjectFiles
            from src.sandbox_runtime.project_vm_runtime import VmProjectRuntime
            from .project_vm_executor import VmProjectBackend

            disks = await _store(
                VmProjectDisks,
                manager.root / "project-volumes",
                self.app.settings.project_instance_id,
                capacity_check=require_capacity,
            )
            runtime = VmProjectRuntime(manager, disks)
            if manager.project_mounts is not None:
                raise RuntimeError("VM project ownership is already configured")
            files = ProjectFileExecutor(
                self.lifecycle,
                VmProjectFiles(runtime.mounts),
                resolve_access=self._execution_access,
            )
            manager.project_mounts = runtime.mounts
            self._vm_mounts = runtime.mounts
            return "vm", VmProjectBackend(runtime, files=files)
        raise RuntimeError(
            "Project quota backend is unavailable; ordinary sandboxes are not a fallback"
        )

    async def start(self):
        async with self._lock:
            if self.closed:
                raise RuntimeError("Project runtime is closed")
            if self.ready:
                return
            try:
                await _store(self._open_store)
                self.lifecycle = ProjectLifecycle(self.store, self.app.job_store)
                key, backend = await self._backend()
                self.backends = {key: backend}
                self.usage = ProjectUsageStore(
                    self.store, self.app.delivery_store, telemetry=telemetry
                )
                self.io = ProjectIOStore(self.store, self.usage)
                self.executor = ProjectLifecycleExecutor(
                    self.lifecycle,
                    self.backends,
                    resolve_access=self._execution_access,
                )
                self.sampler = ProjectUsageSampler(
                    self.usage,
                    self.backends,
                    resolve_access=self.resolve_access,
                    enabled=True,
                )
                settings = self.app.settings
                self.maintenance = ProjectMaintenance(
                    self.lifecycle,
                    self.backends,
                    resolve_access=self._execution_access,
                    policy=ProjectPolicy(
                        idle_stop_seconds=settings.project_idle_stop_minutes * 60,
                        hibernate_seconds=settings.project_hibernate_days * DAY,
                        archive_seconds=settings.project_archive_days * DAY,
                        keep_recent=settings.project_version_keep_recent,
                    ),
                    enabled=True,
                )
                self.app.job_worker.register(KIND, self.execute, timeout_seconds=3600)
                self.ready = True
                self.app.background_tasks.start("project-usage", self.sample_forever)
                self.app.background_tasks.start(
                    "project-maintenance", self.maintain_forever
                )
            except BaseException:
                await self.close()
                raise

    def _open_store(self):
        # Assignment occurs inside the shielded operation so cancellation cannot
        # lose an already-open database connection before startup cleanup.
        self.store = ProjectStore(
            self.app.database, self.app.state_dir / "projects/versions"
        )

    async def execute(self, job):
        if not self.ready or self.closed:
            raise JobDeferred("Project runtime is not ready", 30)
        return await self.executor(job)

    async def sample_forever(self):
        await self.sampler.run(self._stop)

    async def maintain_forever(self):
        await self.maintenance.run(self._stop)

    async def close(self):
        self.ready, self.closed = False, True
        self._stop.set()
        self.io = None
        if self.store is not None:
            await _store(self.store.close)
            self.store = None
        if (
            self._vm_mounts is not None
            and self.app.sandbox_manager.project_mounts is self._vm_mounts
        ):
            self.app.sandbox_manager.project_mounts = None


def install_projects(handlers):
    if handlers.context.settings.projects_enabled:
        handlers.context.project_runtime = ProjectRuntime(handlers)
