"""Trusted host composition of the existing official approval/host adapters.

No command, JSON payload or task objective selects an authority. A configured
service catalog only exposes the bounded tools; each effect still needs the
original native source, current account/job and independent-admin quorum.
"""
from __future__ import annotations

from copy import copy
from types import SimpleNamespace

from src.bot_security.service import MobileAuthorization
from src.bot_security.store import SecurityError, SecurityStore
from .approval_runtime import OfficialApprovalService
from .approval_transport import ApprovalOtpSender, OfficialApprovalTransport
from .background_host import NativeTaskSourcePort
from .host_authority import AllowedService, OfficialHostAuthority
from .host_external import OfficialExternalOperations
from .host_session import HostTaskSessionFactory


class _NativeRoute:
    def __init__(self,session):self.session=session

    def epoch(self,cursor,task):
        row=cursor.execute('''SELECT n.logical_route_epoch,d.source_execution_id FROM qq_official_subagent_dispatches d
            JOIN qq_official_turn_executions e ON e.execution_id=d.source_execution_id
            JOIN qq_migration_native_sources n ON n.inbox_id=e.inbox_id
            WHERE d.task_id=? AND d.account_key=? AND d.endpoint_key=? AND d.requester_actor_key=?''',
            (task.task_id,self.session.token.account_key,task.endpoint.key,task.requester_actor.key)).fetchone()
        if row is None:raise SecurityError('official_host_source_unavailable')
        self.session.admission.require_execution(cursor,row['source_execution_id'],endpoint=task.endpoint,
            actor=task.requester_actor,operation='execute')
        return row['logical_route_epoch']

    def require_current(self,cursor,*,source,operation,now):
        if operation!='execute' or source.task_id is None or not self.session._attached:
            raise SecurityError('official_host_source_unavailable')
        task=SimpleNamespace(task_id=source.task_id,endpoint=source.endpoint,requester_actor=source.actor)
        if self.epoch(cursor,task)!=source.route_epoch:
            raise SecurityError('official_host_route_changed')


def approval_services_factory(*,security=None,services=(),fleet_client=None):
    """Return OfficialRuntime.approval_factory; None is zero-work disabled.

    security is the existing trusted console SecurityStore. The new store is a
    transport-scoped view of that SAME database/secret, never a new approval
    queue. A shallow Fleet client view keeps account-local authorization while
    sharing the host-owned HTTP pool; closing this service never closes it.
    """
    allowed=tuple(services)
    if any(type(item) is not AllowedService for item in allowed):
        raise SecurityError('official_host_configuration_invalid')
    if security is None:
        if allowed or fleet_client is not None:raise SecurityError('official_security_unavailable')
        return lambda runtime,token:None
    if not isinstance(security,SecurityStore):raise SecurityError('official_security_unavailable')

    def create(runtime,token):
        from src.plugins.ai_chat.fleet_client import FleetControlClient
        from src.plugins.ai_chat.fleet_authorization import FleetAuthorization
        from src.plugins.ai_chat.server_task_authorization import ServerTaskAuthorization
        session=getattr(runtime,'migration_service',None)
        if (runtime.database is not security._database or runtime.sender is None
                or session is None or session.token is not token):
            raise SecurityError('official_approval_source_unavailable')
        route=_NativeRoute(session)
        source=NativeTaskSourcePort(token=token,admission=runtime.store.admitted,route=route)
        transport=OfficialApprovalTransport(token=token,secret=security.secret,
            source_port=source,clock=runtime.store.clock)
        store=SecurityStore(runtime.database,security.secret,transport=transport,clock=runtime.store.clock)
        try:
            otp=ApprovalOtpSender(sender=runtime.sender,transport=transport,store=store)
            async def no_legacy(*args,**kwargs):raise SecurityError('official_legacy_transport_denied')
            mobile=MobileAuthorization(store,sender=no_legacy,bot_selector=lambda:None,official_sender=otp)
            host_admission=host_session=None
            if allowed:
                app=runtime.application
                if (not isinstance(fleet_client,FleetControlClient) or app is None
                        or getattr(app,'subagent_store',None) is None):
                    raise SecurityError('official_host_configuration_unavailable')
                host=OfficialHostAuthority(transport=transport,mobile=mobile,services=allowed,clock=runtime.store.clock)
                ServerTaskAuthorization(SimpleNamespace(mobile_authorization=mobile),official_authority=host)
                client=copy(fleet_client)
                client.authorization=FleetAuthorization(client,mobile,official_authority=host)
                bridge=OfficialExternalOperations(store=app.subagent_store,authority=host,
                    source_port=source,transaction_factory=runtime.deliveries.transaction)
                host_session=HostTaskSessionFactory(bridge=bridge,client=client,app=app,route_epoch=route.epoch)
                def admitted(cursor,handle):
                    session.admission.require_execution(cursor,handle.execution_id,endpoint=handle.endpoint,
                        actor=handle.actor,operation='execute')
                    from .project_authority import Denied
                    try:transport.admins.resolve_admin(cursor,endpoint=handle.endpoint,actor=handle.actor)
                    except Denied:return False
                    return True
                host_admission=admitted
            return OfficialApprovalService(token=token,mobile=mobile,
                host_admission=host_admission,host_session_factory=host_session)
        except BaseException:
            store.close()
            raise
    return create
