import { useState } from 'react'
import type { JsonObject } from './api'
import type { useControlPlane } from './useControlPlane'
import { DataTable, EmptyState, RefreshButton, Section, StatusBadge } from './components'

type Plane = ReturnType<typeof useControlPlane>
export type BudgetDraft = { endpoint: string; version: number; enabled: boolean; amount: string }
const notices: Record<string, string> = { not_required: '无通知意图', pending: '等待发送', sending: '已排队，未确认', committed: '已确认送达', failed: '发送失败，不重试', unknown: '结果不明，不重试', cancelled: '已取消，不重放' }

export function beginBudgetDraft(row: JsonObject, version: number): BudgetDraft {
  if (!Number.isSafeInteger(version) || version < 0) throw new Error('请先刷新预算快照')
  return { endpoint: row.endpoint_key, version, enabled: row.daily_budget_usd !== null, amount: row.daily_budget_usd ?? '' }
}

export function budgetRequest(draft: BudgetDraft) {
  if (!Number.isSafeInteger(draft.version) || draft.version < 0) throw new Error('缺少预算快照版本')
  if (draft.enabled && (!/^[0-9]{1,7}(?:\.[0-9]{1,6})?$/.test(draft.amount)
    || BigInt(draft.amount.split('.')[0]) * 1000000n + BigInt((draft.amount.split('.')[1] ?? '').padEnd(6, '0')) > 1000000000000n)) throw new Error('请输入0–1000000的美元十进制，最多6位小数')
  return { body: { endpoint_key: draft.endpoint, daily_budget_usd: draft.enabled ? draft.amount : null }, version: draft.version }
}

export function BudgetEditor({ draft, onChange, onSave, onCancel, busy, stale }: {
  draft: BudgetDraft; onChange: (draft: BudgetDraft) => void; onSave: () => void; onCancel: () => void; busy: boolean; stale: boolean
}) {
  let valid = true
  try { budgetRequest(draft) } catch { valid = false }
  return <div className="actions">
    <label><input type="checkbox" checked={draft.enabled} disabled={busy} onChange={(event) => onChange({ ...draft, enabled: event.target.checked })} />启用美元日预算</label>
    <input type="text" inputMode="decimal" aria-label="美元日预算" placeholder="填写美元金额" value={draft.amount} disabled={busy || !draft.enabled} onChange={(event) => onChange({ ...draft, amount: event.target.value })} />
    <button disabled={busy || stale || !valid} onClick={onSave}>保存预算</button><button disabled={busy} onClick={onCancel}>取消编辑</button>
    {stale && <small>预算快照已更新；请取消编辑并核对新设置，旧表单不会使用新版本覆盖。</small>}
    {draft.enabled && /^0+(?:\.0+)?$/.test(draft.amount) && <small>0会禁止自动动作；关闭预算请取消勾选。设置不清除 /stop。</small>}
  </div>
}

function BudgetRow({ row, version, plane }: { row: JsonObject; version: number; plane: Plane }) {
  const [draft, setDraft] = useState<BudgetDraft | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function save() {
    if (!draft || busy) return
    setBusy(true); setError('')
    try {
      const request = budgetRequest(draft)
      await plane.mutate('budgets', '/conversations/budgets', 'POST', request.body, ['budgets', 'conversations'], request.version)
      setDraft(null)
    } catch { setError('未保存。请取消编辑、刷新并核对预算；不会自动重试。') }
    finally { setBusy(false) }
  }
  return <tr>
    <td><code>{row.endpoint_key}</code><small className="cell-sub">{row.kind === 'group' ? '群聊' : '私聊'}{row.stopped ? ' · /stop仍开启' : ''}</small></td>
    <td>{row.daily_budget_usd === null ? '关闭' : `$${row.daily_budget_usd}`}<small className="cell-sub">北京时间零点日界</small></td>
    <td>{row.cost_is_lower_bound ? '≥ ' : ''}${row.spent_usd}<small className="cell-sub">{row.incomplete_calls}次费用不完整，{row.pending_calls}次未收尾</small></td>
    <td><StatusBadge value={row.blocked ? 'blocked' : 'active'} label={row.blocked ? '预算限制中' : '未被预算限制'} /><small className="cell-sub">{notices[row.notice_state] ?? '通知状态未知'}</small></td>
    <td>{draft ? <BudgetEditor draft={draft} onChange={setDraft} onSave={() => void save()} onCancel={() => { setDraft(null); setError('') }} busy={busy} stale={draft.version !== version} />
      : <button disabled={!Number.isSafeInteger(version) || version < 0} onClick={() => setDraft(beginBudgetDraft(row, version))}>设置预算</button>}{error && <p role="alert">{error}</p>}</td>
  </tr>
}

export function BudgetsPanel({ plane }: { plane: Plane }) {
  const payload = plane.data.budgets
  const items = Array.isArray(payload?.items) ? payload.items : []
  return <Section title="每日美元预算" description="默认关闭。达到已记录费用门槛后停主动接话/程序唤醒，@或私聊直接回复仍计费；未知及在途费用可能超出，不是供应商硬限额。北京时间零点仅恢复新自动动作，不清除 /stop，不补跑旧请求。通知每天只尝试一次，结果不明不重试。"
    action={<RefreshButton loading={plane.loading.has('budgets')} onClick={() => void plane.refreshMany(['budgets'])} />}>
    <DataTable><thead><tr><th>物理会话</th><th>日预算</th><th>今日已记录费用</th><th>自动限制 / 通知</th><th>配置</th></tr></thead><tbody>
      {items.map((row: JsonObject) => <BudgetRow key={row.endpoint_key} row={row} version={payload?.resource_version} plane={plane} />)}
    </tbody></DataTable>
    {!items.length && <EmptyState>{payload?.available === false ? '预算设置未配置' : '没有预算会话；请刷新'}</EmptyState>}
    {payload?.truncated && <p role="alert">仅显示前500个物理会话，未显示的会话不受此页面修改。</p>}
  </Section>
}
