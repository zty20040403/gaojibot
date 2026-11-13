import assert from 'node:assert/strict'
import { createServer } from 'vite'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
try {
  const { BrowserDetails, browserClearIntent } = await server.ssrLoadModule('/src/BrowserPanel.tsx')
  const base = { endpoint_key: 'TEST_ENDPOINT', available: true, resource_version: 7, recent: [],
    snapshot: { state: 'ready', fence: 'a'.repeat(64), domains: ['test.example'], truncated: false } }
  const render = (payload, busy = false) => renderToStaticMarkup(createElement(BrowserDetails, {
    endpoint: 'TEST_ENDPOINT', payload, busy, clear: () => assert.fail('render must not clear'),
  }))
  assert.deepEqual(browserClearIntent('TEST_ENDPOINT', base), {
    body: { endpoint_key: 'TEST_ENDPOINT', fence: 'a'.repeat(64), confirmation: 'clear' }, version: 7,
  })
  assert.equal(browserClearIntent('TEST_OTHER', base), null)
  for (const payload of [null, { ...base, endpoint_key: 'TEST_OTHER' }, { ...base, available: false },
    { ...base, resource_version: undefined }, { ...base, resource_version: 0.5 },
    { ...base, snapshot: { ...base.snapshot, state: 'offline' } },
    { ...base, snapshot: { ...base.snapshot, fence: undefined } }]) {
    assert.equal(browserClearIntent('TEST_ENDPOINT', payload), null)
  }
  const normal = render(base)
  assert.match(normal, /test.example/)
  assert.match(normal, /不保证登录仍有效/)
  assert.doesNotMatch(normal, /button[^>]*disabled/)
  assert.match(render(base, true), /button[^>]*disabled/)
  for (const state of ['offline', 'missing']) {
    const html = render({ ...base, snapshot: { state } })
    assert.match(html, /button[^>]*disabled/)
    assert.doesNotMatch(html, /没有未过期 Cookie 记录/)
  }
  const empty = render({ ...base, snapshot: { ...base.snapshot, domains: [] } })
  assert.match(empty, /不代表所有网站均未登录/)
  const unknown = render({ ...base, recent: [{ operation_id: 'TEST_OPERATION', created_at: 1,
    state: 'unknown', error_code: '<script>TEST_FAILURE</script>' }] })
  assert.match(unknown, /结果不明，可能已执行；不自动重试/)
  assert.doesNotMatch(unknown, /已确认清空|<script>/)
  assert.match(unknown, /&lt;script&gt;TEST_FAILURE/)
  const cleared = render({ ...base, recent: [{ operation_id: 'TEST_OPERATION', created_at: 1, state: 'cleared', error_code: '' }] })
  assert.match(cleared, /已确认清空当时的本地资料/)
  const untrusted = render({ ...base, snapshot: { ...base.snapshot, domains: ['<script>TEST_DOMAIN</script>'], cookie: 'TEST_SECRET' } })
  assert.match(untrusted, /&lt;script&gt;TEST_DOMAIN/)
  assert.doesNotMatch(untrusted, /TEST_SECRET|<script>/)
  console.log('Browser UI: scope/version/fence, offline/unknown, result semantics and escaping checks passed')
} finally {
  await server.close()
}
