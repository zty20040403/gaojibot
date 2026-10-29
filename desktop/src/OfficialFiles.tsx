import { useState } from 'react';
import type { OfficialFileSettings, OfficialMultipartPolicy, OfficialRouteEvidence } from './api';

export const fileDefaults: OfficialFileSettings = { enabled: false, generation_enabled: false, native_routes: [], download: null };

function EvidenceImport({ count, onImport, title }: { count: number; onImport: (value: OfficialRouteEvidence[]) => void; title: string }) {
  const [pending, setPending] = useState<OfficialRouteEvidence[] | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState('');
  async function read(file: File | undefined) {
    setError(''); setPending(null); setConfirmed(false);
    if (!file) return;
    try {
      if (file.size > 262144) throw new Error();
      const value: unknown = JSON.parse(await file.text());
      if (!Array.isArray(value) || value.length > 128 || value.some(row => !row || typeof row !== 'object' || Array.isArray(row))) throw new Error();
      setPending(value as OfficialRouteEvidence[]);
    } catch { setError('证据文件格式无效或超过大小限制。尚未修改配置。'); }
  }
  return <details><summary>{title} · 已保存 {count} 条</summary>
    <p className="muted">仅导入受保护的真实目标验收记录 JSON 数组，保留完整账号、端点、版本与原时间。近期发言和 HTTPS 自检不等于目标文件验收。页面不会生成证据。</p>
    <input type="file" accept="application/json,.json" onChange={event => { void read(event.target.files?.[0]); event.target.value = ''; }} />
    {error && <p role="alert">{error}</p>}
    {pending && <><p>待导入 {pending.length} 条记录；保存时还会核对完整账号、严格字段和协议版本。</p>
      <label className="checkbox"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />这些记录来自该账号与目标的实际验收，未修改原时间或版本</label>
      <button type="button" disabled={!confirmed} onClick={() => { onImport(pending); setPending(null); setConfirmed(false); }}>应用到待保存配置</button></>}
    {count > 0 && <button type="button" onClick={() => onImport([])}>清除这些发送能力证据</button>}
  </details>;
}

function MultipartImport({ value, onImport, onClear }: { value?: OfficialMultipartPolicy; onImport: (value: OfficialMultipartPolicy) => void; onClear: () => void }) {
  const [pending, setPending] = useState<OfficialMultipartPolicy | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState('');
  async function read(file: File | undefined) {
    setError(''); setPending(null); setConfirmed(false);
    if (!file) return;
    try {
      if (file.size > 16384) throw new Error();
      const candidate: unknown = JSON.parse(await file.text());
      if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) throw new Error();
      const record = candidate as Record<string, unknown>;
      if (Object.keys(record).some(key => !['allowed_part_hosts', 'max_parts', 'max_session_seconds'].includes(key)) ||
        !Array.isArray(record.allowed_part_hosts) || record.allowed_part_hosts.length < 1 || record.allowed_part_hosts.length > 16 ||
        record.allowed_part_hosts.some(host => typeof host !== 'string') ||
        ('max_parts' in record && (!Number.isInteger(record.max_parts) || Number(record.max_parts) < 1 || Number(record.max_parts) > 128)) ||
        ('max_session_seconds' in record && (!Number.isInteger(record.max_session_seconds) || Number(record.max_session_seconds) < 1 || Number(record.max_session_seconds) > 600))) throw new Error();
      setPending(candidate as OfficialMultipartPolicy);
    } catch { setError('上传主机配置无效或超过限制。尚未修改配置。'); }
  }
  return <details><summary>分片上传主机配置 · {value ? `已保存 ${value.allowed_part_hosts.length} 个主机` : '未配置'}</summary>
    <p className="muted">仅导入经明确验证的上传主机 JSON 配置；主机必须为精确小写 DNS 名称，不能是网址、IP 或通配符。页面不会从上传返回值添加信任。</p>
    <input type="file" accept="application/json,.json" onChange={event => { void read(event.target.files?.[0]); event.target.value = ''; }} />
    {error && <p role="alert">{error}</p>}
    {pending && <><p>待导入 {pending.allowed_part_hosts.length} 个主机；保存时还会严格核对字段、域名与范围。</p>
      <label className="checkbox"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />这些主机来自实际上传契约和明确验收记录</label>
      <button type="button" disabled={!confirmed} onClick={() => { onImport(pending); setPending(null); setConfirmed(false); }}>应用到待保存配置</button></>}
    {value && <button type="button" onClick={onClear}>清除分片上传主机配置</button>}
    <p className="muted">默认关闭。还需目标能力记录明确包含 multipart=true，且仅限普通文件；旧证据不会自动升级，图片不使用分片。文件大小仍受原证据和 20 MiB 上限限制。保存不代表已通过目标验收。</p>
  </details>;
}

export function OfficialFiles({ value, onChange }: { value?: OfficialFileSettings; onChange: (value: OfficialFileSettings) => void }) {
  const settings = value || fileDefaults;
  const update = (patch: Partial<OfficialFileSettings>) => onChange({ ...settings, ...patch });
  return <details className="advanced"><summary>文件出口与受保护取件</summary>
    <label className="checkbox"><input type="checkbox" checked={settings.enabled} onChange={event => update({ enabled: event.target.checked })} />启用官方文件保留与受保护控制台取件</label>
    <p className="muted">默认关闭。启用后使用实例持久存储中的独立私有目录，仍检查源任务、绑定、期限与文件哈希。没有出口证据时不会承诺可在 QQ 收到文件。</p>
    {settings.enabled && <>
      <label className="checkbox"><input type="checkbox" checked={settings.generation_enabled} onChange={event => update({ generation_enabled: event.target.checked })} />允许已授权任务生成文件（仍受任务与沙盒额度限制）</label>
      <EvidenceImport count={settings.native_routes.length} title="原生图片 / 文件能力记录" onImport={native_routes => update({ native_routes })} />
      <MultipartImport value={settings.multipart} onImport={multipart => update({ multipart })} onClear={() => { const next = { ...settings }; delete next.multipart; onChange(next); }} />
      <label className="checkbox"><input type="checkbox" checked={settings.download !== null} onChange={event => update({ download: event.target.checked ? { origin: '', listen_host: '127.0.0.1', listen_port: 18081, routes: [] } : null })} />配置独立受控下载服务</label>
      {settings.download && <>
        <p className="notice warning">需要已部署的可信 HTTPS 反向代理与有效证书，且代理与机器人共享容器网络命名空间。宿主普通端口映射不能访问容器回环监听。App 不部署公开代理，也不自动开放任何端口。</p>
        <label className="field"><span>HTTPS 来源地址（443，无路径或尾斜杠）</span><input type="url" placeholder="https://downloads.example.com" value={settings.download.origin} onChange={event => update({ download: { ...settings.download!, origin: event.target.value } })} autoComplete="off" spellCheck={false} /></label>
        <div className="field-grid"><label className="field"><span>容器回环监听</span><select value={settings.download.listen_host} onChange={event => update({ download: { ...settings.download!, listen_host: event.target.value as '127.0.0.1' | '::1' } })}><option value="127.0.0.1">127.0.0.1</option><option value="::1">::1</option></select></label>
          <label className="field"><span>固定内部端口</span><input type="number" min={1024} max={65535} step={1} value={settings.download.listen_port} onChange={event => update({ download: { ...settings.download!, listen_port: Number(event.target.value) } })} /></label></div>
        <EvidenceImport count={settings.download.routes.length} title="下载目标验收记录" onImport={routes => update({ download: { ...settings.download!, routes } })} />
        <p className="muted">保存不代表可用。运行时分别显示监听与 HTTPS 自检；QQ 域名政策和收件人网络可达仍需真实目标验收。空记录不会授权发送链接；过期记录不会自动续期。</p>
      </>}
    </>}
  </details>;
}
