import { PlatformOptions, legacyPlatforms } from './PlatformOptions';
import { useEffect, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { ArrowLeft, ArrowRight, Check, ChevronDown, CircleAlert, Cpu, Database, Eye, EyeOff,
  FolderKey, HardDrive, KeyRound, LoaderCircle, LockKeyhole, Plus, RefreshCw, Server as ServerIcon,
  LayoutDashboard, ShieldCheck, Trash2, X } from 'lucide-react';
import { desktop, unwrap } from './api';
import type { AuthType, ConnectRequest, Inspection, Installation, LifecycleRecord, LifecycleTask, Server, Target } from './api';
import { SetupPanel } from './SetupPanel';
import { UpdatePanel } from './UpdatePanel';
import { StoragePanel } from './StoragePanel';
import { LifecyclePanel, maintenanceBlocks } from './LifecyclePanel';
import { LocalPanel } from './LocalPanel';
import { ImageOptions } from './ImageOptions';
import { AppUpdate } from './AppUpdate';
import { Appearance } from './Appearance';
import { Workspace } from './Workspace';
import { ApplicationsPanel } from './ApplicationsPanel';
import { DeploymentProgress } from './DeploymentProgress';
import type { InspectionRecords } from './Workspace';
import { usePreferences } from './preferences';
import type { ImageOptions as ImagePolicy } from './api';

const steps = ['连接服务器', '安装环境', '登录 QQ', '配置模型'];
function ErrorNotice({ text }: { text: string }) {
  return <div className="notice error" role="alert"><CircleAlert size={18} /><span>{text}</span></div>;
}
function Field({ label, children }: { label: string; children: ReactNode }) {
  return <label className="field"><span>{label}</span>{children}</label>;
}
function Secret({ value, onChange, id, autoComplete = 'off' }: { value: string; onChange: (value: string) => void; id: string; autoComplete?: string }) {
  const [visible, setVisible] = useState(false);
  return <div className="secret"><input id={id} type={visible ? 'text' : 'password'} value={value} autoComplete={autoComplete}
    onChange={event => onChange(event.target.value)} maxLength={8192} /><button className="icon-button" type="button"
      title={visible ? '隐藏密码' : '显示密码'} aria-label={visible ? '隐藏密码' : '显示密码'} onClick={() => setVisible(!visible)}>
      {visible ? <EyeOff size={18} /> : <Eye size={18} />}</button></div>;
}
function ConnectionForm({ existing, secureStorage, onConnected, onCancel }: {
  existing?: Server; secureStorage: boolean; onConnected: (server: Server, inspection: Inspection) => void; onCancel: () => void;
}) {
  const [name, setName] = useState(existing?.name || '我的高级');
  const [host, setHost] = useState(existing?.host || '');
  const [username, setUsername] = useState(existing?.username || 'root');
  const [port, setPort] = useState(String(existing?.port || 22));
  const [type, setType] = useState<AuthType>(existing?.authType || 'password');
  const [password, setPassword] = useState('');
  const [passphrase, setPassphrase] = useState('');
  const [key, setKey] = useState<{ selection: string; name: string } | null>(null);
  const [remember, setRemember] = useState(false);
  const [identity, setIdentity] = useState<(Target & { fingerprint: string }) | null>(null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const busyRef = useRef(false);
  async function run(action: () => Promise<void>, title: string) {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(title); setError('');
    try { await action(); } catch (cause) { setError(cause instanceof Error ? cause.message : '连接没有完成'); }
    finally { busyRef.current = false; setBusy(''); }
  }
  async function connect(fingerprint: string, target: Target) {
    const request: ConnectRequest = { ...target, id: existing?.id, name, fingerprint, remember,
      authentication: { type, password, keySelection: key?.selection, passphrase } };
    const result = await unwrap(desktop?.connect(request));
    setPassword(''); setPassphrase(''); onConnected(result.server, result);
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    void run(async () => {
      const target = { host: host.trim(), username: username.trim(), port: Number(port) };
      const discovered = await unwrap(desktop?.discover(target));
      if (existing && existing.host === target.host && existing.port === target.port) {
        if (existing.fingerprint !== discovered.fingerprint) throw new Error('服务器身份与上次不同，已停止连接。请先核实服务器是否重装或更换。');
        await connect(existing.fingerprint, target);
      } else setIdentity(discovered);
    }, '正在连接');
  }
  return <section className="setup-section">
    <div className="section-heading"><div><span className="eyebrow">01 / 服务器</span><h2>{existing ? '重新登录服务器' : '连接你的服务器'}</h2></div><span className="tag"><LockKeyhole size={13} /> 加密连接</span></div>
    <form onSubmit={submit}>
      <fieldset disabled={Boolean(busy || identity)}>
        <Field label="名称"><input value={name} onChange={event => setName(event.target.value)} maxLength={80} required autoComplete="off" /></Field>
        <Field label="服务器 IP 或域名"><input value={host} onChange={event => setHost(event.target.value)} placeholder="例如 192.168.1.20" required autoCapitalize="none" spellCheck={false} /></Field>
        <div className="field-grid"><Field label="登录账户"><input value={username} onChange={event => setUsername(event.target.value)} autoComplete="username" required /></Field>
          <Field label="登录方式"><div className="select"><select value={type} onChange={event => setType(event.target.value as AuthType)}>
            <option value="password">服务器密码</option><option value="private-key">密钥文件</option><option value="agent">系统密钥代理</option><option value="tailscale">Tailscale SSH</option>
          </select><ChevronDown size={16} /></div></Field></div>
        {type === 'password' && <Field label="服务器密码"><Secret id="server-password" value={password} onChange={setPassword} autoComplete="current-password" /></Field>}
        {type === 'private-key' && <><Field label="私钥文件"><button type="button" className="key-picker" onClick={() => void run(async () => {
          const chosen = await unwrap(desktop?.selectKey()); if (chosen) setKey(chosen);
        }, '选择文件')}><FolderKey size={18} /><span>{key?.name || '选择文件'}</span></button></Field>
          <Field label="密钥口令（若有）"><Secret id="key-passphrase" value={passphrase} onChange={setPassphrase} /></Field></>}
        <details className="advanced"><summary>更多连接设置</summary><Field label="SSH 端口"><input type="number" min="1" max="65535" value={port} onChange={event => setPort(event.target.value)} required /></Field></details>
        <label className="checkbox"><input type="checkbox" disabled={!secureStorage} checked={remember} onChange={event => setRemember(event.target.checked)} />记住此服务器的登录信息</label>
      </fieldset>
      {error && <ErrorNotice text={error} />}
      <div className="form-actions"><button type="button" className="secondary" onClick={onCancel} disabled={Boolean(busy)}><ArrowLeft size={16} /> 返回</button>
        <button className="primary" type="submit" disabled={Boolean(busy || identity || !desktop || (type === 'password' && !password) || (type === 'private-key' && !key))}>
          {busy ? <LoaderCircle size={17} className="spin" /> : <ArrowRight size={17} />}{busy || '连接并检测'}</button></div>
    </form>
    {identity && <div className="modal-overlay"><div className="modal" role="dialog" aria-modal="true" aria-labelledby="trust-title">
      <ShieldCheck size={28} className="green" /><h2 id="trust-title">首次连接此服务器</h2><div className="identity-target">{identity.username}@{identity.host}:{identity.port}</div>
      <Field label="服务器身份指纹"><code className="fingerprint">{identity.fingerprint}</code></Field>
      {error && <ErrorNotice text={error} />}
      <div className="form-actions"><button className="secondary" disabled={Boolean(busy)} onClick={() => setIdentity(null)}>取消</button><button className="primary" disabled={Boolean(busy)}
        onClick={() => void run(() => connect(identity.fingerprint, identity), '正在检测')}>
          {busy ? <LoaderCircle size={16} className="spin" /> : <Check size={16} />}{busy || '信任并连接'}</button></div>
    </div></div>}
  </section>;
}
function InstalledPanel({ id, lifecycle, passwordRequired, onStep }: { id: string; lifecycle?: LifecycleRecord; passwordRequired: boolean; onStep: (step: number) => void }) {
  const [view, setView] = useState<'setup' | 'versions' | 'storage' | 'lifecycle'>(lifecycle && maintenanceBlocks(lifecycle) ? 'lifecycle' : 'setup');
  const [maintenance, setMaintenance] = useState<LifecycleRecord | LifecycleTask | null>(lifecycle || null);
  const [lifecycleOpened, setLifecycleOpened] = useState(Boolean(lifecycle));
  const [dirty, setDirty] = useState(false);
  const [storageDirty, setStorageDirty] = useState(false);
  const [storageOpened, setStorageOpened] = useState(false);
  const blocked = maintenanceBlocks(maintenance);
  return <><div className="setup-tabs" role="tablist" aria-label="实例管理">
    <button role="tab" aria-selected={view === 'setup'} onClick={() => setView('setup')}>QQ 与模型</button>
    <button role="tab" aria-selected={view === 'versions'} onClick={() => setView('versions')}>更新与回退</button>
    <button role="tab" aria-selected={view === 'storage'} onClick={() => { setStorageOpened(true); setView('storage'); }}>存储管理</button>
    <button role="tab" aria-selected={view === 'lifecycle'} onClick={() => { setLifecycleOpened(true); setView('lifecycle'); }}>服务维护</button>
  </div>{blocked && view !== 'lifecycle' && <div className="notice warning" role="status"><CircleAlert size={17} /><span>此实例正在维护或已卸载服务。请到“服务维护”查看进度或重新启用。</span></div>}
    <div hidden={view !== 'setup' || blocked}><SetupPanel id={id} onStep={onStep} onDirty={setDirty} suspended={blocked} /></div>
    {view === 'versions' && !blocked && <UpdatePanel id={id} dirty={dirty || storageDirty} passwordRequired={passwordRequired} />}
    {storageOpened && <div hidden={view !== 'storage' || blocked}><StoragePanel id={id} active={view === 'storage' && !blocked} onDirty={setStorageDirty} /></div>}
    {lifecycleOpened && <div hidden={view !== 'lifecycle'}><LifecyclePanel id={id} initial={lifecycle} passwordRequired={passwordRequired} dirty={dirty || storageDirty} onTask={setMaintenance} /></div>}</>;
}
function InstallationPanel({ result, server, runtimeAvailable, onStep }: { result: Inspection; server: Server; runtimeAvailable: boolean; onStep: (step: number) => void }) {
  const [progress, setProgress] = useState<Installation | null>(null);
  const [platforms, setPlatforms] = useState(server.deployment?.platforms || legacyPlatforms);
  const [imageOptions, setImageOptions] = useState<ImagePolicy>(server.deployment?.imageOptions || { mode: 'auto', source: 'official' });
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [pollError, setPollError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const submittingRef = useRef(false);
  const installEpoch = useRef(0);
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      const started = installEpoch.current;
      try {
        const value = await unwrap(desktop?.installStatus(server.id));
        if (active && !submittingRef.current && started === installEpoch.current) { setProgress(value); setPollError(''); }
      } catch (cause) { if (active && !submittingRef.current && started === installEpoch.current) setPollError(cause instanceof Error ? cause.message : '暂时无法获取安装状态'); }
      finally { if (active) timer = setTimeout(() => void poll(), 3000); }
    }
    void poll();
    return () => { active = false; clearTimeout(timer); };
  }, [server.id]);
  const running = submitting || progress && ['queued', 'waiting', 'running'].includes(progress.status);
  const finished = progress?.status === 'awaiting_setup';
  const retry = progress?.status === 'failed' || progress?.status === 'unconfirmed';
  async function install(event: FormEvent) {
    event.preventDefault();
    if (submittingRef.current) return;
    submittingRef.current = true; installEpoch.current++; setSubmitting(true); setError('');
    try { setProgress(await unwrap(desktop?.install({ id: server.id, password, imageOptions, platforms }))); setPassword(''); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '安装未完成'); }
    finally { installEpoch.current++; submittingRef.current = false; setSubmitting(false); }
  }
  if (finished) return <InstalledPanel key={server.id} id={server.id} lifecycle={server.lifecycle} passwordRequired={result.probe.privilege === 'password-required'} onStep={onStep} />;
  return <><h3>安装清单</h3><div className="install-steps">{result.plan.steps.map((step, index) => {
    const done = progress?.completed?.includes(step.id);
    const current = progress?.stage === step.id && running;
    return <div className="install-step" key={step.id}><span className={`step-number ${done ? 'green' : ''}`}>
      {done ? <Check size={13} /> : current ? <LoaderCircle size={13} className="spin" /> : index + 1}</span><span>{step.title}</span><span className="muted">{done ? '已完成' : current ? '进行中' : '待执行'}</span></div>;
  })}</div>
    {progress && <div className={`notice ${retry ? 'warning' : 'install-notice'}`} role="status">{running ? <LoaderCircle size={17} className="spin" /> : <CircleAlert size={17} />}<span>{progress.message}</span></div>}
    {progress && <DeploymentProgress task={progress} disconnected={Boolean(pollError)} />}
    {error && <ErrorNotice text={error} />}
    {pollError && <ErrorNotice text={pollError} />}
    {!runtimeAvailable && <div className="notice warning"><CircleAlert size={18} /><span>此客户端缺少服务器运行包，暂不能安装。</span></div>}
    <form onSubmit={event => void install(event)}>
      {!server.deployment && <PlatformOptions value={platforms} onChange={setPlatforms} disabled={Boolean(running)} />}
      <ImageOptions value={imageOptions} onChange={setImageOptions} disabled={Boolean(running)} />
      {result.probe.privilege === 'password-required' && !running && <Field label="服务器 sudo 密码"><Secret id="sudo-password" value={password} onChange={setPassword} /></Field>}
      <div className="form-actions"><span className="muted">独立安装 · 数据保留在服务器</span><button className="primary" type="submit"
        disabled={!runtimeAvailable || !result.plan.eligible || Boolean(running) || (result.probe.privilege === 'password-required' && !password)}>
        {running ? <LoaderCircle size={16} className="spin" /> : <ArrowRight size={16} />}{submitting ? '正在上传并提交' : running ? '正在安装' : retry ? '继续原安装任务' : '安装到此服务器'}</button></div>
    </form>
  </>;
}
function InspectionView({ result, server, busy, onRefresh, runtimeAvailable, onStep }: { result: Inspection; server: Server; busy: boolean; onRefresh: () => void; runtimeAvailable: boolean; step: number; onStep: (step: number) => void }) {
  const { probe, plan } = result;
  return <section className="inspection">
    <div className="section-heading"><div><span className="eyebrow">服务器</span><h2>{server.name}</h2></div><button className="icon-button" title="重新检测" aria-label="重新检测" disabled={busy} onClick={onRefresh}><RefreshCw size={19} className={busy ? 'spin' : ''} /></button></div>
    <div className="host-line"><span className="status-dot" /> 已连接 <span>{server.username}@{server.host}</span></div>
    <dl className="stats"><div><dt><Cpu size={16} /> 系统</dt><dd>{probe.label}</dd></div><div><dt><HardDrive size={16} /> 可用空间</dt><dd>{(probe.freeBytes / 1024 ** 3).toFixed(1)} <small>GiB</small></dd></div><div><dt><KeyRound size={16} /> 安装权限</dt><dd>{({ root: '管理员', passwordless: '已就绪', 'password-required': '需要 sudo 密码', unavailable: '缺少权限' })[probe.privilege]}</dd></div></dl>
    {plan.blockers.map(text => <ErrorNotice key={text} text={text} />)}
    <ApplicationsPanel key={server.id} server={server} inspection={result} renderBot={instance => <InstallationPanel key={instance.id} server={instance} result={result} runtimeAvailable={runtimeAvailable} onStep={onStep} />} />
    <details className="advanced"><summary>检测详情</summary><ul>{plan.warnings.map(text => <li key={text}>{text}</li>)}</ul></details>
    <div className="boundary-list">{plan.boundaries.map(text => <span key={text}><ShieldCheck size={14} />{text}</span>)}</div>
  </section>;
}
export function App() {
  const { preferences, setPreferences } = usePreferences();
  const [records, setRecords] = useState<InspectionRecords>({});
  const [localMode, setLocalMode] = useState(false);
  const [servers, setServers] = useState<Server[]>([]);
  const [selected, setSelected] = useState<Server | undefined>();
  const [inspection, setInspection] = useState<Inspection | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [reconnect, setReconnect] = useState<Server | undefined>();
  const [info, setInfo] = useState({ version: '0.1.0', secureStorage: false, runtimeAvailable: false });
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [confirmForget, setConfirmForget] = useState<Server | null>(null);
  const [wizardStep, setWizardStep] = useState(0);
  const serial = useRef(0);
  useEffect(() => {
    let active = true;
    if (!desktop) return;
    void unwrap(desktop.info()).then(value => { if (active) setInfo(value); }).catch(cause => { if (active) setError(cause.message); });
    void unwrap(desktop.servers()).then(value => { if (active) setServers(value); }).catch(cause => { if (active) setError(cause.message); });
    return () => { active = false; };
  }, []);
  async function inspect(server: Server) {
    setLocalMode(false);
    const request = ++serial.current;
    setWizardStep(1);
    setSelected(server); setShowForm(false); setInspection(null); setError(''); setBusy(true);
    if (!server.authenticated) { setReconnect(server); setShowForm(true); setBusy(false); return; }
    try { const result = await unwrap(desktop?.inspect(server.id));
      setRecords(current => ({ ...current, [server.id]: { result, checkedAt: Date.now() } }));
      if (request === serial.current) setInspection(result); }
    catch (cause) { setRecords(current => ({ ...current, [server.id]: { failed: true, checkedAt: Date.now() } }));
      if (request === serial.current) setError(cause instanceof Error ? cause.message : '检测失败'); }
    finally { if (request === serial.current) setBusy(false); }
  }
  function add() { setLocalMode(false); serial.current++; setWizardStep(0); setSelected(undefined); setInspection(null); setReconnect(undefined); setShowForm(true); setError(''); setBusy(false); }
  function openLocal() { serial.current++; setLocalMode(true); setSelected(undefined); setShowForm(false); setBusy(false); setError(''); }
  function openHome() { serial.current++; setLocalMode(false); setShowForm(false); setSelected(undefined); setInspection(null); setBusy(false); setError(''); }
  const isHome = !localMode && !showForm && !selected;
  return <div className="app-layout"><aside className="sidebar">
    <a className="brand" href="#" onClick={event => { event.preventDefault(); openHome(); }}><img src="./brand.png" alt="" /><span><strong>高级</strong><small>GAOJI WORKSPACE</small></span></a>
    <nav className="primary-nav" aria-label="工作区"><button className={`server-link ${isHome ? 'selected' : ''}`} onClick={openHome}><LayoutDashboard size={18} /><span>工作台</span></button><button className={`server-link ${localMode ? 'selected' : ''}`} onClick={openLocal}><Cpu size={18} /><span>这台电脑</span></button></nav>
    <div className="sidebar-heading"><span>我的服务器</span><button className="icon-button" title="添加服务器" aria-label="添加服务器" onClick={add}><Plus size={18} /></button></div>
    <nav aria-label="服务器">{servers.map(server => <button className={`server-link ${selected?.id === server.id ? 'selected' : ''}`} key={server.id} onClick={() => void inspect(server)}><ServerIcon size={18} /><span>{server.name}<small>{server.host}</small></span></button>)}</nav>
    <div className="sidebar-bottom"><ShieldCheck size={16} /><span>独立部署 <small>v{info.version}</small></span></div>
  </aside><div className="main-shell"><header className="topbar"><div><h1>{localMode ? '本机部署' : showForm ? '添加服务器' : selected ? '服务器详情' : '工作台'}</h1><span>{selected?.host || '高级 / 部署与管理'}</span></div><div className="topbar-actions"><span className="tag neutral"><LockKeyhole size={13} /> {desktop ? '桌面客户端' : '界面预览'}</span><Appearance theme={preferences.theme} onTheme={theme => setPreferences(current => ({ ...current, theme }))} palette={preferences.palette} onPalette={palette => setPreferences(current => ({ ...current, palette }))} /><AppUpdate /></div></header>
    <main>{showForm && <ol className="wizard-steps">{['连接服务器', '选择软件', '部署与配置'].map((name, index) => <li key={name} className={index === 0 ? 'current' : ''}><span>{index + 1}</span>{name}</li>)}</ol>}
      {error && <ErrorNotice text={error} />}
      {localMode ? <LocalPanel /> : showForm ? <ConnectionForm key={reconnect?.id || 'new'} existing={reconnect} secureStorage={info.secureStorage} onCancel={() => setShowForm(false)} onConnected={(server, result) => {
        serial.current++; setWizardStep(1); setSelected(server); setInspection(result); setShowForm(false); setError(''); setServers(current => current.filter(item => item.id !== server.id).concat(server));
        setRecords(current => ({ ...current, [server.id]: { result, checkedAt: Date.now() } }));
      }} /> : selected ? <>{busy && <div className="loading" role="status"><LoaderCircle className="spin" size={22} />正在检测服务器</div>}
        {inspection && <InspectionView result={inspection} server={selected} busy={busy} onRefresh={() => void inspect(selected)} runtimeAvailable={info.runtimeAvailable} step={wizardStep} onStep={setWizardStep} />}
        {!busy && <div className="server-actions"><button className="secondary" onClick={() => { setReconnect(selected); setShowForm(true); }}><KeyRound size={16} />重新登录</button><button className="text-button danger" onClick={() => setConfirmForget(selected)}><Trash2 size={16} />移除此连接</button></div>}</> : <Workspace servers={servers} records={records} favorites={preferences.favorites} sort={preferences.sort} version={info.version} runtimeAvailable={info.runtimeAvailable} secureStorage={info.secureStorage}
          onSort={sort => setPreferences(current => ({ ...current, sort }))} onFavorite={id => setPreferences(current => ({ ...current, favorites: current.favorites.includes(id) ? current.favorites.filter(item => item !== id) : [...current.favorites, id] }))}
          onInspect={server => void inspect(server)} onAdd={add} onLocal={openLocal} />}
    </main><footer><Database size={14} /><span>{localMode ? '数据保存在本机 Docker 数据卷' : '服务器数据独立保存'}</span><span className="footer-right">Gaoji · Kenneth</span></footer>
  </div>{confirmForget && <div className="modal-overlay"><div className="modal" role="dialog" aria-modal="true" aria-labelledby="forget-title"><button className="modal-close icon-button" aria-label="关闭" title="关闭" onClick={() => setConfirmForget(null)}><X size={18} /></button><h2 id="forget-title">移除 {confirmForget.name} 的连接？</h2><p>仅移除此电脑保存的连接，服务器上的服务和数据不变。</p><div className="form-actions"><button className="secondary" onClick={() => setConfirmForget(null)}>取消</button><button className="primary" onClick={() => {
    void unwrap(desktop?.forget(confirmForget.id)).then(value => { serial.current++; setServers(value); setSelected(undefined); setInspection(null); setConfirmForget(null); }).catch(cause => { setConfirmForget(null); setError(cause.message); });
  }}>移除连接</button></div></div></div>}</div>;
}
