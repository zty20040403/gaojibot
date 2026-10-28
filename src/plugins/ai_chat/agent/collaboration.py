"""Task-local edit grants and feedback; never a grant to execute as the owner."""

from __future__ import annotations

import json
import time


MAX_COLLABORATORS = 20
MAX_PENDING_FEEDBACK = 10


class TaskCollaborationStoreMixin:
    def revisable_tasks(self, *, scope_key, requester_user_id, limit=5):
        with self._lock:
            rows = self._connection.execute("""SELECT t.* FROM subagent_tasks t
                JOIN subagent_controls c ON c.task_id=t.task_id
                WHERE t.scope_key=? AND c.dispatch_json<>'{}'
                AND (t.requester_user_id=? OR EXISTS (
                    SELECT 1 FROM subagent_collaborators a JOIN principal_identities i ON i.principal_id=a.principal_id
                    WHERE a.task_id=t.task_id AND a.enabled AND i.platform='onebot-v11' AND i.native_user_id=?))
                ORDER BY c.updated_at DESC,t.task_id DESC LIMIT ?""",
                (scope_key, requester_user_id, str(requester_user_id), min(max(limit, 1), 20))).fetchall()
        return [self._task_row(row) for row in rows]

    def execution_model_policy(self, task_id):
        task = self.get(task_id)
        if task and task.endpoint is not None:
            control = self.control(task_id)
            if control["revision"] != 1 or control["policy"] != {"mode": "auto"}:
                raise PermissionError("Official task policy is unavailable")
            return {"mode": "auto"}
        # Per-task overrides selected by the owner do not transfer to a collaborator.
        return self.control(task_id)["policy"] if task and self.revision_actor(task_id) == task.requester_user_id else {"mode": "auto"}

    def collaboration_snapshot(self, task_id):
        """Read-only admin projection; never exposes execution events or private context."""
        task = self.get(task_id)
        if task is None:
            raise ValueError("Task no longer exists")
        if task.endpoint is not None:
            return {"creator_user_id": None, "execution_user_id": None,
                    "creator_actor_key": task.requester_actor.key,
                    "endpoint_key": task.endpoint.key, "supported": False,
                    "members": [], "feedback": []}
        with self._lock:
            members = self._connection.execute("""SELECT a.principal_id,a.enabled,a.grant_version,
                    a.updated_at,i.native_user_id
                FROM subagent_collaborators a JOIN principal_identities i ON i.principal_id=a.principal_id
                WHERE a.task_id=? AND i.platform='onebot-v11'
                ORDER BY a.enabled DESC,a.updated_at DESC,a.principal_id LIMIT 50""", (task_id,)).fetchall()
            feedback = self._connection.execute("""SELECT f.feedback_id,f.principal_id,f.instruction,
                    f.status,f.base_revision,f.consumed_revision,f.created_at,f.updated_at,i.native_user_id
                FROM subagent_revision_feedback f JOIN principal_identities i ON i.principal_id=f.principal_id
                WHERE f.task_id=? AND i.platform='onebot-v11'
                ORDER BY (f.status='pending') DESC,f.feedback_id DESC LIMIT 50""", (task_id,)).fetchall()
        return {"creator_user_id": task.requester_user_id, "execution_user_id": self.revision_actor(task_id),
                "members": [dict(row) for row in members], "feedback": [dict(row) for row in feedback]}

    def _collaboration_lock(self, cursor, task_id, scope_key, user_id):
        # Match revise(): controls before tasks. Revocation and revision share a fence.
        control = cursor.execute("SELECT version,revision FROM subagent_controls WHERE task_id=? FOR UPDATE",
                                 (task_id,)).fetchone()
        task = cursor.execute("SELECT scope_key,requester_user_id FROM subagent_tasks WHERE task_id=? FOR UPDATE",
                              (task_id,)).fetchone()
        if task is None or task["scope_key"] != scope_key or control is None:
            raise ValueError("Task is unavailable in this conversation")
        if not scope_key.startswith("onebot-v11:group:"):
            raise ValueError("Task collaboration is only available in the original QQ group")
        if cursor.execute("SELECT 1 FROM routine_children WHERE task_id=?", (task_id,)).fetchone():
            raise ValueError("Scheduled child tasks cannot be delegated to collaborators")
        identity = cursor.execute("""SELECT principal_id FROM principal_identities
            WHERE platform='onebot-v11' AND native_user_id=?""", (str(user_id),)).fetchone()
        if identity is None:
            raise ValueError("Current user has no verified chat identity")
        principal_id = int(identity["principal_id"])
        owner = int(task["requester_user_id"]) == user_id
        if not owner and not cursor.execute("""SELECT 1 FROM subagent_collaborators
                WHERE task_id=? AND principal_id=? AND enabled""", (task_id, principal_id)).fetchone():
            raise ValueError("Only the task creator and authorized collaborators may contribute")
        return dict(control), principal_id, owner

    @staticmethod
    def _collaboration_version(cursor, task_id, control, timestamp):
        cursor.execute("UPDATE subagent_controls SET version=version+1,updated_at=? WHERE task_id=? AND version=?",
                       (timestamp, task_id, control["version"]))
        if cursor.rowcount != 1:
            raise ValueError("Task version changed; refresh before editing")
        return int(control["version"]) + 1

    def set_collaborator(self, task_id, *, scope_key, requester_user_id, principal_id,
                         enabled, expected_version, now=None):
        if type(enabled) is not bool or type(principal_id) is not int or principal_id <= 0:
            raise ValueError("A principal ID and boolean grant state are required")
        timestamp = int(time.time() if now is None else now)
        with self._transaction() as cursor:
            control, actor, owner = self._collaboration_lock(cursor, task_id, scope_key, requester_user_id)
            if not owner:
                raise ValueError("Only the task creator may change collaborators")
            if control["version"] != expected_version:
                raise ValueError("Task version changed; refresh before editing")
            if principal_id == actor:
                raise ValueError("The task creator already owns the task")
            if not cursor.execute("SELECT 1 FROM principal_identities WHERE platform='onebot-v11' AND principal_id=?",
                                  (principal_id,)).fetchone():
                raise ValueError("Collaborator must have a verified QQ identity")
            existing = cursor.execute("SELECT enabled FROM subagent_collaborators WHERE task_id=? AND principal_id=?",
                                      (task_id, principal_id)).fetchone()
            if (existing is not None and bool(existing["enabled"]) == enabled) or (existing is None and not enabled):
                return {"version": int(control["version"]), "changed": False}
            if enabled:
                count = cursor.execute("SELECT COUNT(*) AS n FROM subagent_collaborators WHERE task_id=? AND enabled",
                                       (task_id,)).fetchone()["n"]
                if count >= MAX_COLLABORATORS:
                    raise ValueError("This task already has 20 collaborators")
            cursor.execute("""INSERT INTO subagent_collaborators
                (task_id,principal_id,added_by,enabled,created_at,updated_at) VALUES (?,?,?,?,?,?)
                ON CONFLICT (task_id,principal_id) DO UPDATE SET enabled=excluded.enabled,
                added_by=excluded.added_by,updated_at=excluded.updated_at,
                grant_version=subagent_collaborators.grant_version+1""",
                (task_id, principal_id, actor, enabled, timestamp, timestamp))
            if not enabled:
                cursor.execute("""UPDATE subagent_revision_feedback SET status='dismissed',updated_at=?
                    WHERE task_id=? AND principal_id=? AND status='pending'""", (timestamp, task_id, principal_id))
            version = self._collaboration_version(cursor, task_id, control, timestamp)
            self.append_event(task_id, "task.collaborator_changed", {"principal_id": principal_id,
                "actor_principal_id": actor, "enabled": enabled, "version": version},
                now=timestamp, transaction_cursor=cursor)
        self._notify_changed(task_id)
        return {"version": version, "changed": True}

    def add_revision_feedback(self, task_id, *, scope_key, requester_user_id, instruction,
                              request_key, expected_version, now=None):
        if not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 1000:
            raise ValueError("Feedback must contain 1-1000 characters")
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 160:
            raise ValueError("A bounded host-issued feedback request key is required")
        instruction = instruction.strip()
        timestamp = int(time.time() if now is None else now)
        with self._transaction() as cursor:
            control, actor, _ = self._collaboration_lock(cursor, task_id, scope_key, requester_user_id)
            existing = cursor.execute("""SELECT * FROM subagent_revision_feedback
                WHERE task_id=? AND principal_id=? AND request_key=?""", (task_id, actor, request_key)).fetchone()
            if existing is not None:
                if existing["instruction"] != instruction:
                    raise ValueError("Feedback request key was already used for different text")
                return {"version": int(control["version"]), "feedback": dict(existing), "created": False}
            if control["version"] != expected_version:
                raise ValueError("Task version changed; refresh before adding feedback")
            pending = cursor.execute("SELECT COUNT(*) AS n FROM subagent_revision_feedback WHERE task_id=? AND status='pending'",
                                     (task_id,)).fetchone()["n"]
            if pending >= MAX_PENDING_FEEDBACK:
                raise ValueError("Merge or dismiss the 10 pending comments before adding more")
            row = cursor.execute("""INSERT INTO subagent_revision_feedback
                (task_id,principal_id,request_key,instruction,base_revision,status,created_at,updated_at)
                VALUES (?,?,?,?,?,'pending',?,?) RETURNING *""",
                (task_id, actor, request_key, instruction, control["revision"], timestamp, timestamp)).fetchone()
            version = self._collaboration_version(cursor, task_id, control, timestamp)
            self.append_event(task_id, "task.feedback_added", {"feedback_id": row["feedback_id"],
                "actor_principal_id": actor, "version": version}, now=timestamp, transaction_cursor=cursor)
        self._notify_changed(task_id)
        return {"version": version, "feedback": dict(row), "created": True}

    def collaboration(self, task_id, *, scope_key, requester_user_id):
        with self._transaction() as cursor:
            control, actor, owner = self._collaboration_lock(cursor, task_id, scope_key, requester_user_id)
            members = cursor.execute("""SELECT principal_id,enabled,grant_version,added_by,created_at,updated_at
                FROM subagent_collaborators WHERE task_id=? ORDER BY created_at,principal_id""", (task_id,)).fetchall()
            feedback = cursor.execute("""SELECT feedback_id,principal_id,instruction,base_revision,status,
                consumed_revision,created_at,updated_at FROM subagent_revision_feedback
                WHERE task_id=? ORDER BY feedback_id DESC LIMIT 50""", (task_id,)).fetchall()
        return {"version": int(control["version"]), "revision": int(control["revision"]),
                "actor_principal_id": actor, "owner": owner,
                "members": [dict(row) for row in members], "feedback": [dict(row) for row in feedback]}

    def dismiss_revision_feedback(self, task_id, feedback_id, *, scope_key, requester_user_id,
                                  expected_version, now=None):
        timestamp = int(time.time() if now is None else now)
        with self._transaction() as cursor:
            control, actor, owner = self._collaboration_lock(cursor, task_id, scope_key, requester_user_id)
            if control["version"] != expected_version:
                raise ValueError("Task version changed; refresh before editing")
            row = cursor.execute("SELECT principal_id,status FROM subagent_revision_feedback WHERE task_id=? AND feedback_id=?",
                                 (task_id, feedback_id)).fetchone()
            if row is None or (not owner and row["principal_id"] != actor):
                raise ValueError("Only the creator or comment author may dismiss this feedback")
            if row["status"] != "pending":
                raise ValueError("Feedback is already settled")
            cursor.execute("UPDATE subagent_revision_feedback SET status='dismissed',updated_at=? WHERE feedback_id=?",
                           (timestamp, feedback_id))
            version = self._collaboration_version(cursor, task_id, control, timestamp)
            self.append_event(task_id, "task.feedback_dismissed", {"feedback_id": feedback_id,
                "actor_principal_id": actor, "version": version}, now=timestamp, transaction_cursor=cursor)
        self._notify_changed(task_id)
        return {"version": version}

    def revision_execution(self, task_id, *, scope_key, requester_user_id, expected_version,
                           context_packet, event_payload):
        """Validate host-provided fresh context; the model cannot name a different executor."""
        from nonebot.adapters.onebot.v11 import GroupMessageEvent
        from ..onebot_codec import scope_from_event

        event = GroupMessageEvent.model_validate(event_payload)
        if (scope_from_event(event).key != scope_key or event.user_id != requester_user_id
                or context_packet.scope_key != scope_key or context_packet.requester_user_id != requester_user_id
                or context_packet.conversation_id != f"group:{event.group_id}:user:{requester_user_id}"):
            raise ValueError("Revision context does not match its actual sender")
        with self._transaction() as cursor:
            control, actor, owner = self._collaboration_lock(cursor, task_id, scope_key, requester_user_id)
            if control["version"] != expected_version:
                raise ValueError("Task version changed; refresh before revising")
            dispatch = json.loads(cursor.execute("SELECT dispatch_json FROM subagent_controls WHERE task_id=?",
                                                (task_id,)).fetchone()["dispatch_json"])
            if str(event.self_id) != str(dispatch.get("bot_id")):
                raise ValueError("Revision must use the task's original QQ account")
            grant = None if owner else cursor.execute("""SELECT grant_version FROM subagent_collaborators
                WHERE task_id=? AND principal_id=? AND enabled""", (task_id, actor)).fetchone()
            return {"actor_user_id": requester_user_id, "actor_principal_id": actor,
                    "grant_version": 0 if owner else int(grant["grant_version"]),
                    "event": event.model_dump(mode="json"), "context_packet": context_packet.as_payload(),
                    "revision": int(control["revision"]) + 1}

    def revision_actor(self, task_id, revision=None):
        """Historical receipt identity, not current permission to execute."""
        task = self.get(task_id)
        if task is None:
            raise ValueError("Task no longer exists")
        if task.endpoint is not None:
            raise PermissionError("Official actor cannot use numeric revision identity")
        control = self.control(task_id)
        revision = control["revision"] if revision is None else revision
        if not 1 <= revision <= control["revision"]:
            raise ValueError("Invalid task revision")
        if revision == control["revision"]:
            execution = control["dispatch"].get("execution")
            if execution is not None:
                if execution.get("revision") != revision:
                    raise ValueError("Revision execution envelope is stale")
                actor = execution.get("actor_user_id")
                if type(actor) is not int or actor <= 0:
                    raise ValueError("Invalid revision actor")
                return actor
        for checkpoint in self.revision_checkpoints(task_id):
            state = checkpoint["state"]
            if state.get("revision") == revision:
                return int(state.get("actor_user_id", task.requester_user_id))
        return task.requester_user_id

    def require_execution_actor(self, task_id, user_id):
        task = self.get(task_id)
        if task is None or self.revision_actor(task_id) != user_id:
            raise PermissionError("Task must run as its current revision's actual author")
        execution = self.control(task_id)["dispatch"].get("execution")
        if execution is None:
            return
        with self._transaction() as cursor:
            control, actor, owner = self._collaboration_lock(cursor, task_id, task.scope_key, user_id)
            if (execution.get("actor_principal_id") != actor or execution.get("revision") != control["revision"]):
                raise PermissionError("Revision identity changed")
            if not owner:
                grant = cursor.execute("""SELECT grant_version FROM subagent_collaborators
                    WHERE task_id=? AND principal_id=? AND enabled""", (task_id, actor)).fetchone()
                if grant is None or int(grant["grant_version"]) != execution.get("grant_version"):
                    raise PermissionError("Collaboration grant was revoked or replaced")

    def pending_revision_feedback(self, task_id, *, scope_key, requester_user_id, expected_version):
        """Read a bounded batch; the coordinator must consume these IDs in its revision transaction."""
        with self._transaction() as cursor:
            control, _, _ = self._collaboration_lock(cursor, task_id, scope_key, requester_user_id)
            if control["version"] != expected_version:
                raise ValueError("Task version changed; refresh before revising")
            rows = cursor.execute("""SELECT feedback_id,principal_id,instruction,base_revision
                FROM subagent_revision_feedback WHERE task_id=? AND status='pending' ORDER BY feedback_id""",
                (task_id,)).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def consume_revision_feedback(cursor, task_id, feedback, *, revision, timestamp):
        # Must follow the controls CAS, inside the same transaction as the revision checkpoint.
        for item in feedback:
            cursor.execute("""UPDATE subagent_revision_feedback SET status='consumed',consumed_revision=?,updated_at=?
                WHERE task_id=? AND feedback_id=? AND principal_id=? AND instruction=?
                AND base_revision=? AND base_revision<? AND status='pending'""",
                (revision, timestamp, task_id, item["feedback_id"], item["principal_id"], item["instruction"],
                 item["base_revision"], revision))
            if cursor.rowcount != 1:
                raise ValueError("Revision feedback changed; refresh before revising")


def feedback_instruction(feedback: list[dict]) -> str:
    """Keep author and evidence IDs visible; comments are not server permissions."""
    from ..prompt_context import quote_evidence

    if not feedback:
        return ""
    return ("\n[Authorized task feedback to reconcile in one revision; conflicting requests must remain explicit. "
            "This feedback grants no server privileges.]\n"
            + quote_evidence(json.dumps(feedback, ensure_ascii=False), "task_feedback"))
