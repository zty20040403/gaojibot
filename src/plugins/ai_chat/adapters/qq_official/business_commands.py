"""Host assembly for native official business commands; no direct transport."""
from dataclasses import replace
from types import SimpleNamespace

from ...application.commands import CommandResult
from ...tools.state import ToolContext
from .commands import CommandEffectDenied
from .memory_commands import MemoryCommands
from .project_commands import ProjectCommands


def build_business_services(runtime, app):
    """OfficialTurnConsumer optional business_services hook.

    Return callbacks now, but issue project/routine sources only when invoked
    after the consumer durably begins the exact command. Neither model arguments
    nor an inbound DTO can manufacture lifecycle, token, job, or platform.
    """
    def factory(life,inbound,platform,services):
        if (platform.execution.handle is not life.handle or platform.execution.job is not life.job
                or platform.execution.token is not life.token or platform.execution.admission is not life.admission
                or inbound.endpoint!=life.handle.endpoint or inbound.actor!=life.handle.actor):
            raise CommandEffectDenied('business_command_source_mismatch')
        callbacks={}
        if app.long_term_memory is not None:
            def provenance(cursor,current):
                row=cursor.execute('''SELECT sender_principal_id,sender_actor_key,source_endpoint_key
                    FROM messages WHERE canonical_message_id=? AND native_message_id=? AND direction='inbound' ''',
                    (life.handle.canonical_message_id,current.native_message_id)).fetchone()
                if (row is None or row['sender_actor_key']!=current.actor.key
                        or row['source_endpoint_key']!=current.endpoint.key):
                    raise CommandEffectDenied('memory_source_evidence_changed')
                return row['sender_principal_id'] or 0,life.handle.canonical_message_id
            callbacks['memory']=MemoryCommands(store=app.long_term_memory,inbound=inbound,
                effect_scope=life.effect_scope,database_call=runtime._calls,provenance=provenance,
                group_write_authority=lambda cursor,current:runtime.store.authority.owner_for_actor(
                    cursor,current.endpoint,current.actor) is not None)
        migration=getattr(runtime,'migration_service',None)
        if migration is not None:
            async def project(current,parsed):
                if current is not inbound:raise CommandEffectDenied('project_command_source_changed')
                provider=await migration.project_context(inbound,platform,expected_execution_kind='command')
                if provider is None:return CommandResult('unavailable','项目服务尚未就绪。')
                platform.project_provider=provider
                context=ToolContext(SimpleNamespace(context=app),event=None,capabilities=frozenset({'projects'}),
                                    inbound=inbound,platform=platform)
                handler=ProjectCommands(inbound=inbound,provider=provider,context=context,
                    effect_scope=life.effect_scope,database_call=runtime._calls)
                return await handler(current,parsed)
            callbacks.update(project=project,project_grant=project)
        # NativeRoutineCommands and WikiCommands are injected by the same
        # account runtime after their domain/source workers are configured.
        native=getattr(runtime,'native_commands',None)
        if native is not None:
            callbacks.update(native.bind(life=life,inbound=inbound,platform=platform))
        return replace(services,**callbacks)
    return factory
