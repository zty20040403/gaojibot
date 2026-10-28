"""Recover end-of-turn snapshots without replaying user commands."""

import asyncio
import logging
import time

from .project_executor import _store
from .project_io import ProjectIOGrant
from .project_turn_saves import ProjectTurnSaves
from .projects import ProjectConflict

logger = logging.getLogger(__name__)


class ProjectAutosave:
    def __init__(self, runtime):
        self.runtime = runtime
        self.saves = ProjectTurnSaves(runtime.store)
        self._lock = asyncio.Lock()

    async def save_one(self, candidate, now):
        runtime = self.runtime
        pid, turn_id = candidate["project_id"], candidate["turn_id"]
        actor = candidate["actor_principal_id"]
        access = await runtime._execution_access(candidate, actor)
        operations = await _store(self.saves.operations, access, pid, turn_id)
        for row in operations:
            grant = ProjectIOGrant(row["operation_id"], pid, row["token"])
            if row["state"] == "reserved":
                # The terminal turn cannot dispatch this admission any more.
                await _store(runtime.io.abort, grant, now=now)
            elif row["state"] in {"dispatched", "needs_attention"}:
                if row["result"] is None:
                    await _store(
                        self.saves.defer, pid, turn_id, "unknown_execution", now
                    )
                    return
                project = await _store(runtime.store.get, access, pid)
                evidence = await runtime.backends[project["backend"]].io_evidence(
                    project
                )
                await _store(
                    runtime.io.complete, grant, row["result"], evidence, now=now
                )
        if await _store(self.saves.skip_unchanged, access, pid, turn_id, now):
            return
        project = await _store(runtime.store.get, access, pid)
        backend = runtime.backends[project["backend"]]
        if not await backend.maintenance_ready(project):
            raise ProjectConflict("Project has unverified workspace state")
        access = await runtime._execution_access(project, actor)
        await _store(
            runtime.lifecycle.request,
            access,
            pid,
            "snapshot",
            expected_version=project["version"],
            request_key=f"turn-save:{turn_id}",
            source_turn_id=turn_id,
            now=now,
        )

    async def run_once(self, *, now=None):
        async with self._lock:
            now = int(time.time()) if now is None else now
            rows = await _store(self.saves.due, now)
            for candidate in rows:
                try:
                    async with asyncio.timeout(30):
                        await self.save_one(candidate, now)
                except Exception as exc:
                    code = (
                        "permission_unavailable"
                        if isinstance(exc, PermissionError)
                        else "waiting_for_workspace"
                        if isinstance(exc, ProjectConflict)
                        else "retry_pending"
                    )
                    await _store(
                        self.saves.defer,
                        candidate["project_id"],
                        candidate["turn_id"],
                        code,
                        now,
                    )
                    logger.info(
                        "Project %s automatic save deferred: %s",
                        candidate["project_id"],
                        type(exc).__name__,
                    )
            return len(rows)

    async def run(self, stop):
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception as exc:
                logger.warning(
                    "Project automatic save scan failed: %s", type(exc).__name__
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=10)
            except TimeoutError:
                pass
