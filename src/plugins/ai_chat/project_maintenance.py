"""Opt-in host scheduler: metadata retention and durable idle transitions.

No automatic purge, direct container mutation or fabricated member identity.
Backend capability and live identity adapters must be supplied by the host.
"""

from __future__ import annotations

import asyncio
from functools import partial
import inspect
import logging
import time

from .project_executor import ProjectBackendUnavailable, _store
from .project_policy import ProjectPolicy
from .projects import ProjectAccess, ProjectConflict


logger = logging.getLogger(__name__)


class ProjectMaintenance:
    def __init__(
        self,
        lifecycle,
        backends,
        *,
        resolve_access,
        policy=None,
        enabled=False,
        batch_size=50,
        interval_seconds=60,
    ):
        if (
            type(batch_size) is not int
            or not 1 <= batch_size <= 100
            or type(interval_seconds) is not int
            or interval_seconds < 1
            or type(enabled) is not bool
        ):
            raise ValueError("Invalid project maintenance configuration")
        self.policy = policy if policy is not None else ProjectPolicy()
        if not isinstance(self.policy, ProjectPolicy):
            raise TypeError("Project maintenance requires a ProjectPolicy")
        self.lifecycle, self.projects = lifecycle, lifecycle.projects
        self.backends, self.resolve_access = dict(backends), resolve_access
        self.enabled, self.batch_size = enabled, batch_size
        self.interval_seconds = interval_seconds
        self._after = 0
        self._gc_at = None
        self.gc_status = None
        self._lock = asyncio.Lock()

    async def _access(self, project):
        access = self.resolve_access(project, project["creator_principal_id"])
        access = await access if inspect.isawaitable(access) else access
        if (
            not isinstance(access, ProjectAccess)
            or not access.member
            or access.scope.key != project["scope_key"]
            or access.principal_id != project["creator_principal_id"]
        ):
            raise PermissionError(
                "Maintenance requires the current host-resolved project owner"
            )
        return access

    async def _maintain(self, candidate, now):
        backend = self.backends.get(candidate["backend"])
        if backend is None or not callable(getattr(backend, "maintenance_ready", None)):
            raise ProjectBackendUnavailable(
                "Project maintenance backend is unavailable"
            )
        async with asyncio.timeout(30):
            access = await self._access(candidate)
            project = await _store(self.projects.get, access, candidate["project_id"])
            if not await backend.maintenance_ready(project):
                raise ProjectConflict(
                    "Project has active commands or unverified workspace state"
                )
            # Identity can change while observing the helper. The store also checks
            # scope, owner, version, locks and unresolved delivery in its transaction.
            access = await self._access(project)
        removed = await _store(
            self.projects.prune_versions,
            access,
            project["project_id"],
            partial(
                self.policy.retain_versions, archived=project["status"] == "archived"
            ),
            expected_version=project["version"],
            now=now,
            idle_guard=self.lifecycle._idle,
        )
        project = await _store(self.projects.get, access, project["project_id"])
        action = self.policy.idle_action(project, now=now)
        result = {
            "project_id": project["project_id"],
            "pruned": len(removed),
            "action": None,
        }
        if action is not None:
            run = await _store(
                self.lifecycle.request,
                access,
                project["project_id"],
                action,
                expected_version=project["version"],
                request_key=f"idle:{action}:{project['version']}:{project['last_active_at']}",
                now=now,
                idle_policy=self.policy,
                preflight=backend.preflight,
            )
            result.update(action=action, job_id=run["job_id"])
        return result

    async def run_once(self, *, now=None):
        if not self.enabled:
            return []
        async with self._lock:
            now = int(time.time()) if now is None else now
            rows = await _store(
                self.lifecycle.maintenance_candidates,
                after=self._after,
                limit=self.batch_size,
            )
            self._after = rows[-1]["project_id"] if len(rows) == self.batch_size else 0
            results = []
            for project in rows:
                try:
                    results.append(await self._maintain(project, now))
                except (
                    ProjectConflict,
                    PermissionError,
                    ProjectBackendUnavailable,
                ) as exc:
                    results.append(
                        {
                            "project_id": project["project_id"],
                            "skipped": type(exc).__name__,
                        }
                    )
                except Exception as exc:
                    # Do not leak helper responses, credentials or another scope's
                    # content through a batch failure. One broken project cannot
                    # stop the rest of the inventory from being processed.
                    logger.warning(
                        "Project %s maintenance failed: %s",
                        project["project_id"],
                        type(exc).__name__,
                    )
                    results.append(
                        {
                            "project_id": project["project_id"],
                            "failed": type(exc).__name__,
                        }
                    )
            if self._gc_at is None or now >= self._gc_at + 3600:
                self._gc_at = now
                try:
                    deleted = await _store(
                        self.projects.collect_blobs, now=now, limit=100
                    )
                    self.gc_status = {"checked_at": now, "deleted": len(deleted)}
                except Exception as exc:
                    logger.warning(
                        "Project object collection failed: %s", type(exc).__name__
                    )
                    self.gc_status = {"checked_at": now, "failed": type(exc).__name__}
            return results

    async def run(self, stop):
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception as exc:
                logger.warning(
                    "Project maintenance scan failed: %s", type(exc).__name__
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass
