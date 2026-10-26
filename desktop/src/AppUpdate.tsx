import { useEffect, useRef, useState } from 'react';
import { ArrowDownToLine, Check, CircleAlert, LoaderCircle, PackageOpen, RefreshCw, RotateCw, X } from 'lucide-react';
import { desktop, unwrap } from './api';
import type { AppUpdateState } from './api';

export function AppUpdate() {
  const dialog = useRef<HTMLDialogElement>(null);
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<AppUpdateState | null>(null);
  const [error, setError] = useState('');
  const [working, setWorking] = useState(false);
  const request = useRef(false);
  const generation = useRef(0);
  const busy = working || state?.phase === 'checking' || state?.phase === 'downloading';
  useEffect(() => desktop?.onAppUpdateRequested(() => setOpen(true)), []);
  useEffect(() => {
    if (open) dialog.current?.showModal(); else dialog.current?.close();
    if (!open) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      const at = generation.current;
      try {
        const value = await unwrap(desktop?.appUpdateStatus());
        if (active && at === generation.current) setState(value);
      } catch (cause) { if (active) setError(cause instanceof Error ? cause.message : '无法获取更新状态'); }
      finally { if (active) timer = setTimeout(() => void poll(), 600); }
    }
    void poll();
    return () => { active = false; clearTimeout(timer); };
  }, [open]);
  async function run(action: 'check' | 'download' | 'install') {
    if (!desktop || request.current) return;
    request.current = true; generation.current++; setWorking(true); setError('');
    try {
      const value = await unwrap(action === 'check' ? desktop.checkAppUpdate()
        : action === 'download' ? desktop.downloadAppUpdate() : desktop.installAppUpdate());
      generation.current++; setState(value);
    } catch (cause) { setError(cause instanceof Error ? cause.message : '更新未完成'); }
    finally { request.current = false; setWorking(false); }
  }
  return <><button className="icon-button" title="检查 App 更新" aria-label="检查 App 更新" disabled={!desktop}
    onClick={() => setOpen(true)}><ArrowDownToLine size={18} /></button>
    <dialog ref={dialog} className="modal app-update-dialog" aria-labelledby="app-update-title" onCancel={() => setOpen(false)} onClose={() => setOpen(false)}>
      <button className="modal-close icon-button" title="关闭更新窗口" aria-label="关闭更新窗口" onClick={() => setOpen(false)}><X size={18} /></button>
      <div className="app-update-brand"><img src="./brand.png" alt="" /><div><h2 id="app-update-title">高级 App 更新</h2><span className="muted">GitHub · zty20040403/gaoji-app</span></div></div>
      <dl className="app-update-versions"><div><dt>当前版本</dt><dd>{state ? `v${state.currentVersion}` : '读取中'}</dd></div>
        <div><dt>发布版本</dt><dd>{state?.targetVersion ? `v${state.targetVersion}` : '尚未获取'}</dd></div></dl>
      <div className={`notice ${state?.phase === 'error' ? 'error' : state?.phase === 'ready' || state?.phase === 'up_to_date' ? 'install-notice' : 'warning'}`} role="status">
        {busy ? <LoaderCircle className="spin" size={18} /> : state?.phase === 'ready' || state?.phase === 'up_to_date' ? <Check size={18} /> : <CircleAlert size={18} />}
        <span>{state?.message || '正在读取更新状态'}</span></div>
      {state?.phase === 'downloading' && <div className="app-update-progress"><progress aria-label="App 下载进度" max={100} value={state.percent} />
        <span>{Math.floor(state.percent)}% · {(state.bytes / 1024 ** 2).toFixed(1)} / {(state.total / 1024 ** 2).toFixed(1)} MiB</span></div>}
      {state?.checkedAt && <p>检查时间：{new Date(state.checkedAt).toLocaleString('zh-CN', { hour12: false })}</p>}
      {state?.notes && <details className="app-update-notes"><summary>更新内容</summary><pre>{state.notes}</pre></details>}
      <p>仅更新这台电脑上的 App。连接信息、登录配置和服务器数据保留。</p>
      {state?.mode === 'installer' && <p>Mac 预览版：下载校验后打开安装包，退出旧 App，再拖入“应用程序”并选择“替换”。</p>}
      {error && <div className="notice error" role="alert"><CircleAlert size={18} /><span>{error}</span></div>}
      <div className="form-actions"><button className="secondary" disabled={busy || !state} onClick={() => void run('check')}><RefreshCw size={16} />检查更新</button>
        {state?.phase === 'available' && <button className="primary" disabled={busy} onClick={() => void run('download')}><ArrowDownToLine size={16} />下载更新</button>}
        {state?.phase === 'error' && state.targetVersion && <button className="secondary" disabled={busy} onClick={() => void run('check')}><RotateCw size={16} />重新检查</button>}
        {state?.phase === 'ready' && <button className="primary" disabled={busy} onClick={() => void run('install')}>
          {state.mode === 'native' ? <RotateCw size={16} /> : <PackageOpen size={16} />}{state.mode === 'native' ? '重启并更新' : '打开安装包'}</button>}
      </div>
    </dialog></>;
}
