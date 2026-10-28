import { useEffect, useRef, useState } from 'react'
import { ArrowLeft, ArrowRight, Ban, Check, Pin, PinOff, Search } from 'lucide-react'
import { EmptyState, Metric, RefreshButton, Section, StatusBadge, fmtBytes, fmtTime } from './components'
import type { JsonObject } from './api'
import type { useControlPlane } from './useControlPlane'
import './projects.css'

const states: Record<string, string> = { active: '运行中', stopped: '已停止', hibernated: '已休眠', archived: '已归档', trash: '回收站' }
const sections = { versions: '版本历史', previews: '预览记录', events: '操作记录' }
const pageSize = 5
const MiB = 1024 ** 2

function Pager({ total, offset, loading, setOffset }: { total: number; offset: number; loading: boolean; setOffset: (value: number) => void }) {
  return <div className="pagination"><span>{total ? `${Math.min(offset + 1, total)}–${Math.min(offset + pageSize, total)} / ${total}` : '0 条'}</span><div>
    <button className="icon-button" title="上一页" disabled={!offset || loading} onClick={() => setOffset(Math.max(0, offset - pageSize))}><ArrowLeft size={16} /></button>
    <button className="icon-button" title="下一页" disabled={offset + pageSize >= total || loading} onClick={() => setOffset(offset + pageSize)}><ArrowRight size={16} /></button>
  </div></div>
}

function Usage({ item }: { item: JsonObject }) {
  return <div className="project-usage">
    <strong>{item.usage_fresh ? fmtBytes(item.used_bytes) : '未核实'} <span>/ {fmtBytes(item.quota_bytes)}</span></strong>
    {item.usage_fresh ? <progress aria-label="工作区使用比例" max={item.quota_bytes} value={item.used_bytes} /> : <span className="project-muted">{item.sampled_at ? `上次 ${fmtBytes(item.last_known_used_bytes)}` : '尚未采样'}</span>}
    <small>{item.quota_mode === 'hard' ? '硬配额' : '软配额'} · {item.sampled_at ? fmtTime(item.sampled_at) : '无采样时间'}</small>
  </div>
}

export function ProjectsView({ plane }: { plane: ReturnType<typeof useControlPlane> }) {
  const summary = plane.data.projects
  const [selected, setSelected] = useState<number | null>(null)
  const [state, setState] = useState('')
  const [input, setInput] = useState('')
  const [query, setQuery] = useState('')
  const [offset, setOffset] = useState(0)
  const [section, setSection] = useState<keyof typeof sections>('versions')
  const [detailOffset, setDetailOffset] = useState(0)
  const [page, setPage] = useState<JsonObject | null>(null)
  const [detail, setDetail] = useState<JsonObject | null>(null)
  const [draft, setDraft] = useState<JsonObject | null>(null)
  const dirty = useRef(new Set<string>())
  const [busy, setBusy] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [reload, setReload] = useState(0)

  function loadDraft(value: JsonObject) {
    dirty.current.clear()
    setDraft({ project_id: value.project.project_id, version: value.project.version,
      quota: String(value.project.quota_bytes / MiB), scopeQuota: String(value.scope_limits.quota_bytes / MiB),
      maxProjects: String(value.scope_limits.max_projects), scopeVersion: value.scope_limits.version })
  }

  useEffect(() => {
    if (!summary?.available) return
    const controller = new AbortController()
    setLoading(true)
    const path = selected === null
      ? `/projects?query=${encodeURIComponent(query)}&state=${state}&offset=${offset}&limit=${pageSize}`
      : `/projects/${selected}?section=${section}&offset=${detailOffset}&limit=${pageSize}`
    void plane.query(path, controller.signal).then((value) => {
      if (controller.signal.aborted) return
      if (selected === null) setPage(value)
      else { setDetail(value); if (!dirty.current.size) loadDraft(value) }
      setError('')
    }).catch((reason) => { if (!controller.signal.aborted) setError(String(reason)) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [summary, selected, section, detailOffset, query, state, offset, plane.query, reload])

  function select(id: number | null) {
    if (busy) return
    setSelected(id); setDetail(null); setDraft(null); dirty.current.clear()
    setSection('versions'); setDetailOffset(0); setError('')
  }

  function edit(key: string, value: string) { dirty.current.add(key); setDraft((current) => ({ ...current, [key]: value })) }

  async function change(key: string, path: string, body: JsonObject, method: 'PUT' | 'POST' = 'PUT', savedDraft?: 'quota' | 'scope') {
    if (busy) return
    setBusy(key); setError('')
    try {
      const result = await plane.mutate('projects', path, method, body, ['projects'])
      if (savedDraft) {
        for (const field of savedDraft === 'quota' ? ['quota'] : ['scopeQuota', 'maxProjects']) dirty.current.delete(field)
        setDraft((current) => ({ ...current, version: result.change.version,
          scopeVersion: result.change.scope_version ?? current?.scopeVersion }))
      }
      setReload((value) => value + 1)
    } catch (reason) { setError(String(reason)) } finally { setBusy('') }
  }

  if (!summary) return <EmptyState>正在加载项目…</EmptyState>
  if (!summary.configured) return <EmptyState>项目功能未开启</EmptyState>
  if (!summary.available) return <EmptyState>项目运行环境尚未就绪</EmptyState>

  const project = detail?.project.project_id === selected ? detail.project : null
  const changed = Boolean(dirty.current.size && project && draft &&
    (draft.version !== project.version || draft.scopeVersion !== detail?.scope_limits.version))
  const validQuota = (value: string) => /^\d+$/.test(value) && Number(value) >= 64 && Number(value) <= 100 * 1024 ** 2

  return <div className="projects-view" aria-busy={loading}>
    {error && <p className="project-error" role="alert">{error}</p>}
    {selected === null ? <>
      <div className="project-metrics">
        <Metric label="全部项目" value={Object.values(summary.counts ?? {}).reduce<number>((sum, n) => sum + Number(n), 0)} />
        <Metric label="运行中" value={summary.counts?.active ?? 0} />
        <Metric label="休眠 / 归档" value={`${summary.counts?.hibernated ?? 0} / ${summary.counts?.archived ?? 0}`} />
      </div>
      <Section title="项目空间" action={<RefreshButton loading={loading} onClick={() => void plane.refresh('projects')} />}>
        <div className="project-filters">
          <form onSubmit={(event) => { event.preventDefault(); setQuery(input.trim()); setOffset(0) }}>
            <input aria-label="搜索项目" placeholder="名称、会话或 prj#" maxLength={200} value={input} onChange={(event) => setInput(event.target.value)} />
            <button className="icon-button" title="搜索项目" type="submit"><Search size={16} /></button>
          </form>
          <label>状态<select aria-label="项目状态" value={state} onChange={(event) => { setState(event.target.value); setOffset(0) }}>
            <option value="">全部状态</option>{Object.entries(states).map(([value, name]) => <option key={value} value={value}>{name}</option>)}
          </select></label>
        </div>
        {!page?.items.length && <EmptyState>{loading ? '正在查询…' : '暂无匹配项目'}</EmptyState>}
        {page?.items.map((item: JsonObject) => <article className="project-row" key={item.project_id}>
          <div className="project-name"><strong>{item.title}</strong><span>prj#{item.project_id} · {item.backend.toUpperCase()}{item.pinned ? ' · 已固定' : ''}</span><small>{item.scope_key}</small></div>
          <Usage item={item} />
          <div className="project-row-status"><StatusBadge value={item.status} label={states[item.status]} /><small>{item.lifecycle_job_id ? `job#${item.lifecycle_job_id}` : item.locked_task_id ? `task#${item.locked_task_id}` : '空闲'}</small></div>
          <button className="icon-button" title={`打开 prj#${item.project_id}`} onClick={() => select(item.project_id)}><ArrowRight size={17} /></button>
        </article>)}
        <Pager total={page?.total ?? 0} offset={offset} loading={loading} setOffset={setOffset} />
      </Section>
    </> : <>
      <div className="project-detail-nav"><button className="text-button" disabled={Boolean(busy)} onClick={() => select(null)}><ArrowLeft size={16} />全部项目</button><RefreshButton loading={loading} onClick={() => setReload((value) => value + 1)} /></div>
      {!project && <EmptyState>{loading ? '正在加载项目…' : '项目暂不可用'}</EmptyState>}
      {project && detail && draft && <>
        <Section title={project.title} action={<button className="icon-button" title={project.pinned ? '取消固定项目' : '固定项目'} disabled={Boolean(busy) || Boolean(project.lifecycle_job_id || project.locked_task_id)}
          onClick={() => void change('pin', `/projects/${selected}/pin`, { expected_version: project.version, pinned: !project.pinned })}>{project.pinned ? <PinOff size={17} /> : <Pin size={17} />}</button>}>
          <div className="project-meta"><span>prj#{selected}</span><StatusBadge value={project.status} label={states[project.status]} /><span>{project.backend.toUpperCase()}</span><span>配置 v{project.version}</span><span>{project.pinned ? '已固定' : '未固定'}</span><span>{project.scope_key}</span></div>
          <div className="project-metrics"><Usage item={project} /><Metric label="可重建数据" value={project.usage_fresh ? fmtBytes(project.rebuildable_bytes) : '未核实'} /><Metric label="版本库" value={fmtBytes(project.version_bytes)} /></div>
          {detail.operation && <div className="project-operation"><StatusBadge value={detail.operation.job_status} /><span>job#{detail.operation.job_id} · {detail.operation.action}</span><span>{detail.operation.phase === 'browser_drain' ? '正在确认浏览器关闭' : `${detail.operation.completed_steps}/${detail.operation.total_steps} 步`}</span><time>{fmtTime(detail.operation.updated_at)}</time></div>}
          {project.locked_task_id && <p>执行任务 task#{project.locked_task_id}</p>}
        </Section>
        <Section title="容量配置">
          {changed && <p className="project-error">配置已变更，未覆盖你的输入。<button className="text-button" disabled={Boolean(busy)} onClick={() => loadDraft(detail)}>载入最新配置</button></p>}
          <div className="project-capacity">
            <label>项目配额（MiB）<input aria-label="项目配额 MiB" type="number" min="64" step="1" value={draft.quota} disabled={!project.quota_editable || Boolean(busy)} onChange={(event) => edit('quota', event.target.value)} /></label>
            <button className="text-button" disabled={!project.quota_editable || Boolean(busy) || changed || !validQuota(draft.quota)} onClick={() => void change('quota', `/projects/${selected}/quota`, { expected_version: draft.version, quota_bytes: Number(draft.quota) * MiB }, 'PUT', 'quota')}><Check size={16} />保存项目配额</button>
            {!project.quota_editable && <span className="project-muted">已有工作区，磁盘扩缩容入口尚未开放</span>}
          </div>
          <div className="project-capacity">
            <label>会话总额度（MiB）<input aria-label="会话总额度 MiB" type="number" min="64" step="1" disabled={Boolean(busy)} value={draft.scopeQuota} onChange={(event) => edit('scopeQuota', event.target.value)} /></label>
            <label>最多项目数<input aria-label="最多项目数" type="number" min="1" max="1000" step="1" disabled={Boolean(busy)} value={draft.maxProjects} onChange={(event) => edit('maxProjects', event.target.value)} /></label>
            <button className="text-button" disabled={Boolean(busy) || changed || !validQuota(draft.scopeQuota) || !/^\d+$/.test(draft.maxProjects) || Number(draft.maxProjects) < 1 || Number(draft.maxProjects) > 1000}
              onClick={() => void change('scope', `/projects/${selected}/scope-limits`, { expected_version: draft.version, scope_version: draft.scopeVersion, quota_bytes: Number(draft.scopeQuota) * MiB, max_projects: Number(draft.maxProjects) }, 'PUT', 'scope')}><Check size={16} />保存会话额度</button>
          </div>
          <div className="project-meta"><span>最近计量 {fmtBytes(detail.scope_limits.accounted_bytes)}</span><span>预留 {fmtBytes(detail.scope_limits.reserved_bytes)}</span><span>项目 {detail.scope_limits.project_count}/{detail.scope_limits.max_projects}</span><span>会话配置 v{detail.scope_limits.version}</span></div>
        </Section>
        <Section title={sections[section]}>
          <div className="project-tabs" role="tablist" aria-label="项目详情">
            {Object.entries(sections).map(([key, title]) => <button key={key} type="button" role="tab" aria-selected={section === key} disabled={Boolean(busy)} onClick={() => { setSection(key as keyof typeof sections); setDetailOffset(0) }}>{title}</button>)}
          </div>
          {detail.section === section && <>
            {!detail.items.length && <EmptyState>暂无记录</EmptyState>}
            {detail.items.map((item: JsonObject) => <div className="project-record" key={item.version_no && section === 'versions' ? item.version_no : item.preview_id ?? item.event_id}>
              {section === 'versions' ? <><div><strong>v{item.version_no} {item.label}</strong><span>{item.file_count} 个文件 · {fmtBytes(item.total_bytes)}{item.pinned ? ' · 已固定' : ''}</span></div>
                <time>{fmtTime(item.created_at)}</time><button className="icon-button" title={`${item.pinned ? '取消固定' : '固定'} v${item.version_no}`} disabled={Boolean(busy)} onClick={() => void change('version', `/projects/${selected}/versions/${item.version_no}/pin`, { expected_version: project.version, pinned: !item.pinned })}>{item.pinned ? <PinOff size={16} /> : <Pin size={16} />}</button></>
              : section === 'previews' ? <><div><strong>{item.root} · v{item.version_no}</strong><small>{item.preview_id}</small><span>{item.revoked_at ? `已撤销 ${fmtTime(item.revoked_at)}` : `到期 ${fmtTime(item.expires_at)}`}</span></div>
                <time>{fmtTime(item.created_at)}</time><button className="icon-button" title={`撤销 ${item.preview_id}`} disabled={Boolean(busy) || Boolean(item.revoked_at)} onClick={() => void change('revoke', `/projects/${selected}/previews/${item.preview_id}/revoke`, { expected_version: project.version }, 'POST')}><Ban size={16} /></button></>
              : <><div><strong>{item.kind}</strong><span>{item.actor_principal_id ? `用户 #${item.actor_principal_id}` : '系统 / 管理员'}</span></div><time>{fmtTime(item.created_at)}</time></>}
            </div>)}
            <Pager total={detail.total} offset={detailOffset} loading={loading} setOffset={setDetailOffset} />
          </>}
        </Section>
      </>}
    </>}
  </div>
}
