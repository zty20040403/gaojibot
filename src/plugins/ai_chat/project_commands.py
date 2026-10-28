"""Human-readable project commands over the shared, permission-checked registry."""

from datetime import datetime
import json
from zoneinfo import ZoneInfo

from nonebot.adapters.onebot.v11 import MessageSegment

from .tools import domain_registry
from .tools.state import ToolContext, TurnState


HELP = (
    "/项目\n/项目 新建 名称\n/项目 切换 prj#3\n/项目 状态 [prj#3]\n/项目 版本 [prj#3]"
    "\n/项目 保存 [标签]\n/项目 保存 prj#3 [标签]"
    "\n/项目 回滚 v2\n/项目 回滚 prj#3 v2"
)
STATES = {
    "active": "运行中",
    "stopped": "已停止",
    "hibernated": "休眠中",
    "archived": "已归档",
    "trash": "回收站",
    "purged": "已删除",
}


def _summary(project):
    state = STATES.get(project["status"], project["status"])
    if project["preparing"]:
        state = {
            "pending": "排队中",
            "running": "准备中",
            "failed": "准备失败",
            "needs_attention": "需要处理",
            "cancelled": "已取消",
            "unknown": "状态待核实",
        }.get(project.get("operation_status"), "有待处理操作")
    return f"{project['handle']} {project['title']} · {state} · 配置修订 {project['revision']}"


def _bytes(value):
    return "未知" if value is None else f"{value / 1024**2:.1f} MiB"


async def command_result(services, event, text):
    parts = text.strip().split(maxsplit=1)
    action, rest = (
        (parts[0], parts[1] if len(parts) > 1 else "") if parts else ("列表", "")
    )
    if action in {"帮助", "help"}:
        return HELP
    action = {
        "list": "列表",
        "create": "新建",
        "open": "切换",
        "status": "状态",
        "versions": "版本",
        "save": "保存",
        "snapshot": "保存",
        "快照": "保存",
        "restore": "回滚",
    }.get(action, action)
    if action not in {"列表", "新建", "切换", "状态", "版本", "保存", "回滚"}:
        return HELP
    if action == "列表" and rest or action in {"新建", "切换"} and not rest:
        return HELP
    names = {
        "列表": "project_list",
        "新建": "project_create",
        "切换": "project_open",
        "状态": "project_status",
        "版本": "project_versions",
        "保存": "project_snapshot",
        "回滚": "project_restore",
    }
    arguments = (
        {"title": rest}
        if action == "新建"
        else ({"project_handle": rest} if rest else {})
    )
    if action == "保存":
        save_parts = rest.split(maxsplit=1)
        target = save_parts[0] if save_parts else ""
        label = save_parts[1] if len(save_parts) > 1 else ""
        arguments = (
            {"project_handle": target, "label": label}
            if target.startswith("prj#") else {"label": rest}
        )
    if action == "回滚":
        targets = rest.split()
        if len(targets) == 1:
            arguments = {"version_handle": targets[0]}
        elif len(targets) == 2:
            arguments = {"project_handle": targets[0], "version_handle": targets[1]}
        else:
            return HELP
    turn = TurnState(ToolContext(services, event, frozenset({"projects"})))
    result = json.loads(await domain_registry.execute(turn, names[action], arguments))
    if not result["ok"]:
        return result["error"]
    if action == "列表":
        return (
            "\n".join(
                _summary(p) + (" · 当前" if p["handle"] == result["current"] else "")
                for p in result["projects"]
            )
            or "当前会话还没有项目。\n" + HELP
        )
    lines = [_summary(result["project"])]
    if result.get("operation") and "completed_steps" in result["operation"]:
        operation = result["operation"]
        lines.append(
            f"后台操作进度：{operation['completed_steps']}/{operation['total_steps']} 步"
        )
    if action == "新建":
        lines.append("项目已登记；准备中表示工作区还在后台唤醒，并非任务已完成。")
        if not result["current"]:
            lines.append("这是此前已创建的项目；保留你后来选择的当前项目。")
    if action == "切换":
        lines.append(
            "已切换；你只有查看权限。" if result["readonly"] else "已切换当前项目。"
        )
    if action == "状态" and result.get("autosaves"):
        save = result["autosaves"][0]
        if save["saved"]:
            description = "已保存为 " + save["version"]
        elif save["status"] == "skipped":
            description = "未发生已确认的修改，无需保存"
        elif save["reason"] == "unknown_execution":
            description = "执行结果待核实，不会重复执行命令"
        elif save["reason"] == "permission_unavailable":
            description = "权限暂不可用，保留数据等待恢复"
        elif save["status"] in {"failed", "needs_attention", "cancelled"}:
            description = "保存未完成，需要处理"
        elif save["status"] == "pending":
            description = "等待本轮结束、工作区空闲和文件投递确认"
        else:
            description = "已排队或正在保存，尚未完成"
        lines.append("自动保存 " + save["turn"] + "：" + description)
    if action in {"保存", "状态"} and result.get("snapshot"):
        snapshot = result["snapshot"]
        if snapshot["saved"]:
            lines.append("最近保存已完成：" + snapshot["version"])
        elif snapshot["status"] in {"failed", "needs_attention", "cancelled"}:
            lines.append("版本保存未完成，需要处理；不能当作已保存。")
        else:
            lines.append("版本保存已排队或执行中，完成后可通过 /项目 状态 查看版本号。")
    if action in {"回滚", "状态"} and result.get("restore"):
        restore = result["restore"]
        if restore["restored"]:
            lines.append(f"最近回滚已核实：{restore['source_version']} 的内容恢复为 {restore['version']}，历史版本保留。")
        elif restore["status"] in {"failed", "needs_attention", "cancelled"}:
            lines.append("回滚尚未完成，需要处理；保留恢复任务，不能当作已恢复。")
        else:
            lines.append("回滚已排队或执行中；先保存当前内容，再恢复目标版本，通过 /项目 状态 查看结果。")
    if action == "状态":
        usage = result["usage"]
        lines.append(
            f"{'软配额' if usage['quota_mode'] == 'soft' else '硬配额'}："
            f"{_bytes(usage['used_bytes'])} / {_bytes(usage['quota_bytes'])}"
        )
        lines.append(
            f"可重建数据：{_bytes(usage['rebuildable_bytes'])}；版本库：{_bytes(usage['version_bytes'])}"
        )
        if usage["sampled_at"] is not None:
            stamp = datetime.fromtimestamp(
                usage["sampled_at"], ZoneInfo("Asia/Shanghai")
            )
            lines.append(f"采样时间：{stamp:%Y-%m-%d %H:%M:%S} (Asia/Shanghai)")
        if not usage["complete"]:
            lines.append("用量尚未测得或已过期，不能按零计算。")
        if result["project"]["task_handle"]:
            lines.append("正在执行：" + result["project"]["task_handle"])
    if action in {"状态", "版本"}:
        lines.extend(
            f"{v['handle']} {v['label']}" + (" [固定]" if v["pinned"] else "")
            for v in result["versions"][:10]
        )
        if not result["versions"]:
            lines.append("还没有保存版本。")
        elif len(result["versions"]) > 10:
            lines.append(f"仅显示最近 10 个版本，共返回 {len(result['versions'])} 个。")
    return "\n".join(lines)


async def handle_project_command(services, event, args):
    from .matchers import project_command

    await project_command.finish(
        MessageSegment.text(
            await command_result(services, event, args.extract_plain_text())
        )
    )
