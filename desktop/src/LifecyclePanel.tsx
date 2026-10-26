import { useEffect, useRef, useState } from 'react';
import { Check, CircleAlert, ClipboardList, LoaderCircle, Play, RotateCcw, Trash2, Unplug, X } from 'lucide-react';
import { desktop, unwrap } from './api';
import type { LifecycleKind, LifecycleRecord, LifecycleTask } from './api';

export function maintenanceBlocks(value: LifecycleRecord | LifecycleTask | null | undefined) {
  if (!value) return false;
  const settled = 'status' in value ? value.status === 'completed' || value.status === 'failed' && !value.resumable : value.settled;
  return value.service_state !== 'installed' || value.dispatched && !settled;
}
const stateLabels = { installed: '服务已安装', retained: '服务已卸载，数据保留', removing: '正在卸载服务', restoring: '正在重新启用', purging: '正在永久删除', removed: '实例数据已删除', unknown: '状态待核对' };
const labels = { preview: '待确认', queued: '已排队', running: '执行中', failed: '未完成', completed: '已完成', unconfirmed: '待核对', interrupted: '已中断' };
const text = (cause: unknown) => cause instanceof Error ? cause.message : '维护状态暂不可读';
const time = (value: number) => new Date(value * 1000).toLocaleString('zh-CN', { hour12: false });
type Confirmation = { kind: LifecycleKind; mode: 'prepare' | 'confirm' | 'resume'; plan?: LifecycleTask };

export function LifecyclePanel({ id, initial, passwordRequired, dirty, onTask }: {
  id: string; initial?: LifecycleRecord; passwordRequired: boolean; dirty: boolean; onTask: (value: LifecycleTask | null) => void;
}) {
  const [task, setTask] = useState<LifecycleTask | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [pollError, setPollError] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [password, setPassword] = useState('');
  const [eraseText, setEraseText] = useState('');
  const dialog = useRef<HTMLDialogElement>(null);
  const alive = useRef(true);
  const gate = useRef(false);
  const generation = useRef(0);
  useEffect(() => {
    let disposed = false;
    alive.current = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      const epoch = generation.current;
      try {
        const value = await unwrap(desktop?.lifecycleStatus(id));
        if (!disposed && !gate.current && epoch === generation.current) {
          setTask(value); setLoaded(true); setPollError(''); onTask(value);
        }
      } catch (cause) { if (!disposed && epoch === generation.current) setPollError(text(cause)); }
      if (!disposed) timer = setTimeout(() => void poll(), 3000);
    }
    void poll();
    return () => { disposed = true; alive.current = false; clearTimeout(timer); };
  }, [id, onTask]);
  useEffect(() => {
    if (confirmation && !dialog.current?.open) dialog.current?.showModal();
    else if (!confirmation) dialog.current?.close();
  }, [confirmation]);

  const known = loaded ? task : initial;
  const state = known?.service_state || 'installed';
  const unsettled = Boolean(known?.dispatched && (task ? !['completed'].includes(task.status) && !(task.status === 'failed' && !task.resumable) : !initial?.settled));
  const running = task?.status === 'running' || task?.status === 'queued';
  const blocked = busy || !loaded || Boolean(pollError) || unsettled;
  function accept(value: LifecycleTask) { setTask(value); setLoaded(true); setPollError(''); onTask(value); }
  function close() { if (!gate.current) { setConfirmation(null); setPassword(''); setEraseText(''); setError(''); } }
  function open(kind: LifecycleKind, mode: Confirmation['mode'], plan?: LifecycleTask) {
    setPassword(''); setEraseText(''); setError(''); setConfirmation({ kind, mode, plan });
  }
  async function proceed() {
    if (!confirmation || gate.current) return;
    if (confirmation.kind === 'purge' && confirmation.mode !== 'prepare' && eraseText !== '删除') return;
    gate.current = true; generation.current++; setBusy(true); setError('');
    try {
      if (confirmation.mode === 'prepare') {
        const value = await unwrap(desktop?.lifecyclePrepare({ id, kind: confirmation.kind, password }));
        if (alive.current) { accept(value); setConfirmation({ kind: value.kind, mode: 'confirm', plan: value }); }
      } else {
        const plan = confirmation.plan;
        if (!plan) throw new Error('原任务记录不可读，请重新核对');
        const value = await unwrap(desktop?.lifecycleSubmit({ id, operationId: plan.id, confirm: true, password,
          ...(confirmation.kind === 'purge' ? { eraseInstanceId: plan.instance_id } : {}) }));
        if (alive.current) { accept(value); setConfirmation(null); setPassword(''); setEraseText(''); }
      }
    } catch (cause) {
      if (alive.current) { setError(text(cause)); setPassword(''); }
      try {
        const value = await unwrap(desktop?.lifecycleStatus(id));
        if (alive.current && value) {
          accept(value);
          if (['queued', 'running', 'completed'].includes(value.status)) setConfirmation(null);
          else if (value.dispatched) setConfirmation({ kind: value.kind, mode: 'resume', plan: value });
        }
      } catch { /* Preserve the original error and task identity while disconnected. */ }
    } finally { gate.current = false; generation.current++; if (alive.current) setBusy(false); }
  }
  const plan = confirmation?.plan;
  return <section className="lifecycle-panel" aria-labelledby="lifecycle-title">
    <div className="backup-heading"><h3 id="lifecycle-title">服务维护</h3><span className="tag neutral">{stateLabels[state]}</span></div>
    {pollError && <div className="notice error" role="alert"><CircleAlert size={17} /><span>{pollError}</span></div>}
    {task && <div className={`notice ${['failed', 'interrupted', 'unconfirmed'].includes(task.status) ? 'warning' : 'install-notice'}`} role="status">
      {running ? <LoaderCircle className="spin" size={18} /> : task.status === 'completed' ? <Check size={18} /> : <CircleAlert size={18} />}
      <div className="version-progress"><strong>{labels[task.status]}</strong><p>{task.message}</p>
        <small>任务 <code title={task.id}>{task.id.slice(0, 12)}</code> · {time(task.updated_at || task.created_at)}</small></div>
    </div>}
    <dl className="lifecycle-scope">
      <div><dt>服务范围</dt><dd>本实例的机器人、QQ 组件、数据库和沙盒服务</dd></div>
      <div><dt>{['purging', 'removed'].includes(state) ? '永久删除范围' : '卸载后保留'}</dt><dd>数据库、QQ 会话、用户文件、备份和已停止的容器</dd></div>
      <div><dt>不处理</dt><dd>其他实例与服务、共享 Docker / Nix、镜像缓存</dd></div>
    </dl>
    {dirty && <div className="notice warning"><CircleAlert size={17} /><span>当前有未保存的编辑。维护不会应用这些更改，关闭 App 后草稿会丢失。</span></div>}
    <div className="backup-actions">
      {state === 'installed' && !unsettled && <button type="button" className="secondary" disabled={blocked} onClick={() => open('uninstall', 'prepare')}><Unplug size={17} />卸载服务，保留数据</button>}
      {state === 'retained' && !unsettled && <button type="button" className="primary" disabled={blocked} onClick={() => open('reinstall', 'prepare')}><RotateCcw size={17} />重新启用原实例</button>}
      {state === 'retained' && !unsettled && <button type="button" className="secondary danger" disabled={blocked} onClick={() => open('purge', 'prepare')}><Trash2 size={17} />彻底删除实例</button>}
      {task && unsettled && !running && task.resumable && <button type="button" className="primary" disabled={busy || Boolean(pollError)} onClick={() => open(task.kind, 'resume', task)}><Play size={17} />继续原维护任务</button>}
      {task?.status === 'preview' && !task.dispatched && <button type="button" className="secondary" disabled={busy || Boolean(pollError)} onClick={() => open(task.kind, 'confirm', task)}><ClipboardList size={17} />查看待确认清单</button>}
    </div>
    <dialog ref={dialog} className="modal backup-dialog lifecycle-dialog" aria-labelledby="lifecycle-confirm-title" onCancel={event => { if (gate.current) event.preventDefault(); else close(); }}>
      <button type="button" className="icon-button modal-close" title="取消服务维护" aria-label="取消服务维护" disabled={busy} onClick={close}><X size={18} /></button>
      <h2 id="lifecycle-confirm-title">{confirmation?.mode === 'resume' ? '继续原维护任务' : confirmation?.kind === 'purge' ? '永久删除这个实例' : confirmation?.kind === 'reinstall' ? '重新启用原实例' : '卸载服务，保留数据'}</h2>
      <div className="lifecycle-dialog-body">
        <p>{confirmation?.kind === 'purge' ? '此操作不可撤销：删除本实例的数据库、QQ 会话、用户文件、备份、停止容器和专用工作卷。只保留维护审计，共享软件和镜像不处理。需要的数据请先导出到电脑。' : confirmation?.kind === 'reinstall' ? '恢复原实例的服务与启动项，并重新启动卸载前运行的组件。原 QQ 和机器人可能重新登录、恢复回复；不会创建新数据库。' : '停止本实例组件并移除它们的启动项，聊天和文件数据保留。不会卸载共享软件，不代表已释放全部占用。'}</p>
        {plan && <><p className="muted">维护任务 <code>{plan.id}</code> · {time(plan.created_at)}</p>
          {plan.units && <ul className="lifecycle-units" aria-label="维护服务清单">{plan.units.map(name => <li key={name}><code>{name}</code></li>)}</ul>}
          {plan.data_directory && <div className="lifecycle-directory"><strong>{plan.kind === 'purge' ? '将删除的数据目录' : '保留的数据目录'}</strong><code>{plan.data_directory}</code></div>}
          {plan.delete_resources && <div className="purge-resources">
            <p>数据目录占用约 {((plan.data_bytes || 0) / 1024 ** 3).toFixed(2)} GiB；容器和工作卷的额外占用未计入。</p>
            {Object.entries(plan.delete_resources).map(([kind, rows]) => <details key={kind}><summary>{({ containers: '停止的容器', volumes: '专用工作卷', networks: '隔离网络' })[kind]}：{rows.length} 项</summary>
              <ul className="lifecycle-units">{rows.map(row => <li key={row.id}><code>{row.name}</code></li>)}</ul></details>)}
          </div>}
        </>}
        {confirmation?.kind === 'purge' && confirmation.mode !== 'prepare' && <label className="field version-password"><span>输入“删除”以确认永久删除</span><input autoComplete="off" value={eraseText} disabled={busy} onChange={event => setEraseText(event.target.value)} /></label>}
        {passwordRequired && <label className="field version-password"><span>服务器 sudo 密码</span><input type="password" autoComplete="current-password" maxLength={8192} value={password} disabled={busy} onChange={event => setPassword(event.target.value)} /></label>}
        {error && <div className="notice error" role="alert"><CircleAlert size={17} /><span>{error}</span></div>}
      </div>
      <div className="form-actions"><button type="button" className="secondary" disabled={busy} onClick={close}>取消</button>
        <button type="button" className="primary" disabled={busy || !confirmation || passwordRequired && !password || confirmation?.kind === 'purge' && confirmation.mode !== 'prepare' && eraseText !== '删除'} onClick={() => void proceed()}>
          {busy ? <LoaderCircle size={17} className="spin" /> : confirmation?.mode === 'prepare' ? <ClipboardList size={17} /> : <Check size={17} />}
          {busy ? '正在提交' : confirmation?.mode === 'prepare' ? '生成维护清单' : confirmation?.mode === 'resume' ? '继续此任务' : confirmation?.kind === 'purge' ? '永久删除上述数据' : confirmation?.kind === 'reinstall' ? '确认重新启用' : '确认卸载服务'}
        </button></div>
    </dialog>
  </section>;
}
