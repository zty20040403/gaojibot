"""Persisted accounting, scoped quota admission and once-daily Outbox alerts."""

import asyncio
import hashlib
import inspect
import json
import logging
import time

from src.project_capacity import quota_rejection

from .message_ir import MessageBody, TextNode
from .project_executor import _store
from .projects import ProjectAccess, ProjectConflict, ProjectQuotaExceeded

MAX_SAMPLE_AGE = 90
logger = logging.getLogger(__name__)


class ProjectQuotaRejected(ProjectQuotaExceeded):
    def __init__(self, code):
        self.code = code
        advice = (
            "usage is unavailable or stale; remeasure before writing"
            if code in {"project_usage_unknown", "project_scope_usage_unknown"}
            else "clean dependencies, prune old versions, or ask an administrator to increase quota"
        )
        super().__init__(code + ": " + advice)


class ProjectUsageStore:
    def __init__(self, projects, deliveries=None, *, telemetry=None):
        self.projects, self.deliveries, self.telemetry = projects, deliveries, telemetry

    def record(
        self, access, project_id, observation, *, expected_version, now=None, error=""
    ):
        now = int(time.time()) if now is None else now
        with self.projects._transaction() as cursor:
            self.projects._scope_lock(cursor, access)
            project = self.projects._get(
                cursor,
                access,
                project_id,
                owner=True,
                expected=expected_version,
                allow_trash=True,
            )
            if project["lifecycle_job_id"] is not None:
                raise ProjectConflict(
                    "Usage observation raced with lifecycle maintenance"
                )
            if observation is not None:
                sample = self._validate(project, observation, now)
                if (
                    project["sampled_at"] is not None
                    and sample["sampled_at"] < project["sampled_at"]
                ):
                    raise ProjectConflict("A newer usage observation is already stored")
                cursor.execute(
                    """UPDATE projects SET used_bytes=?,rebuildable_bytes=coalesce(?,least(rebuildable_bytes,?)),
                    quota_mode=?,sampled_at=? WHERE project_id=?""",
                    (
                        sample["used_bytes"],
                        sample["rebuildable_bytes"],
                        sample["used_bytes"],
                        sample["quota_mode"],
                        sample["sampled_at"],
                        project_id,
                    ),
                )
                # Keep completed writes reserved until measured bytes replace
                # their estimate in the same scope-locked transaction.
                cursor.execute(
                    """UPDATE project_io SET usage_accounted=TRUE WHERE project_id=?
                    AND state='completed' AND mutating AND NOT usage_accounted""",
                    (project_id,),
                )
            else:
                sample = {}
                if not error:
                    raise ValueError("A failed measurement needs an error code")
            cursor.execute(
                """INSERT INTO project_usage(project_id,generation,fence,checked_at,complete,sample,error)
                VALUES (?,?,?,?,?,?::jsonb,?) ON CONFLICT(project_id) DO UPDATE SET
                generation=excluded.generation,fence=excluded.fence,checked_at=excluded.checked_at,
                complete=excluded.complete,sample=excluded.sample,error=excluded.error""",
                (
                    project_id,
                    project["workspace_generation"],
                    project["lifecycle_fence"],
                    now,
                    observation is not None,
                    json.dumps(sample),
                    error[:100] if observation is None else "",
                ),
            )
            if (
                sample
                and sample["used_bytes"] * 100 >= project["quota_bytes"] * 80
                and self.deliveries is not None
            ):
                scope_hash = hashlib.sha256(access.scope.key.encode()).hexdigest()
                percent = sample["used_bytes"] * 100 // project["quota_bytes"]
                text = (
                    f"项目 {project['title']} 已使用 {percent}% 空间 "
                    f"({sample['used_bytes'] / 1024**2:.1f}/{project['quota_bytes'] / 1024**2:.1f} MiB)。"
                    "可清理依赖、删除旧版本，或请管理员扩容。"
                )
                delivery, created = self.deliveries.enqueue(
                    idempotency_key=f"project-quota:{scope_hash}:{now // 86400}",
                    source_scope_key=access.scope.key,
                    target_scope=access.scope,
                    body=MessageBody((TextNode(0, text),)),
                    now=now,
                    transaction_cursor=cursor,
                )
                if created:
                    self.projects._event(
                        cursor,
                        project_id,
                        access,
                        "quota_warning_enqueued",
                        {"delivery_id": delivery.delivery_id},
                        now,
                    )
        return sample

    @staticmethod
    def _validate(project, sample, now):
        if (
            not isinstance(sample, dict)
            or sample.get("complete") is not True
            or sample.get("quota_mode") not in {"hard", "soft"}
            or any(
                type(sample.get(k)) is not int or sample[k] != project[field]
                for k, field in (
                    ("project_id", "project_id"),
                    ("generation", "workspace_generation"),
                    ("quota_bytes", "quota_bytes"),
                )
            )
            or type(sample.get("fence")) is not int
            or not 0 <= sample["fence"] <= project["lifecycle_fence"]
            or type(sample.get("sampled_at")) is not int
            or not now - MAX_SAMPLE_AGE <= sample["sampled_at"] <= now + 5
            or type(sample.get("used_bytes")) is not int
            or sample["used_bytes"] < 0
            or (
                sample.get("quota_mode") == "hard"
                and sample["used_bytes"] > project["quota_bytes"]
            )
            or (
                sample.get("rebuildable_bytes") is not None
                and (
                    type(sample["rebuildable_bytes"]) is not int
                    or not 0 <= sample["rebuildable_bytes"] <= sample["used_bytes"]
                )
            )
        ):
            raise ProjectConflict("Invalid, stale or foreign project usage observation")
        return {
            key: sample.get(key)
            for key in (
                "project_id",
                "generation",
                "fence",
                "quota_mode",
                "quota_bytes",
                "used_bytes",
                "rebuildable_bytes",
                "sampled_at",
                "complete",
                "warning",
            )
        }

    def require(
        self,
        access,
        project_id,
        *,
        installing_dependencies=False,
        expected_bytes=0,
        now=None,
    ):
        if type(expected_bytes) is not int or expected_bytes < 0:
            raise ValueError("Invalid project write size")
        now = int(time.time()) if now is None else now
        with self.projects._transaction() as cursor:
            self.projects._scope_lock(cursor, access)
            project = self.projects._get(cursor, access, project_id, edit=True)
            self._require(
                cursor, access, project, expected_bytes=expected_bytes,
                installing_dependencies=installing_dependencies, now=now,
            )

    def _require(
        self, cursor, access, project, *, expected_bytes=0,
        installing_dependencies=False, scope_reservation=None, now,
    ):
        """Admission and a subsequent I/O reservation share one scope transaction."""
        row = cursor.execute(
            "SELECT * FROM project_usage WHERE project_id=?", (project["project_id"],)
        ).fetchone()
        if not self._fresh(project, row, now):
            code = "project_usage_unknown"
        else:
            code = quota_rejection(
                project["used_bytes"], project["quota_bytes"],
                expected_bytes=expected_bytes,
                installing_dependencies=installing_dependencies,
            )
        if code is None:
            peers = cursor.execute(
                """SELECT p.*,u.complete,u.generation,u.fence,u.sample FROM projects p
                LEFT JOIN project_usage u USING(project_id) WHERE p.scope_key=? AND p.status<>'purged'""",
                (access.scope.key,),
            ).fetchall()
            if any(not self._fresh(peer, peer, now) for peer in peers):
                code = "project_scope_usage_unknown"
        reserved = self.projects._scope_reserved_bytes(cursor, access.scope.key)
        if (
            code is None
            and self.projects._scope_bytes(cursor, access.scope.key) + reserved
            + (expected_bytes if scope_reservation is None else scope_reservation)
            >= cursor.execute(
                "SELECT quota_bytes FROM project_scope_limits WHERE scope_key=?",
                (access.scope.key,),
            ).fetchone()[0]
        ):
            code = "project_scope_quota_full"
        if code:
            if self.telemetry is not None:
                self.telemetry.project_quota_rejections.inc()
            raise ProjectQuotaRejected(code)

    def candidates(self, *, after=0, limit=50):
        if (
            type(after) is not int
            or after < 0
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise ValueError("Invalid usage inventory page")
        with self.projects._transaction() as cursor:
            return [
                dict(row)
                for row in cursor.execute(
                    """SELECT * FROM projects WHERE project_id>? AND lifecycle_job_id IS NULL
                AND status IN ('active','stopped','hibernated','archived','trash') ORDER BY project_id LIMIT ?""",
                    (after, limit),
                ).fetchall()
            ]

    def status(self, access, project_id, *, now=None):
        now = int(time.time()) if now is None else now
        with self.projects._transaction() as cursor:
            project = self.projects._get(cursor, access, project_id, allow_trash=True)
            row = cursor.execute(
                "SELECT * FROM project_usage WHERE project_id=?", (project_id,)
            ).fetchone()
            fresh = self._fresh(project, row, now)
            versions = cursor.execute(
                """SELECT coalesce(sum(byte_size),0) FROM project_blobs b WHERE
                EXISTS(SELECT 1 FROM project_version_blobs r WHERE r.project_id=? AND r.sha256=b.sha256)""",
                (project_id,),
            ).fetchone()[0]
            return {
                "project_id": project_id,
                "quota_bytes": project["quota_bytes"],
                "quota_mode": project["quota_mode"],
                "used_bytes": project["used_bytes"] if fresh else None,
                "last_known_used_bytes": project["used_bytes"]
                if project["sampled_at"] is not None
                else None,
                "rebuildable_bytes": row["sample"].get("rebuildable_bytes")
                if fresh
                else None,
                "version_bytes": int(versions),
                "sampled_at": project["sampled_at"],
                "complete": fresh,
                "error": row["error"] if row else "not_sampled",
            }

    @staticmethod
    def _fresh(project, row, now):
        return bool(
            row
            and row["complete"]
            and project["sampled_at"] is not None
            and now - MAX_SAMPLE_AGE <= project["sampled_at"] <= now + 5
            and row["generation"] == project["workspace_generation"]
            and row["fence"] == project["lifecycle_fence"]
            and row["sample"].get("quota_bytes") == project["quota_bytes"]
        )

    def metrics(self, *, now=None):
        now = int(time.time()) if now is None else now
        with self.projects._transaction() as cursor:
            states = {
                row["status"]: row["n"]
                for row in cursor.execute(
                    "SELECT status,count(*) AS n FROM projects WHERE status<>'purged' GROUP BY status"
                ).fetchall()
            }
            rows = cursor.execute("""SELECT p.*,u.complete,u.generation,u.fence,u.sample FROM projects p
                LEFT JOIN project_usage u USING(project_id) WHERE p.status<>'purged'""").fetchall()
            version_bytes = cursor.execute("""SELECT coalesce(sum(byte_size),0) FROM project_blobs b WHERE
                EXISTS(SELECT 1 FROM project_version_blobs r WHERE r.sha256=b.sha256)""").fetchone()[
                0
            ]
            return {
                "states": states,
                "bytes": {
                    "workspace": sum(row["used_bytes"] for row in rows),
                    "rebuildable": sum(
                        row["sample"].get("rebuildable_bytes") or 0
                        for row in rows
                        if self._fresh(row, row, now)
                    ),
                    "versions": int(version_bytes),
                },
                "unknown": sum(not self._fresh(row, row, now) for row in rows),
                "rebuildable_unknown": sum(
                    not self._fresh(row, row, now)
                    or row["sample"].get("rebuildable_bytes") is None
                    for row in rows
                ),
            }


class ProjectUsageSampler:
    def __init__(
        self, store, backends, *, resolve_access, enabled=False, batch_size=50
    ):
        if (
            type(enabled) is not bool
            or type(batch_size) is not int
            or not 1 <= batch_size <= 100
        ):
            raise ValueError("Invalid project usage sampler configuration")
        self.store, self.backends = store, dict(backends)
        self.resolve_access, self.enabled, self.batch_size = (
            resolve_access,
            enabled,
            batch_size,
        )
        self._after, self._lock = 0, asyncio.Lock()

    async def _access(self, project):
        access = self.resolve_access(project, project["creator_principal_id"])
        access = await access if inspect.isawaitable(access) else access
        if (
            not isinstance(access, ProjectAccess)
            or not access.member
            or access.principal_id != project["creator_principal_id"]
            or access.scope.key != project["scope_key"]
        ):
            raise PermissionError("Usage sampling needs the live project owner")
        return access

    async def run_once(self):
        if not self.enabled:
            return []
        async with self._lock:
            projects = await _store(
                self.store.candidates, after=self._after, limit=self.batch_size
            )
            self._after = (
                projects[-1]["project_id"] if len(projects) == self.batch_size else 0
            )
            result = []
            for project in projects:
                try:
                    try:
                        async with asyncio.timeout(30):
                            await self._access(project)
                            sample = await self.backends[
                                project["backend"]
                            ].sample_usage(project)
                    except Exception as exc:
                        access = await self._access(project)
                        await _store(
                            self.store.record,
                            access,
                            project["project_id"],
                            None,
                            expected_version=project["version"],
                            error=type(exc).__name__,
                        )
                        raise
                    access = await self._access(project)
                    await _store(
                        self.store.record,
                        access,
                        project["project_id"],
                        sample,
                        expected_version=project["version"],
                    )
                    result.append(
                        {"project_id": project["project_id"], "sampled": True}
                    )
                except Exception as exc:
                    result.append(
                        {
                            "project_id": project["project_id"],
                            "error": type(exc).__name__,
                        }
                    )
            if self.store.telemetry is not None:
                self.store.telemetry.update_project_gauges(
                    await _store(self.store.metrics)
                )
            return result

    async def run(self, stop):
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception as exc:
                logger.warning("Project usage sampling failed: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=30)
            except TimeoutError:
                pass
