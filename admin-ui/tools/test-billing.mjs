import assert from 'node:assert/strict'
import { createServer } from 'vite'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
try {
  const { BillingRecords, BillingTotals, BillingView } = await server.ssrLoadModule('/src/BillingView.tsx')
  const base = { id: 'TEST_BUCKET', scope_key: 'TEST_SCOPE', label: 'TEST_PROGRAM', person: null, programs: ['TEST_A', 'TEST_B'],
    commands: ['TEST_COMMAND'], shared: true, calls: 1, pending_calls: 0, incomplete_cost_calls: 0,
    input_tokens: '1000', output_tokens: '50', cached_tokens: '600', cache_write_tokens: '0',
    missing_input_calls: 0, missing_output_calls: 0, missing_cache_calls: 0, cost_usd: '0.00132', cost_complete: true, cache_percent: '60.0' }
  const render = (value) => renderToStaticMarkup(createElement(BillingRecords, { items: [value] }))
  const joint = render(base)
  assert.match(joint, /共同费用（只计一笔，不向每人重复分摊）/)
  assert.match(joint, /TEST_A \+ TEST_B/)
  assert.match(joint, /command#TEST_COMMAND/)
  assert.match(joint, /\$0\.00132/)
  assert.match(joint, /60\.0%/)
  const partial = render({ ...base, cost_usd: '0.0004', cost_complete: false, incomplete_cost_calls: 3, pending_calls: 1,
    cache_percent: null, missing_cache_calls: 1, missing_input_calls: 1 })
  assert.match(partial, /≥ \$0\.0004/)
  assert.match(partial, /3 次费用不完整 · 1 次请求未收尾/)
  assert.match(partial, /输入 ≥ 1,000/)
  assert.match(partial, /未知/)
  assert.doesNotMatch(partial, /0%|60\.0%/)
  const precise = render({ ...base, input_tokens: '9007199254740993', cost_usd: '18014398509.481986' })
  assert.match(precise, /9,007,199,254,740,993/)
  assert.match(precise, /18,014,398,509\.481986/)
  assert.doesNotMatch(precise, /9,007,199,254,740,992/)
  assert.match(render({ ...base, input_tokens: '1e100', cost_usd: 'TEST_INVALID' }), /未知/)
  const untrusted = render({ ...base, label: '<script>TEST_LABEL</script>', person: '9007199254740993',
    programs: ['<img src=x onerror=TEST>'], commands: ['<svg onload=TEST>'], actor_key: 'TEST_ACTOR_SECRET',
    source_metadata_json: 'TEST_METADATA_SECRET', api_key: 'TEST_API_SECRET', pricing_json: 'TEST_PRICE_SECRET' })
  assert.match(untrusted, /person#9007199254740993/)
  assert.match(untrusted, /&lt;script&gt;TEST_LABEL/)
  assert.match(untrusted, /&lt;img/)
  assert.doesNotMatch(untrusted, /<script>|<img|<svg|TEST_ACTOR_SECRET|TEST_METADATA_SECRET|TEST_API_SECRET|TEST_PRICE_SECRET/)
  const total = renderToStaticMarkup(createElement(BillingTotals, { totals: base }))
  assert.match(total, /全部分组，不只是本页/)
  assert.match(total, /\$0\.00132/)
  const page = renderToStaticMarkup(createElement(BillingView, { query: () => assert.fail('SSR must not fetch') }))
  for (const label of ['会话', '后台任务', '常驻程序', '发起人']) assert.match(page, new RegExp(`>${label}</option>`))
  assert.match(page, /不把旧 Token 额度预留当实际费用/)
  assert.match(page, /不能跨维度相加/)
  assert.match(page, /下一页 50 组/)
  assert.doesNotMatch(page, /<button[^>]*>批准|<button[^>]*>执行|<button[^>]*>设置预算/)
  console.log('Billing UI: joint attribution, lower bounds, exact decimals/BIGINTs, four dimensions and escaping passed')
} finally { await server.close() }
