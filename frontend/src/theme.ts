export type WallpaperPreset = 'black' | 'none' | 'custom'
export type ThemeConfig = {
  wallpaper: WallpaperPreset
  positionX: number
  positionY: number
  scale: number
  brightness: number
  contrast: number
  blur: number
  overlayDarkness: number
  titleFont: 'display' | 'serif' | 'sans'
  bodyFont: 'sans' | 'system' | 'serif'
  numberFont: 'mono' | 'display' | 'sans'
  fontScale: number
  hudOpacity: number
  accent: string
  customFontFamily?: string
}

export const DEFAULT_THEME: ThemeConfig = {
  wallpaper: 'black', positionX: 50, positionY: 50, scale: 100,
  brightness: 100, contrast: 100, blur: 0, overlayDarkness: 42,
  titleFont: 'sans', bodyFont: 'sans', numberFont: 'mono',
  fontScale: 100, hudOpacity: 92, accent: '#35e08b',
}

// v2 establishes the black-and-green terminal as the new visual baseline while
// leaving a user's older v1 theme untouched and still importable as JSON.
const THEME_KEY = 'quant-lab-theme-v2'
const DB_NAME = 'quant-lab-assets-v1'
const STORE_NAME = 'assets'
const FONT_EXTENSIONS = ['.woff', '.woff2', '.ttf', '.otf']
const IMAGE_EXTENSIONS = ['.png', '.jpg', '.jpeg', '.webp']
const IMAGE_MIME_TYPES = ['image/png', 'image/jpeg', 'image/webp']
const THEME_JSON_MAX_BYTES = 256 * 1024

function clamp(value: unknown, min: number, max: number, fallback: number) {
  const parsed = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(parsed) ? Math.min(max, Math.max(min, parsed)) : fallback
}

function enumValue<T extends string>(value: unknown, allowed: readonly T[], fallback: T): T {
  return typeof value === 'string' && allowed.includes(value as T) ? value as T : fallback
}

export function validateTheme(input: unknown): ThemeConfig {
  const source = input && typeof input === 'object' ? input as Partial<ThemeConfig> : {}
  const accent = typeof source.accent === 'string' && /^#[\da-f]{6}$/i.test(source.accent) ? source.accent : DEFAULT_THEME.accent
  return {
    // Older exports may contain the removed mineral preset; enumValue safely falls back to black.
    wallpaper: enumValue(source.wallpaper, ['black', 'none', 'custom'], DEFAULT_THEME.wallpaper),
    positionX: clamp(source.positionX, 0, 100, DEFAULT_THEME.positionX),
    positionY: clamp(source.positionY, 0, 100, DEFAULT_THEME.positionY),
    scale: clamp(source.scale, 100, 180, DEFAULT_THEME.scale),
    brightness: clamp(source.brightness, 30, 160, DEFAULT_THEME.brightness),
    contrast: clamp(source.contrast, 40, 180, DEFAULT_THEME.contrast),
    blur: clamp(source.blur, 0, 12, DEFAULT_THEME.blur),
    overlayDarkness: clamp(source.overlayDarkness, 0, 85, DEFAULT_THEME.overlayDarkness),
    titleFont: enumValue(source.titleFont, ['display', 'serif', 'sans'], DEFAULT_THEME.titleFont),
    bodyFont: enumValue(source.bodyFont, ['sans', 'system', 'serif'], DEFAULT_THEME.bodyFont),
    numberFont: enumValue(source.numberFont, ['mono', 'display', 'sans'], DEFAULT_THEME.numberFont),
    fontScale: clamp(source.fontScale, 85, 125, DEFAULT_THEME.fontScale),
    hudOpacity: clamp(source.hudOpacity, 35, 100, DEFAULT_THEME.hudOpacity),
    accent,
    ...(sanitizeFontFamily(source.customFontFamily) ? { customFontFamily: sanitizeFontFamily(source.customFontFamily) } : {}),
  }
}

export function sanitizeFontFamily(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined
  const clean = value.trim().slice(0, 80)
  return /^[\p{L}\p{N}][\p{L}\p{N} _-]*$/u.test(clean) ? clean : undefined
}

export function loadTheme(): ThemeConfig {
  try { return validateTheme(JSON.parse(localStorage.getItem(THEME_KEY) || '{}')) } catch { return { ...DEFAULT_THEME } }
}

export function saveTheme(theme: ThemeConfig) {
  localStorage.setItem(THEME_KEY, JSON.stringify(validateTheme(theme)))
}

export function exportTheme(theme: ThemeConfig): string {
  return JSON.stringify({ schema: 'quant_lab_theme_v1', exported_at: new Date().toISOString(), theme: validateTheme(theme), assets: { wallpaper: theme.wallpaper === 'custom' ? 'local-only: reselect after import' : 'none', font: theme.customFontFamily ? 'local-only: reselect after import' : 'none' } }, null, 2)
}

export function importTheme(text: string): ThemeConfig {
  const parsed: unknown = JSON.parse(text)
  if (!parsed || typeof parsed !== 'object') throw new Error('主题文件必须是 JSON 对象')
  const payload = parsed as { schema?: unknown; theme?: unknown }
  if (payload.schema !== 'quant_lab_theme_v1' || !payload.theme || typeof payload.theme !== 'object') throw new Error('不是 Quant-Lab 主题文件')
  return validateTheme(payload.theme)
}

export function validateThemeJsonFile(file: File): string | null {
  if (!file.name.toLowerCase().endsWith('.json') || (file.type && !['application/json', 'text/json'].includes(file.type))) return '请选择 JSON 格式的主题文件'
  if (file.size > THEME_JSON_MAX_BYTES) return '主题 JSON 不能超过 256KB'
  return null
}

function extensionOf(file: File) { const name = file.name.toLowerCase(); return IMAGE_EXTENSIONS.concat(FONT_EXTENSIONS).find((extension) => name.endsWith(extension)) || '' }
export function validateAsset(file: File, kind: 'wallpaper' | 'font'): string | null {
  const extension = extensionOf(file)
  const allowed = kind === 'wallpaper' ? IMAGE_EXTENSIONS : FONT_EXTENSIONS
  const limit = kind === 'wallpaper' ? 10 * 1024 * 1024 : 5 * 1024 * 1024
  const validMime = kind === 'wallpaper' ? IMAGE_MIME_TYPES.includes(file.type) : ['font/woff', 'font/woff2', 'font/ttf', 'font/otf', 'application/font-woff', 'application/octet-stream'].includes(file.type)
  if (!allowed.includes(extension) || !validMime) return kind === 'wallpaper' ? '只支持 PNG、JPG、JPEG 或 WEBP 图片' : '只支持 WOFF、WOFF2、TTF 或 OTF 字体'
  if (file.size > limit) return kind === 'wallpaper' ? '图片不能超过 10MB' : '字体不能超过 5MB'
  return null
}

export async function validateImageFile(file: File): Promise<string | null> {
  const basicError = validateAsset(file, 'wallpaper')
  if (basicError) return basicError
  try {
    if ('createImageBitmap' in window) {
      const bitmap = await createImageBitmap(file)
      const valid = bitmap.width > 0 && bitmap.height > 0
      bitmap.close()
      return valid ? null : '图片尺寸无效'
    }
    const url = URL.createObjectURL(file)
    try { await new Promise<void>((resolve, reject) => { const image = new Image(); image.onload = () => resolve(); image.onerror = () => reject(new Error('decode')); image.src = url }) } finally { URL.revokeObjectURL(url) }
    return null
  } catch { return '图片无法解码，请选择有效的 PNG、JPG、JPEG 或 WEBP 文件' }
}

function openAssetDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1)
    request.onupgradeneeded = () => request.result.createObjectStore(STORE_NAME)
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error || new Error('本地资产存储不可用'))
  })
}

export async function saveAsset(kind: 'wallpaper' | 'font', file: File) {
  const db = await openAssetDb()
  await new Promise<void>((resolve, reject) => { const tx = db.transaction(STORE_NAME, 'readwrite'); tx.objectStore(STORE_NAME).put(file, kind); tx.oncomplete = () => resolve(); tx.onerror = () => reject(tx.error); tx.onabort = () => reject(tx.error || new Error('本地资产事务已中止')) })
  db.close()
}

export async function loadAsset(kind: 'wallpaper' | 'font'): Promise<Blob | null> {
  try {
    const db = await openAssetDb()
    const result = await new Promise<Blob | null>((resolve, reject) => { const request = db.transaction(STORE_NAME, 'readonly').objectStore(STORE_NAME).get(kind); request.onsuccess = () => resolve(request.result || null); request.onerror = () => reject(request.error) })
    db.close(); return result
  } catch { return null }
}

export function applyTheme(theme: ThemeConfig, wallpaperUrl: string | null) {
  const root = document.documentElement
  const wallpaper = theme.wallpaper === 'custom' && wallpaperUrl ? `url("${wallpaperUrl}")` : 'none'
  const titleFamilies = { display: "'Libre Baskerville', 'Songti SC', serif", serif: "'Songti SC', Georgia, serif", sans: "'Noto Sans SC', 'Microsoft YaHei', sans-serif" }
  const bodyFamilies = { sans: "'Noto Sans SC', 'Microsoft YaHei', sans-serif", system: "system-ui, sans-serif", serif: "'Songti SC', Georgia, serif" }
  const numberFamilies = { mono: "'DM Mono', Consolas, monospace", display: "'Libre Baskerville', serif", sans: "'Noto Sans SC', sans-serif" }
  root.style.setProperty('--theme-wallpaper-image', wallpaper)
  root.style.setProperty('--theme-wallpaper-position', `${theme.positionX}% ${theme.positionY}%`)
  root.style.setProperty('--theme-wallpaper-scale', `${theme.scale / 100}`)
  root.style.setProperty('--theme-wallpaper-filter', `brightness(${theme.brightness}%) contrast(${theme.contrast}%) blur(${theme.blur}px)`)
  root.style.setProperty('--theme-overlay-darkness', `${theme.overlayDarkness / 100}`)
  root.style.setProperty('--theme-font-scale', `${theme.fontScale / 100}`)
  root.style.setProperty('--theme-hud-opacity', `${theme.hudOpacity / 100}`)
  root.style.setProperty('--theme-accent', theme.accent)
  root.style.setProperty('--accent', theme.accent)
  root.style.setProperty('--theme-title-font', theme.customFontFamily ? `'${theme.customFontFamily}', ${titleFamilies[theme.titleFont]}` : titleFamilies[theme.titleFont])
  root.style.setProperty('--theme-body-font', theme.customFontFamily ? `'${theme.customFontFamily}', ${bodyFamilies[theme.bodyFont]}` : bodyFamilies[theme.bodyFont])
  root.style.setProperty('--theme-number-font', numberFamilies[theme.numberFont])
}
