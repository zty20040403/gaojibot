"""Host async source/capacity adapter for the neutral owned sandbox executor."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import secrets
import threading
import weakref

from src.plugins.ai_chat.agent.control import LeaseLost
from .file_artifacts import current_source

_issued=weakref.WeakValueDictionary()
_lock=threading.Lock()


@dataclass(frozen=True,slots=True,weakref_slot=True,eq=False,repr=False)
class CapacityReservation:
    reservation_id: str
    source_key: str
    owner: str
    def __repr__(self): return 'CapacityReservation(<protected>)'


class GenerationBindingAdapter:
    def __init__(self,source,store,calls,*,account_limit=16,actor_limit=4):
        if (type(account_limit) is not int or not 1<=account_limit<=100
                or type(actor_limit) is not int or not 1<=actor_limit<=account_limit):
            raise ValueError('invalid_generation_capacity')
        self.source,self.store,self.calls=source,store,calls
        self.account_limit,self.actor_limit=account_limit,actor_limit
        self.endpoint,self.actor=source.endpoint,source.actor
        self.source_key,self.deadline_at=source.source_key,source.deadline_at
        self.source_native_message_id=source.source_native_message_id
        identity=json.dumps([self.endpoint.account.key,self.endpoint.key,self.actor.key],ensure_ascii=False,separators=(',',':'))
        self.owner='qqo:v1:'+hashlib.sha256(identity.encode()).hexdigest()+':x:'+hashlib.sha256(self.source_key.encode()).hexdigest()[:32]

    async def assert_current(self):
        def check():
            with self.store.transaction() as cursor:
                current_source(cursor,self.source,self.store._now())
        await self.calls(check)

    async def reserve_capacity(self,owner):
        if owner not in {self.owner,self.owner+':verify'}:
            raise LeaseLost('generation_owner_mismatch')
        def reserve():
            with self.store.transaction() as cursor:
                now=self.store._now()
                current_source(cursor,self.source,now)
                # The account row lock already held by current_source makes
                # the aggregate reservation check+insert atomic across owners.
                usage=cursor.execute('''SELECT count(*) AS total,
                    count(*) FILTER (WHERE endpoint_key=? AND requester_actor_key=?) AS actor
                    FROM qq_official_generation_capacity WHERE account_key=? AND state='reserved' ''',
                    (self.endpoint.key,self.actor.key,self.endpoint.account.key)).fetchone()
                if usage['total']>=self.account_limit or usage['actor']>=self.actor_limit:
                    raise ValueError('generation_capacity_exhausted')
                reservation_id=secrets.token_hex(16)
                cursor.execute('''INSERT INTO qq_official_generation_capacity
                    (reservation_id,account_key,endpoint_key,requester_actor_key,source_execution_id,
                     source_key,owner,state,created_at,expires_at)
                    VALUES (?,?,?,?,?,?,?,'reserved',?,?)''',
                    (reservation_id,self.endpoint.account.key,self.endpoint.key,self.actor.key,
                     self.source.source_execution_id,self.source_key,owner,now,self.deadline_at))
                return CapacityReservation(reservation_id,self.source_key,owner)
        item=await self.calls(reserve)
        with _lock: _issued[id(item)]=item
        return item

    async def release_capacity(self,reservation):
        with _lock:
            if (not isinstance(reservation,CapacityReservation) or _issued.get(id(reservation)) is not reservation
                    or reservation.source_key!=self.source_key or reservation.owner not in {self.owner,self.owner+':verify'}):
                raise LeaseLost('unissued_generation_reservation')
        def release():
            with self.store.transaction() as cursor:
                now=self.store._now()
                current_source(cursor,self.source,now)
                cursor.execute('''UPDATE qq_official_generation_capacity SET state='released',released_at=?,
                    release_evidence='owned_destroy' WHERE reservation_id=? AND account_key=?
                    AND endpoint_key=? AND requester_actor_key=? AND source_key=? AND owner=? AND state='reserved' ''',
                    (now,reservation.reservation_id,self.endpoint.account.key,self.endpoint.key,self.actor.key,
                     self.source_key,reservation.owner))
        await self.calls(release)

# Expiry deliberately does not refund resources: an unknown create may exist.
# Runtime maintenance must verify the exact owner has no remaining workspaces,
# then record host_verified_absent before releasing expired reservations.
