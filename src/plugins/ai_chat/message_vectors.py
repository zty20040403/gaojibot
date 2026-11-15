"""Derived message vectors: original history/IR is the authority, never the cache."""
from contextlib import contextmanager
import hashlib
import json
import time

from .adapters.qq_official.conversation_control import permitted,state
from .adapters.qq_official.keys import account_job_kind
from .adapters.qq_official.lease import assert_epoch
from .conversation_scope import ConversationScope
from .ledger import MessageLedger
from .message_ir import CardNode,TextNode
from .platform_types import ActorKey,EndpointKey
from .semantic_recall import SemanticDocument
from .timeline import is_noise

# Canonical IR plus derived captions: rendered_text is only a rebuildable cache.
REVISION = "encode(sha256(convert_to(m.body_json || chr(10) || COALESCE(m.media_captions::text,'{}'),'UTF8')),'hex')"


def embedding_identity(client):
    return hashlib.sha256((client.base_url+'\n'+client.model+'\n'+str(client.dimensions)).encode()).hexdigest()


class MessageVectorJobs:
    def __init__(self,database,jobs,token,backend,embedder,*,admission,clock=time.time):
        self.database,self.jobs,self.token,self.backend,self.embedder = database,jobs,token,backend,embedder
        self.admission,self.clock = admission,clock
        self.identity = embedding_identity(embedder)
        self.kind = account_job_kind('semantic',token.account_key)
        self.prefix = 'semantic.message.v1:'+self.identity
        self.page_before = {}
        if backend.schema!=database.schema:raise ValueError('semantic_database_schema_mismatch')
        if embedder.dimensions!=backend.dimensions or backend.dimensions!=1024:raise ValueError('semantic_dimensions_mismatch')

    @contextmanager
    def transaction(self):
        connection = self.database.store_connection()
        try:
            yield connection.cursor()
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:connection.close()

    def _route(self,cursor,endpoint):
        assert_epoch(cursor,self.token)
        if not isinstance(endpoint,EndpointKey) or endpoint.account.key!=self.token.account_key:
            raise PermissionError('semantic_endpoint_unavailable')
        row = cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=? FOR SHARE',
            (endpoint.key,)).fetchone()
        if (row is None or row['binding_state']=='revoked' or row['membership_state']!='present'
            or endpoint.kind=='group' and row['binding_state']!='active' or not permitted(cursor,endpoint)):
            raise PermissionError('semantic_endpoint_unavailable')
        if endpoint.kind=='private' and not self.admission(cursor,endpoint,ActorKey(endpoint.account,'c2c_user','',endpoint.native_conversation_id)):
            raise PermissionError('semantic_endpoint_unavailable')
        scope = ConversationScope.from_key(row['canonical_scope_key'])
        if scope.account!=endpoint.account:raise PermissionError('semantic_history_account_mismatch')
        return dict(row)

    def endpoints(self,*,after='',limit=64):
        with self.transaction() as cursor:
            assert_epoch(cursor,self.token)
            return [EndpointKey.parse(row['endpoint_key']) for row in cursor.execute(
                'SELECT endpoint_key FROM qq_official_endpoints WHERE account_key=? AND endpoint_key>? ORDER BY endpoint_key LIMIT ?',
                (self.token.account_key,after,min(max(int(limit),1),64))).fetchall()]

    def enqueue(self,endpoint,*,limit=16,before=None):
        with self.transaction() as cursor:
            route = self._route(cursor,endpoint)
            self.page_before.pop(endpoint.key,None)
            pending = cursor.execute("SELECT count(*) AS n FROM durable_jobs WHERE kind=? AND status IN ('pending','running')",
                (self.kind,)).fetchone()['n']
            bounded = min(max(int(limit),1),32,128-pending)
            if bounded<=0:return []
            page = cursor.execute('''SELECT m.canonical_message_id FROM messages m JOIN conversations c USING(conversation_id)
                JOIN conversation_visibility v USING(conversation_id) WHERE c.scope_key=?
                  AND m.canonical_message_id>=v.min_canonical_message_id AND m.message_kind='chat'
                  AND m.direction IN ('inbound','outbound') AND (CAST(? AS bigint) IS NULL OR m.canonical_message_id<?)
                ORDER BY m.canonical_message_id DESC LIMIT 128''',(route['canonical_scope_key'],before,before)).fetchall()
            ids = [row['canonical_message_id'] for row in page]
            self.page_before[endpoint.key] = min(ids) if ids else None
            if not ids:return []
            generation = state(cursor,endpoint)['stop_generation']
            admission = hashlib.sha256((endpoint.key+str((self.token.epoch,route['generation'],route['route_epoch'],generation))).encode()).hexdigest()[:16]
            prefix = self.prefix+':'+admission
            rows = cursor.execute(f'''SELECT m.canonical_message_id,{REVISION} AS revision
                FROM messages_with_captions m JOIN conversations c USING(conversation_id)
                JOIN conversation_visibility v USING(conversation_id)
                WHERE c.scope_key=? AND m.canonical_message_id>=v.min_canonical_message_id
                  AND m.canonical_message_id=ANY(?) AND m.message_kind='chat' AND m.direction IN ('inbound','outbound')
                  AND NOT EXISTS (SELECT 1 FROM semantic_documents d WHERE d.scope_key=c.scope_key
                    AND d.source_type='message' AND d.source_handle='msg#' || m.canonical_message_id::text
                    AND d.embedding_model=? AND d.metadata_json->>'embedding_identity'=?
                    AND d.metadata_json->>'source_revision'={REVISION})
                  AND NOT EXISTS (SELECT 1 FROM durable_jobs j WHERE
                    j.idempotency_key=? || ':' || m.canonical_message_id::text || ':' || {REVISION})
                ORDER BY m.canonical_message_id DESC LIMIT ?''',
                (route['canonical_scope_key'],ids,self.embedder.model,self.identity,prefix,bounded)).fetchall()
            if len(rows)==bounded:self.page_before[endpoint.key] = before
            created = []
            for row in rows:
                payload = {'endpoint':endpoint.key,'scope':route['canonical_scope_key'],'account_epoch':self.token.epoch,
                    'endpoint_generation':route['generation'],'route_epoch':route['route_epoch'],'stop_generation':generation,
                    'message_id':row['canonical_message_id'],'revision':row['revision'],'embedding_identity':self.identity}
                job,fresh = self.jobs.enqueue(kind=self.kind,scope_key=route['canonical_scope_key'],payload=payload,
                    idempotency_key=prefix+':'+str(row['canonical_message_id'])+':'+row['revision'],
                    max_attempts=1,resume_on_lease_loss=False,transaction_cursor=cursor,now=int(self.clock()))
                if fresh:created.append(job)
            return created

    def _current(self,cursor,job):
        source = job.payload
        endpoint = EndpointKey.parse(source['endpoint'])
        route = self._route(cursor,endpoint)
        active = cursor.execute('SELECT * FROM durable_jobs WHERE job_id=? FOR UPDATE',(job.job_id,)).fetchone()
        if (job.kind!=self.kind or active is None or active['kind']!=job.kind or json.loads(active['payload_json'])!=source
            or active['scope_key']!=job.scope_key or active['status']!='running' or active['lease_until']<=int(self.clock())
            or (active['lease_owner'],active['lease_generation'],active['attempts'])!=(job.lease_owner,job.lease_generation,job.attempts)
            or source['account_epoch']!=self.token.epoch or source['embedding_identity']!=embedding_identity(self.embedder)
            or source['embedding_identity']!=self.identity or source['scope']!=route['canonical_scope_key']
            or (source['endpoint_generation'],source['route_epoch'])!=(route['generation'],route['route_epoch'])
            or not permitted(cursor,endpoint,generation=source['stop_generation'])):
            raise PermissionError('semantic_job_source_changed')
        row = cursor.execute(f'''SELECT m.*,c.scope_key,{REVISION} AS source_revision
            FROM messages_with_captions m JOIN conversations c USING(conversation_id)
            JOIN conversation_visibility v USING(conversation_id)
            WHERE m.canonical_message_id=? AND c.scope_key=? AND m.canonical_message_id>=v.min_canonical_message_id
              AND m.message_kind='chat' AND m.direction IN ('inbound','outbound') FOR SHARE OF m,c,v''',
            (source['message_id'],source['scope'])).fetchone()
        if row is None or row['source_revision']!=source['revision']:raise PermissionError('semantic_message_changed_or_hidden')
        message = MessageLedger._row_to_message(row)
        meaningful = any(isinstance(node,TextNode) and node.text.strip() or isinstance(node,CardNode) and (node.title or node.url)
            for node in message.body.nodes) or bool(row['media_captions'])
        content = ' '.join(message.prompt_text.split())[:12000] if meaningful and not is_noise(message) else ''
        document = SemanticDocument(source['scope'],'message','msg#'+str(message.canonical_message_id),content,
            {'source_revision':source['revision'],'embedding_identity':self.identity})
        return endpoint,document

    def current(self,job):
        with self.transaction() as cursor:return self._current(cursor,job)

    def save(self,job,expected,vector):
        with self.transaction() as cursor:
            _endpoint,document = self._current(cursor,job)
            if document!=expected:raise PermissionError('semantic_projection_changed')
            if document.content:
                self.backend.upsert(document,vector,model=self.embedder.model,transaction_cursor=cursor)
            else:
                cursor.execute("DELETE FROM semantic_documents WHERE scope_key=? AND source_type='message' AND source_handle=?",
                    (document.scope_key,document.source_handle))
