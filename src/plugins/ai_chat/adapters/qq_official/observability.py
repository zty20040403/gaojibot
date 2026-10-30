"""SDK-free application health and protected aggregate diagnostics.

No credentials or IDs are returned. Read-only database checks are evidence of
health at a point in time, never authority to execute or send.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import time
from fastapi.responses import JSONResponse

from src.plugins.ai_chat.observability import telemetry
from src.plugins.ai_chat.adapters.qq_official.lease import _locally_current, _lock_parts
from .official_metrics import SAFE_STATES, finite, official_metrics

RUNTIME_STATES=frozenset(('disabled','starting','waiting','active','paused','failed','stopped'))
GATEWAY_STATES=frozenset(('closed','connecting','identifying','resuming','ready'))


def _count(value):
    return max(0,min(value,2**63-1)) if type(value) is int else 0


def _disabled(now,enabled=False,reason='disabled'):
    return dict(enabled=enabled,state='disabled' if not enabled else 'degraded',healthy=False,
        reason=reason,observed_at=now,lease={'current':False,'checked_at':None},
        gateway={'state':'closed','message_gap':False,'queue_depth':0,'queue_limit':0},
        sender={'running':False,'active':0,'rate_waiters':0,'unrecorded':0},
        execution={},delivery={},inbox={},files={},migration={},capabilities=[],
        windows={'active':0,'expired':0},quota={'reserved':0,'spent':0},
        cleanup={'scope':'process_cleanup_items','last_at':None,'deleted':0,'failed':0})


class OfficialObservability:
    def __init__(self,context,*,registry=None,clock=time.time,interval=3):
        if type(interval) not in (int,float) or not 0.1 <= interval <= 60:
            raise ValueError('invalid_health_interval')
        self.context,self.clock,self.interval=context,clock,interval
        self.metrics=official_metrics(registry or telemetry.registry)
        self._value=_disabled(int(clock()),reason='not_sampled')
        self._task=None;self._closed=False;self._runtime=None
        self._observed_monotonic=None

    @staticmethod
    def _download(context):
        keys=('configured','listening','proxy_reachable','evidence_configured')
        try:
            host=getattr(context,'official_host',None)
            raw=host.download_status() if host is not None else {}
            if type(raw) is not dict:raw={}
        except Exception:raw={}
        return {key:raw.get(key) is True for key in keys}

    async def details(self,*,section,limit=20,cursor=None):
        from .official_details import OfficialDetails,DetailsUnavailable
        runtime=getattr(self.context,'official_runtime',None)
        if self._closed or runtime is None:raise DetailsUnavailable()
        try:
            reader=OfficialDetails(runtime,clock=self.clock)
            result=await runtime._calls(reader.read,section=section,limit=limit,cursor=cursor)
        except DetailsUnavailable:raise
        except Exception:raise DetailsUnavailable() from None
        if runtime is not getattr(self.context,'official_runtime',None) or self._closed:raise DetailsUnavailable()
        return result

    def snapshot(self):
        value=deepcopy(self._value)
        value['download']=self._download(self.context)
        if self._closed or self._observed_monotonic is None or time.monotonic()-self._observed_monotonic>self.interval*3:
            value['healthy']=False
            value['lease']['current']=False
            value['gateway']['state']='closed'
            value['sender']['running']=False
            if value['enabled']:value['state']='degraded';value['reason']='health_sample_stale'
        return value

    async def start(self):
        if self._closed:raise RuntimeError('official_observability_closed')
        if self._task is None:
            await self.sample()
            self._task=asyncio.create_task(self._run())

    async def close(self):
        self._closed=True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task,return_exceptions=True)
            self._task=None
        self.metrics.health.set(0)
        if self._runtime is not None and getattr(self._runtime,'observer',None) is self.metrics:
            self._runtime.observer=None

    async def _run(self):
        while True:
            await asyncio.sleep(self.interval)
            await self.sample()

    async def sample(self):
        now=int(self.clock());runtime=getattr(self.context,'official_runtime',None)
        enabled=runtime is not None and getattr(getattr(runtime,'config',None),'enabled',False) is True
        value=_disabled(now,enabled,reason='runtime_unavailable' if enabled else 'disabled')
        if enabled:
            # This observer contains only counters; it is not an authority port.
            runtime.observer=self.metrics;self._runtime=runtime
            token=getattr(runtime,'_token',None)
            if token is not None:
                try:value=await runtime._calls(self._read,runtime,token,now)
                except Exception:value=_disabled(now,True,'diagnostics_unavailable')
            else:value['reason']='lease_unavailable';value['state']='degraded'
            # Replacement/stop during the query cannot produce a healthy result.
            if runtime is not getattr(self.context,'official_runtime',None) or token is not getattr(runtime,'_token',None):
                value['healthy']=False;value['state']='degraded';value['reason']='runtime_changed'
        self._value=value;self._observed_monotonic=time.monotonic();self.metrics.snapshot(value)
        return self.snapshot()

    def _read(self,runtime,token,now):
        value=_disabled(now,True,'lease_unavailable');value['state']='degraded'
        high,low=_lock_parts(token.advisory_key)
        with runtime.store.transaction() as c:
            c.execute("SET LOCAL statement_timeout = '1000ms'")
            # No FOR UPDATE: diagnostics do not acquire an account authority lock.
            lease=c.execute('''SELECT a.enabled,a.gateway_state,a.bot_identity_verified_at,
                (a.fencing_epoch=? AND a.holder_id=? AND a.lock_backend_pid=? AND EXISTS (
                  SELECT 1 FROM pg_catalog.pg_locks l WHERE l.locktype='advisory' AND l.granted
                    AND l.pid=a.lock_backend_pid AND l.classid::bigint=? AND l.objid::bigint=? AND l.objsubid=1
                    AND l.database=(SELECT oid FROM pg_catalog.pg_database WHERE datname=current_database()))) AS current
                FROM qq_official_accounts a WHERE a.account_key=?''',
                (token.epoch,token.holder_id,token.backend_pid,high,low,token.account_key)).fetchone()
            current=(lease is not None and lease['current'] is True and lease['enabled'] is True
                and lease['gateway_state']!='paused' and _locally_current(token))
            value['lease']={'current':current,'checked_at':now}
            for domain,table,column,filter_sql,args in (
                ('inbox','qq_official_inbound_events','status','account_key=?',(token.account_key,)),
                ('execution','qq_official_turn_executions','state','account_key=?',(token.account_key,)),
                ('delivery','deliveries','status','account_key=?',(token.account_key,)),
                ('files','qq_official_file_deliveries d JOIN qq_official_file_artifacts a USING(artifact_id)','d.state','a.account_key=?',(token.account_key,)),
                ('migration','migration_routes','phase','target_account_key=?',(token.account_key,))):
                rows=c.execute(f'SELECT {column} AS state,COUNT(*) AS n FROM {table} WHERE {filter_sql} GROUP BY {column}',args).fetchall()
                value[domain]={r['state']:_count(r['n']) for r in rows if r['state'] in SAFE_STATES[domain]}
            windows=c.execute('''SELECT COUNT(*) FILTER(WHERE state='active' AND expires_at>?) AS active,
                COUNT(*) FILTER(WHERE state='expired' OR expires_at<=?) AS expired
                FROM qq_official_reply_windows WHERE account_key=?''',(now,now,token.account_key)).fetchone()
            value['windows']={k:_count(windows[k]) for k in ('active','expired')}
            quotas=c.execute('''SELECT COALESCE(SUM(reserved_count),0)::bigint AS reserved,
                COALESCE(SUM(used_count),0)::bigint AS spent FROM qq_official_quota_periods
                WHERE account_key=? AND (period_end_at IS NULL OR period_end_at>?)''',(token.account_key,now)).fetchone()
            value['quota']={k:_count(quotas[k]) for k in ('reserved','spent')}
            value['cleanup']={'scope':'process_cleanup_items',**deepcopy(self.metrics.cleanup)}
            pending=c.execute('SELECT COUNT(*) AS n FROM qq_official_file_artifacts WHERE account_key=? AND cleanup_pending=TRUE',(token.account_key,)).fetchone()
            value['cleanup']['pending_blobs']=_count(pending['n'])
            verified=lease['bot_identity_verified_at'] if lease else None
            value['capabilities']=[{'kind':'bot_identity','status':'verified' if verified is not None else 'unknown',
                'verified_at':verified,'expires_at':None}]
            value['capabilities'] += self._file_evidence(c,runtime,token,now)
            # No verified physical contracts for these capabilities are implied.
            value['capabilities'] += [{'kind':kind,'status':'unknown','verified_at':None,'expires_at':None}
                for kind in ('recall','moderation','member_events','reactions','voice_output')]
        health=getattr(getattr(runtime,'_gateway',None),'health',{})
        if type(health) is not dict:health={}
        value['gateway']={'state':finite(health.get('state'),GATEWAY_STATES,'closed'),
            'message_gap':health.get('message_gap') is True,
            'queue_depth':_count(health.get('dispatch_queue_depth')),
            'queue_limit':_count(health.get('dispatch_queue_limit'))}
        sender=getattr(runtime,'sender',None)
        value['sender']={'running':getattr(sender,'_running',False) is True and not getattr(sender,'_stopping',False),
            'active':len(getattr(sender,'_workers',())), 'rate_waiters':len(getattr(sender,'_rate_waiters',())),
            'unrecorded':len(getattr(sender,'_unrecorded',()))}
        healthy=(current and health.get('state')=='ready' and not health.get('message_gap')
            and value['sender']['running'] and getattr(sender,'reason',None)=='ready'
            and not getattr(runtime,'_stopping',False))
        value['healthy']=bool(healthy);value['state']='ready' if healthy else 'degraded'
        value['reason']=('ready' if healthy else 'lease_unavailable' if not current else 'gateway_unavailable'
            if health.get('state')!='ready' or health.get('message_gap') else 'sender_unavailable')
        return value


    @staticmethod
    def _file_evidence(cursor,runtime,token,now):
        service=getattr(runtime,'file_service',None)
        return [{'kind':kind,'status':'available' if service is not None else 'disabled',
                 'verified_at':None,'expires_at':None,'max_bytes':service.config.max_file_bytes if service else 0}
                for kind in ('native_image','native_file')]


def register_official_http(app,context):
    service=OfficialObservability(context)
    # Aggregate readiness is safe publicly; no identities, credentials or counts.
    @app.get('/health/qq-official',include_in_schema=False)
    async def official_health():
        value=service.snapshot()
        return JSONResponse({k:value[k] for k in ('enabled','healthy','state','reason','observed_at')},
            status_code=200 if not value['enabled'] or value['healthy'] else 503,
            headers={'Cache-Control':'no-store'})
    return service
