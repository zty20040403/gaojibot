import { useEffect, useState } from 'react'
import { EmptyState, PageHeader, fmtTime } from './components'
import type { useControlPlane } from './useControlPlane'

export function RequestsView({ plane }: { plane: ReturnType<typeof useControlPlane> }) {
  const [turnId, setTurnId] = useState('')
  const [items, setItems] = useState<Record<string, any>[]>([])
  const [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    void plane.query(`/requests${turnId ? `?turn_id=${encodeURIComponent(turnId)}` : ''}`, controller.signal)
      .then((data) => { setItems(data.items); setError('') })
      .catch((reason) => { if (!controller.signal.aborted) setError(reason.message) })
    return () => controller.abort()
  }, [plane.query, turnId])
  return <>
    <PageHeader title="最近请求" description="模型实际收到的消息和工具定义，保留 14 天" />
    <label>回合编号 <input type="number" min="1" value={turnId} onChange={(event) => setTurnId(event.target.value)} placeholder="全部最近请求" /></label>
    {error && <p role="alert">{error}</p>}
    {items.map((item) => <details className="section" key={item.request_id}>
      <summary>#{item.turn_id ?? '后台'} · {item.model} · {fmtTime(item.created_at)} · {item.scope_key}</summary>
      <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{JSON.stringify({ messages: item.messages, tools: item.tools }, null, 2)}</pre>
    </details>)}
    {!items.length && !error && <EmptyState>还没有请求记录</EmptyState>}
  </>
}
