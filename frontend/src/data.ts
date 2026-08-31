import type { ApiState, CoachSummary, Status, TimelineRow } from './types'

export const NAV_ITEMS = [
  { id: 'overview', label: '总览', caption: 'TODAY / COACH', glyph: '◈' },
  { id: 'timeline', label: '行情时间线', caption: 'MARKET TIMELINE', glyph: '⌁' },
  { id: 'transmission', label: '宏观传导', caption: 'TRANSMISSION', glyph: '⇢' },
  { id: 'holdings', label: '持仓与成交', caption: 'HOLDINGS / TRADES', glyph: '▣' },
  { id: 'advice', label: '建议与执行', caption: 'ADVICE / EXECUTION', glyph: '✧' },
  { id: 'narrative', label: '长期叙事', caption: 'CONTINUOUS STORY', glyph: '◌' },
  { id: 'review', label: '复盘与成长', caption: 'REVIEW / MEMORY', glyph: '↻' },
  { id: 'sources', label: '数据来源', caption: 'PROVENANCE / HEALTH', glyph: '▤' },
  { id: 'settings', label: '通知设置', caption: 'LOCAL QQBOT / APPEARANCE', glyph: '⚙' },
] as const

export type RouteId = (typeof NAV_ITEMS)[number]['id']

const ROUTE_ALIASES: Record<string, RouteId> = {
  factors: 'transmission',
  strategy: 'advice',
  evidence: 'sources',
}

export function routeFromHash(hash: string): RouteId {
  const candidate = hash.replace(/^#\/?/, '').split('/')[0]
  if (ROUTE_ALIASES[candidate]) return ROUTE_ALIASES[candidate]
  return NAV_ITEMS.some((item) => item.id === candidate) ? (candidate as RouteId) : 'overview'
}

export function statusLabel(status: string | null | undefined): string {
  const labels: Record<string, string> = {
    READY: '正常', ACTIVE: '有效', STALE: '过期', MISSING: '缺失', CONFLICT: '冲突', UNKNOWN: '未知',
    COMPLETE: '证据完整', PARTIAL: '部分可用', INCOMPLETE: '不完整', PENDING_USER_CONFIRMATION: '待确认', CONFIRMED: '已确认',
    ACCOUNT_PENDING_CONFIRMATION: '账户待确认', NONE: '无清仓级风险', WATCH: '风险观察', UNKNOWN_DATA: '风险事实不足', CONFIRMED_MAJOR_RISK: '重大风险已确认',
    NOT_CONFIGURED: '未配置', CREDENTIALS_CONFIGURED: '凭据已配置', WAITING_TARGET_BINDING: '等待绑定 QQ 好友', CONFIGURED_PENDING_CALL: '已配置，待真实调用', NOT_CALLED: '尚未调用', DELIVERED: '已送达', FAILED: '失败', NOT_REQUESTED: '未请求', PROVIDER_ERROR: '提供器失败', INVALID_RESPONSE: '结构化结果无效', AI_STRUCTURED_OUTPUT_INVALID: 'AI 返回内容不是约定的结构化 JSON', AI_TIMEOUT: 'AI 请求超时', TIMEOUT: '超时',
  }
  if (status?.startsWith('AI_NETWORK_ERROR:')) return 'AI 网络暂时不可用'
  if (status?.startsWith('AI_HTTP_STATUS:')) return 'AI 服务返回 HTTP 错误'
  return labels[status || ''] || status || '未知'
}

export function statusTone(status: string | null | undefined): 'ready' | 'risk' | 'missing' | 'neutral' {
  if (status === 'READY' || status === 'ACTIVE' || status === 'COMPLETE' || status === 'CONFIRMED') return 'ready'
  if (status === 'STALE' || status === 'CONFLICT' || status === 'WATCH' || status === 'CONFIRMED_MAJOR_RISK') return 'risk'
  if (status === 'MISSING' || status === 'INCOMPLETE' || status === 'UNKNOWN_DATA' || status === 'PENDING_USER_CONFIRMATION' || status === 'ACCOUNT_PENDING_CONFIRMATION' || status === 'WAITING_TARGET_BINDING' || status === 'NOT_CONFIGURED') return 'missing'
  return 'neutral'
}

export function formatNumber(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: digits, minimumFractionDigits: digits }).format(value)
}

export function formatPrice(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 4 }).format(value)
}

export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return `${(value * 100).toFixed(2)}%`
}

export function formatLatency(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return `${value.toFixed(value % 1 === 0 ? 0 : 1)} ms`
}

export function formatTimestamp(value: string | null | undefined): string {
  if (!value) return '未记录'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  const date = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(parsed).replaceAll('/', '-')
  const time = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(parsed)
  return `${date} ${time}`
}

export function apiStateLabel(state: ApiState): string {
  return { loading: '连接中', connected: '只读已连接', offline: '等待本地 API' }[state]
}

export const EMPTY_SUMMARY: CoachSummary = {
  schema_version: 'quant_lab_trade_coach_v0.1', product: 'Personal Trade Coach v0.1', read_only: true, manual_execution_only: true, automatic_trading: false, generated_at: '',
  account: { status: 'PENDING_USER_CONFIRMATION', confirmed: null, candidate: null, history: [], financials: { status: 'PENDING_USER_CONFIRMATION', reason_codes: ['ACCOUNT_NOT_CONFIRMED'], current_price: null, market_value: null, cost_basis: null, unrealized_pnl: null, realized_pnl: null, cash_estimate: null, equity_estimate: null, planned_cash_out: null } },
  market_regime: { code: 'UNKNOWN', label: '未知（证据不足）', confidence: 'LOW', score: null, metrics: {}, available_symbols: [], missing_symbols: [], evidence_status: 'MISSING', rule: '' },
  stock_state: { code: 'DATA_INSUFFICIENT', label: '数据不足', evidence_status: 'MISSING', own: { status: 'MISSING', direction: null, return_20d: null, volatility: null, sample_size: 0, reason_codes: [] }, silver: { status: 'MISSING', direction: null, return_20d: null, volatility: null, sample_size: 0, reason_codes: [] }, sector: { status: 'MISSING', direction: null, return_20d: null, volatility: null, sample_size: 0, reason_codes: [] }, reason_codes: [] },
  risk: { status: 'UNKNOWN_DATA', label: '风险事实不足', company_risk_confirmed: false, systemic_risk_confirmed: false, exit_allowed: false, rule: '', vps: { status: 'MISSING', source_ref: null, observed_at: null, generated_at: null, valid_until: null, risk_level: null, prediction_gate_status: 'MISSING', macro_event_gate: null, reason_codes: [], payload: {} } },
  advice: { schema_version: 'quant_lab_trade_coach_v0.1', as_of: '', generated_at: '', symbol: '000426.XSHE', market_regime: 'UNKNOWN', market_regime_label: '未知', stock_state: 'DATA_INSUFFICIENT', stock_state_label: '数据不足', current_shares: null, recommended_share_range: [null, null], action: 'WAIT', step_size: 100, trigger_conditions: [], invalidation_conditions: [], major_risk_status: 'UNKNOWN_DATA', major_risk_label: '风险事实不足', major_risk_evidence: { status: 'MISSING', source_ref: null, observed_at: null, generated_at: null, valid_until: null, risk_level: null, prediction_gate_status: 'MISSING', macro_event_gate: null, reason_codes: [], payload: {} }, confidence: 'LOW', evidence_status: 'MISSING', supporting_evidence: [], opposing_evidence: [], mentor_chain: [], manual_confirmation_required: true, automatic_trading: false },
  narrative: { schema_version: 'quant_lab_trade_coach_v0.1', as_of: '', generated_at: '', prior_narrative_id: null, original_judgement: null, summary: '等待事实层数据。', regime: 'UNKNOWN', regime_label: '未知', stock_state: 'DATA_INSUFFICIENT', stock_state_label: '数据不足', unchanged: [], new_facts: [], affirmed: [], falsified: [], position_adjustment: [null, null], evidence_status: 'MISSING', reasoning_kind: 'DETERMINISTIC_RULES', ai_override_allowed: false },
  mentor_chain: [], deterministic_mentor_chain: [], ai: { schema_version: 'quant_lab_ai_mentor_v1', provider: 'multi_provider', selected_provider: null, status: 'NOT_CONFIGURED', configured: false, model: null, model_source: null, protocol: null, base_url: null, timeout_seconds: 18, is_ai: false, fail_closed: true, reason_codes: ['AI_PROVIDER_NOT_CONFIGURED'], providers: [], fallback_order: ['deepseek', 'mimo'], latest_run: null, memory_retrieval: 'LOCAL_TOKEN_OVERLAP', explanation_only: true, cannot_override_rule_action: true }, notification: { schema_version: 'quant_lab_notification_v1', adapter: 'webhook', status: 'NOT_CONFIGURED', configured: false, target_host: null, reason_codes: ['NOTIFICATION_TARGET_UNSET'], delivery_audit: [] }, instruments: [], events: [], diary: [], memories: [], trades: [], vps: { status: 'MISSING', source_ref: null, observed_at: null, generated_at: null, valid_until: null, risk_level: null, prediction_gate_status: 'MISSING', macro_event_gate: null, reason_codes: [], payload: {} }, refresh: null, auto_refresh: { enabled: true, running: false, timezone: 'Asia/Shanghai', points: ['09:35', '11:25', '13:35', '14:50', '15:10'], status: 'UNKNOWN', reason_codes: ['A_SHARE_TRADING_CALENDAR_UNAVAILABLE'], calendar_source_ref: null, last_auto_refresh: null, next_planned_at: null, next_plan_calendar_source_ref: null, missed_slots_are_backfilled: false, automatic_trading: false }, capabilities: { broker_connection: false, automatic_order: false, deterministic_rule_engine: true, optional_ai_explanation: false, notification_adapter: true },
}

export async function loadCoachSummary(): Promise<CoachSummary> {
  const response = await fetch('/api/trade-coach/summary', { cache: 'no-store' })
  if (!response.ok) throw new Error(`API ${response.status}`)
  return (await response.json()) as CoachSummary
}

export async function postCoach<T>(route: string, payload: Record<string, unknown> = {}): Promise<T> {
  const response = await fetch(`/api/trade-coach/${route.replace(/^\//, '')}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
  const value = await response.json() as T & { message?: string }
  if (!response.ok) throw new Error(value.message || `API ${response.status}`)
  return value
}

export async function loadTimeline(symbol: string, limit = 120): Promise<{ symbol: string; label: string; rows: TimelineRow[] }> {
  const response = await fetch(`/api/trade-coach/timeline?symbol=${encodeURIComponent(symbol)}&limit=${limit}`, { cache: 'no-store' })
  if (!response.ok) throw new Error(`API ${response.status}`)
  return (await response.json()) as { symbol: string; label: string; rows: TimelineRow[] }
}
