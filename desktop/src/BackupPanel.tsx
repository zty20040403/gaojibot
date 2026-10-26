import { useEffect, useRef, useState } from 'react';
import { Archive, ChevronDown, ChevronUp, CircleAlert, Download, RotateCcw, X } from 'lucide-react';
import { BackupTransferPanel } from './BackupTransferPanel';
import { instanceCall } from './api';
import type { Backup, InstanceStatus, OperationKind } from './api';

function size(bytes: number) { return bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GiB` : `${(bytes / 1024 ** 2).toFixed(1)} MiB`; }
function time(seconds: number) { return new Date(seconds * 1000).toLocaleString('zh-CN', { hour12: false }); }

export function BackupPanel({ id, status, disabled, onOperation }: {
  id: string; status: InstanceStatus; disabled: boolean; onOperation: (kind: OperationKind, backup?: string) => void;
}) {
  const [backups, setBackups] = useState<Backup[]>([]);
  const [error, setError] = useState('');
  const [expanded, setExpanded] = useState(false);
  const [transferSupported, setTransferSupported] = useState(false);
  const [transferBusy, setTransferBusy] = useState(false);
  const [exporting, setExporting] = useState<Backup | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [confirmation, setConfirmation] = useState<{ kind: 'backup' | 'restore'; backup?: Backup } | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const value = await instanceCall<{ backups: Backup[]; transfer?: { schema: number } }>(id, 'backups');
        if (!disposed) { setBackups(value.backups); setTransferSupported(value.transfer?.schema === 1); setError(''); }
      } catch (cause) { if (!disposed) setError(cause instanceof Error ? cause.message : '备份列表暂不可用'); }
      finally { if (!disposed) timer = setTimeout(() => void poll(), 5000); }
    };
    void poll();
    return () => { disposed = true; clearTimeout(timer); };
  }, [id, status.operation?.status, refresh]);
  useEffect(() => {
    if (confirmation && !dialog.current?.open) dialog.current?.showModal();
    else if (!confirmation) dialog.current?.close();
  }, [confirmation]);
  const blocked = disabled || status.maintenance_pending || transferBusy;
  return <section className="backup-section">
    <div className="backup-heading"><h3>备份与恢复</h3><span className="tag neutral">{id === 'desktop-local' ? '本机实例备份' : '服务器本机备份'}</span></div>
    {error && <div className="notice error" role="alert"><CircleAlert size={17} /><span>{error}</span></div>}
    <div className="backup-actions">
      <button type="button" className="secondary" disabled={blocked} onClick={() => setConfirmation({ kind: 'backup' })}><Archive size={16} />创建备份</button>
      {status.maintenance_pending && <button type="button" className="secondary" disabled={disabled} onClick={() => onOperation('recover')}><RotateCcw size={16} />完成中断恢复</button>}
    </div>
    <BackupTransferPanel id={id} supported={transferSupported} disabled={blocked} exporting={exporting} onClose={() => setExporting(null)} onBusy={setTransferBusy} onImported={() => setRefresh(value => value + 1)} />
    <div className="backup-list">
      {!backups.length ? <p className="muted">暂无备份</p> : (expanded ? backups : backups.slice(0, 5)).map(backup => <div className="backup-row" key={backup.id}>
        <div><strong>{time(backup.created_at)}</strong><span className="muted">配置版本 {backup.revision} · {backup.files} 个文件{backup.imported ? ' · 已导入' : ''}</span></div>
        <span className="backup-size">{size(backup.bytes)}</span>
        <button type="button" className="icon-button" disabled={blocked || !transferSupported} aria-label={`导出 ${time(backup.created_at)} 的备份`} title="导出加密备份" onClick={() => setExporting(backup)}><Download size={17} /></button>
        <button type="button" className="icon-button" disabled={blocked} aria-label={`恢复 ${time(backup.created_at)} 的备份`} title="恢复此备份" onClick={() => setConfirmation({ kind: 'restore', backup })}><RotateCcw size={17} /></button>
      </div>)}
    </div>
    {backups.length > 5 && <button type="button" className="text-button" onClick={() => setExpanded(value => !value)}>{expanded ? <ChevronUp size={15} /> : <ChevronDown size={15} />}{expanded ? '收起' : `全部 ${backups.length} 份`}</button>}
    <dialog className="backup-dialog modal" ref={dialog} onCancel={() => setConfirmation(null)} aria-labelledby="backup-confirm-title">
      <button type="button" className="icon-button modal-close" aria-label="取消维护操作" onClick={() => setConfirmation(null)}><X size={18} /></button>
      <h2 id="backup-confirm-title">{confirmation?.kind === 'restore' ? '恢复此备份？' : '创建完整备份？'}</h2>
      <p>{confirmation?.kind === 'restore'
        ? `将恢复 ${time(confirmation.backup!.created_at)} 的数据库、配置、QQ 会话和文件。当前数据会另行保留；恢复后机器人保持停止，需要核对后重新启用。`
        : `将短暂停止此实例的机器人和 QQ 组件，保存数据库、配置、QQ 会话及文件。结束后恢复原运行状态。备份含有密钥和聊天内容，保存在${id === 'desktop-local' ? '这台电脑的实例数据卷' : '你的服务器'}中。`}</p>
      <p>{confirmation?.kind === 'restore' ? (confirmation.backup?.source_instance_id && confirmation.backup.source_instance_id !== status.instance.id
        ? '这是其他实例的备份。恢复会替换当前数据；请先停止原服务器的机器人和 QQ，避免两端同时登录。恢复后本实例的机器人和 QQ 均保持停止。'
        : '仅恢复当前运行版本的备份。') : '缓存不备份。现有备份不会被自动删除；空间不足时停止创建。'}</p>
      <div className="form-actions"><button type="button" className="secondary" onClick={() => setConfirmation(null)}>取消</button>
        <button type="button" className="primary" disabled={blocked || !confirmation} onClick={() => {
          if (!confirmation) return;
          onOperation(confirmation.kind, confirmation.backup?.id); setConfirmation(null);
        }}>{confirmation?.kind === 'restore' ? <RotateCcw size={16} /> : <Archive size={16} />}{confirmation?.kind === 'restore' ? '确认恢复' : '开始备份'}</button></div>
    </dialog>
  </section>;
}
