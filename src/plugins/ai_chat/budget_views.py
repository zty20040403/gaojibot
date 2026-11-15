"""Physical daily receipts and notice outcomes; exact strings, never quota estimates."""
from datetime import datetime

from . import daily_budgets
from .model_billing_views import usd

NOTICE_LABELS = {'not_required':'无通知意图','pending':'等待发送','sending':'已排队，尚未确认',
    'committed':'已确认送达','failed':'发送失败，不重试','unknown':'结果不明，不重试','cancelled':'已取消，不重放'}


def project(cursor,endpoints,*,now):
    states = daily_budgets.read_many(cursor,endpoints,now=now)
    if not endpoints:return []
    start,_ = daily_budgets.interval(now)
    notices = {row['endpoint_key']:row for row in cursor.execute('''SELECT endpoint_key,scope_key,notice_state,
        first_exceeded_at,budget_usd,spent_usd FROM conversation_budget_days
        WHERE day_start=? AND endpoint_key IN ('''+','.join('?' for _ in endpoints)+')',
        (start,*(endpoint.key for endpoint in endpoints))).fetchall()}
    items = []
    for endpoint in endpoints:
        state = states[endpoint.key]
        notice = notices.get(endpoint.key)
        if notice is not None and notice['scope_key']!=endpoint.scope.key:notice = None
        items.append(dict(endpoint_key=endpoint.key,kind=endpoint.kind,
            daily_budget_usd=usd(state.limit) if state.limit is not None else None,spent_usd=usd(state.spent),
            cost_is_lower_bound=state.incomplete_calls>0,incomplete_calls=state.incomplete_calls,pending_calls=state.pending_calls,
            blocked=state.blocked,stopped=state.stopped,budget_generation=state.generation,
            day_start=state.day_start,day_end=state.day_end,timezone='Asia/Shanghai',
            notice_state=notice['notice_state'] if notice else 'not_required',
            first_exceeded_at=notice['first_exceeded_at'] if notice else None,
            notice_budget_usd=usd(notice['budget_usd']) if notice else None,
            notice_spent_usd=usd(notice['spent_usd']) if notice else None))
    return items


def status_lines(view):
    limit = '关闭' if view['daily_budget_usd'] is None else '$'+view['daily_budget_usd']
    expense = ('≥ ' if view['cost_is_lower_bound'] else '')+'$'+view['spent_usd']
    incomplete = f"；{view['incomplete_calls']}次费用不完整（{view['pending_calls']}次未收尾）" if view['cost_is_lower_bound'] else ''
    reset = datetime.fromtimestamp(view['day_end'],daily_budgets.ZONE).strftime('%m-%d %H:%M')
    direct = '群内仅响应 @' if view['kind']=='group' else '仍可直接私聊提问'
    return (f"北京时间今日预算：{limit}；已记录费用 {expense}{incomplete}。\n"
        f"自动动作：{'预算限制中' if view['blocked'] else '未被预算限制'}；超额通知：{NOTICE_LABELS[view['notice_state']]}。\n"
        f"北京时间 {reset} 恢复新自动动作，不补跑旧请求，不清除 /stop。\n"
        f"超额停止主动接话/程序唤醒，{direct}仍计费；未知/在途费用可能超出，不是供应商硬限额。")
