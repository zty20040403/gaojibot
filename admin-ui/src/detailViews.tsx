import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import {
  ArrowLeft,
  Ban,
  Check,
  ChevronLeft,
  ChevronRight,
  Clock3,
  ExternalLink,
  Play,
  RotateCcw,
  Search,
  X,
} from 'lucide-react'
import {
  DataTable,
  EmptyState,
  RefreshButton,
  StatusBadge,
  fmtBytes,
  fmtDuration,
  fmtNumber,
  fmtTime,
} from './components'
import type { ResourceName } from './api'
import { ModelRouteSummary } from './LocalModelPanel'
import type { useControlPlane } from './useControlPlane'

type Plane = ReturnType<typeof useControlPlane>

export type DetailViewId =
  | 'usage-records'
  | 'jobs'
  | 'deliveries'
  | 'traces'
  | 'media-items'
  | 'sources'
  | 'audit-records'

export const DETAIL_META: Record<DetailViewId, {
  parent: 'overview' | 'usage' | 'tasks' | 'traces' | 'media' | 'audit'
  route: string
  title: string
  description: string
  parentLabel: string
}> = {
  'usage-records': { parent: 'usage', route: 'usage/records', title: '全部用量明细', description: '按日期、范围和调用来源查询 Token 消耗', parentLabel: '模型用量' },
  jobs: { parent: 'tasks', route: 'tasks/jobs', title: '全部持久任务', description: '查询后台任务状态、重试次数和错误', parentLabel: '任务与投递' },
  deliveries: { parent: 'tasks', route: 'tasks/deliveries', title: '全部消息投递', description: '查询 QQ 消息回执、发送时间和失败原因', parentLabel: '任务与投递' },
  traces: { parent: 'traces', route: 'traces/all', title: '全部 Trace', description: '查询模型、工具、Token 与完整 Trace ID', parentLabel: 'Trace 与上下文' },
  'media-items': { parent: 'media', route: 'media/stickers', title: '全部表情候选', description: '查询表情识图、安全状态和审核记录', parentLabel: '媒体审核' },
  sources: { parent: 'media', route: 'media/sources', title: '全部分享内容', description: '查询帖子和视频解析状态及最后出现时间', parentLabel: '媒体审核' },
  'audit-records': { parent: 'audit', route: 'audit/records', title: '全部审计记录', description: '查询控制台修改、操作者、资源版本和结果', parentLabel: '审计记录' },
}

interface DetailSource {
  resource: ResourceName
  path: string
  extract: (payload: Record<string, any>) => any[]
}

const DETAIL_SOURCES: Record<DetailViewId, DetailSource> = {
  'usage-records': { resource: 'usage', path: '/usage?days=365', extract: (payload) => rows(payload.items) },
  jobs: { resource: 'jobs', path: '/jobs?limit=500', extract: (payload) => rows(payload.items) },
  deliveries: { resource: 'deliveries', path: '/deliveries?limit=500', extract: (payload) => rows(payload.items) },
  traces: { resource: 'traces', path: '/traces?limit=500', extract: (payload) => rows(payload.items) },
  'media-items': { resource: 'media', path: '/media?limit=500', extract: (payload) => rows(payload.items) },
  sources: { resource: 'sources', path: '/sources?limit=500', extract: (payload) => rows(payload.items) },
  'audit-records': { resource: 'audit', path: '/audit?limit=500', extract: (payload) => rows(payload.items) },
}

interface DetailColumn {
  label: string
  className?: string
  render: (item: any) => ReactNode
}

function rows(value: unknown): any[] {
  return Array.isArray(value) ? value : []
}

function jsonRows(value: unknown): any[] {
  if (Array.isArray(value)) return value
  try {
    return rows(JSON.parse(String(value ?? '[]')))
  } catch {
    return []
  }
}

function itemTime(detail: DetailViewId, item: any): unknown {
  if (detail === 'usage-records') return item.day
  if (detail === 'jobs' || detail === 'deliveries') return item.updated_at
  if (detail === 'traces') return item.started_at
  if (detail === 'audit-records') return item.created_at
  return item.last_seen_at ?? item.fetched_at
}

function timestamp(value: unknown): number {
  const numeric = Number(value)
  if (Number.isFinite(numeric)) return numeric > 10_000_000_000 ? numeric : numeric * 1000
  const parsed = Date.parse(String(value ?? ''))
  return Number.isFinite(parsed) ? parsed : 0
}

function rowKey(detail: DetailViewId, item: any, index: number): string {
  return String(
    item.audit_id
      ?? item.delivery_id
      ?? item.job_id
      ?? item.trace_id
      ?? item.turn_id
      ?? item.media_id
      ?? item.source_id
      ?? item.event_id
      ?? item.notification_id
      ?? item.incident_key
      ?? item.fingerprint
      ?? `${detail}-${index}`,
  )
}

function detailColumns(detail: DetailViewId, plane: Plane): DetailColumn[] {
  if (detail === 'usage-records') return [
    { label: '统计日期', render: (item) => item.day },
    { label: 'Scope', render: (item) => <code>{item.scope_key || '-'}</code> },
    { label: '来源', render: (item) => item.source || '-' },
    { label: '调用', render: (item) => fmtNumber(item.calls) },
    { label: '输入 Token', render: (item) => fmtNumber(item.input_tokens) },
    { label: '输出 Token', render: (item) => fmtNumber(item.output_tokens) },
    { label: '缓存命中', render: (item) => fmtNumber(item.cached_tokens) },
  ]
  if (detail === 'jobs') return [
    { label: '更新时间', render: (item) => fmtTime(item.updated_at) },
    { label: '任务', render: (item) => <code>{item.handle}</code> },
    { label: '类型', render: (item) => item.kind },
    { label: '范围', render: (item) => <code>{item.scope_key || '-'}</code> },
    { label: '状态', render: (item) => <StatusBadge value={item.status} /> },
    { label: '尝试', render: (item) => `${item.attempts}/${item.max_attempts}` },
    { label: '错误', className: 'detail-message', render: (item) => item.last_error || '-' },
    { label: '', className: 'actions', render: (item) => <><button className="icon-button" title="重试任务" onClick={() => void plane.mutate('jobs', `/jobs/${item.job_id}/retry`, 'POST', {}, ['jobs'])}><RotateCcw size={15} /></button><button className="icon-button danger" title="取消任务" onClick={() => void plane.mutate('jobs', `/jobs/${item.job_id}/cancel`, 'POST', {}, ['jobs'])}><Ban size={15} /></button></> },
  ]
  if (detail === 'deliveries') return [
    { label: '更新时间', render: (item) => fmtTime(item.updated_at) },
    { label: '投递', render: (item) => <code>{item.handle ?? `delivery#${item.delivery_id}`}</code> },
    { label: '目标', render: (item) => item.scope_key ?? item.conversation_id ?? `${item.target_kind}:${item.target_native_conversation_id}` },
    { label: '状态', render: (item) => <StatusBadge value={item.status} /> },
    { label: '尝试', render: (item) => fmtNumber(item.attempts) },
    { label: '平台消息', render: (item) => item.native_message_id || '-' },
    { label: '错误', className: 'detail-message', render: (item) => item.last_error || '-' },
    { label: '', className: 'actions', render: (item) => <><button className="icon-button" title="重试投递" onClick={() => void plane.mutate('deliveries', `/deliveries/${item.delivery_id}/retry`, 'POST', {}, ['deliveries'])}><Play size={15} /></button><button className="icon-button danger" title="取消投递" onClick={() => void plane.mutate('deliveries', `/deliveries/${item.delivery_id}/cancel`, 'POST', {}, ['deliveries'])}><X size={15} /></button></> },
  ]
  if (detail === 'traces') return [
    { label: '开始时间', render: (item) => fmtTime(item.started_at) },
    { label: 'Trace ID', render: (item) => <code>{item.trace_id || '-'}</code> },
    { label: '回合', render: (item) => item.turn_handle },
    { label: '模型路由', render: (item) => <ModelRouteSummary trace={item} detailed /> },
    { label: '状态', render: (item) => <StatusBadge value={item.status} /> },
    { label: '耗时', render: (item) => fmtDuration(item.duration_seconds) },
    { label: '工具', render: (item) => `${item.tool_call_count} / ${item.tool_failures} 失败` },
    { label: 'Token', render: (item) => <>{fmtNumber(item.total_tokens)}<small className="cell-sub">输入 {fmtNumber(item.input_tokens)} · 输出 {fmtNumber(item.output_tokens)}</small></> },
  ]
  if (detail === 'media-items') return [
    { label: '最后出现', render: (item) => fmtTime(item.last_seen_at) },
    { label: '媒体', render: (item) => <><code>media#{item.media_id}</code><small className="cell-sub">{fmtBytes(item.byte_size)}</small></> },
    { label: '标签', render: (item) => <><strong>{item.summary || '未命名'}</strong><div className="capabilities">{jsonRows(item.emotions_json).map((tag) => <span key={tag}>{tag}</span>)}</div></> },
    { label: '识图模型', render: (item) => item.vision_model || '-' },
    { label: '安全', render: (item) => <StatusBadge value={item.safety} /> },
    { label: '发送', render: (item) => item.enabled && !item.banned ? `已启用 · ${item.times_sent} 次` : item.banned ? '已拒绝' : '未启用' },
    { label: '审核', className: 'actions', render: (item) => <><button className="icon-button success" title="批准并允许发送" onClick={() => void plane.mutate('media', `/media/${item.media_id}/review`, 'PUT', { state: 'approved' }, ['media', 'stickers'])}><Check size={15} /></button><button className="icon-button" title="保留待审" onClick={() => void plane.mutate('media', `/media/${item.media_id}/review`, 'PUT', { state: 'pending' }, ['media', 'stickers'])}><Clock3 size={15} /></button><button className="icon-button danger" title="拒绝并禁止发送" onClick={() => void plane.mutate('media', `/media/${item.media_id}/review`, 'PUT', { state: 'rejected' }, ['media', 'stickers'])}><X size={15} /></button></> },
  ]
  if (detail === 'sources') return [
    { label: '最后出现', render: (item) => fmtTime(item.last_seen_at ?? item.fetched_at) },
    { label: '平台', render: (item) => item.platform },
    { label: '标题', render: (item) => <a className="record-link" href={item.canonical_url} target="_blank" rel="noreferrer">{item.title || item.canonical_url}<ExternalLink size={13} /></a> },
    { label: '作者', render: (item) => item.author || '-' },
    { label: '类型', render: (item) => item.content_kind },
    { label: '状态', render: (item) => <StatusBadge value={item.status} /> },
    { label: '出现次数', render: (item) => fmtNumber(item.occurrences) },
    { label: '错误', className: 'detail-message', render: (item) => item.last_error || '-' },
  ]
  return [
    { label: '时间', render: (item) => fmtTime(item.created_at) },
    { label: '资源版本', render: (item) => <code>{item.resource_key}@{item.resource_version}</code> },
    { label: '动作', render: (item) => item.action },
    { label: '目标', render: (item) => item.target || '-' },
    { label: '操作者', render: (item) => item.actor },
    { label: '结果', render: (item) => <StatusBadge value={item.status} /> },
  ]
}

export function DetailView({ detail, plane, onBack }: { detail: DetailViewId; plane: Plane; onBack: () => void }) {
  const meta = DETAIL_META[detail]
  const source = DETAIL_SOURCES[detail]
  const [payload, setPayload] = useState<Record<string, any> | null>(null)
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const sourceVersion = plane.data[source.resource]

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true)
    try {
      setPayload(await plane.query(source.path, signal))
      setLoadError('')
    } catch (reason) {
      if (!signal?.aborted) setLoadError(reason instanceof Error ? reason.message : '详细记录加载失败')
    } finally {
      if (!signal?.aborted) setLoading(false)
    }
  }, [plane.query, source.path])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load, sourceVersion])

  useEffect(() => {
    setPage(1)
    setQuery('')
  }, [detail])

  const items = useMemo(() => {
    const extracted = source.extract(payload ?? plane.data[source.resource] ?? {})
    return [...extracted].sort((left, right) => timestamp(itemTime(detail, right)) - timestamp(itemTime(detail, left)))
  }, [detail, payload, plane.data, source])
  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase('zh-CN')
    if (!needle) return items
    return items.filter((item) => JSON.stringify(item).toLocaleLowerCase('zh-CN').includes(needle))
  }, [items, query])
  const pageCount = Math.max(Math.ceil(filtered.length / pageSize), 1)
  const currentPage = Math.min(page, pageCount)
  const visible = filtered.slice((currentPage - 1) * pageSize, currentPage * pageSize)
  const columns = detailColumns(detail, plane)

  return (
    <div className="detail-page">
      <div className="detail-toolbar">
        <button className="back-button" type="button" onClick={onBack}><ArrowLeft size={15} />返回{meta.parentLabel}</button>
        <div className="detail-total"><strong>{fmtNumber(items.length)}</strong><span>条已载入记录</span></div>
        <RefreshButton loading={loading} onClick={() => void load()} />
      </div>
      <div className="detail-filters">
        <label className="search-field"><Search size={15} /><input type="search" placeholder="搜索当前详细记录" value={query} onChange={(event) => { setQuery(event.target.value); setPage(1) }} /></label>
        <label className="page-size"><span>每页</span><select value={pageSize} onChange={(event) => { setPageSize(Number(event.target.value)); setPage(1) }}><option value={20}>20 条</option><option value={50}>50 条</option><option value={100}>100 条</option></select></label>
      </div>
      {loadError && <div className="inline-error">{loadError}</div>}
      <DataTable>
        <thead><tr>{columns.map((column, index) => <th key={`${column.label}-${index}`}>{column.label}</th>)}</tr></thead>
        <tbody>{visible.map((item, index) => <tr key={rowKey(detail, item, index)}>{columns.map((column, columnIndex) => <td className={column.className} key={`${column.label}-${columnIndex}`}>{column.render(item)}</td>)}</tr>)}</tbody>
      </DataTable>
      {!visible.length && <EmptyState>没有符合条件的记录</EmptyState>}
      <div className="pagination">
        <span>第 {currentPage} / {pageCount} 页 · 共 {fmtNumber(filtered.length)} 条</span>
        <div><button className="icon-button" title="上一页" disabled={currentPage <= 1} onClick={() => setPage(currentPage - 1)}><ChevronLeft size={16} /></button><button className="icon-button" title="下一页" disabled={currentPage >= pageCount} onClick={() => setPage(currentPage + 1)}><ChevronRight size={16} /></button></div>
      </div>
    </div>
  )
}
