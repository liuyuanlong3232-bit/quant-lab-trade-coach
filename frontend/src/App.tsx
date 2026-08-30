import { useEffect, useMemo, useState } from 'react'
import { apiStateLabel, EMPTY_SUMMARY, formatLatency, formatNumber, formatPercent, formatPrice, formatTimestamp, loadCoachSummary, loadTimeline, NAV_ITEMS, postCoach, routeFromHash, statusLabel, statusTone, type RouteId } from './data'
import { ThemeSettings } from './ThemeSettings'
import { QQBotSettings } from './QQBotSettings'
import { applyTheme, loadAsset, loadTheme, saveTheme, type ThemeConfig } from './theme'
import type { AccountSnapshot, AiMentorResult, ApiState, CoachEvent, CoachInstrument, CoachSummary, DiaryRecord, MentorStep, NotificationStatus, TimelineRow } from './types'

function App() {
  const [route, setRoute] = useState<RouteId>(() => routeFromHash(window.location.hash))
  const [summary, setSummary] = useState<CoachSummary>(EMPTY_SUMMARY)
  const [apiState, setApiState] = useState<ApiState>('loading')
  const [refreshing, setRefreshing] = useState(false)
  const [notice, setNotice] = useState('')
  const [theme, setTheme] = useState<ThemeConfig>(() => loadTheme())
  const [wallpaperUrl, setWallpaperUrl] = useState<string | null>(null)
  const [assetRevision, setAssetRevision] = useState(0)

  useEffect(() => {
    let active = true
    let objectUrl: string | null = null
    if (theme.wallpaper === 'custom') void loadAsset('wallpaper').then((blob) => { if (!active || !blob) return; objectUrl = URL.createObjectURL(blob); setWallpaperUrl(objectUrl) })
    return () => { active = false; if (objectUrl) URL.revokeObjectURL(objectUrl); setWallpaperUrl(null) }
  }, [theme.wallpaper, assetRevision])
  useEffect(() => { applyTheme(theme, wallpaperUrl); saveTheme(theme) }, [theme, wallpaperUrl])
  useEffect(() => { const onHash = () => setRoute(routeFromHash(window.location.hash)); window.addEventListener('hashchange', onHash); return () => window.removeEventListener('hashchange', onHash) }, [])

  const load = async () => {
    try { const next = await loadCoachSummary(); setSummary(next); setApiState('connected') } catch { setApiState('offline') }
  }
  useEffect(() => { void load(); const timer = window.setInterval(() => void load(), 10_000); return () => window.clearInterval(timer) }, [])
  const refresh = async () => {
    setRefreshing(true); setNotice('正在向公开数据源请求真实行情；缺失或过期结果会原样保留。')
    try { await postCoach('refresh', { include_live: true }); await load(); setNotice('真实行情刷新完成；请查看数据来源页核对时点与来源。') } catch (error) { setNotice(error instanceof Error ? error.message : '刷新失败，系统保持原有证据状态。') } finally { setRefreshing(false) }
  }
  const title = NAV_ITEMS.find((item) => item.id === route)?.label || '总览'
  return <div className="app-shell">
    <Sidebar route={route} />
    <main className="app-main">
      <Topbar title={title} apiState={apiState} refreshing={refreshing} onRefresh={() => void refresh()} />
      {notice && <div className="global-notice" role="status">{notice}<button onClick={() => setNotice('')} aria-label="关闭提示">×</button></div>}
      <div className="page-content">
        {route === 'overview' && <Overview summary={summary} />}
        {route === 'timeline' && <TimelinePage summary={summary} />}
        {route === 'transmission' && <TransmissionPage summary={summary} />}
        {route === 'holdings' && <HoldingsPage summary={summary} onUpdated={() => void load()} />}
        {route === 'advice' && <AdvicePage summary={summary} onUpdated={() => void load()} />}
        {route === 'narrative' && <NarrativePage summary={summary} onUpdated={() => void load()} />}
        {route === 'review' && <ReviewPage summary={summary} />}
        {route === 'sources' && <SourcesPage summary={summary} onUpdated={() => void load()} />}
        {route === 'settings' && <><ThemeSettings theme={theme} onChange={setTheme} onAssetChanged={() => setAssetRevision((value) => value + 1)} /><QQBotSettings /></>}
      </div>
      <footer className="app-footer"><span>PERSONAL TRADE COACH / LOCALHOST ONLY</span><span>真实事实 → 确定性状态 → 导师解释 → 人工确认</span><span>不接券商 · 不自动下单</span></footer>
    </main>
    <MobileNav route={route} />
  </div>
}

function Sidebar({ route }: { route: RouteId }) {
  return <aside className="sidebar"><div className="brand"><div className="brand__glyph">⌬</div><div><strong>Quant-Lab</strong><span>PERSONAL TRADE COACH</span></div></div><div className="sidebar__chapter"><span>导师工作台</span><i /></div><nav className="chapter-nav" aria-label="章节导航">{NAV_ITEMS.map((item) => <a key={item.id} className={route === item.id ? 'is-active' : ''} href={`#/${item.id}`}><span className="nav-glyph">{item.glyph}</span><span><b>{item.label}</b><small>{item.caption}</small></span></a>)}</nav><div className="sidebar__guard"><span className="guard-dot" />本地手动确认<br /><small>不连接券商 / 不自动交易</small></div></aside>
}

function MobileNav({ route }: { route: RouteId }) {
  return <nav className="mobile-nav" aria-label="移动端章节导航">{NAV_ITEMS.slice(0, 5).map((item) => <a key={item.id} className={route === item.id ? 'is-active' : ''} href={`#/${item.id}`}><span>{item.glyph}</span><small>{item.label}</small></a>)}</nav>
}

function Topbar({ title, apiState, refreshing, onRefresh }: { title: string; apiState: ApiState; refreshing: boolean; onRefresh: () => void }) {
  return <header className="topbar"><div><span className="topbar__crumb">QUANT-LAB / PERSONAL TRADE COACH v0.1</span><h1>{title}</h1></div><div className="topbar__actions"><div className="topbar__status"><span className={`live-pulse ${apiState === 'connected' ? 'is-live' : ''}`} />{apiStateLabel(apiState)}<span className="topbar__time">{new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date())}</span></div><button className="refresh-button" disabled={refreshing || apiState === 'offline'} onClick={onRefresh}>{refreshing ? '刷新中…' : '刷新真实行情'}</button></div></header>
}

function PageIntro({ eyebrow, title, children }: { eyebrow: string; title: string; children: React.ReactNode }) {
  return <section className="page-intro"><span className="eyebrow">{eyebrow}</span><h2>{title}</h2><p>{children}</p></section>
}

function Panel({ title, eyebrow, children, className = '' }: { title: string; eyebrow?: string; children: React.ReactNode; className?: string }) {
  return <section className={`panel ${className}`}><div className="panel__header">{eyebrow && <span className="panel__eyebrow">{eyebrow}</span>}<h3>{title}</h3></div>{children}</section>
}

function Metric({ label, value, detail, tone = 'neutral' }: { label: string; value: string; detail?: string; tone?: string }) {
  return <div className={`metric metric--${tone}`}><span>{label}</span><strong>{value}</strong>{detail && <small>{detail}</small>}</div>
}

function StatusBadge({ status }: { status: string | null | undefined }) {
  return <span className={`status-badge status-badge--${statusTone(status)}`}><i />{statusLabel(status)}</span>
}

function displayRange(range: [number | null, number | null] | undefined) {
  if (!range || range[0] === null || range[1] === null) return '待账户确认 / 证据补齐'
  return `${formatNumber(range[0], 0)} — ${formatNumber(range[1], 0)} 股`
}

function instrument(summary: CoachSummary, symbol: string): CoachInstrument | undefined { return summary.instruments.find((item) => item.symbol === symbol) }

function Overview({ summary }: { summary: CoachSummary }) {
  const advice = summary.advice
  const account = summary.account
  const ready = summary.instruments.filter((item) => item.selected.status === 'READY').length
  return <>
    <section className="hero-card"><div><span className="eyebrow">TODAY / MENTOR DESK</span><h2>先看事实，再决定动作。</h2><p>系统正式指导：<b>000426.XSHE · 兴业银锡</b>。目前所有操作都必须由本人手动确认，账户候选快照尚未自动生效。</p></div><div className="hero-card__stamp"><span>判断时点</span><b>{formatTimestamp(advice.generated_at)}</b><StatusBadge status={advice.evidence_status} /></div></section>
    {account.status === 'PENDING_USER_CONFIRMATION' && <div className="confirmation-banner"><div><strong>账户仍是候选快照</strong><p>计划值：600 股 · 成本 34.751 元 · 截图现金 9,720.25 元。请在“持仓与成交”页面重新核对并确认；未确认前不会生成正式持仓区间。</p></div><a href="#/holdings">去确认账户 →</a></div>}
    <div className="metric-row"><Metric label="大环境模式" value={summary.market_regime.label} detail={`${summary.market_regime.code} · 置信度 ${summary.market_regime.confidence}`} tone={statusTone(summary.market_regime.evidence_status)} /><Metric label="兴业银锡状态" value={summary.stock_state.label} detail={summary.stock_state.code} tone={statusTone(summary.stock_state.evidence_status)} /><Metric label="建议持仓区间" value={displayRange(advice.recommended_share_range)} detail={`动作：${actionLabel(advice.action)}`} tone={advice.action === 'EXIT_MAJOR_RISK' ? 'risk' : 'neutral'} /><Metric label="风险门槛" value={summary.risk.label} detail={summary.risk.exit_allowed ? '允许提出清仓（仍需人工确认）' : '不满足清仓条件'} tone={statusTone(summary.risk.status)} /><Metric label="可用行情证据" value={`${ready} / ${summary.instruments.length}`} detail={`整体：${statusLabel(summary.market_regime.evidence_status)}`} tone={ready ? 'ready' : 'missing'} /></div>
    <div className="overview-grid"><Panel title="最新导师建议" eyebrow="ACTION CARD" className="advice-panel"><AdviceCard summary={summary} compact /></Panel><Panel title="持续市场叙事" eyebrow="CONTINUOUS NARRATIVE" className="narrative-panel"><NarrativeCard summary={summary} /></Panel></div>
    <div className="overview-grid overview-grid--lower"><Panel title="今天真正发生的事" eyebrow="EVENTS / DEDUPED"><EventList events={summary.events.slice(0, 5)} /></Panel><Panel title="白银 → 板块 → 个股" eyebrow="TRANSMISSION"><MiniTransmission summary={summary} /></Panel></div>
    <Panel title="导师解释链" eyebrow="WHY THIS ACTION"><MentorChain steps={summary.mentor_chain} /></Panel>
  </>
}

function actionLabel(action: string) { return ({ HOLD: '持有', ADD_IN_STEPS: '分批加仓', REDUCE_IN_STEPS: '分批减仓', WAIT: '等待', EXIT_MAJOR_RISK: '重大风险清仓建议' } as Record<string, string>)[action] || action }

function AdviceCard({ summary, compact = false }: { summary: CoachSummary; compact?: boolean }) {
  const advice = summary.advice
  return <div className={`advice-card ${advice.action === 'EXIT_MAJOR_RISK' ? 'advice-card--risk' : ''}`}><div className="advice-card__top"><div><span className="symbol-label">{advice.symbol} · 兴业银锡</span><strong>{actionLabel(advice.action)}</strong><small className="reasoning-tag">确定性规则建议 · 不是 AI</small></div><StatusBadge status={advice.evidence_status} /></div><div className="advice-range"><span>建议持仓区间</span><b>{displayRange(advice.recommended_share_range)}</b><small>当前正式持仓：{advice.current_shares === null ? '待确认' : `${formatNumber(advice.current_shares, 0)} 股`} · 每批 {advice.step_size} 股</small></div><div className="advice-columns"><div><span>触发条件</span><ListItems items={advice.trigger_conditions} /></div><div><span>失效条件</span><ListItems items={advice.invalidation_conditions} /></div></div>{!compact && <><div className="evidence-columns"><div><span>支持证据</span><ListItems items={advice.supporting_evidence} /></div><div><span>反对证据</span><ListItems items={advice.opposing_evidence} /></div></div><p className="manual-note">这是待确认建议，不是订单。系统自动交易：否；券商连接：否。</p></>}</div>
}

function ListItems({ items }: { items: string[] }) { return items.length ? <ul>{items.map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}</ul> : <p className="muted">未记录</p> }

function NarrativeCard({ summary }: { summary: CoachSummary }) {
  const narrative = summary.narrative
  return <div className="narrative-card"><p className="narrative-summary">{narrative.summary}</p><div className="narrative-columns"><div><span>没有变化</span><ListItems items={narrative.unchanged} /></div><div><span>新事实</span><ListItems items={narrative.new_facts} /></div></div><a href="#/narrative">打开完整叙事 →</a></div>
}

function EventList({ events }: { events: CoachEvent[] }) {
  if (!events.length) return <div className="empty-state">尚无事件证据。缺失不会被制造成提醒。</div>
  return <div className="event-list">{events.map((event) => <div className="event-row" key={`${event.event_key}-${event.id}`}><div className={`event-mark event-mark--${event.event_type.includes('风险') ? 'risk' : 'info'}`} /><div><b>{event.event_type}</b><p>{eventText(event)}</p><small>{formatTimestamp(event.last_seen)} · {event.last_notified ? '已提醒一次' : '未提醒'}</small></div></div>)}</div>
}

function eventText(event: CoachEvent) {
  const payload = event.payload
  const change = String(payload.change_summary || '')
  const impact = String(payload.impact_on_stock || '')
  const action = `建议：${actionLabel(String(payload.action || 'WAIT'))}，区间 ${displayRange(payload.recommended_share_range as [number | null, number | null] | undefined)}。`
  if (event.event_key === 'market-regime') return `${change || `当前模式：${String(payload.label || payload.code || '未知')}`}。${impact} ${action}`
  if (event.event_key === 'data-health') return `${change || `数据完整性：${String(payload.status || '未知')}`}；缺失 ${Array.isArray(payload.missing) ? payload.missing.join('、') : '未记录'}。${action}`
  if (event.event_key === 'risk-state') return `${change || `风险事实：${String(payload.label || payload.status || '未知')}`}。${String(payload.risk_rule || '')} ${action}`
  if (event.event_key === 'stock-state') return `${change || `个股状态：${String(payload.label || payload.code || '未知')}`}。${impact} ${action}`
  return `${change || `建议动作：${actionLabel(String(payload.action || 'WAIT'))}`}。${impact} ${action}`
}

function MiniTransmission({ summary }: { summary: CoachSummary }) {
  const nodes = ['SILVER', 'DXY', 'REAL10Y', '801050.SI', '000426.XSHE']
  return <div className="mini-transmission">{nodes.map((symbol, index) => { const item = instrument(summary, symbol); return <div className="mini-node" key={symbol}><span className={`mini-node__dot mini-node__dot--${statusTone(item?.selected.status)}`} /><b>{item?.label || symbol}</b><small>{item ? formatPrice(item.selected.close) : '未记录'}</small>{index < nodes.length - 1 && <i>→</i>}</div> })}</div>
}

function MentorChain({ steps }: { steps: MentorStep[] }) {
  return <div className="mentor-chain">{steps.map((step, index) => <div className="mentor-step" key={`${step.step}-${index}`}><span className="mentor-step__index">0{index + 1}</span><div><b>{step.title}</b><small className="reasoning-tag">{step.reasoning_kind === 'AI_PROVIDER' ? 'AI_PROVIDER' : 'DETERMINISTIC_RULES'}</small><p>{step.text}</p></div></div>)}</div>
}

function TimelinePage({ summary }: { summary: CoachSummary }) {
  const [symbol, setSymbol] = useState('000426.XSHE')
  const [timeline, setTimeline] = useState<{ label: string; rows: TimelineRow[] } | null>(null)
  const [status, setStatus] = useState('loading')
  useEffect(() => { let active = true; setStatus('loading'); void loadTimeline(symbol).then((data) => { if (active) { setTimeline(data); setStatus('ready') } }).catch(() => { if (active) setStatus('offline') }); return () => { active = false } }, [symbol])
  const rows = timeline?.rows || []
  const values = rows.map((row) => row.close).filter((value): value is number => typeof value === 'number' && Number.isFinite(value))
  return <><PageIntro eyebrow="MARKET TIMELINE / SOURCE EVIDENCE" title="行情时间线">每个点都带交易所时间、接收时间、来源与状态。原始价用于操作，复权价只用于趋势；缺失、过期和冲突不补成正常。</PageIntro><div className="toolbar"><label>选择标的<select value={symbol} onChange={(event) => setSymbol(event.target.value)}>{summary.instruments.map((item) => <option key={item.symbol} value={item.symbol}>{item.label} · {item.symbol}</option>)}</select></label><StatusBadge status={status === 'ready' ? 'READY' : status === 'offline' ? 'MISSING' : 'UNKNOWN'} /></div><Panel title={`${timeline?.label || '行情'} · 最近 ${rows.length} 个证据点`} eyebrow="APPEND-ONLY OBSERVATIONS"><div className="chart-area">{values.length > 1 ? <Sparkline values={values} /> : <div className="empty-state">{status === 'loading' ? '读取时间线…' : '尚无足够的真实历史证据；请先点击“刷新真实行情”。'}</div>}</div><div className="table-scroll"><table><thead><tr><th>交易所时间</th><th>本地接收时间</th><th>来源</th><th>原始价</th><th>复权价</th><th>状态</th></tr></thead><tbody>{rows.slice().reverse().map((row, index) => <tr key={`${String(row.exchange_time)}-${index}`}><td>{formatTimestamp(row.exchange_time)}</td><td>{formatTimestamp(row.observed_at)}</td><td>{row.source}</td><td>{formatPrice(row.close)}</td><td>{formatPrice(row.adjusted_close)}</td><td><StatusBadge status={String(row.status)} /></td></tr>)}</tbody></table></div></Panel></>
}

function Sparkline({ values }: { values: number[] }) {
  const min = Math.min(...values); const max = Math.max(...values); const span = max - min || 1
  const points = values.map((value, index) => `${(index / Math.max(values.length - 1, 1)) * 100},${100 - ((value - min) / span) * 84 - 8}`).join(' ')
  return <div className="sparkline-wrap"><svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-label="价格趋势图"><polyline points={points} fill="none" vectorEffect="non-scaling-stroke" /></svg><div><span>低 {formatPrice(min)}</span><span>高 {formatPrice(max)}</span></div></div>
}

function TransmissionPage({ summary }: { summary: CoachSummary }) {
  const edges = [['SILVER', 'DXY'], ['SILVER', 'REAL10Y'], ['SILVER', 'GOLD'], ['GOLD', '801050.SI'], ['TIN', '801050.SI'], ['801050.SI', '000426.XSHE']] as const
  return <><PageIntro eyebrow="TRANSMISSION / EXPLAINABLE CHAIN" title="宏观—商品—板块—个股传导">关系图只展示允许的解释链，不把白银上涨直接等同于兴业银锡上涨。每一段都必须回到对应的真实证据。</PageIntro><Panel title="白银主线传导图" eyebrow="EVIDENCE GRAPH"><div className="transmission-graph"><div className="graph-column graph-column--macro"><GraphNode summary={summary} symbol="DXY" /><GraphNode summary={summary} symbol="REAL10Y" /><GraphNode summary={summary} symbol="TIP" /></div><div className="graph-arrow">影响<br />↓</div><div className="graph-column"><GraphNode summary={summary} symbol="SILVER" /><GraphNode summary={summary} symbol="GOLD" /><GraphNode summary={summary} symbol="TIN" /></div><div className="graph-arrow">传导<br />↓</div><div className="graph-column"><GraphNode summary={summary} symbol="801050.SI" /><GraphNode summary={summary} symbol="000426.XSHE" /></div></div><div className="graph-legend"><span><i className="legend-dot legend-dot--ready" />可用证据</span><span><i className="legend-dot legend-dot--missing" />缺失/过期</span><span><i className="legend-dot legend-dot--neutral" />仅关系，不代表方向</span></div></Panel><Panel title="本次解释依据" eyebrow="CAUSE → EFFECT"><div className="cause-list">{edges.map(([from, to]) => <div key={`${from}-${to}`}><b>{summary.instruments.find((item) => item.symbol === from)?.label || from}</b><span>→</span><b>{summary.instruments.find((item) => item.symbol === to)?.label || to}</b><small>{edgeText(summary, from, to)}</small></div>)}</div></Panel></>
}

function GraphNode({ summary, symbol }: { summary: CoachSummary; symbol: string }) { const item = instrument(summary, symbol); return <div className="graph-node"><div><span className={`graph-node__dot graph-node__dot--${statusTone(item?.selected.status)}`} /><b>{item?.label || symbol}</b></div><small>{item ? `${formatPrice(item.selected.close)} · ${statusLabel(item.selected.status)}` : '未记录'}</small></div> }
function edgeText(summary: CoachSummary, from: string, to: string) { const source = instrument(summary, from); const target = instrument(summary, to); if (!source || !target || source.selected.status !== 'READY' || target.selected.status !== 'READY') return '当前证据不足，不能确认这条传导在本时点成立。'; return '两端均有可用证据，仍需结合20日趋势和反证观察。' }

function HoldingsPage({ summary, onUpdated }: { summary: CoachSummary; onUpdated: () => void }) {
  const candidate = summary.account.candidate
  const confirmed = summary.account.confirmed
  const [form, setForm] = useState({ shares: String(candidate?.shares ?? ''), avg_cost: String(candidate?.avg_cost ?? ''), available_cash: String(candidate?.available_cash ?? ''), total_assets: String(candidate?.total_assets ?? ''), planned_cash_out: String(candidate?.planned_cash_out ?? 0), note: '' })
  const [message, setMessage] = useState('')
  const confirm = async (event: React.FormEvent) => { event.preventDefault(); try { await postCoach('account/confirm', { shares: Number(form.shares), avg_cost: Number(form.avg_cost), available_cash: Number(form.available_cash), total_assets: Number(form.total_assets), planned_cash_out: Number(form.planned_cash_out), note: form.note }); setMessage('账户已追加为当前事实；历史候选仍保留。'); onUpdated() } catch (error) { setMessage(error instanceof Error ? error.message : '确认失败') } }
  return <><PageIntro eyebrow="HOLDINGS / MANUAL FACT" title="持仓与成交">账户是独立事实层。截图值只能作为候选快照；确认后才会参与动态持仓区间。成交记录只接受人工录入，不发送订单。</PageIntro><div className="account-grid"><Panel title={confirmed ? '当前确认账户' : '当前账户'} eyebrow={confirmed ? 'CONFIRMED FACT' : 'NOT CURRENT FACT'}><AccountSummary snapshot={confirmed} pending={!confirmed} /></Panel><Panel title="候选快照" eyebrow="USER PLAN / PENDING"><AccountSummary snapshot={candidate} pending /></Panel></div><Panel title="当前估值与盈亏" eyebrow="RAW PRICE MARK / NO SYNTHETIC PNL"><FinancialSummary financials={summary.account.financials} /></Panel><Panel title="重新确认账户快照" eyebrow="EXPLICIT CONFIRMATION"><form className="account-form" onSubmit={confirm}><NumberField label="持仓股数（100股整数手）" value={form.shares} onChange={(value) => setForm({ ...form, shares: value })} /><NumberField label="当前成本（元）" value={form.avg_cost} onChange={(value) => setForm({ ...form, avg_cost: value })} /><NumberField label="可用现金（元）" value={form.available_cash} onChange={(value) => setForm({ ...form, available_cash: value })} /><NumberField label="总资产（元）" value={form.total_assets} onChange={(value) => setForm({ ...form, total_assets: value })} /><NumberField label="计划取出现金（元）" value={form.planned_cash_out} onChange={(value) => setForm({ ...form, planned_cash_out: value })} /><label className="field field--wide"><span>确认说明</span><textarea value={form.note} onChange={(event) => setForm({ ...form, note: event.target.value })} placeholder="例如：2026-08-29 收盘后核对账户截图" /></label><div className="form-actions"><button className="primary-button" type="submit">确认并追加账户事实</button><span className="muted">确认不会下单，也不会修改券商账户。</span></div>{message && <div className="form-message">{message}</div>}</form></Panel><Panel title="成交与执行记录" eyebrow="MANUAL EXECUTION LOG"><TradeTable trades={summary.trades} /></Panel></>
}

function AccountSummary({ snapshot, pending }: { snapshot: AccountSnapshot | null; pending?: boolean }) { if (!snapshot) return <div className="empty-state">尚未确认账户事实；没有任何零值结论。</div>; return <div className={`account-summary ${pending ? 'account-summary--pending' : ''}`}><div className="account-summary__status"><StatusBadge status={pending ? 'PENDING_USER_CONFIRMATION' : 'CONFIRMED'} /><small>{snapshot.source}</small></div><div className="account-values"><div><span>持仓</span><b>{snapshot.shares === null ? '—' : `${formatNumber(snapshot.shares, 0)} 股`}</b></div><div><span>成本</span><b>{snapshot.avg_cost === null ? '—' : `${formatPrice(snapshot.avg_cost)} 元`}</b></div><div><span>可用现金</span><b>{snapshot.available_cash === null ? '—' : `${formatNumber(snapshot.available_cash)} 元`}</b></div><div><span>总资产</span><b>{snapshot.total_assets === null ? '—' : `${formatNumber(snapshot.total_assets)} 元`}</b></div></div><p>{snapshot.confirmation_note || '未记录说明'}</p><small>记录时间：{formatTimestamp(snapshot.captured_at)}</small></div> }

function FinancialSummary({ financials }: { financials: CoachSummary['account']['financials'] }) {
  if (financials.status !== 'READY') return <div className="empty-state">{statusLabel(financials.status)}：当前原始价不可用，市值与未实现盈亏保持空值。{financials.reason_codes.length ? `（${financials.reason_codes.join(' / ')}）` : ''}</div>
  return <div className="financial-grid"><Metric label="最新原始价" value={`${formatPrice(financials.current_price)} 元`} detail={`${financials.source || '来源未记录'} · ${formatTimestamp(financials.price_exchange_time)}`} tone="ready" /><Metric label="持仓市值" value={`${formatNumber(financials.market_value)} 元`} detail={`${formatNumber(financials.open_shares, 0)} 股`} tone="neutral" /><Metric label="成本基准" value={`${formatNumber(financials.cost_basis)} 元`} detail="确认快照 + 人工成交" tone="neutral" /><Metric label="未实现盈亏" value={`${formatNumber(financials.unrealized_pnl)} 元`} detail="按最新原始价估值" tone={financials.unrealized_pnl !== null && financials.unrealized_pnl >= 0 ? 'ready' : 'risk'} /><Metric label="已实现盈亏" value={`${formatNumber(financials.realized_pnl)} 元`} detail="仅人工成交记录" tone="neutral" /><Metric label="计划取出现金" value={`${formatNumber(financials.planned_cash_out)} 元`} detail="已从加仓容量中扣除" tone="neutral" /></div>
}

function NumberField({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) { return <label className="field"><span>{label}</span><input type="number" min="0" step="any" value={value} onChange={(event) => onChange(event.target.value)} required /></label> }
function TradeTable({ trades }: { trades: Array<Record<string, unknown>> }) { if (!trades.length) return <div className="empty-state">尚无人工成交记录。</div>; return <div className="table-scroll"><table><thead><tr><th>时间</th><th>方向</th><th>股数</th><th>价格</th><th>状态</th><th>理由</th></tr></thead><tbody>{trades.map((trade, index) => <tr key={String(trade.id || index)}><td>{formatTimestamp(String(trade.recorded_at || ''))}</td><td>{String(trade.side || '—')}</td><td>{String(trade.quantity || '—')}</td><td>{formatPrice(typeof trade.price === 'number' ? trade.price : null)}</td><td><StatusBadge status={String(trade.execution_status || 'UNKNOWN')} /></td><td>{String(trade.reason || '—')}</td></tr>)}</tbody></table></div> }

function AiMentorPanel({ summary, onUpdated }: { summary: CoachSummary; onUpdated: () => void }) {
  const [result, setResult] = useState<AiMentorResult | null>(null)
  const [working, setWorking] = useState(false)
  const [message, setMessage] = useState('')
  const status = result?.status || summary.ai.status
  const output = result?.structured_output || null
  const memoryCount = result?.memory_retrieval?.count ?? summary.ai.latest_run?.memory_ids?.length ?? 0
  const request = async () => {
    setWorking(true)
    setMessage('正在读取长期记忆并对来源做联网可达性核验；AI 只能解释，不能改写规则动作。')
    try {
      const next = await postCoach<AiMentorResult>('ai/mentor', { verify_sources: true })
      setResult(next)
      onUpdated()
      setMessage(next.is_ai ? 'AI 导师观点已返回并写入审计日记。' : `AI 未生成观点：${next.reason_codes?.join(' / ') || statusLabel(next.status)}。`)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'AI 请求失败；确定性建议未被改变。')
    } finally { setWorking(false) }
  }
  const validation = result?.cross_validation
  const liveCallText = result
    ? (result.is_ai ? '本次真实调用成功' : `本次调用未成功：${statusLabel(result.status)}`)
    : (summary.ai.last_call_succeeded
      ? '最近一次真实调用成功'
      : summary.ai.latest_run
        ? `最近一次调用未成功：${statusLabel(summary.ai.latest_call_status || summary.ai.latest_run.status)}`
        : '尚未进行真实调用')
  return <Panel title="可选 AI 导师观点" eyebrow="AI PROVIDER / EXPLANATION ONLY" className="ai-panel"><div className="ai-status-row"><StatusBadge status={status} /><span>{summary.ai.configured ? `已配置首选：${result?.selected_provider || summary.ai.selected_provider || summary.ai.provider} · 模型：${result?.model || summary.ai.model || '待模型核验'} · ${liveCallText}` : 'DeepSeek 优先、MiMo 备用均未配置；不会把规则模板冒充为 AI。'}</span></div><p className="ai-disclaimer">“已配置”不等于“AI 正常”。只有真实请求返回合法结构化结果才记为成功。先执行确定性规则、风险门槛和账户确认，再读取本地长期记忆；可选 AI 只负责解释已有证据，不能改写动作、仓位区间或清仓门槛。当前记忆检索：{summary.ai.memory_retrieval}，本次已取 {memoryCount} 条。</p><div className="ai-actions"><button className="primary-button" disabled={working} onClick={() => void request()}>{working ? '核验与请求中…' : '联网核验并请求 AI 导师观点'}</button>{summary.ai.reason_codes.length > 0 && <span className="muted">原因：{summary.ai.reason_codes.join(' / ')}</span>}</div>{message && <div className="form-message" role="status">{message}</div>}{output ? <div className="ai-result"><div className="ai-result__head"><b>{output.summary}</b><StatusBadge status={output.confidence} /></div><div className="ai-result-grid"><div><span>支持因素</span><ListItems items={output.drivers} /></div><div><span>风险</span><ListItems items={output.risks} /></div><div><span>反证</span><ListItems items={output.counter_evidence} /></div><div><span>待回答问题</span><ListItems items={output.questions} /></div></div><p><b>不确定性：</b>{output.uncertainty}</p><p><b>规则引用：</b>{output.rule_action_reference}</p><small>来源引用：{output.source_references.length ? output.source_references.join(' · ') : '未返回'} · ai_run #{result?.ai_run_id ?? '未记录'}</small></div> : <div className="ai-empty"><strong>{status === 'NOT_CONFIGURED' ? 'AI 未配置（NOT_CONFIGURED）' : `AI 状态：${statusLabel(status)}`}</strong><p>{status === 'NOT_CONFIGURED' ? '配置 DEEPSEEK_API_KEY 和（必要时）MIMO_API_KEY 后才会调用真实接口；无配置时请求只会写入 fail-closed 审计，不会产生虚构观点。' : '点击上方按钮后，系统会显示结构化观点或明确的失败原因。'}</p></div>}{validation && <small className="ai-validation">联网核验：{String(validation.status || '未记录')} · 结果仍需人工判断，不构成行情事实。</small>}</Panel>
}

function AdvicePage({ summary, onUpdated }: { summary: CoachSummary; onUpdated: () => void }) {
  const [message, setMessage] = useState('')
  const [text, setText] = useState('')
  const addDecision = async () => { if (!text.trim()) return; try { await postCoach('diary', { layer: '实际决定', content: { text: text.trim(), advice_action: summary.advice.action, execution_status: 'PLANNED', automatic_trading: false } }); setText(''); setMessage('人工决定已追加到不可覆盖日记。'); onUpdated() } catch (error) { setMessage(error instanceof Error ? error.message : '记录失败') } }
  return <><PageIntro eyebrow="ADVICE / EXECUTION SEPARATION" title="建议与执行">建议由事实层和确定性规则生成；实际决定与成交是另一条不可覆盖记录。没有真实证据时，动作保持等待。AI 观点若已配置，会在下方单独显示，不能覆盖这张规则建议卡。</PageIntro><div className="advice-layout"><Panel title="当前操作卡" eyebrow="DETERMINISTIC RULES / NOT AI"><AdviceCard summary={summary} /></Panel><Panel title="风险与清仓门槛" eyebrow="MAJOR RISK GATE"><div className={`risk-card risk-card--${statusTone(summary.risk.status)}`}><StatusBadge status={summary.risk.status} /><h4>{summary.risk.label}</h4><p>{summary.risk.rule}</p><div className="risk-facts"><span>VPS事实：<b>{statusLabel(summary.vps.status)}</b></span><span>Prediction Gate：<b>{statusLabel(summary.vps.prediction_gate_status)}</b></span><span>事件日历：<b>{summary.vps.macro_event_gate || '未记录'}</b></span></div><ListItems items={summary.vps.reason_codes} /></div></Panel></div><AiMentorPanel summary={summary} onUpdated={onUpdated} /><Panel title="记录我的决定" eyebrow="HUMAN DECISION"><div className="decision-form"><textarea value={text} onChange={(event) => setText(event.target.value)} placeholder="例如：我选择观察，不执行本次分批减仓建议；原因……" /><button className="primary-button" onClick={() => void addDecision()}>追加决定（不下单）</button>{message && <span className="form-message">{message}</span>}</div></Panel><Panel title="确定性导师解释链" eyebrow="RULE AUDIT / NOT AI"><MentorChain steps={summary.deterministic_mentor_chain || summary.mentor_chain} /></Panel></>
}

function NarrativePage({ summary, onUpdated }: { summary: CoachSummary; onUpdated: () => void }) {
  const [memory, setMemory] = useState(''); const [kind, setKind] = useState('MARKET_VIEW'); const [message, setMessage] = useState('')
  const addMemory = async () => { if (!memory.trim()) return; try { await postCoach('diary', { layer: '长期记忆', content: { kind, text: memory.trim(), version: 'candidate-v0.1', approval_required_for_rule_change: true } }); setMemory(''); setMessage('已追加到长期日记；正式阈值不会被自动改变。'); onUpdated() } catch (error) { setMessage(error instanceof Error ? error.message : '记录失败') } }
  const narrative = summary.narrative
  return <><PageIntro eyebrow="CONTINUOUS STORY / MEMORY" title="长期市场叙事">本页叙事由确定性事实与规则连续生成，并读取上一份判断；可选 AI 导师观点只在“建议与执行”页单独呈现。长期记忆可以增长，但正式规则改变必须另行审批。</PageIntro><Panel title="当前持续叙事" eyebrow={`NARRATIVE #${narrative.prior_narrative_id ? narrative.prior_narrative_id + 1 : 1}`}><div className="story-head"><StatusBadge status={narrative.evidence_status} /><span>更新：{formatTimestamp(narrative.generated_at)}</span></div><h3>{narrative.summary}</h3><div className="story-grid"><StoryColumn title="原判断 / 未变化" items={[...(narrative.original_judgement ? [narrative.original_judgement] : []), ...narrative.unchanged]} tone="neutral" /><StoryColumn title="新事实" items={narrative.new_facts} tone="ready" /><StoryColumn title="被证实" items={narrative.affirmed} tone="ready" /><StoryColumn title="被证伪" items={narrative.falsified} tone="risk" /></div></Panel><div className="memory-grid"><Panel title="永久记忆" eyebrow="APPEND-ONLY MEMORY"><div className="memory-list">{summary.memories.length ? summary.memories.map((item, index) => <div key={String(item.id || index)}><span>{String(item.kind || '未分类')}</span><p>{String(item.content || item.text || '—')}</p><small>{formatTimestamp(String(item.recorded_at || item.effective_at || ''))} · {String(item.source || 'SYSTEM')}</small></div>) : <div className="empty-state">尚无长期记忆。</div>}</div></Panel><Panel title="追加一条观察" eyebrow="USER MEMORY"><div className="memory-form"><label className="field"><span>记忆类型</span><select value={kind} onChange={(event) => setKind(event.target.value)}><option value="MARKET_VIEW">市场看法</option><option value="THESIS">长期逻辑</option><option value="HABIT">交易习惯</option><option value="HYPOTHESIS">待验证假设</option><option value="EXECUTION">执行经验</option></select></label><label className="field field--wide"><span>内容</span><textarea value={memory} onChange={(event) => setMemory(event.target.value)} placeholder="记录可长期复用的判断或习惯" /></label><button className="primary-button" onClick={() => void addMemory()}>追加记忆</button>{message && <span className="form-message">{message}</span>}</div></Panel></div></>
}

function StoryColumn({ title, items, tone }: { title: string; items: string[]; tone: string }) { return <div className={`story-column story-column--${tone}`}><h4>{title}</h4><ListItems items={items} /></div> }

function ReviewPage({ summary }: { summary: CoachSummary }) {
  const diary = summary.diary
  const layers = ['市场事实', '模式判断', '导师判断', '实际决定', '实际成交']
  return <><PageIntro eyebrow="REVIEW / GROWTH" title="复盘与成长">复盘只评估建议与实际结果，不偷偷改变正式规则。完整的20个交易日前瞻期尚未完成，因此这里不显示伪造收益或胜率。</PageIntro><div className="metric-row"><Metric label="前瞻验证" value="0 / 20 个交易日" detail="等待真实连续运行" /><Metric label="规则版本" value="v0.1" detail="候选改变需用户审批" /><Metric label="日记链" value={`${diary.length} 条`} detail="追加式记录" tone={diary.length ? 'ready' : 'neutral'} /><Metric label="可归因结果" value="待成交" detail="无假收益" /></div><Panel title="五层交易日记" eyebrow="REPLAYABLE JOURNAL"><div className="layer-rail">{layers.map((layer, index) => <div key={layer} className={diary.some((item) => item.layer === layer) ? 'is-recorded' : ''}><span>{index + 1}</span><b>{layer}</b><small>{diary.some((item) => item.layer === layer) ? '已有记录' : '等待记录'}</small></div>)}</div><div className="diary-list">{diary.length ? diary.map((item) => <DiaryRow key={item.record_hash} item={item} />) : <div className="empty-state">尚无可复盘日记。</div>}</div></Panel><Panel title="可学习但不可自改" eyebrow="GOVERNANCE"><div className="governance-grid"><div><b>允许自动做的事</b><ListItems items={['复盘已完成的判断', '发现重复错误并形成候选假设', '评估建议与实际执行偏差']} /></div><div><b>必须人工批准</b><ListItems items={['正式阈值和仓位上限改变', '候选规则投入正式建议', '任何清仓级风险结论']} /></div></div></Panel></>
}

function DiaryRow({ item }: { item: DiaryRecord }) { return <div className="diary-row"><div><StatusBadge status="READY" /><b>{item.layer}</b><span>{formatTimestamp(item.event_time)}</span></div><p>{Object.entries(item.content).filter(([key]) => !['market_facts', 'stock_state', 'advice'].includes(key)).map(([key, value]) => `${key}: ${typeof value === 'object' ? JSON.stringify(value) : String(value)}`).join(' · ') || '结构化判断记录'}</p><small>hash {item.record_hash.slice(0, 12)}… · prev {item.prev_hash ? `${item.prev_hash.slice(0, 12)}…` : 'GENESIS'}</small></div> }

function NotificationPanel({ summary, onUpdated }: { summary: CoachSummary; onUpdated: () => void }) {
  const [working, setWorking] = useState(false)
  const [result, setResult] = useState<{ status?: string; reason_codes?: string[]; audit_id?: number } | null>(null)
  const [message, setMessage] = useState('')
  const notification: NotificationStatus = summary.notification
  const test = async () => {
    setWorking(true)
    setMessage('正在向已配置的真实 webhook 发送一次人工测试事件；测试不会代表交易信号。')
    try {
      const next = await postCoach<{ status?: string; reason_codes?: string[]; audit_id?: number }>('notifications/test', {})
      setResult(next)
      onUpdated()
      setMessage(next.status === 'DELIVERED' ? '通知已送达并写入审计。' : `通知未送达：${next.reason_codes?.join(' / ') || statusLabel(next.status)}`)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '通知测试失败；未影响交易规则。')
    } finally { setWorking(false) }
  }
  const latest = notification.delivery_audit[0]
  return <Panel title="真实提醒适配层" eyebrow="NOTIFICATION / WEBHOOK" className="notification-panel"><div className="ai-status-row"><StatusBadge status={result?.status || notification.status} /><span>{notification.configured ? `已配置 webhook · 目标主机：${notification.target_host || '未记录'}` : '未配置真实通知目标；网页收件箱不等于手机推送。'}</span></div><p className="ai-disclaimer">提醒适配器只发送事件状态和人工测试，不发送订单、不接券商。配置 <code>TRADE_COACH_NOTIFY_WEBHOOK_URL</code> 后才会尝试真实 HTTP POST；未配置时保持 <b>NOT_CONFIGURED</b>，不会伪称手机通知已完成。</p><div className="ai-actions"><button className="primary-button" disabled={working} onClick={() => void test()}>{working ? '测试发送中…' : '发送一次真实适配器测试'}</button>{notification.reason_codes.length > 0 && <span className="muted">原因：{notification.reason_codes.join(' / ')}</span>}</div>{message && <div className="form-message" role="status">{message}</div>}<small className="ai-validation">最近审计：{latest ? `${statusLabel(latest.status)} · ${formatTimestamp(latest.attempted_at)} · #${latest.id || result?.audit_id || '—'}` : '尚无发送记录'}</small></Panel>
}

function SourcesPage({ summary, onUpdated }: { summary: CoachSummary; onUpdated: () => void }) {
  return <><PageIntro eyebrow="PROVENANCE / HEALTH" title="数据来源与错误">来源、交易所语义、时间、新鲜度和故障原因全部可见。`EVENT_CALENDAR_UNAVAILABLE` 保持缺失，不被改成 GREEN。提醒适配器也在本页显示真实配置状态。</PageIntro><Panel title="事实层健康" eyebrow="SOURCE MATRIX"><div className="source-summary"><Metric label="行情模式证据" value={statusLabel(summary.market_regime.evidence_status)} detail={`${summary.market_regime.available_symbols.length} 个因子可用`} tone={statusTone(summary.market_regime.evidence_status)} /><Metric label="VPS风险事实" value={statusLabel(summary.vps.status)} detail={summary.vps.reason_codes.join(' / ') || '无故障码'} tone={statusTone(summary.vps.status)} /><Metric label="Prediction Gate" value={statusLabel(summary.vps.prediction_gate_status)} detail="只消费时点事实" tone={statusTone(summary.vps.prediction_gate_status)} /></div></Panel><NotificationPanel summary={summary} onUpdated={onUpdated} /><Panel title="全部标的来源" eyebrow="MARKET DATA CONTRACT"><div className="table-scroll"><table><thead><tr><th>标的</th><th>市场/交易所</th><th>提供方标识</th><th>采用源</th><th>行情时间</th><th>新鲜度</th><th>主备协调</th><th>原因</th></tr></thead><tbody>{summary.instruments.map((item) => <tr key={item.symbol}><td><b>{item.label}</b><small>{item.symbol}</small></td><td>{item.venue}</td><td><code>{item.provider_symbol}</code><small>{item.contract_semantics}</small></td><td>{item.selected.source || '未记录'}</td><td>{formatTimestamp(item.selected.exchange_time)}</td><td><StatusBadge status={item.selected.status} /></td><td>{item.reconciliation_status}</td><td>{item.selected.reason_codes.length ? item.selected.reason_codes.join(' / ') : '无'}</td></tr>)}</tbody></table></div></Panel><div className="source-detail-grid">{summary.instruments.map((item) => <SourceDetail key={item.symbol} item={item} />)}</div><Panel title="VPS受限事实链" eyebrow="EVENT CALENDAR / PREDICTION GATE / RISK"><div className="vps-card"><div><StatusBadge status={summary.vps.status} /><strong>{summary.vps.risk_level || '未发布风险等级'}</strong></div><div className="vps-values"><span>事件日历：<b>{summary.vps.macro_event_gate || 'MISSING'}</b></span><span>Prediction Gate：<b>{statusLabel(summary.vps.prediction_gate_status)}</b></span><span>有效至：<b>{formatTimestamp(summary.vps.valid_until)}</b></span></div><ListItems items={summary.vps.reason_codes} /><small>来源：{summary.vps.source_ref || '未配置本地 VPS 导出路径'}。本地消费不会修改 VPS。</small></div></Panel></>
}

function SourceDetail({ item }: { item: CoachInstrument }) { const fallback = item.fallback; return <article className="source-detail"><div><b>{item.label}</b><small>{item.symbol}</small></div><div className="source-detail__pair"><div><span>主源 · {item.primary.source || item.primary_source}</span><StatusBadge status={item.primary.status} /><small>{item.primary.reason_codes.join(' / ') || item.primary.source_ref || '无故障码'}</small></div><div><span>备源 · {item.backup.source || item.backup_source}</span><StatusBadge status={item.backup.status} /><small>{item.backup.reason_codes.join(' / ') || item.backup.source_ref || '无故障码'}</small></div></div>{fallback && <div className="source-detail__fallback"><span>最近可用 fallback · {fallback.source || '未记录'}</span><StatusBadge status={fallback.status} /><small>{formatTimestamp(fallback.exchange_time)} · {fallback.reason_codes.join(' / ') || '最新抓取失败时保留的历史值；不提升为 READY'}</small></div>}</article> }

export default App
