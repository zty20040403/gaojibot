import { useEffect, useState } from 'react'
import type { JsonObject } from './api'
import { DataTable, EmptyState, RefreshButton, Section, fmtTime } from './components'
import type { useControlPlane } from './useControlPlane'

const outcomes: Record<string, string> = {
  sending: '批准通知尚未确认', pending: '等待 owner 回复', approved: '已批准，尚未执行', executing: '执行中，结果尚未确认',
  succeeded: '操作执行成功（健康另验）', failed: '操作失败', denied: '未批准，没有执行', expired: '批准已过期，没有执行',
  cancelled: '执行前已取消', unknown: '结果不明，可能已执行；不自动重试',
}
const notices: Record<string, string> = {
  committed: '通知已确认', failed: '通知失败', cancelled: '通知已取消', expired: '通知已过期', ambiguous: '通知结果不明',
}
function person(value: JsonObject | null) {
  return value ? `${value.display || '未知'}${value.person ? ` · person#${value.person}` : ''}` : '无已确认的批准人'
}

export function OperationsRecords({ items }: { items: JsonObject[] }) {
  return <DataTable><thead><tr><th>操作／时间</th><th>来源／批准</th><th>操作单／参数</th><th>状态／输出</th></tr></thead>
    <tbody>{items.map((item) => <tr key={item.action_id}>
      <td><code>{item.action_id}</code><small className="cell-sub">提出：{fmtTime(item.created_at)}</small>
        <small className="cell-sub">批准截止：{fmtTime(item.expires_at)}</small><small className="cell-sub">记录更新：{fmtTime(item.updated_at)}</small></td>
      <td><code>{item.scope_key}</code><small className="cell-sub">{item.job} · {item.request_id}</small>
        <small className="cell-sub">提出：{person(item.requester)}</small><small className="cell-sub">批准：{person(item.approver)}</small>
        {item.approved_at && <small className="cell-sub">{fmtTime(item.approved_at)}</small>}
        <small className="cell-sub">{notices[item.notice_status] ?? '通知未确认'}</small></td>
      <td>{item.plan_name}<details><summary>完整参数</summary><pre>{JSON.stringify(item.parameters, null, 2)}</pre></details></td>
      <td>{outcomes[item.state] ?? `未知状态：${item.state}`}<small className="cell-sub">{item.error_code}</small>
        {item.started_at && <small className="cell-sub">开始：{fmtTime(item.started_at)}</small>}
        {item.finished_at && <small className="cell-sub">结束：{fmtTime(item.finished_at)}</small>}
        {item.remote_id && <small className="cell-sub">远端记录：<code>{item.remote_id}</code></small>}
        {item.output_summary ? <details><summary>输出摘要（外部资料）</summary><pre>{item.output_summary}</pre></details> : <small className="cell-sub">暂无输出摘要</small>}
      </td>
    </tr>)}</tbody></DataTable>
}

export function OperationsView({ query }: { query: ReturnType<typeof useControlPlane>['query'] }) {
  const [scope, setScope] = useState('')
  const [plan, setPlan] = useState('')
  const [state, setState] = useState('')
  const [filter, setFilter] = useState({ scope: '', plan: '', state: '', before: '' })
  const [revision, refresh] = useState(0)
  const [snapshot, setSnapshot] = useState<{ key: string; payload: JsonObject; at: number } | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const path = `/ops-actions?${new URLSearchParams({ limit: '50', scope_key: filter.scope, plan: filter.plan, state: filter.state, before: filter.before })}`
  const payload = snapshot?.key === path ? snapshot.payload : null
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true); setError(''); setSnapshot(null)
    void query(path, controller.signal).then((value) => {
      if (!controller.signal.aborted) setSnapshot({ key: path, payload: value, at: Date.now() })
    }).catch((reason) => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '读取运维审计失败')
    }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [path, query, revision])

  const items: JsonObject[] = payload?.items ?? []
  return <Section title="运维操作审计" description="只读快照，不提供批准、执行或重试按钮。批准必须由原 owner 从原私聊亲自回复短码；管理员登录不能代替。"
    action={<RefreshButton loading={loading} onClick={() => refresh((value) => value + 1)} />}>
    <p>运维输出可能敏感，仅供管理员核对，不转发到群或公开页面。执行成功不表示服务健康；结果不明可能仍已生效，不能盲目重做。</p>
    <form className="actions" onSubmit={(event) => { event.preventDefault(); setFilter({ scope: scope.trim(), plan: plan.trim(), state, before: '' }) }}>
      <input aria-label="运维物理会话" placeholder="物理 scope_key（精确匹配）" maxLength={1000} value={scope} onChange={(event) => setScope(event.target.value)} />
      <input aria-label="操作单名称" placeholder="操作单名称（精确匹配）" maxLength={48} value={plan} onChange={(event) => setPlan(event.target.value)} />
      <select aria-label="操作状态" value={state} onChange={(event) => setState(event.target.value)}><option value="">全部状态</option>
        {Object.entries(outcomes).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select><button type="submit" disabled={loading}>查询</button>
    </form>
    {error && <p role="alert" className="error-text">{error}</p>}
    {payload && <p>快照读取于 {new Date(snapshot!.at).toLocaleTimeString('zh-CN', { hour12: false })}；状态变化请刷新。所选范围：
      {Object.entries(payload.counts ?? {}).map(([key, count]) => `${outcomes[key] ?? key} ${count}`).join(' · ') || '无记录'}</p>}
    <OperationsRecords items={items} />
    {!loading && !items.length && !error && <EmptyState>{payload?.available === false ? '审计数据库未配置' : '没有符合条件的操作'}</EmptyState>}
    <p className="actions"><button disabled={loading || !filter.before} onClick={() => setFilter({ ...filter, before: '' })}>返回最新</button>
      <button disabled={loading || !payload?.next_cursor} onClick={() => setFilter({ ...filter, before: payload!.next_cursor })}>更早 50 条</button></p>
  </Section>
}
