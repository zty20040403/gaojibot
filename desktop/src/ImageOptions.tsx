import type { ImageOptions as Options } from './api';

export function ImageOptions({ value, onChange, disabled }: { value: Options; onChange: (value: Options) => void; disabled: boolean }) {
  return <div className="field-grid">
    <label className="field"><span>运行环境获取方式</span><select disabled={disabled} value={value.mode} onChange={event => onChange({ ...value, mode: event.target.value as Options['mode'] })}>
      <option value="auto">预构建优先，没有时源码构建</option><option value="prebuilt">只用预构建镜像</option><option value="source">源码构建</option>
    </select></label>
    <label className="field"><span>源码构建 · Debian 下载源</span><select disabled={disabled || value.mode === 'prebuilt'} value={value.source} onChange={event => onChange({ ...value, source: event.target.value as Options['source'] })}>
      <option value="official">官方 HTTPS</option><option value="tuna">清华镜像 HTTPS</option>
    </select></label>
  </div>;
}
