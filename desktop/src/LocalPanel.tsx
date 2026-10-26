import { useEffect, useRef, useState } from 'react';
import { Check, CircleAlert, Download, HardDrive, LoaderCircle, Play, RefreshCw } from 'lucide-react';
import { desktop, unwrap } from './api';
import type { LocalInspection, LocalProgress } from './api';
import { SetupPanel } from './SetupPanel';
import { UpdatePanel } from './UpdatePanel';
import { StoragePanel } from './StoragePanel';
import { ImageOptions } from './ImageOptions';
import type { ImageOptions as ImagePolicy } from './api';

const stages = [['runtime', '机器人与管理镜像'], ['verify', '字体、浏览器与依赖校验'],
  ['initialize', '独立数据卷'], ['database', '数据库与 QQ 组件'], ['migrate', '数据库初始化'], ['qq', 'QQ 登录入口']];

export function LocalPanel() {
  const [inspection, setInspection] = useState<LocalInspection | null>(null);
  const [progress, setProgress] = useState<LocalProgress | null>(null);
  const [imageOptions, setImageOptions] = useState<ImagePolicy>({ mode: 'auto', source: 'official' });
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [view, setView] = useState<'setup' | 'versions' | 'storage'>('setup');
  const [dirty, setDirty] = useState(false);
  const [storageDirty, setStorageDirty] = useState(false);
  const [storageOpened, setStorageOpened] = useState(false);
  const [now, setNow] = useState(Date.now());
  const busyRef = useRef(false);
  async function inspect() { setInspection(await unwrap(desktop?.localInspect())); }
  async function run(operation: () => Promise<unknown>) {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true); setError('');
    try { await operation(); } catch (cause) { setError(cause instanceof Error ? cause.message : '操作未完成'); }
    finally { busyRef.current = false; setBusy(false); }
  }
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const clock = setInterval(() => setNow(Date.now()), 1000);
    void unwrap(desktop?.localInspect()).then(value => { if (active) setInspection(value); }).catch(cause => { if (active) setError(cause.message); });
    async function poll() {
      try {
        const value = await unwrap(desktop?.localStatus());
        if (active) setProgress(value);
      } catch (cause) { if (active) setError(cause instanceof Error ? cause.message : '本机状态暂不可用'); }
      finally { if (active) timer = setTimeout(() => void poll(), 3000); }
    }
    void poll();
    return () => { active = false; clearTimeout(timer); clearInterval(clock); };
  }, []);
  const installed = progress?.status === 'awaiting_setup';
  const running = progress?.status === 'running' || progress?.status === 'queued';
  return <section className="inspection">
    <div className="section-heading"><div><span className="eyebrow">本机部署</span><h2>这台电脑上的高级</h2></div>
      <button className="icon-button" title="重新检测本机环境" aria-label="重新检测本机环境" disabled={busy} onClick={() => void run(inspect)}><RefreshCw size={19} className={busy ? 'spin' : ''} /></button></div>
    <dl className="stats"><div><dt>运行位置</dt><dd>{inspection?.platform === 'win32' ? 'Windows / WSL2' : 'macOS'}</dd></div>
      <div><dt><HardDrive size={16} /> 本机可用空间</dt><dd>{inspection ? (inspection.freeBytes / 1024 ** 3).toFixed(1) : '-'} <small>GiB</small></dd></div>
      <div><dt>Docker Desktop</dt><dd>{inspection?.ready ? '已就绪' : inspection?.installed ? '需要检查' : '尚未就绪'}</dd></div></dl>
    {error && <div className="notice error" role="alert"><CircleAlert size={18} /><span>{error}</span></div>}
    {!installed && <>
      {inspection?.blockers.map(text => <div className="notice warning" key={text}><CircleAlert size={18} /><span>{text}</span></div>)}
      {!inspection?.ready && <div className="form-actions">
        <button className="secondary" disabled={busy} onClick={() => void run(() => unwrap(desktop?.localDocker('download')))}><Download size={16} />下载 Docker Desktop</button>
        <button className="secondary" disabled={busy} onClick={() => void run(() => unwrap(desktop?.localDocker('open')))}><Play size={16} />打开 Docker Desktop</button>
      </div>}
      {progress && <div className={`notice ${progress.status === 'failed' ? 'error' : ''}`} role="status">{running ? <LoaderCircle className="spin" size={18} /> : <CircleAlert size={18} />}<span>{progress.message}</span></div>}
      {progress && <div className="install-steps" aria-label="本机安装进度">{stages.map(([stage, title], index) => {
        const done = progress.completed?.includes(stage);
        const current = progress.stage === stage;
        return <div className="install-step" key={stage}><span className={`step-number ${done ? 'green' : ''}`}>
          {done ? <Check size={13} /> : current && running ? <LoaderCircle size={13} className="spin" /> : index + 1}</span>
          <span>{title}</span><span className="muted">{done ? '已完成' : current ? running ? '进行中' : '待恢复' : '待执行'}</span></div>;
      })}</div>}
      {progress?.stage_started_at && <div className="install-detail">
        <span>当前步骤 {Math.floor(Math.max(0, (running ? now / 1000 : progress.updated_at || progress.stage_started_at) - progress.stage_started_at) / 60)} 分</span>
        {progress.updated_at && <span>状态更新于 {new Date(progress.updated_at * 1000).toLocaleTimeString('zh-CN', { hour12: false })}</span>}
        {progress.progress && <strong>{progress.progress.detail}</strong>}
        {(progress.progress?.quiet_seconds || 0) >= 60 && <span className="warning">暂未收到新的组件输出</span>}
      </div>}
      <ImageOptions value={imageOptions} onChange={setImageOptions} disabled={busy || running} />
      <div className="form-actions"><button className="primary" disabled={busy || running || !inspection?.ready}
        onClick={() => void run(async () => { setProgress(await unwrap(desktop?.localStart(imageOptions))); await inspect(); })}>
        {busy || running ? <LoaderCircle className="spin" size={17} /> : <Play size={17} />}{running ? '正在安装' : progress ? '继续本机安装 / 连接' : '安装到这台电脑'}</button></div>
    </>}
    {installed && <>
      <div className="setup-tabs" role="tablist" aria-label="本机实例管理">
        <button role="tab" aria-selected={view === 'setup'} onClick={() => setView('setup')}>QQ 与模型</button>
        <button role="tab" aria-selected={view === 'versions'} onClick={() => setView('versions')}>更新与回退</button>
        <button role="tab" aria-selected={view === 'storage'} onClick={() => { setStorageOpened(true); setView('storage'); }}>存储管理</button>
      </div>
      <div hidden={view !== 'setup'}><SetupPanel id="desktop-local" onStep={() => {}} onDirty={setDirty} /></div>
      {view === 'versions' && <UpdatePanel id="desktop-local" dirty={dirty || storageDirty} passwordRequired={false} />}
      {storageOpened && <div hidden={view !== 'storage'}><StoragePanel id="desktop-local" active={view === 'storage'} onDirty={setStorageDirty} /></div>}
    </>}
  </section>;
}
