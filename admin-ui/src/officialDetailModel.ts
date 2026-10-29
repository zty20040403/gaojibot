export type DetailSection = 'executions' | 'deliveries' | 'bindings';
export type DetailRecord = Record<string, any>;
export type DetailPage = { section: DetailSection; observed_at: number; checkpoint: DetailRecord; items: DetailRecord[]; next_cursor: string | null; truncated: boolean; bounded_rows: number | null };
const sections = ['executions', 'deliveries', 'bindings'];
const integer = (value: unknown) => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null;
const marker = (value: unknown) => typeof value === 'string' && /^[a-zA-Z0-9:_-]{1,128}$/.test(value) ? value : null;
const object = (value: unknown): DetailRecord => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as DetailRecord : {};
const tokenKeys = ['ref', 'plan_ref', 'endpoint_ref', 'actor_ref', 'admin_ref', 'state', 'kind', 'result_status', 'reason', 'wait_reason', 'membership', 'native_ref', 'acceptance'];
const timeKeys = ['deadline_at', 'result_deadline_at', 'created_at', 'updated_at', 'expires_at', 'next_attempt_at', 'part_index', 'part_count', 'attempt_count', 'observed_at', 'applied_at', 'send_by_at', 'revision', 'generation', 'route_epoch', 'capability_revision'];
const boolKeys = ['current', 'owner_current', 'ref_index_present', 'receipts_truncated'];
function record(value: unknown): DetailRecord {
  const input = object(value), result: DetailRecord = {};
  for (const key of tokenKeys) result[key] = marker(input[key]);
  for (const key of timeKeys) result[key] = integer(input[key]);
  for (const key of boolKeys) result[key] = typeof input[key] === 'boolean' ? input[key] : null;
  return result;
}
export function detailsPath(section: DetailSection, cursor: string | null) {
  if (!sections.includes(section) || cursor !== null && (typeof cursor !== 'string' || cursor.length > 4096 || !/^[a-zA-Z0-9_.=-]+$/.test(cursor))) throw new Error('invalid_details_request');
  const query = new URLSearchParams({ section, limit: '20' });
  if (cursor) query.set('cursor', cursor);
  return `/qq-official/details?${query.toString()}`;
}
export function checkedPage(value: unknown, expected: DetailSection): DetailPage {
  const page = object(value);
  if (page.section !== expected || integer(page.observed_at) === null || !Array.isArray(page.items) || page.items.length > 50
      || page.next_cursor !== null && (typeof page.next_cursor !== 'string' || !page.next_cursor || page.next_cursor.length > 4096 || !/^[a-zA-Z0-9_.=-]+$/.test(page.next_cursor))) throw new Error('invalid_details_response');
  const input = object(page.checkpoint);
  const checkpoint = { session_present: typeof input.session_present === 'boolean' ? input.session_present : null,
    durable_seq: integer(input.durable_seq), fencing_epoch: integer(input.fencing_epoch), gap: typeof input.gap === 'boolean' ? input.gap : null,
    gap_reason: marker(input.gap_reason), updated_at: integer(input.updated_at) };
  const items = page.items.map((value: unknown) => {
    const input = object(value), result = record(input);
    result.admin_binding = input.admin_binding ? record(input.admin_binding) : null;
    result.attempt = input.attempt ? record(input.attempt) : null;
    result.receipts = Array.isArray(input.receipts) ? input.receipts.slice(0, 50).map(record) : [];
    if (Array.isArray(input.receipts) && input.receipts.length > 50) result.receipts_truncated = true;
    return result;
  });
  return { section: expected, observed_at: page.observed_at, checkpoint, items, next_cursor: page.next_cursor, truncated: page.truncated === true, bounded_rows: integer(page.bounded_rows) };
}
