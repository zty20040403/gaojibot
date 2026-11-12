import { useEffect, useState } from 'react'
import type { JsonObject } from './api'
import { DataTable, EmptyState, PageHeader, RefreshButton, Section, fmtTime } from './components'
import type { useControlPlane } from './useControlPlane'

type Plane = ReturnType<typeof useControlPlane>
const rows = (value: unknown): JsonObject[] => Array.isArray(value) ? value : []

export function ProgramsView({ plane }: { plane: Plane }) {
  const [endpoint, setEndpoint] = useState('')
  const [revision, refresh] = useState(0)
  const [busy, setBusy] = useState(false)
  return <>
    <PageHeader title="常驻程序" description="持久快照，不是实时进程探测。配置提交后仍需等待 VM 同步；刷新查看结果。"
      action={<RefreshButton loading={busy || plane.loading.has('programs')} onClick={() => { refresh((n) => n + 1); void plane.refresh('programs') }} />} />
    <Section title="选择会话" description="按真实 QQ 端点隔离，不合并共享历史的会话。">
      <select aria-label="程序所属会话" value={endpoint} disabled={busy} onChange={(event) => setEndpoint(event.target.value)}>
        <option value="">请选择会话</option>
        {rows(plane.data.programs?.items).map((item) => <option key={item.endpoint_key} value={item.endpoint_key}>{item.endpoint_key} · {item.binding_state}</option>)}
      </select>
      {plane.data.programs?.available === false && <EmptyState>程序数据库未配置</EmptyState>}
    </Section>
    {endpoint && <ProgramDetail key={endpoint} plane={plane} endpoint={endpoint} revision={revision} busy={busy} setBusy={setBusy} />}
  </>
}

function ProgramDetail({ plane, endpoint, revision, busy, setBusy }: {
  plane: Plane; endpoint: string; revision: number; busy: boolean; setBusy: (value: boolean) => void
}) {
  const [payload, setPayload] = useState<JsonObject | null>(null)
  const [reload, refresh] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [actionError, setActionError] = useState('')
  const [enabled, setEnabled] = useState(true)
  const [sayLimit, setSayLimit] = useState(60)
  const [wakeLimit, setWakeLimit] = useState(6)
  const [program, setProgram] = useState('')
  const [before, setBefore] = useState(0)
  const [messages, setMessages] = useState<JsonObject | null>(null)
  const [messageError, setMessageError] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true); setError(''); setPayload(null)
    void plane.query(`/programs?${new URLSearchParams({ endpoint_key: endpoint })}`, controller.signal).then((value) => {
      if (controller.signal.aborted) return
      if (!value.snapshot) throw new Error('该会话程序状态不可用')
      setPayload(value)
      const settings = value.snapshot.settings
      setEnabled(settings.programs_enabled); setSayLimit(settings.program_say_limit); setWakeLimit(settings.program_wake_limit)
    }).catch((reason) => { if (!controller.signal.aborted) setError(String(reason)) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [plane.query, endpoint, revision, reload])

  useEffect(() => {
    const controller = new AbortController()
    setMessages(null); setMessageError('')
    void plane.query(`/programs/messages?${new URLSearchParams({ endpoint_key: endpoint, program_name: program, before: String(before) })}`, controller.signal)
      .then((value) => { if (!controller.signal.aborted) setMessages(value) })
      .catch((reason) => { if (!controller.signal.aborted) setMessageError(String(reason)) })
    return () => controller.abort()
  }, [plane.query, endpoint, program, before, revision, reload])

  const change = async (path: string, body: JsonObject) => {
    if (!payload || loading || busy) return
    setBusy(true); setActionError(''); setNotice('')
    try {
      // Pin to the exact snapshot being edited, not a newer global SSE version.
      const result = await plane.mutate('programs', `/programs/${path}`, 'POST', { endpoint_key: endpoint, ...body }, ['programs', 'jobs'], payload.resource_version)
      setNotice(result.job_id ? `恢复任务 #${result.job_id} 已入队；不是启动成功。请刷新查看任务与单元状态。` : '策略已保存，正在等待 VM 同步；尚未确认单元已停止或启动。')
    } catch (reason) { setActionError(String(reason)) }
    finally { setBusy(false); refresh((n) => n + 1) }
  }
  const snapshot = payload?.snapshot
  const disabled = new Set(rows(snapshot?.controls).filter((item) => !item.enabled).map((item) => item.program_name))
  const names = [...new Set([...rows(snapshot?.controls).map((item) => item.program_name), ...rows(snapshot?.requests_24h).map((item) => item.program_name),
    ...rows(snapshot?.vms).flatMap((vm) => rows(vm.units).map((unit) => String(unit.name).replace(/\.(service|timer)$/, '')))])].sort()
  return <>
    {error && <p role="alert" className="error-text">{error}</p>}
    {actionError && <p role="alert" className="error-text">上次操作失败：{actionError}</p>}
    {notice && <p role="status">{notice}</p>}
    <Section title="会话策略" description="发言和发文件共享小时额度；0 表示禁止。修改额度或重新启用不会退还已用次数。">
      <form className="actions" onSubmit={(event) => { event.preventDefault(); void change('settings', { enabled, say_limit: sayLimit, wake_limit: wakeLimit }) }}>
        <label><input type="checkbox" checked={enabled} disabled={busy || loading} onChange={(event) => setEnabled(event.target.checked)} />允许常驻程序</label>
        <label>发言/小时 <input type="number" min={0} max={10000} required disabled={busy || loading} value={sayLimit} onChange={(event) => setSayLimit(Number(event.target.value))} /></label>
        <label>唤醒/小时 <input type="number" min={0} max={1000} required disabled={busy || loading} value={wakeLimit} onChange={(event) => setWakeLimit(Number(event.target.value))} /></label>
        <button disabled={busy || loading || !snapshot}>保存</button>
        <button type="button" disabled={busy || loading || !snapshot?.settings.programs_enabled} onClick={() => void change('settings', {
          enabled: false, say_limit: snapshot.settings.program_say_limit, wake_limit: snapshot.settings.program_wake_limit,
        })}>停用本会话全部程序</button>
      </form>
      <p>程序名是本会话内的管理标签，不是不同 VM 用户间的安全边界。/stop 会拒绝程序写动作，但不会停止程序进程。</p>
    </Section>
    <Section title="程序筛选与单独控制">
      <select aria-label="按程序筛选" value={program} onChange={(event) => { setProgram(event.target.value); setBefore(0) }}>
        <option value="">所有程序</option>{names.map((name) => <option key={name} value={name}>{name}</option>)}
      </select>
      <button disabled={!program || busy || loading || !snapshot} onClick={() => void change('control', { program_name: program, enabled: disabled.has(program) })}>
        {disabled.has(program) ? '取消单程序停用' : '停用此程序'}
      </button>
      <DataTable><thead><tr><th>程序</th><th>24h 发言请求</th><th>唤醒请求</th><th>文件请求</th></tr></thead><tbody>
        {rows(snapshot?.requests_24h).filter((row) => !program || row.program_name === program).map((row) => <tr key={row.program_name}>
          <td>{row.program_name}</td><td>{row.say_requests}</td><td>{row.wake_requests}</td><td>{row.file_requests}</td></tr>)}
      </tbody></DataTable><p>已接收请求次数不代表实际送达次数。</p>
    </Section>
    {rows(snapshot?.vms).map((vm) => <Section key={vm.sandbox_id} title={`VM ${vm.sandbox_id}`} description={`快照 ${fmtTime(vm.updated_at)} · ${vm.retained ? '占用常驻名额' : '未占名额'}`}>
      <p>{vm.alert || vm.hold_reason || (vm.paused ? '人工暂停' : '无保护告警')}</p>
      {(vm.suspended || vm.paused) && <button disabled={busy || loading} onClick={() => {
        if (window.confirm('已检查日志？恢复可能重新启动 VM 和登记程序，但不会解除程序停用或 /stop。')) void change('resume', { sandbox_id: vm.sandbox_id, recovery_fence: vm.recovery_fence })
      }}>检查后恢复自动加载</button>}
      <DataTable><thead><tr><th>单元</th><th>宿主策略</th><th>运行快照</th><th>最近失败日志</th></tr></thead><tbody>
        {rows(vm.units).filter((unit) => !program || String(unit.name).replace(/\.(service|timer)$/, '') === program).map((unit) => <tr key={unit.name}>
          <td>{unit.name}</td><td>{!snapshot.settings.programs_enabled || disabled.has(String(unit.name).replace(/\.(service|timer)$/, '')) ? '已停用' : '允许'}</td>
          <td>{unit.ActiveState} / {unit.SubState}</td><td><pre>{unit.logs || '无失败日志'}</pre></td>
        </tr>)}
      </tbody></DataTable>
      {Object.keys(vm.errors ?? {}).length > 0 && <pre role="alert">{JSON.stringify(vm.errors, null, 2)}</pre>}
    </Section>)}
    <Section title="恢复任务与投递失败">
      {rows(snapshot?.recovery_jobs).map((job) => <p key={job.job_id}>#{job.job_id} · {job.status} · {job.last_error || '无任务错误'}（重置完成不等于程序已启动）</p>)}
      {rows(snapshot?.recent_failures).filter((row) => !program || row.program_name === program).map((row, index) => <p key={index}>{row.program_name} · {row.kind} · {row.state} · {row.error_code}</p>)}
    </Section>
    <Section title="程序消息回执" description="只展示已写入账本的程序消息；包括合并唤醒中的所选程序，不展示其他会话或原生账号身份。">
      {messageError && <p role="alert">{messageError}</p>}
      {rows(messages?.items).map((message) => <div key={message.id}><small>msg#{message.id} · {fmtTime(message.at)} · {message.programs?.join('、') || message.program}</small><pre>{message.text}</pre></div>)}
      {messages && !rows(messages.items).length && <EmptyState>没有符合条件的程序消息</EmptyState>}
      <button disabled={!before || !messages} onClick={() => setBefore(0)}>返回最新</button>
      <button disabled={!messages?.next_cursor} onClick={() => setBefore(messages!.next_cursor)}>更早消息</button>
    </Section>
  </>
}
