"""Native /config authority: original authenticated author, never model content."""
import hashlib

from ...application.commands import CommandResult
from ...conversation_config import change,display,parse,snapshot
from .message_roles import source_role


def command(runtime,life,app):
    async def invoke(inbound,request):
        try:
            name,value = parse(request.argument)
        except ValueError:
            return CommandResult('syntax','用法：/config、/config <名称>、/config <名称> <值或default>。布尔值用on/off；范围请先查看/config。')
        def execute():
            with life.effect_scope(inbound) as cursor:
                if value is not None:
                    now = int(runtime.store.clock())
                    role = source_role(cursor,inbound.endpoint,inbound.actor,life.handle.canonical_message_id,now=now)
                    owner = runtime.store.authority.owner_for_actor(cursor,inbound.endpoint,inbound.actor)
                    if inbound.endpoint.kind!='private' and role not in {'owner','admin'} and owner is None:
                        return CommandResult('denied','只有本群群主、管理员或宿主 owner 能改本会话配置；普通成员可用/config查看。')
                    if name=='image_captions' and value and (getattr(app,'vision_worker',None) is None or getattr(app,'media_library',None) is None):
                        return CommandResult('unavailable','宿主图片说明服务未配置，本次没有修改。')
                    values = change(cursor,inbound.endpoint,name,value,
                        actor='qq:'+hashlib.sha256(inbound.actor.key.encode()).hexdigest(),now=now)
                    return CommandResult('ok','已保存本会话配置；不改变/stop状态。\n'+display(values,name))
                return CommandResult('ok','本会话配置（物理入口，不跟随历史别名）：\n'+display(snapshot(cursor,inbound.endpoint),name))
        return await runtime._calls(execute)
    return invoke
