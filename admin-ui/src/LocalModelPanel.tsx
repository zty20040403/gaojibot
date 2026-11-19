import { EmptyState, Section, StatusBadge, fmtNumber, fmtTime } from './components'
import type { JsonObject } from './api'
import type { useControlPlane } from './useControlPlane'

const STATES: Record<string, string> = {
  ready: '已就绪', loading: '模型加载中', unreachable: '接口不可达',
  unavailable: '接口异常', unknown: '状态待确认',
}

export function LocalModelPanel({ plane }: { plane: ReturnType<typeof useControlPlane> }) {
  const data = plane.data.localModel
  if (!data?.configured) return <Section title="本地千问"><EmptyState>{data ? '未配置本地模型' : '正在读取状态'}</EmptyState></Section>
  return (
    <Section title="本地千问">
      <div className="local-model-head"><div><strong>{data.profile}</strong><code>{data.model}</code></div><StatusBadge value={data.ready ? 'ready' : data.state === 'loading' ? 'pending' : data.state} label={STATES[data.state] ?? '状态未知'} /></div>
      <dl className="local-model-metrics">
        <div><dt>简单聊天</dt><dd title="仅接管未指定个人、群或引用模型的简单聊天">{data.serving_simple_chat ? '千问优先' : data.simple_chat_selected ? '千问暂不接管' : '未配置为默认'}</dd></div>
        <div title={`Bot 本次启动以来累计：${fmtTime(data.metrics_since)}；不含被健康检查跳过的请求`}><dt>千问请求数</dt><dd>{fmtNumber(data.request_count)}</dd></div>
        <div title="从发送请求到首个响应的平均耗时，流式回答不包含后续生成时间；本次 Bot 进程累计"><dt>平均首响应</dt><dd>{data.average_latency_ms == null ? '暂无数据' : `${fmtNumber(data.average_latency_ms)} ms`}</dd></div>
      </dl>
      <div className="local-model-meta"><span>{data.reason}{data.circuit_state === 'open' ? ' · 模型请求熔断中' : ''}</span><time>探测于 {fmtTime(data.checked_at)}</time></div>
    </Section>
  )
}

export function ModelRouteSummary({ trace, detailed = false }: { trace: JsonObject; detailed?: boolean }) {
  const routing = Array.isArray(trace.model_routing) ? trace.model_routing : []
  return <div className="model-route-summary">
    <span>期望：<b>{trace.requested_profile ?? trace.profile ?? '-'}</b></span>
    <span title={trace.actual_model ?? trace.model}>实际：<b>{trace.actual_profile === '' ? '未成功响应' : trace.actual_profile ?? trace.profile ?? '-'}</b></span>
    {trace.routing_reason && <small className="model-route-reason">{trace.routing_reason}</small>}
    {detailed && routing.length > 0 && <details><summary>{routing.length} 次路由决策</summary><ol>{routing.map((item: JsonObject, index: number) => <li key={index}><span>{item.requested_profile} → {item.actual_profile || '未成功响应'}</span><small>{item.reason}</small>{(item.outcomes ?? []).map((attempt: JsonObject, attemptIndex: number) => <small key={attemptIndex}>{attempt.profile} · {attempt.status === 'succeeded' ? '成功' : attempt.status === 'skipped' ? '跳过' : '失败'} · {attempt.reason}</small>)}</li>)}</ol></details>}
  </div>
}
