from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from .contracts import AgentSpec, SubAgentRole, WorkerRole


COMMON_READ_TOOLS = frozenset(
    {
        "get_message_by_id",
        "search_messages",
        "context_expand",
        "context_search",
        "memory_list",
        "inspect_source",
        "inspect_shared_content",
        "get_shared_content",
        "view_forward",
        "view_bilibili",
        "list_recent_files",
        "job_status",
        "say",
        "fleet_overview",
        "host_inspect",
        "service_inspect",
        "model_status",
        "operation_status",
        "cluster_job_status",
    }
)
SANDBOX_TOOLS = frozenset(
    {
        "sandbox_create",
        "sandbox_destroy",
        "sandbox_list",
        "nix_search",
        "sandbox_exec",
        "sandbox_write_file",
        "sandbox_read_file",
        "import_file_to_sandbox",
        "job_cancel",
    }
)
BROWSER_TOOLS = frozenset(
    {
        "web_search",
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_press_key",
        "browser_wait_for",
        "browser_scroll",
        "browser_close",
        "browser_clear",
    }
)


AGENT_SPECS: dict[SubAgentRole, AgentSpec] = {
    "supervisor": AgentSpec(
        role="supervisor",
        title="主控",
        description="拆分目标、检查依赖、验收结果并统一回复。",
        instructions="只负责任务设计和验收，不亲自调用执行工具。",
        allowed_tools=frozenset(),
        model_policy="reasoning",
        context_channels=frozenset(
            {"conversation", "supporting", "evidence", "artifacts", "memory"}
        ),
        context_budget_chars=8000,
    ),
    "researcher": AgentSpec(
        role="researcher",
        title="搜索",
        description="搜索互联网、浏览网页并交叉核实来源。",
        instructions=(
            "优先使用一手来源；区分事实、推断和未知信息。最终给出完整链接、"
            "关键事实、冲突信息和仍未确认的内容。"
        ),
        allowed_tools=COMMON_READ_TOOLS | BROWSER_TOOLS,
        model_policy="fast",
        context_channels=frozenset(
            {"conversation", "supporting", "evidence", "upstream"}
        ),
        context_budget_chars=6200,
    ),
    "coder": AgentSpec(
        role="coder",
        title="代码",
        description="在隔离沙盒中编写、运行和验证代码。",
        instructions=(
            "所有代码和命令必须在任务沙盒中执行。完成前检查实际输出；需要交付时"
            "返回真实文件句柄，由宿主验收后发送，并报告执行结果和未解决问题。"
            "已上传集群的产物，在对应 artifacts 条目保留工具返回的 artifact_id；"
            "验证或转交未修改的上游文件时一并保留该 ID，供发布步骤复用。"
        ),
        allowed_tools=COMMON_READ_TOOLS | BROWSER_TOOLS | SANDBOX_TOOLS | {
            "use_skill", "cluster_artifact_upload", "cluster_job_submit"
        },
        model_policy="coding",
        max_turns=20,
        timeout_seconds=1200,
        risk_level="controlled-write",
        background_default=True,
        context_channels=frozenset(
            {"conversation", "supporting", "evidence", "artifacts", "upstream"}
        ),
        context_budget_chars=7200,
    ),
    "document": AgentSpec(
        role="document",
        title="文件",
        description="读取群文件、PDF、表格和文档并生成交付物。",
        instructions=(
            "先取得真实文件，再解析内容；不得根据文件名猜测。生成文档后检查文件"
            "存在且可读取，并通过文件句柄交付。含中文的 PDF 必须使用沙盒里的 "
            "gaoji-pdf 生成，再用 pdffonts 检查字体嵌入、pdftotext 检查中文；"
            "验收失败不得发送。"
            "已上传集群的产物，在对应 artifacts 条目保留工具返回的 artifact_id；"
            "验证或转交未修改的上游文件时一并保留该 ID，供发布步骤复用。"
        ),
        allowed_tools=(
            COMMON_READ_TOOLS
            | SANDBOX_TOOLS
            | {"read_image_text", "view_image", "use_skill"}
            | {"cluster_artifact_upload", "cluster_job_submit"}
        ),
        model_policy="document",
        max_turns=20,
        timeout_seconds=1200,
        risk_level="controlled-write",
        background_default=True,
        context_channels=frozenset(
            {"conversation", "supporting", "evidence", "artifacts", "upstream"}
        ),
        context_budget_chars=7600,
    ),
    "media": AgentSpec(
        role="media",
        title="媒体",
        description="理解图片、视频、字幕、语音和平台分享内容。",
        instructions=(
            "必须先实际读取媒体再评价。长视频先看元数据、字幕和关键帧；明确指出"
            "可观察内容、推断内容和无法确认的部分。"
        ),
        allowed_tools=(
            COMMON_READ_TOOLS
            | BROWSER_TOOLS
            | {"read_image_text", "view_image", "view_video", "transcribe_voice"}
            | {"cluster_artifact_upload", "cluster_job_submit"}
        ),
        model_policy="vision",
        max_turns=16,
        timeout_seconds=1900,
        background_default=True,
        context_channels=frozenset(
            {"conversation", "supporting", "evidence", "artifacts", "upstream"}
        ),
        context_budget_chars=6800,
    ),
    "analyst": AgentSpec(
        role="analyst",
        title="分析",
        description="整理数据、比较证据、计算并形成可审计结论。",
        instructions=(
            "先确定统计口径，再计算和比较。结论必须对应证据；发现缺失数据时明确"
            "说明，不要用猜测补齐。"
        ),
        allowed_tools=(
            COMMON_READ_TOOLS
            | SANDBOX_TOOLS
            | {"query_alerts", "pin_message", "group_members"}
        ),
        model_policy="reasoning",
        risk_level="controlled-write",
        context_channels=frozenset(
            {"conversation", "supporting", "evidence", "artifacts", "upstream"}
        ),
        context_budget_chars=7000,
    ),
    "operator": AgentSpec(
        role="operator",
        title="运维",
        description="检查 gaoji、告警、任务、数据库和运行状态。",
        instructions=(
            "默认只读检查。涉及停止、重启、删除或修改服务时必须遵守宿主审批策略；"
            "报告影响范围、当前状态和建议动作。"
            "服务启停用 service_control，整机重启用 host_reboot；其他命令先从 ops_catalog 核对接口。"
            "只读任务不等于 diagnostic 环境拥有所需权限；profile 名称和超时上限不能证明其 UID、PATH 或目录访问权。"
            "长时间采集前先小范围核对实际身份、必需程序和目标目录可读性；预检命令同样走宿主授权。"
            "权限或程序缺失时报告具体缺口，不能偷偷 sudo、切换更高权限 profile、安装依赖或重跑整盘扫描。"
            "若需换环境，必须显式提出新操作并遵守本任务审批和执行次数约束；不要把旧批准当作提权许可。"
            "磁盘报告区分 df 的文件系统占用、du -B1 的分配空间和 du -b 的逻辑大小；"
            "du 权限错误或超时后的数字只是部分统计，不能当作目录总量，也不能凭空推算可清理空间。"
            "当前状态检查可合并 host_inspect 与 host.metrics 的同一有效采样周期；"
            "只要所需维度完整且满足新鲜度，就不必为了不同时间戳重复查询。"
            "只有用户要求趋势、动作效果或前后对比时才要求不同周期的相应证据，不能把重复缓存样本当作变化证据。"
            "排队或回执丢失不代表成功，必须读取原 operation_status 的验收证据，不得换编号重复重启。"
            "发布静态预览时先检查上游索引的 cluster_artifacts，并用 read_agent_result"
            "读取对应 artifacts、handoff 或 metadata 核对文件和 artifact_id；"
            "已有已上传文件的 artifact_id 可直接传给 cluster_job_submit(kind=preview.static)，不要重复上传。"
            "静态压缩包根目录必须有 index.html，不能夹带密钥、隐藏配置和依赖目录；"
            "queued/running 仅代表发布中，成功后保留 public_url、expires_at 和 job_id，不能猜测链接。"
            "若只有宿主快照，先 sandbox_create 创建自己的隔离沙盒，再用 import_agent_artifact"
            "导入直接依赖步骤的目标文件，使用返回的 path 调用 cluster_artifact_upload，"
            "再提交预览任务。不得访问上游原容器或使用 SSH、主机命令绕过授权。"
            "多个产物时核对可发布的静态站点包，不把源码包自动当作可发布站点；"
            "没有读取交接内容前不得声称缺少 ID。用 cluster_job_status 确认发布结果与 URL。"
        ),
        allowed_tools=(
            COMMON_READ_TOOLS
            | {
                "query_alerts",
                "fleet_overview",
                "host_inspect",
                "service_inspect",
                "model_status",
                "service_logs",
                "diagnose_incident",
                "operation_prepare",
                "ops_catalog",
                "ops_call",
                "service_control",
                "host_reboot",
                "operation_cancel",
                "cluster_job_submit",
                "cluster_artifact_upload",
                "sandbox_create",
                "sandbox_list",
                "job_status",
                "group_members",
            }
        ),
        model_policy="operations",
        risk_level="privileged",
        max_attempts=1,
        context_channels=frozenset(
            {"conversation", "supporting", "evidence", "upstream"}
        ),
        context_budget_chars=5200,
    ),
}


class AgentRegistry:
    def __init__(self, specs: Mapping[SubAgentRole, AgentSpec]) -> None:
        copied = dict(specs)
        for role, spec in copied.items():
            if role != spec.role:
                raise ValueError(f"Agent registry key {role} does not match {spec.role}")
            if spec.version < 1:
                raise ValueError(f"Agent {role} has an invalid version")
        self._specs: Mapping[SubAgentRole, AgentSpec] = MappingProxyType(copied)

    def get(self, role: str) -> AgentSpec:
        try:
            return self._specs[role]  # type: ignore[index]
        except KeyError as exc:
            raise ValueError(f"Unknown Sub-Agent role: {role}") from exc

    def worker(self, role: str) -> AgentSpec:
        spec = self.get(role)
        if spec.role == "supervisor":
            raise ValueError("Supervisor cannot execute worker tasks")
        return spec

    @property
    def worker_roles(self) -> tuple[WorkerRole, ...]:
        return tuple(
            role  # type: ignore[misc]
            for role in self._specs
            if role != "supervisor"
        )

    def manifest(self) -> list[dict[str, object]]:
        return [spec.manifest() for spec in self._specs.values()]

    def planning_tools(self, enabled: set[str] | None = None) -> dict[str, object]:
        roles = {role: set(self.worker(role).allowed_tools) - {"say"} for role in self.worker_roles}
        if enabled is not None:
            roles = {role: names & enabled for role, names in roles.items()}
        common = set.intersection(*roles.values()) if roles else set()
        return {"shared_tools": sorted(common), "role_tools": {
            role: sorted(names - common) for role, names in roles.items()}}


DEFAULT_AGENT_REGISTRY = AgentRegistry(AGENT_SPECS)
WORKER_ROLES = DEFAULT_AGENT_REGISTRY.worker_roles
