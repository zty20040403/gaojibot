import type { PlatformSelection } from './api';
export const legacyPlatforms: PlatformSelection = { onebot: true, qq_official: false };
export function PlatformOptions({ value = legacyPlatforms, onChange, disabled = false }: {
  value?: PlatformSelection; onChange: (value: PlatformSelection) => void; disabled?: boolean;
}) {
  const choice = value.onebot ? value.qq_official ? 'dual' : 'onebot' : 'official';
  return <fieldset className="advanced" disabled={disabled}><legend>机器人平台</legend>
    <label className="field"><span>接入方式</span><select value={choice} onChange={event => onChange({ onebot: event.target.value !== 'official', qq_official: event.target.value !== 'onebot' })}>
      <option value="onebot">QQ 个人号（OneBot / NapCat）</option><option value="official">QQ 官方机器人</option><option value="dual">同时使用两个平台</option>
    </select></label>
    <p className="muted">{choice === 'official' ? '使用 QQ 开放平台的 AppID 和 AppSecret，无需扫码，不安装 NapCat。' : choice === 'dual' ? '两个平台分别认证与授权；不会自动合并用户、群聊或历史。' : '使用独立 QQ 个人号扫码登录，保留现有接入方式。'}</p>
  </fieldset>;
}
