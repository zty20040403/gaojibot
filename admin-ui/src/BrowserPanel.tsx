import { useEffect, useRef, useState } from 'react'
import type { JsonObject } from './api'
import { DataTable, EmptyState, RefreshButton, Section, fmtTime } from './components'
import type { useControlPlane } from './useControlPlane'

type Plane = ReturnType<typeof useControlPlane>
const outcomes: Record<string, string> = {
  cleared: '已确认清空当时的本地资料', not_applied: '此次未执行清理', unknown: '结果不明，可能已执行；不自动重试',
}

export function browserClearIntent(endpoint: string, payload: JsonObject | null) {
  const snapshot = payload?.snapshot
  if (!endpoint || payload?.endpoint_key !== endpoint || payload.available !== true || snapshot?.state !== 'ready' ||
    typeof snapshot.fence !== 'string' || !/^[a-f0-9]{64}$/.test(snapshot.fence) || !Number.isSafeInteger(payload.resource_version) || payload.resource_version < 0) return null
  return { body: { endpoint_key: endpoint, fence: snapshot.fence, confirmation: 'clear' }, version: payload.resource_version }
}

export function BrowserDetails({ endpoint, payload, busy, clear }: {
  endpoint: string; payload: JsonObject; busy: boolean; clear: () => void
}) {
  const snapshot = payload.snapshot
  return <>
    {payload.available !== true ? <EmptyState>会话 VM 运行时不可用，无法确认浏览器状态。</EmptyState> :
      snapshot?.state === 'ready' ? <>
        <p>本会话 Cookie 域名（不保证登录仍有效）：</p>
        {snapshot.domains?.length ? <ul>{snapshot.domains.map((domain: string) => <li key={domain}><code>{domain}</code></li>)}</ul> :
          <p>没有未过期 Cookie 记录；不代表所有网站均未登录。</p>}
        {snapshot.truncated && <p>域名列表已截断，最多显示200项。</p>}
      </> : <EmptyState>{snapshot?.state === 'offline' ? 'VM 已离线；未擅自开机。' :
        snapshot?.state === 'missing' ? '本会话没有 VM；未创建或开机。' : '无法确认浏览器状态。'}</EmptyState>}
    <button className="danger" disabled={busy || !browserClearIntent(endpoint, payload)} onClick={clear}>
      {busy ? '等待清理结果…' : '清空全部浏览器资料'}
    </button>
    <p>清空会关闭本会话浏览器并删除全部本地站点资料，不撤销其他设备或服务器会话。操作进行中或结果不明时，请先核对再重新登录；关闭页面不能撤回已发出的清理请求。</p>
    <DataTable><thead><tr><th>最近清理</th><th>结果</th></tr></thead><tbody>
      {(payload.recent ?? []).map((item: JsonObject) => <tr key={item.operation_id}>
        <td>{fmtTime(item.created_at)}<small className="cell-sub"><code>{item.operation_id}</code></small></td>
        <td>{outcomes[item.state] ?? outcomes.unknown}<small className="cell-sub">{item.error_code}</small></td>
      </tr>)}
    </tbody></DataTable>
  </>
}

export function BrowserPanel({ plane }: { plane: Plane }) {
  const [endpoint, setEndpoint] = useState('')
  const [payload, setPayload] = useState<JsonObject | null>(null)
  const [revision, refresh] = useState(0)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const inFlight = useRef(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  useEffect(() => {
    if (!endpoint) { setPayload(null); setLoading(false); return }
    if (busy) return
    const controller = new AbortController()
    setLoading(true); setPayload(null); setError('')
    void plane.query(`/conversations/browser?${new URLSearchParams({ endpoint_key: endpoint })}`, controller.signal)
      .then((value) => { if (!controller.signal.aborted) setPayload({ ...value, endpoint_key: endpoint }) })
      .catch((reason) => { if (!controller.signal.aborted) setError(String(reason)) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [plane.query, plane.data.browser, endpoint, revision, busy])

  const clear = async () => {
    const intent = browserClearIntent(endpoint, payload)
    if (!intent || loading || inFlight.current) return
    if (!window.confirm(`清空此会话全部浏览器资料？所有本地登录状态将被移除，不能撤销。\n${endpoint}`)) return
    inFlight.current = true; setBusy(true); setError(''); setNotice('')
    try {
      const result = await plane.mutate('browser', '/conversations/browser/clear', 'POST', intent.body, ['browser'], intent.version)
      setNotice(`${outcomes[result.state] ?? outcomes.unknown}${result.error_code ? ` · ${result.error_code}` : ''}`)
    } catch (reason) {
      setNotice(`未取得已确认的清理结果，可能已执行；请刷新核对，不要自动重试。${String(reason)}`)
    } finally {
      inFlight.current = false; setBusy(false); refresh((n) => n + 1)
    }
  }
  return <Section title="会话浏览器" description="只查询所选物理会话；Cookie 值不离开 VM，不因查询或清理自动开机。域名不代表登录仍有效。"
    action={<RefreshButton loading={loading || busy} onClick={() => refresh((n) => n + 1)} />}>
    <input aria-label="浏览器所属会话" list="browser-endpoints" maxLength={1000} disabled={busy} value={endpoint}
      placeholder="选择或填写完整 endpoint_key" onChange={(event) => {
        setEndpoint(event.target.value); setPayload(null); setNotice(''); setError('')
      }} />
    <datalist id="browser-endpoints">{(plane.data.conversations?.items ?? []).map((item: JsonObject) =>
      <option key={item.endpoint_key} value={item.endpoint_key} />)}</datalist>
    {!endpoint && <EmptyState>选择会话后查询浏览器域名与最近清理结果。</EmptyState>}
    {error && <p role="alert" className="error-text">快照读取失败；不能据此认定已退出。{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {payload && <BrowserDetails endpoint={endpoint} payload={payload} busy={busy || loading} clear={() => void clear()} />}
  </Section>
}
