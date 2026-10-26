import { useEffect, useRef, useState } from 'react';
import { QRCodeSVG } from 'qrcode.react';
import { ArrowRight, Check, CircleAlert, ExternalLink, LoaderCircle, Play, RefreshCw, Save, Square } from 'lucide-react';
import { DesktopError, desktop, instanceCall, unwrap } from './api';
import type { InstanceSettings, InstanceStatus, ModelProfile, OperationKind, QQStatus } from './api';
import { BackupPanel } from './BackupPanel';
import { ComponentPanel, componentDefaults } from './ComponentPanel';
import { ModelCatalog } from './ModelCatalog';
import { ReadinessPanel } from './ReadinessPanel';
import { cleanProfile, discoveredProfiles, keyFor, newModel, removeModel, validateCatalog, withCatalog } from './modelSettings';

const phaseLabels: Record<string, string> = { qrcode_scanned: '已扫码，请在手机上确认', initializing: 'QQ 正在完成登录', reconnecting: 'QQ 正在重连' };
type OperationRequest = { id: string; kind: OperationKind; revision: number; administrator?: { username: string; password: string }; backup_id?: string; confirm?: boolean };

function ErrorText({ text }: { text: string }) {
  return <div className="notice error" role="alert"><CircleAlert size={17} /><span>{text}</span></div>;
}

export function SetupPanel({ id, onStep, onDirty, suspended = false }: { id: string; onStep: (step: number) => void; onDirty?: (value: boolean) => void; suspended?: boolean }) {
  const [status, setStatus] = useState<InstanceStatus | null>(null);
  const [qq, setQQ] = useState<QQStatus | null>(null);
  const [draft, setDraft] = useState<InstanceSettings | null>(null);
  const [revision, setRevision] = useState(0);
  const [selectedModel, setSelectedModel] = useState('primary');
  const [keyChanges, setKeyChanges] = useState<Record<string, string>>({});
  const [keySources, setKeySources] = useState<Record<string, string>>({});
  const [serviceKeys, setServiceKeys] = useState<Record<string, string>>({});
  const [embeddingResult, setEmbeddingResult] = useState('');
  const [clusterResult, setClusterResult] = useState('');
  const [matrixResult, setMatrixResult] = useState('');
  const [imessageResult, setIMessageResult] = useState('');
  const [models, setModels] = useState<string[] | null>(null);
  const [groups, setGroups] = useState('');
  const [adminName, setAdminName] = useState('admin');
  const [adminPassword, setAdminPassword] = useState('');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [pollError, setPollError] = useState('');
  const [modelResult, setModelResult] = useState('');
  const [dirty, setDirty] = useState(false);
  const [view, setView] = useState<'qq' | 'settings' | 'health'>('qq');
  const [discarding, setDiscarding] = useState(false);
  const [handoverConfirmed, setHandoverConfirmed] = useState(false);
  const discardDialog = useRef<HTMLDialogElement>(null);
  const [services, setServices] = useState<{ name: string; state: string; health: string }[]>([]);
  const initialized = useRef(false);
  const restored = useRef('');
  const gate = useRef(false);
  const alive = useRef(true);
  const modelGeneration = useRef(0);
  const componentGeneration = useRef(0);
  const pendingOperation = useRef<OperationRequest | null>(null);

  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, [id]);
  useEffect(() => {
    if (suspended) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const value = await instanceCall<InstanceStatus>(id, 'status');
        if (disposed) return;
        setStatus(value); setPollError('');
        if (value.operation?.id === pendingOperation.current?.id) pendingOperation.current = null;
        const restoreCompleted = value.operation && ['restored', 'recovered', 'updated', 'rolled_back'].includes(value.operation.stage) && value.operation.status === 'completed' && restored.current !== value.operation.id;
        if (!initialized.current || restoreCompleted) {
          modelGeneration.current++;
          componentGeneration.current++;
          const settings = withCatalog(value.instance.settings);
          setDraft(settings); setRevision(value.instance.revision); setGroups(settings.group_ids.join('\n'));
          setSelectedModel(settings.default_model || settings.models[0].id);
          setKeyChanges({}); setKeySources({}); setModels(null); setModelResult('');
          setDirty(!value.instance.settings.models.length);
          initialized.current = true;
          if (restoreCompleted) { restored.current = value.operation!.id; setServiceKeys({}); setEmbeddingResult(''); setClusterResult(''); setMatrixResult(''); setIMessageResult(''); }
          if (value.qq_handover) setView('qq');
          else if (value.instance.settings.default_model) setView('settings');
        }
        try {
          const login = await instanceCall<QQStatus>(id, 'qqStatus');
          if (disposed) return;
          setQQ(login);
        } catch (cause) {
          if (disposed) return;
          setQQ(null);
          setPollError(cause instanceof Error ? cause.message : 'QQ 组件暂不可用');
        }
        const components = await instanceCall<{ services: typeof services }>(id, 'services');
        if (!disposed) setServices(components.services);
      } catch (cause) { if (!disposed) setPollError(cause instanceof Error ? cause.message : '连接暂时中断'); }
      finally { if (!disposed) timer = setTimeout(() => void poll(), 4000); }
    }
    void poll();
    return () => { disposed = true; clearTimeout(timer); };
  }, [id, suspended]);
  useEffect(() => { onDirty?.(dirty || Boolean(busy)); }, [dirty, busy, onDirty]);
  useEffect(() => { setHandoverConfirmed(false); }, [id, status?.qq_handover?.operation_id]);
  useEffect(() => { onStep(qq?.online && !pollError && !suspended ? 3 : 2); }, [qq?.online, pollError, suspended, onStep]);
  useEffect(() => {
    if (discarding && !discardDialog.current?.open) discardDialog.current?.showModal();
    else if (!discarding) discardDialog.current?.close();
  }, [discarding]);
  function change<K extends keyof InstanceSettings>(key: K, value: InstanceSettings[K]) {
    setDraft(current => current ? { ...current, [key]: value } : current); setDirty(true);
  }
  const model = draft?.models.find(item => item.id === selectedModel) || draft?.models[0];
  function selectModel(identifier: string) { modelGeneration.current++; setSelectedModel(identifier); setModels(null); setModelResult(''); }
  function changeModel(changes: Partial<ModelProfile>) {
    if (!model) return;
    modelGeneration.current++;
    setDraft(current => current ? { ...current, models: current.models.map(item => item.id === model.id ? { ...item, ...changes, id: item.id, has_key: item.has_key } : item),
      vision_model: changes.vision === false && current.vision_model === model.id ? '' : current.vision_model } : current);
    setDirty(true); setModelResult('');
    if (['base_url', 'protocol', 'provider'].some(key => Object.hasOwn(changes, key))) {
      setKeyChanges(current => ({ ...current, [model.id]: '' })); setModels(null);
      setKeySources(current => { const next = { ...current }; delete next[model.id]; return next; });
    }
  }
  function importModels(names: string[]) {
    if (!model || !draft) return;
    const candidates = discoveredProfiles(model, names.filter(name => models?.includes(name)), draft.models);
    if (!candidates.length) return;
    const replacement = !model.model.trim() ? { ...model, model: candidates[0].model, label: candidates[0].label } : null;
    const added = replacement ? candidates.slice(1) : candidates;
    if (draft.models.length + added.length > 100) throw new Error('最多配置 100 个模型，请减少本次选择');
    const key = keyFor(model, keyChanges);
    if (added.length && key === null && status?.model_key_references?.schema !== 1) throw new Error('此服务器版本暂不支持复用已保存的密钥，请在 API Key 栏重新输入同一个 Key 后添加');
    if (model.api_key_required && key !== null && !key.trim()) throw new Error('请先填写此接口的 API Key');
    setKeyChanges(current => ({ ...current, ...Object.fromEntries(added.filter(() => key !== null).map(item => [item.id, key!])) }));
    if (key === null) setKeySources(current => ({ ...current, ...Object.fromEntries(added.map(item => [item.id, keySources[model.id] || model.id])) }));
    change('models', [...draft.models.map(item => replacement && item.id === model.id ? replacement : item), ...added.map(item => ({ ...item, has_key: key === null }))]);
  }
  function probeProfile(profile: ModelProfile) {
    return { ...cleanProfile(profile), id: keySources[profile.id] || profile.id };
  }
  async function run(label: string, action: () => Promise<void>) {
    if (gate.current || suspended) return;
    gate.current = true; setBusy(label); setError('');
    try { await action(); }
    catch (cause) { if (alive.current) setError(cause instanceof Error ? cause.message : '操作未完成'); }
    finally { gate.current = false; if (alive.current) setBusy(''); }
  }
  const working = status?.operation && ['queued', 'running'].includes(status.operation.status);
  const disabled = Boolean(busy || working || suspended);
  async function reloadSettings() {
    const next = await instanceCall<InstanceStatus>(id, 'status');
    modelGeneration.current++;
    const settings = withCatalog(next.instance.settings);
    componentGeneration.current++;
    setStatus(next); setDraft(settings); setRevision(next.instance.revision); setGroups(settings.group_ids.join('\n'));
    setSelectedModel(settings.models.some(item => item.id === selectedModel) ? selectedModel : settings.default_model);
    setKeyChanges({}); setKeySources({}); setServiceKeys({}); setModels(null); setModelResult(''); setEmbeddingResult('');
    setClusterResult('');
    setMatrixResult('');
    setIMessageResult('');
    setDirty(!next.instance.settings.models.length); setAdminPassword(''); setDiscarding(false);
  }
  async function save() {
    if (!draft) return;
    if (pendingOperation.current) throw new Error('上次运行操作的状态尚未确认，请等待状态更新或重试同一操作后再保存');
    const groupIds = groups.split(/[\s,，]+/).filter(Boolean);
    if (!draft.admin_qq || !/^\d{5,15}$/.test(draft.admin_qq)) throw new Error('请填写你的管理员 QQ 号');
    if (groupIds.some(group => !/^\d{5,15}$/.test(group))) throw new Error('群号只填数字，用空格或换行分开');
    const botQQ = qq?.online ? qq.qq : draft.bot_qq;
    if (!botQQ) throw new Error('请先扫码登录 QQ');
    validateCatalog(draft, keyChanges);
    const settings: InstanceSettings = { ...draft, bot_qq: botQQ, group_ids: groupIds, models: draft.models.map(cleanProfile) };
    // Older managers reject unknown component sections even when disabled.
    const { cluster, matrix, imessage, capacity, ...legacyComponents } = { ...componentDefaults(), ...settings.components };
    const compatibleSettings = { ...settings, components: { ...legacyComponents,
      ...(status?.cluster_configuration?.schema === 1 ? { cluster } : {}), ...(status?.matrix_configuration?.schema === 1 ? { matrix } : {}),
      ...(status?.imessage_configuration?.schema === 1 ? { imessage } : {}),
      ...(status?.capacity_configuration?.schema === 1 ? { capacity } : {}) } };
    const next = await instanceCall<InstanceStatus>(id, 'save', { revision, settings: compatibleSettings, model_keys: keyChanges, component_keys: serviceKeys,
      ...(Object.keys(keySources).length ? { model_key_sources: keySources } : {}) });
    setStatus(next); setDraft(next.instance.settings); setRevision(next.instance.revision);
    setKeyChanges({}); setKeySources({}); setServiceKeys({}); setDirty(false);
  }
  async function activate(kind: OperationKind, backupId?: string) {
    if (!status) return;
    if (!['stop', 'recover', 'resume_qq'].includes(kind) && dirty) throw new Error('请先保存设置，再提交运行或维护操作');
    if (kind === 'resume_qq' && !handoverConfirmed) throw new Error('请确认原服务器的 QQ 已停止');
    if (pendingOperation.current && (pendingOperation.current.kind !== kind || pendingOperation.current.backup_id !== backupId)) {
      throw new Error('上次运行操作的状态尚未确认，请等待状态更新或重试同一操作');
    }
    const request: OperationRequest = pendingOperation.current || {
      id: crypto.randomUUID().replaceAll('-', ''), kind, revision: status.instance.revision,
      ...(kind === 'activate' && adminPassword ? { administrator: { username: adminName, password: adminPassword } } : {}),
      ...(kind === 'restore' ? { backup_id: backupId, confirm: true } : {}),
      ...(kind === 'resume_qq' ? { confirm: true } : {}),
    };
    pendingOperation.current = request;
    try {
      const operation = await instanceCall<NonNullable<InstanceStatus['operation']>>(id, 'operation', request);
      pendingOperation.current = null;
      if (alive.current) { setStatus(current => current ? { ...current, operation } : current); setAdminPassword(''); }
    } catch (cause) {
      if (cause instanceof DesktopError && cause.code === 'setup_failed') pendingOperation.current = null;
      throw cause;
    }
  }
  if (!status || !draft) return <div className="setup-loading"><LoaderCircle className="spin" size={20} /><span>正在连接配置入口</span>{pollError && <ErrorText text={pollError} />}</div>;
  return <section className="instance-setup">
    <div className="setup-tabs" role="tablist" aria-label="机器人配置">
      <button role="tab" aria-selected={view === 'qq'} onClick={() => setView('qq')}>QQ 登录 {qq?.online && <Check size={15} />}</button>
      <button role="tab" aria-selected={view === 'settings'} onClick={() => setView('settings')}>模型与启用</button>
      <button role="tab" aria-selected={view === 'health'} onClick={() => setView('health')}>运行检查</button>
    </div>
    {pollError && <ErrorText text={pollError} />}
    {error && <ErrorText text={error} />}
    {status.operation && <div className={`notice ${status.operation.status === 'failed' ? 'error' : 'install-notice'}`} role="status">{working ? <LoaderCircle size={17} className="spin" /> : <CircleAlert size={17} />}<span>{status.operation.message}</span></div>}
    {view === 'health' ? <ReadinessPanel key={id} id={id} revision={status.instance.revision} suspended={disabled} onNavigate={setView} /> : view === 'qq' ? <div className="qq-setup">
      <h3>{status.qq_handover ? '接管迁移后的 QQ' : qq?.online ? 'QQ 已登录' : phaseLabels[qq?.phase || ''] || '扫码登录机器人 QQ'}</h3>
      {status.qq_handover ? <>
        <div className="notice warning"><CircleAlert size={17} /><span>已恢复其他实例的数据。请先停止原服务器的机器人和 QQ，避免同时登录导致掉线。接管不会自动启用机器人。</span></div>
        <label className="checkbox"><input type="checkbox" checked={handoverConfirmed} disabled={disabled} onChange={event => setHandoverConfirmed(event.target.checked)} />我已停止原服务器的 QQ</label>
        <button className="primary" disabled={disabled || !handoverConfirmed} onClick={() => void run('接管 QQ', () => activate('resume_qq'))}><Play size={16} />{status.qq_handover.state === 'approved' ? '重试启动此处 QQ' : '启动此处 QQ'}</button>
      </> : qq?.online ? <div className="qq-identity"><Check size={32} /><strong>{qq.nickname}</strong><span>{qq.qq}</span></div>
        : <><div className="qr-frame">{qq?.qrcode ? <QRCodeSVG value={qq.qrcode} size={212} marginSize={2} /> : <LoaderCircle size={28} className="spin" />}</div>
          {qq?.has_error && <ErrorText text="QQ 尚未完成登录，请查看手机 QQ 提示或刷新二维码" />}
          <button className="secondary" disabled={disabled} onClick={() => void run('刷新二维码', async () => setQQ(await instanceCall<QQStatus>(id, 'qqRefresh')))}><RefreshCw size={16} />刷新二维码</button></>}
      <div className="form-actions"><span className="muted">手机 QQ 扫码并确认登录</span><button className="primary" disabled={!qq?.online} onClick={() => { change('bot_qq', qq!.qq); setView('settings'); }}><ArrowRight size={16} />下一步</button></div>
    </div> : <form onSubmit={event => { event.preventDefault(); void run('保存设置', save); }}>
      <fieldset disabled={disabled || status.maintenance_pending}>
        <div className="field-grid"><label className="field"><span>机器人名称</span><input value={draft.name} maxLength={80} onChange={event => change('name', event.target.value)} required /></label>
          <label className="field"><span>你的 QQ 号（管理员）</span><input value={draft.admin_qq} inputMode="numeric" onChange={event => change('admin_qq', event.target.value)} required /></label></div>
        <ModelCatalog draft={draft} selected={selectedModel} keyChanges={keyChanges} available={models} testResult={modelResult} blocked={disabled || Boolean(status.maintenance_pending)}
          onSelect={selectModel} onDefault={id => change('default_model', id)} onModel={changeModel}
          onKey={value => { modelGeneration.current++; if (model) {
            setKeyChanges(current => ({ ...current, [model.id]: value }));
            setKeySources(current => { const next = { ...current }; delete next[model.id]; return next; });
          } setModels(null); setDirty(true); setModelResult(''); }}
          onImport={names => void run('添加模型', async () => importModels(names))}
          onAdd={() => { const added = newModel(); change('models', [...draft.models, added]); selectModel(added.id); }}
          onRemove={id => { const next = removeModel(draft, id); setDraft(next); setDirty(true); selectModel(next.default_model);
            setKeyChanges(current => { const kept = { ...current }; delete kept[id]; return kept; });
            setKeySources(current => { const kept = { ...current }; delete kept[id]; return kept; }); }}
          onList={() => void run('读取模型', async () => {
            if (!model) return;
            const generation = modelGeneration.current;
            const result = await instanceCall<{ models: string[] }>(id, 'modelList', { model: { ...probeProfile(model), model: model.model || 'list' }, key: keyFor(model, keyChanges) });
            if (!alive.current || generation !== modelGeneration.current) return;
            setModels(result.models);
          })}
          onTest={() => void run('测试模型', async () => {
            if (!model) return;
            const generation = modelGeneration.current;
            const result = await instanceCall<{ latency_ms: number }>(id, 'modelTest', { model: probeProfile(model), key: keyFor(model, keyChanges) });
            if (!alive.current || generation !== modelGeneration.current) return;
            setModelResult(`模型已返回文字，耗时 ${(result.latency_ms / 1000).toFixed(1)} 秒`);
          })} />
        <label className="field"><span>启用的 QQ 群号（每行一个）</span><textarea value={groups} rows={3} onChange={event => { setGroups(event.target.value); setDirty(true); }} placeholder="留空时不回复群消息" disabled={draft.allow_all_groups} /></label>
        <label className="checkbox"><input type="checkbox" checked={draft.allow_all_groups} onChange={event => change('allow_all_groups', event.target.checked)} />允许机器人在它加入的所有群中回复</label>
        <details className="advanced"><summary>机器人说话风格</summary><label className="field"><span>人设与回答要求</span><textarea value={draft.system_prompt} rows={5} maxLength={16000} onChange={event => change('system_prompt', event.target.value)} /></label></details>
        <ComponentPanel draft={draft} profiles={draft.models} videoModel={status.video_model} sandboxRuntime={status.sandbox_runtime}
          capacitySupported={status.capacity_configuration?.schema === 1}
          clusterSupported={status.cluster_configuration?.schema === 1} clusterResult={clusterResult}
          matrixSupported={status.matrix_configuration?.schema === 1} matrixResult={matrixResult}
          imessageSupported={status.imessage_configuration?.schema === 1} imessageResult={imessageResult}
          keysPresent={status.instance.component_keys_present || []} serviceKeys={serviceKeys}
          onChange={(key, value) => { componentGeneration.current++; change(key, value); setEmbeddingResult(''); setClusterResult(''); setMatrixResult(''); setIMessageResult(''); }}
          onKeys={keys => { componentGeneration.current++; setServiceKeys(keys); setDirty(true); setEmbeddingResult(''); setClusterResult(''); setMatrixResult(''); setIMessageResult(''); }} testResult={embeddingResult}
          onTest={() => void run('测试向量接口', async () => {
            const generation = componentGeneration.current;
            const result = await instanceCall<{ dimensions: number; latency_ms: number }>(id, 'embeddingTest', {
              options: (draft.components || componentDefaults()).embedding,
              key: Object.hasOwn(serviceKeys, 'embedding') ? serviceKeys.embedding : status.instance.component_keys_present?.includes('embedding') ? null : '',
            });
            if (alive.current && generation === componentGeneration.current) setEmbeddingResult(`已返回 ${result.dimensions} 维向量，耗时 ${(result.latency_ms / 1000).toFixed(1)} 秒`);
          })}
          onClusterTest={() => void run('检测集群连接', async () => {
            const generation = componentGeneration.current;
            const result = await instanceCall<{ available: number; unavailable: number; upstream_ready: boolean; latency_ms: number }>(id, 'clusterTest', {
              options: { ...componentDefaults(), ...draft.components }.cluster,
              key: Object.hasOwn(serviceKeys, 'cluster') ? serviceKeys.cluster : status.instance.component_keys_present?.includes('cluster') ? null : '',
            });
            if (!alive.current || generation !== componentGeneration.current) return;
            setClusterResult(result.upstream_ready
              ? `网关已连接 · ${result.available} 项可用 / ${result.unavailable} 项不可用 · ${(result.latency_ms / 1000).toFixed(1)} 秒`
              : '网关已连接，但上游查询尚未就绪；不代表服务器状态正常');
          })}
          onMatrixTest={() => void run('检测 Matrix', async () => {
            const generation = componentGeneration.current;
            const result = await instanceCall<{ rooms: number; latency_ms: number }>(id, 'matrixTest', {
              options: { ...componentDefaults(), ...draft.components }.matrix,
              key: Object.hasOwn(serviceKeys, 'matrix') ? serviceKeys.matrix : status.instance.component_keys_present?.includes('matrix') ? null : '',
            });
            if (alive.current && generation === componentGeneration.current) setMatrixResult(`账户与 ${result.rooms} 个房间已核对 · ${(result.latency_ms / 1000).toFixed(1)} 秒 · 未发送消息`);
          })}
          onIMessageTest={() => void run('检测 iMessage', async () => {
            const generation = componentGeneration.current;
            const result = await instanceCall<{ chats: number; latency_ms: number }>(id, 'imessageTest', {
              options: { ...componentDefaults(), ...draft.components }.imessage,
              key: Object.hasOwn(serviceKeys, 'imessage') ? serviceKeys.imessage : status.instance.component_keys_present?.includes('imessage') ? null : '',
            });
            if (alive.current && generation === componentGeneration.current) setIMessageResult(`已核对 ${result.chats} 个会话的读取接口 · ${(result.latency_ms / 1000).toFixed(1)} 秒 · 未验证发送`);
          })} />
        <div className="form-actions"><span className="muted">配置版本 {revision} {dirty ? '· 有未保存更改' : '· 已保存'}{status.instance.revision !== revision ? ' · 服务器有新版本' : ''}</span>
          <div className="catalog-save-actions"><button type="button" className="secondary icon-button" title="重新载入服务器配置" aria-label="重新载入服务器配置" onClick={() => dirty ? setDiscarding(true) : void run('重新载入配置', reloadSettings)}><RefreshCw size={16} /></button>
            <button type="submit" className="primary"><Save size={16} />保存设置</button></div></div>
      </fieldset>
      <section className="activation"><h3>控制台与运行</h3>
        {!status.desired.bot_enabled && <div className="field-grid"><label className="field"><span>控制台账户名</span><input value={adminName} onChange={event => setAdminName(event.target.value)} autoComplete="username" disabled={disabled} /></label>
          <label className="field"><span>控制台密码（首次设置至少 12 位）</span><input type="password" value={adminPassword} onChange={event => setAdminPassword(event.target.value)} autoComplete="new-password" disabled={disabled} /></label></div>}
        <div className="component-states">{services.map(service => <span className="tag neutral" key={service.name}>{({ bot: '机器人', database: '数据库', napcat: 'QQ 组件' })[service.name] || service.name}: {({ running: '运行中', exited: '已停止', restarting: '重启中', created: '待启动' })[service.state] || service.state}</span>)}</div>
        <div className="activation-actions"><button type="button" className="primary" disabled={disabled || status.maintenance_pending || dirty || !draft.default_model} onClick={() => void run('启用机器人', () => activate('activate'))}><Play size={16} />{status.desired.bot_enabled ? '应用并重新连接' : '启用机器人'}</button>
          <button type="button" className="secondary" disabled={disabled || status.maintenance_pending || !status.desired.bot_enabled} onClick={() => void run('停止机器人', () => activate('stop'))}><Square size={15} />停止</button>
          <button type="button" className="secondary" disabled={disabled || status.maintenance_pending || !status.desired.bot_enabled} onClick={() => void run('打开控制台', async () => { await unwrap(desktop?.openConsole(id)); })}><ExternalLink size={16} />打开控制台</button></div>
      </section>
      {!suspended && <BackupPanel id={id} status={status} disabled={disabled} onOperation={(kind, backupId) => void run('提交维护操作', () => activate(kind, backupId))} />}
      <dialog className="modal catalog-dialog" ref={discardDialog} onCancel={() => setDiscarding(false)} aria-labelledby="discard-settings-title">
        <h2 id="discard-settings-title">丢弃未保存的更改？</h2><p>重新读取服务器配置，本次尚未保存的模型、密钥和其他修改将被清除。服务器上的配置不会改变。</p>
        <div className="form-actions"><button type="button" className="secondary" onClick={() => setDiscarding(false)}>保留当前编辑</button>
          <button type="button" className="primary" disabled={disabled} onClick={() => void run('重新载入配置', reloadSettings)}><RefreshCw size={16} />丢弃并重新载入</button></div>
      </dialog>
    </form>}
    {busy && <div className="setup-busy" role="status"><LoaderCircle className="spin" size={16} />{busy}</div>}
  </section>;
}
