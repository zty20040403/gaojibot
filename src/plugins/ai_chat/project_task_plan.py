"""Host-owned integration step for isolated project workers."""

from copy import deepcopy

from src.project_update import ARTIFACT_NAME
from .agent.execution import EntryDecision
from .project_task_inputs import project_output_contract


INTEGRATION_STEP = "project_integrate"
INTEGRATION_TOOLS = frozenset({"sandbox_create", "sandbox_exec", "sandbox_read_file"})


def planning_instruction(max_steps):
    if type(max_steps) is not int or not 2 <= max_steps <= 12:
        raise ValueError("项目任务至少需要两个步骤名额：业务执行和最终合并。")
    return (
        "\n[项目任务规划]\n此任务有宿主固定的项目基线，各步骤独立工作，不共享可写目录。"
        f"最多规划 {max_steps - 1} 个业务步骤，保留一个名额给宿主追加的 {INTEGRATION_STEP}。"
        "业务步骤交付可导入的实际文件或有依据的研究结果；不要另建只负责打包项目更新的步骤。"
        "宿主最终合并步骤会依赖所有业务步骤，合并成果并实际检查完整项目。"
        "deliverables、constraints、acceptance 各最多 15 条，为宿主的项目交付约束保留一个名额。"
        "不能为了减少步骤或条款而遗漏用户要求。"
    )


def integrate_decision(decision, *, max_steps, registry):
    """Retain the business DAG and contract; append exactly one required sink."""
    planning_instruction(max_steps)
    if decision.mode not in {"delegate", "workflow"}:
        raise ValueError("项目任务需要可执行的新任务计划。")
    if len(decision.steps) >= max_steps:
        raise ValueError("项目计划没有为最终合并预留步骤；请重新规划，不会删减现有任务。")
    if any(step["id"] == INTEGRATION_STEP for step in decision.steps):
        raise ValueError("project_integrate 是宿主保留的步骤编号。")
    raw = deepcopy({**decision.as_payload(), **decision.contract.as_payload(), "answer": ""})
    for field in ("deliverables", "constraints", "acceptance"):
        if len(raw[field]) >= 16:
            raise ValueError(f"项目合同 {field} 需要为宿主交付约束保留一个名额。")
    raw["mode"] = "workflow"
    raw["deliverables"].append(f"唯一项目更新包 {ARTIFACT_NAME}")
    raw["constraints"].append(project_output_contract())
    index = len(raw["acceptance"])
    raw["acceptance"].append(
        f"{ARTIFACT_NAME} 可正确应用到固定项目基线，合并后的完整项目包含本任务成果，"
        "实际检查证明符合原有交付要求；尚未恢复到项目工作区时不得声称已更新。"
    )
    raw["outcome_checks"].append({"kind": "evidence", "criterion_index": index})
    raw["steps"].append({
        "id": INTEGRATION_STEP, "agent": "coder", "optional": False,
        "depends_on": [step["id"] for step in decision.steps],
        "required_tools": sorted(INTEGRATION_TOOLS),
        "objective": (
            "导入宿主固定的项目基线并复制到自己的可写目录，读取全部上游结果，导入实际文件，"
            "按原任务目标合并改动并实际检查完整项目。纯研究或巡检结果保存为有证据的项目报告，"
            "不要凭空改动无关源码。可选上游失败时如实记录；必需成果缺失时不得声称完成。"
            f"按项目更新交付格式生成且只返回一个 {ARTIFACT_NAME} 的实际文件句柄；"
            "保留用户另行要求的成品附件，不把中间过程文件冒充最终成品。"
            "不自行上传群文件或更改项目卷，独立验收和项目落地由宿主执行。"
        ),
        "deliverable": f"{ARTIFACT_NAME}、实际合并检查的证据和用户要求的最终成品附件",
    })
    return EntryDecision.parse(raw, max_steps=max_steps,
        worker_tools={role: registry.worker(role).allowed_tools for role in registry.worker_roles})
