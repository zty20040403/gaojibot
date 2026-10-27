import { useEffect, useRef, useState } from 'react'
import { ArrowLeft, ArrowRight, Check, Search } from 'lucide-react'
import { EmptyState, Metric, RefreshButton, Section, StatusBadge, Toggle, fmtTime } from './components'
import type { JsonObject } from './api'
import type { useControlPlane } from './useControlPlane'
import './wiki.css'

const categories: Record<string, string> = { decision: '决定', faq: '问答', jargon: '术语', expertise: '擅长领域' }
const pageSize = 5

export function WikiView({ plane }: { plane: ReturnType<typeof useControlPlane> }) {
  const summary = plane.data.wiki
  const [group, setGroup] = useState('')
  const [groupInput, setGroupInput] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [query, setQuery] = useState('')
  const [offset, setOffset] = useState(0)
  const [detail, setDetail] = useState<JsonObject | null>(null)
  const [draft, setDraft] = useState<JsonObject | null>(null)
  const dirty = useRef(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [reload, setReload] = useState(0)

  function selectGroup(value: string) {
    if (saving) return
    if (!/^[1-9][0-9]{0,19}$/.test(value)) { setError('请输入有效群号'); return }
    dirty.current = false
    setGroup(value); setGroupInput(value); setDetail(null); setDraft(null)
    setSearchInput(''); setQuery(''); setOffset(0); setError('')
    setReload((value) => value + 1)
  }

  useEffect(() => {
    if (!group || !summary?.configured) return
    const controller = new AbortController()
    setLoading(true)
    void plane.query(`/wiki/${group}?query=${encodeURIComponent(query)}&offset=${offset}&limit=${pageSize}`, controller.signal)
      .then((value) => {
        if (controller.signal.aborted) return
        setDetail(value)
        if (!dirty.current) setDraft(value.settings)
        setError('')
      })
      .catch((reason) => { if (!controller.signal.aborted) setError(String(reason)) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [group, query, offset, summary, plane.query, reload])

  function edit(values: JsonObject) {
    dirty.current = true
    setDraft((current) => ({ ...current, ...values }))
  }

  async function save() {
    if (!draft || saving) return
    setSaving(true)
    try {
      const result = await plane.mutate('wiki', `/wiki/${group}/settings`, 'PUT', {
        bot_id: draft.bot_id, enabled: draft.enabled, newcomer_enabled: draft.newcomer_enabled,
        expected_version: draft.version,
      }, ['wiki', 'jobs'])
      dirty.current = false
      setDraft(result.settings)
      setReload((value) => value + 1)
    } catch (reason) { setError(String(reason)) } finally { setSaving(false) }
  }

  if (!summary) return <EmptyState>正在加载群百科…</EmptyState>
  if (!summary.configured) return <EmptyState>群百科未启用</EmptyState>

  const sameGroup = detail?.scope_key === `onebot-v11:group:${group}`
  const counts = sameGroup ? detail.jobs?.counts ?? {} : {}
  return <div className="wiki-view">
    <Section title="群百科" action={<RefreshButton loading={loading} onClick={() => void plane.refresh('wiki')} />}>
      <div className="wiki-filters">
        <label>已配置群<select aria-label="已配置群" disabled={saving} value={summary.groups.some((item: JsonObject) => item.scope_key.endsWith(`:${group}`)) ? group : ''}
          onChange={(event) => event.target.value && selectGroup(event.target.value)}>
          <option value="">选择群</option>
          {summary.groups.map((item: JsonObject) => <option key={item.scope_key} value={item.scope_key.split(':')[2]}>
            {item.scope_key.split(':')[2]} · {item.enabled ? '已开启' : '已关闭'}
          </option>)}
        </select></label>
        <form onSubmit={(event) => { event.preventDefault(); selectGroup(groupInput.trim()) }}>
          <label>群号<input aria-label="群号" inputMode="numeric" disabled={saving} value={groupInput} onChange={(event) => setGroupInput(event.target.value)} /></label>
          <button className="icon-button" title="打开群百科" type="submit" disabled={saving}><ArrowRight size={17} /></button>
        </form>
        <span className="wiki-schedule">夜间整理 {String(summary.hour).padStart(2, '0')}:00 · {summary.timezone}</span>
      </div>
    </Section>
    {error && <p className="wiki-error" role="alert">{error}</p>}
    {!group && <EmptyState>尚未选择群</EmptyState>}
    {group && draft && <Section title={`群 ${group}`} action={<button className="text-button" disabled={!dirty.current || saving || !/^[1-9][0-9]{0,19}$/.test(draft.bot_id)} onClick={() => void save()}><Check size={16} />{saving ? '保存中' : '保存配置'}</button>}>
      <div className="wiki-settings">
        <label>发送账号 QQ<input aria-label="发送账号 QQ" value={draft.bot_id} inputMode="numeric" onChange={(event) => edit({ bot_id: event.target.value })} disabled={saving} /></label>
        <Toggle checked={Boolean(draft.enabled)} disabled={saving} label="群百科" onChange={(enabled) => edit({ enabled })} />
        <Toggle checked={Boolean(draft.newcomer_enabled)} disabled={saving || !draft.enabled} label="新人私聊简报" onChange={(newcomer_enabled) => edit({ newcomer_enabled })} />
        <span className="wiki-schedule">配置 v{draft.version}</span>
      </div>
      {dirty.current && detail?.settings.version !== draft.version && <p className="wiki-error">配置已被修改，本地编辑尚未保存。<button className="text-button" onClick={() => { dirty.current = false; setDraft(detail?.settings) }}>载入最新配置</button></p>}
    </Section>}
    {sameGroup && <>
      <div className="wiki-metrics">
        <Metric label="当前条目" value={detail.total} />
        <Metric label="来源章节" value={detail.source_chapters} />
        <Metric label="已隐藏的失效条目" value={detail.stale_entries} />
        <Metric label="百科版本" value={`v${detail.revision}`} hint={detail.generated_at ? fmtTime(detail.generated_at) : '尚未生成'} />
      </div>
      <Section title="知识条目" action={<form className="wiki-search" onSubmit={(event) => { event.preventDefault(); setQuery(searchInput.trim()); setOffset(0) }}>
        <input aria-label="搜索群百科" maxLength={200} placeholder="搜索关键词" value={searchInput} onChange={(event) => setSearchInput(event.target.value)} />
        <button className="icon-button" title="搜索" type="submit"><Search size={16} /></button>
      </form>}>
        {!detail.items.length && <EmptyState>{draft?.enabled ? '暂无匹配条目' : '本群百科已关闭'}</EmptyState>}
        {detail.items.map((item: JsonObject) => <article className="wiki-entry" key={`${item.category}:${item.title}:${item.subject_principal_id ?? ''}`}>
          <header><StatusBadge value={item.category} label={categories[item.category]} /><h3>{item.title}</h3></header>
          <p>{item.content}</p>
          <footer>依据 {item.evidence_ids.map((id: number) => `msg#${id}`).join(' · ')}{item.subject_principal_id ? ` · ${item.subject_principal_id}` : ''}</footer>
        </article>)}
        <div className="pagination"><span>{detail.total ? `${offset + 1}–${Math.min(offset + pageSize, detail.total)} / ${detail.total}` : '0 条'}</span><div>
          <button className="icon-button" title="上一页" disabled={!offset || loading} onClick={() => setOffset((value) => Math.max(0, value - pageSize))}><ArrowLeft size={16} /></button>
          <button className="icon-button" title="下一页" disabled={offset + pageSize >= detail.total || loading} onClick={() => setOffset((value) => value + pageSize)}><ArrowRight size={16} /></button>
        </div></div>
      </Section>
      <Section title="最近整理与简报任务" action={<a className="text-button" href="#tasks">全部任务<ArrowRight size={16} /></a>}>
        <div className="wiki-job-counts"><span>待执行 {counts.pending ?? 0}</span><span>运行 {counts.running ?? 0}</span><span>失败 {counts.failed ?? 0}</span></div>
        {!detail.jobs.items.length && <EmptyState>暂无任务</EmptyState>}
        {detail.jobs.items.map((job: JsonObject) => <div className="wiki-job" key={job.job_id}>
          <span>job#{job.job_id}</span><span>{job.kind.endsWith('_newcomer') ? '新人简报' : '夜间整理'}</span>
          <StatusBadge value={job.status} /><span>尝试 {job.attempts}/{job.max_attempts}</span><time>{fmtTime(job.updated_at)}</time>
        </div>)}
      </Section>
    </>}
  </div>
}
