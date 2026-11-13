import { useEffect, useState } from 'react'
import type { JsonObject } from './api'
import { DataTable, EmptyState, RefreshButton, Section, fmtTime } from './components'
import type { useControlPlane } from './useControlPlane'

type Plane = ReturnType<typeof useControlPlane>
const states: Record<string, string> = { pending: '已登记', starting: '正在启动', running: '运行中', stopping: '等待确认停止',
  ambiguous: '结果不明，保留名额', completed: '进程已结束', failed: '命令失败', cancelled: '已取消' }

export function CommandRows({ items, busy, cancel }: { items: JsonObject[]; busy: boolean; cancel: (row: JsonObject) => void }) {
  return <DataTable><thead><tr><th>命令／会话</th><th>进程结果</th><th>完成通知</th><th>输出快照</th><th>操作</th></tr></thead><tbody>
    {items.map((row) => <tr key={row.command_id}>
      <td><code>{row.command_id}</code><small className="cell-sub">{row.program_name} · {row.endpoint_key}</small>
        <small className="cell-sub">创建 {fmtTime(row.created_at)} · 截止 {fmtTime(row.deadline_at)}</small></td>
      <td>{states[row.state] ?? row.state}<small className="cell-sub">退出码 {row.exit_code ?? '未知'} · 用时 {row.elapsed_seconds ?? '未知'} 秒</small>
        <small className="cell-sub">{row.error_code}</small></td>
      <td>{row.notification ? (row.notification.silent && row.notification.state === 'completed' ? '模型保持静默，未发送消息' :
        row.notification.state === 'completed' ? '已有平台回执' : `通知 ${row.notification.state}，未确认送达`) : '尚无通知回执'}
        <small className="cell-sub">{row.notification_error || row.notification?.error_code}</small></td>
      <td><code>{row.output_path}</code><details><summary>已保存头尾 · {row.output_bytes} 字节</summary>
        <pre>{row.output_head || '暂无输出头部'}</pre><pre>{row.output_tail || '暂无输出尾部'}</pre></details></td>
      <td>{row.finished_at == null && <button disabled={busy || row.cancel_requested} onClick={() => cancel(row)}>
        {row.cancel_requested ? '等待停止确认' : '停止命令'}</button>}</td>
    </tr>)}
  </tbody></DataTable>
}

export function CommandsPanel({ plane }: { plane: Plane }) {
  const [endpoint, setEndpoint] = useState('')
  const [active, setActive] = useState(true)
  const [revision, refresh] = useState(0)
  const [payload, setPayload] = useState<JsonObject | null>(null)
  const [draft, setDraft] = useState<{ hours: number; version: number } | null>(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [actionError, setActionError] = useState('')
  const [notice, setNotice] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true); setError(''); setPayload(null)
    void plane.query(`/background-commands?${new URLSearchParams({ endpoint_key: endpoint, active: String(active) })}`, controller.signal)
      .then((value) => { if (!controller.signal.aborted) setPayload(value) })
      .catch((reason) => { if (!controller.signal.aborted) setError(String(reason)) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [plane.query, plane.data.commands, endpoint, active, revision])

  const change = async (path: string, method: 'POST' | 'PUT', body: JsonObject, version: number) => {
    if (busy || loading || !payload) return
    setBusy(true); setActionError(''); setNotice('')
    try {
      await plane.mutate('commands', `/background-commands/${path}`, method, body, ['commands', 'backgroundAgents'], version)
      setDraft(null)
      setNotice(method === 'PUT' ? '时限已保存，仅作用于此后创建的任务；旧任务截止时间不变。' : '已保存取消意图；不等于进程已停止，也不会撤回已发送的消息。')
    } catch (reason) { setActionError(String(reason)) }
    finally { setBusy(false); refresh((n) => n + 1) }
  }
  const endpoints: JsonObject[] = payload?.endpoints ?? plane.data.commands?.endpoints ?? []
  const selected = endpoints.find((row) => row.endpoint_key === endpoint)
  const stale = draft !== null && payload !== null && draft.version !== payload.resource_version
  return <Section title="后台命令与任务时限" description="命令等待不占模型；已登记、进程退出与 QQ 通知送达是三个不同状态。停止确认前仍保留 VM 和命令名额。"
    action={<RefreshButton loading={loading || busy} onClick={() => refresh((n) => n + 1)} />}>
    <div className="actions">
      <input aria-label="后台命令所属会话" list="command-endpoints" maxLength={1000} value={endpoint} disabled={busy}
        placeholder="全部会话；填 endpoint_key 可精确筛选" onChange={(event) => { setEndpoint(event.target.value); setDraft(null); setNotice(''); setActionError('') }} />
      <datalist id="command-endpoints">{endpoints.map((row) => <option key={row.endpoint_key} value={row.endpoint_key} />)}</datalist>
      <label><input type="checkbox" checked={active} disabled={busy} onChange={(event) => setActive(event.target.checked)} />仅未确认结束</label>
    </div>
    {endpoint && selected && <form className="actions" onSubmit={(event) => {
      event.preventDefault()
      if (draft && !stale) void change('settings', 'PUT', { endpoint_key: endpoint, hours: draft.hours }, draft.version)
    }}>
      <label>新 agent 时限 <select disabled={busy || loading || !payload} value={draft?.hours ?? selected.hours} onChange={(event) => {
        setDraft({ hours: Number(event.target.value), version: draft?.version ?? payload!.resource_version })
      }}>{[1, 2, 3, 4, 5, 6].map((hours) => <option key={hours} value={hours}>{hours} 小时</option>)}</select></label>
      <button disabled={busy || loading || !draft || stale}>保存时限</button>
      {draft && <button type="button" disabled={busy} onClick={() => setDraft(null)}>取消编辑，读取最新设置</button>}
      {stale && <span role="alert">配置版本已改变，请重新读取后编辑。</span>}
    </form>}
    <p>默认六小时，按物理会话设置1–6小时；ask_main 最多两小时且不超过任务剩余时间。单次 run-bg 命令仍最多六小时。</p>
    {error && <p role="alert" className="error-text">{error}</p>}
    {actionError && <p role="alert" className="error-text">上次操作失败：{actionError}</p>}
    {notice && <p role="status">{notice}</p>}
    {payload?.truncated && <p>仅显示最近100条，请按会话筛选。</p>}
    {payload?.endpoints_truncated && <p>会话建议列表已截断；可手动填入完整 endpoint_key。</p>}
    <CommandRows items={payload?.items ?? []} busy={busy || loading} cancel={(row) => {
      if (payload && window.confirm('停止此命令，并阻止尚未发出的完成通知？已发送的消息不会撤回。'))
        void change(`${encodeURIComponent(row.command_id)}/cancel`, 'POST', { endpoint_key: row.endpoint_key }, payload.resource_version)
    }} />
    {payload && !payload.items?.length && <EmptyState>{payload.available === false ? '后台命令数据库未配置' : '没有符合条件的命令'}</EmptyState>}
  </Section>
}
