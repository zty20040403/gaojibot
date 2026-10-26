import { Play, Plus, Trash2 } from 'lucide-react';
import type { ComponentSettings } from './api';

export function IMessagePanel({ options, keyValue, keySaved, onChange, onKey, onTest, result }: {
  options: ComponentSettings['imessage']; keyValue: string; keySaved: boolean;
  onChange: (options: Partial<ComponentSettings['imessage']>) => void; onKey: (key: string) => void;
  onTest: () => void; result: string;
}) {
  function routeChange(id: string, field: 'qq_group' | 'chat_guid', value: string) {
    onChange({ routes: options.routes.map(route => route.id === id ? { ...route, [field]: value } : route) });
  }
  return <div className="component-options imessage-options"><h4>iMessage 消息互通</h4>
    <label className="field"><span>BlueBubbles 地址（你自己的 Mac）</span><input type="url" maxLength={2048} value={options.base_url} placeholder="https://你的 BlueBubbles 服务" onChange={event => {
      onChange({ base_url: event.target.value }); onKey('');
    }} /></label>
    <label className="field"><span>iMessage 自身标识</span><input maxLength={512} value={options.bot_handle} placeholder="Mac 信息 App 使用的邮箱或手机号" onChange={event => {
      onChange({ bot_handle: event.target.value }); onKey('');
    }} /></label>
    <label className="field"><span>BlueBubbles 密码 {keySaved ? '（已保存在服务器）' : ''}</span><input type="password" autoComplete="off" maxLength={8192} value={keyValue}
      placeholder={keySaved ? '留空保持原密码' : 'BlueBubbles 服务的 API 密码'} onChange={event => onKey(event.target.value)} /></label>
    <div className="matrix-mappings">{options.routes.map((route, index) => <div className="imessage-mapping" key={route.id}>
      <div className="field-grid"><label className="field"><span>iMessage 对应 QQ 群号 {index + 1}</span><input inputMode="numeric" maxLength={15} value={route.qq_group} onChange={event => routeChange(route.id, 'qq_group', event.target.value)} /></label>
        <label className="field"><span>iMessage 会话 GUID {index + 1}</span><input maxLength={512} placeholder="iMessage;+;chat..." value={route.chat_guid} onChange={event => routeChange(route.id, 'chat_guid', event.target.value)} /></label></div>
      <button type="button" className="secondary icon-button" title={`移除 iMessage 映射 ${index + 1}`} aria-label={`移除 iMessage 映射 ${index + 1}`}
        onClick={() => onChange({ routes: options.routes.filter(item => item.id !== route.id) })}><Trash2 size={16} /></button>
    </div>)}</div>
    <div className="model-test"><button type="button" className="secondary" disabled={options.routes.length >= 20}
      onClick={() => onChange({ routes: [...options.routes, { id: crypto.randomUUID().replaceAll('-', ''), qq_group: '', chat_guid: '' }] })}><Plus size={16} />添加 iMessage 映射</button>
      <button type="button" className="secondary" onClick={onTest}><Play size={16} />检测 iMessage</button><span role="status">{result}</span></div>
  </div>;
}
