from __future__ import annotations

import re
import logging
import threading
from dataclasses import dataclass, replace
from typing import Any, Literal


ToolRisk = Literal["low", "medium", "high", "critical"]
ToolIdempotency = Literal["pure", "idempotent", "keyed", "non-idempotent"]
ToolApprovalMode = Literal["never", "explicit", "task"]
ToolExecutionMode = Literal["inline", "durable-eligible", "durable-required"]
ToolCompensation = Literal[
    "none",
    "cancel-process",
    "close-browser",
]


@dataclass(frozen=True)
class ToolPolicy:
    """Host-owned execution contract; model output cannot override it."""

    risk: ToolRisk = "medium"
    idempotency: ToolIdempotency = "non-idempotent"
    side_effects: tuple[str, ...] = ("unspecified",)
    timeout_seconds: float = 30.0
    approval: ToolApprovalMode = "never"
    execution_mode: ToolExecutionMode = "inline"
    compensation: ToolCompensation = "none"
    max_identical_calls: int = 2

    def as_manifest(self) -> dict[str, Any]:
        return {
            "risk": self.risk,
            "idempotency": self.idempotency,
            "side_effects": list(self.side_effects),
            "timeout_seconds": self.timeout_seconds,
            "approval": self.approval,
            "execution_mode": self.execution_mode,
            "compensation": self.compensation,
            "max_identical_calls": self.max_identical_calls,
        }


@dataclass(frozen=True)
class ToolApproval:
    allowed: bool
    source: str = ""
    reason: str = ""


_READ_TOOLS = {
    "ops_catalog",
    "read_agent_result",
    "read_task_evidence",
    "read_task_evidence_batch",
    "web_search",
    "query_alerts",
    "fleet_overview",
    "host_inspect",
    "service_inspect",
    "model_status",
    "service_logs",
    "read_image_text",
    "read_attachment",
    "view_image",
    "view_video",
    "find_images",
    "find_stickers",
    "transcribe_voice",
    "get_message_by_id",
    "search_messages",
    "sandbox_list",
    "sandbox_read_file",
    "list_recent_files",
    "memory_list",
    "context_expand",
    "context_search",
    "inspect_source",
    "use_skill",
    "group_members",
    "recent_speakers",
    "reminder_list",
    "routine_list",
    "project_list",
    "project_status",
    "project_versions",
    "view_forward",
    "view_bilibili",
    "inspect_shared_content",
    "get_shared_content",
    "browser_snapshot",
    "job_status",
    "nix_search",
    "operation_status",
    "cluster_job_status",
}

_SEND_TOOLS = {
    "send_file_from_sandbox",
    "send_image_from_sandbox",
    "say",
    "send_sticker",
    "send_forward_summary",
    "send_qq_face",
    "reply_with_voice",
    "reply_send",
}

_MEMORY_WRITE_TOOLS = {
    "memory_add",
    "memory_remove",
    "pin_message",
    "unpin_message",
    "reminder_set",
    "reminder_cancel",
    "routine_set",
    "routine_cancel",
}

_SANDBOX_WRITE_TOOLS = {
    "sandbox_create",
    "sandbox_exec",
    "sandbox_write_file",
    "import_file_to_sandbox",
}

_BROWSER_WRITE_TOOLS = {
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_press_key",
    "browser_wait_for",
    "browser_scroll",
    "browser_close",
    "browser_clear",
}


def _policy_registry() -> dict[str, ToolPolicy]:
    policies = {
        name: ToolPolicy(
            risk="low",
            idempotency="pure",
            side_effects=("read",),
            timeout_seconds=45.0,
            max_identical_calls=2,
        )
        for name in _READ_TOOLS
    }
    policies.update(
        {
            name: ToolPolicy(
                risk="high",
                idempotency="non-idempotent",
                side_effects=("send:conversation",),
                timeout_seconds=90.0,
                max_identical_calls=1,
            )
            for name in _SEND_TOOLS
        }
    )
    policies.update(
        {
            name: ToolPolicy(
                risk="medium",
                idempotency="non-idempotent",
                side_effects=("write:memory",),
                timeout_seconds=30.0,
                max_identical_calls=1,
            )
            for name in _MEMORY_WRITE_TOOLS
        }
    )
    policies.update(
        {
            name: ToolPolicy(
                risk="medium",
                idempotency="non-idempotent",
                side_effects=("write:sandbox",),
                timeout_seconds=180.0,
                max_identical_calls=1,
            )
            for name in _SANDBOX_WRITE_TOOLS
        }
    )
    policies.update(
        {
            name: ToolPolicy(
                risk="medium",
                idempotency="non-idempotent",
                side_effects=("write:browser",),
                timeout_seconds=45.0,
                compensation="close-browser",
                max_identical_calls=1,
            )
            for name in _BROWSER_WRITE_TOOLS
        }
    )
    policies["sandbox_exec"] = ToolPolicy(
        risk="high",
        idempotency="non-idempotent",
        side_effects=("write:sandbox", "execute:code"),
        timeout_seconds=310.0,
        execution_mode="durable-eligible",
        compensation="cancel-process",
        max_identical_calls=1,
    )
    policies["inspect_shared_content"] = ToolPolicy(
        risk="low",
        idempotency="pure",
        side_effects=("read", "download:remote-media"),
        timeout_seconds=3900.0,
        max_identical_calls=1,
    )
    policies["view_video"] = ToolPolicy(
        risk="low",
        idempotency="pure",
        side_effects=("read", "download:remote-media"),
        timeout_seconds=1900.0,
        max_identical_calls=1,
    )
    policies["diagnose_incident"] = ToolPolicy(
        risk="low",
        idempotency="non-idempotent",
        side_effects=("read:fleet", "probe:fixed-target", "write:diagnostic-ledger"),
        timeout_seconds=180.0,
        max_identical_calls=1,
    )
    policies["operation_prepare"] = ToolPolicy(
        risk="high",
        idempotency="keyed",
        side_effects=("write:operation-ledger",),
        timeout_seconds=30.0,
        max_identical_calls=1,
    )
    policies["operation_approve"] = ToolPolicy(
        risk="high", idempotency="keyed", side_effects=("write:operation-ledger", "execute:service"),
        timeout_seconds=30.0, approval="task", max_identical_calls=1,
    )
    policies["ops_call"] = ToolPolicy(
        risk="high", idempotency="keyed",
        side_effects=("read:fleet", "write:operation-ledger"),
        timeout_seconds=60.0, max_identical_calls=2,
    )
    policies["service_control"] = replace(policies["ops_call"])
    policies["host_reboot"] = replace(policies["ops_call"], risk="critical")
    policies["operation_cancel"] = ToolPolicy(
        risk="high",
        idempotency="idempotent",
        side_effects=("write:operation-ledger", "cancel:operation"),
        timeout_seconds=30.0,
        approval="never",
        max_identical_calls=1,
    )
    policies["cluster_artifact_upload"] = ToolPolicy(
        risk="medium",
        idempotency="non-idempotent",
        side_effects=("read:sandbox", "write:artifact-store"),
        timeout_seconds=120.0,
        max_identical_calls=1,
    )
    policies["cluster_job_submit"] = ToolPolicy(
        risk="high",
        idempotency="keyed",
        side_effects=("allocate:cluster-resource", "write:job-ledger"),
        timeout_seconds=60.0,
        max_identical_calls=1,
    )
    policies["sandbox_create"] = ToolPolicy(
        risk="medium",
        idempotency="non-idempotent",
        side_effects=("write:sandbox", "allocate:resource"),
        timeout_seconds=310.0,
        compensation="none",
        max_identical_calls=1,
    )
    policies["sandbox_destroy"] = ToolPolicy(
        risk="high", idempotency="non-idempotent",
        side_effects=("write:sandbox", "destructive"), approval="never",
        timeout_seconds=90.0, max_identical_calls=1,
    )
    policies["run_subagents"] = ToolPolicy(
        risk="medium",
        idempotency="non-idempotent",
        side_effects=("spawn:agent", "model:multiple", "write:task-ledger"),
        timeout_seconds=3600.0,
        max_identical_calls=1,
    )
    policies["project_create"] = ToolPolicy(
        risk="medium", idempotency="keyed", side_effects=("write:project",), timeout_seconds=30,
    )
    policies["project_open"] = ToolPolicy(
        risk="medium", idempotency="idempotent", side_effects=("write:project",), timeout_seconds=30,
    )
    policies["project_set_rebuild"] = ToolPolicy(
        risk="medium", idempotency="non-idempotent", side_effects=("write:project",), timeout_seconds=30,
    )
    policies["project_snapshot"] = ToolPolicy(
        risk="medium", idempotency="keyed", side_effects=("write:project-version",), timeout_seconds=30,
    )
    policies["project_restore"] = ToolPolicy(
        risk="medium", idempotency="keyed", side_effects=("write:project", "write:project-version"), timeout_seconds=30,
    )
    policies["routine_set"] = ToolPolicy(
        risk="medium", idempotency="keyed", side_effects=("write:routine", "schedule:model"),
        timeout_seconds=15.0, max_identical_calls=1,
    )
    policies["routine_cancel"] = ToolPolicy(
        risk="medium", idempotency="idempotent", side_effects=("write:routine", "cancel:owned-job"),
        timeout_seconds=15.0, max_identical_calls=1,
    )
    policies["delegate_agent"] = ToolPolicy(
        risk="medium",
        idempotency="non-idempotent",
        side_effects=("spawn:agent", "model:single", "write:task-ledger"),
        timeout_seconds=1900.0,
        max_identical_calls=1,
    )
    policies["resume_subagent"] = ToolPolicy(
        risk="medium",
        idempotency="keyed",
        side_effects=("resume:agent", "model:multiple", "write:task-ledger"),
        timeout_seconds=3600.0,
        max_identical_calls=1,
    )
    policies["revise_subagent"] = ToolPolicy(risk="medium", idempotency="keyed",
        side_effects=("resume:agent", "write:task-ledger"), timeout_seconds=30, max_identical_calls=1)
    policies["import_agent_artifact"] = ToolPolicy(risk="low", idempotency="idempotent",
        side_effects=("write:sandbox",), timeout_seconds=90)
    policies["import_project_baseline"] = ToolPolicy(risk="low", idempotency="idempotent",
        side_effects=("write:sandbox",), timeout_seconds=120)
    policies["read_agent_result"] = ToolPolicy(risk="low", idempotency="pure", timeout_seconds=10)
    policies["read_task_evidence"] = ToolPolicy(risk="low", idempotency="pure", timeout_seconds=10)
    policies["read_task_evidence_batch"] = ToolPolicy(risk="low", idempotency="pure", timeout_seconds=10)
    policies["browser_clear"] = ToolPolicy(
        risk="critical",
        idempotency="idempotent",
        side_effects=("write:browser", "destructive"),
        timeout_seconds=60.0,
        approval="never",
        max_identical_calls=1,
    )
    policies["job_cancel"] = ToolPolicy(
        risk="critical",
        idempotency="idempotent",
        side_effects=("write:job", "destructive"),
        timeout_seconds=20.0,
        approval="never",
        max_identical_calls=1,
    )
    for name in {"memory_remove", "unpin_message", "reminder_cancel"}:
        previous = policies[name]
        policies[name] = ToolPolicy(
            risk="high",
            idempotency="idempotent",
            side_effects=previous.side_effects + ("destructive",),
            timeout_seconds=previous.timeout_seconds,
            approval="never",
            max_identical_calls=1,
        )
    # Leave time for one task-level QQ authorization before a server mutation.
    for name in {"ops_call", "service_control", "host_reboot", "operation_prepare", "operation_cancel", "cluster_job_submit", "cluster_guardian_create"}:
        policies[name] = replace(policies.get(name, ToolPolicy()), timeout_seconds=250.0)
    for name in {"ops_call", "service_control", "host_reboot", "operation_prepare", "operation_cancel"}:
        policies[name] = replace(policies[name], approval="task")
    return policies


TOOL_POLICIES = _policy_registry()
DEFAULT_TOOL_POLICY = ToolPolicy()
MCP_TOOL_POLICY = ToolPolicy(
    risk="high", idempotency="non-idempotent",
    side_effects=("execute:external", "write:external"),
    timeout_seconds=150.0, approval="task", max_identical_calls=1,
)
_TOOL_OVERRIDE_LOCK = threading.RLock()
_TOOL_ENABLED_OVERRIDES: dict[str, bool] = {}


def policy_for_tool(name: str) -> ToolPolicy:
    if name.startswith("mcp__"):
        # Only the MCP host's reviewed configuration can waive approval. Remote
        # readOnly/idempotent annotations never weaken loop or retry safeguards.
        return MCP_TOOL_POLICY
    return TOOL_POLICIES.get(name, DEFAULT_TOOL_POLICY)


def configure_tool_overrides(overrides: dict[str, bool]) -> None:
    with _TOOL_OVERRIDE_LOCK:
        _TOOL_ENABLED_OVERRIDES.clear()
        _TOOL_ENABLED_OVERRIDES.update(
            {
                str(name): bool(enabled)
                for name, enabled in overrides.items()
                if str(name) in TOOL_POLICIES
            }
        )


def set_tool_enabled(name: str, enabled: bool) -> None:
    if name not in TOOL_POLICIES:
        raise ValueError(f"unknown tool: {name}")
    with _TOOL_OVERRIDE_LOCK:
        _TOOL_ENABLED_OVERRIDES[name] = bool(enabled)


def tool_enabled(name: str) -> bool:
    with _TOOL_OVERRIDE_LOCK:
        return _TOOL_ENABLED_OVERRIDES.get(name, True)


def enabled_tool_definitions(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enabled: list[dict[str, Any]] = []
    for tool in tools:
        function = tool.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        if not isinstance(name, str) or tool_enabled(name):
            enabled.append(tool)
    return enabled


def admin_tool_manifest() -> list[dict[str, Any]]:
    with _TOOL_OVERRIDE_LOCK:
        overrides = dict(_TOOL_ENABLED_OVERRIDES)
    return [
        {
            "name": name,
            "enabled": overrides.get(name, True),
            "overridden": name in overrides,
            **policy.as_manifest(),
        }
        for name, policy in sorted(TOOL_POLICIES.items())
    ]


def policy_manifest_for_tools(tools: list[dict[str, Any]]) -> dict[str, Any]:
    names = []
    for tool in tools:
        function = tool.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            names.append(str(function["name"]))
    return {
        name: {
            **policy_for_tool(name).as_manifest(),
            "enabled": tool_enabled(name),
        }
        for name in sorted(set(names))
    }


_EXPLICIT_APPROVAL_TERMS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "browser_clear": (("清空", "重置", "删除", "clear"), ("浏览器", "cookie", "缓存", "profile")),
    "job_cancel": (("取消", "停止", "终止", "cancel"), ("任务", "job", "后台")),
    "memory_remove": (("删除", "忘掉", "移除", "remove"), ("记忆", "memory")),
    "unpin_message": (("取消固定", "取消置顶", "unpin"), ("消息", "msg", "固定", "置顶")),
    "reminder_cancel": (("取消", "删除", "cancel"), ("提醒", "reminder")),
}


def approval_from_user_text(
    user_text: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> ToolApproval:
    policy = policy_for_tool(tool_name)
    if tool_name.startswith("mcp__"):
        return ToolApproval(True, source="mcp-host", reason="外部工具由 MCP 宿主核对独立审批，聊天文字不能批准。")
    if policy.approval == "task":
        return ToolApproval(True, source="fleet-task", reason="实际服务器写操作由 Fleet 验证本任务统一授权。")
    if policy.approval == "never":
        return ToolApproval(True, source="policy", reason="无需额外批准。")
    normalized = " ".join(str(user_text).casefold().split())
    action_terms, target_terms = _EXPLICIT_APPROVAL_TERMS.get(
        tool_name,
        (("确认", "批准", "同意", "approve"), (tool_name.casefold(),)),
    )
    argument_targets = tuple(
        str(value).casefold()
        for value in arguments.values()
        if isinstance(value, (str, int)) and str(value).strip()
    )
    target_is_explicit = any(term in normalized for term in target_terms) or any(
        target in normalized for target in argument_targets
    )
    if any(term in normalized for term in action_terms) and target_is_explicit:
        return ToolApproval(
            True,
            source="current-user-message",
            reason="当前用户消息明确要求执行该危险操作。",
        )
    return ToolApproval(
        False,
        source="current-user-message",
        reason=(
            f"{tool_name} 属于高风险操作。请在当前消息中明确写出要执行的动作和对象，"
            "宿主确认是用户本人提出后才会执行。"
        ),
    )


@dataclass(frozen=True)
class ToolValidation:
    ok: bool
    errors: tuple[str, ...] = ()

    @property
    def message(self) -> str:
        return "；".join(self.errors) if self.errors else "工具参数无效。"


class ToolCatalog:
    """Host-owned lookup and a conservative JSON-schema validator."""

    def __init__(self, tools: list[dict[str, Any]], *, ignore_unknown_fields=False, policies=None) -> None:
        self.ignore_unknown_fields = ignore_unknown_fields
        self._policies = policies or {}
        self._functions: dict[str, dict[str, Any]] = {}
        for tool in tools:
            function = tool.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if isinstance(name, str) and name:
                self._functions[name] = function

    def contains(self, name: str) -> bool:
        return name in self._functions

    def policy(self, name: str) -> ToolPolicy:
        return self._policies.get(name) or policy_for_tool(name)

    def normalize(self, name, arguments):
        if not self.ignore_unknown_fields or name not in self._functions:
            return arguments
        dropped = 0
        def trim(value, schema):
            nonlocal dropped
            if isinstance(value, dict):
                properties = schema.get('properties', {})
                result = {}
                for key, item in value.items():
                    if schema.get('additionalProperties') is False and key not in properties:
                        dropped += 1
                    else:
                        result[key] = trim(item, properties.get(key, {}))
                return result
            if isinstance(value, list):
                return [trim(item, schema.get('items', {})) for item in value]
            return value
        result = trim(arguments, self._functions[name].get('parameters', {}))
        if dropped:
            logging.getLogger(__name__).warning('Ignored %d extra argument fields for tool %s', dropped, name)
        return result

    def validate(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        parse_error: str = "",
    ) -> ToolValidation:
        if parse_error:
            return ToolValidation(False, (parse_error,))
        function = self._functions.get(name)
        if function is None:
            return ToolValidation(
                False,
                (f"工具 {name or '[空名称]'} 未向本轮开放。",),
            )
        schema = function.get("parameters")
        if not isinstance(schema, dict):
            return ToolValidation(False, ("工具参数 schema 缺失。",))
        errors: list[str] = []
        _validate_value(arguments, schema, "$", errors, depth=0,
                        allow_patterns=not name.startswith("mcp__"))
        return ToolValidation(not errors, tuple(errors[:8]))

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._functions)


def _validate_value(
    value: Any,
    schema: dict[str, Any],
    path: str,
    errors: list[str],
    *,
    depth: int,
    allow_patterns: bool = True,
) -> None:
    if depth > 12:
        errors.append(f"{path} 嵌套过深。")
        return
    expected = schema.get("type")
    if isinstance(expected, list):
        allowed_types = tuple(str(item) for item in expected)
    elif isinstance(expected, str):
        allowed_types = (expected,)
    else:
        allowed_types = ()
    if allowed_types and not any(
        _matches_type(value, item) for item in allowed_types
    ):
        errors.append(
            f"{path} 类型应为 {'/'.join(allowed_types)}，"
            f"实际是 {_type_name(value)}。"
        )
        return

    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        errors.append(f"{path} 不在允许值 {enum!r} 中。")
        return

    if isinstance(value, dict):
        properties = schema.get("properties")
        property_map = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        required_names = required if isinstance(required, list) else []
        for name in required_names:
            if isinstance(name, str) and name not in value:
                errors.append(f"{path}.{name} 是必填字段。")
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(property_map))
            if unknown:
                errors.append(
                    f"{path} 包含未声明字段：{', '.join(unknown[:8])}。"
                )
        for key, item in value.items():
            child_schema = property_map.get(key)
            if isinstance(child_schema, dict):
                _validate_value(
                    item,
                    child_schema,
                    f"{path}.{key}",
                    errors,
                    depth=depth + 1,
                    allow_patterns=allow_patterns,
                )
        return

    if isinstance(value, list):
        minimum_items = _safe_number(schema.get("minItems"))
        maximum_items = _safe_number(schema.get("maxItems"))
        if minimum_items is not None and len(value) < minimum_items:
            errors.append(f"{path} 至少需要 {int(minimum_items)} 项。")
        if maximum_items is not None and len(value) > maximum_items:
            errors.append(f"{path} 最多允许 {int(maximum_items)} 项。")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value[:100]):
                _validate_value(
                    item,
                    item_schema,
                    f"{path}[{index}]",
                    errors,
                    depth=depth + 1,
                    allow_patterns=allow_patterns,
                )
        return

    if isinstance(value, str):
        minimum_length = _safe_number(schema.get("minLength"))
        maximum_length = _safe_number(schema.get("maxLength"))
        if minimum_length is not None and len(value) < minimum_length:
            errors.append(f"{path} 至少需要 {int(minimum_length)} 个字符。")
        if maximum_length is not None and len(value) > maximum_length:
            errors.append(f"{path} 最多允许 {int(maximum_length)} 个字符。")
        pattern = schema.get("pattern")
        # Remote schemas are useful to the model but are not trusted code. A
        # backtracking regex must not run on the Bot's event-loop thread.
        if allow_patterns and isinstance(pattern, str):
            try:
                matched = re.search(pattern, value) is not None
            except re.error:
                errors.append(f"{path} 的宿主 schema 正则无效。")
            else:
                if not matched:
                    errors.append(f"{path} 格式不符合宿主 schema。")
        return

    if _is_number(value):
        minimum = _safe_number(schema.get("minimum"))
        maximum = _safe_number(schema.get("maximum"))
        if minimum is not None and value < minimum:
            errors.append(f"{path} 不能小于 {minimum:g}。")
        if maximum is not None and value > maximum:
            errors.append(f"{path} 不能大于 {maximum:g}。")


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return _is_number(value)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _safe_number(value: Any) -> float | None:
    if not _is_number(value):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if -float("inf") < number < float("inf") else None


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return type(value).__name__
