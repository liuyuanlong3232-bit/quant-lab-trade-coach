import type { KeyboardEvent, ReactNode } from 'react'
import { formatLatency, formatPrice, formatTimestamp, statusLabel, statusTone } from './data'
import type { Instrument, ProbeSource, ProbeStatus } from './types'

export function StatusGlyph({ status, label = true }: { status: ProbeStatus; label?: boolean }) {
  return <span className={`status-glyph status-glyph--${statusTone(status)}`}><i aria-hidden="true" />{label && <span>{statusLabel(status)}</span>}</span>
}

export function Panel({ children, className = '', eyebrow, title, action }: { children: ReactNode; className?: string; eyebrow?: string; title?: string; action?: ReactNode }) {
  return <section className={`panel ${className}`}>
    {(eyebrow || title || action) && <header className="panel__header"> <div>{eyebrow && <span className="panel__eyebrow">{eyebrow}</span>}{title && <h2>{title}</h2>}</div>{action}</header>}
    {children}
  </section>
}

export function Metric({ label, value, detail, tone = 'neutral' }: { label: string; value: string; detail?: string; tone?: 'ready' | 'risk' | 'missing' | 'neutral' }) {
  return <div className={`metric metric--${tone}`}><span className="metric__label">{label}</span><strong className="metric__value">{value}</strong>{detail && <span className="metric__detail">{detail}</span>}</div>
}

export function EvidenceStrip({ instrument, compact = false }: { instrument: Instrument; compact?: boolean }) {
  const selected = instrument.selected
  return <div className={`evidence-strip ${compact ? 'evidence-strip--compact' : ''}`}>
    <div className="evidence-strip__name"><StatusGlyph status={selected.status} label={false} /><span>{instrument.label}</span><small>{instrument.symbol}</small></div>
    <div><span className="data-label">采用源</span><b>{selected.source || '未记录'}</b></div>
    <div><span className="data-label">最新价</span><b>{formatPrice(selected.close)}</b></div>
    <div><span className="data-label">延迟</span><b>{formatLatency(selected.latency_ms)}</b></div>
    <div><span className="data-label">状态</span><StatusGlyph status={selected.status} /></div>
  </div>
}

export function VeinMap({ instruments, activeSymbol, onSelect }: { instruments: Instrument[]; activeSymbol?: string; onSelect?: (symbol: string) => void }) {
  const positions: Record<string, [number, number]> = { '000426.XSHE': [648, 102], '000960.XSHE': [648, 304], AG0: [116, 92], AU0: [116, 198], SN0: [116, 300], SC0: [370, 365] }
  const bySymbol = new Map(instruments.map((instrument) => [instrument.symbol, instrument]))
  const nodes = Object.entries(positions).map(([symbol, [x, y]]) => ({ symbol, x, y, instrument: bySymbol.get(symbol) }))
  return <div className="vein-map" role="img" aria-label="矿脉关系图：展示现有只读探针标的与因子节点">
    <svg viewBox="0 0 760 420" aria-hidden="true" preserveAspectRatio="none">
      <defs><linearGradient id="silver-vein" x1="0" x2="1"><stop stopColor="#9b9388" /><stop offset=".48" stopColor="#f0eee6" /><stop offset="1" stopColor="#81766e" /></linearGradient><linearGradient id="tin-vein" x1="0" x2="1"><stop stopColor="#7b6f65" /><stop offset=".52" stopColor="#e4d9ca" /><stop offset="1" stopColor="#786258" /></linearGradient><filter id="soft-glow"><feGaussianBlur stdDeviation="4" result="blur" /><feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge></filter></defs>
      <path className="vein-map__trace vein-map__trace--fine" d="M0 72 C120 70 160 125 270 119 S430 47 648 102" />
      <path className="vein-map__trace vein-map__trace--silver" d="M0 105 C96 102 140 188 255 161 S390 72 504 119 S570 103 648 102" filter="url(#soft-glow)" />
      <path className="vein-map__trace vein-map__trace--tin" d="M0 325 C122 316 179 262 290 285 S420 354 516 296 S591 300 648 304" filter="url(#soft-glow)" />
      <path className="vein-map__trace vein-map__trace--fine" d="M116 198 C222 194 260 218 332 235 S450 213 540 170" />
      <path className="vein-map__link" d="M116 92 C290 85 407 90 648 102 M116 300 C285 293 470 311 648 304 M370 365 C445 319 496 269 540 170" />
      {nodes.map(({ symbol, x, y, instrument }) => { const status = instrument?.selected.status || 'MISSING'; const activate = () => onSelect?.(symbol); const onKeyDown = (event: KeyboardEvent<SVGGElement>) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); activate(); } }; return <g key={symbol} className={`vein-node vein-node--${statusTone(status)} ${activeSymbol === symbol ? 'is-active' : ''}`} onClick={activate} onKeyDown={onKeyDown} role={onSelect ? 'button' : undefined} tabIndex={onSelect ? 0 : undefined} aria-label={onSelect ? `${instrument?.label || symbol} 节点` : undefined}><circle className="vein-node__halo" cx={x} cy={y} r={activeSymbol === symbol ? 23 : 17} /><circle className="vein-node__core" cx={x} cy={y} r="6" /><text x={x + 14} y={y - 8}>{instrument?.label || symbol}</text><text className="vein-node__code" x={x + 14} y={y + 10}>{symbol}</text></g> })}
    </svg>
    <div className="vein-map__legend"><span><i className="legend-dot legend-dot--silver" />贵金属</span><span><i className="legend-dot legend-dot--tin" />有色矿脉</span><span><i className="legend-dot legend-dot--factor" />联动因子</span></div>
  </div>
}

export function DisabledModule({ title, code, children }: { title: string; code: string; children: ReactNode }) {
  return <div className="disabled-module"><div className="disabled-module__mark">◇</div><div><div className="disabled-module__top"><span>{code}</span><b>未启用</b></div><h3>{title}</h3><p>{children}</p><small>等待 Sprint 2 · 仅展示产品边界，不代表已接入</small></div></div>
}

export function SourcePair({ primary, backup }: { primary: ProbeSource; backup: ProbeSource }) {
  return <div className="source-pair"><div><span>主源</span><b>{primary.source || '未记录'}</b><StatusGlyph status={primary.status} /></div><div><span>备源</span><b>{backup.source || '未记录'}</b><StatusGlyph status={backup.status} /></div></div>
}
