import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { ArrowLeft, ArrowRight, Bot, Check, CircleAlert, Globe, LoaderCircle, Pencil, Plus, Search, Trash2, X } from 'lucide-react';
import { desktop, unwrap } from './api';
import type { Application, ApplicationDefinition, ApplicationStatus, Inspection, Server } from './api';
import { WebsitePanel } from './WebsitePanel';
import './applications.css';

export function ApplicationsPanel({ server, inspection, renderBot }: {
  server: Server; inspection: Inspection; renderBot: (instance: Server) => ReactNode;
}) {
  const [items, setItems] = useState<Application[]>([]);
  const [catalog, setCatalog] = useState<ApplicationDefinition[]>([]);
  const [selected, setSelected] = useState<Application | null>(null);
  const [adding, setAdding] = useState(false);
  const [appId, setAppId] = useState<Application['appId']>('gaoji');
  const [name, setName] = useState('我的高级');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [statuses, setStatuses] = useState<Record<string, ApplicationStatus>>({});
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState('all');
  const [editing, setEditing] = useState<Application | null>(null);
  const [draftName, setDraftName] = useState('');
  const [removing, setRemoving] = useState<Application | null>(null);
  const [now, setNow] = useState(Date.now());
  const creating = useRef(false);
  const generation = useRef(0);
  useEffect(() => {
    let active = true;
    void unwrap(desktop?.applicationCatalog()).then(values => { if (active) setCatalog(values); })
      .catch(cause => { if (active) setError(cause.message); });
    return () => { active = false; };
  }, [server.id]);
  useEffect(() => {
    if (selected) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const clock = setInterval(() => setNow(Date.now()), 5000);
    async function poll() {
      const started = generation.current;
      try {
        if (document.hidden || creating.current) return;
        const values = await unwrap(desktop?.applications(server.id));
        if (!active || started !== generation.current) return;
        setItems(values); setLoading(false);
        // One read at a time; an offline host must not create overlapping polling loops.
        for (const item of values) {
          if (!active || document.hidden || started !== generation.current) break;
          let value: ApplicationStatus;
          try { value = await unwrap(desktop?.applicationStatus(item.id)); }
          catch { value = { state: 'unknown', label: '状态读取失败', message: '连接暂时无法确认', checkedAt: Date.now() }; }
          if (active && started === generation.current) setStatuses(current => ({ ...current, [item.id]: value }));
        }
      } catch (cause) { if (active) setError(cause instanceof Error ? cause.message : '实例列表读取失败'); }
      finally { if (active) { setLoading(false); timer = setTimeout(() => void poll(), 8000); } }
    }
    void poll();
    return () => { active = false; clearTimeout(timer); clearInterval(clock); };
  }, [server.id, selected]);
  function statusFor(item: Application): ApplicationStatus | undefined {
    const value = statuses[item.id];
    return value && now - value.checkedAt > 60000 ? { ...value, state: 'unknown', label: '状态已过期', message: '最近一次检查超过一分钟，等待重新确认' } : value;
  }
  const visible = items.filter(item => {
    const state = statusFor(item)?.state;
    return item.name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()) && (filter === 'all'
      || filter === 'working' && state === 'working' || filter === 'attention' && (!state || ['attention', 'unknown', 'setup'].includes(state)));
  });
  const definition = catalog.find(item => item.id === appId);
  const supported = (item: ApplicationDefinition) => item.systems.some(system => {
    const [os, version, arch] = system.split(':');
    return inspection.probe.os === os && (version === '*' || version === inspection.probe.version) && inspection.probe.arch === arch;
  });
  async function create() {
    if (creating.current) return;
    creating.current = true; generation.current++; setBusy(true); setError('');
    try {
      const value = await unwrap(desktop?.createApplication({ id: server.id, appId, name }));
      setItems(current => [...current, value]); setSelected(value); setAdding(false);
    } catch (cause) { setError(cause instanceof Error ? cause.message : '创建未完成'); }
    finally { creating.current = false; setBusy(false); }
  }
  function back() {
    generation.current++; setSelected(null);
  }
  async function changeRecord() {
    if (creating.current) return;
    creating.current = true; generation.current++; setBusy(true); setError('');
    try {
      if (editing) {
        const value = await unwrap(desktop?.renameApplication({ id: editing.id, name: draftName }));
        setItems(current => current.map(item => item.id === value.id ? value : item)); setEditing(null);
      } else if (removing) {
        setItems(await unwrap(desktop?.removeApplicationDraft({ id: removing.id, confirm: true }))); setRemoving(null);
      }
    } catch (cause) { setError(cause instanceof Error ? cause.message : '修改没有完成'); }
    finally { generation.current++; creating.current = false; setBusy(false); }
  }
  return <div className="applications">
    <div className="section-heading application-heading"><div>{selected ? <button className="text-button" onClick={back}><ArrowLeft size={16} /> 已部署软件</button> : <h3>已部署软件</h3>}
      {selected && <h3>{selected.name}</h3>}</div>{!selected && <button className="primary" onClick={() => setAdding(true)}><Plus size={16} />添加软件</button>}</div>
    {error && <div className="notice error" role="alert"><CircleAlert size={17} /><span>{error}</span></div>}
    {selected ? selected.appId === 'gaoji' ? renderBot({ ...server, ...selected }) : <WebsitePanel key={selected.id} instance={selected} inspection={inspection} /> : <>
      <div className="application-toolbar"><label className="application-search"><Search size={17} /><input aria-label="搜索软件" placeholder="搜索软件" value={query} onChange={event => setQuery(event.target.value)} /></label>
        <div className="application-filters" role="group" aria-label="实例状态筛选">{[['all', '全部'], ['working', '进行中'], ['attention', '需关注']].map(([id, label]) => <button key={id} aria-pressed={filter === id} onClick={() => setFilter(id)}>{label}</button>)}</div></div>
      {loading ? <div className="loading"><LoaderCircle size={18} className="spin" />读取实例</div> : visible.length ? <div className="application-list" role="list">{visible.map(item => {
        const value = statusFor(item);
        return <div className="application-row" key={item.id} role="listitem"><button className="application-main" onClick={() => setSelected(item)}>
          {item.appId === 'gaoji' ? <Bot size={22} /> : <Globe size={22} />}<span className="application-name"><strong>{item.name}</strong><small>{item.appId === 'gaoji' ? '高级机器人' : '静态网站'}{item.legacy ? ' · 已有实例' : ''}</small></span>
          <span className={`application-status ${value?.state || 'unknown'}`} title={value ? `${value.message}\n检查时间：${new Date(value.checkedAt).toLocaleString()}` : '等待检查'}><i />{value?.label || '等待检查'}</span><ArrowRight size={17} /></button>
          <div className="application-row-actions"><button className="icon-button" aria-label={`重命名 ${item.name}`} title="重命名" disabled={busy} onClick={() => { setDraftName(item.name); setEditing(item); setError(''); }}><Pencil size={16} /></button>
            {!item.deployment && !item.legacy && <button className="icon-button" aria-label={`移除未部署记录 ${item.name}`} title="移除未部署记录" disabled={busy} onClick={() => { setRemoving(item); setError(''); }}><Trash2 size={16} /></button>}</div></div>;
      })}</div> : <p className="muted application-empty">{items.length ? '没有匹配的软件。' : '这台服务器还没有添加软件。'}</p>}
    </>}
    {(editing || removing) && <div className="modal-overlay"><div className="modal" role="dialog" aria-modal="true" aria-labelledby="application-edit-title"><button className="icon-button modal-close" aria-label="关闭" title="关闭" disabled={busy} onClick={() => { setEditing(null); setRemoving(null); }}><X size={18} /></button>
      <h2 id="application-edit-title">{editing ? '重命名软件' : '移除未部署记录'}</h2>
      {editing ? <label className="field"><span>软件名称</span><input value={draftName} maxLength={80} disabled={busy} onChange={event => setDraftName(event.target.value)} /></label> : <p>移除“{removing?.name}”在本机的空记录，不会删除服务器上的软件或文件。已有部署任务的记录不能移除。</p>}
      {error && <div className="notice error" role="alert">{error}</div>}
      <div className="form-actions"><button className="secondary" disabled={busy} onClick={() => { setEditing(null); setRemoving(null); }}>取消</button><button className="primary" disabled={busy || Boolean(editing && !draftName.trim())} onClick={() => void changeRecord()}>{busy ? <LoaderCircle size={16} className="spin" /> : editing ? <Check size={16} /> : <Trash2 size={16} />}{editing ? '保存名称' : '移除记录'}</button></div>
    </div></div>}
    {adding && <div className="modal-overlay"><div className="modal application-modal" role="dialog" aria-modal="true" aria-labelledby="application-title">
      <button className="icon-button modal-close" aria-label="关闭" title="关闭" disabled={busy} onClick={() => setAdding(false)}><X size={18} /></button><h2 id="application-title">添加软件</h2>
      <div className="application-choices" role="radiogroup" aria-label="软件类型">{catalog.map(item => <label key={item.id} className={`application-choice ${appId === item.id ? 'selected' : ''}`}>
        <input type="radio" name="application" checked={appId === item.id} disabled={busy || !supported(item)} onChange={() => { setAppId(item.id); setName(item.id === 'gaoji' ? '我的高级' : '我的网站'); }} />
        {item.id === 'gaoji' ? <Bot size={24} /> : <Globe size={24} />}<span>{item.name}<small>{supported(item) ? '支持此系统' : '暂不支持此系统'}</small></span>{appId === item.id && <Check size={16} />}
      </label>)}</div>
      <label className="field"><span>实例名称</span><input value={name} maxLength={80} disabled={busy} onChange={event => setName(event.target.value)} /></label>
      {definition && <dl className="application-impact"><div><dt>数据目录</dt><dd>{definition.dataRoot}/独立编号</dd></div><div><dt>访问范围</dt><dd>服务器本机 · App 加密隧道</dd></div><div><dt>最低可用空间</dt><dd>{definition.minimumFreeBytes / 1024 ** 3} GiB</dd></div></dl>}
      <div className="form-actions"><button className="secondary" disabled={busy} onClick={() => setAdding(false)}>取消</button><button className="primary" disabled={busy || !name.trim() || !definition || !supported(definition)} onClick={() => void create()}>{busy ? <LoaderCircle size={16} className="spin" /> : <ArrowRight size={16} />}配置实例</button></div>
    </div></div>}
  </div>;
}
