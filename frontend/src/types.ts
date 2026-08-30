export type Status = 'READY' | 'STALE' | 'MISSING' | 'CONFLICT' | 'UNKNOWN' | 'ACTIVE' | 'INCOMPLETE' | 'PENDING_USER_CONFIRMATION' | 'CONFIRMED'

export type SourceState = {
  source: string | null
  status: Status | string
  close: number | null
  exchange_time: string | null
  observed_at: string | null
  latency_ms: number | null
  reason_codes: string[]
  source_ref?: string | null
  mapping_version?: string | null
  contract_mapping?: Record<string, unknown>
}

export type CoachInstrument = {
  symbol: string
  label: string
  asset_class: string
  venue: string
  source: string
  provider_symbol: string
  contract_semantics: string
  primary_source: string
  backup_source: string
  freshness_hours: number
  primary: SourceState
  backup: SourceState
  sources: SourceState[]
  latest_probe?: SourceState
  fallback?: SourceState | null
  selected: SourceState
  reconciliation_status: string
}

export type RegimeMetric = {
  status: string
  direction: number | null
  return_20d: number | null
  volatility: number | null
  sample_size: number
  last?: number
  ma20?: number
  reason_codes: string[]
}

export type MarketRegime = {
  code: string
  label: string
  confidence: string
  score: number | null
  metrics: Record<string, RegimeMetric>
  available_symbols: string[]
  missing_symbols: string[]
  evidence_status: string
  rule: string
}

export type StockState = {
  code: string
  label: string
  evidence_status: string
  own: RegimeMetric
  silver: RegimeMetric
  sector: RegimeMetric
  reason_codes: string[]
}

export type VpsFacts = {
  status: string
  source_ref: string | null
  observed_at: string | null
  generated_at: string | null
  valid_until: string | null
  risk_level: string | null
  prediction_gate_status: string
  macro_event_gate: string | null
  reason_codes: string[]
  payload: Record<string, unknown>
}

export type RiskState = {
  status: string
  label: string
  company_risk_confirmed: boolean
  systemic_risk_confirmed: boolean
  exit_allowed: boolean
  rule: string
  vps: VpsFacts
}

export type MentorStep = { step: string; title: string; text: string; reasoning_kind?: string }

export type AiRun = {
  id?: number
  request_hash?: string
  provider?: string
  model?: string | null
  status: string
  started_at?: string
  completed_at?: string
  memory_ids?: number[]
  verification?: Record<string, unknown>
  response_hash?: string | null
  result?: Record<string, unknown>
  error_code?: string | null
}

export type AiStatus = {
  schema_version: string
  provider: string
  status: string
  configured: boolean
  model: string | null
  model_source?: string | null
  protocol?: string | null
  selected_provider?: string | null
  base_url?: string | null
  timeout_seconds?: number
  is_ai: boolean
  latest_call_status?: string
  last_call_succeeded?: boolean | null
  fail_closed?: boolean
  reason_codes: string[]
  providers?: Array<Record<string, unknown>>
  fallback_order?: string[]
  latest_run: AiRun | null
  memory_retrieval: string
  explanation_only: boolean
  cannot_override_rule_action: boolean
}

export type NotificationDelivery = {
  id?: number
  event_id?: number | null
  adapter?: string
  status: string
  attempted_at?: string
  response_code?: number | null
  error_code?: string | null
  payload_hash?: string
}

export type NotificationStatus = {
  schema_version: string
  adapter: string
  status: string
  configured: boolean
  target_host?: string | null
  reason_codes: string[]
  delivery_audit: NotificationDelivery[]
}

export type AiMentorResult = {
  schema_version?: string
  provider?: string
  selected_provider?: string | null
  model?: string | null
  status: string
  is_ai: boolean
  fail_closed: boolean
  reason_codes: string[]
  structured_output?: {
    schema_version?: string
    summary: string
    drivers: string[]
    risks: string[]
    counter_evidence: string[]
    questions: string[]
    source_references: string[]
    confidence: string
    uncertainty: string
    rule_action_reference: string
  } | null
  ai_run_id?: number
  deterministic_advice?: Advice
  memory_retrieval?: { query: string; count: number; memory_ids: number[] }
  cross_validation?: Record<string, unknown>
  fallback_attempts?: Array<Record<string, unknown>>
}

export type Advice = {
  schema_version: string
  as_of: string
  generated_at: string
  symbol: string
  market_regime: string
  market_regime_label: string
  stock_state: string
  stock_state_label: string
  current_shares: number | null
  recommended_share_range: [number | null, number | null]
  action: 'HOLD' | 'ADD_IN_STEPS' | 'REDUCE_IN_STEPS' | 'WAIT' | 'EXIT_MAJOR_RISK' | string
  step_size: number
  trigger_conditions: string[]
  invalidation_conditions: string[]
  major_risk_status: string
  major_risk_label: string
  major_risk_evidence: VpsFacts
  confidence: string
  evidence_status: string
  supporting_evidence: string[]
  opposing_evidence: string[]
  mentor_chain: MentorStep[]
  reasoning_kind?: string
  ai_override_allowed?: boolean
  manual_confirmation_required: boolean
  automatic_trading: boolean
  narrative_id?: number
}

export type Narrative = {
  schema_version: string
  as_of: string
  generated_at: string
  prior_narrative_id: number | null
  original_judgement: string | null
  summary: string
  regime: string
  regime_label: string
  stock_state: string
  stock_state_label: string
  unchanged: string[]
  new_facts: string[]
  affirmed: string[]
  falsified: string[]
  position_adjustment: [number | null, number | null]
  evidence_status: string
  reasoning_kind?: string
  ai_override_allowed?: boolean
}

export type AccountSnapshot = {
  id: number
  account_id: string
  captured_at: string
  status: 'PENDING_USER_CONFIRMATION' | 'CONFIRMED'
  shares: number | null
  avg_cost: number | null
  available_cash: number | null
  total_assets: number | null
  planned_cash_out: number | null
  source: string
  confirmation_note: string | null
  is_current_fact: boolean
}

export type AccountState = {
  status: 'PENDING_USER_CONFIRMATION' | 'CONFIRMED'
  confirmed: AccountSnapshot | null
  candidate: AccountSnapshot | null
  history: AccountSnapshot[]
  financials: AccountFinancials
}

export type AccountFinancials = {
  status: string
  reason_codes: string[]
  current_price: number | null
  market_value: number | null
  cost_basis: number | null
  unrealized_pnl: number | null
  realized_pnl: number | null
  cash_estimate: number | null
  equity_estimate: number | null
  open_shares?: number
  planned_cash_out: number | null
  source?: string | null
  price_exchange_time?: string | null
}

export type CoachEvent = {
  id: number
  event_key: string
  event_type: string
  first_seen: string
  last_seen: string
  last_notified: string | null
  status: string
  is_new_notification?: boolean
  payload: Record<string, unknown>
}

export type DiaryRecord = {
  id: number
  layer: string
  event_time: string
  content: Record<string, unknown>
  prev_hash: string | null
  record_hash: string
  recorded_at: string
}

export type CoachSummary = {
  schema_version: string
  product: string
  read_only: boolean
  manual_execution_only: boolean
  automatic_trading: boolean
  generated_at: string
  account: AccountState
  market_regime: MarketRegime
  stock_state: StockState
  risk: RiskState
  advice: Advice
  narrative: Narrative
  mentor_chain: MentorStep[]
  deterministic_mentor_chain?: MentorStep[]
  ai: AiStatus
  notification: NotificationStatus
  instruments: CoachInstrument[]
  events: CoachEvent[]
  diary: DiaryRecord[]
  memories: Array<Record<string, unknown>>
  trades: Array<Record<string, unknown>>
  vps: VpsFacts
  refresh: Record<string, unknown> | null
  capabilities: Record<string, boolean>
}

export type TimelineRow = Record<string, unknown> & {
  exchange_time: string | null
  observed_at: string
  close: number | null
  adjusted_close: number | null
  status: string
  source: string
}

export type ApiState = 'loading' | 'connected' | 'offline'

// Kept for the older read-only diagnostic page/components.  The new coach
// pages use the richer contracts above, while the legacy page remains a
// supported local evidence view.
export type ProbeStatus = 'READY' | 'STALE' | 'MISSING' | 'CONFLICT' | 'PENDING' | 'UNKNOWN'
export type ProbeSource = { source: string | null; status: ProbeStatus; close: number | null; exchange_time: string | null; latency_ms: number | null; reason_codes: string[] }
export type Instrument = { symbol: string; label: string; asset_class: string | null; primary: ProbeSource; backup: ProbeSource; selected: ProbeSource & { price_deviation_bps: number | null } }
