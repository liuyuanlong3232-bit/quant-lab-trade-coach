import { describe, expect, it } from 'vitest'
import { DEFAULT_THEME, exportTheme, importTheme, sanitizeFontFamily, validateAsset, validateTheme, validateThemeJsonFile } from './theme'

describe('theme configuration', () => {
  it('clamps unsafe ranges and rejects invalid colors', () => {
    const theme = validateTheme({ scale: 999, blur: -4, fontScale: 3, accent: 'red' })
    expect(theme.scale).toBe(180)
    expect(theme.blur).toBe(0)
    expect(theme.fontScale).toBe(85)
    expect(theme.accent).toBe(DEFAULT_THEME.accent)
  })

  it('falls back removed mineral wallpapers to the black baseline', () => {
    expect(validateTheme({ wallpaper: 'mineral' }).wallpaper).toBe('black')
  })

  it('round-trips config without embedding binary assets', () => {
    const text = exportTheme({ ...DEFAULT_THEME, wallpaper: 'custom', customFontFamily: 'QuantLabCustom' })
    const imported = importTheme(text)
    expect(imported.wallpaper).toBe('custom')
    expect(text).toContain('local-only')
    expect(text.includes('data:')).toBe(false)
  })

  it('rejects unsupported asset types and oversized files', () => {
    expect(validateAsset(new File(['x'], 'wallpaper.gif', { type: 'image/gif' }), 'wallpaper')).toContain('只支持')
    expect(validateAsset(new File([new Uint8Array(6 * 1024 * 1024)], 'font.woff2', { type: 'font/woff2' }), 'font')).toContain('5MB')
  })

  it('rejects malformed theme JSON', () => {
    expect(() => importTheme('{"nope":true}')).toThrow('不是 Quant-Lab 主题文件')
  })

  it('sanitizes font family names before they reach CSS', () => {
    expect(sanitizeFontFamily('银锡 Display_01')).toBe('银锡 Display_01')
    expect(sanitizeFontFamily('safe; color:red')).toBeUndefined()
    expect(validateTheme({ customFontFamily: 'bad(quote)' }).customFontFamily).toBeUndefined()
  })

  it('validates the imported JSON file envelope', () => {
    expect(validateThemeJsonFile(new File(['{}'], 'theme.txt', { type: 'application/json' }))).toContain('JSON')
    expect(validateThemeJsonFile(new File([new Uint8Array(257 * 1024)], 'theme.json', { type: 'application/json' }))).toContain('256KB')
  })
})
