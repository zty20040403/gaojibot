"""Project scope links follow owner binding; actual project grants stay separate."""
from dataclasses import dataclass
import hashlib

from .project_authority import Denied


@dataclass(frozen=True)
class ResourceOrigin:
    origin_id: str
    project_id: int
    project_scope_key: str


class ProjectResourceOrigins:
    def __init__(self, *, routes=None):
        self.routes = routes

    def _origin(self, cursor, request):
        project = cursor.execute('SELECT scope_key,status FROM projects WHERE project_id=?', (request.project_id,)).fetchone()
        endpoint = cursor.execute('SELECT canonical_scope_key,binding_state FROM qq_official_endpoints WHERE endpoint_key=?',
            (request.endpoint.key,)).fetchone()
        if (project is None or project['status'] == 'purged' or endpoint is None
                or endpoint['binding_state'] != 'active'
                or project['scope_key'] != request.canonical_scope.key
                or project['scope_key'] not in {endpoint['canonical_scope_key'], request.endpoint.scope.key}):
            raise Denied()
        key = hashlib.sha256(f'{request.endpoint.key}:{request.project_id}'.encode()).hexdigest()
        return ResourceOrigin(key, request.project_id, project['scope_key'])

    def capture(self, cursor, *, request, authorize, now):
        if not authorize(cursor):
            raise Denied()
        return self._origin(cursor, request)

    def check_origin(self, cursor, *, request, origin_id):
        origin = self._origin(cursor, request)
        if origin.origin_id != origin_id:
            raise Denied()
        return origin

    def bind_grant(self, cursor, *, request, origin_id, grant, now):
        self.check_origin(cursor, request=request, origin_id=origin_id)

    def project_scope_guard(self, cursor, *, request, project, source):
        self._origin(cursor, request)
        return project['project_id'] == request.project_id
