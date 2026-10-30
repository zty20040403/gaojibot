"""Bind public history snapshots to the actual reviewed migration journal.

The caller owns the transaction and current reader source. Captured text is
rechecked before reuse; it never supplies an identity, reply window or native
message resolver for the new endpoint.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import json
import time
import weakref

from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.platform_types import EndpointKey
from .lease import assert_epoch
from .migration_history import (
    HistoryUnavailable, PublicHistoryView, activate_public_history,
    prepare_public_history, read_public_history,
)
from .migration_journal import Plan
from .migration_types import MigrationDenied, digest, encoded


def _deny(*args, **kwargs):
    return False


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False)
class HistoryRead:
    view: PublicHistoryView = field(repr=False)
    endpoint: EndpointKey = field(repr=False)
    source: object = field(repr=False)
    fingerprint: str = field(repr=False)
    limit: int


class MigrationHistoryService:
    def __init__(self, journal, *, token, reader_guard=_deny, clock=time.time):
        self.journal, self.routes = journal, journal.routes
        self.token, self.reader_guard, self.clock = token, reader_guard, clock
        self._reads = weakref.WeakValueDictionary()

    def _guard(self, cursor, check):
        assert_epoch(cursor, self.token)
        journal, route = self.journal._load(cursor, check.migration_id)
        if (route['target_account_key'] != self.token.account_key
                or (route['route_id'], route['source_scope_key'], route['target_scope_key'],
                    route['target_endpoint_key'], route['target_generation'], route['target_endpoint_epoch']) !=
                (check.route_id, check.source_scope_key, check.target_scope_key, check.target_endpoint_key,
                 check.target_endpoint_generation, check.target_endpoint_route_epoch)):
            raise HistoryUnavailable('history_route_conflict')
        endpoint = cursor.execute('SELECT generation,route_epoch FROM qq_official_endpoints WHERE endpoint_key=?',
                                  (check.target_endpoint_key,)).fetchone()
        if endpoint is None or (endpoint['generation'],endpoint['route_epoch']) != (
                check.target_endpoint_generation,check.target_endpoint_route_epoch):
            raise HistoryUnavailable('history_endpoint_changed')
        if check.purpose == 'prepare':
            # Freeze and switch each advance once. A prepared grant records the
            # future completed epoch, without enabling reads in the meantime.
            if (route['phase'] != 'planned' or check.route_epoch != route['epoch']+2
                    or int(self.clock()) >= journal['deadline']):
                raise HistoryUnavailable('history_preview_not_planned')
            return
        approved = json.loads(journal['preview_json']).get('public_history')
        if (check.purpose not in {'activate', 'read'} or route['phase'] != 'completed'
                or route['epoch'] != check.route_epoch or not journal['approved_by']
                or not isinstance(approved, dict) or approved.get('manifest_digest') != check.manifest_digest):
            raise HistoryUnavailable('history_not_approved')
        grant = cursor.execute('''SELECT manifest_digest,state FROM qq_migration_public_history_grants
            WHERE grant_id=? AND migration_id=?''', (approved.get('grant_id'), check.migration_id)).fetchone()
        if (grant is None or grant['manifest_digest'] != check.manifest_digest
                or grant['state'] not in ({'prepared','active'} if check.purpose=='activate' else {'active'})):
            raise HistoryUnavailable('history_not_approved')

    def prepare(self, cursor, *, migration_id, authorization, chapter_ids=(), public_note_ids=(), now):
        journal, route = self.journal._load(cursor, migration_id)
        self.journal._control(cursor, authorization, 'history_preview', journal['preview_digest'], now)
        if route['phase'] != 'planned' or journal['deadline'] <= now:
            raise MigrationDenied('migration_phase_mismatch')
        result = prepare_public_history(cursor, guard=self._guard, migration_id=migration_id,
            route_id=route['route_id'], route_epoch=route['epoch']+2,
            source_scope=ConversationScope.from_key(route['source_scope_key']),
            target_endpoint=EndpointKey.parse(route['target_endpoint_key']),
            endpoint_generation=route['target_generation'], endpoint_route_epoch=route['target_endpoint_epoch'],
            chapter_ids=chapter_ids, public_note_ids=public_note_ids, now=now)
        previous = json.loads(journal['preview_json'])
        former = previous.get('public_history')
        if isinstance(former, dict):
            cursor.execute("UPDATE qq_migration_public_history_grants SET state='revoked' WHERE grant_id=? AND migration_id=?",
                (former.get('grant_id'), migration_id))
        preview = {**previous, 'public_history': asdict(result)}
        fingerprint, revision = digest(preview), journal['revision']+1
        cursor.execute('''UPDATE migration_journals SET preview_json=?,preview_digest=?,revision=?,
            approved_by=NULL,approved_version=NULL WHERE migration_id=?''',
            (encoded(preview), fingerprint, revision, migration_id))
        cursor.execute('INSERT INTO migration_preview_revisions VALUES (?,?,?,?,?)',
            (migration_id,revision,fingerprint,encoded(preview),now))
        self.journal._event(cursor,migration_id,'planned',route['epoch'],'public_history_preview_updated',now)
        return Plan(migration_id,route['route_id'],fingerprint,route['epoch']), result

    def activate(self, cursor, *, migration_id):
        journal, _ = self.journal._load(cursor,migration_id)
        approved = json.loads(journal['preview_json']).get('public_history')
        if approved is not None:
            activate_public_history(cursor,guard=self._guard,grant_id=approved['grant_id'],
                                    approved_manifest_digest=approved['manifest_digest'])

    def _read(self, cursor, *, endpoint, source, limit):
        if (not isinstance(endpoint,EndpointKey) or endpoint.account.key != self.token.account_key
                or self.reader_guard(cursor,endpoint=endpoint,source=source,now=int(self.clock())) is not True):
            raise HistoryUnavailable('history_reader_unavailable')
        self.routes.lock(cursor,endpoints=(endpoint,),scopes=(endpoint.scope.key,))
        route = self.routes._route(cursor,endpoint.scope.key)
        if route is None:
            return None
        if route['target_endpoint_key'] != endpoint.key:
            raise HistoryUnavailable('history_route_not_completed')
        if route['phase'] == 'planned':
            # Planning leaves normal target chat open. Its historical view is
            # simply absent until the reviewed migration has completed.
            return None
        if route['phase'] != 'completed':
            raise HistoryUnavailable('history_route_not_completed')
        journal = cursor.execute('SELECT * FROM migration_journals WHERE route_id=?', (route['route_id'],)).fetchone()
        approved = json.loads(journal['preview_json']).get('public_history')
        if approved is None:
            return None
        return read_public_history(cursor,guard=self._guard,grant_id=approved['grant_id'],
            target_endpoint=endpoint,route_epoch=route['epoch'],limit=limit)

    def capture(self, cursor, *, endpoint, source, limit=100):
        view = self._read(cursor,endpoint=endpoint,source=source,limit=limit)
        if view is None:
            return None
        try:
            fingerprint = digest(asdict(view))
        except (TypeError, ValueError, OverflowError):
            raise HistoryUnavailable('history_view_too_large') from None
        result = HistoryRead(view,endpoint,source,fingerprint,limit)
        self._reads[id(result)] = result
        return result

    def require_current(self, cursor, captured):
        if type(captured) is not HistoryRead or self._reads.get(id(captured)) is not captured:
            raise HistoryUnavailable('history_read_not_issued')
        view = self._read(cursor,endpoint=captured.endpoint,source=captured.source,limit=captured.limit)
        try:
            unchanged = view is not None and digest(asdict(view)) == captured.fingerprint
        except (TypeError, ValueError, OverflowError):
            unchanged = False
        if not unchanged:
            raise HistoryUnavailable('history_read_changed')

    def session(self, *, endpoint, source, transaction, limit=100):
        return PublicHistorySession(self,endpoint,source,transaction,limit)


class PublicHistorySession:
    """Per-turn read view. Every reuse rechecks authority and original visibility.

    These migration handles are deliberately not current native message IDs,
    principal handles, normal context handles, or permission to send/mention.
    """
    def __init__(self,service,endpoint,source,transaction,limit):
        if not callable(transaction) or type(limit) is not int or not 1<=limit<=100:
            raise HistoryUnavailable('history_reader_unavailable')
        self.service,self.endpoint,self.source,self.transaction,self.limit=service,endpoint,source,transaction,limit
        self._captured=None
        self._started=False

    def _capture(self):
        if not self._started:
            with self.transaction() as cursor:
                self._captured=self.service.capture(cursor,endpoint=self.endpoint,source=self.source,limit=self.limit)
            self._started=True
        return self._captured

    def require_current(self):
        captured=self._capture()
        if captured is not None:
            with self.transaction() as cursor:
                self.service.require_current(cursor,captured)
        return True

    @contextmanager
    def guard_scope(self):
        from src.plugins.ai_chat.agent.control import RouteFence,active_route_fence
        previous=active_route_fence.get()
        def check():
            if previous is not None:
                previous.assert_current()
            return self.require_current()
        token=active_route_fence.set(RouteFence(check))
        try:
            yield
        finally:
            active_route_fence.reset(token)

    def _records(self):
        self.require_current()
        captured=self._captured
        if captured is None:
            return []
        view=captured.view
        prefix='migration:'+view.grant_id+':'
        records=[]
        for message in view.messages:
            records.append(dict(handle=prefix+'message:'+str(message.canonical_message_id),kind='message',
                source_scope=view.source_scope_key,original_author=message.sender_display,
                original_user_id=message.sender_native_user_id,occurred_at=message.occurred_at,
                text=message.rendered_text))
        for chapter in view.chapters:
            records.append(dict(handle=prefix+'chapter:'+str(chapter.compartment_id),kind='chapter',
                source_scope=view.source_scope_key,original_authors=list(chapter.participants),
                text=chapter.summary_p2))
        for note in view.public_notes:
            records.append(dict(handle=prefix+'note:'+str(note.id),kind='public_note',
                source_scope=view.source_scope_key,original_author=(note.creator_actor_key or str(note.creator_user_id or '')),
                text=note.content))
        return records

    def prompt(self, *, max_chars=16000):
        if type(max_chars) is not int or not 256<=max_chars<=64000:
            raise HistoryUnavailable('history_prompt_budget')
        records=self._records()
        if not records:
            return ''
        from src.plugins.ai_chat.prompt_context import quote_evidence
        content=json.dumps({'direction':'旧群公共历史的单向只读视图；作者仍属原群，未映射为本群身份。',
            'records':records},ensure_ascii=False,separators=(',',':'))
        return quote_evidence(content,'migrated_public_history',stable=True,max_chars=max_chars)

    def search(self,query,*,limit=5):
        if not isinstance(query,str) or not 1<=len(query)<=2000 or type(limit) is not int or not 1<=limit<=10:
            raise HistoryUnavailable('invalid_history_request')
        folded=query.casefold()
        return [{**item,'text':item['text'][:500]} for item in self._records()
                if folded in item['text'].casefold()][:limit]

    def expand(self,handle,*,max_chars=4000):
        if not isinstance(handle,str) or len(handle)>128 or type(max_chars) is not int or not 1<=max_chars<=16000:
            raise HistoryUnavailable('invalid_history_request')
        for item in self._records():
            if item['handle']==handle:
                return {**item,'text':item['text'][:max_chars]}
        return None
