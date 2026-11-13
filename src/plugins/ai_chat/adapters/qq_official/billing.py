"""Accounting sources are constructed by authenticated host entry points only."""
from contextlib import nullcontext

from ...billing_context import BillingContext,billing_scope
from ...model_billing import BillingSource


def scoped(app,endpoint,purpose,actor=None,*,current=None,**links):
    store = getattr(app,'model_billing',None)
    if store is None:return nullcontext()
    return billing_scope(BillingContext(store,BillingSource(endpoint,purpose,actor,**links),app.db_call,current))


def foreground(runtime,app):
    def scope(inbound,platform,turn_id):
        def check():
            if platform.inbound!=inbound:raise PermissionError('billing_turn_source_changed')
            with runtime.store.transaction() as cursor:
                row = platform.execution.current(cursor,int(runtime.store.clock()))
                if row['state']!='executing' or row['journal_turn_id']!=turn_id:
                    raise PermissionError('billing_turn_source_changed')
        return scoped(app,inbound.endpoint,'turn',inbound.actor,turn_id=turn_id,
            current=lambda:runtime._calls(check))
    return scope
