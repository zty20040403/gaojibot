import { useEffect, useRef, useState } from 'react';
import { ArrowDown, ArrowUp, Check, Play, Plus, RefreshCw, Trash2, X } from 'lucide-react';
import type { InstanceSettings, ModelProfile } from './api';
import { canFallback, keyFor, modelPurpose } from './modelSettings';

const providers = [
  ['deepseek', 'DeepSeek', 'https://api.deepseek.com/v1'], ['openai', 'OpenAI', 'https://api.openai.com/v1'],
  ['cliproxy', 'CLIProxyAPI', ''], ['anthropic', 'Anthropic', 'https://api.anthropic.com/v1'],
  ['qwen', 'Qwen / 本地千问', ''], ['custom', '其他 OpenAI 兼容服务', ''],
];

export function ModelCatalog({ draft, selected, keyChanges, available, testResult, blocked, onSelect, onAdd, onRemove, onDefault, onModel, onKey, onList, onTest, onImport }: {
  draft: InstanceSettings; selected: string; keyChanges: Record<string, string>; available: string[] | null; testResult: string; blocked: boolean;
  onSelect: (id: string) => void; onAdd: () => void; onRemove: (id: string) => void; onDefault: (id: string) => void;
  onModel: (changes: Partial<ModelProfile>) => void; onKey: (value: string) => void; onList: () => void; onTest: () => void;
  onImport: (names: string[]) => void;
}) {
  const model = draft.models.find(item => item.id === selected) || draft.models[0];
  const [removing, setRemoving] = useState<ModelProfile | null>(null);
  const [query, setQuery] = useState('');
  const [checked, setChecked] = useState<string[]>([]);
  useEffect(() => { setQuery(''); setChecked([]); }, [available, selected]);
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    if (removing && !dialog.current?.open) dialog.current?.showModal();
    else if (!removing) dialog.current?.close();
  }, [removing]);
  if (!model) return null;
  const storedKey = keyFor(model, keyChanges) === null;
  const configured = new Set(draft.models.filter(item => item.base_url === model.base_url && item.protocol === model.protocol).map(item => item.model));
  const visible = (available || []).filter(name => name.toLowerCase().includes(query.toLowerCase().trim()));
  const eligible = visible.filter(name => modelPurpose(name) === 'chat' && !configured.has(name));
  const selectedNames = checked.filter(name => !configured.has(name));
  const supportsEffort = model.protocol === 'openai-chat' && ['openai', 'cliproxy'].includes(model.provider);
  const supportsThinking = model.protocol === 'anthropic-messages' || ['deepseek', 'qwen'].includes(model.provider);
  function move(index: number, offset: number) {
    const targets = [...model.fallbacks];
    [targets[index], targets[index + offset]] = [targets[index + offset], targets[index]];
    onModel({ fallbacks: targets });
  }
  return <section className="model-catalog" aria-labelledby="model-catalog-title">
    <div className="catalog-heading"><h3 id="model-catalog-title">模型目录</h3><span className="tag neutral">已配置 {draft.models.length} 个</span></div>
    <label className="field"><span id="default-model-label">默认聊天模型</span><select aria-labelledby="default-model-label" value={draft.default_model} onChange={event => onDefault(event.target.value)}>
      {draft.models.map(item => <option key={item.id} value={item.id}>{item.label || item.id}</option>)}
    </select></label>
    <div className="catalog-picker"><label className="field"><span id="editing-model-label">正在编辑的模型</span><select aria-labelledby="editing-model-label" value={model.id} onChange={event => onSelect(event.target.value)}>
      {draft.models.map(item => <option key={item.id} value={item.id}>{item.label || item.id}{item.id === draft.default_model ? '（默认）' : ''}</option>)}
    </select></label>
      <button type="button" className="secondary icon-button" title="添加模型" aria-label="添加模型" disabled={draft.models.length >= 100} onClick={onAdd}><Plus size={18} /></button>
      <button type="button" className="secondary icon-button danger" title={model.id === draft.default_model ? '先更换默认模型才能删除' : '删除此模型'} aria-label="删除此模型" disabled={model.id === draft.default_model} onClick={() => setRemoving(model)}><Trash2 size={17} /></button>
    </div>
    <label className="field"><span>模型显示名称</span><input value={model.label} maxLength={100} onChange={event => onModel({ label: event.target.value })} required /></label>
    <label className="field"><span id="model-provider-label">模型服务</span><select aria-labelledby="model-provider-label" value={model.provider} onChange={event => {
      const provider = event.target.value;
      onModel({ provider, protocol: provider === 'anthropic' ? 'anthropic-messages' : 'openai-chat',
        base_url: providers.find(item => item[0] === provider)?.[2] || '', model: provider === 'deepseek' ? 'deepseek-chat' : '',
        thinking: 'auto', reasoning_effort: '' });
    }}>
      {providers.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      {!providers.some(item => item[0] === model.provider) && <option value={model.provider}>{model.provider}</option>}
    </select></label>
    <label className="field"><span>API 地址</span><input value={model.base_url} type="url" placeholder="https://服务地址/v1" onChange={event => onModel({ base_url: event.target.value })} required /></label>
    <div className="catalog-key"><label className="field"><span>API Key {storedKey ? '（已保存在服务器）' : ''}</span><input type="password" autoComplete="off" value={keyChanges[model.id] || ''}
      placeholder={storedKey ? '留空保持原密钥' : '填写你自己的 API Key'} maxLength={8192} onChange={event => onKey(event.target.value)} /></label>
      <button type="button" className="icon-button secondary" title="清除当前模型密钥（保存后生效）" aria-label="清除当前模型密钥" onClick={() => onKey('')}><X size={17} /></button>
    </div>
    <div className="model-picker"><label className="field"><span>模型名称</span><input value={model.model} onChange={event => onModel({ model: event.target.value })} required maxLength={200} /></label>
      <button type="button" className="secondary" disabled={model.protocol !== 'openai-chat'} onClick={onList}><RefreshCw size={16} />读取列表</button>
    </div>
    {available !== null && <div className="discovered-models">
      <div className="catalog-heading"><strong>接口返回 {available.length} 个模型</strong><span className="muted">当前接口已配置 {configured.size} 个</span></div>
      <label className="field"><span>搜索接口模型</span><input type="search" value={query} onChange={event => setQuery(event.target.value)} /></label>
      <div className="discovery-actions"><label className="checkbox"><input type="checkbox" aria-label="全选可添加的聊天模型" disabled={!eligible.length || blocked}
        checked={eligible.length > 0 && eligible.every(name => selectedNames.includes(name))} onChange={event => setChecked(event.target.checked ? [...new Set([...checked, ...eligible])] : checked.filter(name => !eligible.includes(name)))} />全选</label>
        <button type="button" className="secondary" disabled={blocked || !selectedNames.length} onClick={() => onImport(selectedNames)}><Plus size={16} />添加所选 ({selectedNames.length})</button></div>
      <div className="discovered-list" role="group" aria-label="接口模型列表">
        {visible.map(name => <div className="discovered-row" key={name}>
          <label className="checkbox"><input type="checkbox" aria-label={`添加 ${name}`} disabled={blocked || configured.has(name) || modelPurpose(name) !== 'chat'} checked={selectedNames.includes(name)}
            onChange={event => setChecked(event.target.checked ? [...checked, name] : checked.filter(item => item !== name))} /><span>{name}</span></label>
          {modelPurpose(name) !== 'chat' ? <span className="tag neutral">{modelPurpose(name) === 'image' ? '图片生成' : '专用审查'}</span> : <>
            {configured.has(name) && <span className="tag neutral">已配置</span>}
            <button type="button" className="icon-button secondary" disabled={blocked} title={`选用 ${name}`} aria-label={`选用 ${name}`} onClick={() => onModel({ model: name })}><Check size={16} /></button>
          </>}
        </div>)}
        {!visible.length && <div className="muted discovery-empty">{available.length ? '没有匹配的模型' : '接口没有返回模型'}</div>}
      </div>
    </div>}
    <div className="model-test"><button type="button" className="secondary" onClick={onTest}><Play size={16} />测试连接</button><span className="green" role="status">{testResult}</span></div>
    <details className="advanced"><summary>模型高级设置</summary>
      <label className="field"><span id="model-protocol-label">接口类型</span><select aria-labelledby="model-protocol-label" value={model.protocol} onChange={event => onModel({ protocol: event.target.value as ModelProfile['protocol'], reasoning_effort: '', thinking: 'auto' })}>
        <option value="openai-chat">OpenAI 兼容</option><option value="anthropic-messages">Anthropic Messages</option>
      </select></label>
      <label className="checkbox"><input type="checkbox" checked={!model.api_key_required} onChange={event => onModel({ api_key_required: !event.target.checked })} />此服务不需要 API Key</label>
      <label className="checkbox"><input type="checkbox" checked={model.tools} onChange={event => onModel({ tools: event.target.checked })} />模型支持工具调用</label>
      <label className="checkbox"><input type="checkbox" checked={model.vision} onChange={event => onModel({ vision: event.target.checked })} />模型支持识图</label>
      {supportsThinking && <label className="field"><span id="model-thinking-label">思考模式</span><select aria-labelledby="model-thinking-label" value={model.thinking || 'auto'} onChange={event => onModel({ thinking: event.target.value as ModelProfile['thinking'] })}>
        <option value="auto">跟随服务端</option><option value="enabled">开启</option><option value="disabled">关闭</option>
      </select></label>}
      {supportsEffort && <label className="field"><span id="model-effort-label">推理强度</span><select aria-labelledby="model-effort-label" value={model.reasoning_effort} onChange={event => onModel({ reasoning_effort: event.target.value })}>
        <option value="">跟随服务端</option>{['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'].map(value => <option key={value} value={value}>{value}</option>)}
      </select></label>}
      <div className="field-grid"><label className="field"><span>输入 Token 上限（0 为自动）</span><input type="number" min={0} max={10000000} value={model.max_input_tokens} onChange={event => onModel({ max_input_tokens: Number(event.target.value) })} /></label>
        <label className="field"><span>输出 Token 上限（0 为默认）</span><input type="number" min={0} max={10000000} value={model.max_output_tokens} onChange={event => onModel({ max_output_tokens: Number(event.target.value) })} /></label></div>
      <div className="fallback-list"><h4>故障时的备选顺序</h4>
        {model.fallbacks.map((id, index) => <div className="fallback-row" key={id}><span>{index + 1}</span><strong>{draft.models.find(item => item.id === id)?.label || id}</strong>
          <button type="button" className="icon-button" aria-label={`上移备选模型 ${index + 1}`} title="上移" disabled={!index} onClick={() => move(index, -1)}><ArrowUp size={16} /></button>
          <button type="button" className="icon-button" aria-label={`下移备选模型 ${index + 1}`} title="下移" disabled={index === model.fallbacks.length - 1} onClick={() => move(index, 1)}><ArrowDown size={16} /></button>
          <button type="button" className="icon-button" aria-label={`移除备选模型 ${index + 1}`} title="从备选顺序中移除" onClick={() => onModel({ fallbacks: model.fallbacks.filter(item => item !== id) })}><X size={16} /></button>
        </div>)}
        <label className="field"><span id="model-fallback-label">添加备选模型</span><select aria-labelledby="model-fallback-label" value="" onChange={event => { if (event.target.value) onModel({ fallbacks: [...model.fallbacks, event.target.value] }); }}>
          <option value="">选择模型</option>{draft.models.filter(item => item.id !== model.id && !model.fallbacks.includes(item.id)).map(item => <option key={item.id} value={item.id} disabled={!canFallback(model.id, item.id, draft.models)}>{item.label || item.id}</option>)}
        </select></label>
      </div>
    </details>
    <dialog className="modal catalog-dialog" ref={dialog} onCancel={() => setRemoving(null)} aria-labelledby="delete-model-title">
      <button type="button" className="icon-button modal-close" aria-label="取消删除模型" onClick={() => setRemoving(null)}><X size={18} /></button>
      <h2 id="delete-model-title">删除“{removing?.label}”？</h2>
      <p>保存后移除此模型及密钥，同时清除其识图、章节整理和备选关联。聊天记录不会删除，其他模型保持不变。</p>
      <div className="form-actions"><button type="button" className="secondary" onClick={() => setRemoving(null)}>取消</button>
        <button type="button" className="primary" disabled={blocked || !removing} onClick={() => { if (removing) onRemove(removing.id); setRemoving(null); }}><Trash2 size={16} />确认删除模型</button></div>
    </dialog>
  </section>;
}
