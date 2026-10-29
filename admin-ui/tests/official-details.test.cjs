const test = require('node:test');
const assert = require('node:assert/strict');
const { checkedPage, detailsPath } = require('../src/officialDetailModel.ts');
const page = (patch = {}) => ({ section: 'deliveries', observed_at: 100, checkpoint: { durable_seq: 3, fencing_epoch: 1, session_present: true, gap: false, updated_at: 99 }, items: [], next_cursor: null, bounded_rows: 1000, truncated: false, ...patch });

test('request uses exact protected endpoint, bounded limit and opaque cursor', () => {
  const url = detailsPath('deliveries', 'abc_DEF-123');
  assert.equal(url, '/qq-official/details?section=deliveries&limit=20&cursor=abc_DEF-123');
  for (const cursor of ['../account', 'a&account=other', 'https://example.com', 'x'.repeat(4097)]) assert.throws(() => detailsPath('deliveries', cursor));
  assert.throws(() => detailsPath('private-table', null));
});

test('raw identity/body/errors/URL are not carried into detail view model', () => {
  const value = checkedPage(page({ items: [{ ref: 'delivery_abcd', endpoint_ref: 'endpoint_abcd', state: 'ambiguous', wait_reason: 'OTHER PRIVATE BODY', native_message_id: 'RAW_OPENID', raw_url: 'https://private.invalid/token', body: 'secret body', receipts: [{ ref: 'receipt_x', native_ref: 'message_hash', kind: 'response', raw_body: 'RAW_SECRET' }] }] }), 'deliveries');
  const encoded = JSON.stringify(value);
  for (const text of ['RAW_OPENID', 'private.invalid', 'secret body', 'RAW_SECRET', 'OTHER PRIVATE BODY']) assert.ok(!encoded.includes(text));
  assert.equal(value.items[0].state, 'ambiguous');
  assert.equal(value.items[0].receipts[0].native_ref, 'message_hash');
});

test('real two-part receipt model keeps accepted and ambiguous separate', () => {
  const value = checkedPage(page({ items: [{ ref: 'delivery_a', plan_ref: 'plan_x', part_index: 0, part_count: 2, state: 'committed', receipts: [{ref:'receipt_a',kind:'response',observed_at:100,applied_at:101}] }, { ref: 'delivery_b', plan_ref: 'plan_x', part_index: 1, part_count: 2, state: 'ambiguous', expires_at:200, attempt: {state:'ambiguous',acceptance:'unknown',send_by_at:150},receipts:[] }] }), 'deliveries');
  assert.equal(value.items[0].state, 'committed'); assert.equal(value.items[1].state, 'ambiguous');
  assert.equal(value.items[1].attempt.acceptance, 'unknown'); assert.equal(value.items[1].expires_at, 200);
  assert.equal(value.items[1].receipts.length, 0);
});

test('wrong page, unbounded data and unknown expiry cannot pass as a successful page', () => {
  assert.throws(() => checkedPage(page(), 'executions'));
  assert.throws(() => checkedPage(page({ items: Array(51).fill({}) }), 'deliveries'));
  assert.throws(() => checkedPage(page({ next_cursor: '../raw' }), 'deliveries'));
  const value = checkedPage(page({ checkpoint: { durable_seq: true, session_present: 'yes', gap: null }, items: [{ expires_at: NaN, attempt_count: -1 }] }), 'deliveries');
  assert.equal(value.checkpoint.durable_seq, null);assert.equal(value.checkpoint.session_present, null);
  assert.equal(value.items[0].expires_at, null); assert.equal(value.items[0].attempt_count, null);
});

test('execution preserves original result deadline and nullable pending result', () => {
  const value = checkedPage(page({ section:'executions', items:[{kind:'subagent',state:'running',deadline_at:180,result_deadline_at:null},{kind:'subagent',state:'completed',deadline_at:180,result_deadline_at:200}] }), 'executions');
  assert.equal(value.items[0].result_deadline_at, null); assert.equal(value.items[1].result_deadline_at, 200);
});

test('binding status preserves revoked evidence instead of inferring permission', () => {
  const value = checkedPage(page({ section:'bindings',items:[{ref:'endpoint_a',kind:'group',state:'active',admin_binding:null},{ref:'endpoint_b',kind:'private',state:'unbound',admin_binding:{ref:'binding_a',revision:2,state:'revoked',current:false,owner_current:false}}] }), 'bindings');
  assert.equal(value.items[0].admin_binding,null); assert.equal(value.items[1].admin_binding.current,false);
});

test('pagination and truncation remain explicit facts', () => {
  const value = checkedPage(page({ next_cursor:'c_123',truncated:true,bounded_rows:1000 }), 'deliveries');
  assert.equal(value.next_cursor, 'c_123'); assert.equal(value.truncated, true); assert.equal(value.bounded_rows, 1000);
});
