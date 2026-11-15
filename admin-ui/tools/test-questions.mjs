import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
try {
  const { QuestionAuditReader, QuestionLinks, QuestionRecords, QuestionDetail, QuestionAuditView } = await server.ssrLoadModule('/src/QuestionAuditView.tsx')
  const { AdminApiError, RESOURCE_PATHS, EVENT_RESOURCES } = await server.ssrLoadModule('/src/api.ts')
  assert.equal(RESOURCE_PATHS.questions, '/conversation-questions')
  assert.deepEqual(EVENT_RESOURCES.questions, ['questions'])
  const qid = `CQ-${'a'.repeat(32)}`, otherId = `CQ-${'b'.repeat(32)}`, nid = `QN-${'c'.repeat(32)}`, lateNid = `QN-${'d'.repeat(32)}`
  const source = { kind: 'private', native_id: 'TEST_SOURCE<script>bad()</script>' }, target = { kind: 'group', native_id: 'TEST_TARGET' }
  const notice = { notice_id: nid, state: 'committed', delivery_state: 'committed', receipt_recorded: true,
    delivery_received_at: 4, bot_native_id: 'TEST_BOT<script>n()</script>', bot_identity_source: 'ready', created_at: 2, updated_at: 4 }
  const item = { question_id: qid, job_id: '9223372036854775806', link_id: 'TEST_LINK', link_generation: 2,
    state: 'answered', reason_code: '', created_at: 1, deadline_at: 7201, finished_at: 2, consumed_at: 3, source, target,
    notice: null, question: 'TEST_PRIVATE_QUESTION<script>bad()</script>', answer: 'TEST_PRIVATE_ANSWER<img onerror=bad()>', request_hmac: 'TEST_SECRET_HMAC' }
  const html = renderToStaticMarkup(createElement(QuestionRecords, { items: [item], detail() {} }))
  assert.match(html, /9223372036854775806/)
  assert.match(html, /私有回答已生成/)
  assert.match(html, /未触发目标群公开通知/)
  assert.match(html, /原任务已领取/)
  assert.match(html, /已领取 ≠ 已返回或 QQ 已送达/)
  assert.doesNotMatch(html, /TEST_PRIVATE_QUESTION|TEST_PRIVATE_ANSWER|TEST_SECRET_HMAC|<script>/)
  assert.match(html, /&lt;script&gt;bad/)
  const noticedHtml = renderToStaticMarkup(createElement(QuestionRecords, { items: [{ ...item, notice }], detail() {} }))
  assert.match(noticedHtml, /公开通知已授权提交/)
  assert.match(noticedHtml, /平台已接受主动投递（不等于群友已读）/)
  assert.match(noticedHtml, /&lt;script&gt;n/)
  assert.doesNotMatch(noticedHtml, /<script>/)
  const lateHtml = renderToStaticMarkup(createElement(QuestionRecords, { items: [{ ...item, state: 'cancelled', consumed_at: null,
    notice: { ...notice, notice_id: lateNid, state: 'cancelled', delivery_state: 'committed', receipt_recorded: true } }], detail() {} }))
  assert.match(lateHtml, /公开通知已取消/)
  assert.match(lateHtml, /已保存迟到平台接受回执（未恢复原授权，不等于群友已读）/)
  assert.match(lateHtml, /未记录原任务领取/)
  function* elements(node) {
    if (Array.isArray(node)) { for (const child of node) yield* elements(child); return }
    if (!node?.props) return
    yield node; yield* elements(node.props.children)
  }
  let requested
  const button = [...elements(QuestionRecords({ items: [item], detail: (id) => { requested = id } }))].find((node) => node.type === 'button')
  button.props.onClick(); assert.equal(requested, qid, 'the actual row handler only requests the opaque CQ-ID')
  const detail = renderToStaticMarkup(createElement(QuestionDetail, { item: { ...item, notice } }))
  assert.match(detail, /TEST_PRIVATE_QUESTION/)
  assert.match(detail, /TEST_PRIVATE_ANSWER/)
  assert.match(detail, /目标群公开通知/)
  assert.match(detail, /平台已接受主动投递（不等于群友已读）/)
  assert.match(detail, /未保存公开通知正文，且平台接受不等于群友已读/)
  assert.match(detail, /未追踪结果返回或 QQ 送达/)
  assert.doesNotMatch(detail, /<script>|<img |TEST_SECRET_HMAC|已确认送达|(?<!不等于)群友已读/)
  for (const state of ['refused', 'expired', 'cancelled', 'unknown', 'queued', 'running']) {
    const markup = renderToStaticMarkup(createElement(QuestionRecords, { items: [{ ...item, state, consumed_at: null }], detail() {} }))
    assert.match(markup, /未记录原任务领取/)
    assert.doesNotMatch(markup, /私有回答已生成|已确认送达/)
  }
  const links = renderToStaticMarkup(createElement(QuestionLinks, { items: [{ link_id: 'TEST_LINK', source, target, state: 'active',
    generation: 7, created_at: 1, receipt_recorded: true, issue_state: 'committed' }] }))
  assert.match(links, /当前授权另验/)
  assert.match(links, /不是新的访问授权/)
  assert.doesNotMatch(links, /<script>/)
  assert.match(renderToStaticMarkup(createElement(QuestionLinks, { items: [{ link_id: 'TEST_OLD_LINK', source, target, state: 'revoked',
    generation: 8, created_at: 1, revoked_at: 2, receipt_recorded: false }] })), /已撤销.*无有效发行回执/s)
  assert.match(renderToStaticMarkup(createElement(QuestionAuditView, { plane: { data: {} } })), /选择账号后才读取/)

  const calls = [], snapshots = []
  const query = (path, signal) => new Promise((resolve, reject) => calls.push({ path, signal, resolve, reject }))
  const reader = new QuestionAuditReader(query, (state) => snapshots.push(state))
  reader.directory({ accounts: [{ account_key: 'TEST_A', app_id: 'TEST_APP', environment: 'test' }], next_cursor: 'TEST_DIRECTORY_CURSOR' })
  assert.equal(calls.length, 0, 'directory must not load private records')
  const a = reader.select('TEST_A')
  assert.equal(calls.length, 2)
  assert(calls.every((call) => call.path.includes('account_key=TEST_A')))
  calls[0].resolve({ items: [], next_cursor: null }); calls[1].resolve({ items: [item], next_cursor: 'TEST_QUESTION_CURSOR' }); await a
  assert.equal(reader.state.detail, null, 'record metadata must not auto-fetch body')
  const visible = reader.read('detail', qid); calls.at(-1).resolve({ item }); await visible
  assert.equal(reader.state.detail.item.answer, item.answer)
  await reader.select('')
  assert.equal(reader.state.detail, null, 'clearing the selected account must erase even a completed private body')
  const again = reader.select('TEST_A'), againCalls = calls.slice(-2)
  againCalls[0].resolve({ items: [] }); againCalls[1].resolve({ items: [item] }); await again
  const old = reader.read('detail', qid), oldCall = calls.at(-1)
  const b = reader.select('TEST_B', 'TEST_PHYSICAL_ENDPOINT')
  assert.equal(reader.state.detail, null)
  assert.equal(reader.state.records, null)
  assert.equal(oldCall.signal.aborted, true)
  oldCall.resolve({ item }); await old
  assert.equal(reader.state.detail, null, 'ignored abort must not refill account B with account A body')
  assert.equal(reader.state.account, 'TEST_B')
  const bCalls = calls.slice(-2)
  assert(bCalls.every((call) => call.path.includes('account_key=TEST_B') && call.path.includes('endpoint_key=TEST_PHYSICAL_ENDPOINT')))
  bCalls[0].resolve({ items: [], next_cursor: null }); bCalls[1].resolve({ items: [], next_cursor: null }); await b
  const first = reader.read('detail', qid), firstCall = calls.at(-1)
  const second = reader.read('detail', otherId), secondCall = calls.at(-1)
  secondCall.resolve({ item: { ...item, question_id: otherId, answer: 'TEST_NEW_DETAIL' } }); await second
  firstCall.resolve({ item }); await first
  assert.equal(reader.state.detail.item.answer, 'TEST_NEW_DETAIL', 'same-lane stale success must be rejected too')
  const page = reader.read('records', 'TEST_BOUND_CURSOR'), pageCall = calls.at(-1)
  assert.equal(reader.state.detail, null, 'paging clears an old explicit body')
  assert.match(pageCall.path, /after=TEST_BOUND_CURSOR/)
  pageCall.resolve({ items: [], next_cursor: null }); await page
  const more = reader.read('accounts', reader.state.accountCursor), moreCall = calls.at(-1)
  assert.match(moreCall.path, /after=TEST_DIRECTORY_CURSOR/)
  moreCall.resolve({ accounts: [{ account_key: 'TEST_B', app_id: 'TEST_OTHER', environment: 'production' }], next_cursor: null }); await more
  assert.equal(reader.state.accounts.length, 2)
  const fail = reader.read('detail', qid); calls.at(-1).reject(new AdminApiError(403, 'TEST_REVOKED')); await fail
  assert.equal(reader.state.account, '')
  assert.equal(reader.state.detail, null)
  assert.equal(reader.state.records, null)
  assert.match(reader.state.errors.detail, /已清除私有审计/)
  const pending = reader.select('TEST_A'), pendingCalls = calls.slice(-2)
  reader.close()
  pendingCalls.forEach((call) => call.resolve({ items: [item] })); await pending
  assert.equal(reader.state.account, '')
  assert.equal(reader.state.records, null)
  assert.equal(reader.state.detail, null)
  assert(snapshots.every((state) => state.account !== 'TEST_B' || state.detail?.item?.question_id !== qid), 'late A payload must never appear in a B snapshot')
  console.log('Question audit UI: explicit body, honest states, exact IDs, escaped text, account/physical scope, stale races, paging, auth loss and close passed')
} finally { await server.close() }
