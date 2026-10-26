import { useEffect, useRef, useState } from 'react';
import { ArrowUpCircle, Check, ChevronDown, ChevronUp, CircleAlert, LoaderCircle, Play, RotateCcw, X } from 'lucide-react';
import { desktop, unwrap } from './api';
import type { UpdatePlan, VersionTask } from './api';

type Confirmation = { kind: 'update' | 'rollback' | 'resume'; expectedChecksum: string; revision: number; target: string; rollbackId?: string };
function time(value: number) { return new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }); }
function errorText(cause: unknown) { return cause instanceof Error ? cause.message : '版本信息暂不可读'; }
const labels: Record<string, string> = { queued: '已排队', running: '执行中', completed: '已完成', failed: '未完成', interrupted: '已中断', unconfirmed: '待核对' };

export function UpdatePanel({ id, dirty, passwordRequired }: { id: string; dirty: boolean; passwordRequired: boolean }) {
  const local = id === 'desktop-local';
  const [plan, setPlan] = useState<UpdatePlan | null>(null);
  const [task, setTask] = useState<VersionTask | null>(null);
  const [pollError, setPollError] = useState('');
  const [planError, setPlanError] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [selected, setSelected] = useState('');
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [password, setPassword] = useState('');
  const dialog = useRef<HTMLDialogElement>(null);
  const gate = useRef(false);
  const alive = useRef(true);
  useEffect(() => {
    let disposed = false;
    alive.current = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const value = await unwrap(desktop?.updateStatus(id));
        if (!disposed) { setTask(value); setPollError(''); }
      } catch (cause) { if (!disposed) setPollError(errorText(cause)); }
      try {
        const value = await unwrap(desktop?.updatePlan(id));
        if (!disposed) { setPlan(value); setPlanError(''); }
      } catch (cause) { if (!disposed) setPlanError(errorText(cause)); }
      if (!disposed) timer = setTimeout(() => void poll(), 3000);
    }
    void poll();
    return () => { disposed = true; alive.current = false; clearTimeout(timer); };
  }, [id]);
  useEffect(() => {
    if (confirmation && !dialog.current?.open) dialog.current?.showModal();
    else if (!confirmation) dialog.current?.close();
  }, [confirmation]);
  const running = Boolean(task && ['queued', 'running'].includes(task.status));
  const unsettled = Boolean(task && task.status !== 'completed' && task.finished !== true);
  const blocked = Boolean(dirty || busy || unsettled || pollError || planError || !plan || plan.current.maintenance_pending
    || ['queued', 'running'].includes(plan.current.operation?.status || ''));
  const point = plan?.rollbacks.find(item => item.id === selected);
  function close() { if (!gate.current) { setConfirmation(null); setPassword(''); setError(''); } }
  async function submit() {
    if (!confirmation || gate.current) return;
    gate.current = true; setBusy(true); setError('');
    try {
      const result = confirmation.kind === 'resume'
        ? await unwrap(desktop?.resumeUpdate({ id, password }))
        : await unwrap(desktop?.update({ id, kind: confirmation.kind, expectedChecksum: confirmation.expectedChecksum,
          revision: confirmation.revision, rollbackId: confirmation.rollbackId, confirm: true, password }));
      if (alive.current) { setTask(result); setConfirmation(null); }
    } catch (cause) {
      if (alive.current) setError(errorText(cause));
      try {
        const pending = await unwrap(desktop?.updateStatus(id));
        if (alive.current && pending && pending.status !== 'completed' && pending.finished !== true) {
          setTask(pending);
          if (['queued', 'running'].includes(pending.status)) setConfirmation(null);
          else setConfirmation({ kind: 'resume', expectedChecksum: '', revision: 0, target: '' });
        }
      } catch { /* Keep the original submission error if the connection is still unavailable. */ }
    } finally {
      gate.current = false;
      if (alive.current) { setBusy(false); setPassword(''); }
    }
  }
  function confirm(kind: 'update' | 'rollback') {
    if (!plan || blocked || kind === 'rollback' && !point) return;
    setError('');
    setConfirmation({ kind, expectedChecksum: plan.current.checksum, revision: plan.current.revision,
      target: kind === 'update' ? plan.available.version : point!.version, ...(kind === 'rollback' && point ? { rollbackId: point.id } : {}) });
  }
  return <section className="version-panel" aria-labelledby="version-title">
    <div className="backup-heading"><h3 id="version-title">更新与回退</h3><span className="tag neutral">{local ? '此电脑的机器人' : '此服务器实例'}</span></div>
    {task && <div className={`notice ${task.status === 'completed' ? 'install-notice' : task.status === 'failed' || task.status === 'interrupted' ? 'warning' : 'install-notice'}`} role="status">
      {running ? <LoaderCircle size={18} className="spin" /> : task.status === 'completed' ? <Check size={18} /> : <CircleAlert size={18} />}
      <div className="version-progress"><strong>{labels[task.status] || '待核对'}</strong><p>{task.message}</p><small>任务 <code title={task.id}>{task.id.slice(0,12)}</code>{task.updated_at ? ` · ${time(task.updated_at)}` : ''}</small></div>
    </div>}
    {pollError && <div className="notice error" role="alert"><CircleAlert size={17} /><span>{pollError}</span></div>}
    {planError && <div className="notice warning" role="status"><CircleAlert size={17} /><span>{running ? '管理器暂时不可读；更新进度仍通过独立任务记录获取。' : planError}</span></div>}
    {plan ? <dl className="version-stats"><div><dt>{local ? '本机机器人版本' : '服务器运行版本'}</dt><dd>{plan.current.version}<code title={plan.current.checksum}>{plan.current.checksum.slice(0,12)}</code></dd></div>
      <div><dt>此 App 携带的版本</dt><dd>{plan.available.version}<code title={plan.available.checksum}>{plan.available.checksum.slice(0,12)}</code></dd></div></dl>
      : !planError && <p className="version-loading"><LoaderCircle size={18} className="spin" />正在读取版本</p>}
    {dirty && <div className="notice warning"><CircleAlert size={17} /><span>QQ 与模型设置有未保存更改，请先保存或重新载入。</span></div>}
    <div className="backup-actions"><button type="button" className="primary" disabled={blocked || !plan?.canUpdate} onClick={() => confirm('update')}><ArrowUpCircle size={17} />{plan && !plan.canUpdate ? '已是此 App 版本' : '更新此实例'}</button>
      {task && unsettled && !running && <button type="button" className="secondary" disabled={busy} onClick={() => { setError(''); setConfirmation({ kind: 'resume', expectedChecksum: '', revision: 0, target: '' }); }}><Play size={16} />继续原版本任务</button>}
    </div>
    <section className="version-rollback"><h3>回退到升级前</h3>
      <div className="catalog-picker"><label className="field"><span id="rollback-point-label">回退点</span><select aria-labelledby="rollback-point-label" value={selected} onChange={event => setSelected(event.target.value)} disabled={blocked || !plan?.rollbacks.length}>
        <option value="">选择已保留的版本与数据</option>{plan?.rollbacks.map(item => <option value={item.id} key={item.id}>{time(item.created_at)} · {item.version} · {item.target_checksum.slice(0,8)}</option>)}
      </select></label><button type="button" className="secondary" disabled={blocked || !point} onClick={() => confirm('rollback')}><RotateCcw size={16} />回退</button></div>
      {plan && !plan.rollbacks.length && <p className="muted">暂无可用回退点</p>}
      {plan && plan.rollbacks.length > 0 && <div className="backup-list">{(expanded ? plan.rollbacks : plan.rollbacks.slice(0,5)).map(item => <div className="version-history-row" key={item.id}>
        <span>{time(item.created_at)}</span><strong>{item.version}</strong><code title={item.target_checksum}>{item.target_checksum.slice(0,12)}</code></div>)}</div>}
      {plan && plan.rollbacks.length > 5 && <button type="button" className="text-button" onClick={() => setExpanded(value => !value)}>{expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}{expanded ? '收起' : `全部 ${plan.rollbacks.length} 个回退点`}</button>}
    </section>
    <dialog ref={dialog} className="modal backup-dialog" aria-labelledby="version-confirm-title" onCancel={event => { if (gate.current) event.preventDefault(); else close(); }}>
      <button type="button" className="icon-button modal-close" title="取消版本操作" aria-label="取消版本操作" disabled={busy} onClick={close}><X size={18} /></button>
      <h2 id="version-confirm-title">{confirmation?.kind === 'rollback' ? '恢复升级前的版本和数据？' : confirmation?.kind === 'resume' ? '继续原版本任务？' : '更新此实例？'}</h2>
      <p>{confirmation?.kind === 'rollback' ? '将恢复所选升级前的机器人程序、数据库、QQ 会话和文件。之后产生的数据不会合并，会另行保留。管理器保持新版。'
        : confirmation?.kind === 'resume' ? '沿用之前的任务编号核对和继续，不重新安装，也不新建数据库。'
        : '会短暂停止本实例写入，创建备份并验证新版本。切换前失败会恢复原版本；切换后若尚未确认 QQ 连接，会保留新数据并提示核对。'}
      </p>
      {confirmation?.target && <p className="version-target">目标版本：{confirmation.target}</p>}
      {passwordRequired && <label className="field version-password"><span>服务器 sudo 密码</span><input type="password" autoComplete="current-password" value={password} disabled={busy} maxLength={8192} onChange={event => setPassword(event.target.value)} /></label>}
      {error && <div className="notice error" role="alert"><CircleAlert size={17} /><span>{error}</span></div>}
      <div className="form-actions"><button type="button" className="secondary" disabled={busy} onClick={close}>取消</button><button type="button" className="primary" disabled={busy || !confirmation || passwordRequired && !password} onClick={() => void submit()}>
        {busy ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />}{busy ? '正在提交' : confirmation?.kind === 'rollback' ? '确认回退' : confirmation?.kind === 'resume' ? '继续任务' : '确认更新'}</button></div>
    </dialog>
  </section>;
}
