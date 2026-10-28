"""Host-only project metadata administration; never impersonates a chat member.

HTTP authentication is supplied by the protected admin router. No file bytes,
browser profiles, private lifecycle reservations or task envelopes are exposed.
"""

import json
import time

from .project_lifecycle import ProjectLifecycle
from .project_usage import ProjectUsageStore
from .projects import ProjectConflict, ProjectQuotaExceeded


FIELDS = ("project_id", "scope_key", "creator_principal_id", "bot_native_user_id", "title", "status", "backend",
          "quota_bytes", "quota_mode", "pinned", "last_active_at", "updated_at", "version", "locked_task_id",
          "lifecycle_job_id", "workspace_generation")


class ProjectAdmin:
    def __init__(self, store, control):
        self.store, self.control = store, control

    @staticmethod
    def _version(cursor):
        row = cursor.execute("SELECT version FROM admin_resource_versions WHERE resource_key='projects'").fetchone()
        return row[0] if row else 0

    @staticmethod
    def _project(cursor, project_id):
        row = cursor.execute("SELECT * FROM projects WHERE project_id=? AND status<>'purged'", (project_id,)).fetchone()
        if row is None:
            raise LookupError("项目不存在或已经清除")
        return dict(row)

    def _item(self, cursor, project, now):
        usage = cursor.execute("SELECT * FROM project_usage WHERE project_id=?", (project["project_id"],)).fetchone()
        fresh = ProjectUsageStore._fresh(project, usage, now)
        version_bytes = cursor.execute("""SELECT coalesce(sum(b.byte_size),0) FROM project_blobs b
            WHERE EXISTS(SELECT 1 FROM project_version_blobs v WHERE v.project_id=? AND v.sha256=b.sha256)""",
            (project["project_id"],)).fetchone()[0]
        return {**{key: project[key] for key in FIELDS},
                "used_bytes": project["used_bytes"] if fresh else None,
                "last_known_used_bytes": project["used_bytes"] if project["sampled_at"] is not None else None,
                "rebuildable_bytes": project["rebuildable_bytes"] if fresh else None,
                "sampled_at": project["sampled_at"], "usage_fresh": fresh,
                "usage_error": usage["error"] if usage else "not_sampled",
                "version_bytes": int(version_bytes),
                "quota_editable": project["workspace_generation"] == 0 and project["status"] == "stopped"
                    and project["sandbox_id"] is None and project["lifecycle_job_id"] is None
                    and project["locked_task_id"] is None}

    def list(self, *, query="", state="", scope="", offset=0, limit=5):
        with self.store._transaction() as cursor:
            where = """status<>'purged' AND (?='' OR status=?) AND (?='' OR scope_key=?)
                AND (?='' OR position(lower(?) in lower(title || ' ' || scope_key || ' prj#' || project_id::text))>0)"""
            args = (state, state, scope, scope, query, query)
            total = cursor.execute("SELECT count(*) FROM projects WHERE " + where, args).fetchone()[0]
            rows = cursor.execute("SELECT * FROM projects WHERE " + where +
                                  " ORDER BY updated_at DESC,project_id DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
            counts = {row["status"]: row["n"] for row in cursor.execute(
                "SELECT status,count(*) AS n FROM projects WHERE status<>'purged' GROUP BY status").fetchall()}
            return {"resource": "projects", "resource_version": self._version(cursor), "api_version": "v1",
                    "counts": counts, "total": total, "offset": offset, "limit": limit,
                    "items": [self._item(cursor, row, int(time.time())) for row in rows]}

    def detail(self, project_id, *, section="versions", offset=0, limit=5):
        tables = {
            "versions": ("project_versions", "version_no,label,pinned,file_count,total_bytes,source_task_id,created_at", "version_no"),
            "previews": ("project_previews", "preview_id,version_no,root,expires_at,revoked_at,created_at", "created_at DESC,preview_id"),
            "events": ("project_events", "event_id,kind,actor_principal_id,created_at", "event_id"),
        }
        table, fields, order = tables[section]
        with self.store._transaction() as cursor:
            project = self._project(cursor, project_id)
            limits = dict(cursor.execute("SELECT * FROM project_scope_limits WHERE scope_key=?", (project["scope_key"],)).fetchone())
            limits["accounted_bytes"] = self.store._scope_bytes(cursor, project["scope_key"])
            limits["reserved_bytes"] = self.store._scope_reserved_bytes(cursor, project["scope_key"])
            limits["project_count"] = cursor.execute("SELECT count(*) FROM projects WHERE scope_key=? AND status<>'purged'",
                                                     (project["scope_key"],)).fetchone()[0]
            total = cursor.execute(f"SELECT count(*) FROM {table} WHERE project_id=?", (project_id,)).fetchone()[0]
            rows = cursor.execute(f"SELECT {fields} FROM {table} WHERE project_id=? ORDER BY {order} DESC LIMIT ? OFFSET ?",
                                  (project_id, limit, offset)).fetchall()
            run = cursor.execute("""SELECT r.job_id,r.action,r.status,r.next_step,r.plan,j.status AS job_status,j.updated_at
                FROM project_lifecycle_runs r JOIN durable_jobs j USING(job_id) WHERE r.job_id=?""",
                (project["lifecycle_job_id"],)).fetchone()
            operation = None
            if run:
                from .project_browser_drain import pending
                operation = {key: run[key] for key in ("job_id", "action", "status", "job_status", "updated_at")}
                operation.update(completed_steps=run["next_step"], total_steps=len(run["plan"]["steps"]),
                                 phase="browser_drain" if pending(run["plan"]) else "lifecycle")
            return {"resource": "projects", "resource_version": self._version(cursor), "api_version": "v1",
                    "project": self._item(cursor, project, int(time.time())), "scope_limits": limits,
                    "operation": operation, "section": section, "items": [dict(row) for row in rows],
                    "total": total, "offset": offset, "limit": limit}

    def mutate(self, project_id, *, action, expected_version, resource_version, actor, values):
        with self.store._transaction() as cursor:
            project = self._project(cursor, project_id)
            scope = project["scope_key"]
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(?,0))", ("project-scope:" + scope,))

            def write():
                row = cursor.execute("SELECT * FROM projects WHERE project_id=? FOR UPDATE", (project_id,)).fetchone()
                if row["status"] == "purged" or row["version"] != expected_version:
                    raise ProjectConflict("项目已变更，请载入最新配置后重试")
                now, before = int(time.time()), {key: row[key] for key in FIELDS}
                result = {"project_id": project_id}
                if action in {"pin", "quota"}:
                    if row["lifecycle_job_id"] is not None or row["locked_task_id"] is not None:
                        raise ProjectConflict("项目正在执行任务或状态转换，暂不能修改")
                    ProjectLifecycle._idle(cursor, row)
                if action == "pin":
                    cursor.execute("UPDATE projects SET pinned=? WHERE project_id=?", (values["pinned"], project_id))
                    result["pinned"] = values["pinned"]
                elif action == "quota":
                    if row["workspace_generation"] != 0 or row["sandbox_id"] is not None or row["status"] != "stopped":
                        raise ProjectConflict("已有工作区需要真实磁盘扩缩容，不能只修改配额数字；此入口暂未开放")
                    quota = values["quota_bytes"]
                    limits = cursor.execute("SELECT quota_bytes FROM project_scope_limits WHERE scope_key=?", (scope,)).fetchone()[0]
                    retained = cursor.execute("SELECT coalesce(max(total_bytes),0) FROM project_versions WHERE project_id=?", (project_id,)).fetchone()[0]
                    if quota > limits or quota < max(retained, row["used_bytes"]):
                        raise ProjectQuotaExceeded("项目配额超出会话额度，或小于已保留数据")
                    cursor.execute("UPDATE projects SET quota_bytes=? WHERE project_id=?", (quota, project_id))
                    result["quota_bytes"] = quota
                elif action == "scope_limits":
                    limits = cursor.execute("SELECT * FROM project_scope_limits WHERE scope_key=? FOR UPDATE", (scope,)).fetchone()
                    before = dict(limits)
                    if limits["version"] != values["scope_version"]:
                        raise ProjectConflict("会话额度已变更，请载入最新配置后重试")
                    if values["quota_bytes"] < limits["quota_bytes"]:
                        for child in cursor.execute("SELECT * FROM projects WHERE scope_key=? AND status<>'purged'",
                                                    (scope,)).fetchall():
                            if child["lifecycle_job_id"] is not None or child["locked_task_id"] is not None:
                                raise ProjectConflict("会话有正在执行的项目，不能缩小额度")
                            ProjectLifecycle._idle(cursor, child)
                            measured = cursor.execute("SELECT * FROM project_usage WHERE project_id=?", (child["project_id"],)).fetchone()
                            if child["workspace_generation"] and not ProjectUsageStore._fresh(child, measured, now):
                                raise ProjectConflict("会话用量尚未重新核实，不能缩小额度")
                    charged = self.store._scope_bytes(cursor, scope) + self.store._scope_reserved_bytes(cursor, scope)
                    if charged > values["quota_bytes"]:
                        raise ProjectQuotaExceeded("会话额度不能小于已计量与预留数据")
                    count = cursor.execute("SELECT count(*) FROM projects WHERE scope_key=? AND status<>'purged'", (scope,)).fetchone()[0]
                    if count > values["max_projects"]:
                        raise ProjectConflict("项目数量上限不能低于当前数量")
                    cursor.execute("""UPDATE project_scope_limits SET quota_bytes=?,max_projects=?,version=version+1
                        WHERE scope_key=?""", (values["quota_bytes"], values["max_projects"], scope))
                    result.update(scope_key=scope, quota_bytes=values["quota_bytes"], max_projects=values["max_projects"],
                                  scope_version=limits["version"] + 1)
                elif action == "version_pin":
                    updated = cursor.execute("""UPDATE project_versions SET pinned=? WHERE project_id=? AND version_no=?
                        RETURNING version_no,pinned""", (values["pinned"], project_id, values["version_no"])).fetchone()
                    if updated is None:
                        raise LookupError("版本不存在")
                    result.update(dict(updated))
                elif action == "preview_revoke":
                    updated = cursor.execute("""UPDATE project_previews SET revoked_at=coalesce(revoked_at,?)
                        WHERE project_id=? AND preview_id=? RETURNING preview_id,revoked_at""",
                        (now, project_id, values["preview_id"])).fetchone()
                    if updated is None:
                        raise LookupError("预览不属于这个项目或已经不存在")
                    result.update(dict(updated))
                else:
                    raise ValueError("Unsupported project administration action")
                cursor.execute("UPDATE projects SET version=version+1,updated_at=? WHERE project_id=?", (now, project_id))
                result["version"] = row["version"] + 1
                cursor.execute("""INSERT INTO project_events(project_id,kind,payload,created_at)
                    VALUES (?,?,?::jsonb,?)""", (project_id, "admin_" + action,
                    json.dumps({"actor": actor, **result}), now))
                return before, result

            return self.control.mutate_transaction(cursor, "projects", expected_version=resource_version,
                actor=actor, action="project." + action, target=f"prj#{project_id}", operation=write)
