import { useEffect, useState } from 'react';
import type { useControlPlane } from './useControlPlane';
import { checkedPage, detailsPath, type DetailPage, type DetailSection } from './officialDetailModel';

type Plane = ReturnType<typeof useControlPlane>;
const date = (value: unknown) => typeof value === 'number' && Number.isSafeInteger(value) ? new Date(value * 1000).toLocaleString('zh-CN') : '未知';
const flag = (value: unknown) => value === true ? '是' : value === false ? '否' : '未知';
const show = (value: unknown) => typeof value === 'string' || typeof value === 'number' ? value : '未知';
const stateName: Record<string, string> = { pending: '等待', running: '执行中', executing: '执行中', result_ready: '结果已保存', handed_off: '已交给发送器', accepted: '平台已接受', committed: '回执已记录', completed: '已完成', ambiguous: '结果不明', unknown: '结果不明', cancelled: '已取消', failed: '失败', active: '有效', unbound: '未绑定', revoked: '已撤销' };
const state = (value: string | null) => value ? stateName[value] || value : '未知';

export function OfficialDetails({ plane }: { plane: Plane }) {
  const [section, setSection] = useState<DetailSection>('deliveries');
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [page, setPage] = useState<DetailPage | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const cursor = cursors[cursors.length - 1];
  const snapshot = plane.data.qqOfficial;
  useEffect(() => {
    const abort = new AbortController();
    setLoading(true); setError('');
    void plane.query(detailsPath(section, cursor), abort.signal).then(value => {
      if (!abort.signal.aborted) setPage(checkedPage(value, section));
    }).catch(() => { if (!abort.signal.aborted) { setPage(null); setError('明细暂不可用、游标已过期或登录状态已变化。可返回首屏重试。'); } })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [plane.query, section, cursor, snapshot, refresh]);
  const visible = page?.section === section ? page : null;
  const checkpoint = visible?.checkpoint;
  return <section className="panel official-details"><h2>恢复点与持久明细</h2>
    <p>引用是受保护的稳定代号。这里只读核对事实；“平台接受”不表示用户已阅读，“回执已记录”不表示文件已下载。</p>
    <label>查看 <select value={section} onChange={event => { setSection(event.target.value as DetailSection); setCursors([null]); setPage(null); }}><option value="deliveries">发送分片与回执</option><option value="executions">回合执行</option><option value="bindings">当前端点与身份绑定</option></select></label>{' '}
    <button type="button" disabled={loading} onClick={() => setRefresh(value => value + 1)}>刷新当前页</button>
    {error && <p role="alert">{error}</p>}
    {checkpoint && <dl className="official-grid"><div><dt>可恢复会话</dt><dd>{flag(checkpoint.session_present)}</dd></div><div><dt>持久序号</dt><dd>{show(checkpoint.durable_seq)}</dd></div><div><dt>锁世代</dt><dd>{show(checkpoint.fencing_epoch)}</dd></div><div><dt>消息缺口</dt><dd>{flag(checkpoint.gap)}</dd></div><div><dt>缺口原因</dt><dd>{show(checkpoint.gap_reason)}</dd></div><div><dt>恢复点更新时间</dt><dd>{date(checkpoint.updated_at)}</dd></div></dl>}
    <div aria-busy={loading}>{visible?.items.map((item, index) => <article className="panel" key={item.ref || index}>
      <p><code>{show(item.ref)}</code> · {state(item.state)} · 端点 <code>{show(item.endpoint_ref || (section === 'bindings' ? item.ref : null))}</code></p>
      {section === 'deliveries' && <>
        <dl className="official-grid"><div><dt>计划</dt><dd>{show(item.plan_ref)}</dd></div><div><dt>分片序号 / 数量</dt><dd>{typeof item.part_index === 'number' ? item.part_index + 1 : '未知'} / {show(item.part_count)}</dd></div><div><dt>等待原因</dt><dd>{show(item.wait_reason)}</dd></div><div><dt>绝对截止</dt><dd>{date(item.expires_at)}</dd></div><div><dt>下次可检查</dt><dd>{date(item.next_attempt_at)}</dd></div><div><dt>实际尝试次数</dt><dd>{show(item.attempt_count)}</dd></div></dl>
        {item.attempt && <p>当前尝试：{state(item.attempt.state)}；接受判定：{show(item.attempt.acceptance)}；发送截止：{date(item.attempt.send_by_at)}</p>}
        <div className="official-detail-table"><table><thead><tr><th>回执代号</th><th>类型</th><th>观察时间</th><th>入账时间</th><th>平台消息代号</th><th>引用索引</th></tr></thead><tbody>{item.receipts.map((receipt: any, i: number) => <tr key={receipt.ref || i}><td>{show(receipt.ref)}</td><td>{show(receipt.kind)}</td><td>{date(receipt.observed_at)}</td><td>{date(receipt.applied_at)}</td><td>{show(receipt.native_ref)}</td><td>{flag(receipt.ref_index_present)}</td></tr>)}</tbody></table></div>
        {!item.receipts.length && <p>暂无已持久回执；不会因此自动重发。</p>}{item.receipts_truncated && <p>仅显示有界回执记录。</p>}
      </>}
      {section === 'executions' && <dl className="official-grid"><div><dt>类型</dt><dd>{show(item.kind)}</dd></div><div><dt>结果</dt><dd>{show(item.result_status)}</dd></div><div><dt>原因</dt><dd>{show(item.reason)}</dd></div><div><dt>执行原截止</dt><dd>{date(item.deadline_at)}</dd></div><div><dt>结果原截止</dt><dd>{date(item.result_deadline_at)}</dd></div><div><dt>输出计划</dt><dd>{show(item.plan_ref)}</dd></div><div><dt>更新时间</dt><dd>{date(item.updated_at)}</dd></div></dl>}
      {section === 'bindings' && <><dl className="official-grid"><div><dt>端点类型</dt><dd>{show(item.kind)}</dd></div><div><dt>成员状态</dt><dd>{show(item.membership)}</dd></div><div><dt>端点世代</dt><dd>{show(item.generation)}</dd></div><div><dt>路由世代</dt><dd>{show(item.route_epoch)}</dd></div><div><dt>能力版本</dt><dd>{show(item.capability_revision)}</dd></div></dl>
        {item.admin_binding ? <p>管理员 <code>{show(item.admin_binding.admin_ref)}</code>；状态 {state(item.admin_binding.state)}；版本 {show(item.admin_binding.revision)}；当前有效 {flag(item.admin_binding.current)}；所有者当前有效 {flag(item.admin_binding.owner_current)}</p> : <p>此端点无控制台管理员关联。群绑定本身不创建个人所有者权限。</p>}</>}
    </article>)}</div>
    {visible && !visible.items.length && <p>此页没有记录。</p>}
    {visible?.truncated && <p>已达到近期明细上限（{show(visible.bounded_rows)} 条），更早记录需受保护审计查询。</p>}
    <div className="actions"><button type="button" disabled={loading || cursors.length === 1 && !error} onClick={() => { setCursors([null]); setPage(null); setRefresh(value => value + 1); }}>首屏</button>{' '}
      <button type="button" disabled={loading || cursors.length <= 1} onClick={() => { setCursors(values => values.slice(0, -1)); setPage(null); }}>上一页</button>{' '}
      <button type="button" disabled={loading || !visible?.next_cursor || Boolean(error)} onClick={() => { if (visible?.next_cursor) { setCursors(values => [...values, visible.next_cursor]); setPage(null); } }}>下一页</button></div>
  </section>;
}
