"""Host assembly for native official business commands; no direct transport."""
from dataclasses import replace

from .commands import CommandEffectDenied
from .memory_commands import MemoryCommands


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
        return replace(services,**callbacks)
    return factory
