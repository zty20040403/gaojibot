import { useRef, useState } from 'react';
import { ArrowDownToLine, ArrowRight, CheckCheck, CircleAlert, Cpu, HardDrive, KeyRound, Plus, Search, Server as ServerIcon, ShieldCheck, Star, X } from 'lucide-react';
import type { Inspection, Server } from './api';
import { desktop, unwrap } from './api';
import type { ServerSort } from './preferences';

export interface InspectionRecord { result?: Inspection; checkedAt: number; failed?: boolean }
export type InspectionRecords = Record<string, InspectionRecord>;

function time(value: number) { return new Date(value).toLocaleString('zh-CN', { hour12: false }); }

// Reports contain an explicit allowlist, never stored credentials, hostnames, or raw error messages.
export function diagnosticReport(version: string, servers: Server[], records: InspectionRecords) {
  return { schema: 1, app_version: version, exported_at: new Date().toISOString(),
    servers: servers.map((server, index) => {
      const record = records[server.id], result = record?.result;
      return { server: `server-${index + 1}`, checked_at: record ? new Date(record.checkedAt).toISOString() : null,
        status: record?.failed ? 'inspection_failed' : result ? result.plan.eligible ? 'eligible' : 'blocked' : 'not_checked',
        ...(result ? { os: result.probe.os, arch: result.probe.arch, free_bytes: result.probe.freeBytes,
          privilege: result.probe.privilege, blockers: result.plan.blockers.length, warnings: result.plan.warnings.length } : {}) };
    }) };
}

export function Workspace({ servers, records, favorites, sort, version, runtimeAvailable, secureStorage, onSort, onFavorite, onInspect, onAdd, onLocal }: {
  servers: Server[]; records: InspectionRecords; favorites: string[]; sort: ServerSort; version: string;
  runtimeAvailable: boolean; secureStorage: boolean; onSort: (sort: ServerSort) => void;
  onFavorite: (id: string) => void; onInspect: (server: Server) => void; onAdd: () => void; onLocal: () => void;
}) {
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<'all' | 'favorites' | 'attention'>('all');
  const [exportState, setExportState] = useState('');
  const [exporting, setExporting] = useState(false);
  const exportingRef = useRef(false);
  const attention = (server: Server) => records[server.id]?.failed || records[server.id]?.result?.plan.eligible === false;
  const checked = servers.filter(server => records[server.id]);
  const items = servers.filter(server => (!query || `${server.name} ${server.host} ${server.username}`.toLowerCase().includes(query.trim().toLowerCase()))
    && (filter === 'all' || filter === 'favorites' && favorites.includes(server.id) || filter === 'attention' && attention(server)))
    .sort((a, b) => Number(favorites.includes(b.id)) - Number(favorites.includes(a.id)) ||
      (sort === 'name' ? a.name.localeCompare(b.name, 'zh-CN') : b.updatedAt - a.updatedAt));
  async function exportReport() {
    if (exportingRef.current) return;
    exportingRef.current = true; setExporting(true); setExportState('');
    try { const result = await unwrap(desktop?.exportDiagnostics(diagnosticReport(version, servers, records)));
      if (result.saved) setExportState('诊断报告已保存，未包含地址、密码或密钥。');
    } catch (cause) { setExportState(cause instanceof Error ? cause.message : '诊断报告未能保存'); }
    finally { exportingRef.current = false; setExporting(false); }
  }
  return <section className="server-home workspace">
    <div className="workspace-heading"><div><span className="eyebrow">WORKSPACE</span><h2>我的服务器<span className="heading-dot" /></h2></div>
      <button className="primary" onClick={onAdd}><Plus size={16} />添加服务器</button></div>
    <div className="workspace-metrics">
      <div><span><ServerIcon size={16} />已保存连接</span><strong>{servers.length.toString().padStart(2, '0')}</strong></div>
      <div><span><CheckCheck size={16} />本次已检测</span><strong>{checked.length.toString().padStart(2, '0')}<small> / {servers.length}</small></strong></div>
      <div className={servers.some(attention) ? 'metric-warning' : ''}><span><CircleAlert size={16} />检测需处理</span><strong>{servers.filter(attention).length.toString().padStart(2, '0')}</strong></div>
    </div>
    <button className="local-entry" onClick={onLocal}><span className="local-mark"><Cpu size={23} /></span><span><strong>这台电脑</strong><small>本地部署</small></span><span className="local-platform">macOS / Windows</span><ArrowRight size={18} /></button>
    <div className="workspace-toolbar">
      <div className="workspace-filters" role="tablist" aria-label="服务器筛选">{([{ id: 'all', label: '全部' }, { id: 'favorites', label: '已置顶' }, { id: 'attention', label: '需处理' }] as const).map(item =>
        <button key={item.id} role="tab" aria-selected={filter === item.id} onClick={() => setFilter(item.id)}>{item.label}</button>)}</div>
      <div className="workspace-tools"><label className="workspace-search"><Search size={16} /><input aria-label="搜索服务器" placeholder="搜索名称或地址" value={query} onChange={event => setQuery(event.target.value)} />
        {query && <button className="icon-button" aria-label="清空搜索" title="清空搜索" onClick={() => setQuery('')}><X size={14} /></button>}</label>
        <select className="sort-select" aria-label="服务器排序" value={sort} onChange={event => onSort(event.target.value as ServerSort)}><option value="recent">最近连接</option><option value="name">按名称</option></select></div>
    </div>
    {items.length ? <div className="connection-list">{items.map(server => {
      const record = records[server.id];
      const label = record?.failed ? '检测失败' : record?.result ? record.result.plan.eligible ? '环境可部署' : '需处理' : '未检测';
      return <div className="connection-row" key={server.id}>
        <button className="connection-main" onClick={() => onInspect(server)}><span className="connection-icon"><ServerIcon size={20} /></span>
          <span className="connection-name"><strong>{server.name}</strong><small>{server.username}@{server.host}</small></span>
          <span className={`connection-status ${attention(server) ? 'attention' : record?.result ? 'ready' : ''}`}><i />{label}</span>
          <span className="connection-time" title={record ? time(record.checkedAt) : '尚未检测'}>{record ? <><span>最近检测</span><time>{time(record.checkedAt)}</time></> : <><span>SSH : {server.port}</span><span>{server.remembered ? '已记住登录信息' : '需要登录'}</span></>}</span><ArrowRight size={16} /></button>
        <button className={`icon-button pin-button ${favorites.includes(server.id) ? 'pinned' : ''}`} aria-label={`${favorites.includes(server.id) ? '取消置顶' : '置顶'} ${server.name}`} title={favorites.includes(server.id) ? '取消置顶' : '置顶'} aria-pressed={favorites.includes(server.id)} onClick={() => onFavorite(server.id)}><Star size={16} /></button>
      </div>;
    })}</div> : <div className="workspace-empty"><ServerIcon size={26} /><h3>{servers.length ? '没有符合条件的服务器' : '连接第一台服务器'}</h3>
      <button className="secondary" onClick={servers.length ? () => { setQuery(''); setFilter('all'); } : onAdd}>{servers.length ? <X size={15} /> : <Plus size={15} />}{servers.length ? '清除筛选' : '连接第一台服务器'}</button></div>}
    <div className="workspace-bottom"><div className="client-readiness"><span><ShieldCheck size={15} />{secureStorage ? '系统加密已就绪' : '密码仅本次会话保存'}</span><span><HardDrive size={15} />{runtimeAvailable ? '运行包已就绪' : '未载入运行包'}</span></div>
      <button className="secondary" disabled={!checked.length || exporting} onClick={() => void exportReport()}><ArrowDownToLine size={15} />{exporting ? '正在保存' : '导出诊断报告'}</button></div>
    {exportState && <div className="export-status" role="status">{exportState}</div>}
  </section>;
}
