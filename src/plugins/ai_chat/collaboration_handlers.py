"""Authenticated QQ boundary for explicit task grants and collaborative revision."""

from __future__ import annotations

import re

from nonebot.adapters.onebot.v11 import GroupMessageEvent

from .agent import ContextPacket
from .agent.collaboration_runtime import group_roster
from .onebot_codec import scope_from_event
from .tool_policy import tool_enabled
from .workers.durable_jobs import JobDeferred


HELP = ("/协作 task#编号：查看参与者和意见\n"
        "/协作 task#编号 添加 @群友 / 移除 @群友（仅发起人）\n"
        "/协作 task#编号 意见 修改建议\n"
        "/协作 task#编号 撤回 f#编号\n"
        "/协作 task#编号 汇总 [补充要求]：把意见合成一次修订，使用操作人的权限。")


async def collaboration_access(services, bot, event, task_id, *, merging=False):
    app = services.context
    if not isinstance(event, GroupMessageEvent):
        raise ValueError("请在任务原来的 QQ 群里操作，私聊任务不能共享。")
    if app.subagent_store is None or app.message_ledger is None or app.subagent_coordinator is None:
        raise ValueError("Sub-Agent 协作功能尚未开启。")
    if not await app.db_call(services.group_enabled, event.group_id):
        raise ValueError("当前群已停用。")
    members = await group_roster(bot, event)
    if event.user_id not in members:
        raise ValueError("无法确认你仍在这个群里。")
    member = members[event.user_id]
    await app.db_call(app.message_ledger.ensure_principal_identity, "onebot-v11", event.user_id,
                      str(member.get("card") or member.get("nickname") or event.user_id))
    view = await app.db_call(app.subagent_store.collaboration, task_id,
                            scope_key=scope_from_event(event).key, requester_user_id=event.user_id)
    task = await app.db_call(app.subagent_store.get, task_id)
    control = await app.db_call(app.subagent_store.control, task_id)
    if str(control["dispatch"].get("bot_id")) != str(bot.self_id):
        raise ValueError("请使用这个任务原来的机器人账号。")
    if task.requester_user_id not in members:
        raise ValueError("任务发起人已不在原群，不能继续共享任务。")
    if merging:
        for item in view["feedback"]:
            if item["status"] != "pending":
                continue
            identity = await app.db_call(app.message_ledger.native_identity_for_principal,
                                         "onebot-v11", item["principal_id"])
            if identity is None or int(identity[0]) not in members:
                raise ValueError(f"f#{item['feedback_id']} 来自已离群成员，请先撤回该意见或移除该参与者。")
    return task, view, members


async def collaboration_command(services, bot, event, args):
    match = re.fullmatch(r"task#?([1-9][0-9]*)(?:\s+(查看|添加|移除|意见|撤回|汇总)(?:\s+([\s\S]*))?)?",
                         args.extract_plain_text().strip(), re.I)
    if not match:
        return HELP
    task_id, action, text = int(match[1]), match[2] or "查看", (match[3] or "").strip()
    app = services.context
    try:
        task, view, members = await collaboration_access(services, bot, event, task_id, merging=action == "汇总")
        scope = scope_from_event(event).key
        fields = dict(scope_key=scope, requester_user_id=event.user_id, expected_version=view["version"])
        if action in {"添加", "移除"}:
            if not view["owner"]:
                return "只有任务发起人可以添加或移除协作者。"
            mentions = [str(segment.data.get("qq", "")) for segment in args if segment.type == "at"]
            if len(mentions) != 1 or not mentions[0].isdigit() or text:
                return f"用法：/协作 task#{task_id} {action} @一位群友"
            target = int(mentions[0])
            if action == "添加":
                if target not in members or str(target) == str(bot.self_id):
                    return "只能添加当前群内的真实成员。"
                member = members[target]
                principal_id = await app.db_call(app.message_ledger.ensure_principal_identity, "onebot-v11", target,
                    str(member.get("card") or member.get("nickname") or target))
            else:
                principal_id = await app.db_call(app.message_ledger.principal_id_for_native, "onebot-v11", target)
                if principal_id is None:
                    return "这位群友不是该任务的协作者。"
            await app.db_call(app.subagent_store.set_collaborator, task_id, **fields,
                             principal_id=principal_id, enabled=action == "添加")
            return f"task#{task_id} 协作者已{action}；不授予发起人的账号、个人记忆或服务器权限。"
        if action == "意见":
            result = await app.db_call(app.subagent_store.add_revision_feedback, task_id, **fields,
                instruction=text, request_key=f"onebot:{bot.self_id}:{event.message_id}")
            item = result["feedback"]
            return f"task#{task_id} 意见 f#{item['feedback_id']} 已记录（{item['status']}）；汇总时统一修订，不逐条重跑。"
        if action == "撤回":
            number = re.fullmatch(r"f#?([1-9][0-9]*)", text)
            if not number:
                return "请指定意见编号，例如：/协作 task#12 撤回 f#3"
            await app.db_call(app.subagent_store.dismiss_revision_feedback, task_id, int(number[1]), **fields)
            return f"意见 f#{number[1]} 已撤回。"
        if action == "汇总":
            if not tool_enabled("revise_subagent"):
                return "管理员已关闭任务修订。"
            if not any(item["status"] == "pending" for item in view["feedback"]):
                return "没有待合并的意见，没有启动新修订。"
            runs = await app.db_call(app.subagent_store.runs, task_id)
            steps = [run.step_key for run in runs if not run.step_key.startswith("acceptance_r")
                     and not run.result.get("metadata", {}).get("superseded_by_revision")]
            instruction = text or "汇总待处理意见，保留未变更要求；冲突意见明确说明，不能偷偷删除验收条件。"
            packet = ContextPacket(scope, f"group:{event.group_id}:user:{event.user_id}", event.user_id,
                                   None, instruction)
            result = await app.db_call(app.subagent_coordinator.revise, task_id, **fields,
                instruction=instruction, step_keys=steps, include_feedback=True,
                revision_context=packet, revision_event=event.model_dump(mode="json"))
            return f"task#{task_id} 第 {result['revision']} 版已排队，主控会统一整理意见后执行，结果发回原群。"
        people = []
        for item in view["members"]:
            if item["enabled"]:
                identity = await app.db_call(app.message_ledger.native_identity_for_principal,
                                             "onebot-v11", item["principal_id"])
                people.append(identity[1] if identity else f"成员 #{item['principal_id']}")
        pending = [item for item in view["feedback"] if item["status"] == "pending"]
        lines = [f"task#{task_id} · 第 {view['revision']} 版 · 配置版本 {view['version']}",
                 "协作者：" + ("、".join(people) or "尚未添加"), f"待合并意见：{len(pending)} 条"]
        lines.extend(f"f#{item['feedback_id']} · 成员 #{item['principal_id']}：{item['instruction'][:150]}" for item in pending[:5])
        return "\n".join(lines)
    except (ValueError, PermissionError, JobDeferred) as exc:
        return str(exc)
