import { OfficialFiles } from './OfficialFiles';
import type { OfficialConfiguration as ConfigurationStatus, OfficialSettings } from './api';
export const officialDefaults: OfficialSettings = {
  environment: 'production', private_policy: 'owner_only', owner_openids: [], private_allowed_openids: [],
  daily_calls: 100, daily_input_tokens: 500000, daily_output_tokens: 100000, exclusive_deployment_confirmed: false,
};
export type OfficialCredentials = { app_id: string; app_secret: string };
export function OfficialConfiguration({ settings, credentials, saved, onChange, onCredentials }: {
  settings: OfficialSettings; credentials: OfficialCredentials; saved?: ConfigurationStatus;
  onChange: (value: OfficialSettings) => void; onCredentials: (value: OfficialCredentials) => void;
}) {
  const field = <K extends keyof OfficialSettings>(key: K, value: OfficialSettings[K]) => onChange({ ...settings, [key]: value });
  return <section className="official-config"><h3>QQ 官方凭据</h3>
    <p className="muted">保存后不回显凭据。留空保留已保存的 AppID 与 AppSecret；更换时必须同时填写。</p>
    <div className="field-grid"><label className="field"><span>AppID {saved?.credentials_saved ? '· 已保存' : ''}</span>
      <input value={credentials.app_id} autoComplete="off" spellCheck={false} maxLength={256} onChange={event => onCredentials({ ...credentials, app_id: event.target.value })} /></label>
      <label className="field"><span>AppSecret</span><input type="password" value={credentials.app_secret} autoComplete="new-password" maxLength={4096} onChange={event => onCredentials({ ...credentials, app_secret: event.target.value })} /></label></div>
    <label className="field"><span>环境</span><select value={settings.environment} onChange={event => field('environment', event.target.value as OfficialSettings['environment'])}><option value="production">正式环境</option><option value="sandbox">沙箱环境</option></select></label>
    <p className="muted">按账号认证状态与平台实际提示检查：体验名单、IP 白名单、公开服务与群主授权都是条件项，不是所有账号都必须完成。公开服务在平台后台管理；此处的私聊准入与成本限制单独生效。</p>
    <div className="notice warning" role="note">同一个 AppID 在另一台服务器或另一数据库运行时，本地连接锁无法阻止双开。启用前先停止旧部署。{saved?.same_app_local_instances ? ` 检测到本机另有 ${saved.same_app_local_instances} 个相同账号配置。` : ''}</div>
    <label className="checkbox"><input type="checkbox" checked={settings.exclusive_deployment_confirmed} onChange={event => field('exclusive_deployment_confirmed', event.target.checked)} />我已确认该 AppID 的旧部署已停止</label>
    <details className="advanced"><summary>准入与成本限制</summary>
      <label className="field"><span>私聊准入</span><select value={settings.private_policy} onChange={event => field('private_policy', event.target.value as OfficialSettings['private_policy'])}><option value="owner_only">仅已配置的所有者</option><option value="allowlist">指定用户</option><option value="public">允许平台可达用户（仍受用量限制）</option></select></label>
      <label className="field"><span>所有者 C2C OpenID（每行一个）</span><textarea value={settings.owner_openids.join('\n')} rows={2} onChange={event => field('owner_openids', event.target.value.split('\n').filter(Boolean))} /></label>
      <p className="muted">必须使用此 AppID、此环境下的真实 C2C 身份。群成员 OpenID、QQ 号和控制台密码账户都不能代替。控制台管理员关联还需要所有者私聊确认。</p>
      {settings.private_policy === 'allowlist' && <label className="field"><span>可私聊的 C2C OpenID（每行一个）</span><textarea value={settings.private_allowed_openids.join('\n')} rows={2} onChange={event => field('private_allowed_openids', event.target.value.split('\n').filter(Boolean))} /></label>}
      <div className="field-grid">{([['daily_calls', '每日调用次数'], ['daily_input_tokens', '每日输入 Token'], ['daily_output_tokens', '每日输出 Token']] as const).map(([key, label]) => <label className="field" key={key}><span>{label}（每会话）</span><input type="number" min={1} max={key === 'daily_calls' ? 100000 : 100000000} step={1} value={settings[key]} onChange={event => field(key, Number(event.target.value))} required /></label>)}</div>
      <p className="muted">群聊保持逐群双向绑定，填写数字群号不会授权官方群。公开准入不会取消每日成本限制。</p>
    </details>
    <OfficialFiles value={settings.files} onChange={files => field('files', files)} />
  </section>;
}
