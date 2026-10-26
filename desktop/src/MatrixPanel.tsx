import { Play, Plus, Trash2 } from 'lucide-react';
import type { ComponentSettings } from './api';

export function MatrixPanel({ options, keyValue, keySaved, onChange, onKey, onTest, result }: {
  options: ComponentSettings['matrix']; keyValue: string; keySaved: boolean;
  onChange: (options: Partial<ComponentSettings['matrix']>) => void; onKey: (key: string) => void;
  onTest: () => void; result: string;
}) {
  function routeChange(id: string, field: 'qq_group' | 'room_id', value: string) {
    onChange({ routes: options.routes.map(route => route.id === id ? { ...route, [field]: value } : route) });
  }
  return <div className="component-options matrix-options"><h4>Matrix 消息互通</h4>
    <label className="field"><span>Matrix 服务地址</span><input type="url" maxLength={2048} value={options.base_url} placeholder="https://你的 Matrix 服务器" onChange={event => {
      onChange({ base_url: event.target.value }); onKey('');
    }} /></label>
    <label className="field"><span>Matrix 用户 ID</span><input maxLength={512} value={options.user_id} placeholder="@bot:example.org" onChange={event => {
      onChange({ user_id: event.target.value }); onKey('');
    }} /></label>
    <label className="field"><span>Matrix Access Token {keySaved ? '（已保存在服务器）' : ''}</span><input type="password" autoComplete="off" maxLength={8192} value={keyValue}
      placeholder={keySaved ? '留空保持原密钥' : '填写机器人账户的 Access Token'} onChange={event => onKey(event.target.value)} /></label>
    <div className="matrix-mappings">{options.routes.map((route, index) => <div className="matrix-mapping" key={route.id}>
      <div className="field-grid"><label className="field"><span>QQ 群号 {index + 1}</span><input inputMode="numeric" maxLength={15} value={route.qq_group} onChange={event => routeChange(route.id, 'qq_group', event.target.value)} /></label>
        <label className="field"><span>Matrix 房间 ID {index + 1}</span><input maxLength={512} placeholder="!room:example.org" value={route.room_id} onChange={event => routeChange(route.id, 'room_id', event.target.value)} /></label></div>
      <button type="button" className="secondary icon-button" title={`移除会话映射 ${index + 1}`} aria-label={`移除会话映射 ${index + 1}`}
        onClick={() => onChange({ routes: options.routes.filter(item => item.id !== route.id) })}><Trash2 size={16} /></button>
    </div>)}</div>
    <div className="model-test"><button type="button" className="secondary" disabled={options.routes.length >= 20}
      onClick={() => onChange({ routes: [...options.routes, { id: crypto.randomUUID().replaceAll('-', ''), qq_group: '', room_id: '' }] })}><Plus size={16} />添加会话映射</button>
      <button type="button" className="secondary" onClick={onTest}><Play size={16} />检测 Matrix</button><span role="status">{result}</span></div>
  </div>;
}
