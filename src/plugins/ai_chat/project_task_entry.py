"""Chat entry into the existing durable project save-to-task queue."""

from dataclasses import replace
import hashlib
import json

from .project_access import project_access, projects_configured, projects_available
from .project_executor import _store
from .project_task_plan import integrate_decision, planning_instruction, INTEGRATION_TOOLS
from .projects import ProjectConflict
from .tool_policy import tool_enabled
from .workers.durable_jobs import JobDeferred


def entry_receipt(receipt):
    project = f"prj#{receipt['project_id']}"
    if receipt["task_bound"]:
        message = f"{project} 已保存并绑定 task#{receipt['task_id']}，沿用此任务，不会重复创建。"
    elif receipt["status"] in {"pending", "running"}:
        message = (f"{project} 已登记项目任务，正在保存并准备工作区；保存核实后才会启动子 Agent。"
                   "准备状态可通过 /项目 状态 查看。")
    else:
        message = f"{project} 的任务准备状态为 {receipt['status']}，尚未确认启动子 Agent，请查看 /项目 状态。"
    status = ("bound" if receipt["task_bound"] else "preparing"
              if receipt["status"] in {"pending", "running"} else "needs_attention")
    return {**receipt, "status": status,
            "preparation_status": receipt["status"], "message": message}


async def submit_project_task(context, *, objective, role, packet_factory, selected_profile,
                              available_tools, decision=None, parent_trace=None):
    """Return None only when no project applies, never on failed preparation."""
    if not projects_configured(context):
        return None
    if not projects_available(context):
        raise JobDeferred("项目运行环境暂不可用；不会改用普通沙盒执行项目任务。", 30)
    app, runtime = context.app, context.app.project_runtime
    access = await project_access(context)
    admission = runtime.task_admission
    turn_id = context.journal_turn_id
    entry = {"objective": objective, "role": role}
    key = hashlib.sha256(json.dumps([
        "project-task-entry-v1", access.scope.key, access.scope.bot_native_user_id,
        access.scope.actor_native_user_id, turn_id,
    ]).encode()).hexdigest()
    prior = (await _store(admission.status, access, key, entry=entry, turn_id=turn_id)
             if admission is not None and type(turn_id) is int and turn_id > 0 else None)
    project = None if prior is not None else await _store(runtime.store.current, access)
    if prior is None and project is None:
        return None
    from .project_authorization import official_project
    official = official_project(access) is not None
    if not official and not app.settings.is_sandbox_user_allowed(context.event.user_id):
        raise PermissionError("当前用户没有项目沙盒执行权限。")
    if prior is not None:
        return entry_receipt(prior)
    coordinator = app.subagent_coordinator
    if admission is None or coordinator is None or (not official and coordinator.dispatcher is None):
        raise JobDeferred("项目后台任务尚未就绪；不会改用临时任务。", 30)
    if type(turn_id) is not int or turn_id <= 0:
        raise ProjectConflict("项目任务缺少持久消息记录，尚未启动。")
    def require_tools(names):
        missing = {name for name in names if name not in available_tools or not tool_enabled(name)}
        if missing:
            raise PermissionError("项目任务所需工具不可用，尚未启动：" + ", ".join(sorted(missing)))

    require_tools(INTEGRATION_TOOLS)
    await _store(runtime.store.get, access, project["project_id"], edit=True)
    instruction = planning_instruction(coordinator.max_steps)
    packet = await packet_factory(objective)
    packet = replace(packet, constraints=(*packet.constraints, instruction))
    if decision is None:
        decision = await coordinator.prepare_entry(packet, selected_profile, role=role,
                                                   parent_trace=parent_trace, project_task=True)
    decision = integrate_decision(decision, max_steps=coordinator.max_steps, registry=coordinator.registry)
    packet = replace(packet, constraints=(
        *packet.constraints, *decision.contract.constraints,
        "交付物：" + "；".join(decision.contract.deliverables),
        "验收：" + "；".join(decision.contract.acceptance),
    ))
    # Recheck membership after model latency; the transaction also checks focus,
    # project revision, original turn and all outstanding I/O before freezing it.
    access = await project_access(context)
    if not official and not app.settings.is_sandbox_user_allowed(context.event.user_id):
        raise PermissionError("项目沙盒执行权限已撤销。")
    require_tools({name for step in decision.steps for name in step.get("required_tools", [])})
    try:
        receipt = await _store(admission.request, access, project_id=project["project_id"],
            expected_version=project["version"], request_key=key, turn_id=turn_id,
            packet=packet, decision=decision,
            dispatch=({'platform':'qq-official','tool_profile':'official-projects-v1',
                       'profile':selected_profile.name,'project_entry':entry}
                      if official else {"event": context.event.model_dump(mode="json"),
                      "bot_id": access.scope.bot_native_user_id, "profile": selected_profile.name,
                      "project_entry": entry}),
            max_parallelism=coordinator.max_parallelism, max_steps=coordinator.max_steps,
            timeout_seconds=coordinator.timeout_seconds,
            preflight=runtime.backends[project["backend"]].preflight)
    except ProjectConflict:
        # Concurrent identical chat retries may finish planning differently.
        # The first committed envelope wins; never replace it or start another.
        receipt = await _store(admission.status, access, key, entry=entry, turn_id=turn_id)
        if receipt is None:
            raise
    return entry_receipt(receipt)
