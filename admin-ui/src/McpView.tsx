import { useEffect, useRef, useState } from 'react'
import { ArrowLeft, ArrowRight, Ban, Check, Copy, Download, Plus, X } from 'lucide-react'
import { EmptyState, RefreshButton, Section, StatusBadge, Toggle, fmtTime } from './components'
import type { JsonObject } from './api'
import type { useControlPlane } from './useControlPlane'
import './mcp.css'

type Plane = ReturnType<typeof useControlPlane>
type ServerDraft = { name: string; transport: string; endpoint: string; credential: string; profile: string; groups: string; enabled: boolean; version: number }
const pageSize = 5
const scopePrefix = 'onebot-v11:group:'
const callLabels: Record<string, string> = { waiting: '等待审批', ready: '待执行', connecting: '连接中', dispatched: '已提交，等待结果', succeeded: '已成功', failed: '失败', needs_attention: '结果待核实', cancelled: '已取消' }
const callTones: Record<string, string> = { waiting: 'pending', ready: 'pending', connecting: 'running', dispatched: 'warning', needs_attention: 'warning' }

function serverDraft(server?: JsonObject): ServerDraft {
  return { name: server?.name ?? '', transport: server?.config.transport ?? 'http', endpoint: server?.config.url ?? '',
    credential: server?.config.credential_ref ?? '', profile: server?.config.profile ?? '',
    groups: server?.scopes.map((scope: string) => scope.replace(scopePrefix, '')).join('\n') ?? '', enabled: Boolean(server?.enabled), version: server?.version ?? 0 }
}

function Pages({ offset, total, setOffset, disabled = false }: { offset: number; total: number; setOffset: (value: number) => void; disabled?: boolean }) {
  return <div className="pagination"><span>{total ? `${offset + 1}–${Math.min(offset + pageSize, total)} / ${total}` : '0 条'}</span><div>
    <button className="icon-button" title="上一页" disabled={disabled || !offset} onClick={() => setOffset(Math.max(0, offset - pageSize))}><ArrowLeft size={16} /></button>
    <button className="icon-button" title="下一页" disabled={disabled || offset + pageSize >= total} onClick={() => setOffset(offset + pageSize)}><ArrowRight size={16} /></button>
  </div></div>
}

function ToolEditor({ tool, server, plane, onSaved }: { tool: JsonObject; server: JsonObject; plane: Plane; onSaved: () => void }) {
  const [draft, setDraft] = useState(() => ({ enabled: Boolean(tool.enabled), trust: String(tool.trust), version: tool.version, serverVersion: server.version, fingerprint: tool.fingerprint }))
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const changed = draft.version !== tool.version || draft.serverVersion !== server.version
  function reset() {
    setDraft({ enabled: Boolean(tool.enabled), trust: String(tool.trust), version: tool.version, serverVersion: server.version, fingerprint: tool.fingerprint })
    setDirty(false); setError('')
  }
  useEffect(() => { if (!dirty && !saving) reset() }, [tool, server, dirty, saving])
  async function save() {
    setSaving(true); setError('')
    try {
      await plane.mutate('mcp', `/mcp/servers/${encodeURIComponent(server.name)}/tools/${encodeURIComponent(tool.remote_name)}`, 'PUT', {
        enabled: draft.enabled, trust: draft.trust, expected_version: draft.version, server_version: draft.serverVersion, tool_fingerprint: draft.fingerprint,
      }, ['mcp'])
      setDirty(false); onSaved()
    } catch (reason) { setError(String(reason)) } finally { setSaving(false) }
  }
  return <article className="mcp-tool">
    <header><code>{tool.name}</code><StatusBadge value={tool.present ? 'ready' : 'offline'} label={tool.present ? '目录中存在' : '已从目录移除'} /></header>
    <div className="mcp-tool-controls">
      <Toggle checked={draft.enabled} label="启用工具" disabled={saving || !tool.present} onChange={(enabled) => { setDirty(true); setDraft({ ...draft, enabled }) }} />
      <label>审批策略<select aria-label={`${tool.remote_name} 审批策略`} value={draft.trust} disabled={saving || !tool.present} onChange={(event) => { setDirty(true); setDraft({ ...draft, trust: event.target.value }) }}>
        <option value="approval">每次独立双人审批</option><option value="safe">管理员已审查，允许免审</option>
      </select></label>
      <button className="icon-button" title={`保存 ${tool.remote_name}`} disabled={!dirty || saving || changed || !tool.present} onClick={() => void save()}><Check size={16} /></button>
      <span className="mcp-meta">v{tool.version}</span>
    </div>
    {changed && dirty && <p className="mcp-error">目录或配置已改变，尚未保存。<button className="text-button" disabled={saving} onClick={reset}>载入最新配置</button></p>}
    {error && <p className="mcp-error" role="alert">{error}</p>}
    <details><summary>工具说明与参数</summary><p className="mcp-quoted">{tool.definition.description || '无说明'}</p><pre>{JSON.stringify(tool.definition.inputSchema, null, 2)}</pre></details>
  </article>
}

function ServerEditor({ selected, plane, onCreated }: { selected: string; plane: Plane; onCreated: (name: string) => void }) {
  const summary = plane.data.mcp!
  const [detail, setDetail] = useState<JsonObject | null>(null)
  const [draft, setDraft] = useState<ServerDraft>(() => serverDraft())
  const dirty = useRef(false)
  const [saving, setSaving] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [reload, setReload] = useState(0)
  const [toolOffset, setToolOffset] = useState(0)
  // The editor is keyed only by selected server, never by the live resource version.
  useEffect(() => {
    if (!selected) return
    const controller = new AbortController()
    setLoading(true)
    void plane.query(`/mcp/servers/${encodeURIComponent(selected)}`, controller.signal).then((value) => {
      if (controller.signal.aborted) return
      setDetail(value)
      if (!dirty.current) setDraft(serverDraft(value.server))
    }).catch((reason) => { if (!controller.signal.aborted) setError(String(reason)) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [selected, summary, plane.query, reload])
  function edit(values: Partial<ServerDraft>) { dirty.current = true; setDraft((current) => ({ ...current, ...values })) }
  async function save() {
    const groups = [...new Set(draft.groups.trim().split(/[\s,，]+/))]
    if (!groups.length || groups.some((group) => !/^[1-9][0-9]{0,19}$/.test(group))) { setError('请输入有效群号，以换行或逗号分隔'); return }
    setSaving(true); setError('')
    try {
      const config = draft.transport === 'http'
        ? { transport: 'http', url: draft.endpoint.trim(), ...(draft.credential ? { credential_ref: draft.credential } : {}) }
        : { transport: 'stdio', profile: draft.profile }
      const result = await plane.mutate('mcp', `/mcp/servers/${encodeURIComponent(draft.name)}`, 'PUT', {
        config, scopes: groups.map((group) => scopePrefix + group), enabled: draft.enabled, expected_version: draft.version,
      }, ['mcp'])
      dirty.current = false; setDraft(serverDraft(result.server)); setDetail({ server: result.server, tools: [] }); setReload((n) => n + 1)
      if (!selected) onCreated(result.server.name)
    } catch (reason) { setError(String(reason)) } finally { setSaving(false) }
  }
  async function discover() {
    if (!detail) return
    setSaving(true); setError('')
    try {
      const result = await plane.mutate('mcp', `/mcp/servers/${encodeURIComponent(detail.server.name)}/refresh`, 'POST', { expected_version: detail.server.version }, ['mcp'])
      setDetail(result); setDraft(serverDraft(result.server)); setReload((n) => n + 1)
    } catch (reason) { setError(String(reason)) } finally { setSaving(false) }
  }
  const changed = dirty.current && detail && detail.server.version !== draft.version
  const tools: JsonObject[] = detail?.tools ?? []
  return <>
    <Section title={selected || draft.version ? `服务器 · ${draft.name || selected}` : '登记服务器'} action={<button className="text-button" disabled={!dirty.current || saving || loading || Boolean(changed) || !/^[a-z][a-z0-9-]{0,19}$/.test(draft.name)} onClick={() => void save()}><Check size={16} />{saving ? '处理中' : '保存配置'}</button>}>
      {loading && !detail ? <EmptyState>正在读取服务器…</EmptyState> : <div className="mcp-form">
        <label>名称<input aria-label="MCP 服务器名称" maxLength={20} disabled={saving || Boolean(draft.version)} value={draft.name} onChange={(event) => edit({ name: event.target.value })} /></label>
        <label>连接方式<select aria-label="MCP 连接方式" disabled={saving} value={draft.transport} onChange={(event) => edit({ transport: event.target.value })}><option value="http">HTTP</option><option value="stdio">隔离进程</option></select></label>
        {draft.transport === 'http' ? <>
          <label className="mcp-wide">服务端点<input aria-label="MCP 服务端点" type="url" maxLength={2048} disabled={saving} value={draft.endpoint} onChange={(event) => edit({ endpoint: event.target.value })} /></label>
          <label>凭证引用<select aria-label="MCP 凭证引用" disabled={saving} value={draft.credential} onChange={(event) => edit({ credential: event.target.value })}><option value="">不使用凭证</option>{summary.credential_refs.map((ref: string) => <option key={ref} value={ref}>{ref}</option>)}</select></label>
        </> : <label className="mcp-wide">运营者隔离配置<select aria-label="MCP 隔离配置" disabled={saving} value={draft.profile} onChange={(event) => edit({ profile: event.target.value })}><option value="">选择配置</option>{summary.stdio_profiles.map((profile: string) => <option key={profile} value={profile}>{profile}</option>)}</select></label>}
        <label className="mcp-wide">允许使用的群号<textarea aria-label="MCP 允许群号" rows={3} maxLength={1100} disabled={saving} value={draft.groups} onChange={(event) => edit({ groups: event.target.value })} /></label>
        <Toggle checked={draft.enabled} disabled={saving || !draft.version} label="启用服务器" onChange={(enabled) => edit({ enabled })} />
        <span className="mcp-meta">配置 v{draft.version}</span>
      </div>}
      {changed && <p className="mcp-error">配置已改变，本地编辑尚未保存。<button className="text-button" disabled={saving} onClick={() => { dirty.current = false; setDraft(serverDraft(detail!.server)); setError('') }}>载入最新配置</button></p>}
      {error && <p className="mcp-error" role="alert">{error}</p>}
    </Section>
    {detail && <Section title="外部工具" action={<button className="text-button" disabled={saving || dirty.current} onClick={() => void discover()}><Download size={16} />读取工具目录</button>}>
      {!tools.length && <EmptyState>尚无工具目录</EmptyState>}
      {tools.slice(toolOffset, toolOffset + pageSize).map((tool) => <ToolEditor key={tool.name} tool={tool} server={detail.server} plane={plane} onSaved={() => setReload((n) => n + 1)} />)}
      <Pages offset={toolOffset} total={tools.length} setOffset={setToolOffset} disabled={saving} />
    </Section>}
  </>
}

function Tokens({ plane }: { plane: Plane }) {
  const summary = plane.data.mcp!
  const [offset, setOffset] = useState(0)
  const [form, setForm] = useState({ group: '', bot: '', label: '', lifetime: '86400' })
  const [issued, setIssued] = useState<JsonObject | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)
  const tokens: JsonObject[] = summary.tokens
  async function issue() {
    setBusy(true); setError(''); setCopied(false); setIssued(null)
    try {
      const value = await plane.mutate('mcp', '/mcp/tokens', 'POST', { scope_key: scopePrefix + form.group.trim(), bot_id: form.bot.trim(), label: form.label.trim(), lifetime_seconds: Number(form.lifetime) }, ['mcp'])
      setIssued(value.grant); setOffset(0)
    } catch (reason) { setError(String(reason)) } finally { setBusy(false) }
  }
  async function revoke(token: JsonObject) {
    setBusy(true); setError('')
    try {
      await plane.mutate('mcp', `/mcp/tokens/${encodeURIComponent(token.token_id)}/revoke`, 'POST', { expected_version: token.version }, ['mcp'])
      if (issued?.token_id === token.token_id) setIssued(null)
    } catch (reason) { setError(String(reason)) } finally { setBusy(false) }
  }
  return <Section title="群只读 Token" action={<StatusBadge value={summary.read_ready ? 'ready' : 'offline'} label={summary.read_ready ? summary.read_path : '只读入口未就绪'} />}>
    <form className="mcp-form" onSubmit={(event) => { event.preventDefault(); void issue() }}>
      <label>目标群号<input aria-label="Token 目标群号" required pattern="[1-9][0-9]{0,19}" inputMode="numeric" value={form.group} disabled={busy} onChange={(event) => setForm({ ...form, group: event.target.value })} /></label>
      <label>机器人 QQ<input aria-label="Token 机器人 QQ" required pattern="[1-9][0-9]{0,19}" inputMode="numeric" value={form.bot} disabled={busy} onChange={(event) => setForm({ ...form, bot: event.target.value })} /></label>
      <label>备注<input aria-label="Token 备注" maxLength={80} required value={form.label} disabled={busy} onChange={(event) => setForm({ ...form, label: event.target.value })} /></label>
      <label>有效期<select aria-label="Token 有效期" value={form.lifetime} disabled={busy} onChange={(event) => setForm({ ...form, lifetime: event.target.value })}><option value="3600">1 小时</option><option value="86400">1 天</option><option value="604800">7 天</option><option value="2592000">30 天</option></select></label>
      <button className="text-button" type="submit" disabled={busy || !summary.read_ready}><Plus size={16} />创建 Token</button>
    </form>
    {issued && <div className="mcp-token-secret" role="status"><label>新 Token · 仅本次可见<input aria-label="新 Token" type="password" readOnly autoComplete="off" value={issued.token} /></label>
      <button className="icon-button" title={copied ? '已复制 Token' : '复制 Token'} onClick={() => void navigator.clipboard.writeText(issued.token).then(() => setCopied(true)).catch(() => setError('复制失败，请选择新 Token 手动复制'))}>{copied ? <Check size={16} /> : <Copy size={16} />}</button>
      <button className="icon-button" title="隐藏新 Token" onClick={() => setIssued(null)}><X size={16} /></button>
    </div>}
    {error && <p className="mcp-error" role="alert">{error}</p>}
    {!tokens.length && <EmptyState>尚无 Token</EmptyState>}
    {tokens.slice(offset, offset + pageSize).map((token) => {
      const state = token.revoked ? 'revoked' : token.expires_at * 1000 <= Date.now() ? 'expired' : 'active'
      return <div className="mcp-token-row" key={token.token_id}><div><strong>{token.label}</strong><small>群 {token.scope_key.replace(scopePrefix, '')} · QQ {token.bot_id}</small></div><div><small>到期时间</small><time>{fmtTime(token.expires_at)}</time></div><StatusBadge value={state} label={state === 'active' ? '有效' : state === 'revoked' ? '已撤销' : '已过期'} /><button className="icon-button" title={`撤销 ${token.label}`} disabled={busy || state !== 'active'} onClick={() => void revoke(token)}><Ban size={16} /></button></div>
    })}
    <Pages offset={offset} total={tokens.length} setOffset={setOffset} disabled={busy} />
  </Section>
}

function Calls({ plane }: { plane: Plane }) {
  const [offset, setOffset] = useState(0)
  const [result, setResult] = useState<JsonObject | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    void plane.query(`/mcp/calls?offset=${offset}&limit=${pageSize}`, controller.signal).then((value) => {
      if (!controller.signal.aborted) { setResult(value); setError('') }
    }).catch((reason) => { if (!controller.signal.aborted) setError(String(reason)) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [offset, plane.data.mcp, plane.query])
  return <Section title="调用回执">
    {error && <p className="mcp-error" role="alert">{error}</p>}
    {!result?.items.length && <EmptyState>{loading ? '读取回执中…' : '尚无调用'}</EmptyState>}
    {result?.items.map((call: JsonObject) => <article className="mcp-call" key={call.call_id}>
      <header><code>{call.server_name} / {call.remote_name}</code><StatusBadge value={callTones[call.status] ?? call.status} label={callLabels[call.status] ?? call.status} /></header>
      <div className="mcp-meta">{call.call_id} · {fmtTime(call.updated_at)}</div>
      {call.result && <details><summary>结果详情</summary><pre>{JSON.stringify(call.result, null, 2)}</pre></details>}
    </article>)}
    <Pages offset={offset} total={result?.total ?? 0} setOffset={setOffset} disabled={loading} />
  </Section>
}

export function McpView({ plane }: { plane: Plane }) {
  const summary = plane.data.mcp
  const [selected, setSelected] = useState<string | null>(null)
  const [tab, setTab] = useState('servers')
  if (!summary) return <EmptyState>正在读取 MCP 配置…</EmptyState>
  if (!summary.configured) return <EmptyState>MCP 未启用</EmptyState>
  return <div className="mcp-view">
    <div className="mcp-toolbar"><div className="mcp-tabs" role="tablist" aria-label="MCP 页面">
      {[['servers', '外部工具'], ['tokens', '群只读 Token'], ['calls', '调用回执']].map(([id, label]) => <button key={id} role="tab" aria-selected={tab === id} onClick={() => setTab(id)}>{label}</button>)}
    </div><RefreshButton loading={plane.loading.has('mcp')} onClick={() => void plane.refresh('mcp')} /></div>
    {tab === 'servers' && <>
      <div className="mcp-selector"><label>服务器<select aria-label="选择 MCP 服务器" value={selected ?? ''} onChange={(event) => setSelected(event.target.value || null)}><option value="">选择服务器</option>{summary.servers.map((server: JsonObject) => <option key={server.name} value={server.name}>{server.name} · {server.enabled ? '已启用' : '已关闭'}</option>)}</select></label><button className="icon-button" title="登记 MCP 服务器" onClick={() => setSelected('')}><Plus size={16} /></button></div>
      {selected === null ? <EmptyState>尚未选择服务器</EmptyState> : <ServerEditor key={selected} selected={selected} plane={plane} onCreated={setSelected} />}
    </>}
    {tab === 'tokens' && <Tokens plane={plane} />}
    {tab === 'calls' && <Calls plane={plane} />}
  </div>
}
