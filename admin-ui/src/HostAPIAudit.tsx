import { useEffect, useState } from 'react'
import type { JsonObject } from './api'
import { DataTable, EmptyState, RefreshButton, Section, fmtTime } from './components'
import type { useControlPlane } from './useControlPlane'

const outcomes: Record<string, string> = {
  pending: '尚未确认结束', completed: '响应读取完成', failed: '失败', denied: '拒绝', interrupted: '中断／结果待核对',
}

export function HostAPIAudit({ query }: { query: ReturnType<typeof useControlPlane>['query'] }) {
  const [scope, setScope] = useState('')
  const [api, setApi] = useState('')
  const [filter, setFilter] = useState({ scope: '', api: '', before: 0 })
  const [revision, refresh] = useState(0)
  const [payload, setPayload] = useState<JsonObject | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    const params = new URLSearchParams({ limit: '50', scope_key: filter.scope, api_name: filter.api, before: String(filter.before) })
    setLoading(true); setError(''); setPayload(null)
    void query(`/host-apis?${params}`, controller.signal).then((value) => {
      if (!controller.signal.aborted) setPayload(value)
    }).catch((reason) => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '读取 API 审计失败')
    }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [query, filter, revision])

  const items: JsonObject[] = payload?.items ?? []
  return <Section title="宿主 API 调用审计" description="只记录路径，不记录查询参数、请求体或密钥。中断／未完成不代表外部未生效，写操作不会自动重试。"
    action={<RefreshButton loading={loading} onClick={() => refresh((value) => value + 1)} />}>
    <form className="actions" onSubmit={(event) => { event.preventDefault(); setFilter({ scope: scope.trim(), api: api.trim(), before: 0 }) }}>
      <input aria-label="API 名称" placeholder="API 名称（精确匹配）" maxLength={48} value={api} onChange={(event) => setApi(event.target.value)} />
      <input aria-label="物理会话" placeholder="物理 scope_key（精确匹配）" maxLength={1000} value={scope} onChange={(event) => setScope(event.target.value)} />
      <button type="submit" disabled={loading}>查询</button>
    </form>
    {error && <p role="alert" className="error-text">{error}</p>}
    {payload && <p>所选范围最近 24 小时：{Object.entries(payload.counts ?? {}).map(([state, count]) => `${outcomes[state] ?? state} ${count}`).join(' · ') || '无记录'}</p>}
    <DataTable><thead><tr><th>时间</th><th>会话／来源</th><th>API</th><th>方法／路径</th><th>HTTP／结果</th></tr></thead>
      <tbody>{items.map((call) => <tr key={call.call_id}>
        <td>{fmtTime(call.created_at)}</td>
        <td><code>{call.scope_key}</code><small className="cell-sub">{call.caller_ref}</small></td>
        <td>{call.api_name}</td><td><code>{call.method} {call.path}</code></td>
        <td>{call.status_code || '未知'} · {outcomes[call.outcome] ?? call.outcome}<small className="cell-sub">{call.error_code}</small></td>
      </tr>)}</tbody></DataTable>
    {!loading && !items.length && !error && <EmptyState>{payload?.available === false ? '审计数据库未配置' : '没有符合条件的调用'}</EmptyState>}
    <p className="actions">
      <button disabled={loading || !filter.before} onClick={() => setFilter({ ...filter, before: 0 })}>返回最新</button>
      <button disabled={loading || !payload?.next_cursor} onClick={() => setFilter({ ...filter, before: payload!.next_cursor })}>更早 50 条</button>
    </p>
  </Section>
}
