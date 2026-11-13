import assert from 'node:assert/strict'
import { createServer } from 'vite'
import { renderToStaticMarkup } from 'react-dom/server'

const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
try {
  const { MediaView } = await server.ssrLoadModule('/src/views.tsx')
  const { EVENT_RESOURCES } = await server.ssrLoadModule('/src/api.ts')
  const calls = []
  const media = { caption_resource_version: 7, caption_settings: [{ scope_key: 'TEST_SCOPE', image_captions: false, image_daily_limit: 200 }] }
  const plane = { data: { media }, versions: { media: 31, captions: 99 }, mutate: (...args) => { calls.push(args); return Promise.resolve({}) } }
  const tree = MediaView({ plane, onOpenDetail: () => assert.fail('unexpected navigation') })
  function* elements(node) {
    if (Array.isArray(node)) { for (const child of node) yield* elements(child); return }
    if (!node || typeof node !== 'object' || !node.props) return
    yield node
    yield* elements(node.props.children)
  }
  const all = [...elements(tree)]
  const toggle = all.find((node) => node.props.label === '图片说明')
  const limit = all.find((node) => node.props['aria-label'] === '每日图片上限 TEST_SCOPE')
  assert.equal(toggle.props.disabled, false)
  toggle.props.onChange(true)
  limit.props.onBlur({ currentTarget: { value: '3' } })
  assert.equal(calls.length, 2)
  for (const args of calls) {
    assert.equal(args[0], 'captions')
    assert.equal(args[1], '/media/captions/settings')
    assert.equal(args[5], 7) // the displayed snapshot, not a later global version
  }
  assert.match(renderToStaticMarkup(tree), /TEST_SCOPE/)
  const missing = MediaView({ plane: { ...plane, data: { media: { ...media, caption_resource_version: undefined } } }, onOpenDetail() {} })
  for (const element of elements(missing)) {
    if (element.props.label === '图片说明' || element.props['aria-label'] === '每日图片上限 TEST_SCOPE') assert.equal(element.props.disabled, true)
  }
  assert.deepEqual(EVENT_RESOURCES.captions, ['media'])
  console.log('Caption UI: independent snapshot version, missing-version disable and refresh mapping passed')
} finally { await server.close() }
