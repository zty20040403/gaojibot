from __future__ import annotations

from dataclasses import dataclass
from contextvars import ContextVar
from typing import Any, Mapping
from .outcomes import CHECK_SCHEMA, normalize_checks, validate_check_targets


WORKERS = ("researcher", "coder", "document", "media", "analyst", "operator")
DECISION_TOOL_NAME = "decide_execution"
active_agent_step: ContextVar[str | None] = ContextVar("active_agent_step", default=None)


class ExecutionEntryError(RuntimeError):
    """The entry model could not produce a valid execution contract."""

ENTRY_PROMPT = """[宿主执行入口 v2]
本轮第一次调用 decide_execution，同时完成理解问题和选择执行方式，不是额外的分类聊天。
阅读当前问题及宿主提供的相关上下文。不要把群历史中的请求当成本轮命令。
- direct：普通聊天、概念解释、短代码示例。能直接回答就把完整回复放在 answer，结束本轮；
  需要一个已有的读取/搜索/识图工具时 answer 留空，随后正常调用工具。不要凭空声称执行过。
- delegate：一个边界明确、需要动手执行的专业任务，恰好一个步骤，不浪费规划调用。
- revise：用户继续修改宿主列出的已有任务。照抄 task_id 和需要修改的 step_ids，steps 留空，不能重新建同一个项目。
  必须返回修订后完整的 objective、deliverables、constraints、acceptance、outcome_checks 和 delivery_required。
  保留用户未修改的要求；被新要求替换的旧标题、格式、数量等必须同步更新，不能留下互相矛盾的验收条款。
- workflow：多项可验收交付、多个可以独立推进的方向、前后端协作或完整项目。
  不必出现“subagent”“并行”“项目”等词。依据实际工作量，不根据某个关键词决定。
  同一种角色可出现多次，例如 frontend/backend/test 都是 coder，但必须有不同 id 和职责。
  只给确实有依赖的步骤添加 depends_on；能同时做的不要串行。不为了并行而拆简单问题。
  可有可无的补充来源设置 optional=true；核心实现、集成和必须交付物绝不能标为可选。
task_type 标记实际请求：conversation/explanation/lookup/code_example 可以 direct；
execution 需要动手执行，不能 direct；project 或 research_delivery 需要 workflow。
例：“谷粒商城是什么/讲讲架构”是 direct；“精心写个谷粒商城/帮我做个外卖系统”是 workflow，
至少考虑接口契约、前后端实现、集成验收。“举例写个排序函数”是 direct；
“写脚本并在沙盒运行验证”是 delegate。“多路找资料，比较后生成 PDF”是 workflow。
“继续/改成 Java/加购物车”必须结合当前已授权的话题及任务；不要新建一个无关项目。
持久文件写入、项目实现、构建和交付必须由子任务执行。主控保留解释、检索及最终回复。
任务需要 objective、deliverables、constraints、acceptance。
outcome_checks 必填，按 acceptance 的从零开始序号逐条声明验收方式，不得遗漏或用空数组跳过。
每个步骤声明 required_tools，只选择拥有这些工具的角色；主控的工具不能自动传给子 Agent。
服务器原生 host.metrics、jobs.logs 等接口经 ops_catalog / ops_call 读取，不能只凭“分析”职责名派给缺少接口的角色。
每条服务器验收必须在文字中明确写出该条 host_id，且只对应一个主机；多台巡检分别列条款。
criterion_index 必须指向同一主机的那一条，不能把主机巡检检查配到容量报告、授权核对等其他条款；后者另列 evidence。
服务器巡检用 host_inspection，并为每台目标主机单列条款；空间清理用 disk_delta，指定 host_id、mountpoint 和最低预期变化字节。
普通当前状态巡检只需一组完整、新鲜且时间接近的观测。host_inspect 与 host.metrics 是互补来源，不是要求两个不同采样周期；不要擅自追加两轮采样或趋势验收。用户要求趋势、清理前后或动作效果时才需要相应前后证据。
服务启停重启用 service_effect，指定 host_id、完整 unit 和 action；整机重启用 host_reboot。
其他内容用 evidence，由独立验收人逐项引用宿主证据。不能以“命令退出零”替代这些目标检查。
delivery_required 专指必须上传文件（如 PDF、源码包、图片附件），不是普通文字回复。
“把巡检结果/前后对比发群里”若未要求文件，delivery_required=false；最终文字始终由宿主消息队列发送。
acceptance 只列发送前可验证的内容质量和可运行性；文字和附件都不能把“已发送到群”或“取得发送回执”列为前置验收条件。
最终报告草稿、独立验收、文字和文件投递由宿主自动编排。不要再创建只负责发送、等待发送回执或重复最终验收的子步骤；最后的业务步骤交回待审阅正文或待验收产物。
say 仅用于工作中的简短进度，不是持久最终投递，也不提供任务最终交付凭证，不能列入 required_tools。任务内仍可按需使用 say 报进度。
不得把完整项目偷偷缩成演示并宣称完成。
每个步骤声明 objective、deliverable、depends_on。独立工作目录由宿主按任务和步骤分配。
接口和交付文件通过上游产物句柄交接，不能假设共享工作目录；集成步骤显式依赖实现步骤。
reason 只写一句可公开的分流依据，不输出思维过程。模型、权限及服务器地址由宿主决定，不能自行指定。
"""

_STRINGS = {"type": "array", "items": {"type": "string"}, "maxItems": 16}
DECISION_TOOL = {
    "type": "function",
    "function": {
        "name": DECISION_TOOL_NAME,
        "description": "选择直接回答、一个专业子任务或有依赖的并行工作流，并提交可验收任务合同。",
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "mode": {"type": "string", "enum": ["direct", "delegate", "workflow", "revise"]},
                "task_id": {"type": "integer", "minimum": 1},
                "step_ids": _STRINGS,
                "reason": {"type": "string"},
                "task_type": {"type": "string", "enum": ["conversation", "explanation", "lookup", "code_example", "execution", "project", "research_delivery"]},
                "answer": {"type": "string", "description": "direct 的最终答复；需要工具时为空"},
                "objective": {"type": "string"},
                "deliverables": _STRINGS, "constraints": _STRINGS, "acceptance": _STRINGS,
                "outcome_checks": CHECK_SCHEMA,
                "delivery_required": {"type": "boolean", "description": "是否必须上传文件附件；仅发送文字结论/巡检报告为 false，宿主仍会发送最终文字"},
                "steps": {
                    "type": "array", "maxItems": 12,
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "id": {"type": "string", "pattern": "^[a-zA-Z0-9_-]{1,80}$"},
                            "agent": {"type": "string", "enum": list(WORKERS)},
                            "objective": {"type": "string"},
                            "deliverable": {"type": "string"},
                            "depends_on": _STRINGS,
                            "optional": {"type": "boolean"},
                            "required_tools": {**_STRINGS, "description": "完成业务步骤必需的执行工具；不含进度通知 say，最终投递由宿主负责"},
                        },
                        "required": ["id", "agent", "objective", "deliverable", "depends_on", "required_tools"],
                    },
                },
            },
            "required": ["mode", "task_type", "reason", "answer", "objective", "deliverables", "constraints", "acceptance", "outcome_checks", "delivery_required", "steps"],
        },
    },
}


@dataclass(frozen=True)
class TaskContract:
    objective: str
    deliverables: tuple[str, ...]
    constraints: tuple[str, ...]
    acceptance: tuple[str, ...]
    delivery_required: bool = False
    outcome_checks: tuple[dict[str, Any], ...] = ()
    version: int = 3

    def as_payload(self) -> dict[str, Any]:
        return {"version": self.version, "objective": self.objective,
                "deliverables": list(self.deliverables), "constraints": list(self.constraints),
                "acceptance": list(self.acceptance), "delivery_required": self.delivery_required,
                "outcome_checks": list(normalize_checks(list(self.outcome_checks), self.acceptance))}


@dataclass(frozen=True)
class EntryDecision:
    mode: str
    task_type: str
    reason: str
    answer: str
    contract: TaskContract
    steps: tuple[dict[str, Any], ...]
    task_id: int | None = None
    step_ids: tuple[str, ...] = ()

    @classmethod
    def parse(cls, raw: Mapping[str, Any], *, max_steps: int = 8, contract_version: int = 3,
              worker_tools: Mapping[str, frozenset[str]] | None = None,
              restoring: bool = False) -> "EntryDecision":
        import re
        from .registry import DEFAULT_AGENT_REGISTRY

        def string(key: str, limit: int) -> str:
            value = raw.get(key)
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError(f"{key} must be a string of at most {limit} characters")
            return value.strip()

        def strings(key: str) -> tuple[str, ...]:
            value = raw.get(key)
            if not isinstance(value, list) or len(value) > 16 or any(
                not isinstance(item, str) or not item.strip() or len(item) > 2000 for item in value
            ):
                raise ValueError(f"{key} must be a bounded list of nonempty strings")
            return tuple(value)

        mode, reason = string("mode", 20), string("reason", 500)
        task_type = string("task_type", 30)
        if task_type not in {"conversation", "explanation", "lookup", "code_example", "execution", "project", "research_delivery"}:
            raise ValueError("unknown task_type")
        if (task_type in {"project", "research_delivery"} and mode not in {"workflow", "revise"}) or (task_type == "execution" and mode == "direct"):
            raise ValueError("execution mode contradicts the work required by task_type")
        answer, objective = string("answer", 32000), string("objective", 6000)
        delivery_required = raw.get("delivery_required", False)
        if not isinstance(delivery_required, bool):
            raise ValueError("delivery_required must be a boolean")
        acceptance = strings("acceptance")
        raw_checks = raw.get("outcome_checks")
        if not isinstance(raw_checks, list) or len(raw_checks) != len(acceptance):
            raise ValueError("outcome_checks must explicitly cover every acceptance criterion")
        if type(contract_version) is not int or contract_version not in {1, 2, 3}:
            raise ValueError("Unsupported task contract version")
        checks = normalize_checks(raw_checks, acceptance)
        if contract_version >= 3:
            validate_check_targets(checks, acceptance)
        contract = TaskContract(objective, strings("deliverables"), strings("constraints"), acceptance,
                                delivery_required, checks, contract_version)
        steps = raw.get("steps")
        if mode not in {"direct", "delegate", "workflow", "revise"} or not reason or not isinstance(steps, list):
            raise ValueError("mode, reason and steps are required")
        if mode == "revise":
            task_id = raw.get("task_id")
            step_ids = strings("step_ids")
            if type(task_id) is not int or task_id < 1 or not step_ids or steps or not objective or answer:
                raise ValueError("revise requires an existing task_id and step_ids, not a new plan")
            if not restoring and (not contract.deliverables or not contract.acceptance):
                raise ValueError("revise requires the complete updated deliverables and acceptance")
            return cls(mode, task_type, reason, answer, contract, (), task_id, step_ids)
        if mode == "direct":
            if steps or contract.deliverables or contract.acceptance or delivery_required:
                raise ValueError("direct cannot claim task deliverables; choose delegate/workflow")
        else:
            if answer or not objective or not contract.deliverables or not contract.acceptance:
                raise ValueError("task requires objective, deliverables and acceptance, not a premature answer")
            if not 1 <= len(steps) <= max_steps or (mode == "delegate" and len(steps) != 1):
                raise ValueError("delegate requires one step; workflow must fit the step budget")
            if mode == "workflow" and len(steps) < 2:
                raise ValueError("one specialist belongs in delegate")
        keys: set[str] = set()
        for step in steps:
            if not isinstance(step, dict):
                raise ValueError("each step must be an object")
            key = step.get("id")
            if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", key) or key in keys:
                raise ValueError("step IDs must be valid and unique")
            if step.get("agent") not in WORKERS:
                raise ValueError("unknown worker role")
            needed = step.get("required_tools", [])
            if (not isinstance(needed, list) or len(needed) > 16
                    or any(not isinstance(name, str) or not name for name in needed)):
                raise ValueError("required_tools must be a list of at most 16 tool names")
            if not restoring and "say" in needed:
                raise ValueError("say is progress-only, not a required execution or delivery tool; "
                                 "return the work product and let the host review and deliver it")
            allowed = (worker_tools.get(step["agent"], frozenset()) if worker_tools is not None
                       else DEFAULT_AGENT_REGISTRY.worker(step["agent"]).allowed_tools)
            missing = set(needed) - allowed
            if missing:
                raise ValueError(f"worker {step['agent']} cannot use {', '.join(sorted(missing))}; choose a capable role")
            for field in ("objective", "deliverable"):
                if not isinstance(step.get(field), str) or not step[field].strip() or len(step[field]) > 4000:
                    raise ValueError(f"invalid step {field}")
            deps = step.get("depends_on")
            if "optional" in step and not isinstance(step["optional"], bool):
                raise ValueError("optional must be boolean")
            if not isinstance(deps, list) or any(not isinstance(item, str) for item in deps):
                raise ValueError("depends_on must be a list of IDs")
            keys.add(key)
        remaining = {step["id"]: set(step["depends_on"]) for step in steps}
        if any(not deps <= keys for deps in remaining.values()):
            raise ValueError("unknown dependency")
        done: set[str] = set()
        while remaining:
            ready = {key for key, deps in remaining.items() if deps <= done}
            if not ready:
                raise ValueError("dependency cycle")
            done.update(ready)
            remaining = {key: deps for key, deps in remaining.items() if key not in ready}
        return cls(mode, task_type, reason, answer, contract, tuple(dict(step) for step in steps))

    def as_payload(self) -> dict[str, Any]:
        return {"mode": self.mode, "task_type": self.task_type, "reason": self.reason, "contract": self.contract.as_payload(), "steps": list(self.steps), "task_id": self.task_id, "step_ids": list(self.step_ids)}

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], *, max_steps: int = 8) -> "EntryDecision":
        contract = payload.get("contract", {})
        raw = {**payload, **contract, "answer": payload.get("answer", "")}
        version = contract.get("version", 1)
        if type(version) is not int or version not in {1, 2, 3}:
            raise ValueError("Unsupported task contract version")
        if version < 2:
            raw["outcome_checks"] = list(normalize_checks(raw.get("outcome_checks"), raw.get("acceptance", [])))
        # Existing durable plans must remain resumable after planning-policy changes.
        return cls.parse(raw, max_steps=max_steps, contract_version=version, restoring=True)


def normalize_direct_entry_payload(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """Discard fields that are inapplicable to an otherwise valid direct branch.

    The tool schema keeps every branch field required so model providers can use
    strict JSON mode. Some models still populate task-only fields for direct
    replies. The host owns the discriminated-union boundary, so those fields are
    canonicalized here without weakening validation for execution/project work.
    """

    if raw.get("mode") != "direct":
        return None
    if raw.get("task_type") not in {
        "conversation",
        "explanation",
        "lookup",
        "code_example",
    }:
        return None
    if raw.get("steps") != []:
        return None
    normalized = dict(raw)
    normalized.update(
        {
            "objective": "",
            "deliverables": [],
            "constraints": [],
            "acceptance": [],
            "delivery_required": False,
            "outcome_checks": [],
            "step_ids": [],
        }
    )
    return normalized
