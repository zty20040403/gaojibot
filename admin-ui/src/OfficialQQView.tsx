import { OfficialDetails } from './OfficialDetails'
import { useEffect, useRef, useState } from 'react'
import { useControlPlane } from './useControlPlane'

type Plane = ReturnType<typeof useControlPlane>
type Challenge = { challenge_id: string; command: string; expires_at: number }
type ChallengeState = { state: string; expires_at: number; can_confirm: boolean }
const date = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? new Date(value * 1000).toLocaleString('zh-CN') : '未知'
const count = (value: unknown) => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : '未知'
const boolean = (value: unknown) => value === true ? '是' : value === false ? '否' : '未知'
const states: Record<string, string> = { disabled: '未启用', degraded: '需要检查', verified: '已有有效证据', closed: '未连接', connected: '已连接', running: '运行中', paused: '已暂停', stopped: '已停止', ready: '已就绪', unknown: '未知', confirmed: '已确认', observed: '已收到所有者确认', pending: '等待所有者', expired: '已过期', consumed: '已完成', issued: '等待所有者', awaiting_owner: '等待所有者' }

export function OfficialQQView({ plane }: { plane: Plane }) {
  const status = plane.data.qqOfficial
  const [challenge, setChallenge] = useState<Challenge | null>(null)
  const [binding, setBinding] = useState<ChallengeState | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [now, setNow] = useState(() => Date.now() / 1000)
  const [notice, setNotice] = useState('')
  const active = useRef(true)
  useEffect(() => { active.current = true; return () => { active.current = false } }, [])
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now() / 1000), 1000); return () => window.clearInterval(timer) }, [])
  useEffect(() => {
    if (!challenge || now < challenge.expires_at) return
    setChallenge(null); setBinding(null); setNotice('绑定请求已过期。可以重新发起；此前的口令不能再使用。')
  }, [challenge, now])
  useEffect(() => {
    if (!challenge) return
    let disposed = false
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const value = await plane.query(`/qq-official/admin-binding/challenges/${encodeURIComponent(challenge.challenge_id)}`)
        if (!disposed) {
          if (typeof value.state !== 'string' || typeof value.expires_at !== 'number' || typeof value.can_confirm !== 'boolean') throw new Error('绑定状态暂不可用')
          setBinding(value as ChallengeState); setError('')
          if (['confirmed', 'consumed', 'cancelled', 'expired'].includes(value.state)) {
            setChallenge(null); setNotice(states[value.state] || '请求已结束')
          }
        }
      } catch { if (!disposed) setError('无法核对绑定请求；请确认登录和连接状态。不会自动确认绑定。') }
      finally { if (!disposed) timer = setTimeout(() => void poll(), 3000) }
    }
    void poll()
    return () => { disposed = true; clearTimeout(timer) }
  }, [challenge, plane.query])
  async function create() {
    if (busy) return
    setBusy(true); setError(''); setNotice('')
    try {
      const value = await plane.mutate('qq-official', '/qq-official/admin-binding/challenges', 'POST', {}, [])
      if (typeof value.challenge_id !== 'string' || !/^[a-f0-9]{32}$/.test(value.challenge_id)
          || typeof value.command !== 'string' || !value.command.startsWith('/admin-bind ') || value.command.length > 256
          || typeof value.expires_at !== 'number' || !Number.isFinite(value.expires_at)) throw new Error('绑定响应无效')
      if (active.current) { setChallenge(value as Challenge); setBinding(null) }
    } catch { if (active.current) setError('未能创建绑定请求。请检查当前官方账号与控制台登录状态。') }
    finally { if (active.current) setBusy(false) }
  }
  async function confirm() {
    if (busy || !challenge || binding?.can_confirm !== true || now >= challenge.expires_at) return
    setBusy(true); setError('')
    try {
      const result = await plane.mutate('qq-official', `/qq-official/admin-binding/challenges/${encodeURIComponent(challenge.challenge_id)}/confirm`, 'POST', {}, ['qqOfficial'])
      if (result.state !== 'consumed' || !Number.isSafeInteger(result.binding_revision) || result.binding_revision < 1) throw new Error('confirmation_unverified')
      if (active.current) { setChallenge(null); setBinding(null); setNotice('服务器已确认管理员关联。后续高危操作仍需相应权限与确认。') }
    } catch { if (active.current) setError('当前证据或权限已变化，未确认关联。请刷新请求状态后重试。') }
    finally { if (active.current) setBusy(false) }
  }
  return <div className="official-view">
    <section className="panel"><h2>连接与运行</h2><p>状态来自当前账号的真实连接与持久记录。连接成功不代表消息已展示、文件已下载或平台已开放全部能力。</p>
      {!status ? <p role="status">官方状态暂不可用。旧版本服务可能尚未提供此接口。</p> : <>
        <dl className="official-grid"><div><dt>启用</dt><dd>{boolean(status.enabled)}</dd></div><div><dt>服务</dt><dd>{states[status.state] || '待诊断'}</dd></div><div><dt>健康</dt><dd>{boolean(status.healthy)}</dd></div><div><dt>账号锁有效</dt><dd>{boolean(status.lease?.current)}</dd></div><div><dt>网关</dt><dd>{states[status.gateway?.state] || status.gateway?.state || '未知'}</dd></div><div><dt>采集时间</dt><dd>{date(status.observed_at)}</dd></div></dl>
        <dl className="official-grid"><div><dt>入站队列</dt><dd>{count(status.gateway?.queue_depth)} / {count(status.gateway?.queue_limit)}</dd></div><div><dt>消息连续性缺口</dt><dd>{boolean(status.gateway?.message_gap)}</dd></div><div><dt>发送器运行</dt><dd>{boolean(status.sender?.running)}</dd></div><div><dt>限流等待</dt><dd>{count(status.sender?.rate_waiters)}</dd></div><div><dt>未落盘回执</dt><dd>{count(status.sender?.unrecorded)}</dd></div><div><dt>有效回复窗口</dt><dd>{count(status.windows?.active)}</dd></div></dl>
      </>}
      {typeof status?.reason === 'string' && /^[a-z0-9_]{1,96}$/.test(status.reason) && <p>诊断状态：<code>{status.reason}</code></p>}
      <button type="button" onClick={() => void plane.refresh('qqOfficial')}>刷新官方状态</button>
    </section>
    <section className="panel"><h2>关联控制台管理员</h2><p>控制台密码证明本地登录身份。机器人所有者需要在当前账号的 C2C 私聊里回应，再由此会话确认关联。不要把口令发到群里。</p>
      {error && <p className="error-banner" role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
      {!challenge ? <button type="button" disabled={busy || status?.enabled !== true} onClick={() => void create()}>创建关联请求</button> : <>
        <p>请当前所有者向机器人私聊发送以下命令。有效至 {date(challenge.expires_at)}；口令只保存在本页内存，离开本页后不回显。</p>
        <pre className="official-command"><code>{challenge.command}</code></pre>
        <p role="status">{binding ? states[binding.state] || '等待服务器核验' : '正在核对请求'}</p>
        <button type="button" disabled={busy || binding?.can_confirm !== true || now >= challenge.expires_at} onClick={() => void confirm()}>确认关联当前管理员</button>
      </>}
    </section>
    <section className="panel"><h2>群绑定与文件出口</h2><p>群绑定使用机器人私聊发起的 /bind 流程：将一次性绑定码发送到目标群，再回原私聊核对并确认。数字群号和近期发言记录都不是群管理权限。</p>
      <p>原生文件、受控下载链接和控制台取件分别验收。下载链接默认关闭；只有明确配置外部服务与权限后才启用。文件上传成功不代表消息被平台接受。</p>
      <table><thead><tr><th>能力</th><th>证据状态</th><th>验证时间</th><th>到期时间</th></tr></thead><tbody>{Array.isArray(status?.capabilities) && status.capabilities.length ? status.capabilities.map((item: any, index: number) => <tr key={index}><td>{item.kind}</td><td>{states[item.status] || item.status || '未知'}{item.authority_required === true && <small> · 仍需逐次授权</small>}{item.truncated === true && <small> · 仅展示有界样本</small>}</td><td>{date(item.verified_at)}</td><td>{date(item.expires_at)}</td></tr>) : <tr><td colSpan={4}>尚无可核验能力证据</td></tr>}</tbody></table>
    </section>
    <section className="panel"><h2>文件出口、额度与清理</h2>
      <dl className="official-grid"><div><dt>下载服务已配置</dt><dd>{boolean(status?.download?.configured)}</dd></div><div><dt>内部监听</dt><dd>{boolean(status?.download?.listening)}</dd></div><div><dt>HTTPS 反代自检</dt><dd>{boolean(status?.download?.proxy_reachable)}</dd></div><div><dt>已导入目标记录</dt><dd>{boolean(status?.download?.evidence_configured)}</dd></div><div><dt>预留额度</dt><dd>{count(status?.quota?.reserved)}</dd></div><div><dt>已计额度</dt><dd>{count(status?.quota?.spent)}</dd></div><div><dt>本进程最近清理</dt><dd>{date(status?.cleanup?.last_at)}</dd></div><div><dt>已清理 / 失败</dt><dd>{count(status?.cleanup?.deleted)} / {count(status?.cleanup?.failed)}</dd></div><div><dt>待清理对象</dt><dd>{count(status?.cleanup?.pending_blobs)}</dd></div></dl>
      <p>HTTPS 自检只证明该代理连接当前后端；不是 QQ 域名审核或收件人网络验收。已导入记录可能过期，逐次发送仍核对当前目标与权限。</p>
    </section>
    <OfficialDetails plane={plane} />
    <section className="panel"><h2>持久状态与验收</h2><div className="official-grid">{(['execution', 'delivery', 'inbox', 'files', 'migration'] as const).map(key => <div key={key}><h3>{{ execution: '回合', delivery: '发送', inbox: '入站', files: '文件', migration: '迁移' }[key]}</h3>{status?.[key] && typeof status[key] === 'object' ? <dl>{Object.entries(status[key]).map(([state, value]) => <div key={state}><dt>{state}</dt><dd>{count(value)}</dd></div>)}</dl> : <p>未知</p>}</div>)}</div>
      <ol><li>确认账号锁、网关连续性和发送器健康。</li><li>完成所有者与指定群双向绑定。</li><li>在已授权端点进行文字回合，检查持久发送回执。</li><li>按需要验证文件上传、消息接受与实际取件，各项分别记录。</li><li>重启后核对恢复结果；unknown 记录隔离，不通过重复发送消除。</li></ol>
      <p>此页面不会自动发送验收消息。指标只显示已经发生的事实，不把等待状态标为成功。</p>
    </section>
  </div>
}
