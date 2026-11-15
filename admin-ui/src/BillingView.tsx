import { useEffect, useState } from 'react'
import type { JsonObject } from './api'
import { DataTable, EmptyState, RefreshButton, Section, fmtTime } from './components'
import type { useControlPlane } from './useControlPlane'

const dimensions = { scope: '会话', job: '后台任务', program: '常驻程序', actor: '发起人' }
// Never pass decimal money or BIGINT token sums through JS Number.
function decimal(value: unknown) {
  if (typeof value !== 'string' || !/^\d+(?:\.\d+)?$/.test(value)) return '未知'
  const [whole, fraction] = value.split('.')
  return whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + (fraction === undefined ? '' : `.${fraction}`)
}
function cost(row: JsonObject) {
  return `${row.cost_complete === true ? '' : '≥ '}$${decimal(row.cost_usd)}`
}
function cache(row: JsonObject) {
  return row.cache_percent == null ? (row.missing_cache_calls ? '未知' : '无输入') : `${row.cache_percent}%`
}
function Fee({ row }: { row: JsonObject }) {
  return <>{cost(row)}{row.incomplete_cost_calls > 0 && <small className="cell-sub">
    {row.incomplete_cost_calls} 次费用不完整 · {row.pending_calls} 次请求未收尾</small>}</>
}

export function BillingTotals({ totals }: { totals: JsonObject }) {
  return <p>所选范围合计（全部分组，不只是本页）：{totals.calls} 次调用 · <Fee row={totals} />
    <span className="cell-sub">输入 {totals.missing_input_calls ? '≥ ' : ''}{decimal(totals.input_tokens)} ·
      输出 {totals.missing_output_calls ? '≥ ' : ''}{decimal(totals.output_tokens)} Token · 缓存命中 {cache(totals)}</span></p>
}

export function BillingRecords({ items }: { items: JsonObject[] }) {
  return <DataTable><thead><tr><th>物理会话／归属</th><th>调用／费用</th><th>输入／输出 Token</th><th>缓存命中</th></tr></thead>
    <tbody>{items.map((row) => <tr key={row.id}>
      <td><code>{row.scope_key}</code><small className="cell-sub">{row.label}
        {row.person != null && <> · person#{row.person}</>}</small>
        {row.shared && <strong className="cell-sub">共同费用（只计一笔，不向每人重复分摊）</strong>}
        {row.programs?.length > 0 && <small className="cell-sub">程序：{row.programs.join(' + ')}</small>}
        {row.commands?.length > 0 && <small className="cell-sub">命令：{row.commands.map((id: string) => `command#${id}`).join(' + ')}</small>}
      </td>
      <td>{row.calls} 次<small className="cell-sub"><Fee row={row} /></small></td>
      <td>输入 {row.missing_input_calls ? '≥ ' : ''}{decimal(row.input_tokens)}
        <small className="cell-sub">输出 {row.missing_output_calls ? '≥ ' : ''}{decimal(row.output_tokens)}</small></td>
      <td>{cache(row)}</td>
    </tr>)}</tbody></DataTable>
}

export function BillingView({ query }: { query: ReturnType<typeof useControlPlane>['query'] }) {
  const [scope, setScope] = useState('')
  const [dimension, setDimension] = useState('scope')
  const [days, setDays] = useState('7')
  const [filter, setFilter] = useState({ scope: '', dimension: 'scope', days: '7', asOf: '', after: '' })
  const [revision, refresh] = useState(0)
  const [snapshot, setSnapshot] = useState<{ key: string; payload: JsonObject } | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const params = new URLSearchParams({ scope_key: filter.scope, dimension: filter.dimension, days: filter.days, limit: '50', after: filter.after })
  if (filter.asOf) params.set('as_of', filter.asOf)
  const path = `/model-costs?${params}`
  const payload = snapshot?.key === path ? snapshot.payload : null
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true); setError(''); setSnapshot(null)
    void query(path, controller.signal).then((value) => {
      if (!controller.signal.aborted) setSnapshot({ key: path, payload: value })
    }).catch((reason) => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '读取费用统计失败')
    }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [path, query, revision])
  const reset = () => { setFilter((value) => ({ ...value, asOf: '', after: '' })); refresh((value) => value + 1) }
  const items: JsonObject[] = payload?.items ?? []
  return <Section title="模型费用归属" description="只读；按宿主真实用量和请求当时的美元单价统计，不等于供应商最终结算，也不把旧 Token 额度预留当实际费用。"
    action={<RefreshButton loading={loading} onClick={reset} />}>
    <p>四个维度是同一笔账单的不同投影，不能跨维度相加。多程序或命令共同唤醒记共同费用；旧数据不补猜发起人。
      显示名取组内最近请求的来源快照，不读取现在的用户名。</p>
    <form className="actions" onSubmit={(event) => { event.preventDefault(); setFilter({ scope: scope.trim(), dimension, days, asOf: '', after: '' }); refresh((value) => value + 1) }}>
      <select aria-label="费用维度" value={dimension} onChange={(event) => setDimension(event.target.value)}>
        {Object.entries(dimensions).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select>
      <select aria-label="费用周期" value={days} onChange={(event) => setDays(event.target.value)}>
        {[1, 7, 30, 90, 365].map((value) => <option key={value} value={value}>最近 {value} 天</option>)}
      </select>
      <input aria-label="费用物理会话" placeholder="物理 scope_key（精确匹配；留空为全部）" maxLength={1000} value={scope} onChange={(event) => setScope(event.target.value)} />
      <button type="submit" disabled={loading}>查询费用</button>
    </form>
    {error && <p role="alert" className="error-text">{error}</p>}
    {payload?.available && <><p>请求开始时间范围：{fmtTime(payload.since)} 至 {fmtTime(payload.as_of)}（不含结束时刻） · {payload.group_count} 个分组。
      翻页保持同一时间范围；在途费用及迟到数据会随新读取更新，刷新可重新查看全部。</p><BillingTotals totals={payload.totals} /></>}
    <BillingRecords items={items} />
    {!loading && !items.length && !error && <EmptyState>{payload?.available === false ? '账单数据库未配置' : '没有符合条件的模型请求'}</EmptyState>}
    <p className="actions"><button disabled={loading || !filter.after} onClick={reset}>返回最新范围</button>
      <button disabled={loading || !payload?.next_cursor} onClick={() => setFilter({ ...filter, asOf: String(payload!.as_of), after: payload!.next_cursor })}>下一页 50 组</button></p>
  </Section>
}
