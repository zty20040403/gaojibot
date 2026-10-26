import { Play, Settings2 } from 'lucide-react';
import { useEffect, useId, useState } from 'react';
import type { ComponentSettings, InstanceSettings, InstanceStatus, ModelProfile } from './api';
import { MatrixPanel } from './MatrixPanel';
import { IMessagePanel } from './IMessagePanel';

export function componentDefaults(): ComponentSettings {
  return {
    capacity: { max_data_gib: 100, reserve_free_gib: 2 },
    search: { max_results: 5, timeout_seconds: 10 },
    voice: { name: 'zh-CN-XiaoxiaoNeural', rate: '+0%', pitch: '+0Hz', max_chars: 350 },
    embedding: { base_url: '', model: 'bge-m3', dimensions: 1024, api_key_required: false },
    cluster: { base_url: '', timeout_seconds: 12, allowed_groups: [], log_allowed_groups: [] },
    matrix: { enabled: false, base_url: '', user_id: '', routes: [] },
    imessage: { enabled: false, base_url: '', bot_handle: '', routes: [] },
    historian: { profile: '' }, alerts: { base_url: '', group_id: '' },
    video: { max_duration_minutes: 60, max_download_mb: 512, frame_count: 8, threads: 2, timeout_seconds: 1800 },
  };
}

const features = [
  ['search', '联网搜索'], ['media', '识图与表情库'], ['voice', '语音回复'], ['browser', '网页浏览器'],
  ['subagents', '子 Agent'], ['historian', '聊天章节整理'], ['semantic', '语义记忆检索'],
  ['proactive', '主动参与群聊'], ['notifications', '群告警'], ['video', '视频深入分析'], ['sandbox', '隔离沙盒'], ['cluster', '外部集群'],
];
const splitGroups = (text: string) => text.split(/[\s,，]+/).filter(Boolean);

function GroupField({ label, groups, onChange }: { label: string; groups: string[]; onChange: (groups: string[]) => void }) {
  const labelId = useId();
  const [text, setText] = useState(groups.join('\n'));
  useEffect(() => {
    setText(current => JSON.stringify(splitGroups(current)) === JSON.stringify(groups) ? current : groups.join('\n'));
  }, [groups]);
  return <label className="field"><span id={labelId}>{label}</span><textarea aria-labelledby={labelId} rows={2} value={text} placeholder="留空：仅管理员" onChange={event => {
    setText(event.target.value); onChange(splitGroups(event.target.value));
  }} /></label>;
}

export function ComponentPanel({ draft, profiles, keysPresent, serviceKeys, onChange, onKeys, onTest, testResult, videoModel, sandboxRuntime, clusterSupported, onClusterTest, clusterResult, matrixSupported, onMatrixTest, matrixResult, imessageSupported, onIMessageTest, imessageResult, capacitySupported }: {
  draft: InstanceSettings; profiles: ModelProfile[]; keysPresent: string[]; serviceKeys: Record<string, string>;
  onChange: <K extends keyof InstanceSettings>(key: K, value: InstanceSettings[K]) => void;
  onKeys: (keys: Record<string, string>) => void; onTest: () => void; testResult: string;
  videoModel: InstanceStatus['video_model'];
  sandboxRuntime: InstanceStatus['sandbox_runtime'];
  clusterSupported: boolean; onClusterTest: () => void; clusterResult: string;
  matrixSupported: boolean; onMatrixTest: () => void; matrixResult: string;
  imessageSupported: boolean; onIMessageTest: () => void; imessageResult: string;
  capacitySupported: boolean;
}) {
  const options = { ...componentDefaults(), ...draft.components };
  function update<K extends keyof ComponentSettings>(section: K, value: Partial<ComponentSettings[K]>) {
    onChange('components', { ...options, [section]: { ...options[section], ...value } });
  }
  const hasKey = keysPresent.includes('embedding') && !Object.hasOwn(serviceKeys, 'embedding');
  const hasClusterKey = keysPresent.includes('cluster') && !Object.hasOwn(serviceKeys, 'cluster');
  const unavailableFeatures: Record<string, string> = { ...(!clusterSupported ? { cluster: '服务端需更新' } : {}), ...(!sandboxRuntime?.supported ? { sandbox: sandboxRuntime?.state === 'desktop_unsupported' ? '本机版暂不支持' : '服务端需更新' } : {}) };
  return <details className="advanced component-config">
    <summary><Settings2 size={16} />功能组件</summary>
    <div className="feature-switches">{features.map(([key, label]) => <div className="feature-switch-row" key={key}>
      <label className="checkbox"><input type="checkbox" role="switch" checked={Boolean(draft.features[key])}
        disabled={(Boolean(unavailableFeatures[key]) || key === 'video' && !videoModel) && !draft.features[key]} onChange={event => onChange('features', { ...draft.features, [key]: event.target.checked })} />{label}</label>
      <span className="muted">{unavailableFeatures[key] || (key === 'video' && !videoModel ? '服务端需更新' : draft.features[key] ? '已选择' : '未选择')}</span>
    </div>)}<div className="feature-switch-row">
      <label className="checkbox"><input type="checkbox" role="switch" checked={options.matrix.enabled} disabled={!matrixSupported && !options.matrix.enabled}
        onChange={event => update('matrix', { enabled: event.target.checked })} />Matrix 消息互通</label>
      <span className="muted">{!matrixSupported ? '服务端需更新' : options.matrix.enabled ? '已选择' : '未选择'}</span>
    </div><div className="feature-switch-row">
      <label className="checkbox"><input type="checkbox" role="switch" checked={options.imessage.enabled} disabled={!imessageSupported && !options.imessage.enabled}
        onChange={event => update('imessage', { enabled: event.target.checked })} />iMessage 消息互通</label>
      <span className="muted">{!imessageSupported ? '服务端需更新' : options.imessage.enabled ? '已选择' : '未选择'}</span>
    </div></div>
    {capacitySupported && <div className="component-options"><h4>存储容量</h4><div className="field-grid">
      <label className="field"><span>本实例数据额度（GiB）</span><input type="number" min={5} max={1048576} step={1} value={options.capacity.max_data_gib}
        onChange={event => update('capacity', { max_data_gib: Number(event.target.value) })} /></label>
      <label className="field"><span>磁盘预留空间（GiB）</span><input type="number" min={1} max={1024} step={1} value={options.capacity.reserve_free_gib}
        onChange={event => update('capacity', { reserve_free_gib: Number(event.target.value) })} /></label>
    </div></div>}
    {draft.features.sandbox && sandboxRuntime?.supported && <div className="component-options"><h4>隔离沙盒</h4>
      <div className="video-model-state"><span className="tag neutral">{sandboxRuntime.state === 'recorded' ? '启用时复查镜像' : '应用时安装镜像'}</span>
        {sandboxRuntime.bytes > 0 && <span>{(sandboxRuntime.bytes / 1024 ** 3).toFixed(2)} GiB</span>}
        {sandboxRuntime.state !== 'recorded' && <span>首次安装：引擎内存 ≥ 6 GiB · 空间 ≥ 12 GiB</span>}
        <span>每个 1 CPU / 1 GiB · 最多 2 个</span></div>
    </div>}
    {draft.features.search && <div className="component-options"><h4>搜索</h4><div className="field-grid">
      <label className="field"><span>搜索结果数</span><input type="number" min={1} max={20} value={options.search.max_results} onChange={event => update('search', { max_results: Number(event.target.value) })} /></label>
      <label className="field"><span>搜索超时（秒）</span><input type="number" min={3} max={60} value={options.search.timeout_seconds} onChange={event => update('search', { timeout_seconds: Number(event.target.value) })} /></label>
    </div></div>}
    {draft.features.media && <div className="component-options"><h4>图片</h4>
      <label className="field"><span id="vision-model-label">识图模型</span><select aria-labelledby="vision-model-label" value={draft.vision_model} onChange={event => onChange('vision_model', event.target.value)}>
        <option value="">默认模型（需支持识图）</option>{profiles.filter(model => model.vision).map(model => <option key={model.id} value={model.id}>{model.label || model.id}</option>)}
      </select></label>
    </div>}
    {draft.features.voice && <div className="component-options"><h4>语音 · Edge TTS</h4>
      <label className="field"><span>音色名称</span><input list="voice-presets" value={options.voice.name} maxLength={100} onChange={event => update('voice', { name: event.target.value })} /></label>
      <datalist id="voice-presets"><option value="zh-CN-XiaoxiaoNeural" /><option value="zh-CN-XiaoyiNeural" /><option value="zh-CN-YunxiNeural" /><option value="zh-TW-HsiaoChenNeural" /></datalist>
      <div className="field-grid"><label className="field"><span>语速</span><input value={options.voice.rate} placeholder="+0%" onChange={event => update('voice', { rate: event.target.value })} /></label>
        <label className="field"><span>音调</span><input value={options.voice.pitch} placeholder="+0Hz" onChange={event => update('voice', { pitch: event.target.value })} /></label></div>
      <label className="field"><span>单条语音字数上限</span><input type="number" min={50} max={1000} value={options.voice.max_chars} onChange={event => update('voice', { max_chars: Number(event.target.value) })} /></label>
    </div>}
    {draft.features.historian && <div className="component-options"><h4>聊天章节</h4>
      <label className="field"><span id="historian-model-label">整理模型</span><select aria-labelledby="historian-model-label" value={options.historian.profile} onChange={event => update('historian', { profile: event.target.value })}>
        <option value="">跟随默认模型</option>{profiles.map(model => <option key={model.id} value={model.id}>{model.label || model.id}</option>)}
      </select></label>
    </div>}
    {draft.features.video && <div className="component-options"><h4>视频分析</h4>
      {videoModel && <div className="video-model-state" role="status"><strong>{videoModel.model}</strong><span className="tag neutral">{
        ({ installed: '模型已下载', not_installed: '应用时下载', partial: '下载未完成', needs_verification: '需重新校验', invalid: '模型目录异常' } as Record<string, string>)[videoModel.state] || '状态待确认'
      }</span><span>{(videoModel.downloaded_bytes / 1024 ** 2).toFixed(1)} / {(videoModel.total_bytes / 1024 ** 2).toFixed(1)} MiB</span></div>}
      <div className="field-grid"><label className="field"><span>最长视频（分钟）</span><input type="number" min={1} max={60} value={options.video.max_duration_minutes} onChange={event => update('video', { max_duration_minutes: Number(event.target.value) })} /></label>
        <label className="field"><span>单条下载上限（MiB）</span><input type="number" min={10} max={1024} value={options.video.max_download_mb} onChange={event => update('video', { max_download_mb: Number(event.target.value) })} /></label>
        <label className="field"><span>关键帧数量</span><input type="number" min={4} max={12} value={options.video.frame_count} onChange={event => update('video', { frame_count: Number(event.target.value) })} /></label>
        <label className="field"><span>转写 CPU 线程</span><input type="number" min={1} max={8} value={options.video.threads} onChange={event => update('video', { threads: Number(event.target.value) })} /></label>
        <label className="field"><span>分析超时（秒）</span><input type="number" min={60} max={1800} value={options.video.timeout_seconds} onChange={event => update('video', { timeout_seconds: Number(event.target.value) })} /></label></div>
    </div>}
    {draft.features.semantic && <div className="component-options"><h4>Embedding</h4>
      <label className="field"><span>Embedding API 地址</span><input type="url" value={options.embedding.base_url} placeholder="https://服务地址/v1" onChange={event => {
        update('embedding', { base_url: event.target.value }); onKeys({ ...serviceKeys, embedding: '' });
      }} /></label>
      <div className="field-grid"><label className="field"><span>Embedding 模型</span><input value={options.embedding.model} maxLength={200} onChange={event => update('embedding', { model: event.target.value })} /></label>
        <label className="field"><span>向量维数（数据库固定）</span><input type="number" value={options.embedding.dimensions} readOnly /></label></div>
      <label className="checkbox"><input type="checkbox" checked={options.embedding.api_key_required} onChange={event => update('embedding', { api_key_required: event.target.checked })} />Embedding 服务需要密钥</label>
      <label className="field"><span>Embedding Key {hasKey ? '（已保存在服务器）' : ''}</span><input type="password" autoComplete="off" maxLength={8192} value={serviceKeys.embedding || ''} placeholder={hasKey ? '留空保持原密钥' : '服务无密钥时留空'} onChange={event => onKeys({ ...serviceKeys, embedding: event.target.value })} /></label>
      <div className="model-test"><button type="button" className="secondary" onClick={onTest}><Play size={16} />测试向量接口</button><span role="status">{testResult}</span></div>
    </div>}
    {draft.features.notifications && <div className="component-options"><h4>群告警</h4>
      <label className="field"><span>Alertmanager 地址</span><input type="url" value={options.alerts.base_url} onChange={event => update('alerts', { base_url: event.target.value })} /></label>
      <label className="field"><span>接收告警的群号</span><input inputMode="numeric" value={options.alerts.group_id} onChange={event => update('alerts', { group_id: event.target.value })} /></label>
    </div>}
    {draft.features.cluster && clusterSupported && <div className="component-options"><h4>Gaoji Cluster Control</h4>
      <label className="field"><span>集群网关地址</span><input type="url" value={options.cluster.base_url} maxLength={2048} placeholder="https://你的集群网关" onChange={event => {
        update('cluster', { base_url: event.target.value }); onKeys({ ...serviceKeys, cluster: '' });
      }} /></label>
      <label className="field"><span>集群 Token {hasClusterKey ? '（已保存在服务器）' : ''}</span><input type="password" autoComplete="off" maxLength={512} value={serviceKeys.cluster || ''}
        placeholder={hasClusterKey ? '留空保持原密钥' : '填写网关的 API Token'} onChange={event => onKeys({ ...serviceKeys, cluster: event.target.value })} /></label>
      <label className="field"><span>集群请求超时（秒）</span><input type="number" min={1} max={30} value={options.cluster.timeout_seconds} onChange={event => update('cluster', { timeout_seconds: Number(event.target.value) })} /></label>
      <div className="field-grid"><GroupField label="允许集群查询的群号" groups={options.cluster.allowed_groups} onChange={groups => update('cluster', { allowed_groups: groups })} />
        <GroupField label="允许查看集群日志的群号" groups={options.cluster.log_allowed_groups} onChange={groups => update('cluster', { log_allowed_groups: groups })} /></div>
      <div className="model-test"><button type="button" className="secondary" onClick={onClusterTest}><Play size={16} />检测集群连接</button><span role="status">{clusterResult}</span></div>
    </div>}
    {options.matrix.enabled && matrixSupported && <MatrixPanel options={options.matrix} keyValue={serviceKeys.matrix || ''}
      keySaved={keysPresent.includes('matrix') && !Object.hasOwn(serviceKeys, 'matrix')}
      onChange={value => update('matrix', value)} onKey={value => onKeys({ ...serviceKeys, matrix: value })} onTest={onMatrixTest} result={matrixResult} />}
    {options.imessage.enabled && imessageSupported && <IMessagePanel options={options.imessage} keyValue={serviceKeys.imessage || ''}
      keySaved={keysPresent.includes('imessage') && !Object.hasOwn(serviceKeys, 'imessage')}
      onChange={value => update('imessage', value)} onKey={value => onKeys({ ...serviceKeys, imessage: value })} onTest={onIMessageTest} result={imessageResult} />}
  </details>;
}
