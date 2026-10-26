import { useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronUp, CircleAlert, HardDrive, LoaderCircle, RefreshCw, Save, Trash2, X } from 'lucide-react';
import { instanceCall } from './api';
import type { StoragePolicy, StorageStatus, StoragePlan, StorageItem } from './api';

const names: Record<string, string> = { database: '当前数据库', state: '聊天与任务状态', files: '用户文件与产物', napcat: 'QQ 会话', cache: '运行缓存', logs: '日志', assets: '转写模型等资源' };
function size(value: number | null | undefined) { return value == null ? '未完整统计' : value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(2)} GiB` : `${(value / 1024 ** 2).toFixed(1)} MiB`; }
function time(value: number) { return new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }); }

function Copies({ items }: { items: StorageItem[] }) {
  const [expanded, setExpanded] = useState(false);
  return <><div className="storage-copies">{(expanded ? items : items.slice(0, 5)).map(item => <div className="storage-copy" key={item.id}>
    <div><strong>{item.kind === 'backup' ? '完整备份' : '更新 / 恢复副本'}</strong><span>{time(item.created_at)}</span>
      <details><summary>查看目录</summary>{item.paths.map(path => <code key={path}>{path}</code>)}</details></div>
    <span>{size(item.bytes)}</span><span className={`tag ${item.eligible ? '' : 'neutral'}`}>{item.eligible ? '符合清理策略' : item.protected}</span>
  </div>)}</div>{items.length > 5 && <button type="button" className="text-button" onClick={() => setExpanded(value => !value)}>{expanded ? <ChevronUp size={15} /> : <ChevronDown size={15} />}{expanded ? '收起' : `全部 ${items.length} 项`}</button>}</>;
}

export function StoragePanel({ id, active, onDirty }: { id: string; active: boolean; onDirty: (value: boolean) => void }) {
  const [status, setStatus] = useState<StorageStatus | null>(null);
  const [draft, setDraft] = useState<StoragePolicy | null>(null);
  const [revision, setRevision] = useState(0);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [pollError, setPollError] = useState('');
  const [confirmation, setConfirmation] = useState<'policy' | StoragePlan | null>(null);
  const [notice, setNotice] = useState('');
  const initial = useRef(false);
  const gate = useRef(false);
  const alive = useRef(true);
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { onDirty(dirty); }, [dirty, onDirty]);
  useEffect(() => { alive.current = true; return () => { alive.current = false; onDirty(false); }; }, [onDirty]);
  useEffect(() => {
    if (!active) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const value = await instanceCall<StorageStatus>(id, 'storageStatus');
        if (!disposed) {
          setStatus(value); setPollError('');
          if (!initial.current) { initial.current = true; setDraft(value.policy); setRevision(value.revision); }
        }
      } catch (cause) { if (!disposed) setPollError(cause instanceof Error ? cause.message : '存储状态暂不可读'); }
      if (!disposed) timer = setTimeout(() => void poll(), 5000);
    }
    void poll();
    return () => { disposed = true; clearTimeout(timer); };
  }, [id, active]);
  useEffect(() => {
    if (confirmation && !dialog.current?.open) dialog.current?.showModal();
    else if (!confirmation) dialog.current?.close();
  }, [confirmation]);
  async function refresh() { const value = await instanceCall<StorageStatus>(id, 'storageStatus'); if (alive.current) setStatus(value); return value; }
  async function run(label: string, operation: () => Promise<void>) {
    if (gate.current) return;
    gate.current = true; setBusy(label); setError(''); setNotice('');
    try { await operation(); }
    catch (cause) { if (alive.current) setError(cause instanceof Error ? cause.message : '操作未完成'); }
    finally { gate.current = false; if (alive.current) setBusy(''); }
  }
  function change<K extends keyof StoragePolicy>(key: K, value: StoragePolicy[K]) { setDraft(current => current && { ...current, [key]: value }); setDirty(true); }
  async function save() {
    if (!draft) return;
    const value = await instanceCall<{ revision: number; policy: StoragePolicy }>(id, 'storagePolicy', { revision, policy: draft });
    if (alive.current) { setRevision(value.revision); setDraft(value.policy); setDirty(false); setConfirmation(null); setNotice('保留策略已保存'); }
    await refresh();
  }
  const pending = status?.job && ['queued', 'running'].includes(status.job.status);
  const blocked = Boolean(busy || pending);
  const valid = draft && Number.isInteger(draft.keep_backups) && draft.keep_backups >= 1 && draft.keep_backups <= 100
    && Number.isInteger(draft.keep_restore_points) && draft.keep_restore_points >= 1 && draft.keep_restore_points <= 100
    && Number.isInteger(draft.min_age_days) && draft.min_age_days >= 1 && draft.min_age_days <= 3650;
  const warnings = confirmation && confirmation !== 'policy' ? confirmation.warnings : status?.scan?.warnings || [];
  return <section className="storage-panel">
    <div className="section-heading"><div><h3>存储与保留</h3><span className="muted">仅当前实例</span></div>
      <button type="button" className="secondary" disabled={blocked} onClick={() => void run('扫描空间', async () => { await instanceCall(id, 'storageScan'); await refresh(); })}><RefreshCw size={16} />扫描空间</button></div>
    {pollError && <div className="notice error" role="alert"><CircleAlert size={17} /><span>{pollError}</span></div>}
    {!confirmation && error && <div className="notice error" role="alert"><CircleAlert size={17} /><span>{error}</span></div>}
    {notice && <div className="notice install-notice" role="status">{notice}</div>}
    {status && <>
      <div className="storage-metrics"><div><span>磁盘可用</span><strong>{size(status.disk.free_bytes)}</strong></div><div><span>磁盘总容量</span><strong>{size(status.disk.total_bytes)}</strong></div>
        <div><span>可清理副本估算</span><strong title="已分配磁盘空间估算，实际释放受硬链接、快照及其他进程影响">{size(status.scan?.candidate_bytes)}</strong></div></div>
      {status.capacity && <section className="capacity-status" aria-label="实例容量状态">
        <div className="section-heading"><h4>实例容量</h4><span className={`tag ${status.capacity.state === 'ready' ? '' : 'neutral'}`} role="status">{
          { ready: '可接收新任务', blocked: '新大文件操作已暂停', unknown: '容量待确认' }[status.capacity.state]
        }</span></div>
        <div className="storage-metrics"><div><span>已用 / 数据额度</span><strong>{size(status.capacity.complete ? status.capacity.used_bytes : null)} / {status.capacity.policy ? `${status.capacity.policy.max_data_gib} GiB` : '待确认'}</strong></div>
          <div><span>每块磁盘最低预留</span><strong>{status.capacity.policy ? `${status.capacity.policy.reserve_free_gib} GiB` : '待确认'}</strong></div></div>
        <div className="storage-live"><div><span>实例目录（含数据库与备份）</span><span>{size(status.capacity.data_bytes)}</span></div>
          <div><span>专用沙盒工作卷与工具缓存</span><span>{size(status.capacity.sandbox_bytes)}</span></div>
          <div><span>容器新增数据</span><span>{size(status.capacity.writable_bytes)}</span></div>
          {status.capacity.disks?.map(disk => <div key={disk.name}><span>{disk.name === 'docker' ? 'Docker 所在磁盘可用' : '实例所在磁盘可用'}</span><span>{size(disk.free_bytes)}</span></div>)}</div>
        {status.capacity.reason && <p className="storage-warning" role="status"><CircleAlert size={15} />{status.capacity.reason}</p>}
        {status.capacity.sampled_at && <p className="muted">统计于 {time(status.capacity.sampled_at)}</p>}
      </section>}
      <form className="storage-policy" onSubmit={event => { event.preventDefault(); if (!valid || blocked) return; if (draft!.enabled) setConfirmation('policy'); else void run('保存策略', save); }}>
        <label className="checkbox"><input type="checkbox" checked={draft?.enabled || false} disabled={blocked} onChange={event => change('enabled', event.target.checked)} />每小时自动清理过期副本</label>
        <div className="storage-policy-fields"><label className="field"><span>至少保留完整备份（份）</span><input type="number" min={1} max={100} value={draft?.keep_backups ?? ''} disabled={blocked} onChange={event => change('keep_backups', Number(event.target.value))} /></label>
          <label className="field"><span>至少保留恢复点（个）</span><input type="number" min={1} max={100} value={draft?.keep_restore_points ?? ''} disabled={blocked} onChange={event => change('keep_restore_points', Number(event.target.value))} /></label>
          <label className="field"><span>副本至少保留（天）</span><input type="number" min={1} max={3650} value={draft?.min_age_days ?? ''} disabled={blocked} onChange={event => change('min_age_days', Number(event.target.value))} /></label></div>
        <div className="form-actions"><span className="muted">策略版本 {revision}{dirty ? ' · 有未保存更改' : ''}{status.revision !== revision ? ' · 服务器已有新版本' : ''}</span>
          <div className="storage-buttons"><button type="button" className="icon-button secondary" title="重新载入保留策略" aria-label="重新载入保留策略" disabled={blocked} onClick={() => void run('重新载入策略', async () => { const value = await refresh(); setDraft(value.policy); setRevision(value.revision); setDirty(false); })}><RefreshCw size={16} /></button>
            <button type="submit" className="primary" disabled={blocked || !dirty || !valid}><Save size={16} />保存保留策略</button></div></div>
      </form>
      <div className="storage-actions"><button type="button" className="secondary" disabled={blocked || dirty || Boolean(status.job?.resumable)} onClick={() => void run('生成清理预览', async () => { const plan = await instanceCall<StoragePlan>(id, 'storagePlan'); if (alive.current) setConfirmation(plan); })}><Trash2 size={16} />预览可清理内容</button>
        {status.scan && <span className="muted">扫描于 {time(status.scan.created_at)}</span>}</div>
      {busy && <div className="setup-busy" role="status"><LoaderCircle size={15} className="spin" />{busy}</div>}
      {status.job && <div className={`notice ${status.job.status === 'failed' ? 'error' : 'install-notice'}`} role="status"><HardDrive size={18} /><div className="storage-job"><strong>{status.job.message}</strong><p>{time(status.job.updated_at)} · 已处理 {status.job.items_done} 项</p>
        {status.job.free_after != null && <p>磁盘可用：{size(status.job.free_before)} → {size(status.job.free_after)}</p>}
        {status.job.resumable && <button type="button" className="secondary" disabled={blocked} onClick={() => void run('继续清理', async () => { await instanceCall(id, 'storageCleanup', { id: status.job!.id, confirm: true }); await refresh(); })}>继续上次清理</button>}</div></div>}
      {status.scan && <><h4>当前数据 · 不在清理范围</h4><div className="storage-live">{status.scan.live.map(item => <div key={item.name}><span>{names[item.name] || item.name}</span><span>{size(item.bytes)}</span></div>)}</div>
        <h4>已核对的备份与恢复点</h4>{status.scan.items.length ? <Copies items={status.scan.items} /> : <p className="muted">暂无可列出的副本</p>}</>}
    </>}
    {!confirmation && warnings.map(warning => <p className="storage-warning" key={warning}><CircleAlert size={15} />{warning}</p>)}
    <dialog ref={dialog} className="modal storage-dialog" aria-labelledby="storage-confirm-title" onCancel={event => { if (gate.current) event.preventDefault(); else setConfirmation(null); }}>
      <button className="icon-button modal-close" title="取消清理操作" aria-label="取消清理操作" disabled={Boolean(busy)} onClick={() => setConfirmation(null)}><X size={18} /></button>
      <h2 id="storage-confirm-title">{confirmation === 'policy' ? '应用自动清理策略？' : '清理预览'}</h2>
      <div className="storage-dialog-scroll">
      <p>{confirmation === 'policy' ? `每小时检查一次：保留至少 ${draft?.keep_backups} 份完整备份、${draft?.keep_restore_points} 个恢复点，且只删除超过 ${draft?.min_age_days} 天的多余副本。` : '只删除下面列出的副本及其恢复点记录，删除后无法用这些副本恢复。当前数据库、用户文件、容器镜像和其他应用不在本次清理范围。'}</p>
      {confirmation && confirmation !== 'policy' && <><p className="storage-estimate">{confirmation.items.length} 项 · 预计释放 {size(confirmation.estimated_bytes)}</p><Copies items={confirmation.items} /></>}
      {confirmation && warnings.map(warning => <p className="storage-warning" key={warning}>{warning}</p>)}
      {error && <div className="notice error" role="alert"><CircleAlert size={17} /><span>{error}</span></div>}
      </div>
      <div className="form-actions"><button className="secondary" disabled={Boolean(busy)} onClick={() => setConfirmation(null)}>取消</button><button className="primary" disabled={blocked || !confirmation || confirmation !== 'policy' && !confirmation.items.length}
        onClick={() => void run('提交清理设置', async () => { if (confirmation === 'policy') await save(); else if (confirmation) { await instanceCall(id, 'storageCleanup', { id: confirmation.id, confirm: true }); if (alive.current) setConfirmation(null); await refresh(); } })}>
        {busy ? <LoaderCircle size={16} className="spin" /> : <Trash2 size={16} />}{confirmation === 'policy' ? '确认应用策略' : '确认清理所列副本'}</button></div>
    </dialog>
  </section>;
}
