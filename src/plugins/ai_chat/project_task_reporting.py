"""Host-owned project completion receipts, independent of model-written reports."""

import hashlib
import json
import time

from nonebot.adapters.onebot.v11 import Message, MessageSegment

from src.project_update import ARTIFACT_NAME
from .onebot_codec import decode_onebot_message, scope_from_event


TERMINAL = frozenset({"completed", "partial", "failed", "cancelled"})
DEFERRED = {
    "access": "当前权限无法通过核验，项目尚未更新；恢复权限后后台会重新检查。",
    "artifact": "项目更新包缺失或不可读取，项目尚未更新；需要恢复产物或修订任务。",
    "invalid": "项目更新包未通过格式或内容检查，项目尚未更新；需要修订任务。",
    "conflict": "项目验收或状态检查未通过，项目尚未更新；原版本与任务锁保留。",
    "temporary": "项目处理暂时失败，后台会重试；目前不能认定已更新。",
}


def record_deferred(tasks, task_id, revision, exc):
    """Persist a bounded, non-secret diagnosis; repeated polling is not a new event."""
    if revision is None:
        return
    from .projects import ProjectConflict

    code = ("access" if isinstance(exc, PermissionError) else
            "artifact" if isinstance(exc, FileNotFoundError) else
            "conflict" if isinstance(exc, ProjectConflict) else
            "invalid" if isinstance(exc, ValueError) else "temporary")
    state = {"revision": revision, "code": code}
    with tasks._transaction() as cursor:
        control = cursor.execute("SELECT revision FROM subagent_controls WHERE task_id=? FOR UPDATE",
                                 (task_id,)).fetchone()
        if control is None or control["revision"] != revision:
            return
        previous = cursor.execute("SELECT state_json FROM subagent_checkpoints WHERE task_id=? "
            "AND phase='project_reconcile_deferred' ORDER BY sequence DESC LIMIT 1", (task_id,)).fetchone()
        if previous is None or json.loads(previous["state_json"]) != state:
            tasks.append_checkpoint(task_id, "project_reconcile_deferred", state, transaction_cursor=cursor)


def report_candidates(tasks, *, after=0, limit=20):
    """Rotate bounded pages, including tasks whose first final was already queued."""
    with tasks._transaction() as cursor:
        return [row["task_id"] for row in cursor.execute("""SELECT t.task_id FROM subagent_tasks t
            JOIN project_task_bindings b USING(task_id) JOIN subagent_controls c USING(task_id)
            WHERE t.task_id>? AND t.status IN ('completed','partial','failed','cancelled')
            AND c.dispatch_json<>'{}' AND NOT EXISTS (SELECT 1 FROM routine_children r WHERE r.task_id=t.task_id)
            ORDER BY t.task_id LIMIT ?""", (after, limit)).fetchall()]


def latest_report(tasks, task_id, revision):
    with tasks._transaction() as cursor:
        row = cursor.execute("""SELECT state_json FROM subagent_checkpoints WHERE task_id=?
            AND phase='project_report' AND state_json::jsonb->>'revision'=?
            ORDER BY sequence DESC LIMIT 1""", (task_id, str(revision))).fetchone()
        return json.loads(row["state_json"]) if row else None


def _project_state(cursor, task, revision):
    row = cursor.execute("""SELECT b.project_id,b.base_version,p.scope_key,p.bot_native_user_id,
        v.revision,v.version_no,m.status AS materialized,j.status AS job_status,r.revision AS released_revision
        FROM project_task_bindings b JOIN projects p USING(project_id)
        LEFT JOIN project_task_publications v ON v.task_id=b.task_id
        LEFT JOIN project_task_materializations m ON m.task_id=b.task_id
        LEFT JOIN durable_jobs j ON j.job_id=m.job_id
        LEFT JOIN project_task_releases r ON r.task_id=b.task_id WHERE b.task_id=?""", (task.task_id,)).fetchone()
    if row is None or row["project_id"] != task.project_id or row["scope_key"] != task.scope_key:
        raise ValueError("Project report binding changed")
    state = {"project_id": row["project_id"], "base_version": row["base_version"]}
    if row["revision"] is not None and row["revision"] != revision:
        raise ValueError("Project report publication revision changed")
    if row["materialized"] == "completed":
        state.update(code="materialized", version=row["version_no"])
        note = f"项目 prj#{task.project_id}：本任务的 v{row['version_no']} 已恢复到工作区并通过检查。"
    elif row["revision"] is not None:
        stopped = row["job_status"] in {"failed", "cancelled", "compensated"}
        state.update(code="restore_failed" if stopped else "restoring", version=row["version_no"])
        note = f"项目 prj#{task.project_id}：v{row['version_no']} 已保存到版本库，"
        note += ("但工作区恢复失败或已停止，尚未完成更新；版本和任务锁保留，需要处理恢复任务。"
                 if stopped else "正在等待工作区恢复与检查，尚未完成更新。")
    elif row["released_revision"] == revision:
        state.update(code="released")
        note = f"项目 prj#{task.project_id}：本任务未更新项目，原 v{row['base_version']} 保留，任务锁已释放。"
    else:
        previous = cursor.execute("""SELECT state_json FROM subagent_checkpoints WHERE task_id=?
            AND phase='project_reconcile_deferred' AND state_json::jsonb->>'revision'=?
            ORDER BY sequence DESC LIMIT 1""", (task.task_id, str(revision))).fetchone()
        code = json.loads(previous["state_json"])["code"] if previous else "pending"
        state.update(code=code)
        note = f"项目 prj#{task.project_id}：" + DEFERRED.get(code,
            "尚未取得项目更新回执，后台仍在核对；这不等于项目已更新。")
    return state, note, row["bot_native_user_id"]


def _file_state(cursor, tasks, task, revision):
    from .subagents import _delivery_outcomes, _outcome_from_run

    required = bool(task.plan.get("contract", {}).get("delivery_required"))
    expected = {}
    if required:
        rows = cursor.execute("SELECT * FROM subagent_runs WHERE task_id=?", (task.task_id,)).fetchall()
        runs = [tasks._run_row(row) for row in rows]
        outcomes = _delivery_outcomes(task, {run.step_key: _outcome_from_run(task, run) for run in runs},
                                      include_project_updates=False)
        for outcome in outcomes:
            for artifact in outcome.result.get("artifacts", []):
                if isinstance(artifact, dict):
                    key = str(artifact.get("snapshot") or artifact.get("handle") or "")
                    if key:
                        expected[key] = {"key": key, "name": artifact.get("name") or "附件",
                                         "state": "not_queued", "draft": False}
    for row in cursor.execute("SELECT * FROM subagent_deliveries WHERE task_id=? AND revision=?",
                              (task.task_id, revision)).fetchall():
        payload = json.loads(row["payload_json"])
        if payload.get("artifact", {}).get("name") == ARTIFACT_NAME:
            continue
        expected[row["delivery_key"]] = {"key": row["delivery_key"], "name": payload.get("filename") or "附件",
                                        "state": row["state"], "draft": payload.get("draft") is True}
    files = sorted(expected.values(), key=lambda item: item["key"])
    return {"required": required, "items": files}


def _render(task, project, note, files):
    labels = {"completed": "子任务执行结束", "partial": "子任务部分完成",
              "failed": "子任务执行失败", "cancelled": "任务已取消"}
    items = files["items"]
    confirmed = sum(item["state"] == "acknowledged" for item in items)
    all_files = (bool(items) or not files["required"]) and all(
        item["state"] == "acknowledged" and not item["draft"] for item in items)
    complete = task.status == "completed" and project["code"] == "materialized" and all_files
    lines = ["项目任务已完成。" if complete else "项目任务尚未全部完成。", labels[task.status] + "。", note]
    if items:
        lines.append(f"用户附件：{confirmed}/{len(items)} 个已确认送达。")
        states = {"not_queued": "尚未进入发送队列，请检查验收和发送权限",
                  "queued": "已进入持久投递队列", "sending": "正在核对上传回执",
                  "unknown": "上传回执不明确，只核对，不重复上传", "rejected": "发送失败，自动重试已停止",
                  "acknowledged": "已确认送达"}
        for item in items:
            lines.append(f"{item['name']}：{states.get(item['state'], '尚未确认送达')}"
                         + ("（未完成草稿）。" if item["draft"] else "。"))
    elif files["required"]:
        lines.append("用户附件：尚无可发送成品，未完成交付；内部项目更新包不是用户附件。")
    else:
        lines.append("本任务未要求另发文件；项目更新包仅供内部导入。")
    acceptance = task.result.get("validation", {}).get("acceptance", {})
    criteria = acceptance.get("task_outcome", {}).get("criteria", [])
    if criteria:
        lines.append("执行验收（与项目落地、文件送达分别核对）：")
        for row in criteria:
            label = {"passed": "已核实", "failed": "未达标"}.get(row.get("status"), "尚未验证")
            lines.append(f"{label}：{row.get('description', '')}。{row.get('reason', '')}")
    return "\n".join(lines)


def enqueue_project_report(dispatcher, task_id):
    """Snapshot receipts and enqueue one revision-bound report in one transaction."""
    tasks, outbox = dispatcher.store, dispatcher.context.delivery_store
    with tasks._transaction() as cursor:
        control = cursor.execute("SELECT * FROM subagent_controls WHERE task_id=? FOR UPDATE", (task_id,)).fetchone()
        row = cursor.execute("SELECT * FROM subagent_tasks WHERE task_id=? FOR UPDATE", (task_id,)).fetchone()
        if row is None or row["project_id"] is None or row["status"] not in TERMINAL:
            return None
        if control is None or outbox is None:
            raise RuntimeError("Project reports require durable dispatch and outbox")
        if cursor.execute("SELECT 1 FROM routine_children WHERE task_id=?", (task_id,)).fetchone():
            return None
        task = tasks._task_row(row)
        revision = control["revision"]
        event = dispatcher.restore_event(json.loads(control["dispatch_json"]))
        if scope_from_event(event).key != task.scope_key or event.user_id != task.requester_user_id:
            raise ValueError("Project report does not match its original task owner")
        project, note, bot_id = _project_state(cursor, task, revision)
        if str(event.self_id) != str(bot_id):
            raise ValueError("Project report bot identity changed")
        files = _file_state(cursor, tasks, task, revision)
        state = {"revision": revision, "execution": task.status, "project": project, "files": files}
        previous = cursor.execute("SELECT sequence,state_json FROM subagent_checkpoints WHERE task_id=? "
            "AND phase='project_report' ORDER BY sequence DESC LIMIT 1", (task_id,)).fetchone()
        if previous:
            recorded = json.loads(previous["state_json"])
            delivery_id = recorded.pop("delivery_id")
            if recorded == state:
                return outbox.get(delivery_id)
        transition = {"state": state, "after": previous["sequence"] if previous else 0}
        signature = hashlib.sha256(json.dumps(transition, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        text = f"{task.handle}（第 {revision} 版）\n" + _render(task, project, note, files)
        from .agent.preview_receipts import preview_delivery_note

        preview = preview_delivery_note(tasks, task, revision)
        if preview:
            text += "\n\n" + preview
        body = decode_onebot_message(Message(MessageSegment.text(text))).body
        delivery, created = outbox.enqueue(
            idempotency_key=f"subagent-final:{task_id}:{revision}:project:{signature}",
            source_scope_key=task.scope_key, source_canonical_message_id=task.trigger_message_id,
            target_scope=scope_from_event(event), body=body, reply_to_native_message_id=str(event.message_id),
            transaction_cursor=cursor)
        if created:
            # Replace only messages that have not been dispatched. An ambiguous
            # or in-flight send must keep its receipt for echo reconciliation.
            cursor.execute("""UPDATE deliveries SET status='cancelled',updated_at=?
                WHERE status IN ('pending','failed') AND delivery_id IN (
                    SELECT CAST(state_json::jsonb->>'delivery_id' AS BIGINT) FROM subagent_checkpoints
                    WHERE task_id=? AND phase='project_report')""", (int(time.time()), task_id))
            tasks.append_checkpoint(task_id, "project_report", {**state, "delivery_id": delivery.delivery_id},
                                    transaction_cursor=cursor)
        cursor.execute("UPDATE subagent_controls SET final_queued_revision=? WHERE task_id=? AND revision=?",
                       (revision, task_id, revision))
    if created:
        tasks._notify_changed(task_id)
    return delivery


async def validate_report_delivery(app, bot, delivery):
    from .bridges import BridgePermanentError, BridgeRetryableError
    from .project_executor import _store
    from .project_file_transport import project_runtime
    from .workers.durable_jobs import JobDeferred

    runtime = project_runtime(app)
    def identity():
        try:
            _, task_id, revision, _, _ = delivery.idempotency_key.split(":")
            task_id, revision = int(task_id), int(revision)
        except (ValueError, TypeError) as exc:
            raise BridgePermanentError("项目任务报告编号无效") from exc
        with app.subagent_store._transaction() as cursor:
            row = cursor.execute("""SELECT p.*,b.actor_principal_id,c.revision FROM project_task_bindings b
                JOIN projects p USING(project_id) JOIN subagent_controls c USING(task_id)
                WHERE b.task_id=?""", (task_id,)).fetchone()
            proof = cursor.execute("""SELECT state_json FROM subagent_checkpoints WHERE task_id=?
                AND phase='project_report' ORDER BY sequence DESC LIMIT 1""", (task_id,)).fetchone()
            if (row is None or row["revision"] != revision or proof is None
                    or json.loads(proof["state_json"]).get("delivery_id") != delivery.delivery_id
                    or row["scope_key"] != delivery.target_scope.key
                    or row["scope_key"] != delivery.source_scope_key
                    or str(row["bot_native_user_id"]) != str(bot.self_id)):
                raise BridgePermanentError("项目任务报告已过时或投递身份不匹配")
            return dict(row)
    project = await _store(identity)
    try:
        access = await runtime.resolve_access(project, project["actor_principal_id"])
        await _store(runtime.store.get, access, project["project_id"])
    except JobDeferred as exc:
        raise BridgeRetryableError("暂时无法核实项目成员身份，报告等待发送") from exc
    except PermissionError as exc:
        raise BridgePermanentError("项目成员权限已失效，停止发送报告") from exc
    await _store(identity)
