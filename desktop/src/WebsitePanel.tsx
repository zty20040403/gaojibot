import { useEffect, useRef, useState } from 'react';
import { Archive, Check, CircleAlert, Download, ExternalLink, FolderOpen, LoaderCircle, Play, RotateCcw, Square, Trash2, Upload, X } from 'lucide-react';
import { desktop, unwrap } from './api';
import type { Application, Inspection, WebsiteOperation, WebsiteSelection, WebsiteStatus } from './api';
import { DeploymentProgress } from './DeploymentProgress';

const titles: Record<WebsiteOperation, string> = { install: '部署网站', update: '更新网站', start: '启动网站', stop: '停止网站', rollback: '回退上一版本', uninstall: '卸载并保留数据', purge: '彻底删除数据', backup: '创建备份', restore: '恢复此备份', 'delete-backup': '删除此备份' };
const stages = [['prepare', '检查配置'], ['engine', '准备运行环境'], ['runtime', '校验网站组件'], ['service', '启动网站'], ['verify', '实际访问验证']];
const bytes = (value: number) => `${(value / 1024 ** 2).toFixed(1)} MiB`;

export function WebsitePanel({ instance, inspection }: { instance: Application; inspection: Inspection }) {
  const [status, setStatus] = useState<WebsiteStatus | null>(null);
  const [selection, setSelection] = useState<WebsiteSelection | null>(null);
  const [operation, setOperation] = useState<WebsiteOperation | null>(null);
  const [password, setPassword] = useState('');
  const [backupId, setBackupId] = useState('');
  const [transfer, setTransfer] = useState<'export' | 'import' | null>(null);
  const [backupPassword, setBackupPassword] = useState('');
  const [backupConfirm, setBackupConfirm] = useState('');
  const [notice, setNotice] = useState('');
  const [erase, setErase] = useState('');
  const [error, setError] = useState('');
  const [pollError, setPollError] = useState('');
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const epoch = useRef(0);
  const remoteId = instance.id.slice(4).replaceAll('-', '');
  const site = status?.site;
  const task = status?.task;
  const running = task && ['queued', 'running', 'waiting'].includes(task.status);
  const retry = task && ['failed', 'unconfirmed'].includes(task.status);
  const replaceable = task?.status === 'failed' && (task.rejected || site?.operation?.safe_to_replace);
  const sudo = inspection.probe.privilege === 'password-required';
  const maintenance = ['stop', 'uninstall', 'purge'].includes(site?.operation?.request?.operation || '');
  const backupTask = ['backup', 'delete-backup'].includes(site?.operation?.request?.operation || '');
  const visibleStages = backupTask ? [['prepare', '检查配置'], ['service', '处理备份'], ['verify', '校验备份结果']] : maintenance ? [['prepare', '检查配置'], ['engine', '检查运行环境'], ['service', '维护网站'], ['verify', '核对结果']] : stages;
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      const started = epoch.current;
      try {
        const value = await unwrap(desktop?.websiteStatus(instance.id));
        if (active && !busyRef.current && started === epoch.current) { setStatus(value); setPollError(''); }
      } catch (cause) { if (active && !busyRef.current && started === epoch.current) setPollError(cause instanceof Error ? cause.message : '连接中断，进度暂时未知'); }
      finally { if (active) timer = setTimeout(() => void poll(), 4000); }
    }
    void poll();
    return () => { active = false; clearTimeout(timer); };
  }, [instance.id]);
  async function run(action: () => Promise<void>) {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true); setError(''); setNotice(''); epoch.current++;
    try { await action(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '操作没有完成'); }
    finally { epoch.current++; busyRef.current = false; setBusy(false); }
  }
  async function submit() {
    if (!operation) return;
    const value = await unwrap(desktop?.websiteSubmit({ id: instance.id, operation, revision: site?.revision || 0,
      selection: ['install', 'update'].includes(operation) ? selection?.selection : undefined, confirm: true,
      backupId: ['restore', 'delete-backup'].includes(operation) ? backupId : undefined,
      eraseInstanceId: operation === 'purge' ? erase : undefined, password }));
    setStatus(current => ({ site: current?.site || null, health: current?.health || 'unknown', task: value }));
    setPassword(''); setOperation(null); setSelection(null);
  }
  async function transferBackup() {
    if (transfer === 'export') {
      const value = await unwrap(desktop?.websiteBackupExport({ id: instance.id, backupId, password: backupPassword, sudoPassword: password }));
      if (value.exported) setNotice('加密备份已导出到所选位置');
    } else {
      const value = await unwrap(desktop?.websiteBackupImport({ password: backupPassword }));
      if (value) { setSelection(value); setNotice('备份校验通过，等待部署确认，服务器尚未变更'); }
    }
    setTransfer(null); setBackupPassword(''); setBackupConfirm(''); setPassword('');
  }
  function openTransfer(kind: 'export' | 'import', id = '') {
    setTransfer(kind); setBackupId(id); setBackupPassword(''); setBackupConfirm(''); setPassword('');
  }
  const blocked = busy || Boolean(running || retry && !replaceable || pollError) || !status;
  return <div className="website-detail">
    <dl className="application-impact"><div><dt>服务状态</dt><dd>{pollError ? '连接中断，状态未知' : !status ? '正在读取' : !site && task ? running ? '正在部署，尚未就绪' : retry ? '部署需处理' : '等待网站状态' : site?.status === 'running' ? ({ ready: '运行中 · 页面验证通过', unhealthy: '页面验证失败', unknown: '访问暂时不可确认', stopped: '未运行' })[status.health] : ({ stopped: '已停止', retained: '已卸载 · 数据保留', removed: '数据已删除', not_installed: '待安装' })[site?.status || 'not_installed']}</dd></div>
      <div><dt>当前内容版本</dt><dd><code>{site?.current?.checksum.slice(0, 12) || '未部署'}</code></dd></div><div><dt>网站数据</dt><dd>{site ? bytes(site.data_bytes) : '0 MiB'}</dd></div><div><dt>数据目录</dt><dd>{site?.data_directory || `/var/lib/gaoji-app/applications/${remoteId}`}</dd></div>
      <div><dt>访问端口</dt><dd>{site?.current ? `127.0.0.1:${site.current.port}` : '部署时自动分配，仅本机监听'}</dd></div></dl>
    {site?.current && <div className="website-actions"><button className="primary" disabled={busy || status?.health !== 'ready' || Boolean(pollError)} onClick={() => void run(async () => { await unwrap(desktop?.openWebsite(instance.id)); })}><ExternalLink size={16} />打开网站</button>
      <button className="secondary" disabled={blocked || site.status === 'removed'} onClick={() => setOperation(site.status === 'running' ? 'stop' : 'start')}>{site.status === 'running' ? <Square size={16} /> : <Play size={16} />}{site.status === 'running' ? '停止' : '启动'}</button>
      {site.previous && <button className="secondary" disabled={blocked} onClick={() => setOperation('rollback')}><RotateCcw size={16} />回退</button>}
      <button className="text-button danger" disabled={blocked} onClick={() => setOperation(site.status === 'retained' ? 'purge' : 'uninstall')}><Trash2 size={16} />{site.status === 'retained' ? '彻底删除数据' : '卸载'}</button></div>}
    {site?.status !== 'removed' && !running && (!retry || replaceable) && <div className="website-upload"><button className="secondary" disabled={blocked} onClick={() => void run(async () => { const value = await unwrap(desktop?.selectWebsite()); if (value) setSelection(value); })}><FolderOpen size={17} />选择网页文件夹</button>
      {selection && <><span><strong>{selection.name}</strong><small>{selection.files} 个文件 · {bytes(selection.bytes)}</small></span><button className="primary" disabled={blocked} onClick={() => setOperation(site?.current ? 'update' : 'install')}><Upload size={16} />{site?.current ? '更新网站' : '部署网站'}</button></>}</div>}
    {site?.status !== 'removed' && <section className="website-backups"><div className="website-actions"><h3>网站备份</h3><button className="secondary" disabled={blocked || !site?.current} onClick={() => setOperation('backup')}><Archive size={16} />创建备份</button><button className="secondary" disabled={blocked} onClick={() => openTransfer('import')}><Upload size={16} />导入备份</button></div>
      <ul className="website-backup-list">{(site?.backups || []).map(item => <li key={item.id}><div><strong>{new Date(item.created_at * 1000).toLocaleString('zh-CN', { hour12: false })}</strong><small>{bytes(item.bytes)} · {item.files} 个文件 · {item.id.slice(0, 8)}</small></div><div className="website-actions"><button className="icon-button" title="加密导出" aria-label="加密导出" disabled={blocked} onClick={() => openTransfer('export', item.id)}><Download size={17} /></button><button className="icon-button" title="恢复此备份" aria-label="恢复此备份" disabled={blocked} onClick={() => { setBackupId(item.id); setOperation('restore'); }}><RotateCcw size={17} /></button><button className="icon-button danger" title="删除此备份" aria-label="删除此备份" disabled={blocked} onClick={() => { setBackupId(item.id); setOperation('delete-backup'); }}><Trash2 size={17} /></button></div></li>)}</ul>
      {!site?.backups?.length && <p className="muted">暂无网站备份</p>}</section>}
    {notice && <div className="notice" role="status"><Check size={17} /><span>{notice}</span></div>}
    {task && <><ol className="install-steps website-steps">{visibleStages.map(([id, title], index) => <li key={id} className="install-step"><span className="step-number">{task.completed.includes(id) ? <Check size={14} /> : running && task.stage === id ? <LoaderCircle size={14} className="spin" /> : index + 1}</span><span>{title}</span></li>)}</ol><div className={`notice ${retry ? 'warning' : ''}`} role="status"><CircleAlert size={17} /><span>{task.message}</span></div></>}
    {pollError && <div className="notice warning" role="status"><CircleAlert size={17} /><span>{pollError}。已提交的任务保留在服务器。</span></div>}
    {task && <DeploymentProgress task={task} disconnected={Boolean(pollError)} />}
    {error && <div className="notice error" role="alert"><CircleAlert size={17} /><span>{error}</span></div>}
    {site?.cleanup?.message && <div className="notice warning"><CircleAlert size={17} /><span>{site.cleanup.message}</span></div>}
    {retry && !task.rejected && <div className="website-retry">{sudo && <label className="field"><span>服务器 sudo 密码</span><input type="password" value={password} onChange={event => setPassword(event.target.value)} autoComplete="off" /></label>}<button className="secondary" disabled={busy || sudo && !password} onClick={() => void run(async () => {
      const value = await unwrap(desktop?.websiteResume({ id: instance.id, password })); setPassword(''); setStatus(current => ({ site: current?.site || null, health: current?.health || 'unknown', task: value }));
    })}><RotateCcw size={16} />继续原任务</button></div>}
    {operation && <div className="modal-overlay"><div className="modal" role="dialog" aria-modal="true" aria-labelledby="website-operation"><button className="icon-button modal-close" aria-label="关闭" title="关闭" disabled={busy} onClick={() => setOperation(null)}><X size={18} /></button>
      <h2 id="website-operation">{titles[operation]}</h2><p>{instance.name}</p><dl className="application-impact"><div><dt>操作对象</dt><dd><code>{remoteId}</code></dd></div><div><dt>影响</dt><dd>{operation === 'purge' ? '永久删除此实例网页版本和服务器备份；不删除其他软件或共享镜像' : operation === 'uninstall' ? '删除此网站容器和专属网络，网页文件和备份保留' : operation === 'stop' ? '此网站暂时不可访问，数据保留' : operation === 'backup' ? '保存当前网页的独立副本，不停止网站；最多保留 20 份' : operation === 'delete-backup' ? '永久删除所选服务器备份；当前网站和已导出的文件不变' : '校验页面后启用此版本，其他软件不变'}</dd></div>
      {['restore', 'delete-backup'].includes(operation) && <div><dt>备份编号</dt><dd><code>{backupId}</code></dd></div>}
      {['install', 'update'].includes(operation) && <><div><dt>网页文件</dt><dd>{selection?.name} · {bytes(selection?.bytes || 0)}</dd></div><div><dt>运行上限</dt><dd>0.5 核 · 128 MiB 内存 · 本机随机端口</dd></div><div><dt>运行环境</dt><dd>{inspection.probe.dockerPath ? '复用现有 Docker' : '安装 Docker 与 Compose'}</dd></div></>}</dl>
      {operation === 'purge' && <label className="field"><span>输入上方实例编号，确认永久删除</span><input value={erase} onChange={event => setErase(event.target.value)} autoComplete="off" /></label>}
      {sudo && <label className="field"><span>服务器 sudo 密码</span><input type="password" value={password} onChange={event => setPassword(event.target.value)} autoComplete="off" /></label>}
      <div className="form-actions"><button className="secondary" disabled={busy} onClick={() => setOperation(null)}>取消</button><button className="primary" disabled={busy || sudo && !password || operation === 'purge' && erase !== remoteId} onClick={() => void run(submit)}>{busy ? <LoaderCircle size={16} className="spin" /> : <Check size={16} />}{titles[operation]}</button></div>
    </div></div>}
    {transfer && <div className="modal-overlay"><div className="modal" role="dialog" aria-modal="true" aria-labelledby="website-transfer"><button className="icon-button modal-close" title="关闭" aria-label="关闭" disabled={busy} onClick={() => { setTransfer(null); setBackupPassword(''); setBackupConfirm(''); setPassword(''); }}><X size={18} /></button><h2 id="website-transfer">{transfer === 'export' ? '加密导出网站备份' : '导入网站备份'}</h2>
      <label className="field"><span>{transfer === 'export' ? '设置独立备份密码（至少 12 个字符）' : '备份密码'}</span><input type="password" value={backupPassword} onChange={event => setBackupPassword(event.target.value)} autoComplete="off" /></label>
      {transfer === 'export' && <label className="field"><span>再次输入备份密码</span><input type="password" value={backupConfirm} onChange={event => setBackupConfirm(event.target.value)} autoComplete="off" /></label>}
      {transfer === 'export' && sudo && <label className="field"><span>服务器 sudo 密码</span><input type="password" value={password} onChange={event => setPassword(event.target.value)} autoComplete="off" /></label>}
      {error && <div className="notice error" role="alert">{error}</div>}
      <div className="form-actions"><button className="primary" disabled={busy || backupPassword.length < 12 || transfer === 'export' && (backupConfirm !== backupPassword || sudo && !password)} onClick={() => void run(transferBackup)}>{busy ? <LoaderCircle size={16} className="spin" /> : transfer === 'export' ? <Download size={16} /> : <Upload size={16} />}{transfer === 'export' ? '选择保存位置并导出' : '选择备份并校验'}</button></div>
    </div></div>}
  </div>;
}
