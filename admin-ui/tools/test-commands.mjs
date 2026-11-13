import assert from 'node:assert/strict'
import { createServer } from 'vite'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
try {
  const { CommandRows } = await server.ssrLoadModule('/src/CommandsPanel.tsx')
  const base = { command_id: 'TEST_COMMAND', endpoint_key: 'TEST_ENDPOINT', program_name: 'TEST_PROGRAM',
    created_at: 1, deadline_at: 21601, output_path: '/work/.gaoji/out/TEST.txt', output_bytes: 25,
    output_head: '<script>TEST_UNTRUSTED</script>', output_tail: 'TEST_TAIL' }
  const render = (changes) => renderToStaticMarkup(createElement(CommandRows, {
    items: [{ ...base, ...changes }], busy: false, cancel: () => assert.fail('render must not mutate'),
  }))
  const denied = render({ state: 'completed', finished_at: 2, exit_code: 0, elapsed_seconds: 1,
    notification: { state: 'failed', error_code: 'TEST_DENIED' } })
  assert.match(denied, /进程已结束/)
  assert.match(denied, /退出码 0/)
  assert.match(denied, /通知 failed，未确认送达/)
  assert.match(denied, /TEST_DENIED/)
  assert.doesNotMatch(denied, /已有平台回执|停止命令|<script>/)
  assert.match(denied, /&lt;script&gt;TEST_UNTRUSTED/)
  const uncertain = render({ state: 'ambiguous', finished_at: null, exit_code: null })
  assert.match(uncertain, /结果不明，保留名额/)
  assert.match(uncertain, /退出码 未知/)
  assert.match(uncertain, /停止命令/)
  const stopping = render({ state: 'stopping', finished_at: null, cancel_requested: true })
  assert.match(stopping, /等待确认停止/)
  assert.match(stopping, /button disabled/)
  assert.match(render({ notification: { state: 'completed', silent: true } }), /模型保持静默，未发送消息/)
  assert.match(render({ notification: { state: 'completed', silent: false } }), /已有平台回执/)
  console.log('CommandRows: exit, receipt, unknown stop and HTML-escaping checks passed')
} finally {
  await server.close()
}
