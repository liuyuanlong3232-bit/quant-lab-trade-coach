import { describe, expect, it } from 'vitest'
import { EMPTY_SUMMARY, formatTimestamp, routeFromHash, statusTone } from './data'

describe('Quant-Lab frontend display contracts', () => {
  it('keeps automatic refresh non-trading and fail-closed by default', () => {
    expect(EMPTY_SUMMARY.auto_refresh.automatic_trading).toBe(false)
    expect(EMPTY_SUMMARY.auto_refresh.missed_slots_are_backfilled).toBe(false)
    expect(EMPTY_SUMMARY.auto_refresh.reason_codes).toContain('A_SHARE_TRADING_CALENDAR_UNAVAILABLE')
  })

  it('keeps route navigation within the complete coach chapters', () => {
    expect(routeFromHash('#/overview')).toBe('overview')
    expect(routeFromHash('#/evidence')).toBe('sources')
    expect(routeFromHash('#/settings')).toBe('settings')
    expect(routeFromHash('#/unknown')).toBe('overview')
  })

  it('does not turn missing or risky probe evidence into green', () => {
    expect(statusTone('MISSING')).toBe('missing')
    expect(statusTone('CONFLICT')).toBe('risk')
    expect(statusTone('READY')).toBe('ready')
  })

  it('formats a timestamp for a narrow surface without changing its day or time', () => {
    expect(formatTimestamp('2026-08-24T16:17:19.312026+08:00')).toContain('2026-08-24 16:17:19')
    expect(formatTimestamp(null)).toBe('未记录')
  })
})
