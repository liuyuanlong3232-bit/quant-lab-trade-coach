import { useRef, useState, type ChangeEvent } from 'react'
import { DEFAULT_THEME, exportTheme, importTheme, loadAsset, saveAsset, saveTheme, validateAsset, validateImageFile, validateTheme, validateThemeJsonFile, type ThemeConfig } from './theme'

export function ThemeSettings({ theme, onChange, onAssetChanged }: { theme: ThemeConfig; onChange: (theme: ThemeConfig) => void; onAssetChanged: () => void }) {
  const [message, setMessage] = useState('')
  const imageRef = useRef<HTMLInputElement>(null)
  const fontRef = useRef<HTMLInputElement>(null)
  const update = (patch: Partial<ThemeConfig>) => { const next = validateTheme({ ...theme, ...patch }); onChange(next); saveTheme(next) }
  const upload = async (kind: 'wallpaper' | 'font', file?: File) => {
    if (!file) return
    const error = kind === 'wallpaper' ? await validateImageFile(file) : validateAsset(file, kind)
    if (error) { setMessage(error); return }
    try {
      await saveAsset(kind, file)
      if (kind === 'wallpaper') { update({ wallpaper: 'custom' }); onAssetChanged() }
      else {
        const blob = await loadAsset('font')
        if (!blob || !('FontFace' in window)) throw new Error('当前浏览器不支持本地字体预览')
        const family = `QuantLabCustom${Date.now()}`
        const url = URL.createObjectURL(blob)
        try { const face = new FontFace(family, `url(${url})`); await face.load(); document.fonts.add(face); update({ customFontFamily: family }); setMessage('字体已载入本机预览') } finally { URL.revokeObjectURL(url) }
      }
      setMessage(kind === 'wallpaper' ? '壁纸已保存到本机' : '字体已保存到本机')
    } catch { setMessage('本地资产保存失败，请重试') }
  }
  const onImport = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]; if (!file) return
    const fileError = validateThemeJsonFile(file); if (fileError) { setMessage(fileError); event.target.value = ''; return }
    const reader = new FileReader(); reader.onload = () => { try { const next = importTheme(String(reader.result)); onChange(next); saveTheme(next); setMessage('主题配置已导入；自定义图片/字体需重新选择') } catch (error) { setMessage(error instanceof Error ? error.message : '主题 JSON 无法导入') } }; reader.readAsText(file); event.target.value = ''
  }
  return <section className="theme-settings" aria-label="外观设置">
    <div className="page-intro"><span className="hero-intro__eyebrow">APPEARANCE · LOCAL ONLY</span><h2>外观</h2><p>只保存到这台电脑，不联网、不上传、不触碰行情与策略接口。</p></div>
    <div className="settings-grid">
      <div className="settings-section"><h3>壁纸预设</h3><div className="theme-choices">{([['black', '纯黑'], ['none', '无图'], ['custom', '本地图片']] as const).map(([value, label]) => <button key={value} className={theme.wallpaper === value ? 'is-selected' : ''} onClick={() => update({ wallpaper: value })}>{label}</button>)}</div><button className="settings-upload" onClick={() => imageRef.current?.click()}>上传本地图片</button><input ref={imageRef} hidden type="file" accept=".png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp" onChange={(event) => void upload('wallpaper', event.target.files?.[0])} /><div className="settings-sliders"><Slider label="位置 X" value={theme.positionX} min={0} max={100} unit="%" onChange={(value) => update({ positionX: value })} /><Slider label="位置 Y" value={theme.positionY} min={0} max={100} unit="%" onChange={(value) => update({ positionY: value })} /><Slider label="缩放" value={theme.scale} min={100} max={180} unit="%" onChange={(value) => update({ scale: value })} /><Slider label="亮度" value={theme.brightness} min={30} max={160} unit="%" onChange={(value) => update({ brightness: value })} /><Slider label="对比度" value={theme.contrast} min={40} max={180} unit="%" onChange={(value) => update({ contrast: value })} /><Slider label="模糊" value={theme.blur} min={0} max={12} unit="px" onChange={(value) => update({ blur: value })} /><Slider label="遮罩深度" value={theme.overlayDarkness} min={0} max={85} unit="%" onChange={(value) => update({ overlayDarkness: value })} /></div></div>
      <div className="settings-section"><h3>文字与 HUD</h3><Select label="标题字体" value={theme.titleFont} options={[['display', '书卷衬线'], ['serif', '古典衬线'], ['sans', '现代无衬线']]} onChange={(value) => update({ titleFont: value as ThemeConfig['titleFont'] })} /><Select label="正文字体" value={theme.bodyFont} options={[['sans', '中文无衬线'], ['system', '系统字体'], ['serif', '中文衬线']]} onChange={(value) => update({ bodyFont: value as ThemeConfig['bodyFont'] })} /><Select label="数字字体" value={theme.numberFont} options={[['mono', '等宽数字'], ['display', '衬线数字'], ['sans', '无衬线数字']]} onChange={(value) => update({ numberFont: value as ThemeConfig['numberFont'] })} /><Slider label="字号倍率" value={theme.fontScale} min={85} max={125} unit="%" onChange={(value) => update({ fontScale: value })} /><Slider label="HUD 透明度" value={theme.hudOpacity} min={35} max={100} unit="%" onChange={(value) => update({ hudOpacity: value })} /><label className="settings-color"><span>强调色</span><input type="color" value={theme.accent} onChange={(event) => update({ accent: event.target.value })} /><code>{theme.accent}</code></label><button className="settings-upload" onClick={() => fontRef.current?.click()}>上传本地字体（WOFF / TTF）</button><input ref={fontRef} hidden type="file" accept=".woff,.woff2,.ttf,.otf,font/woff,font/woff2,font/ttf,font/otf" onChange={(event) => void upload('font', event.target.files?.[0])} /><small className="settings-hint">图片 ≤10MB，字体 ≤5MB。自定义字体仅保存在本机。</small></div>
    </div>
    <div className="settings-actions"><button onClick={() => { onChange({ ...DEFAULT_THEME }); saveTheme(DEFAULT_THEME); setMessage('已恢复默认外观') }}>恢复默认</button><button onClick={() => { const blob = new Blob([exportTheme(theme)], { type: 'application/json' }); const url = URL.createObjectURL(blob); const anchor = document.createElement('a'); anchor.href = url; anchor.download = 'quant-lab-theme.json'; anchor.click(); window.setTimeout(() => URL.revokeObjectURL(url), 0); setMessage('主题配置已导出；自定义资产不包含在 JSON 中') }}>导出主题 JSON</button><label className="settings-import">导入主题 JSON<input type="file" accept="application/json,.json" onChange={onImport} /></label>{message && <span className="settings-message" role="status">{message}</span>}</div>
  </section>
}

function Slider({ label, value, min, max, unit, onChange }: { label: string; value: number; min: number; max: number; unit: string; onChange: (value: number) => void }) { return <label className="settings-slider"><span>{label}</span><input type="range" min={min} max={max} value={value} onChange={(event) => onChange(Number(event.target.value))} /><output>{value}{unit}</output></label> }
function Select({ label, value, options, onChange }: { label: string; value: string; options: readonly (readonly [string, string])[]; onChange: (value: string) => void }) { return <label className="settings-select"><span>{label}</span><select value={value} onChange={(event) => onChange(event.target.value)}>{options.map(([option, label]) => <option key={option} value={option}>{label}</option>)}</select></label> }
