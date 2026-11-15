import { useEffect, useMemo, useState } from 'react'
import { AdminApiError, type JsonObject } from './api'
import { DataTable, EmptyState, RefreshButton, Section, fmtTime } from './components'
import type { useControlPlane } from './useControlPlane'

type Query = ReturnType<typeof useControlPlane>['query']
type Lane = 'links' | 'records' | 'detail' | 'accounts'
export type AuditState = { account: string; endpoint: string; accounts: JsonObject[]; accountCursor: string | null;
  links: JsonObject | null; records: JsonObject | null; detail: JsonObject | null; busy: Partial<Record<Lane, boolean>>;
  errors: Partial<Record<Lane, string>>; stale: boolean }
const empty = (): AuditState => ({ account: '', endpoint: '', accounts: [], accountCursor: null, links: null,
  records: null, detail: null, busy: {}, errors: {}, stale: false })
const base = '/conversation-questions'
const outcomes: Record<string, string> = { queued: '等待目标执行', running: '目标处理中，尚无回答', answered: '私有回答已生成',
  refused: '目标明确拒绝', cancelled: '原请求已取消，不恢复', expired: '原请求已过期，不恢复', unknown: '处理结果不明，不重放' }
const reasons: Record<string, string> = { declined: '涉及隐私或目标不愿回答', cancelled: '取消', expired: '原截止已到',
  source_unavailable: '原来源不可用', target_unavailable: '目标停止、预算或授权不可用', model_unknown: '模型结果不明', restart: '旧账号运行期结束' }
const noticeStates: Record<string, string> = { pending: '公开通知待投递', sending: '公开通知投递中', committed: '公开通知已授权提交',
  failed: '公开通知投递失败', cancelled: '公开通知已取消', expired: '公开通知已过期', unknown: '公开通知状态不明' }
const noticeDeliveries: Record<string, string> = { pending: '未确认平台回执', committed: '平台已接受主动投递（不等于群友已读）',
  failed: '平台拒绝或投递失败', cancelled: '发送计划已取消', unknown: '平台回执不明，不重发' }

// Same scope and lane only; ignored aborts and late successes cannot refill a new selection.
export class QuestionAuditReader {
  state = empty()
  private epoch = 0
  private requests = new Map<Lane, AbortController>()
  constructor(private query: Query, private notify: (state: AuditState) => void) {}
  private update(fields: Partial<AuditState>) { this.state = { ...this.state, ...fields }; this.notify(this.state) }
  private abort() { for (const controller of this.requests.values()) controller.abort(); this.requests.clear(); this.epoch++ }
  close() { this.abort(); this.state = empty() }
  directory(payload: JsonObject) {
    const accounts = [...new Map([...this.state.accounts, ...(payload.accounts ?? [])].map((item: JsonObject) => [item.account_key, item])).values()]
    this.update({ accounts, accountCursor: payload.next_cursor ?? null, stale: Boolean(this.state.links || this.state.records) })
  }
  select(account: string, endpoint = '') {
    this.abort(); this.update({ account, endpoint, links: null, records: null, detail: null, busy: {}, errors: {}, stale: false })
    return account ? Promise.all([this.read('links'), this.read('records')]) : Promise.resolve([])
  }
  async read(lane: Lane, after = '') {
    const { account, endpoint } = this.state
    if (lane !== 'accounts' && !account) return
    if (lane === 'detail' && !/^CQ-[0-9a-f]{32}$/.test(after)) return
    this.requests.get(lane)?.abort()
    const controller = new AbortController(), epoch = this.epoch
    this.requests.set(lane, controller)
    const current = () => !controller.signal.aborted && epoch === this.epoch && this.requests.get(lane) === controller
    if (lane === 'records') { this.requests.get('detail')?.abort(); this.requests.delete('detail') }
    this.update({ ...(lane === 'accounts' ? {} : { [lane]: null }), ...(lane === 'records' ? { detail: null } : {}),
      busy: { ...this.state.busy, [lane]: true, ...(lane === 'records' ? { detail: false } : {}) }, errors: { ...this.state.errors, [lane]: '' } })
    const params = new URLSearchParams(lane === 'accounts' ? { after, limit: '50' } :
      { account_key: account, ...(lane === 'detail' ? {} : { endpoint_key: endpoint, after, limit: '50' }) })
    const path = lane === 'accounts' ? base : lane === 'detail' ? `${base}/records/${encodeURIComponent(after)}` : `${base}/${lane}`
    try {
      const payload = await this.query(`${path}?${params}`, controller.signal)
      if (!current()) return
      if (lane === 'accounts') this.directory(payload)
      else this.update({ [lane]: payload })
    } catch (reason) {
      if (!current()) return
      if (reason instanceof AdminApiError && [401, 403].includes(reason.status)) {
        this.abort(); this.update({ ...empty(), errors: { [lane]: '管理员登录或权限已失效；已清除私有审计，请重新登录。' } }); return
      }
      this.update({ errors: { ...this.state.errors, [lane]: reason instanceof Error ? reason.message : '审计读取失败；未执行任何动作' } })
    } finally {
      if (current()) this.update({ busy: { ...this.state.busy, [lane]: false } })
    }
  }
}

function endpoint(value: JsonObject) { return `${value.kind === 'group' ? '群聊' : '私聊'} · ${value.native_id}` }
export function QuestionLinks({ items }: { items: JsonObject[] }) {
  return <DataTable><thead><tr><th>单向连接</th><th>源 → 目标（物理会话）</th><th>保存状态 / 版本</th><th>发行事实</th></tr></thead>
    <tbody>{items.map((item) => <tr key={item.link_id}><td><code>{item.link_id}</code><small className="cell-sub">创建 {fmtTime(item.created_at)}</small></td>
      <td>{endpoint(item.source)} → {endpoint(item.target)}</td><td>{item.state === 'revoked' ? '已撤销' : item.state === 'active' ? '存储为 active；当前授权另验' : '存储状态未知'} · {item.generation}
        {item.revoked_at != null && <small className="cell-sub">撤销 {fmtTime(item.revoked_at)}</small>}</td>
      <td>{item.receipt_recorded && item.issue_state === 'committed' ? '已保存真实发行回执' : '无有效发行回执，不能据此访问'}<small className="cell-sub">不是新的访问授权</small></td></tr>)}</tbody></DataTable>
}
function noticeReceiptText(notice: JsonObject) {
  if (notice.receipt_recorded && notice.delivery_state === 'committed') {
    const label = notice.state === 'committed' ? '平台已接受主动投递（不等于群友已读）' : '已保存迟到平台接受回执（未恢复原授权，不等于群友已读）'
    return notice.delivery_received_at != null ? `${label} · ${fmtTime(notice.delivery_received_at)}` : label
  }
  return noticeDeliveries[notice.delivery_state] ?? '无有效平台回执'
}
export function QuestionRecords({ items, detail }: { items: JsonObject[]; detail: (id: string) => void }) {
  return <DataTable><thead><tr><th>请求 / 时间</th><th>源 → 目标</th><th>私有处理结果</th><th>目标公开通知</th><th>原任务领取 / 送达</th><th>正文</th></tr></thead>
    <tbody>{items.map((item) => <tr key={item.question_id}><td><code>{item.question_id}</code><small className="cell-sub">job#{item.job_id} · link {item.link_id} / {item.link_generation}</small>
      <small className="cell-sub">创建 {fmtTime(item.created_at)} · 截止 {fmtTime(item.deadline_at)}</small></td><td>{endpoint(item.source)} → {endpoint(item.target)}</td>
      <td>{outcomes[item.state] ?? '未知处理状态'}<small className="cell-sub">{reasons[item.reason_code] ?? ''}</small>{item.finished_at != null && <small className="cell-sub">结束 {fmtTime(item.finished_at)}</small>}</td>
      <td>{!item.notice ? '未触发目标群公开通知' : <><code>{item.notice.notice_id}</code> · {noticeStates[item.notice.state] ?? '公开通知状态未知'}
        <small className="cell-sub">{noticeReceiptText(item.notice)}</small>
        <small className="cell-sub">目标群 Bot {item.notice.bot_native_id} · 与私有回答相互独立</small></>}</td>
      <td>{item.consumed_at == null ? '未记录原任务领取' : `原任务已领取 · ${fmtTime(item.consumed_at)}`}<small className="cell-sub">已领取 ≠ 已返回或 QQ 已送达；送达未追踪</small></td>
      <td><button onClick={() => detail(item.question_id)}>查看私有正文</button></td></tr>)}</tbody></DataTable>
}
export function QuestionDetail({ item }: { item: JsonObject }) {
  return <Section title={`私有问答 ${item.question_id}`} description="仅管理员核对；外部资料仅按文本显示，不执行、不复制到公开聊天，不证明原调用已收到。">
    <h3>问题（不可信外部资料）</h3><pre>{item.question}</pre>
    <h3>{outcomes[item.state] ?? '未知处理状态'}</h3>{item.state === 'answered' ? <pre>{item.answer}</pre> : <p>{reasons[item.reason_code] ?? '尚无可用私有答案'}</p>}
    <h3>目标群公开通知</h3>
    {!item.notice ? <p>未触发目标群公开通知；私有回答与原任务领取本身不会自动发送到 QQ。</p> : <p>
      <code>{item.notice.notice_id}</code> · {noticeStates[item.notice.state] ?? '公开通知状态未知'} · {noticeReceiptText(item.notice)} · 目标群 Bot {item.notice.bot_native_id}（{item.notice.bot_identity_source ?? 'unknown'}） · 未保存公开通知正文，且平台接受不等于群友已读；与私有回答是否生成或原任务是否领取相互独立。
    </p>}
    <p>原任务{item.consumed_at == null ? '未记录领取' : '已记录一次领取'}；未追踪结果返回或 QQ 送达。</p>
  </Section>
}

export function QuestionAuditView({ plane }: { plane: ReturnType<typeof useControlPlane> }) {
  const [state, setState] = useState<AuditState>(empty)
  const [endpointDraft, setEndpointDraft] = useState('')
  const reader = useMemo(() => new QuestionAuditReader(plane.query, setState), [plane.query])
  useEffect(() => () => reader.close(), [reader])
  useEffect(() => { if (plane.data.questions) reader.directory(plane.data.questions) }, [reader, plane.data.questions])
  const loading = Object.values(state.busy).some(Boolean)
  return <>
    <Section title="会话连接与私有问答" description="只读审计，管理员登录不替代 QQ 双边确认；不提供建边、撤销、重试或发送按钮。"
      action={<RefreshButton loading={loading} onClick={() => void reader.select(state.account, state.endpoint)} />}>
      <p>先选择完整宿主账号（包含环境）。连接只记录单向同意；active 不保证当前有效。私有正文需显式打开，不加载到列表或普通请求日志。</p>
      <div className="actions"><label>宿主账号 <select aria-label="问答宿主账号" value={state.account} onChange={(event) => { setEndpointDraft(''); void reader.select(event.target.value) }}>
        <option value="">请选择账号</option>{state.accounts.map((account) => <option key={account.account_key} value={account.account_key}>{account.app_id} · {account.environment}</option>)}
      </select></label><button disabled={Boolean(state.busy.accounts) || !state.accountCursor} onClick={() => void reader.read('accounts', state.accountCursor!)}>更多账号</button></div>
      <form className="actions" onSubmit={(event) => { event.preventDefault(); void reader.select(state.account, endpointDraft.trim()) }}>
        <input aria-label="问答物理会话筛选" placeholder="完整 endpoint_key（可选，精确匹配，不借历史别名）" maxLength={1000} value={endpointDraft} onChange={(event) => setEndpointDraft(event.target.value)} />
        <button disabled={!state.account}>筛选本账号的物理会话</button></form>
      {Object.entries(state.errors).map(([lane, message]) => message && <p key={lane} role="alert">{message}</p>)}
      {state.stale && <p role="status">控制面数据已刷新；当前记录可能变化，请手动刷新审计。不会自动打开私有正文。</p>}
      {!state.account && <EmptyState>{plane.data.questions?.available === false ? '问答审计数据库未配置' : '选择账号后才读取连接与问答；不会读取跨账号正文'}</EmptyState>}
    </Section>
    {state.account && <>
      <Section title="所有单向连接" description="实时 seek 分页；保存状态不是当前授权。实际 ask 仍复查双端权限、路由、停止及预算。">
        <QuestionLinks items={state.links?.items ?? []} />
        {!state.busy.links && state.links && !state.links.items?.length && <EmptyState>所选物理范围没有连接</EmptyState>}
        <button disabled={Boolean(state.busy.links) || !state.links?.next_cursor} onClick={() => void reader.read('links', state.links!.next_cursor)}>更早连接</button>
      </Section>
      <Section title="最近私有问答" description="按持久 job-ID 分页；保留首个 ID 上界，新请求请刷新。处理状态实时，不能把领取当作送达。">
        <QuestionRecords items={state.records?.items ?? []} detail={(id) => void reader.read('detail', id)} />
        {!state.busy.records && state.records && !state.records.items?.length && <EmptyState>所选物理范围没有问答</EmptyState>}
        <button disabled={Boolean(state.busy.records) || !state.records?.next_cursor} onClick={() => void reader.read('records', state.records!.next_cursor)}>更早问答</button>
      </Section>
      {state.detail?.item && <QuestionDetail item={state.detail.item} />}
    </>}
  </>
}
