"""Shared command grammar and presentation; host callback owns effects."""

import re


HELP = (
    "/例行 列表\n"
    "/例行 添加 晨报 | 0 9 * * * | 汇总昨晚群聊\n"
    "/例行 修改 routine#1 v1 | 0 10 * * * | 汇总群聊和新闻\n"
    "/例行 取消 routine#1 v1\n"
    "默认时区 Asia/Shanghai；v1 是列表中的版本号。"
)


def _target(text: str) -> tuple[str, int]:
    match = re.fullmatch(r"(routine#[1-9][0-9]*)\s+v([1-9][0-9]*)", text)
    if match is None:
        raise ValueError("请填写任务句柄和版本，如 routine#1 v1；用 /例行 列表 查看。")
    return match[1], int(match[2])


def _contract(item):
    until=item.get('authorization_until')
    if type(until) is not int:return ''
    from datetime import datetime, timezone
    label=datetime.fromtimestamp(until,timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    return f"\n授权至 {label}；单次执行 {item['execution_seconds']} 秒，结果期限 {item['delivery_seconds']} 秒。"


async def command_result(text, call, *, explicit_targets=False) -> str:
    parts = text.strip().split(maxsplit=1)
    action, rest = (
        (parts[0], parts[1] if len(parts) > 1 else "") if parts else ("列表", "")
    )
    if action in {"help", "帮助"}:
        return HELP
    if action in {"列表", "list", "修改", "edit"}:
        listed = await call("routine_list", {})
        if not listed["ok"]:
            return listed["error"]
    if action in {"列表", "list"}:
        return (
            "\n".join(
                f"{item['handle']} v{item['version']} · {item['name']} · "
                f"{'启用' if item['enabled'] else '关闭'}\n"
                f"{item['cron']} ({item['timezone']}) · 下次 {item['next_due_at']}" + _contract(item)
                for item in listed["routines"]
            )
            or "你还没有例行任务。\n" + HELP
        )
    try:
        if action in {"取消", "cancel"}:
            handle, version = _target(rest)
            result = await call(
                "routine_cancel",
                {"routine_handle": handle, "expected_version": version},
            )
            return f"已取消 {handle}。" if result["ok"] else result["error"]
        if action not in {"添加", "add", "修改", "edit"}:
            return HELP
        fields = [value.strip() for value in rest.split("|", 2)]
        if len(fields) != 3 or not all(fields):
            return HELP
        name, cron, prompt = fields
        arguments = {"name": name, "cron": cron, "prompt": prompt}
        if action in {"修改", "edit"}:
            handle, version = _target(name)
            item = next(
                (item for item in listed["routines"] if item["handle"] == handle), None
            )
            if item is None:
                return "当前会话没有这个由你创建的任务。"
            arguments.update(
                {
                    key: item[key]
                    for key in (
                        "name",
                        "timezone",
                        "max_tokens",
                        "daily_tokens",
                        "daily_runs",
                    )
                }
            )
            arguments["expected_version"] = version
            if explicit_targets:
                arguments["routine_handle"] = handle
        result = await call("routine_set", arguments)
        if not result["ok"]:
            return result["error"]
        item = result["routine"]
        return f"已保存 {item['handle']} v{item['version']}：{item['name']}\n下次执行 {item['next_due_at']}" + _contract(item)
    except ValueError as exc:
        return str(exc)
