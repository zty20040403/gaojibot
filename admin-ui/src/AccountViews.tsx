import { useState } from 'react'
import { Bot, KeyRound, LogOut, ShieldCheck } from 'lucide-react'
import { DataTable, EmptyState, Metric, Section, StatusBadge, fmtDuration, fmtTime } from './components'
import type { useControlPlane } from './useControlPlane'
import type { JsonObject } from './api'

type Plane = ReturnType<typeof useControlPlane>

export function LoginGate({ plane }: { plane: Plane }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  return <main className="token-gate">
    <form onSubmit={async (event) => {
      event.preventDefault(); setBusy(true)
      try { await plane.login(username, password); setPassword('') } catch { /* Login error is shown below. */ }
      finally { setBusy(false) }
    }}>
      <div className="token-mark"><KeyRound size={22} /></div>
      <h1>gaoji Control</h1>
      <p>使用你的账户登录。管理操作通过 QQ 私聊确认。</p>
      <label><span>账户</span><input autoFocus autoComplete="username" required maxLength={32} value={username} onChange={(e) => setUsername(e.target.value)} /></label>
      <label><span>密码</span><input type="password" autoComplete="current-password" required maxLength={128} value={password} onChange={(e) => setPassword(e.target.value)} /></label>
      {plane.error && <p role="alert" className="account-error">{plane.error}</p>}
      <button className="primary-button" type="submit" disabled={busy || !username.trim() || !password}>{busy ? '正在登录…' : '登录'}</button>
      <small>账户由管理员创建。普通成员可查看机器人基础状态。</small>
    </form>
  </main>
}

export function MemberStatus({ plane }: { plane: Plane }) {
  const status = plane.data.status
  return <div className="member-shell">
    <header className="member-header"><div><Bot size={24} /><strong>gaoji · 机器人状态</strong></div><div><span>{plane.user?.username} · 普通成员</span><button className="text-button" onClick={() => void plane.logout()}><LogOut size={16} />退出</button></div></header>
    <main>
      <Section title="运行状态" description="基础状态每 30 秒更新一次。" action={<button className="command-button" onClick={() => void plane.refreshAll()}>刷新</button>}>
        {plane.error && <p role="alert" className="account-error">{plane.error}</p>}
        {status ? <div className="account-metrics">
          <Metric label="机器人服务" value={<StatusBadge value={plane.online ? 'online' : 'offline'} label={plane.online ? '运行中' : '连接中断'} />} />
          <Metric label="QQ 连接" value={<StatusBadge value={status.qq_connected ? 'online' : 'offline'} label={status.qq_connected ? '已连接' : '未连接'} />} />
          <Metric label="运行时间" value={fmtDuration(status.uptime_seconds)} />
          <Metric label="版本" value={status.version} />
        </div> : <EmptyState>正在读取机器人状态…</EmptyState>}
      </Section>
    </main>
  </div>
}

const LABELS: Record<string, string> = { sending: '发送口令中', pending: '待手机确认', awaiting_quorum: '等待另一位管理员', queued: '已确认，待执行', executing: '执行中', succeeded: '操作完成', failed: '执行失败', expired: '已过期', cancelled: '已取消', locked: '口令已锁定', delivery_failed: '私聊发送失败', needs_attention: '需核对结果' }

export function AccountsView({ plane }: { plane: Plane }) {
  const [selected, setSelected] = useState<JsonObject | null>(null)
  const [creating, setCreating] = useState(false)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState('member')
  const [qq, setQq] = useState('')
  const [enabled, setEnabled] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const accounts: JsonObject[] = plane.data.accounts?.items ?? []
  const approvals: JsonObject[] = plane.data.approvals?.items ?? []
  const open = (account: JsonObject | null) => {
    setSelected(account); setCreating(!account); setUsername(account?.username ?? '')
    setPassword(''); setRole(account?.role ?? 'member'); setQq(account?.qq_id ?? '')
    setEnabled(account ? Boolean(account.enabled) : true); setError('')
  }
  return <>
    <Section title="账户与权限" description="管理员变更账户后，该账户的已有登录和未执行批准会失效。" action={<button className="command-button" onClick={() => open(null)}>创建账户</button>}>
      <DataTable><thead><tr><th>账户</th><th>权限</th><th>绑定 QQ</th><th>状态</th><th /></tr></thead><tbody>
        {accounts.map((account) => <tr key={account.account_id}><td>{account.username}</td><td>{account.role === 'admin' ? '管理员' : '普通成员 · 仅状态'}</td><td>{account.qq_id || '—'}</td><td><StatusBadge value={account.enabled ? 'active' : 'blocked'} label={account.enabled ? '启用' : '停用'} /></td><td><button className="text-button" onClick={() => open(account)}>编辑</button></td></tr>)}
      </tbody></DataTable>
      {(creating || selected) && <form className="account-form" onSubmit={async (event) => {
        event.preventDefault(); setBusy(true); setError('')
        const body = selected ? { expected_version: selected.version, role, qq_id: qq || null, enabled, ...(password ? { password } : {}) } : { username, password, role, qq_id: qq || null }
        try {
          await plane.mutate('accounts', selected ? `/accounts/${selected.account_id}` : '/accounts', selected ? 'PUT' : 'POST', body, ['accounts', 'approvals', 'securityAudit'])
          setSelected(null); setCreating(false); setPassword('')
        } catch (reason) { setError(reason instanceof Error ? reason.message : '操作失败') }
        finally { setBusy(false) }
      }}>
        <h3>{selected ? `编辑 ${selected.username}` : '创建账户'}</h3>
        <label>账户名<input disabled={Boolean(selected) || busy} required pattern="[a-zA-Z0-9][a-zA-Z0-9_.-]{2,31}" autoComplete="off" value={username} onChange={(e) => setUsername(e.target.value)} /></label>
        <label>{selected ? '新密码（留空保持现有密码）' : '密码（12～128 个字符）'}<input type="password" autoComplete="new-password" minLength={12} maxLength={128} required={!selected} disabled={busy} value={password} onChange={(e) => setPassword(e.target.value)} /></label>
        <label>角色<select value={role} disabled={busy} onChange={(e) => setRole(e.target.value)}><option value="member">普通成员 · 只看状态</option><option value="admin">管理员</option></select></label>
        <label>绑定 QQ{role === 'admin' ? '（必填）' : '（选填）'}<input inputMode="numeric" pattern="[1-9][0-9]{4,14}" required={role === 'admin'} disabled={busy} value={qq} onChange={(e) => setQq(e.target.value)} /></label>
        {selected && <label className="account-checkbox"><input type="checkbox" checked={enabled} disabled={busy} onChange={(e) => setEnabled(e.target.checked)} />启用账户</label>}
        {error && <p role="alert" className="account-error">{error}</p>}
        <div className="account-actions"><button className="primary-button" type="submit" disabled={busy}>{busy ? '保存中…' : '保存'}</button><button type="button" className="text-button" onClick={() => { setSelected(null); setCreating(false); setPassword('') }}>关闭表单</button></div>
      </form>}
    </Section>
    <Section title="服务器任务授权" description="高风险服务器操作需要两位管理员；任务内共享授权，结束或修订后失效。" action={<button className="command-button" onClick={() => void plane.refresh('approvals')}>刷新</button>}>
      {!approvals.length && <EmptyState>还没有需要确认的操作。</EmptyState>}
      <div className="approval-list">{approvals.map((item) => <article key={item.approval_id}>
        <header><code>{item.approval_id}</code><StatusBadge value={item.status} label={item.kind === 'quorum_vote' && item.status === 'succeeded' ? '复核已记录' : LABELS[item.status] ?? item.status} /><small>{fmtTime(item.created_at)}</small></header>
        {item.quorum > 1 && <p>已确认 {item.approved_count ?? 0}/{item.quorum} · <code>{item.review_command}</code>{item.parent_approval_id && <> · 原操作 {LABELS[item.parent_status] ?? item.parent_status}</>}</p>}
        <details><summary>查看操作内容</summary><pre>{item.summary}</pre>{Object.keys(item.result ?? {}).length > 0 && <pre>{JSON.stringify(item.result, null, 2)}</pre>}</details>
        <div className="account-actions">
          {['pending', 'awaiting_quorum', 'expired', 'locked', 'delivery_failed'].includes(item.status) && <button className="command-button" onClick={async () => { try { await plane.approvalAction(item.approval_id, 'resend') } catch (e) { setError(String(e)) } }}>重发口令</button>}
          {['pending', 'awaiting_quorum', 'queued', 'sending', 'expired', 'locked', 'delivery_failed'].includes(item.status) && <button className="text-button" onClick={async () => { try { await plane.approvalAction(item.approval_id, 'cancel') } catch (e) { setError(String(e)) } }}>取消操作</button>}
        </div>
      </article>)}</div>
      {error && <p className="account-error" role="alert">{error}</p>}
    </Section>
    <Section title="账户与授权审计" description="记录真实账户身份，记录中不保存密码或口令。">
      <DataTable><thead><tr><th>时间</th><th>账户 ID</th><th>事件</th><th>操作 / 目标</th></tr></thead><tbody>{(plane.data.securityAudit?.items ?? []).map((item: JsonObject) => <tr key={item.audit_id}><td>{fmtTime(item.created_at)}</td><td><code>{item.account_id || '未登录'}</code></td><td>{item.action}</td><td><code>{item.target || '—'}</code></td></tr>)}</tbody></DataTable>
    </Section>
  </>
}

export function ApprovalNotice({ plane }: { plane: Plane }) {
  if (!plane.pendingApproval) return null
  return <div className="approval-notice" role="status"><ShieldCheck size={19} /><div><strong>{LABELS[plane.pendingApproval.status] ?? '等待手机确认'} · {plane.pendingApproval.approval_id}</strong><span>{plane.pendingApproval.quorum > 1 ? `已确认 ${plane.pendingApproval.approved_count ?? 0}/${plane.pendingApproval.quorum}；另一位管理员私聊发送「${plane.pendingApproval.review_command}」，核对后回复自己的验证码。` : '请在绑定 QQ 的机器人私聊中核对操作并回复 6 位口令。'}</span></div></div>
}
