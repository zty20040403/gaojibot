"""Read-only accounting projections: physical scope, real receipts, fixed windows."""
from decimal import Decimal,ROUND_HALF_UP

from .platform_types import EndpointKey

# Each count distinguishes a partial sum from an actual zero. A pending request
# participates as unknown, even if the worker never got to write its receipt.
METRICS = '''COUNT(b.call_id) AS calls,
    COUNT(b.call_id) FILTER (WHERE b.finished_at IS NULL) AS pending_calls,
    COUNT(b.call_id) FILTER (WHERE NOT b.cost_complete) AS incomplete_cost_calls,
    COUNT(b.call_id) FILTER (WHERE b.input_tokens IS NULL) AS missing_input_calls,
    COUNT(b.call_id) FILTER (WHERE b.output_tokens IS NULL) AS missing_output_calls,
    COUNT(b.call_id) FILTER (WHERE b.input_tokens IS NULL OR b.cached_tokens IS NULL) AS missing_cache_calls,
    COALESCE(SUM(b.input_tokens),0) AS input_tokens,
    COALESCE(SUM(b.output_tokens),0) AS output_tokens,
    COALESCE(SUM(b.cached_tokens),0) AS cached_tokens,
    COALESCE(SUM(b.cache_write_tokens),0) AS cache_write_tokens,
    COALESCE(SUM(b.cost_usd),0) AS cost_usd'''


def metrics(row):
    result = {name:int(row[name]) for name in ('calls','pending_calls','incomplete_cost_calls','missing_input_calls',
        'missing_output_calls','missing_cache_calls','input_tokens','output_tokens','cached_tokens','cache_write_tokens')}
    result['cost_usd'] = Decimal(row['cost_usd'])
    result['cost_complete'] = result['incomplete_cost_calls']==0
    result['cache_percent'] = (Decimal(result['cached_tokens'])*100/Decimal(result['input_tokens'])).quantize(
        Decimal('0.1'),rounding=ROUND_HALF_UP) if result['missing_cache_calls']==0 and result['input_tokens'] else None
    return result


def windows(cursor,endpoint,*,now):
    if not isinstance(endpoint,EndpointKey) or endpoint.account.platform!='qq-official':
        raise ValueError('billing_requires_physical_endpoint')
    if type(now) is not int or not 0<=now<=2**63-1:raise ValueError('invalid_billing_clock')
    # One statement supplies both windows from one MVCC snapshot. No current
    # catalogue price, legacy usage_events, canonical history or visibility join.
    rows = cursor.execute('''WITH periods(seconds) AS (VALUES (86400),(604800))
        SELECT p.seconds,'''+METRICS+''' FROM periods p LEFT JOIN model_usage_calls b
        ON b.endpoint_key=? AND b.scope_key=? AND b.started_at>=?-p.seconds AND b.started_at<=?
        GROUP BY p.seconds ORDER BY p.seconds''',(endpoint.key,endpoint.scope.key,now,now)).fetchall()
    return {row['seconds']:metrics(row) for row in rows}


def usd(value):
    text = format(value,'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def status_lines(totals):
    lines = []
    for seconds,label in ((86400,'24 小时'),(604800,'7 天')):
        row = totals[seconds]
        if not row['calls']:
            lines.append(label+'：暂无已记录的模型请求。')
            continue
        incoming = ('≥' if row['missing_input_calls'] else '')+f"{row['input_tokens']:,}"
        outgoing = ('≥' if row['missing_output_calls'] else '')+f"{row['output_tokens']:,}"
        cache = f"{row['cache_percent']}%" if row['cache_percent'] is not None else '未知' if row['missing_cache_calls'] else '无输入'
        cost = ('≥ ' if not row['cost_complete'] else '')+'$'+usd(row['cost_usd'])
        note = f"（{row['incomplete_cost_calls']} 次费用不完整，其中 {row['pending_calls']} 次未收尾）" if not row['cost_complete'] else ''
        lines.append(f'{label}：输入 {incoming} · 输出 {outgoing} Token · 缓存命中 {cache} · {cost}{note}')
    return '\n'.join(lines)
