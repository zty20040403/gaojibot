"""Official host wiring: existing owner bindings, no migration gate."""
from contextlib import nullcontext
import time

from .migration_types import MigrationDenied


class BoundRoutes:
    def assert_active_route(self, cursor, *, canonical_scope, endpoint, route_epoch=0, operation='read'):
        scope = canonical_scope.key if hasattr(canonical_scope, 'key') else canonical_scope
        row = cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?', (endpoint.key,)).fetchone()
        if row is None or row['binding_state'] == 'revoked' or row['membership_state'] == 'absent':
            raise MigrationDenied('conversation_unavailable')
        if scope not in {endpoint.scope.key, row['canonical_scope_key']}:
            raise MigrationDenied('conversation_mismatch')
        return True


class SourceAdmission:
    def __init__(self, base): self.base = base

    def __call__(self, cursor, endpoint, actor):
        return self.base(cursor, endpoint, actor)

    def require_execution(self, cursor, execution_id, *, endpoint, actor, operation):
        row = cursor.execute('SELECT * FROM qq_official_turn_executions WHERE execution_id=? AND endpoint_key=? AND requester_actor_key=?',
            (execution_id, endpoint.key, actor.key)).fetchone()
        if row is None or not self(cursor, endpoint, actor):
            raise MigrationDenied('turn_source_unavailable')
        return {**row, 'logical_route_epoch': row['route_epoch']}

    def require_inbox(self, cursor, inbox_id, *, endpoint, actor, operation):
        row = cursor.execute('SELECT * FROM qq_official_inbound_events WHERE inbox_id=? AND endpoint_key=?', (inbox_id, endpoint.key)).fetchone()
        if row is None or not self(cursor, endpoint, actor):
            raise MigrationDenied('message_source_unavailable')
        return {**row, 'logical_route_epoch': row['route_epoch']}


class HostServices:
    native_only = True
    baselines = {}

    def __init__(self, *, database, transaction, database_calls, clock=time.time, **legacy):
        self.database, self.transaction, self.database_calls = database, transaction, database_calls
        self.clock = clock
        self.routes = BoundRoutes()
        self.official = {}
        self.file_admission = None

    async def start_native(self):
        pass

    def admin_service(self):
        return None

    def official_factory(self, runtime, token):
        session = OfficialHostSession(self, runtime, token)
        self.official[token.account_key] = session
        return session

    def job_context(self, *args, **kwargs):
        return nullcontext()

    async def close(self):
        for session in tuple(self.official.values()):
            await session.close()


class OfficialHostSession:
    def __init__(self, services, runtime, token):
        self.services, self.runtime, self.token = services, runtime, token
        self.admission = SourceAdmission(runtime.store.admitted)
        self._attached = False
        self._files = None
        self.domains = {}
        self.task_sources = None

    def attach(self):
        self.runtime.store.admitted = self.runtime.outbox.admission = self.admission
        self._attached = True

    def file_configuration(self, config):
        return config

    def attach_file_services(self, files):
        self._files = files

    def public_history(self, inbound, platform):
        # Both old and new messages use the bound canonical conversation.
        return None

    async def maintain(self):
        pass

    async def close(self):
        if self._attached:
            self.runtime.store.admitted = self.runtime.outbox.admission = self.admission.base
            self._attached = False
        self.services.official.pop(self.token.account_key, None)
