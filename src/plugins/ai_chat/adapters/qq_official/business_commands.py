"""Host assembly for native official business commands; no direct transport."""
from dataclasses import replace

from ...application.commands import CommandResult
from .commands import CommandEffectDenied


def build_business_services(runtime, app):
    """OfficialTurnConsumer optional business_services hook.

    Neither model arguments nor an inbound DTO can manufacture lifecycle,
    token, job, or platform.
    """
    def factory(life,inbound,platform,services):
        if (platform.execution.handle is not life.handle or platform.execution.job is not life.job
                or platform.execution.token is not life.token or platform.execution.admission is not life.admission
                or inbound.endpoint!=life.handle.endpoint or inbound.actor!=life.handle.actor):
            raise CommandEffectDenied('business_command_source_mismatch')
        callbacks={}
        store=getattr(app,'conversations',None)
        if store is not None:
            async def memory(current,parsed):
                text=await runtime._calls(store.memory,current.scope.key)
                return CommandResult('ok',text.strip() or '（这个会话还没有记忆。）')

            async def clear(current,parsed):
                def run():
                    latest=app.message_ledger.history(current.scope,limit=1,newest=True)
                    store.set_tail_after(current.scope.key,latest[-1].canonical_message_id if latest else 0)
                await runtime._calls(run)
                return CommandResult('ok','上下文已清空。之前的聊天记录还在，需要时我会用 history 查。')
            callbacks.update(memory=memory,clear=clear)
        return replace(services,**callbacks)
    return factory
