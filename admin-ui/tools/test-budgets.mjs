import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
try {
  const { BudgetsPanel, BudgetEditor, beginBudgetDraft, budgetRequest } = await server.ssrLoadModule('/src/BudgetsPanel.tsx')
  const { RESOURCE_PATHS, EVENT_RESOURCES } = await server.ssrLoadModule('/src/api.ts')
  const row = { endpoint_key: 'TEST_GROUP', kind: 'group', daily_budget_usd: '999999.000001', spent_usd: '999999.999999999999', cost_is_lower_bound: true, incomplete_calls: 2, pending_calls: 1, blocked: true, stopped: true, notice_state: 'unknown' }
  const plane = { data: { budgets: { items: [row], resource_version: 7, available: true } }, versions: { budgets: 99 }, loading: new Set() }
  assert.equal(RESOURCE_PATHS.budgets, '/conversations/budgets')
  assert.deepEqual(EVENT_RESOURCES.budgets, ['budgets'])
  const markup = renderToStaticMarkup(createElement(BudgetsPanel, { plane }))
  assert.match(markup, /999999\.000001/)
  assert.match(markup, /≥.*999999\.999999999999/)
  assert.match(markup, /结果不明，不重试/)
  assert.match(markup, /预算限制中/)
  assert.match(markup, /\/stop仍开启/)
  assert.match(markup, /不是供应商硬限额/)
  assert.doesNotMatch(markup, /已确认送达/)
  const escaped = renderToStaticMarkup(createElement(BudgetsPanel, { plane: { ...plane, data: { budgets: { items: [{ ...row, endpoint_key: 'TEST_<script>alert(1)</script>' }], resource_version: 7 } } } }))
  assert.doesNotMatch(escaped, /<script>alert/)
  const draft = beginBudgetDraft(row, 7)
  const changed = beginBudgetDraft({ ...row, daily_budget_usd: '100' }, 8)
  assert.equal(changed.version, 8)
  assert.deepEqual(budgetRequest(draft), { body: { endpoint_key: 'TEST_GROUP', daily_budget_usd: '999999.000001' }, version: 7 })
  assert.equal(budgetRequest({ ...draft, amount: '0' }).body.daily_budget_usd, '0')
  assert.equal(budgetRequest({ ...draft, enabled: false }).body.daily_budget_usd, null)
  assert.match(renderToStaticMarkup(createElement(BudgetEditor, { draft: { ...draft, amount: '0.000000' }, onChange() {}, onSave() {}, onCancel() {}, busy: false, stale: false })), /0会禁止自动动作/)
  for (const amount of ['', 'NaN', '1e2', '-1', '0.0000001', '1000000.000001', '1000001', ' 1']) assert.throws(() => budgetRequest({ ...draft, amount }))
  for (const version of [undefined, null, -1, '7', Number.MAX_SAFE_INTEGER + 1]) assert.throws(() => beginBudgetDraft(row, version))
  function* elements(node) {
    if (Array.isArray(node)) { for (const child of node) yield* elements(child); return }
    if (!node || typeof node !== 'object' || !node.props) return
    yield node; yield* elements(node.props.children)
  }
  let updated, saved = 0
  const props = { draft, onChange: (value) => { updated = value }, onSave: () => { saved++ }, onCancel() {}, busy: false, stale: false }
  const editor = [...elements(BudgetEditor(props))]
  editor.find((node) => node.props['aria-label'] === '美元日预算').props.onChange({ target: { value: '0.000001' } })
  assert.equal(updated.amount, '0.000001'); assert.equal(updated.version, 7)
  editor.find((node) => node.type === 'button' && node.props.children === '保存预算').props.onClick()
  assert.equal(saved, 1)
  const stale = [...elements(BudgetEditor({ ...props, stale: true }))]
  assert.equal(stale.find((node) => node.type === 'button' && node.props.children === '保存预算').props.disabled, true)
  assert.match(renderToStaticMarkup(BudgetEditor({ ...props, stale: true })), /旧表单不会使用新版本覆盖/)
  const noVersion = renderToStaticMarkup(createElement(BudgetsPanel, { plane: { ...plane, data: { budgets: { items: [row] } } } }))
  assert.match(noVersion, /<button disabled="">设置预算/)
  console.log('Budget UI: exact money, off vs zero, displayed/frozen versions, stale/missing guards and honest notice outcomes passed')
} finally { await server.close() }
