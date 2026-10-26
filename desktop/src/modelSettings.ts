import type { InstanceSettings, ModelProfile } from './api';

export function newModel(id = 'm_' + crypto.randomUUID().replaceAll('-', '').slice(0, 16)): ModelProfile {
  return { id, label: id === 'primary' ? '默认模型' : '新模型', provider: id === 'primary' ? 'deepseek' : 'custom', protocol: 'openai-chat',
    base_url: id === 'primary' ? 'https://api.deepseek.com/v1' : '', model: id === 'primary' ? 'deepseek-chat' : '',
    api_key_required: true, tools: true, vision: false, thinking: 'auto', reasoning_effort: '', max_input_tokens: 0, max_output_tokens: 0, fallbacks: [] };
}

export function withCatalog(settings: InstanceSettings): InstanceSettings {
  return settings.models.length ? settings : { ...settings, models: [newModel('primary')], default_model: 'primary' };
}

export function cleanProfile({ has_key: _privateFlag, ...profile }: ModelProfile): ModelProfile { return profile; }

export function keyFor(model: ModelProfile, changes: Record<string, string>): string | null {
  return Object.hasOwn(changes, model.id) ? changes[model.id] : model.has_key ? null : '';
}

export function modelPurpose(name: string): 'chat' | 'image' | 'review' {
  if (/^gpt-image(?:-|$)/i.test(name)) return 'image';
  return name === 'codex-auto-review' ? 'review' : 'chat';
}

export function discoveredProfiles(source: ModelProfile, names: string[], existing: ModelProfile[]): ModelProfile[] {
  const seen = new Set(existing.filter(item => item.base_url === source.base_url && item.protocol === source.protocol).map(item => item.model));
  return [...new Set(names)].filter(name => modelPurpose(name) === 'chat' && !seen.has(name)).map(name => ({
    ...newModel(), provider: source.provider, protocol: source.protocol, base_url: source.base_url,
    api_key_required: source.api_key_required, label: name, model: name,
  }));
}

export function canFallback(source: string, target: string, profiles: ModelProfile[]): boolean {
  const visited = new Set<string>();
  const reach = (name: string): boolean => {
    if (name === source) return true;
    if (visited.has(name)) return false;
    visited.add(name);
    return profiles.find(item => item.id === name)?.fallbacks.some(reach) || false;
  };
  return !reach(target);
}

export function validateCatalog(settings: InstanceSettings, changes: Record<string, string>): void {
  if (!settings.models.length || !settings.models.some(item => item.id === settings.default_model)) throw new Error('请先选择默认聊天模型');
  for (const model of settings.models) {
    if (!model.label.trim() || !model.base_url.trim() || !model.model.trim()) throw new Error(`请补全“${model.label || model.id}”的名称和接口地址`);
    const key = keyFor(model, changes);
    if (model.api_key_required && key !== null && !key.trim()) throw new Error(`请填写“${model.label}”的 API Key；更换地址或接口后需要重新填写`);
    if (model.fallbacks.some(target => !settings.models.some(item => item.id === target) || !canFallback(model.id, target, settings.models))) {
      throw new Error('模型降级顺序不能互相绕回，请调整备选模型');
    }
  }
}

export function removeModel(settings: InstanceSettings, identifier: string): InstanceSettings {
  if (settings.default_model === identifier) throw new Error('请先更换默认聊天模型，再删除此模型');
  return { ...settings, models: settings.models.filter(item => item.id !== identifier).map(item => ({ ...item, fallbacks: item.fallbacks.filter(id => id !== identifier) })),
    vision_model: settings.vision_model === identifier ? '' : settings.vision_model,
    ...(settings.components ? { components: { ...settings.components,
      historian: { profile: settings.components.historian.profile === identifier ? '' : settings.components.historian.profile } } } : {}),
  };
}
