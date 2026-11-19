"""One durable, bounded attention glance per due physical group.

Observation rows are at-most-once decision markers, not dispatch jobs. An
interrupted classifier is never replayed, including after a new account epoch.
"""
from dataclasses import dataclass
from contextlib import nullcontext
import asyncio
import json
import logging
import math
import weakref

import psycopg
from src.bot_storage import DatabaseError

from .conversation_control import state as control_state
from .media_refs import MediaDenied
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch,LeaseLost as AccountLeaseLost
from src.plugins.ai_chat.adapters.qq_official.identity_store import snapshot
from src.plugins.ai_chat.adapters.qq_official.migration_types import MigrationDenied
from src.plugins.ai_chat.execution_control import JobFence,active_job_fence,LeaseLost
from src.plugins.ai_chat.platform_types import EndpointKey,ActorKey,AccountKey
from src.plugins.ai_chat.proactive import is_candidate_message
from src.plugins.ai_chat.conversation_config import proactive_state,proactive_attention,in_quiet_utc
from src.plugins.ai_chat import daily_budgets
from src.plugins.ai_chat.adapters.qq_official import peer_bots
from src.plugins.ai_chat.adapters.qq_official.proactive_delivery import ProactiveLifetime,score_and_deliver

logger = logging.getLogger(__name__)

MAX_CLASSIFICATIONS_PER_HOUR = 60
ATTENTION_BATCH = 20
HOT_LOOK_SECONDS = 3
QUIET_LOOK_SECONDS = 600


def record_group_activity(cursor, endpoint, canonical, now, *, direct):
    """The ledger transaction already holds this physical endpoint's row lock."""
    if endpoint.kind != 'group':return
    if direct:
        # A late @ can promote an older message while newer work is pending.
        cursor.execute('''UPDATE qq_official_endpoints SET
            interest_after_lull=0,
            attention_cursor=GREATEST(attention_cursor,?),
            attention_end=GREATEST(attention_end,?),
            attention_next_at=CASE WHEN attention_end>GREATEST(attention_cursor,?)
                THEN attention_next_at ELSE NULL END,
            attention_activity_at=GREATEST(COALESCE(attention_activity_at,0),?),
            attention_suppressed_through=GREATEST(attention_suppressed_through,?)
            WHERE endpoint_key=?''',(canonical,canonical,canonical,now,canonical,endpoint.key))
        return
    row=cursor.execute('''SELECT attention_cursor,attention_end,attention_next_at,attention_activity_at
        FROM qq_official_endpoints WHERE endpoint_key=?''',(endpoint.key,)).fetchone()
    gap=None if row['attention_activity_at'] is None else now-row['attention_activity_at']
    # The fresh source expires in 30s; a long idle hint cannot delay it beyond that.
    interval=HOT_LOOK_SECONDS if gap is None or gap<=15 or gap>=QUIET_LOOK_SECONDS else min(20,gap)
    due=now+interval
    hint=row['attention_next_at']
    if hint is not None and (row['attention_end']>row['attention_cursor'] or hint>now):
        due=min(hint,due)
    cursor.execute('''UPDATE qq_official_endpoints SET attention_end=?,attention_next_at=?,attention_activity_at=?,interest_after_lull=?
        WHERE endpoint_key=?''',(canonical,due,now,canonical if gap is not None and gap>=QUIET_LOOK_SECONDS else 0,endpoint.key))


def wake_available(cursor, endpoint, now):
    limit, quiet = proactive_attention(cursor, endpoint)
    if not limit or in_quiet_utc(quiet, now):
        return False
    # The observation timestamp precedes its wake by at most 30 seconds.
    queued = cursor.execute("""SELECT count(*) FROM qq_official_proactive_observations
        WHERE endpoint_key=? AND created_at>? AND state='queued'""", (endpoint.key, now-3630)).fetchone()[0]
    return queued < limit


@dataclass(frozen=True,repr=False,eq=False)
class ProactiveCandidate:
    inbox_id: str
    batch: tuple
    first_canonical: int


class ProactiveIngress:
    def __init__(self,*,token,transaction,admission,outbox,scorer,clock,database_calls,billing=None,wake_turn=None,media=None):
        self.token,self.tx,self.admission,self.outbox=token,transaction,admission,outbox
        self.scorer,self.clock,self.calls=scorer,clock,database_calls
        self.billing=billing
        self.wake_turn=wake_turn
        self.media=media
        self.sharing=None
        self._issued=weakref.WeakKeyDictionary()

    def _now(self):
        value=self.clock()
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:raise MigrationDenied('invalid_proactive_clock')
        return int(value)

    def _load(self,c,inbox_id,*,operation='execute'):
        assert_epoch(c,self.token)
        inbox=c.execute('SELECT * FROM qq_official_inbound_events WHERE inbox_id=? AND account_key=?',(inbox_id,self.token.account_key)).fetchone()
        if inbox is None:raise MigrationDenied('proactive_source_unavailable')
        ep=EndpointKey.parse(inbox['endpoint_key'])
        message=c.execute('SELECT m.*,co.scope_key FROM messages m JOIN conversations co USING(conversation_id) WHERE canonical_message_id=? AND m.canonical_message_id>=co.min_canonical_message_id',
            (inbox['canonical_message_id'],)).fetchone()
        if message is None:raise MigrationDenied('proactive_source_unavailable')
        from .conversation_control import permitted
        if not permitted(c,ep,canonical=message['canonical_message_id']):
            raise MigrationDenied('conversation_stopped_or_replaced')
        try:daily_budgets.require(c,ep,now=self._now())
        except PermissionError as error:raise MigrationDenied(str(error)) from None
        enabled,generation = proactive_state(c,ep)
        if not enabled or inbox['proactive_generation']!=generation:
            raise MigrationDenied('conversation_proactive_disabled_or_replaced')
        actor=ActorKey.parse(message['sender_actor_key'])
        if getattr(self.admission,'require_inbox',None) is not None:
            self.admission.require_inbox(c,inbox_id,endpoint=ep,actor=actor,operation=operation)
        physical=c.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=? FOR UPDATE',(ep.key,)).fetchone()
        if (ep.kind!='group' or not actor.matches_endpoint(ep) or actor.identity_kind=='bot' or inbox['status']!='processed'
                or inbox['echo_state']!='not_echo' or inbox['trigger_requested'] or inbox['dispatch_job_id'] is not None
                or inbox['turn_text_version']!=1 or not is_candidate_message(inbox['turn_text'])
                or message['source_endpoint_key']!=ep.key or message['native_message_id']!=inbox['native_message_id']
                or message['direction']!='inbound' or message['reply_to_native_message_id']
                or physical['membership_state']!='present' or physical['binding_state']!='active'
                or physical['generation']!=inbox['endpoint_generation'] or physical['route_epoch']!=inbox['route_epoch']
                or physical['attention_suppressed_through']>=message['canonical_message_id']
                or self.admission(c,ep,actor) is not True
                or (peer_bots.is_bot_message(message) and peer_bots.exchange_exhausted(c,ep.key,inbox['received_at']))
                or not c.execute('SELECT 1 FROM qq_official_gateway_receipts WHERE inbox_id=? AND account_key=?',(inbox_id,self.token.account_key)).fetchone()):
            raise MigrationDenied('proactive_source_unavailable')
        return inbox,ep,actor,message,physical

    def claim_due(self):
        """Consume exactly one due range and mark its decision in one transaction.

        Claimed-but-interrupted scoring is intentionally not recovered. A new
        message gets a new range; it does not revive an unknown decision.
        """
        with self.tx() as c:
            assert_epoch(c,self.token)
            if snapshot(c,AccountKey.parse(self.token.account_key)) is None:
                return None
            now=self._now()
            physical=c.execute('''SELECT * FROM qq_official_endpoints
                WHERE account_key=? AND kind='group' AND attention_end>attention_cursor
                    AND attention_next_at<=?
                ORDER BY attention_next_at,endpoint_key LIMIT 1 FOR UPDATE SKIP LOCKED''',
                (self.token.account_key,now)).fetchone()
            if physical is None:return None
            start,end=physical['attention_cursor'],physical['attention_end']
            rows=c.execute('''SELECT i.inbox_id,i.canonical_message_id,i.received_at,i.turn_text,
                    i.trigger_requested,m.sender_display,m.reply_to_native_message_id
                FROM qq_official_inbound_events i JOIN messages m USING(canonical_message_id)
                JOIN conversations c USING(conversation_id)
                WHERE i.account_key=? AND i.endpoint_key=? AND i.status='processed'
                    AND i.echo_state='not_echo' AND i.canonical_message_id>?
                    AND i.canonical_message_id<=? AND i.canonical_message_id>=c.min_canonical_message_id
                ORDER BY i.canonical_message_id DESC LIMIT ?''',
                (self.token.account_key,physical['endpoint_key'],start,end,ATTENTION_BATCH)).fetchall()
            batch=tuple(reversed(rows))
            previous=c.execute('''SELECT created_at FROM qq_official_proactive_observations
                WHERE endpoint_key=? ORDER BY created_at DESC LIMIT 1''',
                (physical['endpoint_key'],)).fetchone()
            interval=(min(QUIET_LOOK_SECONDS,max(10,(now-previous['created_at'])*2))
                      if previous is not None else 10)
            c.execute('''UPDATE qq_official_endpoints SET attention_cursor=?,attention_next_at=?
                WHERE endpoint_key=?''',(end,now+interval,physical['endpoint_key']))
            if not batch or now>=batch[-1]['received_at']+30:return None
            chosen=next((item for item in reversed(batch)
                if item['received_at']<=now<item['received_at']+30 and not item['trigger_requested']
                    and not item['reply_to_native_message_id'] and is_candidate_message(item['turn_text'])),None)
            if chosen is None:return None
            try:
                inbox,ep,actor,_,_=self._load(c,chosen['inbox_id'],operation='execute')
            except MigrationDenied:
                return None  # Expected admission/config/route revocation; cursor commits.
            if not wake_available(c,ep,now):return None
            classified=c.execute('''SELECT count(*) FROM qq_official_proactive_observations
                WHERE endpoint_key=? AND created_at>?''',(ep.key,now-3600)).fetchone()[0]
            if classified>=MAX_CLASSIFICATIONS_PER_HOUR:return None
            day,interest=self.sharing.reserve(c,inbox,ep,now) if self.sharing is not None else (None,None)
            inserted=c.execute('''INSERT INTO qq_official_proactive_observations
                (inbox_id,account_key,endpoint_key,actor_key,canonical_message_id,source_native_message_id,source_received_at,
                 deadline_at,ingest_job_id,worker_owner,worker_generation,account_epoch,endpoint_generation,endpoint_route_epoch,state,created_at,proactive_generation,interest_day,interest_candidate_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'scoring',?,?,?,?) ON CONFLICT DO NOTHING''',
                (inbox['inbox_id'],ep.account.key,ep.key,actor.key,inbox['canonical_message_id'],inbox['native_message_id'],inbox['received_at'],
                 inbox['received_at']+30,None,self.token.holder_id,self.token.epoch,self.token.epoch,
                 inbox['endpoint_generation'],inbox['route_epoch'],now,proactive_state(c,ep)[1],day,interest))
            if inserted.rowcount!=1:return None
            source=ProactiveCandidate(inbox['inbox_id'],batch,batch[0]['canonical_message_id'])
            self._issued[source]=True
            return source

    def _candidate(self,c,source):
        if type(source) is not ProactiveCandidate or source not in self._issued:raise MigrationDenied('unissued_proactive_source')
        loaded=self._load(c,source.inbox_id,operation='execute')
        row=c.execute('SELECT * FROM qq_official_proactive_observations WHERE inbox_id=?',(source.inbox_id,)).fetchone()
        if (row is None or row['state']!='scoring' or row['worker_owner']!=self.token.holder_id
                or row['worker_generation']!=self.token.epoch or row['account_epoch']!=self.token.epoch
                or row['proactive_generation']!=proactive_state(c,loaded[1])[1]):
            raise MigrationDenied('proactive_source_lost')
        return row,loaded

    def _current(self,source):
        with self.tx() as c:
            row,_=self._candidate(c,source)
            return ProactiveLifetime(row['source_received_at'],row['deadline_at'])

    async def current(self,source):return await self.calls(self._current,source)

    def _text(self,source):
        with self.tx() as c:
            _,(inbox,ep,actor,message,_)=self._candidate(c,source)
            recent=c.execute('''SELECT m.body_json,m.sender_display,m.direction FROM messages m
                JOIN conversations c USING(conversation_id)
                WHERE m.source_endpoint_key=? AND m.canonical_message_id>=c.min_canonical_message_id
                    AND m.canonical_message_id<? ORDER BY m.canonical_message_id DESC LIMIT ?''',
                (ep.key,source.first_canonical,ATTENTION_BATCH)).fetchall()
            text='\n'.join(f"[{r['sender_display'] or '群友'}]: {r['turn_text']}" for r in source.batch)
            return (text,
                tuple(dict(body_json=r['body_json'],sender_display=r['sender_display'],direction=r['direction'])
                      for r in reversed(recent)),ep,actor)

    async def score(self,source):
        from .vision_adapter import guarded_wait
        text,history,endpoint,actor=await self.calls(self._text,source)
        async def invoke():
            await self.current(source)
            with self.billing(endpoint,actor,lambda:self.current(source)) if self.billing else nullcontext():
                return await self.scorer(text=text,history=history,endpoint=endpoint,actor=actor)
        return await guarded_wait(invoke(),lambda:self.current(source),timeout=30)

    def _send_policy(self,source,*,purpose,deadline):
        with self.tx() as c:
            row,(inbox,ep,_,_,physical)=self._candidate(c,source)
            if deadline!=row['deadline_at'] or purpose!='unsolicited':raise MigrationDenied('proactive_deadline_changed')
            return physical['membership_state']=='present' and physical['binding_state']=='active'

    async def send_policy(self,*args,**kwargs):return await self.calls(self._send_policy,*args,**kwargs)

    def _mark_woken(self, source, enqueue=None):
        with self.tx() as c:
            row, (inbox, ep, _, message, _) = self._candidate(c, source)
            now=self._now()
            if not now < row['deadline_at']:
                raise MigrationDenied('proactive_source_expired')
            # The endpoint is locked again after the classifier's network call.
            if not wake_available(c,ep,now):
                raise MigrationDenied('proactive_attention_unavailable')
            conv_gen = control_state(c, ep)['stop_generation']
            plan_id = None
            if enqueue is not None:
                plan_id = c.execute('SELECT plan_id FROM deliveries WHERE delivery_id=?',
                                    (enqueue(c),)).fetchone()['plan_id']
            c.execute("UPDATE qq_official_proactive_observations SET state='queued',plan_id=? WHERE inbox_id=?",
                      (plan_id, source.inbox_id))
            return inbox['inbox_id'], ep.key, conv_gen, message['canonical_message_id']

    def _sticker_source(self, source):
        with self.tx() as c:
            row,(inbox,ep,actor,message,_)=self._candidate(c,source)
            return dict(endpoint=ep, requester=actor, source_scope_key=message['scope_key'],
                        source_native_message_id=inbox['native_message_id'], source_canonical_message_id=message['canonical_message_id'],
                        expires_at=row['deadline_at'], source_generation=control_state(c,ep)['stop_generation'])

    async def _wake_sticker(self,source,query):
        if self.media is None:
            return False
        route = await self.calls(self._sticker_source, source)
        try:
            content, filename, _ = await self.media.unsolicited_sticker(query, route['endpoint'], route['requester'])
        except MediaDenied:
            return False
        enqueue = await self.media.runtime.file_service.send_bytes(**route, content=content, filename=filename,
            logical_key='official-media:proactive:' + source.inbox_id, current=lambda:self.current(source),
            defer=True, purpose='unsolicited', source_proof=source)
        await self.calls(self._mark_woken, source, enqueue)
        self.media.runtime.sender.wake()
        return True

    async def wake(self, source, decision):
        if decision.action=='sticker':
            return await self._wake_sticker(source,decision.reason)
        if self.wake_turn is None:return False
        inbox_id, ep_key, conv_gen, canonical_id = await self.calls(self._mark_woken, source)
        await self.wake_turn(
            inbox_id=inbox_id,
            endpoint=ep_key,
            conversation_generation=conv_gen,
            canonical_message_id=canonical_id,
            reason=decision.reason,
        )
        return True

    def _discard(self,source):
        with self.tx() as c:
            assert_epoch(c,self.token)
            c.execute("UPDATE qq_official_proactive_observations SET state='discarded' WHERE inbox_id=? AND account_epoch=? AND state='scoring'",
                (source.inbox_id,self.token.epoch))

    async def glance(self):
        # claim_due is the only consumer of pending work, including after a
        # restart. DB/lease failures must reach the account supervisor.
        source=await self.calls(self.claim_due)
        if source is None:return None
        def check():
            try:return self._now()<self._current(source).deadline_at
            except MigrationDenied:return False
        marker=active_job_fence.set(JobFence(None,self.token.holder_id,1,check,self.token.epoch))
        try:
            lifetime=await self.current(source)
            if self.sharing is not None and await self.calls(self.sharing.share,source):
                self.sharing.wake()
                return None
            async with asyncio.timeout(max(0,lifetime.deadline_at-self._now())):
                result=await score_and_deliver(source=source,scorer=self.score,current=self.current,
                    send_policy=self.send_policy,wake=self.wake,now=self._now)
            if result.status=='discarded':await self.calls(self._discard,source)
            return result
        except (AccountLeaseLost,DatabaseError,psycopg.Error):
            raise
        except asyncio.CancelledError:
            await self.calls(self._discard,source)
            raise
        except (MigrationDenied,LeaseLost):
            await self.calls(self._discard,source)
            return None
        except Exception as error:
            logger.warning('QQ attention glance failed: %s',type(error).__name__)
            await self.calls(self._discard,source)
            return None
        finally:active_job_fence.reset(marker)

    def plan_guard(self,c,*,token,plan,operation,now,source_proof=None,**_):
        if token is not self.token:raise MigrationDenied('proactive_account_changed')
        if operation=='enqueue':
            row,(inbox,ep,actor,message,_)=self._candidate(c,source_proof)
        else:
            row=c.execute('SELECT * FROM qq_official_proactive_observations WHERE plan_id=?',(plan['plan_id'],)).fetchone()
            if row is None or row['state']!='queued':raise MigrationDenied('proactive_plan_source_missing')
            inbox,ep,actor,message,_=self._load(c,row['inbox_id'],operation='send')
        if (row['proactive_generation']!=proactive_state(c,ep)[1]
                or plan['purpose']!='unsolicited' or plan['account_key']!=ep.account.key or plan['endpoint_key']!=ep.key
                or plan['requester_actor_key']!=actor.key or plan['source_canonical_message_id']!=message['canonical_message_id']
                or plan['source_scope_key']!=message['scope_key'] or plan['endpoint_generation']!=inbox['endpoint_generation']
                or plan['route_epoch']!=inbox['route_epoch'] or not self._now()<plan['expires_at']<=row['deadline_at']
                or json.loads(plan['manifest_json']).get('source_native_message_id')!=row['source_native_message_id']):
            raise MigrationDenied('proactive_plan_source_changed')
        if row['interest_day'] is not None:
            if self.sharing is None:raise MigrationDenied('interest_sharing_unavailable')
            self.sharing.guard(c,row,loaded=(inbox,ep,actor,message),plan=plan)
        return True
