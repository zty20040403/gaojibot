import { useEffect, useRef, useState } from 'react';
import { ArrowRight, CheckCircle2, CircleAlert, CircleHelp, Download, LoaderCircle, RefreshCw, Stethoscope } from 'lucide-react';
import { desktop, instanceCall, unwrap } from './api';
import type { InstanceStatus, QQStatus } from './api';
import './readiness.css';

type State = 'ok' | 'attention' | 'unknown' | 'off';
type Destination = 'qq' | 'settings';
type Check = { id: string; label: string; state: State; detail: string; destination?: Destination };
type Services = { services: { name: string; state: string; health: string }[]; connection?: string };
type Probe = { revision: number; at: number; state: 'ok' | 'attention'; latency?: number };
type Snapshot = { at: number; status: InstanceStatus | null; services: Services | null; qq: QQStatus | null };
const states: Record<State, string> = { ok: '已确认', attention: '需处理', unknown: '未确认', off: '未启用' };
const timestamp = (value: number) => new Date(value).toLocaleString('zh-CN', { hour12: false });

export function ReadinessPanel({ id, revision, suspended, onNavigate }: {
  id: string; revision: number; suspended: boolean; onNavigate: (destination: Destination) => void;
}) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [busy, setBusy] = useState(false);
  const [probe, setProbe] = useState<Probe | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [notice, setNotice] = useState('');
  const [now, setNow] = useState(Date.now());
  const dialog = useRef<HTMLDialogElement>(null);
  const gate = useRef(false);
  const active = useRef(false);
  const generation = useRef(0);
  async function inspect() {
    if (gate.current || suspended) return;
    gate.current = true; setBusy(true); setNotice('');
    const turn = generation.current;
    const result: Snapshot = { at: Date.now(), status: null, services: null, qq: null };
    try {
      // A failed component must not leave last time's green check in place.
      for (const [action, field] of [['status', 'status'], ['services', 'services'], ['qqStatus', 'qq']] as const) {
        if (!active.current || turn !== generation.current) return;
        try { Object.assign(result, { [field]: await instanceCall(id, action) }); } catch { /* Each missing result becomes unknown. */ }
      }
      if (active.current && turn === generation.current) {
        result.at = Date.now(); setSnapshot(result); setNow(result.at);
        setProbe(current => current?.revision === result.status?.instance.revision ? current : null);
      }
    } finally { gate.current = false; if (active.current && turn === generation.current) setBusy(false); }
  }
  useEffect(() => {
    active.current = true;
    void inspect();
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => { active.current = false; generation.current++; clearInterval(timer); };
  }, [id]);
  useEffect(() => { if (!suspended) void inspect(); }, [suspended]);
  useEffect(() => {
    if (confirming) dialog.current?.showModal(); else dialog.current?.close();
  }, [confirming]);
  const status = snapshot?.status;
  const settings = status?.instance.settings;
  const model = settings?.models.find(value => value.id === settings.default_model);
  const stale = Boolean(snapshot && (now - snapshot.at > 60000 || status && status.instance.revision !== revision));
  const checks: Check[] = [];
  if (snapshot) {
    checks.push({ id: 'manager', label: '管理入口', state: status ? 'ok' : 'unknown', detail: status ? `已读取配置版本 ${status.instance.revision}` : '连接中断或管理入口未响应，不能判断实例已停止。' });
    const service = (name: string, label: string): Check => {
      const row = snapshot.services?.services.find(value => value.name === name);
      const stopped = name === 'bot' && status?.desired.bot_enabled === false;
      if (!snapshot.services) return { id: name, label, state: 'unknown', detail: '本轮没有取得组件状态。' };
      if (row?.state === 'running') return { id: name, label,
        state: name === 'database' ? row.health === 'healthy' ? 'ok' : row.health === 'unhealthy' ? 'attention' : 'unknown' : 'ok',
        detail: name === 'database' ? row.health === 'healthy' ? 'PostgreSQL 就绪检查通过；不代表数据完整性检查通过。' : '容器运行中，数据库就绪检查尚未通过。' : '容器正在运行。' };
      return { id: name, label, state: stopped ? 'off' : 'attention', detail: stopped ? '机器人设置为停止，不是故障。' : '组件未运行或正在重启，请先核对运行与维护状态。', destination: 'settings' };
    };
    checks.push(service('database', '数据库'), service('napcat', 'QQ 接入组件'), service('bot', '机器人进程'));
    checks.push({ id: 'qq', label: 'QQ 登录', state: status?.qq_handover ? 'attention' : snapshot.qq ? snapshot.qq.online ? 'ok' : 'attention' : 'unknown',
      detail: status?.qq_handover ? '迁移后的 QQ 仍待接管。' : snapshot.qq?.online ? 'QQ 已登录。' : snapshot.qq ? 'QQ 未登录或正在重连。' : '未取得登录状态；不会自动清空会话。', destination: 'qq' });
    const connection = snapshot.services?.connection;
    checks.push({ id: 'connection', label: 'QQ 与机器人连接', state: connection === 'connected' ? 'ok' : status?.desired.bot_enabled === false ? 'off' : !connection || ['unavailable', 'stale'].includes(connection) ? 'unknown' : 'attention',
      detail: connection === 'connected' ? '最近 15 秒的机器人心跳确认已接入 QQ。' : !connection ? '此运行包未提供连接心跳，不能仅凭 QQ 登录推断已接通。' : connection === 'stale' ? '机器人心跳过期，尚未确认恢复。' : status?.desired.bot_enabled === false ? '启用机器人后再检查。' : '尚未收到本实例 QQ 的有效连接确认。', destination: 'settings' });
    const tested = probe && probe.revision === status?.instance.revision && now - probe.at <= 60000 ? probe : null;
    checks.push({ id: 'model', label: '默认聊天模型', state: !status ? 'unknown' : !model ? 'attention' : tested?.state || 'unknown',
      detail: !status ? '未取得已保存的模型配置。' : !model ? '尚未选择默认模型。' : tested ? tested.state === 'ok' ? `模型实际返回了文字，耗时 ${((tested.latency || 0) / 1000).toFixed(1)} 秒；${timestamp(tested.at)}。` : '模型测试失败；请核对地址、密钥、模型名称和服务状态。' : '已保存模型配置，本轮尚未确认真实调用结果。', destination: 'settings' });
    checks.push({ id: 'groups', label: '群聊范围', state: !settings ? 'unknown' : settings.allow_all_groups || settings.group_ids.length ? 'ok' : 'off',
      detail: !settings ? '未读取到群设置。' : settings.allow_all_groups ? '已允许当前 QQ 加入的所有群。' : settings.group_ids.length ? `已允许 ${settings.group_ids.length} 个群；群成员资格和真实收发仍需在 QQ 验证。` : '没有启用群聊，机器人不会在群里回复。', destination: 'settings' });
    checks.push({ id: 'configuration', label: '配置应用', state: !status ? 'unknown' : status.desired.bot_enabled && status.desired.revision !== status.instance.revision ? 'attention' : status.desired.bot_enabled ? 'ok' : 'off',
      detail: !status ? '未读取到运行配置。' : !status.desired.bot_enabled ? '设置已保存时仍需单独启用机器人。' : status.desired.revision !== status.instance.revision ? '已保存的设置尚未应用到运行实例。' : '运行配置与已保存版本一致。', destination: 'settings' });
    const missing = Object.keys(status?.component_requirements || {}).filter(name => settings?.features[name]
      || name === 'matrix' && settings?.components?.matrix?.enabled
      || name === 'imessage' && settings?.components?.imessage?.enabled).length;
    checks.push({ id: 'components', label: '功能依赖', state: !status?.component_requirements ? 'unknown' : missing ? 'attention' : 'ok',
      detail: !status?.component_requirements ? '此运行包没有提供功能依赖检查。' : missing ? `${missing} 项已选功能缺少必要配置或组件。` : '未发现已选功能缺少必要配置；不代表外部服务全部可用。', destination: 'settings' });
    const maintenance = status?.maintenance_pending || status?.operation && ['running', 'queued', 'failed'].includes(status.operation.status);
    checks.push({ id: 'maintenance', label: '维护任务', state: !status ? 'unknown' : maintenance ? 'attention' : 'ok', detail: !status ? '未取得维护状态。' : maintenance ? '存在进行中、失败或待恢复的维护操作，请在模型与启用中查看原任务。' : '没有发现未完成的维护操作。', destination: 'settings' });
  }
  async function testModel() {
    if (!model || !status || stale || gate.current || suspended) return;
    setConfirming(false); gate.current = true; setBusy(true); setNotice('');
    const turn = generation.current;
    const targetRevision = status.instance.revision;
    try {
      const current = await instanceCall<InstanceStatus>(id, 'status');
      if (!active.current || turn !== generation.current) return;
      if (current.instance.revision !== targetRevision) { setSnapshot(null); setProbe(null); setNotice('配置已经变化，请重新检查。'); return; }
      const { has_key: _hasKey, ...profile } = model;
      const result = await instanceCall<{ responded: boolean; latency_ms: number }>(id, 'modelTest', { model: profile, key: null });
      if (active.current && turn === generation.current) setProbe({ revision: targetRevision, at: Date.now(), state: result.responded ? 'ok' : 'attention', latency: result.latency_ms });
    } catch { if (active.current && turn === generation.current) setProbe({ revision: targetRevision, at: Date.now(), state: 'attention' }); }
    finally { gate.current = false; if (active.current && turn === generation.current) setBusy(false); }
  }
  const attention = checks.filter(value => value.state === 'attention').length;
  return <section className="readiness" aria-label="运行检查">
    <div className="section-heading"><div><h3><Stethoscope size={21} />运行检查</h3><span className="muted">{snapshot ? `检查时间：${timestamp(snapshot.at)}` : '尚无检查结果'}</span></div>
      <div className="readiness-actions"><button type="button" className="icon-button" title="重新检查运行状态" aria-label="重新检查运行状态" disabled={busy || suspended} onClick={() => void inspect()}><RefreshCw size={18} className={busy ? 'spin' : ''} /></button>
        <button type="button" className="icon-button" title="导出匿名运行报告" aria-label="导出匿名运行报告" disabled={busy || !snapshot} onClick={() => void unwrap(desktop?.exportDiagnostics({ runtime: { checked_at: snapshot!.at, stale, checks } })).then(value => setNotice(value.saved ? '运行报告已保存。' : '')).catch(() => setNotice('报告导出失败，请重试。'))}><Download size={18} /></button></div></div>
    <div className={`notice ${stale || attention ? 'warning' : ''}`} role="status"><CircleAlert size={18} /><span>{busy ? '检查中，尚未确认本轮结果' : stale ? '上次结果已过期或配置已变化，请重新检查。' : snapshot ? attention ? `${attention} 项需要处理` : '已完成基础检查，真实消息与文件送达仍需在 QQ 验证。' : '读取实例状态中'}</span></div>
    {notice && <div className="notice" role="status">{notice}</div>}
    <div className="readiness-list">{checks.map(check => <div key={check.id} className="readiness-row" data-check={check.id}>
      <div className={`readiness-icon ${stale || busy ? 'unknown' : check.state}`}>{check.state === 'ok' && !stale && !busy ? <CheckCircle2 size={20} /> : check.state === 'attention' && !stale && !busy ? <CircleAlert size={20} /> : <CircleHelp size={20} />}</div>
      <div className="readiness-copy"><strong>{check.label}</strong><p>{check.detail}</p></div><span className="tag neutral">{stale ? '已过期' : busy ? '待核对' : states[check.state]}</span>
      {check.destination && <button type="button" className="icon-button" title={`查看${check.label}设置`} aria-label={`查看${check.label}设置`} onClick={() => onNavigate(check.destination!)}><ArrowRight size={17} /></button>}
    </div>)}</div>
    <div className="form-actions"><button className="secondary" type="button" disabled={busy || suspended || stale || !model || Boolean(status?.maintenance_pending)} onClick={() => setConfirming(true)}><Stethoscope size={17} />测试默认模型</button></div>
    <dialog className="modal" ref={dialog} onCancel={() => setConfirming(false)} aria-labelledby="model-probe-title">
      <h2 id="model-probe-title">测试默认模型？</h2><p>向已保存的默认模型发送一次简短测试，可能产生少量 API 费用。不发送群聊历史，也不发送 QQ 消息。</p>
      <div className="form-actions"><button type="button" className="secondary" onClick={() => setConfirming(false)}>取消</button><button type="button" className="primary" disabled={busy || suspended || stale} onClick={() => void testModel()}>{busy ? <LoaderCircle size={16} className="spin" /> : <Stethoscope size={16} />}确认测试</button></div>
    </dialog>
  </section>;
}
