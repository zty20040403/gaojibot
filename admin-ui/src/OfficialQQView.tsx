import { OfficialDetails } from './OfficialDetails'
import { useControlPlane } from './useControlPlane'

type Plane = ReturnType<typeof useControlPlane>
const date = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? new Date(value * 1000).toLocaleString('zh-CN') : '未知'
const count = (value: unknown) => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : '未知'
const boolean = (value: unknown) => value === true ? '是' : value === false ? '否' : '未知'
const states: Record<string, string> = { disabled: '未启用', degraded: '需要检查', verified: '已有有效证据', closed: '未连接', connected: '已连接', running: '运行中', paused: '已暂停', stopped: '已停止', ready: '已就绪', unknown: '未知', confirmed: '已确认', pending: '等待中', expired: '已过期' }

export function OfficialQQView({ plane }: { plane: Plane }) {
  const status = plane.data.qqOfficial
  return <div className="official-view">
    <section className="panel"><h2>连接与运行</h2><p>状态来自当前账号的真实连接与持久记录。连接成功不代表消息已展示、文件已下载或平台已开放全部能力。</p>
      {!status ? <p role="status">官方状态暂不可用。旧版本服务可能尚未提供此接口。</p> : <>
        <dl className="official-grid"><div><dt>启用</dt><dd>{boolean(status.enabled)}</dd></div><div><dt>服务</dt><dd>{states[status.state] || '待诊断'}</dd></div><div><dt>健康</dt><dd>{boolean(status.healthy)}</dd></div><div><dt>账号锁有效</dt><dd>{boolean(status.lease?.current)}</dd></div><div><dt>网关</dt><dd>{states[status.gateway?.state] || status.gateway?.state || '未知'}</dd></div><div><dt>采集时间</dt><dd>{date(status.observed_at)}</dd></div></dl>
        <dl className="official-grid"><div><dt>入站队列</dt><dd>{count(status.gateway?.queue_depth)} / {count(status.gateway?.queue_limit)}</dd></div><div><dt>消息连续性缺口</dt><dd>{boolean(status.gateway?.message_gap)}</dd></div><div><dt>发送器运行</dt><dd>{boolean(status.sender?.running)}</dd></div><div><dt>限流等待</dt><dd>{count(status.sender?.rate_waiters)}</dd></div><div><dt>未落盘回执</dt><dd>{count(status.sender?.unrecorded)}</dd></div><div><dt>有效回复窗口</dt><dd>{count(status.windows?.active)}</dd></div></dl>
      </>}
      {typeof status?.reason === 'string' && /^[a-z0-9_]{1,96}$/.test(status.reason) && <p>诊断状态：<code>{status.reason}</code></p>}
      <button type="button" onClick={() => void plane.refresh('qqOfficial')}>刷新官方状态</button>
    </section>
    <section className="panel"><h2>群绑定与文件出口</h2><p>QQ 所有者身份与控制台登录分开。数字群号和近期发言记录都不是群管理权限。</p>
      <p>原生文件、受控下载链接和控制台取件分别验收。下载链接默认关闭；只有明确配置外部服务与权限后才启用。文件上传成功不代表消息被平台接受。</p>
      <table><thead><tr><th>能力</th><th>证据状态</th><th>验证时间</th><th>到期时间</th></tr></thead><tbody>{Array.isArray(status?.capabilities) && status.capabilities.length ? status.capabilities.map((item: any, index: number) => <tr key={index}><td>{item.kind}</td><td>{states[item.status] || item.status || '未知'}{item.authority_required === true && <small> · 仍需逐次授权</small>}{item.truncated === true && <small> · 仅展示有界样本</small>}</td><td>{date(item.verified_at)}</td><td>{date(item.expires_at)}</td></tr>) : <tr><td colSpan={4}>尚无可核验能力证据</td></tr>}</tbody></table>
    </section>
    <section className="panel"><h2>文件出口与清理</h2>
      <dl className="official-grid"><div><dt>下载服务已配置</dt><dd>{boolean(status?.download?.configured)}</dd></div><div><dt>内部监听</dt><dd>{boolean(status?.download?.listening)}</dd></div><div><dt>HTTPS 反代自检</dt><dd>{boolean(status?.download?.proxy_reachable)}</dd></div><div><dt>已导入目标记录</dt><dd>{boolean(status?.download?.evidence_configured)}</dd></div><div><dt>本进程最近清理</dt><dd>{date(status?.cleanup?.last_at)}</dd></div><div><dt>已清理 / 失败</dt><dd>{count(status?.cleanup?.deleted)} / {count(status?.cleanup?.failed)}</dd></div></dl>
      <p>HTTPS 自检只证明该代理连接当前后端；不是 QQ 域名审核或收件人网络验收。已导入记录可能过期，逐次发送仍核对当前目标与权限。</p>
    </section>
    <OfficialDetails plane={plane} />
    <section className="panel"><h2>持久状态与验收</h2><div className="official-grid">{(['execution', 'delivery', 'inbox', 'migration'] as const).map(key => <div key={key}><h3>{{ execution: '回合', delivery: '发送', inbox: '入站', migration: '迁移' }[key]}</h3>{status?.[key] && typeof status[key] === 'object' ? <dl>{Object.entries(status[key]).map(([state, value]) => <div key={state}><dt>{state}</dt><dd>{count(value)}</dd></div>)}</dl> : <p>未知</p>}</div>)}</div>
      <ol><li>确认账号锁、网关连续性和发送器健康。</li><li>完成所有者与指定群双向绑定。</li><li>在已授权端点进行文字回合，检查持久发送回执。</li><li>按需要验证文件上传、消息接受与实际取件，各项分别记录。</li><li>重启后核对恢复结果；unknown 记录隔离，不通过重复发送消除。</li></ol>
      <p>此页面不会自动发送验收消息。指标只显示已经发生的事实，不把等待状态标为成功。</p>
    </section>
  </div>
}
