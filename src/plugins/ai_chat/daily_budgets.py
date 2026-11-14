"""Soft automation budgets from host receipts, never from quota reservations.

Addressed replies remain billable. Unknown rates/usage and in-flight calls can
exceed the budget; this is not a supplier-enforced hard spending cap.
"""
from dataclasses import dataclass
from datetime import datetime,time,timedelta
from decimal import Decimal
import re
from zoneinfo import ZoneInfo

from .platform_types import EndpointKey

ZONE = ZoneInfo('Asia/Shanghai')


def amount(value):
    if type(value) is not str or re.fullmatch(r'[0-9]{1,7}(?:\.[0-9]{1,6})?',value) is None:
        raise ValueError('invalid_daily_budget_amount')
    result = Decimal(value)
    if result>1000000:raise ValueError('invalid_daily_budget_amount')
    return result


def interval(now):
    if type(now) is not int or now<0:raise ValueError('invalid_daily_budget_clock')
    try:
        local = datetime.fromtimestamp(now,ZONE)
        start = datetime.combine(local.date(),time(),ZONE)
        return int(start.timestamp()),int((start+timedelta(days=1)).timestamp())
    except (ValueError,OverflowError,OSError):
        raise ValueError('invalid_daily_budget_clock') from None


@dataclass(frozen=True)
class BudgetState:
    limit: Decimal | None
    spent: Decimal
    incomplete_calls: int
    pending_calls: int
    generation: int
    day_start: int
    day_end: int

    @property
    def blocked(self):return self.limit is not None and self.spent>=self.limit


def read(cursor,endpoint,*,now):
    if not isinstance(endpoint,EndpointKey) or endpoint.account.platform!='qq-official':
        raise ValueError('budget_requires_physical_endpoint')
    start,end = interval(now)
    # One MVCC statement covers settings and actual cost. Both endpoint and
    # physical scope must match; canonical history or current price is irrelevant.
    row = cursor.execute('''SELECT s.daily_budget_usd,COALESCE(s.budget_generation,0) AS generation,
        COALESCE(SUM(b.cost_usd),0) AS spent,
        COUNT(b.call_id) FILTER(WHERE NOT b.cost_complete) AS incomplete,
        COUNT(b.call_id) FILTER(WHERE b.finished_at IS NULL) AS pending
        FROM (SELECT 1) seed LEFT JOIN conversation_settings s ON s.scope_key=?
        LEFT JOIN model_usage_calls b ON b.endpoint_key=? AND b.scope_key=? AND b.started_at>=? AND b.started_at<?
        GROUP BY s.daily_budget_usd,s.budget_generation''',
        (endpoint.scope.key,endpoint.key,endpoint.scope.key,start,end)).fetchone()
    return BudgetState(row['daily_budget_usd'],Decimal(row['spent']),row['incomplete'],row['pending'],row['generation'],start,end)


def refresh(cursor,endpoint,*,now):
    """Serialize crossing/config/admission at account -> day -> settings order.

    A new crossing invalidates old automatic work. Reconfiguration can allow new
    work, but a second crossing that day must again invalidate it without issuing
    another notice. A restart never increments an already-recorded crossing.
    """
    if not isinstance(endpoint,EndpointKey) or endpoint.account.platform!='qq-official':
        raise ValueError('budget_requires_physical_endpoint')
    account = cursor.execute('SELECT fencing_epoch FROM qq_official_accounts WHERE account_key=? FOR UPDATE',
        (endpoint.account.key,)).fetchone()
    if account is None:raise ValueError('budget_endpoint_unavailable')
    state = read(cursor,endpoint,now=now)
    if not state.blocked:return state
    previous = cursor.execute('SELECT last_blocked_generation FROM conversation_budget_days WHERE scope_key=? AND day_start=? FOR UPDATE',
        (endpoint.scope.key,state.day_start)).fetchone()
    if previous is not None and previous['last_blocked_generation']==state.generation:return state
    generation = state.generation+1
    if previous is None:
        physical = cursor.execute('SELECT generation,route_epoch FROM qq_official_endpoints WHERE endpoint_key=?',
            (endpoint.key,)).fetchone()
        if physical is None:raise ValueError('budget_endpoint_unavailable')
        cursor.execute('''INSERT INTO conversation_budget_days(scope_key,day_start,endpoint_key,account_epoch,endpoint_generation,
            route_epoch,first_exceeded_at,budget_usd,spent_usd,last_blocked_generation)
            VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (endpoint.scope.key,state.day_start,endpoint.key,account['fencing_epoch'],physical['generation'],physical['route_epoch'],
             now,state.limit,state.spent,generation))
    else:
        cursor.execute('UPDATE conversation_budget_days SET last_blocked_generation=? WHERE scope_key=? AND day_start=?',
            (generation,endpoint.scope.key,state.day_start))
    cursor.execute('''UPDATE conversation_settings SET budget_generation=?,proactive_generation=proactive_generation+1
        WHERE scope_key=?''',(generation,endpoint.scope.key))
    return read(cursor,endpoint,now=now)


def require(cursor,endpoint,*,now,generation=None):
    # Receipt finalization/configuration commits crossings via refresh(). Denied
    # guards are read-only: raising must not roll back a newly written crossing.
    state = read(cursor,endpoint,now=now)
    if state.blocked:raise PermissionError('conversation_daily_budget_exceeded')
    if generation is not None and (type(generation) is not int or generation!=state.generation):
        raise PermissionError('conversation_daily_budget_replaced')
    return state.generation
