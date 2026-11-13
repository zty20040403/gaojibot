import assert from 'node:assert/strict'
import { createServer } from 'vite'
import { renderToStaticMarkup } from 'react-dom/server'

const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
try {
  const { GroupsView } = await server.ssrLoadModule('/src/views.tsx')
  const calls = []
  const items = [
    { endpoint_key: 'TEST_GROUP', kind: 'group', proactive_enabled: true, stopped: true },
    { endpoint_key: 'TEST_PRIVATE', kind: 'private', proactive_enabled: true, stopped: false },
  ]
  const plane = { data: { conversations: { items, resource_version: 3 } }, loading: new Set(), versions: { conversations: 99 },
    mutate: (...args) => { calls.push(args); return Promise.resolve({}) } }
  function* elements(node) {
    if (Array.isArray(node)) { for (const child of node) yield* elements(child); return }
    if (!node || typeof node !== 'object' || !node.props) return
    yield node
    yield* elements(node.props.children)
  }
  const tree = GroupsView({ plane })
  const all = [...elements(tree)]
  const group = all.find((node) => node.props.label === '主动接话 TEST_GROUP')
  const privateChat = all.find((node) => node.props.label === '主动接话 TEST_PRIVATE')
  assert.equal(group.props.disabled, false)
  assert.equal(privateChat.props.disabled, true)
  group.props.onChange(false)
  assert.deepEqual(calls[0], ['conversations', '/conversations/proactive', 'POST', { endpoint_key: 'TEST_GROUP', enabled: false }, ['conversations'], 3])
  all.find((node) => node.type === 'button' && node.props.children === '恢复会话').props.onClick()
  assert.equal(calls[1][5], 3) // use displayed state, never a newer global version
  const markup = renderToStaticMarkup(tree)
  assert.match(markup, /已停止/)
  assert.match(markup, /不恢复已停止会话/)
  const missing = GroupsView({ plane: { ...plane, data: { conversations: { items } } } })
  for (const node of elements(missing)) {
    if (node.props.label?.startsWith('主动接话 ') || node.type === 'button' && node.props.children === '恢复会话') assert.equal(node.props.disabled, true)
  }
  console.log('Conversation UI: independent proactive toggle, displayed version and missing-version guards passed')
} finally { await server.close() }
