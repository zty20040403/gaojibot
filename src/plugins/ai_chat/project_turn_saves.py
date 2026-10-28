"""Database-only turn ownership and automatic project save receipts."""

import json

from .projects import ProjectConflict


def require_running_turn(cursor, access, turn_id, native_message_id=None):
    if type(turn_id) is not int or turn_id <= 0:
        raise ProjectConflict("Project writes require a durable host turn")
    row = cursor.execute(
        """SELECT t.status,t.scope_key,m.sender_principal_id,m.direction,
        m.native_message_id,c.scope_key AS message_scope FROM agent_turns t
        JOIN messages m ON m.canonical_message_id=t.trigger_canonical_message_id
        JOIN conversations c USING(conversation_id) WHERE t.turn_id=? FOR SHARE OF t""",
        (turn_id,),
    ).fetchone()
    if (
        row is None
        or row["status"] != "running"
        or row["direction"] != "inbound"
        or row["scope_key"] != access.scope.key
        or row["message_scope"] != access.scope.key
        or row["sender_principal_id"] != access.principal_id
        or (
            native_message_id is not None
            and row["native_message_id"] != str(native_message_id)
        )
    ):
        raise ProjectConflict(
            "Project turn does not match the current actor and message"
        )


def require_finished_save(cursor, access, project_id, turn_id):
    row = cursor.execute(
        """SELECT s.*,t.status AS turn_status,t.finished_at,t.scope_key
        FROM project_turn_saves s JOIN agent_turns t USING(turn_id)
        WHERE s.project_id=? AND s.turn_id=? FOR UPDATE OF s""",
        (project_id, turn_id),
    ).fetchone()
    if (
        row is None
        or row["state"] != "pending"
        or row["turn_status"] == "running"
        or row["finished_at"] is None
        or row["scope_key"] != access.scope.key
        or row["actor_principal_id"] != access.principal_id
    ):
        raise ProjectConflict("No finished project turn is ready to save")
    if cursor.execute(
        """SELECT 1 FROM project_io io JOIN agent_turns t ON t.turn_id=io.journal_turn_id
        WHERE io.project_id=? AND (t.status='running'
        OR io.state IN ('reserved','dispatched','needs_attention')) LIMIT 1""",
        (project_id,),
    ).fetchone():
        raise ProjectConflict("Project turns or effects are still unresolved")
    return bool(
        cursor.execute(
            """SELECT 1 FROM project_io WHERE project_id=? AND journal_turn_id=?
        AND mutating AND state='completed' LIMIT 1""",
            (project_id, turn_id),
        ).fetchone()
    )


class ProjectTurnSaves:
    def __init__(self, projects):
        self.projects = projects

    def due(self, now, *, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid automatic save batch")
        with self.projects._transaction() as cursor:
            rows = cursor.execute(
                """SELECT p.*,s.turn_id,s.actor_principal_id FROM project_turn_saves s
                JOIN projects p USING(project_id) JOIN agent_turns t USING(turn_id)
                WHERE s.state='pending' AND s.next_attempt_at<=? AND t.status<>'running'
                AND t.finished_at IS NOT NULL ORDER BY s.next_attempt_at,s.created_at,s.turn_id
                LIMIT ? FOR UPDATE OF s SKIP LOCKED""",
                (now, limit),
            ).fetchall()
            for row in rows:
                cursor.execute(
                    "UPDATE project_turn_saves SET next_attempt_at=? WHERE project_id=? AND turn_id=?",
                    (now + 30, row["project_id"], row["turn_id"]),
                )
            return [dict(row) for row in rows]

    def operations(self, access, project_id, turn_id):
        with self.projects._transaction() as cursor:
            self.projects._scope_lock(cursor, access)
            project = self.projects._get(cursor, access, project_id)
            self.projects._editor(cursor, access, project)
            return [
                dict(row)
                for row in cursor.execute(
                    """SELECT io.* FROM project_io io JOIN agent_turns t ON t.turn_id=io.journal_turn_id
                WHERE io.project_id=? AND io.journal_turn_id=? AND io.actor_principal_id=?
                AND t.status<>'running' AND t.finished_at IS NOT NULL
                ORDER BY operation_id""",
                    (project_id, turn_id, access.principal_id),
                ).fetchall()
            ]

    def skip_unchanged(self, access, project_id, turn_id, now):
        with self.projects._transaction() as cursor:
            self.projects._scope_lock(cursor, access)
            self.projects._get(cursor, access, project_id, edit=True)
            if require_finished_save(cursor, access, project_id, turn_id):
                return False
            cursor.execute(
                """UPDATE project_turn_saves SET state='skipped',error_code='',updated_at=?
                WHERE project_id=? AND turn_id=?""",
                (now, project_id, turn_id),
            )
            return True

    def defer(self, project_id, turn_id, code, now):
        if code not in {
            "waiting_for_workspace",
            "unknown_execution",
            "permission_unavailable",
            "retry_pending",
        }:
            raise ValueError("Invalid automatic save reason")
        with self.projects._transaction() as cursor:
            cursor.execute(
                """UPDATE project_turn_saves SET error_code=?,updated_at=?,next_attempt_at=?
                WHERE project_id=? AND turn_id=? AND state='pending'""",
                (code, now, now + 30, project_id, turn_id),
            )

    def status(self, access, project_id):
        with self.projects._transaction() as cursor:
            self.projects._get(cursor, access, project_id)
            rows = cursor.execute(
                """SELECT s.*,t.turn_ordinal,r.status AS run_status,j.status AS job_status,
                j.result_json,greatest(s.updated_at,r.updated_at,j.updated_at) AS latest_update
                FROM project_turn_saves s JOIN agent_turns t USING(turn_id)
                LEFT JOIN project_lifecycle_runs r USING(job_id) LEFT JOIN durable_jobs j USING(job_id)
                WHERE s.project_id=? ORDER BY s.created_at DESC,s.turn_id DESC LIMIT 5""",
                (project_id,),
            ).fetchall()
            result = []
            for row in rows:
                saved = (
                    row["run_status"] == "completed"
                    and row["job_status"] == "succeeded"
                )
                completion = json.loads(row["result_json"]) if saved else {}
                version = completion.get("saved_snapshot", completion.get("snapshot", {})).get("version_no")
                saved = saved and type(version) is int
                state = row["state"]
                if state == "queued":
                    state = row["job_status"]
                    if saved:
                        state = "completed"
                    elif row["run_status"] == "needs_attention":
                        state = "needs_attention"
                    elif state == "pending":
                        state = "queued"
                result.append(
                    {
                        "turn": f"t#{row['turn_ordinal']}",
                        "status": state,
                        "saved": saved,
                        "version": f"v{version}" if saved else None,
                        "reason": row["error_code"],
                        "updated_at": row["latest_update"],
                    }
                )
            return result
