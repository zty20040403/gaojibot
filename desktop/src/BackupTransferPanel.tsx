import { useEffect, useRef, useState } from 'react';
import { Check, CircleAlert, Download, LoaderCircle, Upload, X } from 'lucide-react';
import { desktop, unwrap } from './api';
import type { Backup, BackupTransferStatus } from './api';

export function BackupTransferPanel({ id, supported, disabled, exporting, onClose, onBusy, onImported }: {
  id: string; supported: boolean; disabled: boolean; exporting: Backup | null;
  onClose: () => void; onBusy: (value: boolean) => void; onImported: () => void;
}) {
  const [mode, setMode] = useState<'export' | 'import' | null>(null);
  const [selection, setSelection] = useState<{ selection: string; name: string } | null>(null);
  const [password, setPassword] = useState('');
  const [repeat, setRepeat] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [transfer, setTransfer] = useState<BackupTransferStatus | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const gate = useRef(false);
  const alive = useRef(true);
  useEffect(() => { onBusy(busy); }, [busy, onBusy]);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; onBusy(false); };
  }, [onBusy]);
  useEffect(() => {
    if (exporting) { setMode('export'); setPassword(''); setRepeat(''); setError(''); }
  }, [exporting]);
  useEffect(() => {
    if (mode && !dialog.current?.open) dialog.current?.showModal();
    else if (!mode) dialog.current?.close();
  }, [mode]);
  useEffect(() => {
    if (!supported) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try { const value = await unwrap(desktop?.backupTransferStatus(id)); if (!disposed) setTransfer(value); }
      catch { /* Submission errors remain in the dialog; polling must not erase them. */ }
      if (!disposed) timer = setTimeout(() => void poll(), 1000);
    }
    void poll();
    return () => { disposed = true; clearTimeout(timer); };
  }, [id, supported]);
  function close() { if (!gate.current) { setMode(null); setPassword(''); setRepeat(''); setError(''); setSelection(null); onClose(); } }
  async function choose() {
    if (gate.current || disabled) return;
    gate.current = true; setBusy(true); setError('');
    try {
      const value = await unwrap(desktop?.selectBackup());
      if (alive.current && value) { setSelection(value); setMode('import'); setPassword(''); setRepeat(''); }
    } catch (cause) { if (alive.current) setError(cause instanceof Error ? cause.message : '文件选择未完成'); }
    finally { gate.current = false; if (alive.current) setBusy(false); }
  }
  async function submit() {
    if (gate.current || disabled || !supported || !mode || password.length < 12 || mode === 'export' && password !== repeat) return;
    gate.current = true; setBusy(true); setError('');
    try {
      if (mode === 'export' && exporting) await unwrap(desktop?.exportBackup({ id, backupId: exporting.id, password }));
      else if (mode === 'import' && selection) { await unwrap(desktop?.importBackup({ id, selection: selection.selection, password })); onImported(); }
      else throw new Error('请重新选择备份');
      if (alive.current) { setMode(null); onClose(); }
    } catch (cause) { if (alive.current) setError(cause instanceof Error ? cause.message : '备份传输未完成'); }
    finally { gate.current = false; if (alive.current) { setBusy(false); setPassword(''); setRepeat(''); } }
  }
  const progress = <>{transfer && <div className={`notice ${transfer.status === 'failed' ? 'error' : 'install-notice'}`} role="status">
    {transfer.status === 'running' ? <LoaderCircle size={17} className="spin" /> : transfer.status === 'completed' ? <Check size={17} /> : <CircleAlert size={17} />}
    <div className="backup-transfer-progress"><strong>{transfer.name}</strong><p>{transfer.message}</p>
      {transfer.total > 0 && <progress max={transfer.total} value={transfer.bytes} aria-label="备份传输进度" />}
      {transfer.status === 'running' && <button type="button" className="text-button" onClick={() => void unwrap(desktop?.cancelBackupTransfer(id)).catch(cause => setError(String(cause)))}><X size={14} />取消传输</button>}
    </div>
  </div>}</>;
  return <div className="backup-transfers">
    <button type="button" className="secondary" disabled={!supported || disabled || busy} title={supported ? '从电脑导入加密备份，不立即恢复' : '服务端更新后可用'} onClick={() => void choose()}><Upload size={16} />导入加密备份</button>
    {!mode && progress}
    {!mode && error && <div className="notice error" role="alert"><CircleAlert size={17} /><span>{error}</span></div>}
    <dialog ref={dialog} className="modal backup-dialog" aria-labelledby="backup-transfer-title" onCancel={event => { if (gate.current) event.preventDefault(); else close(); }}>
      <button type="button" className="icon-button modal-close" aria-label="关闭备份传输" title="关闭备份传输" disabled={busy} onClick={close}><X size={18} /></button>
      <h2 id="backup-transfer-title">{mode === 'export' ? '导出加密备份' : '导入加密备份'}</h2>
      <p>{mode === 'export' ? '文件包含数据库、QQ 会话、模型密钥及用户文件。请设置独立长密码；遗忘后无法找回。'
        : `${selection?.name || ''}。只将备份放入当前服务器的列表，不自动覆盖数据或登录 QQ。运行版本必须匹配。`}</p>
      <label className="field transfer-password"><span>备份密码</span><input type="password" autoComplete={mode === 'export' ? 'new-password' : 'off'} minLength={12} maxLength={1024} disabled={busy} value={password} onChange={event => setPassword(event.target.value)} /></label>
      {mode === 'export' && <label className="field"><span>再次输入备份密码</span><input type="password" autoComplete="new-password" disabled={busy} value={repeat} onChange={event => setRepeat(event.target.value)} /></label>}
      {error && <div className="notice error" role="alert"><CircleAlert size={17} /><span>{error}</span></div>}
      {busy && progress}
      <div className="form-actions"><button type="button" className="secondary" disabled={busy} onClick={close}>取消</button>
        <button type="button" className="primary" disabled={busy || disabled || !supported || password.length < 12 || mode === 'export' && password !== repeat} onClick={() => void submit()}>
          {busy ? <LoaderCircle size={16} className="spin" /> : mode === 'export' ? <Download size={16} /> : <Upload size={16} />}{busy ? '正在处理' : mode === 'export' ? '加密并保存' : '校验并导入'}</button></div>
    </dialog>
  </div>;
}
