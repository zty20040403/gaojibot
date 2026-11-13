import assert from 'node:assert/strict'
import { createServer } from 'vite'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
try {
  const { OperationsRecords, OperationsView } = await server.ssrLoadModule('/src/OperationsView.tsx')
  const base = { action_id: 'TEST_ACTION', scope_key: 'TEST_PRIVATE_SCOPE', job: 'job#1', request_id: 'TEST_REQUEST',
    created_at: 1, updated_at: 2, expires_at: 601, plan_name: 'TEST_PLAN', parameters: { unit: 'TEST.service' },
    requester: { person: 42, display: 'TEST_OWNER' }, approver: null, notice_status: null, state: 'sending', output_summary: '' }
  const render = (value) => renderToStaticMarkup(createElement(OperationsRecords, { items: [value] }))
  assert.match(render(base), /批准通知尚未确认/)
  assert.match(render(base), /person#42/)
  assert.match(render(base), /无已确认的批准人/)
  assert.match(render({ ...base, state: 'approved', approver: base.requester, approved_at: 2, notice_status: 'committed' }), /已批准，尚未执行/)
  assert.match(render({ ...base, state: 'executing' }), /执行中，结果尚未确认/)
  const unknown = render({ ...base, state: 'unknown', notice_status: 'ambiguous', output_summary: '<script>TEST_REMOTE</script>' })
  assert.match(unknown, /结果不明，可能已执行；不自动重试/)
  assert.match(unknown, /通知结果不明/)
  assert.match(unknown, /&lt;script&gt;TEST_REMOTE/)
  assert.doesNotMatch(unknown, /操作执行成功|没有执行|<script>|<button/)
  assert.match(render({ ...base, state: 'succeeded' }), /操作执行成功（健康另验）/)
  assert.match(render({ ...base, state: 'denied' }), /未批准，没有执行/)
  assert.match(render({ ...base, state: 'expired' }), /批准已过期，没有执行/)
  const untrusted = render({ ...base, plan_name: '<script>TEST_PLAN</script>', parameters: { unit: '<img src=x onerror=TEST>' },
    requester: { display: '<script>TEST_OWNER</script>' }, code_hash: 'TEST_CODE_SECRET', actor_key: 'TEST_NATIVE_SECRET', remote_contract: 'TEST_CONTRACT_SECRET' })
  assert.doesNotMatch(untrusted, /<script>|<img|TEST_CODE_SECRET|TEST_NATIVE_SECRET|TEST_CONTRACT_SECRET/)
  assert.match(untrusted, /&lt;img/)
  const page = renderToStaticMarkup(createElement(OperationsView, { query: () => assert.fail('SSR must not fetch or execute') }))
  assert.match(page, /管理员登录不能代替/)
  assert.match(page, /状态变化请刷新|运维操作审计/)
  assert.doesNotMatch(page, /<button[^>]*>批准|<button[^>]*>执行|<button[^>]*>重试/)
  console.log('Operations UI: approval/result semantics, explicit fields, read-only controls and escaping passed')
} finally { await server.close() }
