import { useRef } from 'react';
import { Check, Flower2, Leaf, Monitor, Moon, Palette as PaletteIcon, Pencil, Sun, Waves, X } from 'lucide-react';
import type { Palette, Theme } from './preferences';

const palettes = [
  { id: 'forest', label: '森林', subtitle: '薄荷 / 丁香', Icon: Leaf },
  { id: 'ocean', label: '海盐', subtitle: '湖蓝 / 珊瑚', Icon: Waves },
  { id: 'blossom', label: '花信', subtitle: '玫瑰 / 叶绿', Icon: Flower2 },
  { id: 'graphite', label: '石墨', subtitle: '墨黑 / 荧光黄', Icon: Pencil },
] as const;

export function Appearance({ theme, onTheme, palette, onPalette }: {
  theme: Theme; onTheme: (theme: Theme) => void; palette: Palette; onPalette: (palette: Palette) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const currentPalette = palettes.find(item => item.id === palette) || palettes[0];
  const ThemeIcon = currentPalette.Icon;
  return <><button className="appearance-trigger" title="外观设置" aria-label="外观设置" aria-haspopup="dialog" onClick={() => dialog.current?.showModal()}>
    <PaletteIcon size={19} /><span>主题<span className="appearance-current"> · {currentPalette.label}</span></span>
    <span className="theme-ink-dots" aria-hidden="true"><i /><i /><i /></span>
  </button>
    <dialog ref={dialog} className="modal appearance-dialog" aria-labelledby="appearance-title">
      <button className="icon-button modal-close" title="关闭外观设置" aria-label="关闭外观设置" onClick={() => dialog.current?.close()}><X size={18} /></button>
      <div className="appearance-heading"><img src="./brand.png" alt="" /><div><div className="eyebrow">GAOJI / SKETCHBOOK</div><h2 id="appearance-title">主题工坊<ThemeIcon size={25} aria-hidden="true" /></h2></div></div>
      <fieldset className="palette-field"><legend>配色主题</legend><div className="palette-options" role="radiogroup" aria-label="配色主题">
        {palettes.map(({ id, label, subtitle }) =>
          <button type="button" role="radio" key={id} aria-label={label} title={label} aria-checked={palette === id} className="palette-choice" onClick={() => onPalette(id)}>
            <span className="palette-preview" data-palette={id} aria-hidden="true">
              <span className="preview-rail"><i /><i /><i /></span>
              <span className="preview-page"><span className="preview-lines"><i /><i /></span><span className="preview-button" /></span>
              <span className="palette-check">{palette === id && <Check size={14} />}</span>
            </span>
            <span className="palette-caption"><strong>{label}</strong><span>{subtitle}</span></span>
          </button>)}
      </div></fieldset>
      <fieldset><legend>深浅模式</legend><div className="theme-options" role="radiogroup" aria-label="深浅模式">
        {([{ id: 'light', label: '浅色', Icon: Sun }, { id: 'dark', label: '深色', Icon: Moon }, { id: 'system', label: '跟随系统', Icon: Monitor }] as const).map(({ id, label, Icon }) =>
          <button type="button" role="radio" aria-checked={theme === id} key={id} onClick={() => onTheme(id)}><Icon size={21} /><span>{label}</span></button>)}
      </div></fieldset>
      <div className="appearance-footer"><span className="theme-ink-dots" aria-hidden="true"><i /><i /><i /></span><button className="primary" onClick={() => dialog.current?.close()}><Check size={16} />完成</button></div>
    </dialog></>;
}
