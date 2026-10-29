"""Finite-label QQ metrics. Called at actual boundaries, never on scrape."""
from __future__ import annotations

import time
import threading
import weakref
from prometheus_client import Counter, Gauge, Histogram

STATES = {
    'gateway': frozenset(('closed','connecting','identifying','resuming','ready')),
    'inbox': frozenset(('pending','processed','quarantined','revoked','skipped','duplicate','control','accepted')),
    'ingest': frozenset(('processed','deferred','skipped','quarantined','revoked','duplicate')),
    'send': frozenset(('started','accepted','confirmed_no','unknown')),
    'rate': frozenset(('admitted','expired','preempted')),
    'cleanup': frozenset(('completed','failed')),
    'migration': frozenset(('plan','freeze','drain','switch','verify','complete','rollback','prepare','activate','other')),
}
KINDS = frozenset(('passive','proactive','unknown'))
_lock = threading.Lock()
_instances = weakref.WeakKeyDictionary()


def finite(value, choices, fallback='other'):
    return value if type(value) is str and value in choices else fallback


class OfficialMetrics:
    def __init__(self, registry):
        self.events = Counter('gaoji_qq_official_events_total','Observed official boundary events.',
            ('boundary','result'),registry=registry)
        self.sends = Counter('gaoji_qq_official_send_total','Physical message request boundaries.',
            ('mode','result'),registry=registry)
        self.duration = Histogram('gaoji_qq_official_send_seconds','Physical message request duration.',
            ('mode','result'),buckets=(.05,.1,.25,.5,1,2,5,10,30),registry=registry)
        self.backlog = Gauge('gaoji_qq_official_backlog','Current durable rows by bounded stage.',
            ('domain','state'),registry=registry)
        self.health = Gauge('gaoji_qq_official_healthy','Fresh actual lease/gateway/sender readiness.',registry=registry)
        self.cleanup_deleted = Counter('gaoji_qq_official_cleanup_deleted_total','Confirmed cleanup items by type; tombstones do not imply deleted bytes.',
            ('kind',),registry=registry)
        self.migration_failures=Counter('gaoji_qq_official_migration_failures_total','Authenticated migration operations that failed.',('action',),registry=registry)
        self._seen_backlog = set()
        self.cleanup={'last_at':None,'deleted':0,'failed':0}

    def observe(self, boundary, result, *, mode='unknown', seconds=None, count=None, kind='inbox_payload'):
        if boundary not in STATES:
            return
        result = finite(result, STATES[boundary])
        if boundary == 'migration':
            self.migration_failures.labels(result).inc()
        elif boundary == 'send':
            mode = finite(mode,KINDS,'unknown')
            self.sends.labels(mode,result).inc()
            if type(seconds) in (int,float) and 0 <= seconds <= 86400:
                self.duration.labels(mode,result).observe(seconds)
        else:
            self.events.labels(boundary,result).inc()
        if boundary == 'cleanup' and result == 'completed' and type(count) is int and 0 <= count <= 10000:
            kind=finite(kind,{'inbox_payload','artifact_tombstone','orphan_blob'},'other')
            self.cleanup_deleted.labels(kind).inc(count)
            self.cleanup['last_at']=int(time.time());self.cleanup['deleted']+=count
        elif boundary=='cleanup' and result=='failed':
            self.cleanup['last_at']=int(time.time());self.cleanup['failed']+=1

    def snapshot(self, value):
        self.health.set(int(value.get('healthy') is True))
        keys = set()
        # These are count-only gauges. Refresh never increments event counters.
        for domain in ('inbox','execution','delivery','files','migration'):
            for state,count in value.get(domain,{}).items():
                if state not in SAFE_STATES.get(domain,()) or type(count) is not int or count < 0:
                    continue
                keys.add((domain,state)); self.backlog.labels(domain,state).set(count)
        for domain,state in self._seen_backlog-keys:
            self.backlog.labels(domain,state).set(0)
        self._seen_backlog=keys


SAFE_STATES = {
    'inbox': STATES['inbox'],
    'execution': frozenset(('prepared','executing','result_ready','handed_off','cancelled','needs_attention')),
    'delivery': frozenset(('pending','sending','accepted','committed','ambiguous','failed','cancelled')),
    'files': frozenset(('stored','publishing','publication_unknown','pending','uploading','uploaded','upload_unknown','link_ready','queued','message_accepted','send_failed','delivery_unknown','cancelled','expired','unsupported','cleanup_pending')),
    'migration': frozenset(('planned','frozen','draining','switching','verifying','completed','rollback_pending','rolled_back')),
}


def official_metrics(registry):
    with _lock:
        found=_instances.get(registry)
        if found is None:
            found=OfficialMetrics(registry);_instances[registry]=found
        return found


def observe(observer, boundary, result, **values):
    """Telemetry failure never changes protocol/transaction semantics."""
    if observer is not None:
        try: observer.observe(boundary,result,**values)
        except Exception: pass
